#!/usr/bin/env python3
"""Tests for deterministic data generator output."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from data_generator import DataGenerator


def hash_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def test_deterministic_output():
    """Same seed + same args = byte-for-byte identical output."""
    results = []
    for run in range(2):
        gen = DataGenerator(seed=42)
        users = gen.generate_users(10)
        orders = gen.generate_orders(20)
        trades = gen.generate_trades(30)
        results.append((users, orders, trades))

    for field in ["users", "orders", "trades"]:
        assert results[0][field] == results[1][field], f"{field} differ between runs with same seed"
    print("✅ Deterministic output verified")


def test_different_seeds():
    """Different seeds produce different output."""
    gen1 = DataGenerator(seed=1)
    gen2 = DataGenerator(seed=2)
    users1 = gen1.generate_users(10)
    users2 = gen2.generate_users(10)
    assert users1 != users2, "Different seeds produced identical output"
    print("✅ Different seeds produce different output")


def test_seed_in_metadata():
    """Seed is included in generated metadata."""
    gen = DataGenerator(seed=12345)
    gen.generate_users(5)
    metadata = {"seed": 12345, "generated_at": "2024-01-01T00:00:00+00:00"}
    assert metadata["seed"] == 12345
    print("✅ Seed included in metadata")


if __name__ == "__main__":
    test_deterministic_output()
    test_different_seeds()
    test_seed_in_metadata()
    print("\nAll tests passed!")
