#!/usr/bin/env python3
"""Tests for log aggregator JSONL output."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from log_aggregator import LogAggregator, JSONLogParser, TextLogParser


class TestJSONLExport(unittest.TestCase):
    def setUp(self):
        self.aggregator = LogAggregator()

    def test_jsonl_output_format(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("2024-01-15T10:30:00 [backend] INFO Request processed\n")
            f.write("2024-01-15T10:30:01 [backend] ERROR Connection failed\n")
            f.write("not a log line\n")
            f.name
            tmpfile = f.name
        try:
            self.aggregator.process_file(tmpfile)
            out = tmpfile + ".jsonl"
            self.aggregator.export_jsonl(out)
            with open(out) as f:
                lines = f.readlines()
            self.assertGreaterEqual(len(lines), 3)
            for line in lines:
                record = json.loads(line)
                self.assertIn("timestamp", record)
                self.assertIn("level", record)
                self.assertIn("source", record)
                self.assertIn("message", record)
                self.assertIn("metadata", record)
        finally:
            os.unlink(tmpfile)
            if os.path.exists(out):
                os.unlink(out)

    def test_jsonl_ordering(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("2024-01-15T10:30:02 [svc] INFO third\n")
            f.write("2024-01-15T10:30:00 [svc] INFO first\n")
            f.write("2024-01-15T10:30:01 [svc] INFO second\n")
            tmpfile = f.name
        try:
            self.aggregator.process_file(tmpfile)
            out = tmpfile + ".jsonl"
            self.aggregator.export_jsonl(out)
            with open(out) as f:
                lines = f.readlines()
            timestamps = [json.loads(l)["timestamp"] for l in lines]
            self.assertEqual(timestamps, sorted(timestamps))
        finally:
            os.unlink(tmpfile)
            if os.path.exists(out):
                os.unlink(out)

    def test_jsonl_unparseable_warning(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("completely invalid data %%%\n")
            tmpfile = f.name
        try:
            self.aggregator.process_file(tmpfile)
            out = tmpfile + ".jsonl"
            self.aggregator.export_jsonl(out)
            with open(out) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["level"], "warning")
            self.assertTrue(record["metadata"]["parse_error"])
        finally:
            os.unlink(tmpfile)
            if os.path.exists(out):
                os.unlink(out)


class TestJSONLogParser(unittest.TestCase):
    def test_parse_json_line(self):
        parser = JSONLogParser()
        line = json.dumps({"timestamp": "2024-01-15T10:00:00", "level": "info", "service": "api", "message": "OK"})
        result = parser.parse(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "info")
        self.assertEqual(result["service"], "api")

    def test_parse_invalid_json(self):
        parser = JSONLogParser()
        result = parser.parse("not json at all")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
