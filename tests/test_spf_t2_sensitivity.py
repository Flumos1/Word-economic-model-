"""Tests for the SPF sensitivity analysis in the T2 scoring (``tools/spf_t2_sensitivity.py``).

The suite is offline and standard-library-plus-numpy only:

* the release-date availability rule (h=0 middle-month rule, h=1 quarter-end rule) against the official
  ``spf_calendar`` anchors - the SPF values themselves are synthetic fixtures, never third-party data;
* the mixture sanity / CRPS gate (docs/91 defect detection, docs/94 fix detection);
* the paired RMSE/DM/Romano-Wolf summariser on synthetic per-origin records (improvement, identical
  variants, digests, structure, determinism);
* the committed ``protocols/spf_t2_sensitivity_report.json`` contract: aggregates only, no SPF values,
  CRPS status consistent with the data-quality block, T2 mapping, licence note;
* the honest NOT_AVAILABLE path when the git-ignored SPF file is absent, and the CLI guard rails.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import spf_t2_sensitivity as tool  # noqa: E402
from amfe.ensemble.sources import spf_available, spf_forecast  # noqa: E402

REPORT = ROOT / "protocols" / "spf_t2_sensitivity_report.json"
LOCAL_SPF = spf_available()

#: Synthetic SPF medians fixture: two survey quarters, both forecast codes, clearly not real values.
SYNTHETIC_MEDIANS = {
    "drgdp2": {"1990Q3": {"value": 1.5, "survey_date": "1990-08-01"},
               "2020Q1": {"value": 2.0, "survey_date": "2020-02-01"}},
    "drgdp3": {"1990Q3": {"value": 1.7, "survey_date": "1990-08-01"}},
}


def record(origin, actual, no_spf, with_spf, *, spf_available_flag=True, spf_point=None,
           quantiles_delta=0.0, crps_ok=True):
    """One synthetic per-origin record in the tool's internal format."""
    levels = np.array([-1.64, -1.28, -0.84, -0.52, -0.26, 0.0, 0.26, 0.52, 0.84, 1.28,
                       1.64, 2.0, 2.33, 2.58, 2.81, 3.09, 3.4, 3.8, 4.2])
    base = float(actual)
    return {
        "origin": origin,
        "target": "1990-03-31",
        "actual": base,
        "point_no_spf": float(no_spf),
        "point_with_spf": float(with_spf),
        "quantiles_no_spf": list(base + 2.0 * levels),
        "quantiles_with_spf": list(base + 2.0 * levels + quantiles_delta),
        "spf_available": bool(spf_available_flag),
        "spf_point": float(spf_point) if spf_point is not None else None,
        "spf_weight": 0.2 if spf_available_flag else 0.0,
        "n_members_no_spf": 4,
        "n_members_with_spf": 5 if spf_available_flag else 4,
        "crps_ok": bool(crps_ok),
    }


def numeric_lists(value, path="report"):
    """Yield every list of numbers longer than 12 entries (the report must carry aggregates)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from numeric_lists(item, f"{path}.{key}")
    elif isinstance(value, list):
        if value and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                         for item in value):
            if len(value) > 12:
                yield path, len(value)
        for index, item in enumerate(value):
            yield from numeric_lists(item, f"{path}[{index}]")


class SpfAvailability(unittest.TestCase):
    def test_middle_month_release_rule_h0(self):
        # 2020Q1: official news release 2020-02-14 (spf_calendar anchor); origin is the month end
        verdict = tool.spf_availability("2020-02-29", 0, medians=SYNTHETIC_MEDIANS)
        self.assertTrue(verdict["available"], verdict)
        self.assertEqual(verdict["survey_quarter"], "2020Q1")
        self.assertEqual(verdict["release_date"], "2020-02-14")
        self.assertEqual(verdict["code"], "drgdp2")
        self.assertEqual(verdict["value"], 2.0)
        # 1990Q3: anchor 1990-08-31, the first real-time survey
        self.assertTrue(tool.spf_availability("1990-08-31", 0, medians=SYNTHETIC_MEDIANS)["available"])
        # not a middle month
        self.assertEqual(tool.spf_availability("2020-01-31", 0, medians=SYNTHETIC_MEDIANS)["reason"],
                         "ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH")
        # before 1990Q3 the survey was not real time
        self.assertEqual(tool.spf_availability("1990-02-28", 0, medians=SYNTHETIC_MEDIANS)["reason"],
                         "SPF_NOT_REAL_TIME_BEFORE_1990Q3")

    def test_quarter_end_rule_h1(self):
        # h=1 origins are quarter ends; the survey of the origin quarter is released in its middle
        # month, so its drgdp3 forecast for the target quarter is public by the quarter end.
        verdict = tool.spf_availability("1990-09-30", 1, medians=SYNTHETIC_MEDIANS)
        self.assertTrue(verdict["available"], verdict)
        self.assertEqual(verdict["code"], "drgdp3")
        self.assertEqual(verdict["value"], 1.7)
        self.assertEqual(verdict["survey_quarter"], "1990Q3")
        self.assertEqual(verdict["release_date"], "1990-08-31")
        # the synthetic file has no drgdp3 for 2020Q1: explicit reason, not a fabricated value
        self.assertEqual(tool.spf_availability("2020-03-31", 1, medians=SYNTHETIC_MEDIANS)["reason"],
                         "SURVEY_QUARTER_NOT_IN_SPF_FILE")
        self.assertEqual(tool.spf_availability("1990-03-31", 1, medians=SYNTHETIC_MEDIANS)["reason"],
                         "SPF_NOT_REAL_TIME_BEFORE_1990Q3")
        self.assertEqual(tool.spf_availability("1990-09-30", 3, medians=SYNTHETIC_MEDIANS)["reason"],
                         "NO_SPF_HORIZON_CODE_FOR_H3")

    def test_missing_values_are_reported_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp:
            verdict = tool.spf_availability("2020-02-29", 0, data_root=Path(tmp))
        self.assertFalse(verdict["available"])
        self.assertEqual(verdict["reason"], "SPF_VALUES_NOT_DOWNLOADED")
        self.assertIsNone(verdict["value"])

    def test_availability_summary_counts_and_reasons(self):
        origins = ["1990-01-31", "1990-02-28", "1990-08-31", "2020-01-31", "2020-02-29"]
        summary = tool.spf_availability_summary(origins, 0, medians=SYNTHETIC_MEDIANS)
        self.assertEqual(summary["origins"], 5)
        self.assertEqual(summary["available"], 2)
        self.assertEqual(summary["first_available"], "1990-08-31")
        self.assertEqual(summary["not_available_reasons"]["ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH"], 2)
        self.assertEqual(summary["not_available_reasons"]["SPF_NOT_REAL_TIME_BEFORE_1990Q3"], 1)

    @unittest.skipUnless(LOCAL_SPF, "local git-ignored SPF values are absent")
    def test_tool_verdict_matches_the_ensemble_source_verdict(self):
        # h=0 uses the same middle-month rule as the ensemble source; h>0 deliberately extends it to
        # the protocol quarter-end origins (documented in docs/95) and is not compared here.
        for origin, horizon in (("1990-08-31", 0), ("2020-02-28", 0), ("2019-03-29", 0),
                                ("2019-05-31", 0), ("2019-11-29", 0), ("2005-02-28", 0)):
            ours = tool.spf_availability(origin, horizon)
            theirs = spf_forecast("T2_GDP_GROWTH", origin, horizon)
            self.assertEqual(ours["available"], theirs.available, origin)
            if not theirs.available:
                self.assertEqual(ours["reason"], theirs.reason, origin)


class MixtureGate(unittest.TestCase):
    def test_sane_mixture_passes(self):
        point = 2.0
        quantiles = np.linspace(-3.0, 8.0, 19)
        verdict = tool.mixture_sanity(point, quantiles)
        self.assertTrue(verdict["sane"])
        self.assertLessEqual(verdict["max_deviation"], tool.MIXTURE_DEVIATION_LIMIT)

    def test_docs91_extreme_quantiles_are_rejected(self):
        point = 2.0
        quantiles = np.linspace(-3.0, 8.0, 19)
        quantiles[-1] = 18625.0  # the docs/91 artefact
        verdict = tool.mixture_sanity(point, quantiles)
        self.assertFalse(verdict["sane"])
        self.assertEqual(verdict["reason"], "EXTREME_QUANTILES")

    def test_non_monotone_and_non_finite_are_rejected(self):
        quantiles = np.linspace(-3.0, 8.0, 19)
        quantiles[5], quantiles[6] = quantiles[6], quantiles[5]
        self.assertEqual(tool.mixture_sanity(2.0, quantiles)["reason"], "NON_MONOTONE_QUANTILES")
        broken = np.linspace(-3.0, 8.0, 19)
        broken[0] = float("nan")
        self.assertEqual(tool.mixture_sanity(2.0, broken)["reason"], "NON_FINITE_QUANTILES")


class PairedSummary(unittest.TestCase):
    def _records(self, with_spf_bias=-0.5, n=12):
        actuals = [1.0 + 0.5 * math.sin(index) for index in range(n)]
        no_spf = [value + 0.8 for value in actuals]
        with_spf = [value + 0.8 + with_spf_bias for value in actuals]
        return [record(f"2000-{index % 12 + 1:02d}-28", actual, no_spf_point, spf_point,
                       spf_point=spf_point)  # noqa: F821 (explicit below)
                for index, (actual, no_spf_point, spf_point) in
                enumerate(zip(actuals, no_spf, with_spf))]

    def test_improvement_is_detected(self):
        records = self._records()
        summary = tool.summarize_subset(records, horizon=0, loss_set=["RMSE"], seed=7,
                                        replications=199, subset="TEST")
        self.assertEqual(summary["status"], "OK")
        losses = summary["losses"]["RMSE"]
        self.assertLess(losses["with_spf"], losses["no_spf"])
        self.assertLess(losses["relative_with_spf"], 1.0)
        self.assertGreater(losses["rmse_improvement_percent"], 0.0)
        dm = summary["dm"]["RMSE"]
        self.assertLess(dm["mean_differential"], 0.0)
        self.assertEqual(dm["n"], len(records))
        self.assertLess(dm["p_value"], 0.5)
        self.assertIn("romano_wolf", summary)
        self.assertIn(summary["romano_wolf"]["method"], {"ROMANO_WOLF_STEPDOWN_STATIONARY_BOOTSTRAP"})

    def test_identical_variants_give_a_null_result(self):
        records = self._records(with_spf_bias=0.0)
        summary = tool.summarize_subset(records, horizon=0, loss_set=["RMSE"], seed=7,
                                        replications=99, subset="TEST")
        losses = summary["losses"]["RMSE"]
        self.assertAlmostEqual(losses["relative_with_spf"], 1.0, places=12)
        self.assertAlmostEqual(summary["dm"]["RMSE"]["mean_differential"], 0.0, places=12)
        self.assertLessEqual(summary["dm"]["RMSE"]["p_value"], 0.5)

    def test_without_spf_points_has_no_spf_only_block(self):
        records = self._records()
        for row in records:
            row["spf_point"] = None
        summary = tool.summarize_subset(records, horizon=0, loss_set=["RMSE"], seed=7,
                                        replications=99, subset="TEST")
        self.assertNotIn("spf_only", summary)

    def test_empty_subset_is_not_available(self):
        summary = tool.summarize_subset([], horizon=0, loss_set=["RMSE"], seed=7,
                                        replications=99, subset="EMPTY")
        self.assertEqual(summary["status"], "NOT_AVAILABLE")
        self.assertEqual(summary["reason"], "NO_ORIGINS")
        self.assertEqual(summary["losses"], {})

    def test_summary_carries_only_aggregates_and_digests(self):
        records = self._records()
        summary = tool.summarize_subset(records, horizon=0, loss_set=["RMSE", "CRPS"], seed=7,
                                        replications=199, subset="TEST")
        self.assertEqual(list(numeric_lists(summary)), [])
        text = json.dumps(summary)
        for key in ("actual", "point_no_spf", "point_with_spf", "spf_point"):
            self.assertNotIn(f'"{key}"', text)
        self.assertRegex(summary["losses"]["RMSE"]["digest"], r"^[0-9a-f]{64}$")

    def test_summary_is_deterministic(self):
        records = self._records()
        first = tool.summarize_subset(records, horizon=0, loss_set=["RMSE", "CRPS"], seed=11,
                                      replications=199, subset="TEST")
        second = tool.summarize_subset(records, horizon=0, loss_set=["RMSE", "CRPS"], seed=11,
                                       replications=199, subset="TEST")
        self.assertEqual(first, second)


class ReportContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REPORT.exists():
            raise unittest.SkipTest("report is produced by tools/spf_t2_sensitivity.py")
        cls.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_structure(self):
        report = self.report
        for key in ("report_id", "version", "tool", "generated_at", "purpose", "protocol",
                    "target", "horizons", "ensemble", "calendar_rule", "spf_mapping",
                    "spf_attribution", "spf_licence_note", "spf_values_present", "results",
                    "data_quality", "limitations", "next_steps", "content_sha256", "status",
                    "status_reason", "summary", "availability", "weights_diagnostics"):
            self.assertIn(key, report)
        self.assertEqual(report["report_id"], "AMFE-SPF-T2-SENSITIVITY")
        self.assertEqual(report["target"], "T2_GDP_GROWTH")
        self.assertIn(report["status"], {"OK", "PARTIAL", "NOT_AVAILABLE"})
        serialized = json.dumps(report)
        self.assertNotIn('"admission":', serialized,
                         "the report is diagnostic: it must not carry an admission decision")
        self.assertNotIn("CANDIDATE", serialized)
        self.assertIn("no admission is assigned", report["purpose"])
        self.assertIn("research use", report["spf_licence_note"])
        self.assertTrue(report["limitations"] and report["next_steps"])

    def test_t2_mapping_and_horizons(self):
        mapping = self.report["spf_mapping"]["T2_GDP_GROWTH"]
        self.assertEqual(mapping, {"0": "drgdp2", "1": "drgdp3", "2": "drgdp4", "4": "drgdp6"})
        self.assertEqual(self.report["spf_mapping"]["not_used"]["h"], 3)
        self.assertIn(0, self.report["horizons"])
        for horizon in self.report["results"]:
            self.assertIn(horizon, {"0", "1", "2", "4"})

    def test_results_are_aggregates_only(self):
        self.assertEqual(list(numeric_lists(self.report)), [],
                         "the report must not carry per-origin arrays")
        for forbidden_key in ("records", "per_origin_values", "actual_values", "spf_values_list"):
            self.assertNotIn(forbidden_key, self.report)
        text = json.dumps(self.report)
        self.assertNotIn('"point":', text)
        self.assertNotIn('"actual":', text)

    def test_crps_status_is_consistent_with_data_quality(self):
        for horizon, block in self.report["results"].items():
            quality = self.report["data_quality"][f"T2_GDP_GROWTH/h{horizon}"]
            self.assertEqual(block["crps"]["available"], quality["crps_usable"])
            if not block["crps"]["available"]:
                self.assertEqual(block["crps"]["status"], "NOT_AVAILABLE")
                self.assertTrue(block["crps"]["reason"])
                self.assertGreaterEqual(quality["mixture_sanity"]["rejected"], 0)
            else:
                self.assertEqual(block["crps"]["status"], "AVAILABLE")
                for window in ("W1", "W2", "ALL"):
                    self.assertIn("CRPS", block["windows"][window]["full_sample"]["losses"])

    def test_availability_block_uses_the_release_calendar(self):
        if not self.report["spf_values_present"]:
            self.skipTest("SPF values were not available when the report was generated")
        for horizon, block in self.report["availability"].items():
            self.assertGreater(block["origins"], 0)
            self.assertGreater(block["available"], 0, horizon)
            self.assertIsNotNone(block["first_available"])
            reasons = block["not_available_reasons"]
            self.assertTrue(reasons, "non-available origins must have an explicit reason")
            for reason in reasons:
                self.assertIn(reason, {"ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH",
                                       "SPF_NOT_REAL_TIME_BEFORE_1990Q3",
                                       "SPF_NEWS_RELEASE_NOT_PUBLISHED_AT_ORIGIN",
                                       "SPF_VALUES_NOT_DOWNLOADED",
                                       "SURVEY_QUARTER_NOT_IN_SPF_FILE",
                                       "SPF_MEMBER_EXCLUDED_BY_ENSEMBLE"})

    def test_paired_subset_has_dm_and_romano_wolf(self):
        for horizon, block in self.report["results"].items():
            for window in ("W1", "W2", "ALL"):
                paired = block["windows"][window]["paired_spf_subsample"]
                self.assertIn(paired["status"], {"OK", "NOT_AVAILABLE"})
                if paired["status"] != "OK":
                    continue
                self.assertIn("RMSE", paired["dm"])
                self.assertIn("p_value", paired["dm"]["RMSE"])
                self.assertIsNotNone(paired["romano_wolf"])
                losses = paired["losses"]["RMSE"]
                self.assertAlmostEqual(losses["with_spf"] / losses["no_spf"],
                                       losses["relative_with_spf"], places=9)
                self.assertLessEqual(losses["with_spf"] / losses["no_spf"], 1.0)

    def test_content_hash_covers_the_report(self):
        digest = self.report["content_sha256"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        copy = {key: value for key, value in self.report.items()
                if key not in ("generated_at", "content_sha256")}
        self.assertEqual(tool.content_digest(copy), digest)


class AvailabilityOnlyPaths(unittest.TestCase):
    def test_check_plan_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = tool.check_plan(data_root=Path(tmp), horizons=(0,))
        self.assertEqual(plan["status"], "NOT_AVAILABLE")
        self.assertEqual(plan["status_reason"], "SPF_VALUES_NOT_DOWNLOADED")
        self.assertFalse(plan["spf_values_present"])
        self.assertEqual(plan["horizons"]["0"]["available"], 0)
        reasons = plan["horizons"]["0"]["not_available_reasons"]
        self.assertEqual(sum(reasons.values()), plan["horizons"]["0"]["origins"])
        self.assertGreater(reasons["SPF_VALUES_NOT_DOWNLOADED"], 0)
        self.assertGreater(reasons["ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH"], 0)

    def test_cli_check_returns_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = tool.main(["--check", "--horizons", "0", "--data-root", tmp])
            payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "NOT_AVAILABLE")

    def test_build_report_without_values_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = tool.build_report([0], data_root=Path(tmp), max_origins=0)
        self.assertEqual(report["status"], "NOT_AVAILABLE")
        self.assertEqual(report["status_reason"], "SPF_VALUES_NOT_DOWNLOADED")
        self.assertFalse(report["spf_values_present"])
        self.assertEqual(report["results"]["0"]["crps"]["status"], "NOT_AVAILABLE")
        self.assertEqual(list(numeric_lists(report)), [])

    def test_cli_guards_unsupported_target_and_horizon(self):
        with self.assertRaises(SystemExit):
            tool.main(["--check", "--target", "T1_CPI_YOY"])
        with self.assertRaises(SystemExit):
            tool.main(["--check", "--horizons", "3"])


class StaticRules(unittest.TestCase):
    def test_hac_horizon_mapping(self):
        self.assertEqual(tool.HAC_HORIZON[0], 3)
        for horizon in (1, 2, 4):
            self.assertEqual(tool.HAC_HORIZON[horizon], 1)

    def test_target_units_come_from_the_evaluation_layer(self):
        # the tool never transforms the target itself: it delegates to actual_pair, whose module
        # documents the protocol definitions (T1 yoy, T2 400*ln growth).
        from amfe.evaluation import actuals
        doc = actuals._target_from_series.__doc__ or ""
        self.assertIn("T2", doc)
        self.assertIn("400", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
