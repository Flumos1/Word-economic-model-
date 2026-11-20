"""Tests for the G6/beta readiness audit (docs/196).

The suite does not trust the audit: it tampers with copies of the artefacts and asserts that the
checklist catches a false PASS (admission, ACTIVE stage, C6 MET, forged signature, stale docs), that
it fails closed on missing inputs and that the document is deterministic.  Product test suites and
release_check are not executed inside the tests (``run_tests=False``); their live results are the
job of the CLI run documented in docs/196.
"""
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import beta_audit as ba  # noqa: E402

DOCS_196 = ROOT / "docs" / "196_Beta_готовность.md"


def load(name: str) -> dict:
    return json.loads((ROOT / ba.ARTIFACTS[name]).read_text(encoding="utf-8"))


def docs_text(name: str) -> str:
    return (ROOT / ba.DOCS[name]).read_text(encoding="utf-8")


def audit(**overrides) -> dict:
    params = dict(repo=ROOT, run_tests=False, run_release_check=False)
    params.update(overrides)
    return ba.build_audit(**params)


def findings_of(report: dict, check_id: str) -> list:
    return next(item["findings"] for item in report["checks"] if item["id"] == check_id)


def blocker_ids(report: dict, prefix: str) -> list:
    return [item["id"] for item in report["blockers"] if item["id"].startswith(prefix)]


class AuditStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = audit()

    def test_schema_gates_and_checks(self):
        self.assertEqual(self.report["schema"], ba.SCHEMA)
        self.assertEqual(self.report["audit_date"], ba.AUDIT_DATE)
        self.assertEqual(sorted(self.report["gates"]), ["G1", "G2", "G3", "G4", "G5", "G6"])
        for gate, block in self.report["gates"].items():
            self.assertIn(block["status"], ba.GATE_STATUSES, gate)
            self.assertIn("blocking_findings", block)
        check_ids = [item["id"] for item in self.report["checks"]]
        self.assertIn("A1", check_ids)
        self.assertIn("A6", check_ids)
        self.assertIn("A12", check_ids)
        self.assertIn(self.report["summary"]["verdict"], ("BETA_READY", "BETA_NOT_READY"))

    def test_verdict_matches_blockers(self):
        summary = self.report["summary"]
        if summary["blockers"]:
            self.assertEqual(summary["verdict"], "BETA_NOT_READY")
            self.assertTrue(summary["gates_not_passed"])
            self.assertIn("G6", summary["gates_not_passed"])
        else:
            self.assertEqual(summary["verdict"], "BETA_READY")

    def test_blockers_are_complete_and_sorted(self):
        for item in self.report["blockers"]:
            for key in ("id", "gate", "check", "severity", "message", "evidence"):
                self.assertIn(key, item)
            self.assertIn(item["severity"], ("BLOCKER", "HIGH"))
            self.assertIn(item["gate"], ("G1", "G2", "G3", "G4", "G5", "G6"))
        order = [ba.SEVERITIES.index(item["severity"]) for item in self.report["blockers"]]
        self.assertEqual(order, sorted(order))

    def test_current_state_expectations(self):
        # G1/G2 closed; G4 blocked by the PROPOSED pre-registration and the V2 verdict
        self.assertEqual(self.report["gates"]["G1"]["status"], "PASS")
        self.assertEqual(self.report["gates"]["G2"]["status"], "PASS")
        self.assertIn(self.report["gates"]["G4"]["status"], ("BLOCKED", "FAIL"))
        prereg = load("prereg")
        if prereg.get("status") != "ACTIVE":
            self.assertTrue(blocker_ids(self.report, "A4"))
        v2 = docs_text("v2_review")
        if "NOT_CONFIRMED" in v2:
            self.assertTrue(any("V2" in item["message"] for item in self.report["blockers"]))

    def test_c6_blocker_matches_the_journal(self):
        c6 = load("c6_report")["c6"]
        audit_c6 = self.report["c6"]
        self.assertEqual(audit_c6["n_resolved"], c6.get("n_resolved"))
        self.assertEqual(audit_c6["distinct_periods"], c6.get("distinct_target_periods"))
        if int(c6.get("n_resolved") or 0) < 12 or int(c6.get("distinct_target_periods") or 0) < 4:
            self.assertTrue(any(item["id"].startswith("A8") for item in self.report["blockers"]))

    def test_admission_and_claims_are_clean(self):
        admission = next(item for item in self.report["checks"] if item["id"] == "A7")
        self.assertEqual(admission["status"], "PASS", admission["findings"])
        self.assertIn("claims_not_allowed", admission["evidence"])
        for claim in ("ACTIVE", "C6 пройден", "beta 2.0 готова"):
            self.assertIn(claim, admission["evidence"]["claims_not_allowed"])


class FalsePassDetection(unittest.TestCase):
    def test_admission_true_is_caught(self):
        tampered = copy.deepcopy(load("p2_release"))
        tampered["admission"] = True
        report = audit(overrides={"p2_release": tampered})
        self.assertEqual(report["gates"]["G6"]["status"], "NOT_READY")
        self.assertTrue(any("admission=false" in item["message"]
                            for item in findings_of(report, "A7")))

    def test_active_stage_is_caught(self):
        tampered = copy.deepcopy(load("p2_release"))
        tampered["world"]["candidates"]["regime"]["stages"]["LIMITED_DEPLOYMENT_to_ACTIVE"] = True
        report = audit(overrides={"p2_release": tampered})
        messages = " ".join(item["message"] for item in findings_of(report, "A7"))
        self.assertIn("ACTIVE", messages)

    def test_c6_met_requires_the_counters(self):
        tampered = copy.deepcopy(load("c6_report"))
        tampered["c6"].update({"n_resolved": 12, "distinct_target_periods": 4, "gate": "MET"})
        report = audit(overrides={"c6_report": tampered})
        self.assertFalse(any(item["id"].startswith("A8") for item in report["blockers"]))
        # a "MET" gate with low counters must be flagged as a contradictory artefact
        tampered["c6"].update({"n_resolved": 0, "distinct_target_periods": 1})
        report = audit(overrides={"c6_report": tampered})
        messages = " ".join(item["message"] for item in findings_of(report, "A8"))
        self.assertIn("claims MET", messages)

    def test_prereg_activation_must_be_visible(self):
        tampered = copy.deepcopy(load("prereg"))
        tampered["status"] = "ACTIVE"
        report = audit(overrides={"prereg": tampered})
        messages = " ".join(item["message"] for item in findings_of(report, "A4"))
        self.assertNotIn("PROPOSED", messages)
        # the PROPOSED state in the real registry is a blocker
        messages_real = " ".join(item["message"] for item in findings_of(audit(), "A4"))
        self.assertIn("PROPOSED", messages_real)

    def test_v2_verdict_is_read_from_the_review(self):
        confirmed = "Вердикт: CONFIRMED. Пакет подтверждён."
        report = audit(overrides={"_docs": {"v2_review": confirmed}})
        messages = " ".join(item["message"] for item in findings_of(report, "A4"))
        self.assertNotIn("V2 verdict is not CONFIRMED", messages)
        report = audit(overrides={"_docs": {"v2_review": "verdict NOT_CONFIRMED"}})
        messages = " ".join(item["message"] for item in findings_of(report, "A4"))
        self.assertIn("V2 verdict is not CONFIRMED", messages)

    def test_forbidden_claim_in_public_document_is_caught(self):
        readme = "Статус: beta 2.0 готова, модель допущена, C6 пройден."
        report = audit(overrides={"_readme": readme})
        messages = " ".join(item["message"] for item in findings_of(report, "A7"))
        self.assertIn("beta 2.0", messages)
        self.assertIn("model admitted", messages)

    def test_forged_signature_is_caught(self):
        tampered = copy.deepcopy(load("signoff_registry"))
        tampered["signoffs"][0]["object"]["sha256"] = "0" * 64
        report = audit(overrides={"signoff_registry": tampered})
        messages = " ".join(item["message"] for item in findings_of(report, "A9"))
        self.assertIn("SHA-256 does not recompute", messages)

    def test_stale_readme_is_caught(self):
        readme = ("Актуально на **18 ноября 2026**. G5 начат: FLAKS и Simulator UI впереди.")
        report = audit(overrides={"_readme": readme})
        messages = " ".join(item["message"] for item in findings_of(report, "A11"))
        self.assertIn("18 November", messages)
        self.assertIn("FLAKS", messages)

    def test_raw_data_violation_is_caught(self):
        tracked = {"available": True, "files": ["docs/164_План_и_статус_проекта.md",
                                                "data/private/raw_series.csv"]}
        report = audit(overrides={"_git_tracked": tracked})
        messages = " ".join(item["message"] for item in findings_of(report, "A10"))
        self.assertIn("raw third-party data", messages)
        clean = {"available": True, "files": ["docs/164_План_и_статус_проекта.md",
                                              "registry/forecasts.jsonl"]}
        report = audit(overrides={"_git_tracked": clean})
        messages = " ".join(item["message"] for item in findings_of(report, "A10"))
        self.assertNotIn("raw third-party data", messages)


class FailClosedAndDeterminism(unittest.TestCase):
    def test_missing_required_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ba.AuditError):
                ba.build_audit(repo=Path(tmp), run_tests=False, run_release_check=False)
            out = Path(tmp) / "out"
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "beta_audit.py"),
                                   "--repo", tmp, "--no-tests", "--out", str(out)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            self.assertEqual(done.returncode, 2)
            self.assertIn("BLOCKED_AUDIT", done.stderr)
            self.assertFalse(out.exists())

    def test_audit_is_deterministic(self):
        first = audit()
        second = audit()
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_cli_help_and_run(self):
        help_done = subprocess.run([sys.executable, str(ROOT / "tools" / "beta_audit.py"), "--help"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(help_done.returncode, 0)
        self.assertIn("usage", help_done.stdout.lower())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit"
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "beta_audit.py"),
                                   "--no-tests", "--json", "--out", str(out)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(done.returncode, 0,
                             (done.stderr or "") + " | stdout=" + str(done.stdout)[:200])
            payload = json.loads(done.stdout)
            self.assertEqual(payload["schema"], ba.SCHEMA)
            self.assertTrue((out / "report.json").is_file())
            self.assertTrue((out / "report.md").is_file())
            markdown = (out / "report.md").read_text(encoding="utf-8")
            self.assertIn("Beta readiness audit", markdown)
            self.assertIn("Gate status", markdown)

    def test_gate_status_helper(self):
        self.assertEqual(ba._gate_status([], "G1"), "PASS")
        medium = [{"gate": "G1", "severity": "MEDIUM"}]
        self.assertEqual(ba._gate_status(medium, "G1"), "PASS_WITH_NOTES")
        high = [{"gate": "G1", "severity": "HIGH"}]
        self.assertEqual(ba._gate_status(high, "G1"), "FAIL")

    def test_docs_196_exists_and_states_the_verdict(self):
        self.assertTrue(DOCS_196.is_file(), f"missing {DOCS_196}")
        text = DOCS_196.read_text(encoding="utf-8")
        for token in ("G1", "G6", "BETA_NOT_READY", "блокер", "C6", "prereg"):
            self.assertIn(token, text)
        self.assertIn("beta", text.lower())
        self.assertIn("BETA_NOT_READY", text)
        self.assertIn("tests/test_beta_audit.py", text)


class LiveEvidenceConsistency(unittest.TestCase):
    """The audit must agree with the live artefacts it reads."""

    def test_registry_and_api_agree(self):
        registry = load("modules_report")
        api = load("api_index")
        self.assertEqual(set(registry["modules_present"]),
                         {item["module_id"] for item in api["modules"]})
        self.assertTrue(registry["all_research_only"])

    def test_sources_registry_is_consistent(self):
        registry = load("source_registry")
        statuses = {str(item.get("status")) for item in registry["sources"]}
        self.assertTrue(all(re.match(r"(VERIFIED|ACCESS_VERIFIED|CATALOGUED|NOT_DOWNLOADED)$",
                                         status) for status in statuses))
        audit_report = audit()
        check = next(item for item in audit_report["checks"] if item["id"] == "A10")
        self.assertEqual(check["evidence"]["sources_total"], len(registry["sources"]))

    def test_product_artifacts_detected(self):
        report = audit()
        check = next(item for item in report["checks"] if item["id"] == "A5")
        self.assertTrue(check["evidence"]["api"]["schema"].startswith("amfe.forecast_api"))
        self.assertGreaterEqual(check["evidence"]["api"]["counts"]["modules"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
