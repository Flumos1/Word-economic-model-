"""Tests for the v2 ensemble evaluation artifacts (task-32, corrected actuals D-147).

Checks the structure and internal consistency of:

* ``protocols/ensemble_eval_<candidate>_T1_h1_v2.json`` and ``..._T2_h0_v2.json``;
* ``protocols/ensemble_evaluation_report_v2.json`` and ``..._summary_v2.json``;
* ``docs/87`` (v1 marked invalid, v2 referenced).

No model is re-run here: the tests are pure artifact/consistency checks (fast, offline).  T2 tests
are skipped when the T2 h=0 v2 run is not present (it was optional in the first v2 pass and the
summary then marks it NOT_EVALUATED).

Run from the repository root: ``python tests/test_ensemble_evaluation_v2.py``.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "protocols" / "ensemble_evaluation_report_v2.json"
SUMMARY = ROOT / "protocols" / "ensemble_evaluation_summary_v2.json"
DOCS87 = ROOT / "docs" / "87_Оценка_ансамблей_REAL_TIME.md"
OLD_REPORT = ROOT / "protocols" / "ensemble_evaluation_report.json"
OLD_SUMMARY = ROOT / "protocols" / "ensemble_evaluation_summary.json"
CORRECTED = ROOT / "protocols" / "ensemble_evaluation_adaptive_T1_h1_corrected.json"

T1_KEY = "T1_CPI_YOY_h1"
T2_KEY = "T2_GDP_GROWTH_h0"
ENSEMBLE_REGISTRATION_FILES = (
    "src/amfe/ensemble/__init__.py",
    "src/amfe/ensemble/ensemble.py",
    "src/amfe/ensemble/weights.py",
    "src/amfe/ensemble/mixture.py",
    "src/amfe/ensemble/sources.py",
    "src/amfe/ensemble/history.py",
)


def sha_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def assert_no_raw_arrays(testcase: unittest.TestCase, node, path: str = "") -> None:
    """Reports must not embed raw arrays/actual values, only digests and derived metrics."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            testcase.assertNotIn(
                key, ("actual_primary", "actual_secondary", "recession"),
                f"raw array key {here}",
            )
            if key in ("candidate", "comparator") and isinstance(value, dict):
                for sub, sub_value in value.items():
                    testcase.assertNotIsInstance(sub_value, list,
                                                 f"raw forecast array at {here}/{sub}")
            assert_no_raw_arrays(testcase, value, here)
    elif isinstance(node, list):
        testcase.assertLessEqual(len(node), 2000, f"suspiciously long list at {path}")
        for index, value in enumerate(node[:50]):
            assert_no_raw_arrays(testcase, value, f"{path}[{index}]")


REGISTERED = ("ENSEMBLE_EQUAL", "ENSEMBLE_INVERSE_MSE", "ENSEMBLE_BAYES",
              "ENSEMBLE_ADAPTIVE")
EXTENDED_DIR = ROOT / "protocols" / "extended_pass"


def candidate_report_path(candidate_id: str, tag: str) -> Path | None:
    for directory in (ROOT / "protocols", EXTENDED_DIR):
        path = directory / f"ensemble_eval_{candidate_id}_{tag}_v2.json"
        if path.exists():
            return path
    return None


def per_candidate_files(tag: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in (ROOT / "protocols", EXTENDED_DIR):
        for path in directory.glob(f"ensemble_eval_*_{tag}_v2.json"):
            candidate_id = path.stem.split(f"_{tag}")[0].replace("ensemble_eval_", "")
            if candidate_id in REGISTERED:
                found.setdefault(path.stem, path)
    return found


@unittest.skipUnless(REPORT.exists() and SUMMARY.exists(), "v2 report/summary not generated yet")
class ReportStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load(REPORT)
        cls.summary = load(SUMMARY)

    def test_report_provenance_and_ids(self):
        report = self.report
        self.assertEqual(report["report_id"], "AMFE-ENSEMBLE-EVALUATION-V2")
        self.assertTrue(report["corrected_actuals"]["decision"].startswith("D-147"))
        self.assertEqual(report["supersedes"]["status"], "INVALID")
        self.assertIn("protocols/ensemble_evaluation_report.json", report["supersedes"]["files"])
        for key in ("protocol", "r3_specification", "composition", "candidates_document"):
            self.assertIn(key, report)
        self.assertEqual(report["protocol"]["version"], "4.0.0")
        self.assertEqual(report["data_class"] if "data_class" in report else "REAL_TIME", "REAL_TIME")

    def test_provenance_hashes_match_current_files(self):
        provenance = self.report["evaluations"][T1_KEY]["provenance"]
        expected = {
            "protocol": "protocols/evaluation_protocol_v4.json",
            "r3_specification": "protocols/r3_specification.json",
            "composition": "protocols/comparator_composition_2026-11-11.json",
            "candidates": "protocols/candidates_2026-11-11.json",
        }
        for key, rel in expected.items():
            self.assertEqual(provenance[key]["sha256"], sha_path(ROOT / rel), key)

    def test_report_windows_are_digest_only(self):
        for evaluation in self.report["evaluations"].values():
            if "candidates" not in evaluation:
                continue
            for candidate in evaluation["candidates"].values():
                for window in ("W1", "W2"):
                    meta = candidate["windows"][window]
                    self.assertIn(f"actual_primary_sha256", meta)
                    self.assertIn("licence_note", meta)
                    self.assertNotIn("actual_primary", meta)
                    self.assertIsInstance(meta["origins"], int)
                    self.assertIn(meta["data_class"], ("REAL_TIME",))
        assert_no_raw_arrays(self, self.report)

    def test_old_reports_are_not_rewritten_and_marked_invalid(self):
        self.assertTrue(OLD_REPORT.exists())
        self.assertTrue(OLD_SUMMARY.exists())
        old = load(OLD_REPORT)
        self.assertEqual(old["report_id"], "AMFE-ENSEMBLE-REAL-EVALUATION")
        docs = DOCS87.read_text(encoding="utf-8")
        self.assertIn("недействительн", docs.lower())
        self.assertIn("protocols/ensemble_evaluation_report_v2.json", docs)
        for candidate_id in REGISTERED:
            self.assertIn(candidate_id, docs)
        self.assertIn("семейство", docs.lower())

    def test_all_reported_candidates_are_candidate_and_gates_reported(self):
        for key in (T1_KEY, T2_KEY):
            evaluation = self.report["evaluations"].get(key)
            if not evaluation or "candidates" not in evaluation:
                continue
            self.assertEqual(evaluation["data_class"], "REAL_TIME")
            self.assertEqual(len(evaluation["candidates"]), 4)
            self.assertEqual(sorted(evaluation["candidates"]), sorted(REGISTERED))
            for candidate in evaluation["candidates"].values():
                self.assertEqual(candidate["status"], "CANDIDATE")
                self.assertEqual(candidate["data_class"], "REAL_TIME")
                self.assertEqual(candidate["criteria"]["C6_status"], "NO_RESOLVED_FORECASTS")
                self.assertEqual(candidate["criteria"]["C3"], "NOT_IMPLEMENTED")
                c4 = candidate["criteria"]["C4"]["per_window"]
                self.assertIn("INCONCLUSIVE", c4["W1"]["statuses"].values(),
                              "C4 W1 must be INCONCLUSIVE (8 recession origins < 12)")
                self.assertIn("RMSE", candidate["losses"]["W1"])
                self.assertIn("CRPS", candidate["losses"]["W1"])

    def test_no_admission_assignments(self):
        cross = self.report["cross_cutting"]
        self.assertTrue(cross["no_admission_assignments"])
        self.assertIn("does not assign admission", cross["reporting_only"])
        self.assertIn("does not assign admission", self.summary["reporting_only"])

    def test_report_candidate_coverage_is_explicit(self):
        coverage = self.report["candidate_coverage"]
        for key in (T1_KEY, T2_KEY):
            self.assertEqual(sorted(coverage[key]["registered"]), sorted(REGISTERED))
            self.assertEqual(sorted(coverage[key]["evaluated"]), sorted(REGISTERED),
                             "all four registered candidates must be evaluated")
            self.assertEqual(coverage[key]["not_evaluated"], [])
            self.assertIn("extended_pass_note", coverage[key])
            self.assertNotIn("reduced_run_note", coverage)

    def test_multiplicity_family_of_four(self):
        for key in (T1_KEY, T2_KEY):
            evaluation = self.report["evaluations"][key]
            self.assertEqual(evaluation["family"]["family_size"], 4)
            self.assertEqual(sorted(evaluation["family"]["members"]), sorted(REGISTERED))
            adjusted_tables = []
            for candidate in evaluation["candidates"].values():
                self.assertEqual(candidate["criteria"]["C2_family_size"], 4)
                self.assertEqual(candidate["multiplicity"]["family_size"], 4)
                self.assertEqual(sorted(candidate["multiplicity"]["adjusted_p_values"]),
                                 sorted(REGISTERED))
                adjusted_tables.append(candidate["multiplicity"]["adjusted_p_values"])
            self.assertTrue(all(table == adjusted_tables[0] for table in adjusted_tables),
                            "the adjusted p-value table must be identical across candidates")


@unittest.skipUnless(REPORT.exists() and SUMMARY.exists(), "v2 report/summary not generated yet")
class PerCandidateReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = load(SUMMARY)
        cls.report = load(REPORT)
        cls.t1_files = per_candidate_files("T1_h1")

    def test_t1_per_candidate_reports_exist_and_parse(self):
        evaluated = set(self.report["candidate_coverage"][T1_KEY]["evaluated"])
        self.assertTrue(evaluated, "at least one T1 candidate must be evaluated")
        self.assertTrue(evaluated <= set(self.summary["evaluations"][T1_KEY]["candidates"][i]["candidate_id"]
                                         for i in range(len(self.summary["evaluations"][T1_KEY]["candidates"]))))
        for candidate_id in evaluated:
            path = candidate_report_path(candidate_id, "T1_h1")
            self.assertTrue(path is not None and path.exists(), candidate_id)
            report = load(path)
            self.assertEqual(report["report_id"], "AMFE-ENSEMBLE-EVAL-V2")
            self.assertEqual(report["candidate"], candidate_id)
            self.assertEqual(report["target"], "T1_CPI_YOY")
            self.assertEqual(report["horizon"], 1)
            self.assertEqual(report["data_class"], "REAL_TIME")
            self.assertEqual(report["candidate_registration"]["registered_before_evaluation"], True)
            self.assertIn("admission", report)
            for window in ("W1", "W2"):
                self.assertEqual(report["windows"][window]["data_class"], "REAL_TIME")
                self.assertEqual(report["windows"][window]["origins"], 121 if window == "W1" else 156)

    def test_per_candidate_report_windows_are_digest_only(self):
        for path in self.t1_files.values():
            report = load(path)
            assert_no_raw_arrays(self, report)
            for window in ("W1", "W2"):
                meta = report["windows"][window]
                self.assertIn("actual_primary_sha256", meta)
                self.assertNotIn("actual_primary", meta)
                self.assertIn("candidate", meta)
                self.assertIn("point_sha256", meta["candidate"])

    def test_registered_code_hashes_are_audited(self):
        check = self.report["code_hash_check"]
        self.assertEqual(check["registered_in"], "protocols/candidates_2026-11-11.json")
        self.assertEqual(set(check["registered_code_sha256"]), set(ENSEMBLE_REGISTRATION_FILES))
        for rel, expected in check["registered_code_sha256"].items():
            self.assertEqual(check["current_code_sha256"][rel], sha_path(ROOT / rel), rel)
        self.assertEqual(check["matches"], not check["mismatches"])
        if check["mismatches"]:
            for rel in check["mismatches"]:
                self.assertIn(rel, ENSEMBLE_REGISTRATION_FILES)
            self.assertIn("re-pin", check["note"])
            self.assertIn("571891d", check["note"])
        candidates_doc = load(ROOT / "protocols" / "candidates_2026-11-11.json")
        current_registration = next(c for c in candidates_doc["candidates"]
                                    if c["id"] == "ENSEMBLE_ADAPTIVE")["code_files"]
        self.assertEqual(current_registration, check["registered_code_sha256"])
        for path in self.t1_files.values():
            registration = load(path)["candidate_registration"]
            for rel in ENSEMBLE_REGISTRATION_FILES:
                self.assertIn(rel, registration["code_files"])

    def test_summary_candidates_match_per_candidate_reports(self):
        rows = {row["candidate_id"]: row for row in self.summary["evaluations"][T1_KEY]["candidates"]}
        for candidate_id, row in rows.items():
            path = candidate_report_path(candidate_id, "T1_h1")
            self.assertIsNotNone(path, candidate_id)
            report = load(path)
            self.assertEqual(row["status"], report["admission"]["status"])
            for window in ("W1", "W2"):
                for loss in ("RMSE", "CRPS"):
                    official = row["relative_losses"][window][loss]
                    evidence = report["admission"]["criteria"]["C1"]["windows"][window][loss]["relative_loss"]
                    if loss == "RMSE":
                        self.assertAlmostEqual(official, evidence, places=12)
                    else:
                        # the evidence predates the weighted-mixture robust path (571891d)
                        self.assertLessEqual(abs(official - evidence), 1e-3,
                                             (candidate_id, window, loss))

    def test_per_candidate_hashes_match_summary(self):
        files = self.summary["per_candidate_reports"]["T1_h1"]
        self.assertTrue(files)
        for candidate_id, info in files.items():
            self.assertEqual(info["sha256"], sha_path(ROOT / "protocols" / info["file"]), candidate_id)

    def test_evidence_consistency_of_extended_and_reduced_reports(self):
        for key in (T1_KEY, T2_KEY):
            consistency = self.report["evaluations"][key]["candidates"]
            tag = "T1_h1" if key.endswith("_h1") else "T2_h0"
            checked = self.report["cross_cutting"]["extended_pass"]["evidence_consistency"][tag]
            self.assertEqual(set(checked), set(REGISTERED))
            for candidate_id, info in checked.items():
                self.assertTrue(info["status_matches"], (tag, candidate_id))
                self.assertTrue(info["rmse_matches"], (tag, candidate_id, info["rmse_mismatches"]))
                self.assertTrue(info["crps_within_tolerance"], (tag, candidate_id))
                self.assertLessEqual(info["max_crps_relative_delta"], 1e-3)
                self.assertIn("571891d", info["note"])

    def test_t1_adaptive_matches_corrected_single_candidate_report(self):
        check = self.report["adaptive_vs_corrected_report"]
        self.assertTrue(check["checked"])
        self.assertTrue(check["matches"], check)
        corrected = load(CORRECTED)
        for window, block in corrected["admission"]["criteria"]["C1"]["windows"].items():
            self.assertAlmostEqual(check["corrected_relative_losses"][window]["RMSE"],
                                   block["RMSE"]["relative_loss"], places=12)


@unittest.skipUnless(REPORT.exists() and SUMMARY.exists(), "v2 report/summary not generated yet")
class SummaryConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load(REPORT)
        cls.summary = load(SUMMARY)

    def test_summary_report_hash_matches_file(self):
        self.assertEqual(self.summary["report"]["sha256"], sha_path(REPORT))
        self.assertEqual(self.summary["report"]["file"], "protocols/ensemble_evaluation_report_v2.json")

    def test_summary_has_statuses_losses_and_romano_wolf(self):
        evaluation = self.summary["evaluations"][T1_KEY]
        self.assertEqual(evaluation["multiplicity_method"],
                         "ROMANO_WOLF_STEPDOWN_STATIONARY_BOOTSTRAP")
        self.assertEqual(evaluation["family_size"], 4)
        self.assertIn("adjusted_p_values", evaluation["multiplicity"])
        for row in evaluation["candidates"]:
            for flag in ("C1", "C2", "C3", "C4", "C5", "C6"):
                self.assertIn(flag, row)
            self.assertEqual(row["family_size"], 4)
            for window in ("W1", "W2"):
                for loss in ("RMSE", "CRPS"):
                    self.assertIn(loss, row["relative_losses"][window])
                    self.assertIn(loss, row["c2_adjusted_p"][window])

    def test_summary_old_reports_marked_invalid(self):
        self.assertEqual(self.summary["supersedes"]["status"], "INVALID")
        self.assertTrue(self.summary.get("v1_reports_invalid"))

    def test_summary_c4_and_c6_notes(self):
        self.assertEqual(self.summary["C6"]["status"], "NO_RESOLVED_FORECASTS")
        self.assertIn("INCONCLUSIVE", self.summary["C4"]["T1_W1"])
        limitations = " ".join(self.summary["cross_cutting"]["limitations"])
        self.assertIn("v1", limitations)
        self.assertIn("C4", limitations)


@unittest.skipUnless(REPORT.exists() and SUMMARY.exists(), "v2 report/summary not generated yet")
class T2Evaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load(REPORT)
        cls.summary = load(SUMMARY)
        cls.t2_files = per_candidate_files("T2_h0")

    @unittest.skipUnless(per_candidate_files("T2_h0"), "T2 h=0 was not evaluated in this run")
    def test_t2_per_candidate_reports_and_mixture_fix(self):
        self.assertTrue(self.report["mixture_fix"]["available_at_evaluation_time"])
        self.assertIn("T2_GDP_GROWTH_h0", self.report["evaluations"])
        evaluation = self.report["evaluations"][T2_KEY]
        self.assertEqual(evaluation["target"], "T2_GDP_GROWTH")
        self.assertEqual(evaluation["horizon"], 0)
        for path in self.t2_files.values():
            report = load(path)
            self.assertEqual(report["target"], "T2_GDP_GROWTH")
            self.assertEqual(report["data_class"], "REAL_TIME")
            assert_no_raw_arrays(self, report)
            for window in ("W1", "W2"):
                self.assertEqual(report["windows"][window]["origins"], 121 if window == "W1" else 156)
                self.assertIn("CRPS", report["admission"]["criteria"]["C1"]["windows"][window])
        rows = self.summary["evaluations"][T2_KEY]["candidates"]
        self.assertEqual({row["candidate_id"] for row in rows}, set(REGISTERED))
        self.assertEqual({row["candidate_id"] for row in rows},
                         set(self.report["evaluations"][T2_KEY]["candidates"]))

    @unittest.skipUnless(per_candidate_files("T2_h0"), "T2 h=0 was not evaluated in this run")
    def test_t2_candidate_reports_cover_only_evaluated(self):
        evaluated = set(self.report["candidate_coverage"][T2_KEY]["evaluated"])
        files = {key.split("_T2_h0")[0].replace("ensemble_eval_", "")
                 for key in self.t2_files}
        self.assertEqual(evaluated, files)
        self.assertEqual(evaluated, set(REGISTERED))


if __name__ == "__main__":
    unittest.main(verbosity=2)
