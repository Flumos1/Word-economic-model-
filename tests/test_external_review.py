"""External review of the P2 0.1 release manifest: the adversarial checklist must be honest.

The tests do not trust the stored review either: they recompute the input digests, re-derive the
D1-D6 numbers from the evidence files, and deliberately tamper with a copy of the manifest to prove
that a forged checksum, a forged status, a forged claim or a forged docs/137 is caught.  One test
re-runs the whole checklist twice (determinism) and one exercises the CLI fail-closed path.
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

import external_review as review  # noqa: E402

RELEASE = ROOT / "build" / "p2_release" / "p2_release.json"
FIGARO_MANIFEST = ROOT / "data" / "mrio" / "figaro" / "figaro_2019" / "manifest.json"
if not FIGARO_MANIFEST.exists():
    raise unittest.SkipTest(
        "private FIGARO MRIO data are absent in this checkout (data/mrio, git-ignored)")
WORLD_REPORTS = ROOT / "build" / "world" / "reports"
WORLD_REPORTS_AVAILABLE = WORLD_REPORTS.exists() and any(WORLD_REPORTS.glob("*.json"))
DOC137 = ROOT / "docs" / "137_P2_0.1_релиз.md"
FIXED_TS = "2026-11-14T22:00:00Z"
FAST = dict(check_help=False, check_rebuild=False)


def load_release() -> dict:
    if not RELEASE.is_file():
        raise unittest.SkipTest(f"release manifest not generated yet: {RELEASE}")
    return json.loads(RELEASE.read_text(encoding="utf-8"))


def build(**overrides) -> dict:
    params = dict(release_path=RELEASE, repo=ROOT, generated_at=FIXED_TS)
    params.update(FAST)
    params.update(overrides)
    return review.build_review(**params)


def build_tampered(mutate, **overrides) -> dict:
    manifest = load_release()
    mutate(manifest)
    params = dict(release_path=RELEASE, repo=ROOT, generated_at=FIXED_TS, _release=manifest)
    params.update(FAST)
    params.update(overrides)
    return review.build_review(**params)


def findings_of(report: dict, check_id: str) -> list:
    return next(item["findings"] for item in report["checks"] if item["id"] == check_id)


def checks_of(report: dict, check_id: str) -> dict:
    return next(item for item in report["checks"] if item["id"] == check_id)


def criteria_of(report: dict) -> dict:
    return report["criteria"]


@unittest.skipUnless(WORLD_REPORTS_AVAILABLE,
                     "real MRIO scenario reports are absent (private FIGARO/OECD data)")
class ChecklistOnTheCurrentRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = build()

    def test_schema_release_and_ten_checks(self):
        self.assertEqual(self.review["schema"], review.SCHEMA)
        self.assertEqual(self.review["review_type"], "ADVERSARIAL_INDEPENDENT")
        release = self.review["release"]
        self.assertEqual(release["release"], "P2 0.1")
        self.assertEqual(release["version"], "0.1")
        self.assertEqual(release["status"], "RESEARCH_RELEASE")
        self.assertEqual(release["admission"], False)
        self.assertRegex(release["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual([item["id"] for item in self.review["checks"]],
                         ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10"])
        self.assertEqual(self.review["summary"]["checks"], 10)

    def test_no_blockers_and_every_check_has_a_verdict(self):
        summary = self.review["summary"]
        statuses = {item["id"]: item["status"] for item in self.review["checks"]}
        self.assertNotIn("FAIL", statuses.values(), statuses)
        self.assertIn(summary["status"], ("PASS", "PASS_WITH_FINDINGS"))
        self.assertEqual(summary["failed_checks"], [])
        self.assertEqual(summary["by_severity"]["BLOCKER"], 0)
        self.assertEqual(summary["by_severity"]["HIGH"], 0)
        for check in self.review["checks"]:
            self.assertIn(check["status"], ("PASS", "WARN", "FAIL", "SKIP"))
            for finding in check["findings"]:
                self.assertIn(finding["severity"], review.SEVERITIES)

    def test_all_34_input_digests_are_recomputed(self):
        check = checks_of(self.review, "R2")
        evidence = check["evidence"]
        self.assertEqual(evidence["total"], 34)
        self.assertEqual(len(evidence["inputs"]), 34)
        self.assertEqual(evidence["verified"] + evidence["working_tree_drift"]
                         + evidence["mismatched"], 34)        # independent recomputation: the review must agree with a fresh hash of every input
        independently_ok = 0
        for entry in load_release()["inputs"]:
            path = review._input_path(load_release(), entry, ROOT)
            if path.is_file() and review.sha256_file(path) == entry["sha256"] \
                    and path.stat().st_size == entry["bytes"]:
                independently_ok += 1
        classified_ok = sum(1 for row in evidence["inputs"] if row["status"] == "OK")
        self.assertEqual(classified_ok, independently_ok)
        self.assertGreaterEqual(evidence["verified"], 32)
        for row in evidence["inputs"]:
            if row["status"] in ("MISMATCH", "MISSING"):
                self.fail(f"the current release must verify on the release commit: {row}")

    def test_d1_d6_verdicts_are_confirmed_by_evidence(self):
        criteria = criteria_of(self.review)
        self.assertEqual(sorted(criteria), ["D1", "D2", "D3", "D4", "D5", "D6"])
        for cid in ("D1", "D2", "D5", "D6"):
            self.assertEqual(criteria[cid]["verdict"], "CONFIRMED", cid)
            self.assertEqual(criteria[cid]["claimed_status"], "PASS")
        self.assertEqual(criteria["D3"]["verdict"], "CONFIRMED_WITH_NOTES")
        self.assertEqual(criteria["D4"]["verdict"], "CONFIRMED_WITH_NOTES")
        self.assertEqual(criteria["D4"]["claimed_status"], "RESEARCH_ONLY")

    def test_d1_numbers_are_rederived_from_the_manifests(self):
        evidence = criteria_of(self.review)["D1"]["evidence"]
        self.assertEqual(evidence["figaro"]["countries"], 50)
        self.assertEqual(evidence["figaro"]["industries"], 64)
        self.assertEqual(evidence["figaro"]["sectors"], 3200)
        self.assertAlmostEqual(evidence["figaro"]["coverage_named_pct"], 87.2771, places=6)
        self.assertGreaterEqual(evidence["figaro"]["coverage_named_pct"], 80.0)
        self.assertEqual(evidence["oecd"]["economies"], 71)
        self.assertEqual(evidence["oecd"]["industries"], 34)
        self.assertEqual(evidence["oecd"]["sectors"], 2414)
        self.assertAlmostEqual(evidence["oecd"]["coverage_min_pct"], 95.35, places=6)
        self.assertAlmostEqual(evidence["oecd"]["coverage_max_pct"], 95.47, places=6)
        self.assertEqual(evidence["oecd"]["d1_pass_period"], "2000-2011")
        self.assertEqual(evidence["wiod"]["countries"], [44])
        self.assertEqual(evidence["wiod"]["industries"], [56])
        self.assertEqual(len(evidence["wiod"]["years"]), 15)
        self.assertAlmostEqual(evidence["wiod"]["coverage_min_pct"], 84.3824, places=4)
        self.assertAlmostEqual(evidence["wiod"]["coverage_max_pct"], 89.8466, places=4)
        self.assertTrue(evidence["wiod"]["all_validation_pass"])
        self.assertTrue(evidence["wiod"]["schema_v11"])

    def test_sfc_and_gvar_and_scenarios_numbers(self):
        criteria = criteria_of(self.review)
        sfc = criteria["D3"]["evidence"]["observed"]
        for label in ("figaro", "oecd"):
            self.assertTrue(sfc[label]["sfc_pass"])
            self.assertEqual(sfc[label]["sfc_identity_residual"], 0.0)
            self.assertEqual(sfc[label]["sfc_net_worth_residual"], 0.0)
        self.assertGreater(criteria["D3"]["evidence"]["oecd_material_residual_usd_mn"], 1.0)
        self.assertTrue(criteria["D3"]["evidence"]["absorption_convention_documented"])
        gvar = criteria["D4"]["evidence"]["observed"]
        self.assertEqual(gvar["countries"], 53)
        self.assertEqual(gvar["years"], 30)
        self.assertEqual(gvar["cells"], 32)
        self.assertEqual(gvar["primary_better_cells"], 4)
        self.assertEqual(gvar["baseline_better_cells"], 28)
        self.assertTrue(gvar["oos_negative"])
        self.assertAlmostEqual(gvar["year_weights_mean_delta_rmse"], 0.020518, places=6)
        scenarios = criteria["D5"]["evidence"]["observed"]
        for label in ("figaro", "oecd"):
            self.assertEqual(scenarios[label]["scenario_count"], 8)
            self.assertEqual(scenarios[label]["index_scenario_count"], 8)
            self.assertEqual(scenarios[label]["missing_json"], [])
            self.assertEqual(scenarios[label]["missing_md"], [])
            self.assertTrue(scenarios[label]["all_feasible"])
            self.assertTrue(scenarios[label]["all_sfc_pass"])
            self.assertTrue(scenarios[label]["all_validations_pass"])

    def test_no_admission_claims_and_candidate_states(self):
        check = checks_of(self.review, "R5")
        self.assertEqual(check["status"], "PASS", check["findings"])
        candidates = check["evidence"]
        self.assertEqual(candidates["admitted"], False)
        self.assertEqual(candidates["t1"]["status"], "CANDIDATE")
        self.assertEqual(candidates["regime"]["status"], "SHADOW_TEST")
        self.assertTrue(candidates["regime"]["conditional_data_class"])
        self.assertFalse(candidates["regime_stages"]["LIMITED_DEPLOYMENT_to_ACTIVE"])
        states = checks_of(self.review, "R10")
        self.assertEqual(states["status"], "PASS", states["findings"])
        observed = states["evidence"]["observed"]
        self.assertEqual(observed["t1"]["c4_pass"], False)
        self.assertEqual(observed["shadow"]["c6_resolved"], 0)
        self.assertEqual(observed["shadow"]["c6_min"], 12)
        self.assertEqual(observed["shadow"]["journal_rows"], 11)

    def test_limitations_and_docs_consistency_are_reported(self):
        self.assertEqual(checks_of(self.review, "R8")["status"], "PASS")
        all_tokens = checks_of(self.review, "R8")["evidence"]["tokens"]
        self.assertTrue(all(all_tokens.values()), all_tokens)
        docs = checks_of(self.review, "R6")
        messages = " ".join(finding["message"] for finding in docs["findings"])
        self.assertEqual(docs["status"], "PASS", messages)
        self.assertNotIn("absent from the working tree", messages)
        self.assertFalse(any("32 manifest inputs" in message for message in
                             (finding["message"] for finding in docs["findings"])))


class Tampering(unittest.TestCase):
    def test_a_forged_checksum_is_caught(self):
        def mutate(manifest):
            manifest["inputs"][0]["sha256"] = "0" * 64
        report = build_tampered(mutate)
        findings = findings_of(report, "R2")
        self.assertTrue(any(item["severity"] == "BLOCKER" and
                            report["checks"][1]["evidence"]["inputs"][0]["id"] in item["message"]
                            for item in findings), findings)
        self.assertIn("R2", report["summary"]["failed_checks"])
        self.assertEqual(report["summary"]["status"], "FAIL")

    def test_a_forged_criterion_status_is_caught(self):
        def mutate(manifest):
            manifest["criteria"][0]["status"] = "FAIL"
            manifest["criteria_summary"]["D1"] = "FAIL"
        report = build_tampered(mutate)
        criteria = criteria_of(report)
        self.assertEqual(criteria["D1"]["verdict"], "NOT_CONFIRMED")
        self.assertEqual(criteria["D1"]["claimed_status"], "FAIL")
        self.assertIn("R4", report["summary"]["failed_checks"])
        self.assertEqual(report["summary"]["status"], "FAIL")

    def test_a_forged_evidence_number_is_caught(self):
        def mutate(manifest):
            manifest["world"]["figaro"]["gdp_coverage_named_pct"] = 99.9999
        report = build_tampered(mutate)
        self.assertEqual(criteria_of(report)["D1"]["verdict"], "NOT_CONFIRMED")
        self.assertIn("D1", " ".join(f["message"] for f in findings_of(report, "R4")))

    def test_a_forged_research_only_status_is_caught(self):
        def mutate(manifest):
            manifest["criteria"][3]["status"] = "PASS"
            manifest["criteria_summary"]["D4"] = "PASS"
        report = build_tampered(mutate)
        self.assertIn("R4", report["summary"]["failed_checks"])
        self.assertEqual(report["summary"]["verdicts"]["D4"], "NOT_CONFIRMED")

    def test_admission_and_active_claims_are_caught(self):
        def mutate(manifest):
            manifest["admission"] = True
            manifest["world"]["candidates"]["admitted"] = True
            manifest["world"]["candidates"]["regime"]["stages"]["LIMITED_DEPLOYMENT_to_ACTIVE"] = True
            manifest["limitations"].append("Модель допущена к промышленной эксплуатации")
        report = build_tampered(mutate)
        messages = " ".join(f["message"] for f in findings_of(report, "R5"))
        self.assertIn("admission flag", messages)
        self.assertIn("admitted", messages)
        self.assertIn("ACTIVE", messages)
        self.assertIn("R5", report["summary"]["failed_checks"])

    def test_a_deployment_candidate_status_is_caught(self):
        def mutate(manifest):
            manifest["world"]["candidates"]["t1"]["status"] = "ACTIVE"
        report = build_tampered(mutate)
        self.assertIn("R5", report["summary"]["failed_checks"])
        self.assertIn("R10", report["summary"]["failed_checks"])

    def test_a_forged_docs137_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "137.md"
            text = DOC137.read_text(encoding="utf-8")
            doc.write_text(text.replace("87,2771", "87,0000"), encoding="utf-8")
            report = build(doc=str(doc))
            self.assertIn("R6", report["summary"]["failed_checks"])
            self.assertTrue(any("number mismatch" in item["message"]
                                for item in findings_of(report, "R6")))


class FailClosedAndDeterminism(unittest.TestCase):
    def test_a_missing_or_broken_release_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaises(review.ReviewError):
                review.build_review(release_path=missing, repo=ROOT)
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(review.ReviewError):
                review.build_review(release_path=broken, repo=ROOT)
            out = Path(tmp) / "out"
            code = review.main(["--release", str(missing), "--out-dir", str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_two_runs_are_identical_except_generated_at(self):
        first = build(generated_at=FIXED_TS)
        second = build(generated_at=FIXED_TS)
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))
        other = build(generated_at="2026-11-14T23:59:59Z")
        first.pop("generated_at")
        other.pop("generated_at")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(other, sort_keys=True))

    @unittest.skipUnless(WORLD_REPORTS_AVAILABLE,
                         "real MRIO scenario reports are absent (private FIGARO/OECD data)")
    def test_cli_help_and_cli_run(self):
        help_done = subprocess.run([sys.executable, str(ROOT / "tools" / "external_review.py"),
                                    "--help"], capture_output=True, text=True, timeout=180)
        self.assertEqual(help_done.returncode, 0)
        self.assertIn("usage", help_done.stdout.lower())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "review"
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "external_review.py"),
                                   "--no-rebuild", "--no-help",
                                   "--generated-at", FIXED_TS, "--out-dir", str(out)],
                                  capture_output=True, text=True, timeout=300, cwd=str(ROOT))
            self.assertIn(done.returncode, (0,), done.stderr)
            summary = json.loads(done.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["release"], "P2 0.1")
            self.assertFalse(summary["failed_checks"], summary["failed_checks"])
            payload = json.loads((out / "review.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], review.SCHEMA)
            markdown = (out / "review.md").read_text(encoding="utf-8")
            self.assertIn("## Verdict:", markdown)
            self.assertIn("D1-D6 verdicts", markdown)
            # the markdown is rendered from the same document, deterministically
            self.assertEqual(review.render_markdown(payload),
                             (out / "review.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
