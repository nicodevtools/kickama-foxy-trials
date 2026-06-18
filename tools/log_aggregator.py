#!/usr/bin/env python3
"""
Legacy log aggregator and analysis tool for the Tent of Trials platform.

This tool collects logs from all services, aggregates them by various
dimensions, and generates analysis reports. It supports multiple input
formats (JSON, plain text, syslog) and output formats (JSON, CSV, HTML, JSONL).

WARNING: This tool is LEGACY. The new log aggregation pipeline uses
Elasticsearch + Kibana and is the recommended approach for log analysis.
"""

import argparse
import collections
import csv
import gzip
import io
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Counter, Dict, List, Optional, Tuple
from collections import defaultdict, Counter

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("log_aggregator")

class LogParser:
    TIMESTAMP_PATTERNS = [
        (r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', 'iso8601'),
        (r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', 'standard'),
        (r'^\[?\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}', 'nginx'),
        (r'^\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}', 'syslog'),
    ]

    LEVEL_PATTERNS = [
        (r'\b(ERROR|FATAL|CRITICAL)\b', 'error'),
        (r'\b(WARN|WARNING)\b', 'warn'),
        (r'\b(INFO|NOTICE)\b', 'info'),
        (r'\b(DEBUG|TRACE)\b', 'debug'),
    ]

    def parse(self, line: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def extract_timestamp(self, line: str) -> Optional[int]:
        for pattern, _ in self.TIMESTAMP_PATTERNS:
            match = re.search(pattern, line)
            if match:
                try:
                    dt_str = match.group(0)
                    for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%d/%b/%Y:%H:%M:%S', '%b %d %H:%M:%S']:
                        try:
                            dt = datetime.strptime(dt_str, fmt)
                            return int(dt.replace(tzinfo=timezone.utc).timestamp())
                        except ValueError:
                            continue
                except:
                    pass
        return None

    def extract_level(self, line: str) -> str:
        for pattern, level in self.LEVEL_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                return level
        return 'unknown'

    def extract_service(self, line: str) -> Optional[str]:
        match = re.search(r'\[(\w+)\]', line)
        if match:
            return match.group(1)
        match = re.search(r'(\w+)\s*:', line)
        if match and match.group(1).isupper():
            return match.group(1)
        return None

class JSONLogParser(LogParser):
    def parse(self, line: str) -> Optional[Dict[str, Any]]:
        try:
            entry = json.loads(line.strip())
            if not isinstance(entry, dict):
                return None
            return {
                'timestamp': entry.get('timestamp') or entry.get('time') or entry.get('@timestamp'),
                'level': entry.get('level') or entry.get('severity') or entry.get('lvl', 'info'),
                'service': entry.get('service') or entry.get('logger') or entry.get('app'),
                'message': entry.get('message') or entry.get('msg') or entry.get('event', ''),
                'fields': entry,
                'format': 'json',
            }
        except json.JSONDecodeError:
            return None

class TextLogParser(LogParser):
    def parse(self, line: str) -> Optional[Dict[str, Any]]:
        line = line.strip()
        if not line:
            return None
        return {
            'timestamp': self.extract_timestamp(line),
            'level': self.extract_level(line),
            'service': self.extract_service(line),
            'message': line,
            'fields': {'raw': line},
            'format': 'text',
        }

class NginxLogParser(LogParser):
    NGINX_PATTERN = re.compile(
        r'(\S+)\s+'
        r'(\S+)\s+'
        r'(\S+)\s+'
        r'\[([^\]]+)\]\s+'
        r'"([^"]*)"\s+'
        r'(\d+)\s+'
        r'(\d+)\s+'
        r'"([^"]*)"\s+'
        r'"([^"]*)"'
    )

    def parse(self, line: str) -> Optional[Dict[str, Any]]:
        match = self.NGINX_PATTERN.match(line)
        if not match:
            return None
        try:
            dt = datetime.strptime(match.group(4), '%d/%b/%Y:%H:%M:%S %z')
            timestamp = int(dt.timestamp())
        except:
            timestamp = None
        status_code = int(match.group(6))
        level = 'error' if status_code >= 500 else 'warn' if status_code >= 400 else 'info'
        return {
            'timestamp': timestamp,
            'level': level,
            'service': 'nginx',
            'message': match.group(5),
            'fields': {
                'remote_addr': match.group(1),
                'remote_user': match.group(2),
                'request': match.group(5),
                'status': status_code,
                'body_bytes': match.group(7),
                'referer': match.group(8),
                'user_agent': match.group(9),
            },
            'format': 'nginx',
        }

class LogAggregator:
    def __init__(self):
        self.parsers = [JSONLogParser(), TextLogParser(), NginxLogParser()]
        self.entries: List[Dict[str, Any]] = []
        self.unparseable: List[str] = []
        self.level_counts: Counter = Counter()
        self.service_counts: Counter = Counter()
        self.hourly_counts: Counter = Counter()
        self.error_patterns: Counter = Counter()
        self.errors_by_service: Dict[str, List[str]] = defaultdict(list)

    def process_file(self, filepath: str) -> int:
        parsed_count = 0
        try:
            if filepath.endswith('.gz'):
                with gzip.open(filepath, 'rt', errors='replace') as f:
                    for line in f:
                        if self._parse_line(line):
                            parsed_count += 1
            else:
                with open(filepath, 'r', errors='replace') as f:
                    for line in f:
                        if self._parse_line(line):
                            parsed_count += 1
        except Exception as e:
            logger.error(f"Error processing {filepath}: {e}")
        return parsed_count

    def _parse_line(self, line: str) -> bool:
        for parser in self.parsers:
            entry = parser.parse(line)
            if entry:
                self.entries.append(entry)
                ts = entry.get('timestamp')
                if ts:
                    try:
                        hour = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:00')
                        self.hourly_counts[hour] += 1
                    except (OSError, ValueError):
                        pass
                level = entry.get('level', 'unknown').lower()
                self.level_counts[level] += 1
                service = entry.get('service', 'unknown')
                self.service_counts[service] += 1
                if level in ('error', 'critical'):
                    msg = entry.get('message', '')
                    if len(msg) > 200:
                        msg = msg[:200]
                    self.errors_by_service[service].append(msg)
                    self.error_patterns[msg] += 1
                return True
        stripped = line.strip()
        if stripped:
            self.unparseable.append(stripped)
        return False

    def get_sorted_entries(self) -> List[Dict[str, Any]]:
        return sorted(self.entries, key=lambda e: e.get('timestamp') or 0)

    def export_jsonl(self, output_path: str):
        sorted_entries = self.get_sorted_entries()
        with open(output_path, 'w') as f:
            for entry in sorted_entries:
                ts = entry.get('timestamp')
                iso_ts = None
                if ts:
                    try:
                        iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    except (OSError, ValueError):
                        iso_ts = str(ts)
                record = {
                    'timestamp': iso_ts,
                    'level': entry.get('level', 'unknown'),
                    'source': entry.get('service', 'unknown'),
                    'message': entry.get('message', ''),
                    'metadata': {
                        'format': entry.get('format', 'unknown'),
                        'fields': entry.get('fields', {}),
                    },
                }
                f.write(json.dumps(record, default=str) + '\n')
            for line in self.unparseable:
                record = {
                    'timestamp': None,
                    'level': 'warning',
                    'source': 'parser',
                    'message': f'Unparseable log line: {line[:200]}',
                    'metadata': {'parse_error': True, 'raw': line[:500]},
                }
                f.write(json.dumps(record) + '\n')
        logger.info(f"JSONL exported to {output_path}")

    def get_summary(self) -> Dict[str, Any]:
        return {
            'total_entries': len(self.entries),
            'unparseable': len(self.unparseable),
            'time_range': self._get_time_range(),
            'by_level': dict(self.level_counts.most_common()),
            'by_service': dict(self.service_counts.most_common()),
            'by_hour': dict(sorted(self.hourly_counts.items())),
            'top_errors': dict(self.error_patterns.most_common(20)),
            'error_rate': self._calculate_error_rate(),
        }

    def _get_time_range(self) -> Optional[Dict[str, str]]:
        timestamps = [e['timestamp'] for e in self.entries if e.get('timestamp')]
        if not timestamps:
            return None
        return {
            'start': datetime.fromtimestamp(min(timestamps), tz=timezone.utc).isoformat(),
            'end': datetime.fromtimestamp(max(timestamps), tz=timezone.utc).isoformat(),
            'duration_hours': round((max(timestamps) - min(timestamps)) / 3600, 2),
        }

    def _calculate_error_rate(self) -> float:
        total = len(self.entries)
        if total == 0:
            return 0.0
        errors = self.level_counts.get('error', 0) + self.level_counts.get('critical', 0)
        return round(errors / total * 100, 2)

    def export_csv(self, output_path: str, max_entries: int = 10000):
        fields = ['timestamp', 'level', 'service', 'message']
        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for entry in self.entries[:max_entries]:
                writer.writerow(entry)
        logger.info(f"Exported {min(len(self.entries), max_entries)} entries to {output_path}")

    def export_json(self, output_path: str):
        with open(output_path, 'w') as f:
            json.dump({
                'summary': self.get_summary(),
                'entries': self.entries[:1000],
            }, f, indent=2, default=str)
        logger.info(f"Report exported to {output_path}")

def parse_args():
    parser = argparse.ArgumentParser(description="Log aggregator and analysis tool")
    parser.add_argument("--input", "-i", help="Input log file or glob pattern")
    parser.add_argument("--dir", help="Directory containing log files")
    parser.add_argument("--output", "-o", default="log_report.json", help="Output file path")
    parser.add_argument("--format", choices=["text", "jsonl", "json", "csv", "html"], default="text", help="Output format (default: text)")
    parser.add_argument("--search", help="Search for a string in logs")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    return parser.parse_args()

def main():
    args = parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    aggregator = LogAggregator()

    if args.input:
        if '*' in args.input or '?' in args.input:
            import glob
            for path in glob.glob(args.input):
                count = aggregator.process_file(path)
                logger.info(f"Processed {path}: {count} entries")
        else:
            count = aggregator.process_file(args.input)
            logger.info(f"Processed {args.input}: {count} entries")

    if args.dir:
        count = aggregator.process_directory(args.dir) if hasattr(aggregator, 'process_directory') else 0
        logger.info(f"Processed directory {args.dir}: {count} entries")

    summary = aggregator.get_summary()
    print(f"\nSummary:")
    print(f"  Total entries: {summary['total_entries']:,}")
    print(f"  Unparseable: {summary['unparseable']:,}")
    print(f"  Error rate: {summary.get('error_rate', 0)}%")

    if args.format == "jsonl":
        aggregator.export_jsonl(args.output)
    elif args.format == "csv":
        aggregator.export_csv(args.output)
    else:
        aggregator.export_json(args.output)

    return 0

if __name__ == "__main__":
    main()
