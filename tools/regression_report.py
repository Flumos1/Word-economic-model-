#!/usr/bin/env python3
"""Summary regression report for AMFE: run every ``tests/test_*.py`` file and record the results.

Each test file runs as a separate process (``python tests/test_x.py`` here, i.e. the same command the
existing CI workflows use), so an import error or a crash in one file cannot hide the others and the
duration of every file is measured independently. The tool never touches the network and writes only
the JSON report (and optionally a Markdown summary); it does not modify tests, sources or protocols.

Output: ``protocols/regression_report.json`` (machine-readable) and one Markdown line on stdout for
the CI run page. Exit code: 0 only when every discovered test file exited 0, 1 otherwise.

Use ``python tools/regression_report.py`` from anywhere; ``--root``/``--tests-dir`` allow running on
a copy or on a synthetic fixture (used by tests/test_regression_report.py).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = "AMFE-REGRESSION-REPORT"
DEFAULT_PATTERN = "test_*.py"
DEFAULT_TIMEOUT = 900.0
TAIL_LINES = 40


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def discover_tests(tests_dir: Path, pattern: str = DEFAULT_PATTERN) -> list:
    """Sorted test files in ``tests_dir`` matching ``pattern``."""
    folder = Path(tests_dir)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob(pattern) if p.is_file()), key=lambda p: p.name)


def _skipped_count(output: str) -> int:
    """Number of skipped tests reported by unittest, when the output carries it."""
    match = re.search(r"skipped=(\d+)", output)
    return int(match.group(1)) if match else 0


def run_one(path: Path, root: Path, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run one test file in a subprocess and return its structured result."""
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = round(time.perf_counter() - started, 3)
        output = (completed.stdout or "") + (completed.stderr or "")
        status = "passed" if completed.returncode == 0 else "failed"
        result = {
            "file": str(path),
            "status": status,
            "returncode": completed.returncode,
            "duration_seconds": duration,
            "skipped": _skipped_count(output),
        }
        if status != "passed":
            tail = "\n".join(output.strip().splitlines()[-TAIL_LINES:])
            result["output_tail"] = tail[-8000:]
        return result
    except subprocess.TimeoutExpired:
        duration = round(time.perf_counter() - started, 3)
        return {
            "file": str(path),
            "status": "timeout",
            "returncode": None,
            "duration_seconds": duration,
            "skipped": 0,
            "output_tail": f"timeout after {timeout:g} s",
        }
    except OSError as exc:  # interpreter could not be started
        duration = round(time.perf_counter() - started, 3)
        return {
            "file": str(path),
            "status": "failed",
            "returncode": None,
            "duration_seconds": duration,
            "skipped": 0,
            "output_tail": f"could not start the test process: {exc!r}",
        }


def build_report(results: list, root: Path, tests_dir: Path, pattern: str,
                 timeout: float, generated_at: str | None = None) -> dict:
    """Assemble the JSON report and its Markdown line from per-test results."""
    root = Path(root)
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "passed")
    failed = total - passed
    duration = round(sum(float(r["duration_seconds"]) for r in results), 3)
    failures = [r["file"] for r in results if r["status"] != "passed"]
    skipped = sum(int(r.get("skipped") or 0) for r in results)
    if total == 0:
        markdown = "**AMFE regression: no test files found** — check --tests-dir"
    elif failed == 0:
        markdown = (f"**AMFE regression: {passed}/{total} passed** — {duration:.1f} s "
                    f"({total} files, {skipped} skipped tests)")
    else:
        markdown = (f"**AMFE regression: {passed}/{total} passed, {failed} FAILED** — {duration:.1f} s "
                    f"(failed: {', '.join(_rel(f, root) for f in failures)})")
    return {
        "report_id": REPORT_ID,
        "generated_at": generated_at or utc_now(),
        "root": str(root),
        "tests_dir": str(tests_dir),
        "pattern": pattern,
        "timeout_seconds": timeout,
        "python": sys.version.split()[0],
        "totals": {"total": total, "passed": passed, "failed": failed, "skipped": skipped,
                   "duration_seconds": duration},
        "passed": total > 0 and failed == 0,
        "failures": failures,
        "markdown": markdown,
        "tests": results,
    }


def _rel(path_str: str, root: Path) -> str:
    try:
        return str(Path(path_str).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return path_str


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run all tests/test_*.py and write a summary report")
    parser.add_argument("--root", default=str(ROOT), help="repository root (working directory of the tests)")
    parser.add_argument("--tests-dir", default=None, help="test directory (default: <root>/tests)")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help="test file glob (default: test_*.py)")
    parser.add_argument("--json-out", default=None,
                        help="JSON report path (default: <root>/protocols/regression_report.json)")
    parser.add_argument("--markdown-out", default=None, help="optional Markdown file with the summary line")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-test timeout, seconds")
    parser.add_argument("--no-json", action="store_true", help="do not write the JSON report")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    tests_dir = Path(args.tests_dir) if args.tests_dir else root / "tests"
    json_out = Path(args.json_out) if args.json_out else root / "protocols" / "regression_report.json"

    paths = discover_tests(tests_dir, args.pattern)
    print(f"AMFE regression: {len(paths)} test file(s) under {tests_dir}", flush=True)
    results = []
    for path in paths:
        print(f"  running {path.name} ...", flush=True)
        result = run_one(path, root, args.timeout)
        result["file"] = _rel(result["file"], root)
        results.append(result)
        print(f"    {result['status']} in {result['duration_seconds']:.1f} s", flush=True)

    report = build_report(results, root, tests_dir, args.pattern, args.timeout)
    print(report["markdown"])
    if not args.no_json:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report: {json_out}")
    if args.markdown_out:
        markdown_path = Path(args.markdown_out)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(report["markdown"] + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
