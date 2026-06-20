"""Tests for --disable-rate-limiter flag in benchmark tool."""

import math
import sys
import os
from unittest import TestCase, main as unittest_main

# Add tools dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from benchmark import (
    calculate_worker_rps,
    interval_for_worker_rps,
    RateLimiterBypassError,
)


class TestCalculateWorkerRps(TestCase):
    """Tests for calculate_worker_rps function."""

    def test_normal_mode_divides_rps(self):
        """Without flag, RPS is divided by concurrency."""
        result = calculate_worker_rps(100.0, 10, False)
        self.assertEqual(result, 10.0)

    def test_normal_mode_single_worker(self):
        """Single worker gets full RPS."""
        result = calculate_worker_rps(50.0, 1, False)
        self.assertEqual(result, 50.0)

    def test_disable_rate_limiter_returns_inf(self):
        """With flag, returns infinity (no pacing)."""
        result = calculate_worker_rps(100.0, 10, True)
        self.assertTrue(math.isinf(result))

    def test_disable_rate_limiter_ignores_total_rps(self):
        """With flag, total_rps value doesn't matter."""
        result = calculate_worker_rps(1.0, 1, True)
        self.assertTrue(math.isinf(result))

    def test_concurrency_below_one_raises(self):
        """Concurrency < 1 raises error."""
        with self.assertRaises(RateLimiterBypassError):
            calculate_worker_rps(100.0, 0, False)

    def test_negative_concurrency_raises(self):
        """Negative concurrency raises error."""
        with self.assertRaises(RateLimiterBypassError):
            calculate_worker_rps(100.0, -1, False)

    def test_zero_total_rps_raises(self):
        """Zero total RPS raises error when not disabled."""
        with self.assertRaises(RateLimiterBypassError):
            calculate_worker_rps(0.0, 10, False)

    def test_negative_total_rps_raises(self):
        """Negative total RPS raises error when not disabled."""
        with self.assertRaises(RateLimiterBypassError):
            calculate_worker_rps(-10.0, 10, False)

    def test_disable_with_zero_rps_does_not_raise(self):
        """With flag, zero total RPS does not raise (returns inf)."""
        result = calculate_worker_rps(0.0, 10, True)
        self.assertTrue(math.isinf(result))


class TestIntervalForWorkerRps(TestCase):
    """Tests for interval_for_worker_rps function."""

    def test_finite_rps_returns_interval(self):
        """Finite RPS returns correct interval."""
        result = interval_for_worker_rps(10.0)
        self.assertAlmostEqual(result, 0.1)

    def test_inf_rps_returns_zero(self):
        """Infinite RPS (disabled limiter) returns 0 delay."""
        result = interval_for_worker_rps(float("inf"))
        self.assertEqual(result, 0)

    def test_zero_rps_raises(self):
        """Zero worker RPS raises error."""
        with self.assertRaises(RateLimiterBypassError):
            interval_for_worker_rps(0.0)

    def test_negative_rps_raises(self):
        """Negative worker RPS raises error."""
        with self.assertRaises(RateLimiterBypassError):
            interval_for_worker_rps(-5.0)


class TestRateLimiterBypassIntegration(TestCase):
    """Integration tests for rate limiter bypass flow."""

    def test_bypass_flow_no_pacing(self):
        """Full bypass flow: disabled limiter → inf RPS → 0 interval."""
        worker_rps = calculate_worker_rps(100.0, 10, True)
        interval = interval_for_worker_rps(worker_rps)
        self.assertEqual(interval, 0)

    def test_normal_flow_has_pacing(self):
        """Normal flow: enabled limiter → finite RPS → positive interval."""
        worker_rps = calculate_worker_rps(100.0, 10, False)
        interval = interval_for_worker_rps(worker_rps)
        self.assertGreater(interval, 0)

    def test_bypass_with_different_concurrency(self):
        """Bypass returns inf regardless of concurrency."""
        for concurrency in [1, 5, 10, 50, 100]:
            worker_rps = calculate_worker_rps(1000.0, concurrency, True)
            self.assertTrue(
                math.isinf(worker_rps),
                f"Expected inf for concurrency={concurrency}",
            )


if __name__ == "__main__":
    unittest_main()
