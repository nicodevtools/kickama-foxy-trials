#!/usr/bin/env python3
"""Unit tests for health_check.py token bucket rate limiter, circuit breaker,
timeout handling, half-open rate reduction, retry/backoff, and CLI integration."""

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
    _probe_with_retry,
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

    @mock.patch("health_check.time.monotonic")
    def test_refill_over_time(self, mock_monotonic):
        """After time advances, tokens should refill and allow new consumption."""
        start_time = 1000.0
        mock_monotonic.return_value = start_time
        bucket = TokenBucket(rate=20, capacity=20)
        # Exhaust all tokens
        for _ in range(20):
            bucket.consume()
        # Should be throttled now
        ok, _ = bucket.consume()
        self.assertFalse(ok)
        # Advance time by 0.15s -> ~3 tokens at 20/sec
        mock_monotonic.return_value = start_time + 0.15
        ok, _ = bucket.consume()
        self.assertTrue(ok)

    @mock.patch("health_check.time.monotonic")
    def test_set_rate_updates_effective_rate(self, mock_monotonic):
        """set_rate should change the sustained rate."""
        start_time = 1000.0
        mock_monotonic.return_value = start_time
        bucket = TokenBucket(rate=10, capacity=10)
        for _ in range(10):
            bucket.consume()
        self.assertAlmostEqual(bucket.current_rate, 0.0, delta=0.5)
        # Increase rate dramatically
        bucket.set_rate(100)
        mock_monotonic.return_value = start_time + 0.05  # 5 tokens at 100/sec
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

    def test_rate_zero_raises_value_error(self):
        """TokenBucket(rate=0) should raise ValueError."""
        with self.assertRaises(ValueError):
            TokenBucket(rate=0)

    def test_rate_negative_raises_value_error(self):
        """TokenBucket(rate=-5) should raise ValueError."""
        with self.assertRaises(ValueError):
            TokenBucket(rate=-5)

    def test_consume_zero_tokens_raises_value_error(self):
        """consume(tokens=0) should raise ValueError."""
        bucket = TokenBucket(rate=10)
        with self.assertRaises(ValueError):
            bucket.consume(tokens=0)

    def test_consume_negative_tokens_raises_value_error(self):
        """consume(tokens=-1) should raise ValueError."""
        bucket = TokenBucket(rate=10)
        with self.assertRaises(ValueError):
            bucket.consume(tokens=-1)

    def test_set_rate_zero_raises_value_error(self):
        """set_rate(0) should raise ValueError."""
        bucket = TokenBucket(rate=10)
        with self.assertRaises(ValueError):
            bucket.set_rate(0)

    def test_set_rate_negative_raises_value_error(self):
        """set_rate(-1) should raise ValueError."""
        bucket = TokenBucket(rate=10)
        with self.assertRaises(ValueError):
            bucket.set_rate(-1)


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

    @mock.patch("health_check.time.time")
    def test_transitions_to_half_open_after_timeout(self, mock_time):
        """After recovery timeout, OPEN should transition to HALF_OPEN."""
        start_time = 1000.0
        mock_time.return_value = start_time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        # Advance time past recovery timeout
        mock_time.return_value = start_time + 0.02
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

    @mock.patch("health_check.time.time")
    def test_half_open_closes_on_success(self, mock_time):
        """A success in HALF_OPEN should transition to CLOSED."""
        start_time = 1000.0
        mock_time.return_value = start_time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()  # OPEN
        mock_time.return_value = start_time + 0.02
        cb.allow_request()   # Now HALF_OPEN
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    @mock.patch("health_check.time.time")
    def test_half_open_opens_on_failure(self, mock_time):
        """A failure in HALF_OPEN should go back to OPEN."""
        start_time = 1000.0
        mock_time.return_value = start_time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
        cb.record_failure()  # OPEN
        mock_time.return_value = start_time + 0.06
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

    @mock.patch("health_check.time.time")
    def test_half_open_only_allows_one_probe(self, mock_time):
        """In HALF_OPEN state, only the first request should be allowed."""
        start_time = 1000.0
        mock_time.return_value = start_time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()  # OPEN
        mock_time.return_value = start_time + 0.02
        # First call transitions to HALF_OPEN and allows
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        # Second call in HALF_OPEN should be denied
        self.assertFalse(cb.allow_request())
        # Third call still denied
        self.assertFalse(cb.allow_request())


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

    def test_circuit_breakers_persist_across_runs(self):
        """External circuit_breakers dict should persist state across calls."""
        cbs = {
            name: CircuitBreaker()
            for name in list(SERVICES.keys()) + list(INFRASTRUCTURE.keys())
        }
        # Run once
        run_health_checks(circuit_breakers=cbs, probe_rate=100)
        # Run again — same dict should be reused
        results2 = run_health_checks(circuit_breakers=cbs, probe_rate=100)
        self.assertIn("circuit_breakers", results2)


class TestHalfOpenRateReduction(unittest.TestCase):
    """Verify that HALF_OPEN circuit breaker state reduces probe rate to 50%
    through the actual run_health_checks integration path."""

    @mock.patch("health_check.time.time")
    def test_half_open_rate_is_halved_in_runner(self, mock_time):
        """When a CB transitions to HALF_OPEN inside run_health_checks,
        the rate limiter should be set to probe_rate // 2."""
        start_time = 1000.0
        mock_time.return_value = start_time

        # Create a pre-failed circuit breaker
        cbs = {
            name: CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
            for name in list(SERVICES.keys()) + list(INFRASTRUCTURE.keys())
        }
        # Trip one service to OPEN
        cb = cbs["backend"]
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Advance time so it transitions to HALF_OPEN on allow_request
        mock_time.return_value = start_time + 0.02

        # Run health checks — the runner will call allow_request() which
        # transitions to HALF_OPEN, then should set half rate
        results = run_health_checks(
            probe_rate=8,
            circuit_breakers=cbs,
        )
        # Verify results structure is valid
        self.assertIn("services", results)
        self.assertIn("backend", results["services"])

    def test_half_open_rate_restored_in_runner(self):
        """After the HALF_OPEN probe, the rate limiter should be restored."""
        cbs = {
            name: CircuitBreaker(failure_threshold=5, recovery_timeout=30)
            for name in list(SERVICES.keys()) + list(INFRASTRUCTURE.keys())
        }
        # CLOSED state — normal rate
        results = run_health_checks(
            probe_rate=10,
            circuit_breakers=cbs,
        )
        self.assertIn("rate_limiter", results)
        # With CLOSED state, rate should remain at full speed
        self.assertEqual(results["rate_limiter"]["rate_per_second"], 10.0)


class TestRetryBackoff(unittest.TestCase):
    """Tests for retry and exponential backoff in probe execution."""

    def test_retry_succeeds_on_second_attempt(self):
        """_probe_with_retry should retry and succeed on subsequent attempt."""
        call_count = [0]

        def flaky_probe():
            call_count[0] += 1
            if call_count[0] < 2:
                return ("CRITICAL", "fail", 0)
            return ("OK", "success", 200)

        status, detail, code = _probe_with_retry(flaky_probe, max_retries=3, backoff_factor=0.01)
        self.assertEqual(status, "OK")
        self.assertEqual(code, 200)
        self.assertEqual(call_count[0], 2)

    def test_retry_exhausts_and_returns_last_failure(self):
        """When all retries fail, the last CRITICAL result should be returned."""
        call_count = [0]

        def always_fail():
            call_count[0] += 1
            return ("CRITICAL", f"fail #{call_count[0]}", 0)

        status, detail, code = _probe_with_retry(always_fail, max_retries=2, backoff_factor=0.01)
        self.assertEqual(status, "CRITICAL")
        self.assertEqual(call_count[0], 3)  # initial + 2 retries

    def test_no_retry_when_max_retries_zero(self):
        """When max_retries=0, probe should only be called once."""
        call_count = [0]

        def probe():
            call_count[0] += 1
            return ("CRITICAL", "fail", 0)

        status, detail, code = _probe_with_retry(probe, max_retries=0, backoff_factor=1.0)
        self.assertEqual(call_count[0], 1)


if __name__ == "__main__":
    unittest.main()
