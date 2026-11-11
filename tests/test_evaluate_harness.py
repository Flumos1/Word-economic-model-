"""Admission harness C1-C6 on synthetic data: losses, thresholds, DM/GW, C4-C6 and controls.

The harness must be independent of the forecast models: it imports only numpy and reads the frozen
protocol files, so this suite exercises it before src/amfe/forecast exists.
"""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import evaluate as ev  # noqa: E402

N = 120
H = 3
QUANTILE_SHAPE = np.linspace(-1.5, 1.5, len(ev.QUANTILES))


def window_data(actual, cand_point, comp_point, cand_q, comp_q, *, secondary=None,
                recession=None):
    data = {
        "actual_primary": actual,
        "actual_secondary": actual if secondary is None else secondary,
        "candidate": {"point": cand_point, "quantiles": cand_q},
        "comparator": {"point": comp_point, "quantiles": comp_q},
    }
    if recession is not None:
        data["recession"] = recession
    return data


def synthetic_pair(seed=20261111, n=N):
    rng = np.random.default_rng(seed)
    actual = rng.normal(2.0, 1.0, size=n)
    shock = rng.normal(0.0, 1.0, size=n)
    comp_point = actual + shock
    good_point = actual + 0.7 * shock
    bad_point = actual + 2.5 * shock
    comp_q = comp_point[:, None] + QUANTILE_SHAPE[None, :]
    good_q = good_point[:, None] + QUANTILE_SHAPE[None, :]
    bad_q = bad_point[:, None] + QUANTILE_SHAPE[None, :]
    secondary = actual + rng.normal(0.0, 0.1, size=n)
    recession = np.zeros(n, dtype=bool)
    recession[::5] = True
    return {"actual": actual, "comp_point": comp_point, "good_point": good_point,
            "bad_point": bad_point, "comp_q": comp_q, "good_q": good_q, "bad_q": bad_q,
            "secondary": secondary, "recession": recession}


class Losses(unittest.TestCase):
    def test_rmse_brier_and_log_score_hand_values(self):
        self.assertEqual(ev.aggregate_loss("RMSE", np.array([1.0, 4.0, 9.0])), float(np.sqrt(14 / 3)))
        p = np.array([0.2, 0.8])
        y = np.array([0.0, 1.0])
        self.assertAlmostEqual(ev.aggregate_loss("Brier", ev.brier_squared_error(p, y)), 0.04)
        expected_log = -np.log(0.8)
        self.assertAlmostEqual(ev.aggregate_loss("log_score", ev.log_score_observation(p, y)),
                               expected_log, places=12)
        clipped = ev.log_score_observation(np.array([0.0]), np.array([0.0]))
        self.assertAlmostEqual(float(clipped[0]), -np.log(0.99), places=12)
        self.assertTrue(np.all(np.isfinite(ev.log_score_observation(
            np.array([0.0, 1.0]), np.array([0.0, 1.0])))))

    def test_crps_is_twice_the_mean_pinball_over_the_grid(self):
        actual = np.array([0.5, -0.3])
        quantiles = actual[:, None] + QUANTILE_SHAPE[None, :]
        matrix = ev.pinball_matrix(quantiles, actual)
        self.assertEqual(matrix.shape, (2, 19))
        self.assertTrue(np.allclose(ev.crps(quantiles, actual), 2.0 * matrix.mean(axis=1)))
        # a forecast whose quantiles all equal the actual has zero CRPS
        degenerate = np.tile(actual[:, None], (1, 19))
        self.assertTrue(np.allclose(ev.crps(degenerate, actual), 0.0))
        self.assertEqual(ev.QUANTILE_LEVELS, [round(0.05 * i, 2) for i in range(1, 20)])

    def test_target_loss_map(self):
        self.assertEqual(ev.loss_names("T1_CPI_YOY"), ["RMSE", "CRPS"])
        self.assertEqual(ev.loss_names("T3_RECESSION"), ["Brier", "log_score"])
        with self.assertRaises(ev.EvaluationError):
            ev.loss_names("T9")


class Thresholds(unittest.TestCase):
    def test_table_matches_the_frozen_rule(self):
        table = ev.load_thresholds()
        self.assertEqual(len(table), 44)
        for threshold in table.values():
            self.assertLessEqual(threshold, 0.97)
            self.assertGreater(threshold, 0.0)
        self.assertEqual(ev.threshold_for(table, "T1_CPI_YOY", 3, "RMSE", "W1"), 0.97)
        self.assertAlmostEqual(ev.threshold_for(table, "T1_CPI_YOY", 12, "CRPS", "W1"), 0.9514)
        self.assertAlmostEqual(ev.threshold_for(table, "T1_CPI_YOY", 6, "RMSE", "W2"), 0.9678)
        with self.assertRaises(ev.EvaluationError):
            ev.threshold_for(table, "T1_CPI_YOY", 9, "RMSE", "W1")

    def test_c1_relative_loss_and_threshold(self):
        table = ev.load_thresholds()
        info = ev.c1_window(0.9, 1.0, ev.threshold_for(table, "T1_CPI_YOY", 6, "RMSE", "W2"))
        self.assertAlmostEqual(info["relative_loss"], 0.9)
        self.assertTrue(info["pass"])
        info = ev.c1_window(1.05, 1.0, ev.threshold_for(table, "T1_CPI_YOY", 6, "RMSE", "W2"))
        self.assertFalse(info["pass"])


class Tests(unittest.TestCase):
    def setUp(self):
        self.data = synthetic_pair()

    def test_hln_correction(self):
        self.assertEqual(ev.hln_factor(100, 1), 1.0)
        self.assertLess(ev.hln_factor(100, 3), 1.0)
        self.assertEqual(ev.newey_west_variance(np.zeros(20), 3), 0.0)

    def test_dm_one_sided_and_degenerate(self):
        rng = np.random.default_rng(3)
        better = rng.normal(-0.3, 1.0, size=200)
        result = ev.dm_test(better, H, "less")
        self.assertLess(result["p_value"], 0.02)
        self.assertEqual(result["hac_lags"], H - 1)
        worse = ev.dm_test(-better, H, "greater")
        self.assertLess(worse["p_value"], 0.02)
        identical = ev.dm_test(np.zeros(50), 1, "less")
        self.assertEqual(identical["p_value"], 0.5)
        self.assertEqual(identical["statistic"], 0.0)

    def test_gw_conditional_instrument(self):
        rng = np.random.default_rng(4)
        d = rng.normal(-0.2, 1.0, size=240)
        instrument = (rng.random(240) < 0.3).astype(float)
        result = ev.gw_conditional_test(d, instrument, H)
        self.assertEqual(result["kind"], "instruments")
        self.assertEqual(result["df"], 1)
        self.assertTrue(0.0 <= result["p_value"] <= 1.0)
        self.assertIn("mean_differential_instrument_1", result)
        constant_only = ev.gw_conditional_test(d, np.ones(240), H)
        self.assertEqual(constant_only["kind"], "constant_only")

    def test_romano_wolf_simplified(self):
        result = ev.romano_wolf_simplified({"a": 0.001, "b": 0.02, "c": 0.5})
        self.assertEqual(result["adjusted_p_values"]["c"], 0.5)
        self.assertLessEqual(result["adjusted_p_values"]["a"], result["adjusted_p_values"]["b"])
        self.assertGreaterEqual(result["adjusted_p_values"]["a"], 0.003)
        self.assertIn("HOLM", result["method"])

    def test_identical_methods_ratio_is_one(self):
        d = self.data
        block = ev.evaluate_window("W1", "T1_CPI_YOY", H, d["actual"],
                                   candidate={"point": d["comp_point"], "quantiles": d["comp_q"]},
                                   comparator={"point": d["comp_point"], "quantiles": d["comp_q"]})
        for loss in ("RMSE", "CRPS"):
            self.assertAlmostEqual(block["losses"][loss]["c1"]["relative_loss"], 1.0, places=12)
            self.assertFalse(block["losses"][loss]["c1"]["pass"])
            self.assertEqual(block["losses"][loss]["c2"]["mean_differential"], 0.0)

    def test_bad_candidate_is_rejected(self):
        d = self.data
        windows = {
            "W1": window_data(d["actual"], d["bad_point"], d["comp_point"], d["bad_q"],
                              d["comp_q"], secondary=d["secondary"], recession=d["recession"]),
            "W2": window_data(d["actual"], d["bad_point"], d["comp_point"], d["bad_q"],
                              d["comp_q"], secondary=d["secondary"], recession=d["recession"]),
        }
        report = ev.evaluate_admission("T1_CPI_YOY", H, windows, data_class="REAL_TIME")
        self.assertEqual(report["status"], "CANDIDATE")
        self.assertFalse(report["criteria"]["C1"]["pass"])
        self.assertFalse(report["criteria"]["C2"]["pass"])
        ratio = report["criteria"]["C1"]["windows"]["W1"]["RMSE"]["relative_loss"]
        self.assertGreater(ratio, 2.0)

    def test_good_candidate_passes_historical_gate(self):
        d = self.data
        windows = {
            "W1": window_data(d["actual"], d["good_point"], d["comp_point"], d["good_q"],
                              d["comp_q"], secondary=d["secondary"], recession=d["recession"]),
            "W2": window_data(d["actual"], d["good_point"], d["comp_point"], d["good_q"],
                              d["comp_q"], secondary=d["secondary"], recession=d["recession"]),
        }
        report = ev.evaluate_admission("T1_CPI_YOY", H, windows, data_class="REAL_TIME")
        self.assertTrue(report["criteria"]["C1"]["pass"])
        self.assertTrue(report["criteria"]["C2"]["pass"])
        self.assertTrue(report["criteria"]["C4"]["pass"], report["criteria"]["C4"])
        self.assertTrue(report["criteria"]["C5"]["pass"])
        self.assertEqual(report["criteria"]["C3"]["status"], "NOT_IMPLEMENTED")
        self.assertFalse(report["criteria"]["C3"]["gating"])
        self.assertEqual(report["status"], "SHADOW_TEST")
        self.assertTrue(report["stages"]["CANDIDATE_to_SHADOW_TEST"])

    def test_c4_inconclusive_with_few_recession_origins(self):
        d = self.data
        sparse = np.zeros(N, dtype=bool)
        sparse[:5] = True
        windows = {
            "W1": window_data(d["actual"], d["good_point"], d["comp_point"], d["good_q"],
                              d["comp_q"], secondary=d["secondary"], recession=sparse),
            "W2": window_data(d["actual"], d["good_point"], d["comp_point"], d["good_q"],
                              d["comp_q"], secondary=d["secondary"], recession=sparse),
        }
        report = ev.evaluate_admission("T1_CPI_YOY", H, windows, data_class="REAL_TIME")
        self.assertFalse(report["criteria"]["C4"]["pass"])
        self.assertEqual(report["criteria"]["C4"]["per_window"]["W1"]["RMSE"]["status"],
                         "INCONCLUSIVE")
        self.assertFalse(report["stages"]["CANDIDATE_to_SHADOW_TEST"])

    def test_c5_opposite_alternative(self):
        d = self.data
        rng = np.random.default_rng(11)
        # the candidate is better on the primary actuals and stays better when the secondary
        # actuals differ only slightly: C5 (not significantly worse) passes
        block = ev.evaluate_window("W1", "T1_CPI_YOY", H, d["actual"],
                                   actual_secondary=d["actual"] + rng.normal(0, 0.1, N),
                                   candidate={"point": d["good_point"], "quantiles": d["good_q"]},
                                   comparator={"point": d["comp_point"], "quantiles": d["comp_q"]})
        self.assertGreater(block["losses"]["RMSE"]["c5"]["p_value"], 0.10)
        # a clearly worse candidate on the secondary actuals must fail C5
        better = d["actual"] + 0.2 * (d["comp_point"] - d["actual"])
        block = ev.evaluate_window("W1", "T1_CPI_YOY", H, d["actual"],
                                   actual_secondary=d["actual"],
                                   candidate={"point": d["good_point"], "quantiles": d["good_q"]},
                                   comparator={"point": better,
                                               "quantiles": better[:, None] + QUANTILE_SHAPE[None, :]})
        self.assertLess(block["losses"]["RMSE"]["c5"]["p_value"], 0.10)

    def test_t3_event_losses_and_calibration(self):
        y = np.array([0.0, 1.0, 1.0, 0.0])
        p = np.array([0.2, 0.8, 0.6, 0.3])
        block = ev.evaluate_window("W1", "T3_RECESSION", 6, y,
                                   candidate={"probability": p},
                                   comparator={"probability": np.full(4, 0.5)})
        self.assertAlmostEqual(block["losses"]["Brier"]["c1"]["candidate_loss"],
                               float(np.mean((p - y) ** 2)))
        expected_log = float(np.mean(-(y * np.log(p) + (1 - y) * np.log(1 - p))))
        self.assertAlmostEqual(block["losses"]["log_score"]["c1"]["candidate_loss"], expected_log)
        calibrated = ev.calibration_bins(np.array([0.1] * 10 + [0.5] * 10 + [0.9] * 10),
                                         np.array([1] + [0] * 9 + [1] * 5 + [0] * 5
                                                  + [1] * 9 + [0] * 1), bins=5)
        self.assertTrue(calibrated["pass"], calibrated["bins"])
        bad = ev.calibration_bins(np.array([0.1] * 10), np.ones(10), bins=5)
        self.assertFalse(bad["pass"])
        self.assertLess(bad["n_bins"], 3)

    def test_c4_not_applicable_to_t3(self):
        y = (np.random.default_rng(5).random(40) < 0.3).astype(float)
        p = np.clip(y * 0.7 + 0.15, 0.01, 0.99)
        forward = ev.c6_forward_check("T3_RECESSION", y, candidate_probability=p,
                                      comparator_probability=np.clip(p + 0.1, 0.01, 0.99))
        windows = {"W1": {"actual_primary": y, "candidate": {"probability": p},
                          "comparator": {"probability": np.full(40, 0.3)}},
                   "W2": {"actual_primary": y, "candidate": {"probability": p},
                          "comparator": {"probability": np.full(40, 0.3)}}}
        report = ev.evaluate_admission("T3_RECESSION", 6, windows, forward=forward, data_class="REAL_TIME")
        self.assertFalse(report["criteria"]["C4"]["applies"])
        self.assertTrue(report["criteria"]["C4"]["pass"])


class ForwardRegistryC6(unittest.TestCase):
    @staticmethod
    def calibrated_forward(n_per_z=10):
        z = np.array([-1.6449, -1.2816, -1.0364, -0.8416, -0.6745, -0.5244, -0.3853,
                      -0.2533, -0.1257, 0.0, 0.1257, 0.2533, 0.3853, 0.5244, 0.6745,
                      0.8416, 1.0364, 1.2816, 1.6449])
        errors = np.repeat(z, n_per_z)
        center = np.full(errors.size, 2.0)
        actual = center + errors
        candidate_q = center[:, None] + z[None, :]
        candidate_point = center + 0.9 * errors
        comparator_point = center + 0.5 * errors
        comparator_q = candidate_q + 1.0
        return actual, candidate_point, candidate_q, comparator_point, comparator_q

    def test_not_enough_forecasts(self):
        y = np.zeros(5)
        q = np.tile(np.linspace(-1, 1, 19)[None, :], (5, 1))
        result = ev.c6_forward_check("T1_CPI_YOY", y, candidate_point=np.zeros(5),
                                     candidate_quantiles=q, comparator_point=np.ones(5),
                                     comparator_quantiles=q)
        self.assertFalse(result["count_met"])
        self.assertFalse(result["pass"])

    def test_coverage_and_cumulative_loss(self):
        actual, cand_point, cand_q, comp_point, comp_q = self.calibrated_forward()
        self.assertGreaterEqual(actual.size, ev.MIN_FORWARD)
        result = ev.c6_forward_check("T1_CPI_YOY", actual, candidate_point=cand_point,
                                     candidate_quantiles=cand_q, comparator_point=comp_point,
                                     comparator_quantiles=comp_q)
        self.assertTrue(result["count_met"])
        for loss in ("RMSE", "CRPS"):
            self.assertLessEqual(result["per_loss"][loss]["cumulative_relative_loss"], 1.0)
        self.assertEqual(set(result["coverage"]), {"nominal_0.50", "nominal_0.80", "nominal_0.90"})
        self.assertTrue(result["coverage_met"], result["coverage"])
        self.assertTrue(result["pass"], result)

    def test_t3_calibration_gate(self):
        y = np.array([1] + [0] * 9 + [1] * 5 + [0] * 5 + [1] * 9 + [0] * 1, dtype=float)
        p = np.array([0.1] * 10 + [0.5] * 10 + [0.9] * 10)
        result = ev.c6_forward_check("T3_RECESSION", y, candidate_probability=p,
                                     comparator_probability=np.full(30, 0.5))
        self.assertTrue(result["pass"], result)
        miscalibrated = ev.c6_forward_check("T3_RECESSION", y,
                                            candidate_probability=np.full(30, 0.9),
                                            comparator_probability=np.full(30, 0.5))
        self.assertFalse(miscalibrated["pass"])


class Independence(unittest.TestCase):
    def test_harness_does_not_import_the_forecast_package(self):
        source = (ROOT / "tools" / "evaluate.py").read_text(encoding="utf-8")
        imports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith(("import ", "from "))]
        for line in imports:
            self.assertNotIn("amfe", line)
            self.assertNotIn("forecast", line)
        self.assertTrue(any("numpy" in line for line in imports))

    def test_complexity_report_available_on_synthetic_arrays(self):
        rng = np.random.default_rng(7)
        y = rng.normal(size=60)
        q = y[:, None] + np.linspace(-1, 1, 19)[None, :]
        candidates = {"c1": {"actual_primary": y, "candidate": {"point": y + 0.1, "quantiles": q},
                             "comparator": {"point": y + 0.2, "quantiles": q}},
                      "c2": {"actual_primary": y, "candidate": {"point": y + 0.5, "quantiles": q},
                             "comparator": {"point": y + 0.2, "quantiles": q}}}
        windows = {name: {"W1": data, "W2": data} for name, data in candidates.items()}
        reports = {name: ev.evaluate_admission("T1_CPI_YOY", 1, windows[name], data_class="REAL_TIME")
                   for name in candidates}
        adjusted = ev.apply_romano_wolf(reports)
        self.assertEqual(len(adjusted["adjusted_p_values"]), 2)
        for report in reports.values():
            self.assertIn("romano_wolf_adjusted_p", report["criteria"]["C2"])


class DataClassGuard(unittest.TestCase):
    """admission is only allowed for REAL_TIME / REAL_TIME_TAPE (protocol v4)."""

    def _windows(self):
        rng = np.random.default_rng(11)
        actual = rng.normal(2.0, 1.0, size=N)
        shock = rng.normal(0.0, 1.0, size=N)
        comp = actual + shock
        cand = actual + 0.5 * shock
        q = cand[:, None] + QUANTILE_SHAPE[None, :]
        cq = comp[:, None] + QUANTILE_SHAPE[None, :]
        data = window_data(actual, cand, comp, q, cq, recession=np.zeros(N, dtype=bool))
        return {"W1": data, "W2": data}

    def test_real_time_allows_admission(self):
        report = ev.evaluate_admission("T1_CPI_YOY", H, self._windows(), data_class="REAL_TIME")
        self.assertTrue(report["admission_allowed"])
        self.assertIn(report["status"], {"SHADOW_TEST", "LIMITED_DEPLOYMENT", "CANDIDATE"})

    def test_pseudo_and_unstated_are_blocked(self):
        for data_class in ("PSEUDO_REAL_TIME", "UNSTATED"):
            report = ev.evaluate_admission("T1_CPI_YOY", H, self._windows(), data_class=data_class)
            self.assertFalse(report["admission_allowed"], data_class)
            self.assertEqual(report["status"], "CANDIDATE", data_class)
            self.assertIn("admission NOT allowed", report["data_class_note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
