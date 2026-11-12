"""T3 admission report (REAL_TIME, protocol v4): schema, no raw values, statuses, determinism, PIT.

The tests read the six v2 reports and the summary produced by ``tools/run_t3_evaluation.py`` and
check them against the frozen protocol, the frozen comparator composition and the frozen NF-SIM
threshold table.  One test recomputes a two-origin window with the runner functions, so the
point-in-time comparator rule (mean of the two member probabilities) and the determinism of the
digests are verified against the live code, not only against the stored JSON.
"""
import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import evaluate  # noqa: E402
import run_t3_evaluation as runner  # noqa: E402
import windows  # noqa: E402

TARGET = "T3_RECESSION"
CANDIDATES = ("ENSEMBLE_ADAPTIVE", "ENSEMBLE_EQUAL")
HORIZONS = (3, 6, 12)
WINDOWS = ("W1", "W2")
LOSSES = ("Brier", "log_score")
SUMMARY = ROOT / "protocols" / "ensemble_t3_report.json"
PROTOCOL = json.loads((ROOT / "protocols" / "evaluation_protocol_v4.json").read_text(encoding="utf-8"))
THRESHOLDS = json.loads((ROOT / "protocols" / "nf_sim_thresholds.json").read_text(encoding="utf-8"))
COMPOSITION = json.loads((ROOT / "protocols" / "comparator_composition_2026-11-11.json")
                         .read_text(encoding="utf-8"))
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_ARRAY_KEYS = ("probability", "probabilities", "quantiles", "point", "actual",
                        "actual_primary", "actual_secondary", "recession", "values", "losses_by_obs")


def report_path(candidate: str, horizon: int) -> Path:
    return ROOT / "protocols" / f"ensemble_eval_{candidate}_T3_h{horizon}_v2.json"


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise unittest.SkipTest(f"report not generated yet: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def walk(node, path=""):
    """Yield (path, value) for every node of a JSON document."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def threshold_for(horizon: int, loss: str, window: str) -> float:
    for entry in THRESHOLDS["entries"]:
        if (entry["target"] == TARGET and int(entry["horizon"]) == int(horizon)
                and entry["loss"] == loss and entry["window"] == window):
            return float(entry["threshold"])
    raise AssertionError(f"no threshold {TARGET}/{horizon}/{loss}/{window}")


class ReportsExist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {(candidate, horizon): load_json(report_path(candidate, horizon))
                       for candidate in CANDIDATES for horizon in HORIZONS}
        cls.summary = load_json(SUMMARY)

    def test_all_six_reports_and_the_summary_are_present(self):
        for candidate in CANDIDATES:
            for horizon in HORIZONS:
                self.assertIn((candidate, horizon), self.reports)
        self.assertEqual(self.summary["target"], TARGET)
        self.assertEqual(sorted(self.summary["candidates"]), sorted(CANDIDATES))
        self.assertEqual(sorted(self.summary["horizons"]), sorted(HORIZONS))
        self.assertEqual(len(self.summary["pairs"]), len(CANDIDATES) * len(HORIZONS))

    def test_report_identity_and_reporting_only(self):
        for (candidate, horizon), report in self.reports.items():
            self.assertEqual(report["report_id"], "AMFE-ENSEMBLE-EVAL-V2")
            self.assertEqual(report["candidate"], candidate)
            self.assertEqual(report["target"], TARGET)
            self.assertEqual(int(report["horizon"]), horizon)
            self.assertEqual(report["mode"], "auto")
            self.assertEqual(report["data_class"], "REAL_TIME")
            self.assertIn("no admission is assigned", report["reporting_only"])
            self.assertIn("not stored", report["licence_note"])
            self.assertEqual(report["admission"]["data_class"], "REAL_TIME")
            self.assertTrue(report["admission"]["admission_allowed"])

    def test_frozen_artefact_hashes_match_the_files_on_disk(self):
        frozen = self.summary["frozen_artefacts"]
        expected = {
            "protocols/evaluation_protocol_v4.json": runner.sha(runner.PROTOCOL),
            "protocols/r3_specification.json": runner.sha(runner.R3),
            "protocols/comparator_composition_2026-11-11.json": runner.sha(runner.COMPOSITION),
            "protocols/candidates_2026-11-11.json": runner.sha(runner.CANDIDATES_FILE),
            "protocols/nf_sim_thresholds.json": runner.sha(runner.THRESHOLDS),
            "protocols/nber_dates.json": runner.sha(runner.NBER),
        }
        self.assertEqual(frozen, expected)
        for (candidate, horizon), report in self.reports.items():
            self.assertEqual(report["protocol"]["sha256"], expected[
                "protocols/evaluation_protocol_v4.json"])
            self.assertEqual(report["composition"]["sha256"], expected[
                "protocols/comparator_composition_2026-11-11.json"])
            self.assertEqual(report["candidates"]["sha256"], expected[
                "protocols/candidates_2026-11-11.json"])
            self.assertEqual(report["r3_specification"]["sha256"], expected[
                "protocols/r3_specification.json"])
            self.assertEqual(report["nf_sim_thresholds"]["sha256"], expected[
                "protocols/nf_sim_thresholds.json"])
            self.assertEqual(report["nber_dates"]["sha256"], expected[
                "protocols/nber_dates.json"])

    def test_candidate_was_registered_before_evaluation(self):
        for report in self.reports.values():
            registration = report["candidate_registration"]
            self.assertTrue(registration["registered_before_evaluation"])
            self.assertIn(TARGET, registration["targets"])
            self.assertEqual(registration["id"], report["candidate"])
            self.assertRegex(registration["code_sha256"], HEX64)

    def test_comparator_is_the_frozen_probit_sahm_rule(self):
        for horizon in HORIZONS:
            by_loss = {loss: runner.members_for(COMPOSITION, TARGET, loss, horizon)
                       for loss in LOSSES}
            self.assertEqual(by_loss["Brier"], by_loss["log_score"])
            self.assertEqual(runner.comparator_members(horizon), ["PROBIT_SPREAD", "SAHM_RULE"])
            for candidate in CANDIDATES:
                report = self.reports[(candidate, horizon)]
                self.assertEqual(sorted(report["composition"]["members"]),
                                 ["PROBIT_SPREAD", "SAHM_RULE"])
                for window in WINDOWS:
                    self.assertEqual(sorted(report["windows"][window]["member_ids"]),
                                     ["PROBIT_SPREAD", "SAHM_RULE"])


class WindowSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {(candidate, horizon): load_json(report_path(candidate, horizon))
                       for candidate in CANDIDATES for horizon in HORIZONS}

    def test_window_block_has_metadata_digests_and_counts(self):
        required = {"window", "target", "horizon", "candidate", "protocol_origins", "origins",
                    "first_origin", "last_origin", "data_class", "vintage_dates", "member_ids",
                    "member_failures", "candidate_failures", "actual_missing",
                    "pseudo_origins_skipped", "member_verification",
                    "actual_primary_sha256", "actual_primary_n", "actual_secondary_sha256",
                    "actual_secondary_n", "actual_event_origins", "candidate", "comparator",
                    "licence_note"}
        for (candidate, horizon), report in self.reports.items():
            for window in WINDOWS:
                block = report["windows"][window]
                self.assertTrue(required <= set(block), (candidate, horizon, window,
                                                         required - set(block)))
                self.assertEqual(block["window"], window)
                self.assertEqual(block["target"], TARGET)
                self.assertEqual(int(block["horizon"]), horizon)
                self.assertGreater(block["origins"], 100)
                self.assertEqual(block["origins"], block["actual_primary_n"])
                self.assertEqual(block["origins"], block["actual_secondary_n"])
                self.assertEqual(block["origins"], block["candidate"]["probability_n"])
                self.assertEqual(block["origins"], block["comparator"]["probability_n"])
                self.assertEqual(block["data_class"], "REAL_TIME")
                self.assertEqual(block["pseudo_origins_skipped"], 0)
                for key in ("actual_primary_sha256", "actual_secondary_sha256"):
                    self.assertRegex(block[key], HEX64)
                for role in ("candidate", "comparator"):
                    self.assertRegex(block[role]["probability_sha256"], HEX64)
                self.assertGreaterEqual(block["actual_event_origins"], 0)
                self.assertLessEqual(block["actual_event_origins"], block["origins"])

    def test_secondary_actual_equals_primary_for_t3(self):
        for report in self.reports.values():
            for window in WINDOWS:
                block = report["windows"][window]
                self.assertEqual(block["actual_primary_sha256"], block["actual_secondary_sha256"])
                self.assertEqual(block["actual_primary_n"], block["actual_secondary_n"])

    def test_member_verification_passed(self):
        for report in self.reports.values():
            for window in WINDOWS:
                verification = report["windows"][window]["member_verification"]
                self.assertGreaterEqual(verification["origins_checked"], 1)
                self.assertLessEqual(verification["max_abs_diff"], verification["tolerance"])

    def test_no_raw_forecasts_actuals_or_probabilities_are_stored(self):
        for (candidate, horizon), report in self.reports.items():
            for path, value in walk(report):
                leaf = path.rsplit(".", 1)[-1]
                if leaf in FORBIDDEN_ARRAY_KEYS:
                    self.assertIsInstance(value, dict, f"raw array at {path}")
                if isinstance(value, list):
                    numeric = [item for item in value
                               if isinstance(item, (int, float)) and not isinstance(item, bool)]
                    self.assertLessEqual(len(numeric), 4,
                                         f"numeric array of length {len(numeric)} at {path}")
                    for item in value:
                        if isinstance(item, str) and len(item) > 40:
                            self.assertNotRegex(item, r"^[-\d., ]+$",
                                                f"numeric vector serialised at {path}")

    def test_no_float_probability_like_fields(self):
        for report in self.reports.values():
            for path, value in walk(report):
                leaf = path.split(".")[-1]
                if leaf in ("probability", "probabilities"):
                    self.fail(f"raw probability field at {path}")


class CriteriaConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {(candidate, horizon): load_json(report_path(candidate, horizon))
                       for candidate in CANDIDATES for horizon in HORIZONS}
        cls.summary = load_json(SUMMARY)

    def test_c1_relative_loss_threshold_and_gate(self):
        for (candidate, horizon), report in self.reports.items():
            criteria = report["admission"]["criteria"]
            for window in WINDOWS:
                for loss in LOSSES:
                    entry = criteria["C1"]["windows"][window][loss]
                    expected = threshold_for(horizon, loss, window)
                    self.assertAlmostEqual(entry["threshold"], expected, places=12)
                    self.assertAlmostEqual(
                        entry["relative_loss"],
                        entry["candidate_loss"] / entry["comparator_loss"], places=12)
                    self.assertEqual(entry["pass"],
                                     entry["relative_loss"] <= entry["threshold"])
            expected_pass = all(criteria["C1"]["windows"][w][loss]["pass"]
                                for w in WINDOWS for loss in LOSSES)
            self.assertEqual(criteria["C1"]["pass"], expected_pass)

    def test_c2_uses_the_romano_wolf_adjusted_p_value(self):
        for (candidate, horizon), report in self.reports.items():
            criteria = report["admission"]["criteria"]
            multiplicity = report["admission"]["multiplicity"]
            self.assertEqual(multiplicity["method"], evaluate.RW_METHOD)
            self.assertEqual(multiplicity["family_size"], len(CANDIDATES))
            self.assertEqual(multiplicity["seed"], evaluate.DEFAULT_RW_SEED)
            self.assertEqual(multiplicity["replications"], evaluate.DEFAULT_RW_REPLICATIONS)
            for window in WINDOWS:
                for loss in LOSSES:
                    entry = criteria["C2"]["windows"][window][loss]
                    self.assertGreaterEqual(entry["raw_p_value"], 0.0)
                    self.assertLessEqual(entry["raw_p_value"], 1.0)
                    self.assertGreaterEqual(entry["adjusted_p_value"],
                                            entry["raw_p_value"] - 1e-12)
                    self.assertEqual(entry["family_size"], len(CANDIDATES))
            expected_pass = all(criteria["C2"]["windows"][w][loss]["adjusted_p_value"]
                                < evaluate.ALPHA for w in WINDOWS for loss in LOSSES)
            self.assertEqual(criteria["C2"]["pass"], expected_pass)
            worst = max(criteria["C2"]["windows"][w][loss]["adjusted_p_value"]
                        for w in WINDOWS for loss in LOSSES)
            self.assertAlmostEqual(criteria["C2"]["romano_wolf_adjusted_p"], worst, places=12)

    def test_c4_does_not_apply_to_t3(self):
        for report in self.reports.values():
            c4 = report["admission"]["criteria"]["C4"]
            self.assertFalse(c4["applies"])
            self.assertTrue(c4["pass"])
            self.assertIn("c4_not_applicable", c4["reason"])

    def test_c6_has_no_resolved_forecasts(self):
        for report in self.reports.values():
            c6 = report["admission"]["criteria"]["C6"]
            self.assertEqual(c6["status"], "NO_RESOLVED_FORECASTS")
            self.assertFalse(c6["pass"])
            self.assertIn("NO_RESOLVED_FORECASTS", report["c6"])
        self.assertIn("NO_RESOLVED_FORECASTS", self.summary["c6"])
        for pair in self.summary["pairs"]:
            self.assertEqual(pair["C6"]["status"], "NO_RESOLVED_FORECASTS")
            self.assertFalse(pair["C6"]["pass"])

    def test_status_is_exactly_the_harness_regrade_rule(self):
        for (candidate, horizon), report in self.reports.items():
            admission = report["admission"]
            criteria = admission["criteria"]
            historical = bool(criteria["C1"]["pass"] and criteria["C2"]["pass"]
                              and criteria["C5"]["pass"] and criteria["C4"]["pass"])
            forward = bool(criteria["C6"]["pass"])
            expected = ("LIMITED_DEPLOYMENT" if historical and forward
                        else "SHADOW_TEST" if historical else "CANDIDATE")
            if expected != "CANDIDATE" and not admission["admission_allowed"]:
                expected = "CANDIDATE"
            self.assertEqual(admission["status"], expected, (candidate, horizon))
            self.assertEqual(admission["stages"]["CANDIDATE_to_SHADOW_TEST"], historical)
            self.assertEqual(admission["stages"]["SHADOW_TEST_to_LIMITED_DEPLOYMENT"],
                             historical and forward)
            self.assertFalse(admission["stages"]["LIMITED_DEPLOYMENT_to_ACTIVE"])

    def test_status_label_and_conditional_data_class_follow_the_regrade_rule(self):
        """Wave-5 harness fields: T3 is an unconditional REAL_TIME evaluation.

        ``status_label`` and ``conditional_data_class`` were added to ``evaluate_admission``
        (C4/W1 rework for T1/T2); for T3 they must stay degenerate: the flag is false and the
        label is the plain status, and reloading the stored admission block through
        ``evaluate._regrade_status`` must reproduce the status, the label and the stages.
        """
        for (candidate, horizon), report in self.reports.items():
            admission = report["admission"]
            self.assertFalse(admission["conditional_data_class"])
            self.assertEqual(admission["status_label"], admission["status"])
            reloaded = evaluate._regrade_status(json.loads(json.dumps(admission)))
            self.assertEqual(reloaded["status"], admission["status"], (candidate, horizon))
            self.assertEqual(reloaded["status_label"], admission["status_label"])
            self.assertEqual(reloaded["stages"], admission["stages"])

    def test_no_status_above_the_factual_criteria(self):
        for (candidate, horizon), report in self.reports.items():
            admission = report["admission"]
            status = admission["status"]
            self.assertIn(status, ("CANDIDATE", "SHADOW_TEST"))
            if status == "SHADOW_TEST":
                criteria = admission["criteria"]
                self.assertTrue(criteria["C1"]["pass"])
                self.assertTrue(criteria["C2"]["pass"])
                self.assertTrue(criteria["C5"]["pass"])
            if status == "CANDIDATE":
                self.assertFalse(admission["stages"]["CANDIDATE_to_SHADOW_TEST"])

    def test_summary_matches_the_reports(self):
        pairs = {(pair["candidate"], int(pair["horizon"])): pair
                 for pair in self.summary["pairs"]}
        self.assertEqual(sorted(pairs), sorted(self.reports))
        for key, pair in pairs.items():
            report = self.reports[key]
            admission = report["admission"]
            self.assertEqual(pair["status"], admission["status"])
            self.assertEqual(pair["data_class"], "REAL_TIME")
            self.assertEqual(pair["C1_pass"], admission["criteria"]["C1"]["pass"])
            self.assertEqual(pair["C2_pass"], admission["criteria"]["C2"]["pass"])
            self.assertEqual(pair["C5_pass"], admission["criteria"]["C5"]["pass"])
            self.assertFalse(pair["C4"]["applies"])
            for window in WINDOWS:
                for loss in LOSSES:
                    self.assertAlmostEqual(
                        pair["per_window"][window][loss]["relative_loss"],
                        admission["criteria"]["C1"]["windows"][window][loss]["relative_loss"],
                        places=12)
                    self.assertAlmostEqual(
                        pair["per_window"][window][loss]["c2_adjusted_p_value"],
                        admission["criteria"]["C2"]["windows"][window][loss]["adjusted_p_value"],
                        places=12)
            self.assertEqual(pair["report_file"],
                             f"protocols/ensemble_eval_{key[0]}_T3_h{key[1]}_v2.json")
        self.assertEqual(self.summary["c4"]["applies"], False)
        self.assertEqual(self.summary["composition"]["members"]["6"],
                         ["PROBIT_SPREAD", "SAHM_RULE"])


class PointInTimeAndDeterminism(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {(candidate, horizon): load_json(report_path(candidate, horizon))
                       for candidate in CANDIDATES for horizon in HORIZONS}

    def test_origins_follow_the_protocol_and_are_inside_the_windows(self):
        override = PROTOCOL["windows"]["target_overrides"][TARGET]["W1_start"]
        covid_start, covid_end = PROTOCOL["windows"]["origin_exclusion"]["covid_targets"].split(
            " to ")
        for horizon in HORIZONS:
            for window in WINDOWS:
                rows = windows.origins_for(PROTOCOL, TARGET, horizon, window)
                start = PROTOCOL["windows"][window]["start"]
                end = PROTOCOL["windows"][window]["end"]
                if window == "W1":
                    start = override
                report_block = self.reports[("ENSEMBLE_ADAPTIVE", horizon)]["windows"][window]
                self.assertEqual(report_block["origins"], len(rows))
                self.assertEqual(report_block["protocol_origins"], len(rows))
                self.assertEqual(report_block["first_origin"], rows[0]["origin"])
                self.assertEqual(report_block["last_origin"], rows[-1]["origin"])
                for row in rows:
                    origin = date.fromisoformat(row["origin"])
                    target = date.fromisoformat(row["target"])
                    self.assertGreater(target, origin, (horizon, window, row))
                    self.assertEqual(target, windows.add_months(origin, horizon))
                    self.assertGreaterEqual(origin.isoformat()[:7], start)
                    self.assertLessEqual(target.isoformat()[:7], end)
                    months = {windows.add_months(origin, step).isoformat()[:7]
                              for step in range(1, horizon + 1)}
                    self.assertFalse(any(covid_start <= month <= covid_end for month in months),
                                     (horizon, window, row))
                if window == "W1":
                    self.assertEqual(rows[0]["origin"][:7], "1996-12")

    def test_recorded_vintages_are_not_after_the_origins(self):
        for (candidate, horizon), report in self.reports.items():
            for window in WINDOWS:
                block = report["windows"][window]
                self.assertTrue(block["vintage_dates"])
                for stamp in block["vintage_dates"]:
                    self.assertLessEqual(stamp, block["last_origin"],
                                         (candidate, horizon, window, stamp))

    def test_comparator_equals_the_mean_of_the_member_probabilities(self):
        from amfe.forecast import forecast
        first = runner.build_window(TARGET, 3, "W1", "ENSEMBLE_EQUAL", max_origins=2,
                                    verify_members=2)
        second = runner.build_window(TARGET, 3, "W1", "ENSEMBLE_EQUAL", max_origins=2,
                                     verify_members=0)
        self.assertEqual(first["origins"], 2)
        self.assertEqual(first["comparator_probability"], second["comparator_probability"])
        self.assertEqual(first["candidate_probability"], second["candidate_probability"])
        origin = first["first_origin"]
        direct = [float(forecast(member, TARGET, origin, 3, mode="auto").to_dict()["probability"])
                  for member in first["member_ids"]]
        self.assertAlmostEqual(first["comparator_probability"][0], sum(direct) / len(direct),
                               places=12)
        self.assertLessEqual(first["member_verification"]["max_abs_diff"], 1e-12)

    def test_digests_are_deterministic(self):
        first = runner.build_window(TARGET, 3, "W1", "ENSEMBLE_EQUAL", max_origins=2,
                                    verify_members=0)
        second = runner.build_window(TARGET, 3, "W1", "ENSEMBLE_EQUAL", max_origins=2,
                                     verify_members=0)
        for key in ("actual_primary", "actual_secondary", "candidate_probability",
                    "comparator_probability"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["vintage_dates"], second["vintage_dates"])
        self.assertEqual(first["member_failures"], second["member_failures"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
