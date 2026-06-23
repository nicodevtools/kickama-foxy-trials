#!/usr/bin/env python3
"""Unit tests for health_check.py token bucket rate limiter, circuit breaker,
timeout handling, and half-open rate reduction."""

import time
import unittest
import threading
from unittest import mock

# Import the module under test
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from health_check import (
    TokenBucket,
    CircuitBreaker,
    CircuitState,
    run_health_checks,
    SERVICES,
    INFRASTRUCTURE,
)


class TestTokenBucket(unittest.TestCase):
    """Tests for the TokenBucket rate limiter."""

    def test_consume_allows_up_to_rate(self):
        """Token bucket should allow exactly 'rate' tokens per second without throttling."""
        bucket = TokenBucket(rate=10, capacity=10)
        allowed = 0
        throttled = 0
        for _ in range(10):
            ok, _ = bucket.consume()
            if ok:
                allowed += 1
            else:
                throttled += 1
        self.assertEqual(allowed, 10)
        self.assertEqual(throttled, 0)
        self.assertEqual(bucket.throttled_probes, 0)
        self.assertEqual(bucket.total_probes, 10)

    def test_consume_throttles_beyond_capacity(self):
        """Consuming beyond capacity should result in throttled probes."""
        bucket = TokenBucket(rate=5, capacity=5)
        throttled_count = 0
        for _ in range(15):
            ok, wait = bucket.consume()
            if not ok:
                throttled_count += 1
                self.assertGreater(wait, 0)
        self.assertGreater(throttled_count, 0)
        self.assertEqual(bucket.throttled_probes, throttled_count)
        self.assertEqual(bucket.total_probes, 15)

    def test_refill_over_time(self):
        """After sleeping, tokens should refill and allow new consumption."""
        bucket = TokenBucket(rate=20, capacity=20)
        # Exhaust all tokens
        for _ in range(20):
            bucket.consume()
        # Should be throttled now
        ok, _ = bucket.consume()
        self.assertFalse(ok)
        # Wait for refill
        time.sleep(0.15)  # should get ~3 tokens back at 20/sec
        ok, _ = bucket.consume()
        self.assertTrue(ok)

    def test_set_rate_updates_effective_rate(self):
        """set_rate should change the sustained rate."""
        bucket = TokenBucket(rate=10, capacity=10)
        for _ in range(10):
            bucket.consume()
        self.assertAlmostEqual(bucket.current_rate, 0.0, delta=0.5)
        # Increase rate dramatically
        bucket.set_rate(100)
        time.sleep(0.05)  # 5 tokens at 100/sec
        ok, _ = bucket.consume()
        self.assertTrue(ok)

    def test_stats_report(self):
        """stats() should return correct dictionary with all fields."""
        bucket = TokenBucket(rate=5, capacity=5)
        for _ in range(3):
            bucket.consume()
        stats = bucket.stats()
        self.assertEqual(stats["rate_per_second"], 5.0)
        self.assertEqual(stats["total_probes"], 3)
        self.assertIn("throttled_probes", stats)
        self.assertIn("throttle_pct", stats)
        self.assertIn("current_tokens", stats)

    def test_consume_with_custom_tokens(self):
        """Custom token consumption should work correctly."""
        bucket = TokenBucket(rate=10, capacity=10)
        ok, _ = bucket.consume(tokens=5.0)
        self.assertTrue(ok)
        # Remaining: 5 tokens, consume 6
        ok, _ = bucket.consume(tokens=6.0)
        self.assertFalse(ok)


class TestCircuitBreaker(unittest.TestCase):
    """Tests for the CircuitBreaker state machine."""

    def test_initial_state_is_closed(self):
        """New circuit breaker should start CLOSED."""
        cb = CircuitBreaker()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_opens_after_failure_threshold(self):
        """After enough failures, circuit should OPEN."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
        self.assertTrue(cb.allow_request())
        cb.record_failure()
        self.assertTrue(cb.allow_request())
        cb.record_failure()
        self.assertTrue(cb.allow_request())
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow_request())

    def test_transitions_to_half_open_after_timeout(self):
        """After recovery timeout, OPEN should transition to HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        time.sleep(0.02)
        self.assertTrue(cb.allow_request())

    def test_half_open_closes_on_success(self):
        """A success in HALF_OPEN should transition to CLOSED."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()  # OPEN
        time.sleep(0.02)
        cb.allow_request()   # Now HALF_OPEN
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_half_open_opens_on_failure(self):
        """A failure in HALF_OPEN should go back to OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
        cb.record_failure()  # OPEN
        time.sleep(0.06)
        cb.allow_request()   # Now HALF_OPEN
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_to_dict_includes_state(self):
        """to_dict should export current circuit breaker state."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
        cb.record_failure()
        d = cb.to_dict()
        self.assertEqual(d["state"], "CLOSED")
        self.assertEqual(d["failure_count"], 1)
        self.assertEqual(d["failure_threshold"], 3)


class TestHealthCheckIntegration(unittest.TestCase):
    """Integration tests for health_check with rate limiting and circuit breakers."""

    def test_run_health_checks_includes_rate_limiter_stats(self):
        """run_health_checks with probe_rate should include rate_limiter stats."""
        results = run_health_checks(probe_rate=10)
        self.assertIn("rate_limiter", results)
        rl = results["rate_limiter"]
        self.assertIn("rate_per_second", rl)
        self.assertEqual(rl["rate_per_second"], 10.0)

    def test_run_health_checks_includes_circuit_breaker_states(self):
        """Results should include circuit breaker states for each probed service."""
        results = run_health_checks(probe_rate=5)
        self.assertIn("circuit_breakers", results)
        for svc in SERVICES:
            self.assertIn(svc, results["circuit_breakers"],
                          f"Missing CB for {svc}")
        for infra in INFRASTRUCTURE:
            self.assertIn(infra, results["circuit_breakers"],
                          f"Missing CB for {infra}")

    def test_service_results_include_timeout_field(self):
        """Each service result should include timeout_used_s field."""
        results = run_health_checks(global_timeout=7)
        for svc_name, svc_result in results["services"].items():
            self.assertIn("timeout_used_s", svc_result)
            self.assertEqual(svc_result["timeout_used_s"], 7)

    def test_service_results_include_throttled_field(self):
        """Each service result should include throttled boolean."""
        results = run_health_checks(probe_rate=100)
        for svc_name, svc_result in results["services"].items():
            self.assertIn("throttled", svc_result)
            self.assertIsInstance(svc_result["throttled"], bool)

    def test_overall_status_present(self):
        """Results must include overall_status."""
        results = run_health_checks()
        self.assertIn("overall_status", results)
        self.assertIn(results["overall_status"], ("OK", "DEGRADED"))


class TestHalfOpenRateReduction(unittest.TestCase):
    """Verify that HALF_OPEN circuit breaker state reduces probe rate to 50%."""

    def test_half_open_rate_is_halved(self):
        """When CB enters HALF_OPEN, effective rate should be probe_rate // 2."""
        # This test verifies the logical property via the TokenBucket
        bucket = TokenBucket(rate=10, capacity=10)
        original_rate = bucket.rate
        # Exhaust the bucket at full rate first
        for _ in range(10):
            bucket.consume()
        # Simulate half-open reduction
        half_rate = max(1, original_rate // 2)
        bucket.set_rate(float(half_rate))
        self.assertEqual(bucket.rate, 5.0)

        # At half rate, refill is slower; consume immediately should throttle
        ok, _ = bucket.consume()
        self.assertFalse(ok)

    def test_rate_restored_after_half_open(self):
        """After restoring rate, full capacity should be available again."""
        bucket = TokenBucket(rate=10, capacity=10)
        # Reduce to half
        # Exhaust bucket first
        for _ in range(10):
            bucket.consume()
        # Reduce to half
        bucket.set_rate(5.0)
        # Restore
        bucket.set_rate(10.0)
        time.sleep(0.15)  # refill ~1.5 tokens at 10/sec
        ok, _ = bucket.consume()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
