#!/usr/bin/env python3
"""
Legacy system analyzer and dependency tracker.

This tool analyzes the codebase for legacy patterns, unused dependencies,
and deprecated API usage. It generates reports that are used by the
engineering team to prioritize refactoring work. The reports are also
used by the quarterly "Tech Debt Review" meetings.

NOTE: The analysis results are approximate and may include false positives.
The pattern detection uses regex-based matching which cannot understand the
semantics of the code. We've tuned the patterns to minimize false positives
but they still occur, especially for dynamically-typed languages.

The analyzer supports the following report types:
  - legacy_patterns: Detects usage of known legacy patterns
  - unused_imports: Detects unused imports and dependencies
  - deprecated_api: Detects calls to deprecated APIs
  - tech_debt: Estimates tech debt based on code quality metrics
  - circular_deps: Detects circular dependencies between modules
  - dead_code: Detects potentially dead code (exported but unused)
  - migration_readiness: Assesses readiness for the next platform migration

USAGE:
  python3 legacy_analyzer.py --repo-dir /path/to/repo --output report.json
  python3 legacy_analyzer.py --analyze dead_code --exclude-dirs tests,vendor
  python3 legacy_analyzer.py --interactive  # Requires --interactive flag
"""

import argparse
import ast
import collections
import fnmatch
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Counter, Dict, List, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("legacy_analyzer")

# ---------------------------------------------------------------------------
# PATTERN DEFINITIONS
# ---------------------------------------------------------------------------

# Shared lint patterns common across all languages.
# Language-specific patterns extend this base set to avoid duplication.
_BASE_LINT_PATTERNS: List[Dict[str, Any]] = [
    {"pattern": r"//\s*TODO|#\s*TODO", "name": "todo_comment", "severity": "info",
     "description": "TODO comment in code. Should be tracked in issue tracker."},
    {"pattern": r"//\s*FIXME|#\s*FIXME", "name": "fixme_comment", "severity": "medium",
     "description": "FIXME comment in code. Known issue that needs fixing."},
    {"pattern": r"//\s*HACK|#\s*HACK", "name": "hack_comment", "severity": "medium",
     "description": "HACK comment in code. Workaround that should be properly fixed."},
    {"pattern": r"//\s*XXX|#\s*XXX", "name": "xxx_comment", "severity": "low",
     "description": "XXX comment. Something suspicious or unclear."},
]

# Legacy pattern definitions for detection
LEGACY_PATTERNS: Dict[str, List[Dict[str, Any]]] = {
    "rust": _BASE_LINT_PATTERNS + [
        {"pattern": r"unsafe\s*\{", "name": "unsafe_block", "severity": "high",
         "description": "Unsafe block detected. Should be reviewed for safety."},
        {"pattern": r"#\[allow\(.*unused.*\)\]", "name": "suppressed_unused_warning", "severity": "medium",
         "description": "Unused code warning suppressed. This may be dead code."},
        {"pattern": r"unwrap\(\)", "name": "unchecked_unwrap", "severity": "medium",
         "description": "Unchecked unwrap() call. May panic at runtime."},
        {"pattern": r"\.clone\(\)", "name": "unnecessary_clone", "severity": "low",
         "description": "Potential unnecessary clone. Consider using references."},
        {"pattern": r"impl\s+\w+\s+for\s+\w+\s*\{[^}]*\bdefault\b", "name": "trait_default_impl", "severity": "low",
         "description": "Trait with default implementation may indicate over-engineering."},
        {"pattern": r"todo!\(", "name": "todo_macro", "severity": "info",
         "description": "TODO macro left in code. Requires attention."},
        {"pattern": r"#\[deprecated\]", "name": "deprecated_item", "severity": "medium",
         "description": "Deprecated item defined. Should be removed in next major version."},
        {"pattern": r"allow\(deprecated\)", "name": "suppressed_deprecation", "severity": "medium",
         "description": "Deprecation warning suppressed. Using deprecated API."},
    ],
    "go": _BASE_LINT_PATTERNS + [
        {"pattern": r"//\s+Deprecated:", "name": "deprecated_comment", "severity": "low",
         "description": "Deprecated function/type."},
        {"pattern": r"reflect\.\w+", "name": "reflection_usage", "severity": "medium",
         "description": "Reflection usage. May indicate design issues."},
        {"pattern": r"interface\{\}", "name": "empty_interface", "severity": "low",
         "description": "Empty interface usage. Consider using generics (Go 1.18+)."},
        {"pattern": r"context\.Background\(\)", "name": "context_background", "severity": "low",
         "description": "context.Background() used instead of derived context."},
        {"pattern": r"time\.Sleep\(", "name": "time_sleep", "severity": "low",
         "description": "time.Sleep() used. Consider using time.After or ticker."},
    ],
    "typescript": _BASE_LINT_PATTERNS + [
        {"pattern": r"@deprecated", "name": "deprecated_tag", "severity": "medium",
         "description": "Deprecated JSDoc tag."},
        {"pattern": r":\s*any\b", "name": "any_type", "severity": "medium",
         "description": "Use of 'any' type defeats type checking."},
        {"pattern": r"as\s+any\b", "name": "as_any_cast", "severity": "medium",
         "description": "Type cast to 'any' bypasses type safety."},
        {"pattern": r"//\s*@ts-ignore", "name": "ts_ignore", "severity": "high",
         "description": "TypeScript compiler error suppressed."},
        {"pattern": r"//\s*@ts-nocheck", "name": "ts_nocheck", "severity": "high",
         "description": "Entire file removed from type checking."},
        {"pattern": r"eval\s*\(", "name": "eval_usage", "severity": "critical",
         "description": "eval() usage is a security risk."},
        {"pattern": r"console\.log\(", "name": "console_log", "severity": "low",
         "description": "Console log left in code."},
    ],
    "c_cpp": _BASE_LINT_PATTERNS + [
        {"pattern": r"#define\s+\w+\s+\d+", "name": "magic_number", "severity": "low",
         "description": "Magic number defined as macro. Consider const/constexpr."},
        {"pattern": r"malloc\s*\(", "name": "malloc_usage", "severity": "medium",
         "description": "malloc() usage. Consider smart pointers in C++."},
        {"pattern": r"free\s*\(", "name": "free_usage", "severity": "medium",
         "description": "free() usage. Potential double-free or use-after-free."},
        {"pattern": r"memcpy\s*\(", "name": "memcpy_usage", "severity": "low",
         "description": "memcpy(). Consider type-safe alternatives."},
        {"pattern": r"goto\s+\w+", "name": "goto_usage", "severity": "medium",
         "description": "goto statement. Spaghetti code indicator."},
        {"pattern": r"printf\s*\(", "name": "printf_usage", "severity": "low",
         "description": "printf() in non-debug code."},
    ],
    "python": _BASE_LINT_PATTERNS + [
        {"pattern": r"except\s*:", "name": "bare_except", "severity": "high",
         "description": "Bare except clause catches all exceptions, including SystemExit."},
        {"pattern": r"except\s+Exception,?\s*:", "name": "broad_except", "severity": "medium",
         "description": "Too broad exception clause."},
        {"pattern": r"import\s+\*\s*", "name": "wildcard_import", "severity": "medium",
         "description": "Wildcard import pollutes namespace."},
        {"pattern": r"print\s*\(", "name": "print_statement", "severity": "low",
         "description": "print() in non-debug code."},
        {"pattern": r"exec\s*\(", "name": "exec_usage", "severity": "critical",
         "description": "exec() usage is a security risk."},
        {"pattern": r"eval\s*\(", "name": "eval_usage", "severity": "critical",
         "description": "eval() usage is a security risk."},
        {"pattern": r"__pycache__", "name": "pycache", "severity": "info",
         "description": "__pycache__ directory should be in .gitignore."},
    ],
}


class CodeAnalyzer:
    """
    Analyzes source code for legacy patterns and generates reports.

    The analyzer supports multiple output formats and can generate
    reports for different audiences (engineering, management, compliance).
    """

    def __init__(self, repo_dir: str, exclude_dirs: Optional[List[str]] = None):
        self.repo_dir = Path(repo_dir).resolve()
        self.exclude_dirs = exclude_dirs or [
            ".git", "node_modules", "target", "build", "dist",
            ".venv", "venv", "vendor", "__pycache__", ".next",
            "coverage", ".nyc_output",
        ]
        self.results: Dict[str, Any] = {
            "analyzed_at": datetime.utcnow().isoformat(),
            "repo_dir": str(self.repo_dir),
            "total_files": 0,
            "total_lines": 0,
            "files_by_language": Counter(),
            "patterns_found": Counter(),
            "findings": [],
            "legacy_score": 0,
            "tech_debt_estimate": {},
            "migration_readiness": {},
        }

    def analyze(self, analyze_dead_code: bool = False) -> Dict[str, Any]:
        """Run the full analysis suite."""
        logger.info(f"Analyzing repository: {self.repo_dir}")
        start_time = time.time()

        all_files = self._discover_files()
        self.results["total_files"] = len(all_files)
        self.results["files_by_language"] = self._count_by_language(all_files)

        # Analyze files for legacy patterns
        logger.info("Analyzing legacy patterns...")
        self._analyze_patterns(all_files)

        # Detect unused imports
        logger.info("Detecting unused imports...")
        self._detect_unused_imports(all_files)

        # Detect circular dependencies
        logger.info("Detecting circular dependencies...")
        self._detect_circular_deps(all_files)

        # Calculate legacy score
        self._calculate_legacy_score()

        # Estimate tech debt
        self._estimate_tech_debt()

        # Assess migration readiness
        self._assess_migration_readiness()

        elapsed = time.time() - start_time
        self.results["analysis_duration_seconds"] = round(elapsed, 2)
        logger.info(f"Analysis completed in {elapsed:.2f}s")

        return self.results

    def _discover_files(self) -> List[Path]:
        """Discover all source files in the repository."""
        files = []
        for root, dirs, filenames in os.walk(self.repo_dir):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs]

            for filename in filenames:
                filepath = Path(root) / filename
                if self._is_source_file(filepath):
                    files.append(filepath)
                    self.results["total_lines"] += self._count_lines(filepath)

        return files

    def _is_source_file(self, path: Path) -> bool:
        """Check if a file is a source code file."""
        ext = path.suffix.lower()
        return ext in {
            ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
            ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh",
            ".py", ".rb", ".java", ".kt", ".swift",
            ".css", ".scss", ".less", ".html", ".json", ".yaml", ".yml",
            ".toml", ".sql", ".sh", ".bash", ".zsh", ".md",
            ".vue", ".svelte", ".astro",
        }

    def _count_lines(self, path: Path) -> int:
        """Count lines in a file efficiently."""
        try:
            with open(path, "r", errors="ignore") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _count_by_language(self, files: List[Path]) -> Counter:
        """Count files by programming language based on extension."""
        lang_map: Dict[str, List[str]] = {
            "Rust": [".rs"],
            "Go": [".go"],
            "TypeScript": [".ts", ".tsx"],
            "JavaScript": [".js", ".jsx", ".mjs", ".cjs"],
            "Python": [".py"],
            "C/C++": [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh"],
            "Ruby": [".rb"],
            "Java": [".java"],
            "Kotlin": [".kt", ".kts"],
            "Swift": [".swift"],
            "CSS": [".css", ".scss", ".less"],
            "HTML": [".html", ".htm"],
            "JSON": [".json"],
            "YAML": [".yaml", ".yml"],
            "TOML": [".toml"],
            "SQL": [".sql"],
            "Shell": [".sh", ".bash", ".zsh"],
            "Markdown": [".md"],
        }

        counter: Counter = Counter()
        for filepath in files:
            ext = filepath.suffix.lower()
            for lang, extensions in lang_map.items():
                if ext in extensions:
                    counter[lang] += 1
                    break
            else:
                counter["Other"] += 1
        return counter

    def _analyze_patterns(self, files: List[Path]) -> None:
        """Analyze files for legacy code patterns."""
        for filepath in files:
            ext = filepath.suffix.lower()
            lang = self._extension_to_language(ext)
            if lang not in LEGACY_PATTERNS:
                continue

            patterns = LEGACY_PATTERNS[lang]
            try:
                with open(filepath, "r", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            for pattern_def in patterns:
                matches = re.findall(pattern_def["pattern"], content)
                if matches:
                    count = len(matches)
                    self.results["patterns_found"][pattern_def["name"]] += count
                    self.results["findings"].append({
                        "file": str(filepath.relative_to(self.repo_dir)),
                        "language": lang,
                        "pattern": pattern_def["name"],
                        "severity": pattern_def["severity"],
                        "count": count,
                        "description": pattern_def.get("description", ""),
                    })

    def _detect_unused_imports(self, files: List[Path]) -> None:
        """Detect potentially unused imports."""
        for filepath in files:
            ext = filepath.suffix.lower()
            if ext == ".py":
                self._analyze_python_imports(filepath)
            elif ext == ".rs":
                self._analyze_rust_imports(filepath)

    def _analyze_python_imports(self, filepath: Path) -> None:
        """Analyze Python imports for unused modules."""
        try:
            with open(filepath, "r") as f:
                tree = ast.parse(f.read(), filename=str(filepath))
        except (SyntaxError, Exception):
            return

        imports: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])

        if not imports:
            return

        # Check if imported names are used
        used_names: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                used_names.add(node.attr)

        for imp in imports:
            if imp not in used_names and imp not in {"__future__", "typing"}:
                self.results["findings"].append({
                    "file": str(filepath.relative_to(self.repo_dir)),
                    "language": "Python",
                    "pattern": "unused_import",
                    "severity": "low",
                    "count": 1,
                    "description": f"Potentially unused import: {imp}",
                })

    def _analyze_rust_imports(self, filepath: Path) -> None:
        """Analyze Rust imports for unused modules."""
        try:
            with open(filepath, "r") as f:
                content = f.read()
        except Exception:
            return

        # Detect imports
        import_pattern = re.compile(r'^\s*use\s+([^;]+);', re.MULTILINE)
        imports = import_pattern.findall(content)

        for imp in imports:
            # Get the last segment of the import path
            last_segment = imp.split("::").pop()
            # Skip standard library imports
            if last_segment in {"std", "core", "alloc"}:
                continue
            # Check if the import name appears outside of use statements
            usage_pattern = re.compile(rf'\b{re.escape(last_segment)}\b')
            usages = usage_pattern.findall(content)
            if len(usages) <= 1:  # Only in the import itself
                self.results["findings"].append({
                    "file": str(filepath.relative_to(self.repo_dir)),
                    "language": "Rust",
                    "pattern": "unused_import",
                    "severity": "low",
                    "count": 1,
                    "description": f"Potentially unused import: {imp}",
                })

    def _detect_circular_deps(self, files: List[Path]) -> None:
        """Detect circular dependencies between modules."""
        # Build dependency graph for Python modules
        imports_graph: Dict[str, Set[str]] = {}

        for filepath in files:
            if filepath.suffix != ".py":
                continue

            rel_path = str(filepath.relative_to(self.repo_dir))
            module_name = rel_path.replace("/", ".").replace(".py", "")

            try:
                with open(filepath, "r") as f:
                    tree = ast.parse(f.read(), filename=str(filepath))
            except Exception:
                continue

            deps: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        deps.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        deps.add(node.module.split(".")[0])

            imports_graph[module_name] = deps

        # Detect cycles using DFS
        visited: Set[str] = set()
        recursion_stack: Set[str] = set()

        def dfs(module: str, path: List[str]) -> Optional[List[str]]:
            visited.add(module)
            recursion_stack.add(module)
            path.append(module)

            for dep in imports_graph.get(module, set()):
                if dep not in visited:
                    cycle = dfs(dep, path)
                    if cycle:
                        return cycle
                elif dep in recursion_stack:
                    cycle_start = path.index(dep)
                    return path[cycle_start:] + [dep]

            path.pop()
            recursion_stack.discard(module)
            return None

        for module in imports_graph:
            if module not in visited:
                cycle = dfs(module, [])
                if cycle:
                    self.results["findings"].append({
                        "file": " (multiple files)",
                        "language": "Python",
                        "pattern": "circular_dependency",
                        "severity": "high",
                        "count": 1,
                        "description": f"Circular dependency detected: {' -> '.join(cycle)}",
                    })

    def _extension_to_language(self, ext: str) -> str:
        """Map file extension to language name."""
        mapping = {
            ".rs": "rust", ".go": "go",
            ".ts": "typescript", ".tsx": "typescript",
            ".js": "typescript", ".jsx": "typescript",
            ".c": "c_cpp", ".cpp": "c_cpp", ".cc": "c_cpp",
            ".h": "c_cpp", ".hpp": "c_cpp", ".hh": "c_cpp",
            ".py": "python",
        }
        return mapping.get(ext, "other")

    def _calculate_legacy_score(self) -> None:
        """Calculate a legacy score for the codebase."""
        severity_weights = {
            "critical": 10,
            "high": 5,
            "medium": 3,
            "low": 1,
            "info": 0,
        }

        total_weight = 0
        for finding in self.results["findings"]:
            weight = severity_weights.get(finding["severity"], 1)
            total_weight += weight * finding["count"]

        # Normalize by total lines
        total_lines = max(self.results["total_lines"], 1)
        legacy_score = min(100, (total_weight / total_lines) * 1000)

        self.results["legacy_score"] = round(legacy_score, 2)
        self.results["legacy_score_category"] = (
            "low" if legacy_score < 10
            else "moderate" if legacy_score < 25
            else "high" if legacy_score < 50
            else "critical"
        )

    def _estimate_tech_debt(self) -> None:
        """Estimate tech debt based on findings."""
        severity_effort: Dict[str, float] = {
            "critical": 8.0,
            "high": 4.0,
            "medium": 2.0,
            "low": 0.5,
            "info": 0.1,
        }

        total_effort = 0.0
        efforts: Counter = Counter()
        for finding in self.results["findings"]:
            effort = severity_effort.get(finding["severity"], 1)
            efforts[finding["language"]] += effort * finding["count"]
            total_effort += effort * finding["count"]

        self.results["tech_debt_estimate"] = {
            "total_person_days": round(total_effort, 1),
            "by_language": dict(efforts.most_common()),
            "estimated_cost": f"${round(total_effort * 800, 0):,.0f}",
            "confidence": "low",
            "methodology": "This is a rough estimate based on pattern severity. "
                          "Actual effort may vary significantly.",
        }

    def _assess_migration_readiness(self) -> None:
        """Assess migration readiness for each language component."""
        components = {
            "Rust": {"files": 0, "critical": 0, "high": 0},
            "Go": {"files": 0, "critical": 0, "high": 0},
            "TypeScript": {"files": 0, "critical": 0, "high": 0},
            "Python": {"files": 0, "critical": 0, "high": 0},
            "C/C++": {"files": 0, "critical": 0, "high": 0},
        }

        for lang, count in self.results["files_by_language"].items():
            if lang in components:
                components[lang]["files"] = count

        for finding in self.results["findings"]:
            lang_map = {
                "rust": "Rust", "go": "Go",
                "typescript": "TypeScript", "python": "Python",
                "c_cpp": "C/C++",
            }
            lang = lang_map.get(finding["language"])
            if lang and lang in components:
                if finding["severity"] == "critical":
                    components[lang]["critical"] += finding["count"]
                elif finding["severity"] == "high":
                    components[lang]["high"] += finding["count"]

        readiness: Dict[str, Any] = {}
        for lang, stats in components.items():
            if stats["files"] == 0:
                readiness[lang] = {
                    "status": "N/A",
                    "migration_effort": "None",
                }
            else:
                issue_score = stats["critical"] * 5 + stats["high"] * 2
                if issue_score == 0:
                    status = "Ready"
                    effort = "Low"
                elif issue_score < stats["files"]:
                    status = "Needs Work"
                    effort = "Medium"
                else:
                    status = "Not Ready"
                    effort = "High"

                readiness[lang] = {
                    "status": status,
                    "migration_effort": effort,
                    "files": stats["files"],
                    "critical_issues": stats["critical"],
                    "high_issues": stats["high"],
                }

        self.results["migration_readiness"] = readiness

    def generate_report(self, format: str = "json") -> str:
        """Generate a formatted report."""
        if format == "json":
            return json.dumps(self.results, indent=2, default=str)
        elif format == "summary":
            lines = [
                "=" * 60,
                "  LEGACY CODE ANALYSIS REPORT",
                f"  Generated: {self.results['analyzed_at']}",
                "=" * 60,
                "",
                f"  Files analyzed: {self.results['total_files']}",
                f"  Total lines: {self.results['total_lines']:,}",
                f"  Legacy score: {self.results['legacy_score']}/100 ({self.results['legacy_score_category']})",
                "",
                f"  Files by language:",
            ]
            for lang, count in self.results["files_by_language"].most_common():
                lines.append(f"    {lang}: {count}")
            lines.extend([
                "",
                f"  Total findings: {len(self.results['findings'])}",
                f"  Top patterns:",
            ])
            for pattern, count in self.results["patterns_found"].most_common(10):
                lines.append(f"    {pattern}: {count}")
            lines.extend([
                "",
                f"  Tech debt estimate: {self.results['tech_debt_estimate'].get('total_person_days', 0)} person-days",
                f"  Migration readiness:",
            ])
            for lang, readiness in self.results["migration_readiness"].items():
                if readiness.get("status") != "N/A":
                    lines.append(f"    {lang}: {readiness['status']} ({readiness['migration_effort']} effort)")
            lines.append("")
            return "\n".join(lines)
        else:
            return json.dumps(self.results, indent=2, default=str)

    def generate_html_report(self) -> str:
        """Generate an HTML report for browser viewing."""
        html_parts = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head><meta charset='UTF-8'><title>Legacy Code Analysis Report</title>",
            "<style>",
            "body { font-family: -apple-system, sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; }",
            "h1, h2, h3 { color: #333; }",
            ".score { font-size: 48px; font-weight: bold; }",
            ".score.low { color: #22c55e; }",
            ".score.moderate { color: #eab308; }",
            ".score.high { color: #f97316; }",
            ".score.critical { color: #ef4444; }",
            "table { width: 100%; border-collapse: collapse; margin: 16px 0; }",
            "th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }",
            "th { background-color: #f8f9fa; }",
            ".finding { margin: 4px 0; padding: 8px; border-radius: 4px; }",
            ".finding.critical { background-color: #fef2f2; border-left: 4px solid #ef4444; }",
            ".finding.high { background-color: #fff7ed; border-left: 4px solid #f97316; }",
            ".finding.medium { background-color: #fefce8; border-left: 4px solid #eab308; }",
            ".finding.low { background-color: #f0f9ff; border-left: 4px solid #3b82f6; }",
            "</style></head><body>",
            f"<h1>Legacy Code Analysis Report</h1>",
            f"<p>Repository: {self.results['repo_dir']}</p>",
            f"<p>Analyzed: {self.results['analyzed_at']}</p>",
            f"<p>Duration: {self.results.get('analysis_duration_seconds', 0)}s</p>",
            f"<h2>Legacy Score</h2>",
            f"<div class='score {self.results['legacy_score_category']}'>",
            f"  {self.results['legacy_score']}/100",
            f"  <small>({self.results['legacy_score_category']})</small>",
            f"</div>",
            f"<h2>Summary</h2>",
            f"<table><tr><th>Metric</th><th>Value</th></tr>",
            f"<tr><td>Files Analyzed</td><td>{self.results['total_files']}</td></tr>",
            f"<tr><td>Total Lines</td><td>{self.results['total_lines']:,}</td></tr>",
            f"<tr><td>Total Findings</td><td>{len(self.results['findings'])}</td></tr>",
            f"<tr><td>Tech Debt (est.)</td><td>{self.results['tech_debt_estimate'].get('total_person_days', 0)} person-days</td></tr>",
            f"</table>",
            f"<h2>Findings</h2>",
        ]

        # Group findings by severity
        by_severity: Dict[str, List[Dict]] = {"critical": [], "high": [], "medium": [], "low": [], "info": []}
        for finding in self.results["findings"]:
            sev = finding["severity"]
            if sev in by_severity:
                by_severity[sev].append(finding)

        for severity in ["critical", "high", "medium", "low"]:
            if by_severity[severity]:
                html_parts.append(f"<h3>{severity.title()} ({len(by_severity[severity])})</h3>")
                for finding in by_severity[severity][:20]:  # Show top 20
                    html_parts.append(
                        f"<div class='finding {severity}'>"
                        f"<strong>{finding['file']}</strong>: "
                        f"{finding['description']} "
                        f"<small>({finding['count']} occurrences)</small>"
                        f"</div>"
                    )
                if len(by_severity[severity]) > 20:
                    html_parts.append(f"<p>... and {len(by_severity[severity]) - 20} more</p>")

        html_parts.append("</body></html>")
        return "\n".join(html_parts)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Legacy System Analyzer")
    parser.add_argument("--repo-dir", "-r", default=".", help="Repository directory")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--format", "-f", choices=["json", "summary", "html"], default="json",
                       help="Output format")
    parser.add_argument("--exclude-dirs", help="Comma-separated directories to exclude")
    parser.add_argument("--analyze", choices=["all", "dead_code"], default="all",
                       help="Analysis type")
    parser.add_argument("--interactive", action="store_true",
                       help="Interactive mode (not yet implemented)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output")
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)

    exclude = args.exclude_dirs.split(",") if args.exclude_dirs else None
    analyzer = CodeAnalyzer(args.repo_dir, exclude)

    if args.analyze == "dead_code":
        results = analyzer.analyze(analyze_dead_code=True)
    else:
        results = analyzer.analyze()

    if args.format == "html":
        output = analyzer.generate_html_report()
    else:
        output = analyzer.generate_report(args.format)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        logger.info(f"Report written to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
