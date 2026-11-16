"""Tests for the PIT-style oil calibration and OOS check (``amfe.markets.calibration``, docs/158).

Offline and deterministic: train episodes are declared inputs, the grid is pre-registered, the fit
is a mechanical RMSE search, and the OOS episodes are scored with the frozen parameters.  Reference
numbers below are the run of 2026-11-16 recorded in docs/158; re-baseline them together with the
docs if the oil parameters, engine or grid change.

Leakage is tested structurally: ``fit`` must refuse overlap with OOS ids, and the fitted parameters
must not depend on the OOS actuals.

Run from the repository root: ``python tests/test_markets_calibration.py``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets import calibration as cal  # noqa: E402
from amfe.markets.backtest import EPISODES  # noqa: E402

REFERENCE_PROTOCOL_SHA = "660eaf2cdd8e22acfe5020b4c11568483a617458e66253cded4bcb3efae89645"
REFERENCE_FIT = {"elasticity_short": -0.25, "elasticity_long": -0.4, "risk_premium": 15.0}
REFERENCE_TRAIN = {"mae_usd": 8.688462, "rmse_usd": 10.602754}
REFERENCE_OOS = {"mae_usd": 12.044954, "rmse_usd": 13.753851,
                 "skill_score_vs_no_shock": 0.426431, "direction_hits": 3, "order_hits": 2}
REFERENCE_FIXED = {"mae_usd": 13.044954, "rmse_usd": 14.247764,
                   "skill_score_vs_no_shock": 0.378812, "direction_hits": 3, "order_hits": 2}
REFERENCE_OOS_DELTAS = {"1990_gulf": 19.052785, "2019_abqaiq": 29.918091,
                        "2022_russia": 19.730443}


class ProtocolTests(unittest.TestCase):
    def test_protocol_is_deterministic_and_matches_the_pre_registration(self):
        first = cal.build_protocol()
        second = cal.build_protocol()
        self.assertEqual(first, second)
        self.assertEqual(cal.protocol_sha256(first), REFERENCE_PROTOCOL_SHA)
        self.assertEqual(cal.protocol_sha256(second), REFERENCE_PROTOCOL_SHA)

    def test_protocol_declares_grid_criteria_train_and_oos(self):
        protocol = cal.build_protocol()
        self.assertEqual(protocol["schema"], cal.PROTOCOL_VERSION)
        self.assertEqual(protocol["train_episodes"], list(cal.TRAIN_IDS))
        self.assertEqual(protocol["oos_episodes"], list(cal.OOS_IDS))
        self.assertTrue(protocol["train_oos_disjoint"])
        self.assertEqual(protocol["grid"]["size"], 105)
        self.assertEqual(len(protocol["grid"]["elasticity_short_magnitude"]), 7)
        self.assertEqual(len(protocol["grid"]["elasticity_long_multiplier"]), 3)
        self.assertEqual(len(protocol["grid"]["risk_premium_usd_per_bbl"]), 5)
        self.assertIn("root-mean-square", protocol["selection_rule"])
        self.assertEqual(set(protocol["success_criteria"]),
                         {"OOS_PASS", "OOS_PARTIAL", "OOS_FAIL"})
        self.assertEqual(protocol["status"], "RESEARCH_ONLY")

    def test_train_and_oos_are_disjoint(self):
        self.assertEqual(set(cal.TRAIN_IDS) & set(cal.OOS_IDS), set())
        with self.assertRaises(cal.CalibrationError):
            cal.fit(EPISODES)  # the backtest episodes are exactly the OOS set here

    def test_grid_is_complete_and_valid(self):
        points = cal.grid()
        self.assertEqual(len(points), 105)
        self.assertEqual(len({(item.elasticity_short, item.elasticity_long, item.risk_premium)
                              for item in points}), 105)
        for item in points:
            self.assertLess(item.elasticity_short, 0.0)
            self.assertLess(item.elasticity_long, 0.0)
            self.assertLessEqual(abs(item.elasticity_short), abs(item.elasticity_long))
            self.assertGreaterEqual(item.risk_premium, 0.0)


class FitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = cal.calibrate()

    def test_fit_selects_the_best_grid_point_deterministically(self):
        fitted = self.report.fit_result
        self.assertEqual(fitted.candidates, 105)
        self.assertEqual(fitted.params.as_dict(), REFERENCE_FIT)
        again = cal.fit(cal.TRAIN_EPISODES)
        self.assertEqual(again.params, fitted.params)
        self.assertEqual(again.train, fitted.train)

    def test_fit_minimizes_train_rmse(self):
        scores = [cal.train_score(point, cal.TRAIN_EPISODES) for point in cal.grid()]
        best_rmse = min(score["rmse_usd"] for score in scores)
        self.assertAlmostEqual(self.report.fit_result.train["rmse_usd"], best_rmse, places=6)
        # no other grid point has both a lower RMSE and a different tie-break
        for score in scores:
            if score["rmse_usd"] < best_rmse:
                self.fail("fit did not pick the minimum train RMSE")

    def test_reference_train_metrics(self):
        train = self.report.fit_result.train
        self.assertAlmostEqual(train["mae_usd"], REFERENCE_TRAIN["mae_usd"], places=6)
        self.assertAlmostEqual(train["rmse_usd"], REFERENCE_TRAIN["rmse_usd"], places=6)
        self.assertEqual(train["direction_hits"], 3)
        self.assertEqual(train["order_hits"], 2)
        self.assertEqual([row["episode_id"] for row in train["episodes"]], list(cal.TRAIN_IDS))

    def test_fit_does_not_depend_on_oos(self):
        # the OOS episodes are scored from the same frozen parameters; changing their declared
        # actuals cannot move the fit (fit only ever sees train episodes)
        mutated = []
        for episode in EPISODES:
            data = dict(episode.__dict__)
            data["actual_delta_usd"] = episode.actual_delta_usd * 3.0
            mutated.append(cal.Episode(**data))
        baseline = cal.fit(cal.TRAIN_EPISODES)
        unchanged = cal.fit(cal.TRAIN_EPISODES)
        self.assertEqual(baseline.params, unchanged.params)
        self.assertEqual(baseline.train, unchanged.train)
        # and the OOS scores are a pure function of the parameters (no episode-level tuning)
        first = cal.oos_calibrated(baseline.params)
        second = cal.oos_calibrated(baseline.params)
        self.assertEqual(first, second)
        self.assertEqual(len(mutated), 3)

    def test_fit_requires_train_episodes(self):
        with self.assertRaises(cal.CalibrationError):
            cal.fit([])


class ParamsTests(unittest.TestCase):
    def test_invalid_params_are_refused(self):
        with self.assertRaises(cal.CalibrationError):
            cal.CalibrationParams(elasticity_short=0.1, elasticity_long=-0.5, risk_premium=0.0)
        with self.assertRaises(cal.CalibrationError):
            cal.CalibrationParams(elasticity_short=-0.6, elasticity_long=-0.5, risk_premium=0.0)
        with self.assertRaises(cal.CalibrationError):
            cal.CalibrationParams(elasticity_short=-0.2, elasticity_long=-0.5, risk_premium=-1.0)


class OOSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = cal.calibrate()

    def test_reference_oos_numbers_with_calibrated_params(self):
        calibrated = self.report.oos_calibrated
        for key, value in REFERENCE_OOS.items():
            self.assertAlmostEqual(calibrated[key], value, places=6, msg=key)
        self.assertEqual(calibrated["n"], 3)
        deltas = {row["episode_id"]: row["model_delta_usd"] for row in calibrated["episodes"]}
        self.assertEqual(deltas, REFERENCE_OOS_DELTAS)

    def test_fixed_reference_calibration_is_reproduced(self):
        fixed = self.report.oos_fixed
        for key, value in REFERENCE_FIXED.items():
            self.assertAlmostEqual(fixed[key], value, places=6, msg=key)
        self.assertEqual([row["episode_id"] for row in fixed["episodes"]],
                         ["1990_gulf", "2019_abqaiq", "2022_russia"])

    def test_verdict_and_where_worse_follow_the_preregistration(self):
        report = self.report
        self.assertEqual(report.verdict, "OOS_PASS")
        self.assertEqual(report.where_worse, ("2019_abqaiq",))
        comparison = report.as_dict()["comparison"]
        self.assertAlmostEqual(comparison["mae_calibrated_usd"], 12.044954, places=6)
        self.assertAlmostEqual(comparison["mae_fixed_usd"], 13.044954, places=6)
        self.assertLess(report.oos_calibrated["mae_usd"], report.oos_fixed["mae_usd"])
        # the criteria are applied mechanically: all three conditions hold
        self.assertEqual(report.oos_calibrated["direction_hits"], 3)
        self.assertGreaterEqual(report.oos_calibrated["order_hits"], 2)
        self.assertEqual(cal.verdict(report.oos_calibrated, report.oos_fixed), "OOS_PASS")

    def test_verdict_rules_are_fail_closed(self):
        good = {"direction_hits": 3, "order_hits": 2, "mae_usd": 5.0, "n": 3}
        bad = {"direction_hits": 2, "order_hits": 1, "mae_usd": 40.0, "n": 3}
        self.assertEqual(cal.verdict(good, {"mae_usd": 10.0}), "OOS_PASS")
        self.assertEqual(cal.verdict(good, {"mae_usd": 1.0}), "OOS_PARTIAL")
        self.assertEqual(cal.verdict(bad, {"mae_usd": 10.0}), "OOS_FAIL")

    def test_oos_metrics_formulas(self):
        calibrated = self.report.oos_calibrated
        errors = [row["abs_error_usd"] for row in calibrated["episodes"]]
        self.assertAlmostEqual(calibrated["mae_usd"],
                               round(sum(errors) / len(errors), 6), places=6)
        squared = [row["error_usd"] ** 2 for row in calibrated["episodes"]]
        self.assertAlmostEqual(calibrated["rmse_usd"],
                               round((sum(squared) / len(squared)) ** 0.5, 6), places=6)
        self.assertAlmostEqual(calibrated["skill_score_vs_no_shock"],
                               round(1.0 - calibrated["mae_usd"]
                                     / calibrated["mae_baseline_usd"], 6), places=6)
        for row in calibrated["episodes"]:
            self.assertEqual(row["order_ok"], 0.5 <= row["ratio"] <= 2.0)

    def test_report_is_deterministic_and_aggregate_only(self):
        again = cal.calibrate()
        self.assertEqual(self.report.digest(), again.digest())
        document = self.report.as_dict()
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertTrue(document["research_only"])
        self.assertEqual(document["protocol_sha256"], REFERENCE_PROTOCOL_SHA)
        self.assertEqual(len(document["digest"]), 64)
        text = json.dumps(document, ensure_ascii=False)
        for forbidden in ('"matrix"', '"Z"', "timestamp", "generated_at"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("values", document["fit"])

    def test_markdown_report(self):
        markdown = cal.report_markdown(self.report)
        self.assertIn("PIT-калибровка и OOS", markdown)
        self.assertIn("OOS_PASS", markdown)
        self.assertIn(REFERENCE_PROTOCOL_SHA, markdown)
        self.assertIn("2019_abqaiq", markdown)
        self.assertIn(self.report.digest(), markdown)


class CLITests(unittest.TestCase):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-X", "utf8",
                               str(ROOT / "tools" / "oil_calibration.py"), *args],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=180)

    def test_protocol_only_mode(self):
        result = self.run_tool("--protocol-only", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["protocol_sha256"], REFERENCE_PROTOCOL_SHA)
        self.assertEqual(document["protocol"]["grid"]["size"], 105)

    def test_json_mode_and_output_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_tool("--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            report_path = Path(tmp) / "report.json"
            protocol_path = Path(tmp) / "protocol.json"
            sha_path = Path(tmp) / "protocol.sha256.txt"
            for path in (report_path, protocol_path, sha_path, Path(tmp) / "report.md"):
                self.assertTrue(path.is_file(), path)
            document = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(document["verdict"], "OOS_PASS")
            self.assertEqual(sha_path.read_text(encoding="utf-8").strip(),
                             REFERENCE_PROTOCOL_SHA)
            self.assertEqual(document["protocol_sha256"], REFERENCE_PROTOCOL_SHA)

    def test_json_report_mode(self):
        result = self.run_tool("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["verdict"], "OOS_PASS")
        self.assertEqual(len(document["digest"]), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
