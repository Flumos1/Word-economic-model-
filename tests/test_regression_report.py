"""Summary regression tool: discovery, per-file results, exit codes and the JSON/Markdown report.

The tool is tested on a synthetic tests directory (a passing, a failing and a skipping test file), so
the project's own suite is never recursed into; the tests also check the empty-directory case and the
structure of protocols/regression_report.json.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import regression_report  # noqa: E402

PASS_SOURCE = """import unittest


class Ok(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(2 + 2, 4)


if __name__ == "__main__":
    unittest.main(verbosity=0)
"""

FAIL_SOURCE = """import unittest


class Bad(unittest.TestCase):
    def test_bad(self):
        self.assertEqual(1, 2, "intentional failure")


if __name__ == "__main__":
    unittest.main(verbosity=0)
"""

SKIP_SOURCE = """import unittest


class Skipped(unittest.TestCase):
    @unittest.skip("intentionally skipped")
    def test_skip(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=0)
"""


def make_tests_dir(base: Path, sources: dict) -> Path:
    tests_dir = base / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        (tests_dir / name).write_text(source, encoding="utf-8")
    return tests_dir


class RegressionTool(unittest.TestCase):
    def _run(self, sources: dict, extra_args=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = make_tests_dir(root, sources)
            json_out = root / "protocols" / "regression_report.json"
            markdown_out = root / "build" / "regression.md"
            argv = ["--root", str(root), "--tests-dir", str(tests_dir),
                    "--json-out", str(json_out), "--markdown-out", str(markdown_out),
                    "--timeout", "120"] + list(extra_args or [])
            code = regression_report.main(argv)
            report = json.loads(json_out.read_text(encoding="utf-8"))
            markdown = markdown_out.read_text(encoding="utf-8")
        return code, report, markdown

    def test_all_green_returns_zero_and_counts_skips(self):
        code, report, markdown = self._run({"test_ok.py": PASS_SOURCE,
                                            "test_skip.py": SKIP_SOURCE})
        self.assertEqual(code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["report_id"], "AMFE-REGRESSION-REPORT")
        self.assertEqual(report["totals"]["total"], 2)
        self.assertEqual(report["totals"]["passed"], 2)
        self.assertEqual(report["totals"]["failed"], 0)
        self.assertEqual(report["totals"]["skipped"], 1)
        self.assertTrue(markdown.startswith("**AMFE regression: 2/2 passed**"))
        for test in report["tests"]:
            self.assertEqual(set(("file", "status", "returncode", "duration_seconds")),
                             set(test) & set(("file", "status", "returncode", "duration_seconds")))
            self.assertEqual(test["status"], "passed")
            self.assertEqual(test["returncode"], 0)
            self.assertGreaterEqual(test["duration_seconds"], 0.0)
        self.assertEqual([test["file"] for test in report["tests"]],
                         ["tests/test_ok.py", "tests/test_skip.py"])

    def test_a_failing_test_fails_the_tool_and_keeps_the_output_tail(self):
        code, report, markdown = self._run({"test_ok.py": PASS_SOURCE,
                                            "test_bad.py": FAIL_SOURCE})
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["totals"]["total"], 2)
        self.assertEqual(report["totals"]["passed"], 1)
        self.assertEqual(report["totals"]["failed"], 1)
        self.assertEqual(report["failures"], ["tests/test_bad.py"])
        self.assertIn("FAILED", markdown)
        self.assertIn("test_bad.py", markdown)
        bad = [test for test in report["tests"] if test["status"] != "passed"][0]
        self.assertIn("AssertionError", bad["output_tail"])
        self.assertIn("intentional failure", bad["output_tail"])

    def test_empty_directory_is_reported_not_crashed(self):
        code, report, markdown = self._run({})
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["totals"]["total"], 0)
        self.assertIn("no test files found", markdown)
        self.assertEqual(report["tests"], [])

    def test_missing_repository_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-dir"
            json_out = Path(tmp) / "report.json"
            code = regression_report.main(["--root", str(missing), "--tests-dir",
                                           str(missing / "tests"), "--json-out", str(json_out)])
            self.assertEqual(code, 1)
            report = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(report["totals"]["total"], 0)


class ReportShape(unittest.TestCase):
    def test_report_has_the_documented_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = make_tests_dir(root, {"test_ok.py": PASS_SOURCE})
            json_out = root / "out" / "report.json"
            code = regression_report.main(["--root", str(root), "--tests-dir", str(tests_dir),
                                           "--json-out", str(json_out)])
            report = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        for key in ("report_id", "generated_at", "root", "tests_dir", "pattern", "timeout_seconds",
                    "python", "totals", "passed", "failures", "markdown", "tests"):
            self.assertIn(key, report, key)
        self.assertEqual(report["pattern"], "test_*.py")
        self.assertEqual(report["timeout_seconds"], 900.0)
        self.assertRegex(report["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(report["totals"]["skipped"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
