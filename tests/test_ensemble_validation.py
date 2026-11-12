"""Tests for the V2 ensemble walk-forward validation tool (``tools/ensemble_validation.py``).

The suite runs on synthetic data (injected ``forecast_fn``/``actual_fn``) so it is fast and
deterministic; the committed report, when present, is checked for structure and for the
"aggregates only" hygiene rule.  No real vintage values are read by the tests except the NBER
reference chronology (metadata) and the protocol windows (metadata).

Run from the repository root: ``python -m unittest tests.test_ensemble_validation -v``.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import ensemble_validation as validation  # noqa: E402
import windows as windows_module  # noqa: E402
from amfe.forecast.data import month_index  # noqa: E402

REPORT_PATH = ROOT / "protocols" / "ensemble_validation_report.json"
PROTOCOL = validation.load_protocol()
NBER = validation.load_nber()
RECESSION_MONTHS = validation.recession_month_set(NBER)

ENSEMBLE_EQUAL = validation.ENSEMBLE_EQUAL
ENSEMBLE_INVERSE_MSE = validation.ENSEMBLE_INVERSE_MSE
ENSEMBLE_BAYES = validation.ENSEMBLE_BAYES
ENSEMBLE_ADAPTIVE = validation.ENSEMBLE_ADAPTIVE
ENSEMBLE_IDS = validation.ENSEMBLE_IDS

#: deterministic per-ensemble error multipliers: lower is better
FACTORS = {
    ENSEMBLE_EQUAL: 1.0,
    ENSEMBLE_INVERSE_MSE: 0.70,
    ENSEMBLE_BAYES: 0.90,
    ENSEMBLE_ADAPTIVE: 0.35,
}


def _unit(*parts) -> float:
    """Deterministic pseudo-random value in [-1, 1] from arbitrary parts."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 10001) / 10000.0 * 2.0 - 1.0


def make_functions(fail_ensemble=None, missing_actual_every=None):
    def actual_fn(target, origin, horizon):
        if missing_actual_every and month_index(origin) % missing_actual_every == 0:
            return None
        return 2.0 + 1.5 * _unit(target, origin.isoformat(), horizon, "actual")

    def forecast_fn(ensemble_id, target, origin, horizon):
        if ensemble_id == fail_ensemble:
            raise RuntimeError("synthetic candidate failure")
        actual = actual_fn(target, origin, horizon)
        if actual is None:
            actual = 2.0
        point = actual + FACTORS[ensemble_id] * _unit(target, origin.isoformat(), horizon, "shock")
        return {
            "point": float(point),
            "quantiles": [float(point + value) for value in np.linspace(-1.0, 1.0, 19)],
            "data_class": "REAL_TIME",
            "history": {"weight_source": "HISTORY" if ensemble_id != ENSEMBLE_EQUAL else "EQUAL_DECLARED_CONTROL"},
        }

    return forecast_fn, actual_fn


def synthetic_records(n: int = 20, *, window: str = "W1") -> list:
    """Complete per-origin records with the adaptive factor clearly better than the control."""
    records = []
    for index in range(n):
        shock = _unit(index, "shock") + 0.1
        losses = {}
        for ensemble_id, factor in FACTORS.items():
            losses[ensemble_id] = {
                "squared_error": float((factor * shock) ** 2),
                "crps": float(abs(factor * shock)),
            }
        records.append({
            "origin": f"2000-{index % 12 + 1:02d}-28",
            "target_period": f"2000-{index % 12 + 1:02d}-28",
            "window": window,
            "regime": validation.REGIME_RECESSION if index % 5 == 0 else validation.REGIME_EXPANSION,
            "data_class": validation.DATA_REAL_TIME if index % 4 else validation.DATA_PSEUDO,
            "losses": losses,
        })
    return records


class NberRegimes(unittest.TestCase):
    def test_recession_month_set_matches_the_final_chronology(self):
        self.assertGreater(len(NBER["reference_dates"]), 0)
        for cycle in NBER["reference_dates"]:
            if not cycle.get("peak") or not cycle.get("trough"):
                continue
            start = month_index(date.fromisoformat(cycle["peak"] + "-01"))
            end = month_index(date.fromisoformat(cycle["trough"] + "-01"))
            self.assertIn(start, RECESSION_MONTHS, cycle["peak"])
            self.assertIn(end, RECESSION_MONTHS, cycle["trough"])
            self.assertNotIn(start - 1, RECESSION_MONTHS, cycle["peak"])
            self.assertNotIn(end + 1, RECESSION_MONTHS, cycle["trough"])

    def test_recession_peak_and_trough_months(self):
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "1990-07-31", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "1991-03-31", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "1991-04-30", RECESSION_MONTHS),
                         validation.REGIME_EXPANSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "2001-03-31", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "2007-12-31", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "2009-07-31", RECESSION_MONTHS),
                         validation.REGIME_EXPANSION)

    def test_quarter_target_is_a_recession_when_any_month_is(self):
        self.assertEqual(validation.regime_label("T2_GDP_GROWTH", "2008-09-30", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T2_GDP_GROWTH", "2005-03-31", RECESSION_MONTHS),
                         validation.REGIME_EXPANSION)
        self.assertEqual(validation.regime_label("T2_GDP_GROWTH", "2001-06-30", RECESSION_MONTHS),
                         validation.REGIME_RECESSION)

    def test_labels_use_the_passed_calendar_not_a_usrec_tape(self):
        synthetic = {month_index(date(2008, 9, 1))}
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "2008-09-30", synthetic),
                         validation.REGIME_RECESSION)
        self.assertEqual(validation.regime_label("T1_CPI_YOY", "2008-10-31", synthetic),
                         validation.REGIME_EXPANSION)
        forecast_fn, actual_fn = make_functions()
        report = validation.build_report(
            pairs=(("T1_CPI_YOY", 1),), max_origins=1, replications=99,
            forecast_fn=forecast_fn, actual_fn=actual_fn)
        self.assertIn("not USREC latest", report["nber_dates"]["regime_rule"])


class Collection(unittest.TestCase):
    def test_collect_records_scores_all_candidates(self):
        forecast_fn, actual_fn = make_functions()
        result = validation.collect_records(
            "T1_CPI_YOY", 1, "W1", protocol=PROTOCOL, recession_months=RECESSION_MONTHS,
            max_origins=6, forecast_fn=forecast_fn, actual_fn=actual_fn)
        records = result["records"]
        self.assertGreater(len(records), 0)
        for record in records:
            self.assertEqual(set(record["losses"]), set(ENSEMBLE_IDS))
            self.assertIn(record["regime"], (validation.REGIME_RECESSION, validation.REGIME_EXPANSION))
            self.assertIn(record["data_class"], (validation.DATA_REAL_TIME, validation.DATA_PSEUDO))
            for ensemble_id in ENSEMBLE_IDS:
                self.assertGreaterEqual(record["losses"][ensemble_id]["squared_error"], 0.0)
                self.assertGreaterEqual(record["losses"][ensemble_id]["crps"], 0.0)
        meta = result["meta"]
        self.assertEqual(meta["window"], "W1")
        self.assertEqual(meta["origins_used"], len(records))
        self.assertEqual(meta["candidate_failures"],
                         {ensemble_id: 0 for ensemble_id in ENSEMBLE_IDS})
        self.assertIn("EQUAL_DECLARED_CONTROL", meta["weight_source_counts"][ENSEMBLE_EQUAL])

    def test_collect_records_handles_missing_actuals_and_candidate_failures(self):
        forecast_fn, actual_fn = make_functions(fail_ensemble=ENSEMBLE_BAYES,
                                                missing_actual_every=2)
        result = validation.collect_records(
            "T1_CPI_YOY", 1, "W1", protocol=PROTOCOL, recession_months=RECESSION_MONTHS,
            max_origins=10, forecast_fn=forecast_fn, actual_fn=actual_fn)
        meta = result["meta"]
        self.assertGreater(meta["skipped_no_first_publication_actual"], 0)
        self.assertGreater(meta["candidate_failures"][ENSEMBLE_BAYES], 0)
        self.assertEqual(result["records"], [])  # BAYES is missing at every origin -> incomplete
        self.assertGreater(meta["skipped_incomplete_candidates"], 0)
        self.assertEqual(meta["origins_used"], 0)

    def test_evenly_spaced_sampling_keeps_the_edges(self):
        items = [{"origin": f"2000-{index:02d}-01"} for index in range(100)]
        sampled = validation._evenly_spaced(items, 5)
        self.assertEqual(len(sampled), 5)
        self.assertEqual(sampled[0], items[0])
        self.assertEqual(sampled[-1], items[-1])


class Statistics(unittest.TestCase):
    def test_relative_losses_and_significance(self):
        records = synthetic_records(24)
        summary = validation.summarize(records, horizon=1, replications=499)
        self.assertEqual(summary["n_origins"], 24)
        control = summary["per_ensemble"][ENSEMBLE_EQUAL]
        self.assertTrue(control["is_control"])
        self.assertAlmostEqual(control["relative_rmse_vs_equal"], 1.0)
        self.assertAlmostEqual(control["relative_crps_vs_equal"], 1.0)
        self.assertLess(summary["per_ensemble"][ENSEMBLE_ADAPTIVE]["relative_rmse_vs_equal"], 0.5)
        self.assertLess(summary["per_ensemble"][ENSEMBLE_ADAPTIVE]["relative_crps_vs_equal"], 0.5)
        dm = summary["per_ensemble"][ENSEMBLE_ADAPTIVE]["dm_rmse"]
        self.assertLess(dm["p_value"], 0.05)
        multiplicity = summary["multiplicity"]
        for loss in ("RMSE", "CRPS"):
            self.assertEqual(multiplicity[loss]["family_size"], 3)
            self.assertIn(ENSEMBLE_ADAPTIVE, multiplicity[loss]["rejected"])
            self.assertEqual(multiplicity[loss]["method"], "ROMANO_WOLF_STEPDOWN_STATIONARY_BOOTSTRAP")

    def test_identical_losses_give_no_significance(self):
        records = synthetic_records(20)
        for record in records:
            for ensemble_id in ENSEMBLE_IDS:
                record["losses"][ensemble_id] = {"squared_error": 1.0, "crps": 1.0}
        summary = validation.summarize(records, horizon=1, replications=399)
        for ensemble_id, entry in summary["per_ensemble"].items():
            self.assertAlmostEqual(entry["relative_rmse_vs_equal"], 1.0)
            if not entry["is_control"]:
                self.assertEqual(entry["dm_rmse"]["p_value"], 0.5)
        for loss in ("RMSE", "CRPS"):
            self.assertEqual(summary["multiplicity"][loss]["rejected"], [])
            self.assertEqual(summary["multiplicity"][loss]["adjusted_p_values"][ENSEMBLE_ADAPTIVE], 1.0)

    def test_insufficient_data_is_reported(self):
        self.assertEqual(validation.summarize([], horizon=1)["status"], "NO_RECORDS")
        summary = validation.summarize(synthetic_records(1), horizon=1, replications=49)
        self.assertEqual(summary["status"], "OK")
        self.assertEqual(summary["multiplicity"]["status"], "INSUFFICIENT_N_FOR_SIGNIFICANCE")


class Report(unittest.TestCase):
    @staticmethod
    def build(**overrides):
        forecast_fn, actual_fn = overrides.pop("forecast_fn", make_functions())
        actual = overrides.pop("actual_fn", actual_fn)
        options = dict(pairs=(("T1_CPI_YOY", 1),), max_origins=6, replications=199)
        options.update(overrides)
        return validation.build_report(forecast_fn=forecast_fn, actual_fn=actual,
                                       **options)

    def test_structure_and_determinism(self):
        first = self.build()
        second = self.build()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertNotIn("generated_at", first)
        self.assertEqual(first["report_id"], "AMFE-ENSEMBLE-VALIDATION-V2")
        self.assertIn("T1_CPI_YOY/h1", first["pairs"])
        block = first["pairs"]["T1_CPI_YOY/h1"]
        self.assertEqual(block["status"], "OK")
        for section in ("overall", "by_window", "by_data_class", "by_regime", "digests", "origins"):
            self.assertIn(section, block)
        self.assertEqual(set(block["by_window"]), {"W1", "W2"})
        self.assertEqual(set(block["by_data_class"]),
                         {validation.DATA_REAL_TIME, validation.DATA_PSEUDO})
        self.assertEqual(set(block["by_regime"]),
                         {validation.REGIME_RECESSION, validation.REGIME_EXPANSION})
        self.assertGreater(block["origins"]["total"], 0)
        self.assertGreater(len(first["limitations"]), 3)

    def test_pair_without_actuals_is_skipped(self):
        def no_actual(target, origin, horizon):
            return None
        report = self.build(actual_fn=no_actual)
        block = report["pairs"]["T1_CPI_YOY/h1"]
        self.assertEqual(block["status"], "SKIPPED_NO_DATA")
        self.assertIn("reason", block)
        self.assertNotIn("overall", block)

    def test_report_contains_only_aggregates(self):
        report = self.build()
        self.assertEqual(validation.find_raw_values(report), [])
        text = json.dumps(report, ensure_ascii=False)
        self.assertGreater(len(text), 500)
        self.assertNotIn('"actual_primary"', text)
        self.assertNotIn('"member_weights"', text)
        for forbidden in ("actual_primary", "quantiles", "squared_error\": ["):
            self.assertNotIn(forbidden, text)

    def test_write_report_roundtrip(self):
        report = self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            written = validation.write_report(report, path)
            self.assertEqual(written, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["report_id"], report["report_id"])

    def test_summary_lines_render(self):
        report = self.build()
        lines = validation.summary_lines(report)
        self.assertGreater(len(lines), 0)
        self.assertTrue(any("origins=" in line for line in lines))


class ProtocolWindows(unittest.TestCase):
    def test_t1_horizon_one_windows(self):
        w1 = windows_module.origins_for(PROTOCOL, "T1_CPI_YOY", 1, "W1")
        w2 = windows_module.origins_for(PROTOCOL, "T1_CPI_YOY", 1, "W2")
        self.assertEqual(len(w1), 204)
        self.assertEqual(len(w2), 156)
        self.assertLess(w1[-1]["origin"], w2[0]["origin"])

    def test_t2_horizon_zero_windows(self):
        w1 = windows_module.origins_for(PROTOCOL, "T2_GDP_GROWTH", 0, "W1")
        self.assertEqual(len(w1), 204)
        self.assertTrue(all(row["origin"] <= row["target"] for row in w1[:12]))


LOCAL_VINTAGES = ((ROOT / "data" / "raw" / "CPIAUCSL" / "vintages.zip").exists()
                  and (ROOT / "data" / "raw" / "GDPC1" / "vintages.zip").exists())


@unittest.skipUnless(LOCAL_VINTAGES, "local ALFRED vintage archive is not available")
class FirstPublicationUnits(unittest.TestCase):
    """The scored target is the protocol transformation, not the raw series level."""

    def test_t1_is_percent_year_on_year(self):
        value = validation.first_publication_target("T1_CPI_YOY", date(2000, 1, 31), 1)
        self.assertIsNotNone(value)
        self.assertGreater(value, -5.0)
        self.assertLess(value, 20.0)  # a percent figure, not the ~169 CPI index level

    def test_t2_is_annualised_quarterly_growth(self):
        value = validation.first_publication_target("T2_GDP_GROWTH", date(2000, 1, 31), 0)
        self.assertIsNotNone(value)
        self.assertGreater(value, -30.0)
        self.assertLess(value, 30.0)

    def test_unpublished_period_returns_none(self):
        # a target period that no archived vintage contains is unresolved, not a first publication
        self.assertIsNone(validation.first_publication_target("T1_CPI_YOY", date(2035, 1, 31), 1))


@unittest.skipUnless(REPORT_PATH.exists(), f"report not generated yet: {REPORT_PATH.name}")
class CommittedReport(unittest.TestCase):
    def test_committed_report_structure_and_hygiene(self):
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(report["report_id"], "AMFE-ENSEMBLE-VALIDATION-V2")
        self.assertIn("pairs", report)
        self.assertGreaterEqual(len(report["pairs"]), 1)
        self.assertEqual(validation.find_raw_values(report), [])
        for key, block in report["pairs"].items():
            self.assertIn("status", block)
            if block["status"] == "OK":
                self.assertIn("overall", block)
                self.assertIn("by_regime", block)
                for ensemble_id, entry in block["overall"]["per_ensemble"].items():
                    for field in ("rmse", "mean_crps", "relative_rmse_vs_equal",
                                  "relative_crps_vs_equal"):
                        self.assertIn(field, entry, f"{key}/{ensemble_id}")
                self.assertIn("multiplicity", block["overall"])
        self.assertGreaterEqual(len(report["limitations"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
