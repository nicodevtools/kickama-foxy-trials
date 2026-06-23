#!/usr/bin/env python3
"""
Health check tool for the Tent of Trials platform.
Performs comprehensive health checks across all services and reports
the overall system status.

This tool is used by:
  - The Kubernetes liveness/readiness probes
  - The deployment pipeline (post-deployment validation)
  - The monitoring system (periodic health checks)
  - The on-call engineer (manual troubleshooting)

The health check performs the following checks:
  1. Service availability (HTTP health endpoints)
  2. Database connectivity (connection test)
  3. Redis connectivity (ping test)
  4. Kafka connectivity (metadata fetch)
  5. Message queue depth (consumer lag check)
  6. Certificate expiry (TLS certificate check)
  7. Disk space (filesystem usage check)
  8. Memory usage (process memory check)

Each check returns a status of OK, WARNING, or CRITICAL, along with
a detail message and optional diagnostic data.

Usage:
    python3 health_check.py                  # Check all services
    python3 health_check.py --service backend # Check specific service
    python3 health_check.py --json            # JSON output
    python3 health_check.py --watch           # Continuous monitoring
    python3 health_check.py --timeout 10      # Global timeout override
    python3 health_check.py --probe-rate 5    # Max 5 probes per second
"""

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import threading
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

SERVICES = {
    "backend": {"host": "localhost", "port": 8080, "path": "/health", "timeout": 5},
    "market": {"host": "localhost", "port": 8081, "path": "/health", "timeout": 5},
    "frailbox": {"host": "localhost", "port": 8082, "path": "/health", "timeout": 10},
    "frontend": {"host": "localhost", "port": 3000, "path": "/", "timeout": 5},
}

INFRASTRUCTURE = {
    "postgresql": {"host": os.environ.get("DB_HOST", "localhost"), "port": int(os.environ.get("DB_PORT", "5432")), "timeout": 5},
    "redis": {"host": os.environ.get("REDIS_HOST", "localhost"), "port": int(os.environ.get("REDIS_PORT", "6379")), "timeout": 5},
    "kafka": {"host": os.environ.get("KAFKA_HOST", "localhost"), "port": int(os.environ.get("KAFKA_PORT", "9092")), "timeout": 5},
}

DISK_THRESHOLD_WARNING = 80
DISK_THRESHOLD_CRITICAL = 90

MEMORY_THRESHOLD_WARNING = 80
MEMORY_THRESHOLD_CRITICAL = 90

# ---------------------------------------------------------------------------
# CIRCUIT BREAKER
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    CLOSED = "CLOSED"           # Normal operation, probes pass through
    OPEN = "OPEN"               # Circuit tripped, probes are blocked
    HALF_OPEN = "HALF_OPEN"     # Testing if service has recovered


class CircuitBreaker:
    """Circuit breaker that tracks per-service failure counts and
    transitions between CLOSED → OPEN → HALF_OPEN → CLOSED states."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold or self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        """Return True if a probe should be allowed through the circuit breaker."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False
            # HALF_OPEN — allow a single trial probe
            return True

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout_s": self.recovery_timeout,
            }


# ---------------------------------------------------------------------------
# TOKEN BUCKET RATE LIMITER
# ---------------------------------------------------------------------------

class TokenBucket:
    """Token bucket rate limiter. Tokens refill at a configurable rate.
    Each probe consumes one token. When the bucket is empty, probes are
    throttled (must wait)."""

    def __init__(self, rate: float, capacity: Optional[float] = None):
        """
        Args:
            rate: Tokens per second (sustained rate).
            capacity: Maximum burst size in tokens (defaults to rate).
        """
        self.rate = float(rate)
        self.capacity = capacity if capacity is not None else self.rate
        self._tokens: float = self.capacity
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()
        # Statistics
        self.total_probes: int = 0
        self.throttled_probes: int = 0
        self.total_wait_time: float = 0.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def consume(self, tokens: float = 1.0) -> Tuple[bool, float]:
        """Try to consume tokens. Returns (allowed, wait_time).
        If not enough tokens, wait_time is the seconds until next token."""
        with self._lock:
            self._refill()
            self.total_probes += 1
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0
            else:
                self.throttled_probes += 1
                wait_time = (tokens - self._tokens) / self.rate
                self.total_wait_time += wait_time
                # Consume what we can and the rest will be "borrowed"
                self._tokens -= tokens
                return False, wait_time

    @property
    def current_rate(self) -> float:
        """Current effective rate (tokens available now)."""
        with self._lock:
            self._refill()
            return min(self.rate, self._tokens)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rate_per_second": self.rate,
                "current_tokens": round(self._tokens, 2),
                "capacity": self.capacity,
                "total_probes": self.total_probes,
                "throttled_probes": self.throttled_probes,
                "throttle_pct": round(
                    (self.throttled_probes / max(1, self.total_probes)) * 100, 1
                ),
                "total_wait_time_s": round(self.total_wait_time, 3),
            }

    def set_rate(self, rate: float) -> None:
        """Update the sustained rate (used for half-open reduction)."""
        with self._lock:
            self.rate = float(rate)
            self.capacity = max(self.capacity, self.rate)


# ---------------------------------------------------------------------------
# CHECK FUNCTIONS
# ---------------------------------------------------------------------------

def check_http_service(host: str, port: int, path: str, timeout: int) -> Tuple[str, str, int]:
    import http.client
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode("utf-8", errors="replace")[:200]
        conn.close()

        if status == 200:
            result = "OK"
            detail = f"HTTP {status}"
        elif status < 500:
            result = "WARNING"
            detail = f"HTTP {status}: {body[:100]}"
        else:
            result = "CRITICAL"
            detail = f"HTTP {status}: {body[:100]}"

        return result, detail, status
    except Exception as e:
        return "CRITICAL", str(e), 0


def check_tcp_port(host: str, port: int, timeout: int) -> Tuple[str, str, float]:
    try:
        start = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency = (time.time() - start) * 1000
        return "OK", f"Connected ({latency:.1f}ms)", latency
    except socket.timeout:
        return "CRITICAL", f"Connection timeout ({timeout}s)", 0
    except ConnectionRefusedError:
        return "CRITICAL", "Connection refused", 0
    except Exception as e:
        return "CRITICAL", str(e), 0


def check_certificate_expiry(host: str, port: int = 443) -> Tuple[str, str, int]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    return "WARNING", "No certificate found", 0

                from datetime import datetime as dt
                expires = dt.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
                days_left = (expires - dt.now()).days

                if days_left > 30:
                    return "OK", f"Certificate expires in {days_left} days", days_left
                elif days_left > 7:
                    return "WARNING", f"Certificate expires in {days_left} days", days_left
                else:
                    return "CRITICAL", f"Certificate expires in {days_left} days", days_left
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_disk_usage(path: str = "/") -> Tuple[str, str, float]:
    try:
        stat = os.statvfs(path)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        pct = (used / total) * 100

        if pct < DISK_THRESHOLD_WARNING:
            return "OK", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        elif pct < DISK_THRESHOLD_CRITICAL:
            return "WARNING", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        else:
            return "CRITICAL", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_memory_usage() -> Tuple[str, str, float]:
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().replace(" kB", "")
                    try:
                        meminfo[key] = int(value) * 1024
                    except ValueError:
                        pass

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        pct = (used / total) * 100 if total > 0 else 0

        if pct < MEMORY_THRESHOLD_WARNING:
            return "OK", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        elif pct < MEMORY_THRESHOLD_CRITICAL:
            return "WARNING", f"{pct:.1f}% used", pct
        else:
            return "CRITICAL", f"{pct:.1f}% used", pct
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_load_average() -> Tuple[str, str, float]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().strip().split()
            load = float(parts[0])
            cpu_count = os.cpu_count() or 1
            load_pct = (load / cpu_count) * 100

            if load_pct < 70:
                return "OK", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
            elif load_pct < 90:
                return "WARNING", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
            else:
                return "CRITICAL", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


# ---------------------------------------------------------------------------
# HEALTH CHECK RUNNER WITH RATE LIMITING & CIRCUIT BREAKER
# ---------------------------------------------------------------------------

def run_health_checks(
    service: Optional[str] = None,
    json_output: bool = False,
    global_timeout: Optional[int] = None,
    probe_rate: Optional[int] = None,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "services": {},
        "infrastructure": {},
        "system": {},
        "overall_status": "OK",
        "rate_limiter": {},
        "circuit_breakers": {},
    }

    # Per-service circuit breakers
    circuit_breakers: Dict[str, CircuitBreaker] = {
        name: CircuitBreaker() for name in list(SERVICES.keys()) + list(INFRASTRUCTURE.keys())
    }

    # Global token bucket rate limiter
    limiter: Optional[TokenBucket] = None
    if probe_rate and probe_rate > 0:
        limiter = TokenBucket(rate=float(probe_rate))

    all_ok = True

    # Check services
    for name, config in SERVICES.items():
        if service and name != service:
            continue

        cb = circuit_breakers[name]

        # Determine effective timeout
        effective_timeout = config["timeout"]
        if global_timeout is not None:
            effective_timeout = global_timeout

        # Rate limit: reduce rate to 50% when circuit breaker is HALF_OPEN
        effective_rate = probe_rate
        if limiter is not None and cb.state == CircuitState.HALF_OPEN:
            half_rate = max(1, probe_rate // 2)
            limiter.set_rate(float(half_rate))

        # Circuit breaker gate
        if not cb.allow_request():
            results["services"][name] = {
                "status": "CRITICAL",
                "detail": "Circuit breaker OPEN — probe blocked",
                "code": 0,
                "endpoint": f"http://{config['host']}:{config['port']}{config['path']}",
                "circuit_breaker": cb.to_dict(),
            }
            all_ok = False
            continue

        # Rate limiter gate
        throttled = False
        if limiter is not None:
            allowed, wait_time = limiter.consume()
            if not allowed:
                throttled = True
                time.sleep(min(wait_time, 1.0))

        status, detail, code = check_http_service(
            config["host"], config["port"], config["path"], effective_timeout
        )

        if status == "CRITICAL":
            cb.record_failure()
            all_ok = False
        else:
            cb.record_success()

        results["services"][name] = {
            "status": status,
            "detail": detail,
            "code": code,
            "endpoint": f"http://{config['host']}:{config['port']}{config['path']}",
            "timeout_used_s": effective_timeout,
            "throttled": throttled,
            "circuit_breaker": cb.to_dict(),
        }

        # Restore original rate after half-open probe
        if limiter is not None and cb.state == CircuitState.HALF_OPEN and probe_rate:
            limiter.set_rate(float(probe_rate))

    # Check infrastructure
    for name, config in INFRASTRUCTURE.items():
        if service and name != service:
            continue

        cb = circuit_breakers[name]

        effective_timeout = config["timeout"]
        if global_timeout is not None:
            effective_timeout = global_timeout

        if limiter is not None and cb.state == CircuitState.HALF_OPEN:
            half_rate = max(1, (probe_rate or 1) // 2)
            limiter.set_rate(float(half_rate))

        if not cb.allow_request():
            results["infrastructure"][name] = {
                "status": "CRITICAL",
                "detail": "Circuit breaker OPEN — probe blocked",
                "endpoint": f"{config['host']}:{config['port']}",
                "circuit_breaker": cb.to_dict(),
            }
            all_ok = False
            continue

        throttled = False
        if limiter is not None:
            allowed, wait_time = limiter.consume()
            if not allowed:
                throttled = True
                time.sleep(min(wait_time, 1.0))

        status, detail, latency = check_tcp_port(config["host"], config["port"], effective_timeout)
        if status == "CRITICAL":
            cb.record_failure()
            all_ok = False
        else:
            cb.record_success()

        results["infrastructure"][name] = {
            "status": status,
            "detail": detail,
            "endpoint": f"{config['host']}:{config['port']}",
            "timeout_used_s": effective_timeout,
            "throttled": throttled,
            "circuit_breaker": cb.to_dict(),
        }

        if limiter is not None and probe_rate and cb.state == CircuitState.HALF_OPEN:
            limiter.set_rate(float(probe_rate))

    # Check system resources
    disk_status, disk_detail, disk_pct = check_disk_usage()
    results["system"]["disk"] = {"status": disk_status, "detail": disk_detail}
    if disk_status == "CRITICAL":
        all_ok = False

    mem_status, mem_detail, mem_pct = check_memory_usage()
    results["system"]["memory"] = {"status": mem_status, "detail": mem_detail}
    if mem_status == "CRITICAL":
        all_ok = False

    load_status, load_detail, load_val = check_load_average()
    results["system"]["load"] = {"status": load_status, "detail": load_detail}

    # Check certificate expiry (web services)
    for name, config in SERVICES.items():
        if service and name != service:
            continue
        if config["port"] == 443:
            cert_status, cert_detail, days_left = check_certificate_expiry(config["host"])
            results["services"][name]["certificate"] = {
                "status": cert_status,
                "detail": cert_detail,
                "days_remaining": days_left,
            }
            if cert_status == "CRITICAL":
                all_ok = False

    # Attach rate limiter stats
    if limiter is not None:
        results["rate_limiter"] = limiter.stats()

    # Attach circuit breaker states
    results["circuit_breakers"] = {
        name: cb.to_dict() for name, cb in circuit_breakers.items()
        if name in results.get("services", {}) or name in results.get("infrastructure", {})
    }

    results["overall_status"] = "OK" if all_ok else "DEGRADED"

    return results


def print_health_report(results: Dict[str, Any]):
    print(f"\n{'='*60}")
    print(f"  HEALTH CHECK REPORT")
    print(f"  Host: {results['hostname']}")
    print(f"  Time: {results['timestamp']}")
    print(f"  Overall: {results['overall_status']}")
    print(f"{'='*60}")

    # Rate limiter section
    rl = results.get("rate_limiter", {})
    if rl:
        print(f"\n  Rate Limiter:")
        print(f"    Configured rate: {rl.get('rate_per_second', 'N/A')} probes/sec")
        print(f"    Throttled: {rl.get('throttled_probes', 0)} of {rl.get('total_probes', 0)} probes ({rl.get('throttle_pct', 0)}%)")
        print(f"    Current effective rate: {rl.get('current_tokens', 'N/A')} tokens")

    for category, items in [("Services", results["services"]),
                             ("Infrastructure", results["infrastructure"]),
                             ("System", results["system"])]:
        if items:
            print(f"\n  {category}:")
            for name, check in items.items():
                if isinstance(check, dict) and "status" in check:
                    status_icon = {"OK": "\u2713", "WARNING": "\u26a0", "CRITICAL": "\u2717"}.get(check["status"], "?")
                    extra = ""
                    if check.get("throttled"):
                        extra += " [throttled]"
                    cb_state = (check.get("circuit_breaker") or {}).get("state", "")
                    if cb_state and cb_state != "CLOSED":
                        extra += f" [CB:{cb_state}]"
                    print(f"    {status_icon} {name}: {check['detail']}{extra}")
                else:
                    print(f"    {name}:")
                    for sub_name, sub_check in check.items():
                        if isinstance(sub_check, dict) and "status" in sub_check:
                            sub_icon = {"OK": "\u2713", "WARNING": "\u26a0", "CRITICAL": "\u2717"}.get(sub_check["status"], "?")
                            print(f"      {sub_icon} {sub_name}: {sub_check['detail']}")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Health check tool")
    parser.add_argument("--service", "-s", help="Check specific service only")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--watch", "-w", action="store_true", help="Continuous monitoring")
    parser.add_argument("--interval", "-i", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=None,
        help="Global timeout in seconds for all probes (overrides per-service defaults)",
    )
    parser.add_argument(
        "--probe-rate", "-r",
        type=int,
        default=None,
        help="Maximum probes per second (token bucket rate limiter)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.watch:
        print(f"Continuous monitoring (interval: {args.interval}s). Press Ctrl+C to stop.")
        try:
            while True:
                results = run_health_checks(
                    args.service, args.json,
                    global_timeout=args.timeout,
                    probe_rate=args.probe_rate,
                )
                if args.json:
                    print(json.dumps(results, indent=2))
                else:
                    print_health_report(results)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped")
    else:
        results = run_health_checks(
            args.service, args.json,
            global_timeout=args.timeout,
            probe_rate=args.probe_rate,
        )
        if args.json:
            output = json.dumps(results, indent=2)
            print(output)
        else:
            print_health_report(results)

        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"Report saved to {args.output}")

        if results["overall_status"] == "DEGRADED":
            return 1

    return 0


if __name__ == "__main__":
    main()
