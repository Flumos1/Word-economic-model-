"""Release checklist: report structure, mutation sensitivity, determinism, skip-tests and offline use.

Mutation tests run on a temporary copy of the repository. The copy is made internally consistent for
the blocker checks (the comparator composition's licence-register digest is refreshed and the data
log is extended to every planned series with synthetic valid entries), so a mutation of one artefact
produces exactly the expected verdict instead of inheriting the in-flight state of the working tree.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_check  # noqa: E402

FIXED_DATE = "2026-11-12"
COPY_IGNORE = shutil.ignore_patterns(".git", "data", ".tmp-test", "__pycache__", "build")


# -------------------------------------------------------------------------------------- fixtures
def build_fixture(base: Path) -> Path:
    """Copy the repository and make the artefact checks internally consistent (blockers pass)."""
    root = base / "repo"
    shutil.copytree(ROOT, root, ignore=COPY_IGNORE)
    protocols = root / "protocols"
    # 1) the composition is re-pinned against the current licence register (the working tree may
    #    carry an in-flight licence revision); this touches a copy only
    compositions = sorted(protocols.glob("comparator_composition_*.json"))
    if compositions:
        composition = compositions[-1]
        document = json.loads(composition.read_text(encoding="utf-8"))
        document.setdefault("licence_register", {})["sha256"] = release_check.lf_sha256(
            protocols / "licence_register.json")
        composition.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        composition.with_suffix(".sha256").write_text(
            release_check.lf_sha256(composition) + "  " + composition.name + " (fixture)\n",
            encoding="utf-8")
    # 2) extend the data log to every planned vintage series with synthetic valid entries
    plan = json.loads((protocols / "data_plan.json").read_text(encoding="utf-8"))
    log_path = protocols / "data_log.json"
    data_log = json.loads(log_path.read_text(encoding="utf-8"))
    logged = {entry.get("series") for entry in data_log.get("entries", [])
              if entry.get("vintage") == "set"}
    for series in plan.get("series", []):
        if not series.get("vintages") or series.get("id") in logged:
            continue
        data_log["entries"].append({
            "series": series.get("id"), "register": series.get("register"),
            "vintage": "set", "url": "fixture", "retrieved_at": FIXED_DATE + "T00:00:00Z",
            "bytes": 100, "sha256": "a" * 64, "file": f"data/raw/{series.get('id')}/vintages.zip",
            "planned_vintages": series.get("vintages"), "vintage_dates": ["1990-03-20"],
            "unmapped_planned_dates": [], "available_vintage_dates": ["1990-03-20"],
            "vintage_count": 1, "rows": 1, "vintage_last_observation": {},
            "vintage_check": True, "is_csv": True, "chunks": [], "merged": {"from_chunks": 1},
        })
    data_log["failures"] = []
    log_path.write_text(json.dumps(data_log, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return root


def run_check(root: Path, out_dir: Path, skip_tests: bool = True, extra=None) -> tuple:
    out = out_dir / "release_check_report.json"
    argv = ["--root", str(root), "--out", str(out), "--date", FIXED_DATE,
            f"--skip-tests={'true' if skip_tests else 'false'}"] + list(extra or [])
    code = release_check.main(argv)
    report = json.loads(out.read_text(encoding="utf-8"))
    return code, report


def check_by_id(report: dict, check_id: str) -> dict:
    matches = [check for check in report["checks"] if check["id"] == check_id]
    if not matches:
        raise AssertionError(f"check {check_id} not found in {[c['id'] for c in report['checks']]}")
    return matches[0]


# ------------------------------------------------------------------------------- report structure
class ReportStructure(unittest.TestCase):
    """The tool must describe the real working tree, whatever its current state is."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.code, cls.report = run_check(ROOT, cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_top_level_structure(self):
        report = self.report
        for key in ("report_id", "tool", "tool_version", "generated_at", "root", "options",
                    "status", "exit_code", "counts", "blockers", "warnings", "skipped",
                    "checks", "known_limitations", "markdown", "executed_tests"):
            self.assertIn(key, report, key)
        self.assertEqual(report["report_id"], "AMFE-RELEASE-CHECK")
        self.assertEqual(report["tool"], "tools/release_check.py")
        self.assertEqual(report["generated_at"], "2026-11-12T00:00:00Z")
        self.assertIn(report["status"], release_check.STATUSES)
        self.assertEqual(self.code, report["exit_code"])

    def test_counts_and_status_are_consistent(self):
        report = self.report
        checks = report["checks"]
        self.assertEqual(report["counts"]["checks"], len(checks))
        self.assertEqual(report["counts"]["passed"],
                         sum(1 for check in checks if check["status"] == "PASS"))
        self.assertEqual(report["counts"]["failed"],
                         sum(1 for check in checks if check["status"] == "FAIL"))
        self.assertEqual(report["counts"]["skipped"],
                         sum(1 for check in checks if check["status"] == "SKIP"))
        expected_exit = {"PASS": 0, "FAIL": 1, "CONDITIONAL": 2}[report["status"]]
        self.assertEqual(report["exit_code"], expected_exit)
        self.assertGreaterEqual(len(checks), 15)

    def test_every_check_has_the_documented_shape(self):
        for check in self.report["checks"]:
            for key in ("id", "group", "severity", "status", "detail"):
                self.assertIn(key, check, check.get("id"))
            self.assertIn(check["group"], release_check.GROUPS)
            self.assertIn(check["severity"], release_check.SEVERITIES)
            self.assertIn(check["status"], release_check.STATUSES)

    def test_known_limitations_describe_the_research_state(self):
        text = " ".join(self.report["known_limitations"]).lower()
        self.assertIn("forward", text)
        self.assertTrue("candidate" in text or "c1" in text)
        self.assertIn("ci", text)

    def test_regression_is_skipped_in_the_fast_mode(self):
        regression = check_by_id(self.report, "tests.regression")
        self.assertEqual(regression["status"], "SKIP")
        self.assertTrue(self.report["options"]["skip_tests"])
        self.assertIsNone(self.report["executed_tests"])
        self.assertIn("tests.regression", self.report["skipped"])


class FixtureVerdict(unittest.TestCase):
    """A consistent artefact tree must have no blockers (warnings are allowed by design)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = build_fixture(cls.base)
        cls.code, cls.report = run_check(cls.root, cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_blockers_and_conditional_status(self):
        self.assertEqual(self.report["blockers"], [])
        self.assertEqual(self.report["status"], "CONDITIONAL")
        self.assertEqual(self.code, 2)
        self.assertIn("tests.regression", self.report["skipped"])

    def test_blocker_checks_pass_on_the_fixture(self):
        for check_id in ("frozen.protocol_v4", "frozen.r3_specification", "frozen.nf_sim",
                         "frozen.composition", "frozen.chain", "reports.evaluation",
                         "reports.r2_gate", "reports.data_log", "reports.no_third_party_values",
                         "docs.required", "docs.links"):
            self.assertEqual(check_by_id(self.report, check_id)["status"], "PASS", check_id)

    def test_expected_warnings_are_reported(self):
        warning_ids = [check["id"] for check in self.report["checks"]
                       if check["severity"] == "warning" and check["status"] == "FAIL"]
        self.assertIn("reports.references_current", warning_ids)
        self.assertIn("reports.forward_registry", warning_ids)
        # after the 2026-11-12 data refresh the gate report covers every planned vintage series
        coverage = check_by_id(self.report, "reports.plan_coverage")
        self.assertEqual(coverage["severity"], "warning")
        self.assertEqual(coverage["status"], "PASS", coverage)


class Mutations(unittest.TestCase):
    """One mutated artefact must move the verdict exactly like a release checker should."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = build_fixture(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def mutate(self, path: Path, write) -> tuple:
        original = path.read_bytes()
        try:
            write(path)
            return run_check(self.root, self.base)
        finally:
            path.write_bytes(original)

    def test_frozen_file_mutation_fails(self):
        path = self.root / "protocols" / "evaluation_protocol_v4.json"
        code, report = self.mutate(
            path, lambda p: p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("frozen.protocol_v4", report["blockers"])

    def test_pin_mutation_fails(self):
        path = self.root / "protocols" / "r3_specification.sha256"
        code, report = self.mutate(path, lambda p: p.write_text("0" * 64 + "  r3.json\n",
                                                               encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("frozen.r3_specification", report["blockers"])

    def test_evaluation_report_mutation_fails(self):
        path = self.root / "protocols" / "first_real_evaluation_report.json"

        def write(p):
            document = json.loads(p.read_text(encoding="utf-8"))
            document["data_class"] = "PSEUDO_REAL_TIME"
            p.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        code, report = self.mutate(path, write)
        self.assertEqual(code, 1)
        self.assertIn("reports.evaluation", report["blockers"])

    def test_gate_report_mutation_fails(self):
        path = self.root / "protocols" / "r2_gate_report.json"

        def write(p):
            document = json.loads(p.read_text(encoding="utf-8"))
            document["passed"] = False
            document["totals"]["failures"] = 3
            p.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        code, report = self.mutate(path, write)
        self.assertEqual(code, 1)
        self.assertIn("reports.r2_gate", report["blockers"])

    def test_gitignore_mutation_fails(self):
        path = self.root / ".gitignore"
        code, report = self.mutate(path, lambda p: p.write_text("# nothing\n", encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("repo.gitignore_data", report["blockers"])

    def test_broken_readme_link_fails(self):
        path = self.root / "README.md"
        code, report = self.mutate(
            path, lambda p: p.write_text(
                p.read_text(encoding="utf-8") + "\n[битая ссылка](docs/no_such_document.md)\n",
                encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("docs.links", report["blockers"])

    def test_plan_expansion_without_a_new_log_fails(self):
        path = self.root / "protocols" / "data_plan.json"

        def write(p):
            document = json.loads(p.read_text(encoding="utf-8"))
            document["series"].append({"id": "NEW_SERIES", "register": "ALFRED:NEW_SERIES",
                                       "url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NEW_SERIES",
                                       "vintages": ["2020-03-31"], "purpose": "fixture"})
            p.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        code, report = self.mutate(path, write)
        self.assertEqual(code, 1)
        self.assertIn("reports.data_log", report["blockers"])
        self.assertIn("NEW_SERIES", json.dumps(report["checks"], ensure_ascii=False))

    def test_stale_evaluation_reference_is_conditional(self):
        path = self.root / "protocols" / "first_real_evaluation_report.json"

        def write(p):
            document = json.loads(p.read_text(encoding="utf-8"))
            document["r3_specification"]["sha256"] = "0" * 64
            p.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        code, report = self.mutate(path, write)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "CONDITIONAL")
        self.assertEqual(report["blockers"], [])
        self.assertIn("reports.references_current", report["warnings"])

    def test_missing_documents_do_not_crash_and_fail(self):
        empty = self.base / "empty"
        empty.mkdir(exist_ok=True)
        out_dir = self.base / "empty-out"
        out_dir.mkdir(exist_ok=True)
        code, report = run_check(empty, out_dir)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(report["checks"])


class IndexCheck(unittest.TestCase):
    def test_tracked_raw_data_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(ROOT, root, ignore=COPY_IGNORE)
            for command in (["git", "init", "-q"], ["git", "config", "user.email", "t@example.com"],
                            ["git", "config", "user.name", "test"]):
                subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=120)
            (root / "data" / "raw").mkdir(parents=True)
            (root / "data" / "raw" / "CPIAUCSL").mkdir()
            (root / "data" / "raw" / "CPIAUCSL" / "latest.csv").write_text("date,value\n",
                                                                           encoding="utf-8")
            # /data/ is in .gitignore, so the raw file can only enter the index with -f
            subprocess.run(["git", "add", "-f", "data/raw/CPIAUCSL/latest.csv"], cwd=str(root),
                           capture_output=True, text=True, timeout=120)
            subprocess.run(["git", "add", "README.md"], cwd=str(root), capture_output=True,
                           text=True, timeout=120)
            facts = release_check.collect_facts(root)
            checks = release_check.check_repository(facts)
            failed = [check["id"] for check in checks if check["status"] == "FAIL"]
            self.assertIn("repo.no_tracked_data", failed)
            self.assertIn("repo.no_raw_archives", failed)


class DeterminismAndCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = build_fixture(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fast_report_is_deterministic(self):
        first = self.base / "first"
        second = self.base / "second"
        first.mkdir()
        second.mkdir()
        code_a, report_a = run_check(self.root, first)
        code_b, report_b = run_check(self.root, second)
        self.assertEqual(code_a, code_b)
        self.assertEqual(json.dumps(report_a, ensure_ascii=False, sort_keys=True),
                         json.dumps(report_b, ensure_ascii=False, sort_keys=True))

    def test_markdown_summary_is_written(self):
        out_dir = self.base / "markdown"
        out_dir.mkdir()
        markdown = out_dir / "summary.md"
        code, report = run_check(self.root, out_dir, extra=["--markdown-out", str(markdown)])
        self.assertTrue(markdown.is_file())
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("# AMFE — релизный чеклист", text)
        self.assertIn(report["status"], text)
        self.assertIn("Известные ограничения", text)

    def test_invalid_date_returns_two(self):
        code = release_check.main(["--root", str(self.root), "--skip-tests=true",
                                   "--date", "2026-13-40", "--out", str(self.base / "unused.json")])
        self.assertEqual(code, 2)

    def test_the_tool_does_not_import_network_modules(self):
        source = (ROOT / "tools" / "release_check.py").read_text(encoding="utf-8")
        for token in ("import urllib", "import requests", "import socket", "http.client",
                      "urlopen"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
