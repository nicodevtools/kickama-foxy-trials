#!/usr/bin/env python3
"""Smoke checks for monitoring_setup.py Prometheus port fallback behavior."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import monitoring_setup  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    busy_ports = {9090}

    def fake_port_available(_host: str, port: int) -> bool:
        return port not in busy_ports

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        resolved = monitoring_setup.resolve_prometheus_url(
            "http://localhost:9090",
            port_fallbacks=3,
            port_available=fake_port_available,
        )
    require(resolved == "http://localhost:9091", resolved)
    require("port 9090 is already in use" in stderr.getvalue(), stderr.getvalue())

    explicit_remote = monitoring_setup.resolve_prometheus_url(
        "http://prometheus.internal:9090",
        port_available=lambda _host, _port: False,
    )
    require(explicit_remote == "http://prometheus.internal:9090", explicit_remote)

    no_fallback = monitoring_setup.resolve_prometheus_url(
        "http://localhost:9090",
        port_fallbacks=0,
        port_available=lambda _host, _port: False,
    )
    require(no_fallback == "http://localhost:9090", no_fallback)

    all_busy_stderr = io.StringIO()
    with contextlib.redirect_stderr(all_busy_stderr):
        all_busy = monitoring_setup.resolve_prometheus_url(
            "http://127.0.0.1:9090",
            port_fallbacks=2,
            port_available=lambda _host, _port: False,
        )
    require(all_busy == "http://127.0.0.1:9090", all_busy)
    require("ports 9090-9092 are already in use" in all_busy_stderr.getvalue(), all_busy_stderr.getvalue())

    print("monitoring port fallback checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
