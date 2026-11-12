"""Tests for the stage-7 ensemble layer (``src/amfe/ensemble``).

Layers:
  * weight schemes on synthetic errors: non-negativity, sum to one, ranking, correlation
    shrinkage, Bayesian updating, outlier robustness, horizon/regime adaptation, missing members;
  * distribution mixtures: equality with the protocol's equal-weight mixture, monotonicity, bounds,
    probability pooling;
  * sources: provenance classification (external forecast vs derived statistic), SPF horizon
    mapping and availability rules, missing source handling, RTDSM metadata-only status, data policy
    (third-party values only in the git-ignored directory);
  * history: point-in-time availability of actuals, aligned error matrix, observable regime label;
  * combination: point/quantile/probability outputs, data-class propagation, weight redistribution,
    external-attribution handling, diagnostics;
  * compatibility with the admission harness (``tools/evaluate.py`` functions);
  * a local-data smoke run (skipped when ``data/raw`` is absent).

Run from the repository root: ``python tests/test_ensemble.py``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import amfe.ensemble as E  # noqa: E402
from amfe.ensemble import mixture, weights as W  # noqa: E402
from amfe.ensemble.sources import (  # noqa: E402
    KIND_DERIVED_STATISTIC,
    KIND_EXTERNAL_DATA,
    KIND_EXTERNAL_FORECAST,
    KIND_INTERNAL_MODEL,
    SPF_GDP_CODE_FOR_HORIZON,
    EnsembleError,
    classify_member,
    is_third_party_forecast,
    member_kind,
    spf_available,
    spf_forecast,
    spf_values_path,
    survey_quarter_for_origin,
)
import evaluate  # noqa: E402

DATA_RAW = ROOT / "data" / "raw"
SERIES_FILES = ("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS", "USREC")
LOCAL_DATA = all((DATA_RAW / name / "latest.csv").exists() for name in SERIES_FILES)
SPF_LOCAL = (DATA_RAW / "spf" / "spf_median.json").exists()
QGRID = np.round(np.arange(0.05, 1.0, 0.05), 2)


# --------------------------------------------------------------------------------------
# Synthetic helpers
# --------------------------------------------------------------------------------------
def quantile_vector(center: float, spread: float = 1.0) -> np.ndarray:
    """A monotone 19-quantile vector centred on ``center``."""
    return np.asarray(center, dtype=float) + np.linspace(-1.5, 1.5, 19) * float(spread)


def member_part(member_id: str, point=None, probability=None, quantiles=None,
                data_class: str = "REAL_TIME", **extra) -> dict:
    try:
        kind = member_kind(member_id)
    except EnsembleError:  # synthetic test-only member ids live outside the provenance registry
        kind = "TEST_MEMBER"
    part = {
        "member_id": member_id,
        "kind": kind,
        "point": None if point is None else float(point),
        "probability": None if probability is None else float(probability),
        "quantiles": None if quantiles is None else [float(v) for v in quantiles],
        "data_class": data_class,
    }
    part.update(extra)
    return part


def synthetic_errors(n: int = 200, members=("A", "B", "C"), seed: int = 0):
    rng = np.random.default_rng(seed)
    scales = {"A": 1.0, "B": 2.0, "C": 1.5}
    return {member: rng.normal(0.0, scales.get(member, 1.0), n) for member in members}


def synthetic_history(errors_by_member, regimes=None, actual_value: float = 0.0,
                      target: str = "T1_CPI_YOY") -> list[dict]:
    members = list(errors_by_member)
    n = len(errors_by_member[members[0]])
    rows = []
    for index in range(n):
        parts = {}
        for member_id in members:
            error = float(errors_by_member[member_id][index])
            if not np.isfinite(error):
                continue
            if target == "T3_RECESSION":
                parts[member_id] = member_part(member_id, probability=actual_value + error)
            else:
                parts[member_id] = member_part(member_id, point=actual_value + error)
        rows.append(
            {
                "origin": f"20{index // 12:02d}-{index % 12 + 1:02d}-28",
                "target": target,
                "horizon": 1,
                "actual": float(actual_value),
                "actual_source": "TEST",
                "regime": (regimes[index] if regimes is not None else "POSITIVE_SPREAD"),
                "parts": parts,
            }
        )
    return rows


def write_spf_values(root: Path, series: list[dict]) -> None:
    path = Path(root) / "spf" / "spf_median.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dataset": "TEST SPF medians",
                "source_url": "file://test",
                "retrieved_at": "2026-11-12T00:00:00Z",
                "series": series,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def sample_spf_series() -> list[dict]:
    rows = []
    values = {"drgdp2": 0.5, "drgdp3": 1.7, "drgdp4": 2.1, "drgdp5": 2.5, "drgdp6": 2.8}
    for quarter, year, month_day in (("2008Q1", 2008, "2008-02-01"), ("2008Q2", 2008, "2008-05-01")):
        for code, value in values.items():
            rows.append(
                {
                    "variable": "Real GDP growth",
                    "code": code,
                    "horizon": code,
                    "survey_quarter": quarter,
                    "survey_date": month_day,
                    "value": value + (0.1 if quarter == "2008Q2" else 0.0),
                }
            )
    return rows


# --------------------------------------------------------------------------------------
# Weight schemes
# --------------------------------------------------------------------------------------
class WeightSchemes(unittest.TestCase):
    def test_equal_weights_sum_to_one(self):
        result = W.equal_weights(["A", "B", "C"])
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)
        self.assertTrue(all(value > 0 for value in result.weights.values()))
        self.assertAlmostEqual(result.effective_n, 3.0, places=12)

    def test_inverse_mse_favours_the_better_member(self):
        errors = {"A": np.zeros(50) + 0.1, "B": np.zeros(50) + 2.0}
        result = W.inverse_mse_weights(["A", "B"], errors)
        self.assertGreater(result.weights["A"], result.weights["B"])
        self.assertGreater(result.weights["A"], 0.9)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)

    def test_inverse_mse_handles_a_missing_member_via_the_prior(self):
        errors = {"A": np.full(50, 0.5), "B": np.full(50, 1.0), "C": np.full(50, np.nan)}
        result = W.inverse_mse_weights(["A", "B", "C"], errors, prior_strength=0.1)
        self.assertIn("C", result.weights)
        self.assertGreater(result.weights["C"], 0.0)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)

    def test_outlier_robustness_of_inverse_mse(self):
        rng = np.random.default_rng(1)
        good = rng.normal(0.0, 1.0, 200)
        good[0] = 1000.0  # one catastrophic origin
        errors = {"A": good, "B": rng.normal(0.0, 1.2, 200)}
        plain = W.inverse_mse_weights(["A", "B"], errors)
        robust = W.inverse_mse_weights(["A", "B"], errors, robust_clip=0.99)
        self.assertGreater(robust.weights["A"], plain.weights["A"])

    def test_shrinkage_covariance_is_positive_definite(self):
        rng = np.random.default_rng(2)
        base = rng.normal(0.0, 1.0, 40)
        matrix = np.column_stack([base, base + 1e-9 * rng.normal(size=40), rng.normal(size=40)])
        raw = np.cov(matrix, rowvar=False)
        shrink = W.shrinkage_covariance(matrix, shrink=0.5)
        self.assertTrue(np.all(np.linalg.eigvalsh(shrink) > 0.0))
        self.assertGreater(np.linalg.cond(raw), np.linalg.cond(shrink))
        unbiased = W.shrinkage_covariance(matrix, shrink=1.0)
        self.assertAlmostEqual(unbiased[0, 1], 0.0, places=12)

    def test_correlation_weights_are_valid_and_reduce_portfolio_variance(self):
        rng = np.random.default_rng(3)
        base = rng.normal(0.0, 1.0, 120)
        matrix = {"A": base, "B": base + 0.05 * rng.normal(size=120), "C": rng.normal(0.0, 1.4, 120)}
        result = W.correlation_weights(["A", "B", "C"], matrix)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)
        self.assertTrue(all(value >= 0.0 for value in result.weights.values()))
        self.assertEqual(result.method, "MIN_VARIANCE_SHRINKAGE_COVARIANCE")
        covariance = W.shrinkage_covariance(np.column_stack([matrix[m] for m in ("A", "B", "C")]))
        vector = np.asarray([result.weights[m] for m in ("A", "B", "C")])
        equal = np.full(3, 1.0 / 3.0)
        self.assertLessEqual(vector @ covariance @ vector, equal @ covariance @ equal + 1e-12)

    def test_correlation_weights_fall_back_when_history_is_too_short(self):
        errors = {"A": np.asarray([0.1, 0.2]), "B": np.asarray([0.3, 0.1])}
        result = W.correlation_weights(["A", "B"], errors)
        self.assertIn("FALLBACK", result.method)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)

    def test_bayesian_weights_favour_the_better_member(self):
        rng = np.random.default_rng(4)
        errors = {"A": rng.normal(0.0, 1.0, 200), "B": rng.normal(0.0, 3.0, 200)}
        result = W.bayesian_update_weights(["A", "B"], errors)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)
        self.assertGreater(result.weights["A"], 0.6)
        self.assertGreater(result.weights["A"], result.weights["B"])

    def test_bayesian_update_is_robust_to_one_catastrophic_origin(self):
        rng = np.random.default_rng(5)
        a = rng.normal(0.0, 1.0, 100)
        a[0] = 500.0
        errors = {"A": a, "B": rng.normal(0.0, 2.0, 100)}
        bayes = W.bayesian_update_weights(["A", "B"], errors)
        inverse = W.inverse_mse_weights(["A", "B"], errors)
        self.assertGreater(bayes.weights["A"], inverse.weights["A"])

    def test_bayesian_decay_emphasises_recent_history(self):
        first = {"A": np.full(70, 3.0), "B": np.full(70, 0.5)}
        recent = {"A": np.full(30, 0.2), "B": np.full(30, 3.0)}
        errors = {m: np.concatenate([first[m], recent[m]]) for m in ("A", "B")}
        no_decay = W.bayesian_update_weights(["A", "B"], errors, decay=0.0)
        decay = W.bayesian_update_weights(["A", "B"], errors, decay=0.10)
        self.assertGreater(decay.weights["A"], no_decay.weights["A"])

    def test_adaptive_weights_shrink_towards_equal_at_long_horizons(self):
        errors = synthetic_errors(seed=6)
        short = W.adaptive_weights(["A", "B", "C"], errors, target="T1_CPI_YOY", horizon=1)
        long = W.adaptive_weights(["A", "B", "C"], errors, target="T1_CPI_YOY", horizon=12)
        uniform = np.full(3, 1.0 / 3.0)
        short_deviation = float(np.abs(np.asarray([short.weights[m] for m in ("A", "B", "C")]) - uniform).sum())
        long_deviation = float(np.abs(np.asarray([long.weights[m] for m in ("A", "B", "C")]) - uniform).sum())
        self.assertLess(long_deviation, short_deviation)
        self.assertGreater(long.diagnostics["horizon_shrink"], short.diagnostics["horizon_shrink"])

    def test_adaptive_weights_use_the_current_regime_subset(self):
        n = 80
        members = ["A", "B"]
        errors = {
            "A": np.concatenate([np.full(40, 0.3), np.full(40, 3.0)]),
            "B": np.concatenate([np.full(40, 3.0), np.full(40, 0.3)]),
        }
        regimes = ["REGIME_1"] * 40 + ["REGIME_2"] * 40
        regime_1 = W.adaptive_weights(members, errors, target="T1_CPI_YOY", horizon=1,
                                      regime_labels=regimes, current_regime="REGIME_1")
        regime_2 = W.adaptive_weights(members, errors, target="T1_CPI_YOY", horizon=1,
                                      regime_labels=regimes, current_regime="REGIME_2")
        self.assertTrue(regime_1.diagnostics["regime_used"])
        self.assertGreater(regime_1.weights["A"], regime_1.weights["B"])
        self.assertGreater(regime_2.weights["B"], regime_2.weights["A"])

    def test_renormalize_available_redistributes_missing_weights(self):
        weights = {"A": 0.2, "B": 0.3, "C": 0.5}
        renormalized = W.renormalize_available(weights, ["A", "C"])
        self.assertAlmostEqual(renormalized["A"] + renormalized["C"], 1.0, places=12)
        self.assertAlmostEqual(renormalized["A"], 0.2 / 0.7, places=12)
        with self.assertRaises(EnsembleError):
            W.renormalize_available(weights, ["D"])

    def test_weight_result_rejects_invalid_inputs(self):
        with self.assertRaises(EnsembleError):
            W.WeightResult(weights={"A": -0.1, "B": 1.1}, method="X", member_ids=("A", "B"))
        with self.assertRaises(EnsembleError):
            W.WeightResult(weights={"A": 0.5}, method="X", member_ids=("A", "B"))
        with self.assertRaises(EnsembleError):
            W.weights_for_scheme("NO_SUCH_SCHEME", ["A"], {"A": np.zeros(10)},
                                 target="T1_CPI_YOY", horizon=1)


# --------------------------------------------------------------------------------------
# Distribution mixtures
# --------------------------------------------------------------------------------------
class DistributionMixtures(unittest.TestCase):
    def setUp(self):
        self.vectors = [
            quantile_vector(0.0, 1.0),
            quantile_vector(1.0, 2.0),
            quantile_vector(-0.5, 0.7),
        ]

    def test_equal_weights_match_the_protocol_mixture(self):
        from amfe.evaluation.distributions import mixture_quantiles

        weights = np.full(len(self.vectors), 1.0 / len(self.vectors))
        ours = mixture.weighted_mixture_quantiles(self.vectors, weights)
        protocol = np.asarray(mixture_quantiles(self.vectors), dtype=float)
        self.assertLess(float(np.max(np.abs(ours - protocol))), 1e-10)
        # the public wrapper delegates to the evaluation layer for equal weights
        delegated = mixture.mix_distributions(self.vectors)
        self.assertLess(float(np.max(np.abs(delegated - protocol))), 1e-10)

    def test_weighted_mixture_is_monotone_and_inside_the_member_envelope(self):
        weights = [0.6, 0.3, 0.1]
        mixed = mixture.weighted_mixture_quantiles(self.vectors, weights)
        self.assertEqual(mixed.shape, (19,))
        self.assertTrue(np.all(np.diff(mixed) >= -1e-12))
        bounds = mixture.expected_quantile_bounds(self.vectors)
        self.assertTrue(np.all(mixed >= bounds["lower"] - 1e-9))
        self.assertTrue(np.all(mixed <= bounds["upper"] + 1e-9))

    def test_degenerate_weights_return_the_selected_member(self):
        mixed = mixture.weighted_mixture_quantiles(self.vectors, [1.0, 0.0, 0.0])
        # a finite 4001-point CDF grid makes the inversion exact only up to the grid step
        self.assertLess(float(np.max(np.abs(mixed - self.vectors[0]))), 0.01)

    def test_probability_pooling(self):
        self.assertAlmostEqual(mixture.pool_probabilities([0.2, 0.6]), 0.4, places=12)
        self.assertAlmostEqual(mixture.pool_probabilities([0.2, 0.6], [0.75, 0.25]), 0.3, places=12)
        log_odds = mixture.pool_probabilities([0.01, 0.02], method="log_odds")
        self.assertGreaterEqual(log_odds, mixture.LOG_CLIP[0])
        self.assertLessEqual(log_odds, mixture.LOG_CLIP[1])
        with self.assertRaises(EnsembleError):
            mixture.pool_probabilities([0.2], method="no_such_pool")
        with self.assertRaises(EnsembleError):
            mixture.pool_probabilities([1.2])

    def test_mixture_diagnostics(self):
        diagnostics = mixture.mixture_diagnostics(self.vectors)
        self.assertEqual(diagnostics["n_members"], 3)
        self.assertAlmostEqual(diagnostics["effective_n"], 3.0, places=6)
        self.assertGreaterEqual(diagnostics["median_spread"], 0.0)
        self.assertLessEqual(diagnostics["median_lower"], diagnostics["median_upper"])


# --------------------------------------------------------------------------------------
# Provenance classification and SPF mapping
# --------------------------------------------------------------------------------------
class SourceClassification(unittest.TestCase):
    def test_internal_external_and_derived_kinds(self):
        self.assertEqual(member_kind("RW"), KIND_INTERNAL_MODEL)
        self.assertFalse(is_third_party_forecast("RW"))
        self.assertEqual(member_kind("SPF_GDP_MEDIAN"), KIND_EXTERNAL_FORECAST)
        self.assertTrue(is_third_party_forecast("SPF_GDP_MEDIAN"))
        self.assertEqual(member_kind("SPF_GDP_ERROR_QUANTILES"), KIND_DERIVED_STATISTIC)
        self.assertEqual(member_kind("ENSEMBLE_WEIGHTS"), KIND_DERIVED_STATISTIC)
        self.assertFalse(is_third_party_forecast("EQUAL_WEIGHT_MEAN"))
        self.assertEqual(member_kind("RTDSM"), KIND_EXTERNAL_DATA)
        with self.assertRaises(EnsembleError):
            classify_member("MADE_UP_SOURCE")

    def test_rtdsm_is_metadata_only_and_not_a_forecast_member(self):
        status = E.rtdsm_status()
        self.assertEqual(status["kind"], KIND_EXTERNAL_DATA)
        self.assertIn("not a forecast", status["role"])
        for target in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"):
            self.assertNotIn("RTDSM", E.target_member_ids(target, 1))

    def test_spf_values_are_not_committed_and_metadata_holds_no_values(self):
        metadata = json.loads((ROOT / "protocols" / "spf_metadata.json").read_text(encoding="utf-8"))
        self.assertNotIn("series", metadata)
        self.assertNotIn("values", metadata)

        def assert_no_value_key(node):
            if isinstance(node, dict):
                self.assertNotIn("value", node)
                for child in node.values():
                    assert_no_value_key(child)
            elif isinstance(node, list):
                for child in node:
                    assert_no_value_key(child)

        assert_no_value_key(metadata)
        classification = classify_member("SPF_GDP_MEDIAN")
        self.assertIn("git-ignored", classification["values_location"])
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/data/raw/", gitignore)
        self.assertEqual(spf_values_path().parent.name, "spf")


class SpfMapping(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raw_root = Path(self.temp.name) / "data" / "raw"
        write_spf_values(self.raw_root, sample_spf_series())

    def tearDown(self):
        self.temp.cleanup()

    def test_horizon_mapping_uses_published_codes(self):
        expected = {
            ("T2_GDP_GROWTH", 0): "drgdp2",
            ("T2_GDP_GROWTH", 1): "drgdp3",
            ("T2_GDP_GROWTH", 2): "drgdp4",
            ("T2_GDP_GROWTH", 4): "drgdp6",
        }
        for (target, horizon), code in expected.items():
            self.assertEqual(SPF_GDP_CODE_FOR_HORIZON[target][horizon], code)
            external = spf_forecast(target, date(2008, 5, 31), horizon, data_root=self.raw_root)
            self.assertTrue(external.available, external.reason)
            self.assertEqual(external.code, code)
            self.assertIsNotNone(external.value)
            self.assertEqual(external.survey_quarter, "2008Q2")
            self.assertIn("drgdp2=current survey quarter", external.mapping)

    def test_spf_is_available_only_at_the_survey_deadline_month(self):
        available = spf_forecast("T2_GDP_GROWTH", date(2008, 5, 31), 2, data_root=self.raw_root)
        self.assertTrue(available.available)
        not_a_deadline = spf_forecast("T2_GDP_GROWTH", date(2008, 6, 30), 2, data_root=self.raw_root)
        self.assertFalse(not_a_deadline.available)
        self.assertEqual(not_a_deadline.reason, "ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH")
        # a survey published late in the middle month is not yet known at the month end
        late_root = Path(self.temp.name) / "late" / "raw"
        late_series = [dict(row, survey_date="2008-05-25") for row in sample_spf_series()
                       if row["survey_quarter"] == "2008Q2"]
        write_spf_values(late_root, late_series)
        before_publication = spf_forecast("T2_GDP_GROWTH", date(2008, 5, 31), 2, data_root=late_root)
        self.assertFalse(before_publication.available)
        self.assertEqual(before_publication.reason, "SPF_DEADLINE_NOT_REACHED_AT_ORIGIN")

    def test_spf_has_no_horizon_code_for_h3_and_no_t1_t3_counterpart(self):
        h3 = spf_forecast("T2_GDP_GROWTH", date(2008, 5, 31), 3, data_root=self.raw_root)
        self.assertFalse(h3.available)
        self.assertTrue(h3.reason.startswith("NO_SPF_HORIZON_CODE"))
        t1 = spf_forecast("T1_CPI_YOY", date(2008, 5, 31), 1, data_root=self.raw_root)
        self.assertFalse(t1.available)
        self.assertIn("MONTHLY_YOY", t1.reason)
        t3 = spf_forecast("T3_RECESSION", date(2008, 5, 31), 6, data_root=self.raw_root)
        self.assertFalse(t3.available)
        self.assertIn("NBER", t3.reason)

    def test_missing_spf_values_are_reported_not_fabricated(self):
        empty_root = Path(self.temp.name) / "empty" / "raw"
        empty_root.mkdir(parents=True)
        self.assertFalse(spf_available(empty_root))
        external = spf_forecast("T2_GDP_GROWTH", date(2008, 5, 31), 1, data_root=empty_root)
        self.assertFalse(external.available)
        self.assertEqual(external.reason, "SPF_VALUES_NOT_DOWNLOADED")
        self.assertIsNone(external.value)

    def test_survey_quarter_detection(self):
        self.assertEqual(survey_quarter_for_origin(date(2008, 5, 31)), 2008 * 4 + 1)
        self.assertIsNone(survey_quarter_for_origin(date(2008, 6, 30)))
        self.assertEqual(survey_quarter_for_origin(date(2008, 2, 29)), 2008 * 4 + 0)


# --------------------------------------------------------------------------------------
# History: PIT availability, error matrix, regime label
# --------------------------------------------------------------------------------------
class HistoryCalibration(unittest.TestCase):
    def test_actual_known_by_t1_t2_vintage(self):
        record = {"value": 1.0, "vintage": "2008-06-17", "period_end": "2008-05-31"}
        self.assertTrue(E.actual_known_by("T1_CPI_YOY", date(2008, 5, 31), 1,
                                          date(2008, 6, 30), record))
        self.assertFalse(E.actual_known_by("T1_CPI_YOY", date(2008, 5, 31), 1,
                                           date(2008, 5, 31), record))
        unknown = {"value": 1.0, "vintage": None, "period_end": "2007-12-31"}
        self.assertTrue(E.actual_known_by("T2_GDP_GROWTH", date(2007, 9, 30), 1,
                                          date(2008, 4, 30), unknown))
        self.assertFalse(E.actual_known_by("T2_GDP_GROWTH", date(2007, 9, 30), 1,
                                           date(2008, 2, 29), unknown))

    def test_actual_known_by_t3_resolution_lag(self):
        record = {"value": 1.0, "vintage": None, "period_end": None}
        # event window ends 2011-07; known after 12 more months -> 2012-07
        self.assertTrue(E.actual_known_by("T3_RECESSION", date(2011, 1, 31), 6,
                                          date(2012, 8, 31), record))
        self.assertFalse(E.actual_known_by("T3_RECESSION", date(2011, 1, 31), 6,
                                           date(2012, 6, 30), record))
        self.assertFalse(E.actual_known_by("T3_RECESSION", date(2011, 1, 31), 6,
                                           date(2012, 8, 31), None))

    def test_history_errors_align_members_with_nan(self):
        rows = synthetic_history({"A": np.asarray([0.5, 0.3]), "B": np.asarray([np.nan, 0.1])})
        errors, actuals, regimes = E.history_errors(rows, "T1_CPI_YOY")
        self.assertEqual(set(errors), {"A", "B"})
        self.assertEqual(errors["A"].shape, (2,))
        self.assertTrue(np.isnan(errors["B"][0]))
        self.assertAlmostEqual(errors["B"][1], 0.1, places=12)
        self.assertEqual(len(actuals), 2)
        self.assertEqual(len(regimes), 2)

    def test_fit_weights_uses_the_history(self):
        errors = {"A": np.full(30, 0.5), "B": np.full(30, 2.0)}
        rows = synthetic_history(errors)
        result = E.fit_weights(E.ENSEMBLE_INVERSE_MSE, "T1_CPI_YOY", 1, rows)
        self.assertGreater(result.weights["A"], result.weights["B"])
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)

    def test_fit_weights_without_history_falls_back_to_equal(self):
        result = E.fit_weights(E.ENSEMBLE_BAYES, "T1_CPI_YOY", 1, [])
        self.assertEqual(result.method, "EQUAL_WEIGHTS")
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_observable_regime_label(self):
        regime = E.rate_regime(date(2008, 6, 30))
        self.assertIn(regime, (E.INVERTED_OR_FLAT, E.POSITIVE_SPREAD))
        self.assertEqual(E.rate_regime(date(1900, 1, 31)), E.UNKNOWN_REGIME)


# --------------------------------------------------------------------------------------
# Combination and diagnostics
# --------------------------------------------------------------------------------------
class EnsembleCombination(unittest.TestCase):
    def _parts(self, data_class="REAL_TIME"):
        return {
            "RW": member_part("RW", point=1.0, quantiles=quantile_vector(1.0, 1.0),
                              data_class=data_class),
            "AO": member_part("AO", point=2.0, quantiles=quantile_vector(2.0, 2.0),
                              data_class=data_class),
            "AR_BIC": member_part("AR_BIC", point=3.0, quantiles=quantile_vector(3.0, 1.5),
                                 data_class=data_class),
        }

    def test_equal_combination_point_and_mixture(self):
        parts = self._parts()
        result = E.combine_member_forecasts(
            "T1_CPI_YOY", date(2010, 12, 31), 1, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertAlmostEqual(result["point"], 2.0, places=12)
        self.assertEqual(len(result["quantiles"]), 19)
        self.assertTrue(np.all(np.diff(result["quantiles"]) >= -1e-12))
        self.assertAlmostEqual(sum(result["member_weights"].values()), 1.0, places=12)
        self.assertEqual(result["data_class"], "REAL_TIME")
        from amfe.evaluation.distributions import mixture_quantiles

        expected = mixture_quantiles([parts[m]["quantiles"] for m in parts])
        self.assertLess(float(np.max(np.abs(np.asarray(result["quantiles"]) - expected))), 1e-10)

    def test_missing_member_weights_are_redistributed(self):
        all_parts = self._parts()
        available = {m: all_parts[m] for m in ("RW", "AR_BIC")}
        weights = E.equal_weights(list(all_parts))
        result = E.combine_member_forecasts(
            "T1_CPI_YOY", date(2010, 12, 31), 1, available, weights,
            ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertAlmostEqual(result["point"], 2.0, places=12)  # mean of 1 and 3
        self.assertAlmostEqual(sum(result["member_weights"].values()), 1.0, places=12)
        self.assertEqual(set(result["member_weights"]), {"RW", "AR_BIC"})
        self.assertIn("AO", result["member_weights_all"])
        self.assertAlmostEqual(result["member_weights_all"]["AO"], 1.0 / 3.0, places=12)

    def test_t3_probability_pool(self):
        parts = {
            "PROBIT_SPREAD": member_part("PROBIT_SPREAD", probability=0.2),
            "SAHM_RULE": member_part("SAHM_RULE", probability=0.6),
        }
        result = E.combine_member_forecasts(
            "T3_RECESSION", date(2010, 12, 31), 6, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertIsNone(result["point"])
        self.assertIsNone(result["quantiles"])
        self.assertAlmostEqual(result["probability"], 0.4, places=12)
        self.assertAlmostEqual(sum(result["member_weights"].values()), 1.0, places=12)

    def test_data_class_propagation(self):
        result = E.combine_member_forecasts(
            "T1_CPI_YOY", date(2010, 12, 31), 1, self._parts("REAL_TIME"),
            E.equal_weights(["RW", "AO", "AR_BIC"]), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertEqual(result["data_class"], "REAL_TIME")
        mixed_parts = self._parts("REAL_TIME")
        mixed_parts["AO"]["data_class"] = "PSEUDO_REAL_TIME"
        mixed = E.combine_member_forecasts(
            "T1_CPI_YOY", date(2010, 12, 31), 1, mixed_parts,
            E.equal_weights(list(mixed_parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertEqual(mixed["data_class"], "PSEUDO_REAL_TIME")
        self.assertEqual(mixed["member_data_classes"]["AO"], "PSEUDO_REAL_TIME")

    def test_external_member_is_classified_and_values_are_hidden_by_default(self):
        parts = self._parts()
        parts["SPF_GDP_MEDIAN"] = member_part(
            "SPF_GDP_MEDIAN", point=1.5, quantiles=quantile_vector(1.5, 1.2),
            attribution="Source: Philadelphia Fed SPF",
            external_forecast={"survey_quarter": "2010Q4", "mapping": "drgdp(h+2)", "value": 1.5},
            distribution_kind=KIND_DERIVED_STATISTIC,
        )
        result = E.combine_member_forecasts(
            "T2_GDP_GROWTH", date(2010, 12, 31), 1, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        self.assertEqual(result["member_kinds"]["SPF_GDP_MEDIAN"], KIND_EXTERNAL_FORECAST)
        self.assertIn("SPF_GDP_MEDIAN", result["distribution_members"])
        external = result["external_forecasts"]
        self.assertEqual(len(external), 1)
        self.assertNotIn("value", external[0])
        self.assertIn("attribution", external[0])
        published = E.combine_member_forecasts(
            "T2_GDP_GROWTH", date(2010, 12, 31), 1, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
            publish_external_values=True,
        )
        self.assertIn("value", published["external_forecasts"][0])

    def test_diagnostics_are_derived_statistics(self):
        parts = self._parts()
        parts["SPF_GDP_MEDIAN"] = member_part(
            "SPF_GDP_MEDIAN", point=1.5, quantiles=quantile_vector(1.5, 1.0),
        )
        result = E.combine_member_forecasts(
            "T2_GDP_GROWTH", date(2010, 12, 31), 1, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        diagnostics = E.ensemble_diagnostics(result)
        self.assertEqual(diagnostics["kind"], KIND_DERIVED_STATISTIC)
        self.assertAlmostEqual(diagnostics["hhi"], 0.25, places=12)
        self.assertAlmostEqual(diagnostics["effective_n"], 4.0, places=6)
        self.assertAlmostEqual(diagnostics["external_weight_share"], 0.25, places=12)
        self.assertGreater(diagnostics["member_point_agreement_std"], 0.0)
        self.assertIn("internal_external_gap", diagnostics)

    def test_unknown_ensemble_and_empty_members_raise(self):
        with self.assertRaises(E.EnsembleForecastError):
            E.ensemble_forecast("ENSEMBLE_NOPE", "T1_CPI_YOY", date(2010, 12, 31), 1)
        with self.assertRaises(E.EnsembleForecastError):
            E.combine_member_forecasts("T1_CPI_YOY", date(2010, 12, 31), 1, {},
                                       E.equal_weights(["RW"]), ensemble_id=E.ENSEMBLE_EQUAL)


# --------------------------------------------------------------------------------------
# Harness compatibility and the walk-forward improvement property
# --------------------------------------------------------------------------------------
class HarnessCompatibility(unittest.TestCase):
    def test_ensemble_output_is_accepted_by_the_admission_harness(self):
        parts = {
            "RW": member_part("RW", point=1.0, quantiles=quantile_vector(1.0, 1.0)),
            "AO": member_part("AO", point=2.0, quantiles=quantile_vector(2.0, 2.0)),
        }
        result = E.combine_member_forecasts(
            "T1_CPI_YOY", date(2010, 12, 31), 1, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        actual = np.asarray([1.7], dtype=float)
        quantiles = np.asarray(result["quantiles"], dtype=float)[None, :]
        crps = evaluate.observation_loss("CRPS", actual, quantiles=quantiles)
        squared = evaluate.observation_loss("RMSE", actual, point=np.asarray([result["point"]]))
        self.assertEqual(crps.shape, (1,))
        self.assertTrue(np.isfinite(crps[0]))
        self.assertTrue(np.isfinite(squared[0]))
        self.assertAlmostEqual(float(evaluate.aggregate_loss("CRPS", crps)), float(crps[0]), places=12)

    def test_t3_output_probability_is_accepted_by_the_harness(self):
        parts = {
            "PROBIT_SPREAD": member_part("PROBIT_SPREAD", probability=0.2),
            "SAHM_RULE": member_part("SAHM_RULE", probability=0.8),
        }
        result = E.combine_member_forecasts(
            "T3_RECESSION", date(2010, 12, 31), 6, parts,
            E.equal_weights(list(parts)), ensemble_id=E.ENSEMBLE_EQUAL,
        )
        actual = np.asarray([1.0])
        brier = evaluate.observation_loss("Brier", actual,
                                          probability=np.asarray([result["probability"]]))
        log_score = evaluate.observation_loss("log_score", actual,
                                              probability=np.asarray([result["probability"]]))
        self.assertTrue(np.isfinite(brier[0]))
        self.assertTrue(np.isfinite(log_score[0]))

    def test_inverse_mse_weights_improve_the_held_out_point_loss(self):
        rng = np.random.default_rng(7)
        n = 240
        actual = rng.normal(0.0, 2.0, n)
        forecasts = {
            "A": actual + rng.normal(0.0, 0.8, n),
            "B": actual + rng.normal(0.0, 2.5, n),
        }
        calibration = slice(0, 120)
        evaluation = slice(120, n)
        errors = {m: forecasts[m][calibration] - actual[calibration] for m in forecasts}
        weights = W.inverse_mse_weights(["A", "B"], errors)
        point_inverse = weights.weights["A"] * forecasts["A"][evaluation] + \
            weights.weights["B"] * forecasts["B"][evaluation]
        point_equal = 0.5 * (forecasts["A"][evaluation] + forecasts["B"][evaluation])
        mse_inverse = float(np.mean((point_inverse - actual[evaluation]) ** 2))
        mse_equal = float(np.mean((point_equal - actual[evaluation]) ** 2))
        self.assertLess(mse_inverse, mse_equal)


# --------------------------------------------------------------------------------------
# Local-data smoke run
# --------------------------------------------------------------------------------------
@unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
class LocalEnsembleSmoke(unittest.TestCase):
    def test_equal_ensemble_t1_on_local_data(self):
        result = E.ensemble_forecast(E.ENSEMBLE_EQUAL, "T1_CPI_YOY", date(2008, 6, 30), 1)
        self.assertEqual(result["target"], "T1_CPI_YOY")
        self.assertEqual(result["candidate_id"], E.ENSEMBLE_EQUAL)
        self.assertTrue(np.isfinite(result["point"]))
        self.assertEqual(len(result["quantiles"]), 19)
        self.assertTrue(np.all(np.diff(result["quantiles"]) >= -1e-9))
        self.assertAlmostEqual(sum(result["member_weights"].values()), 1.0, places=12)
        self.assertEqual(result["data_class"], "REAL_TIME")
        self.assertIn("RW", result["members"])

    def test_adaptive_ensemble_t1_uses_history_on_local_data(self):
        E.clear_cache()
        result = E.ensemble_forecast(E.ENSEMBLE_ADAPTIVE, "T1_CPI_YOY", date(2008, 6, 30), 1,
                                     lookback=2, min_history=2)
        self.assertTrue(np.isfinite(result["point"]))
        self.assertIn(result["history"]["weight_source"], ("HISTORY", "EQUAL_FALLBACK_INSUFFICIENT_HISTORY"))
        self.assertGreater(result["history"]["n_rows"], 0)
        self.assertGreaterEqual(result["effective_n"], 1.0)
        self.assertEqual(result["weight_scheme"], "ADAPTIVE")

    def test_external_spf_enters_a_t2_ensemble_only_at_a_survey_deadline(self):
        with_spf = E.ensemble_forecast(E.ENSEMBLE_EQUAL, "T2_GDP_GROWTH", date(2008, 5, 31), 1)
        self.assertIn("SPF_GDP_MEDIAN", with_spf["members"])
        self.assertEqual(len(with_spf["external_forecasts"]), 1)
        self.assertEqual(with_spf["external_forecasts"][0]["survey_quarter"], "2008Q2")
        without_spf = E.ensemble_forecast(E.ENSEMBLE_EQUAL, "T2_GDP_GROWTH", date(2008, 6, 30), 1)
        self.assertNotIn("SPF_GDP_MEDIAN", without_spf["members"])
        self.assertIn("SPF_GDP_MEDIAN", without_spf["excluded_members"])

    def test_t3_ensemble_probability_on_local_data(self):
        result = E.ensemble_forecast(E.ENSEMBLE_EQUAL, "T3_RECESSION", date(2015, 6, 30), 6)
        self.assertIsNone(result["quantiles"])
        self.assertIsNone(result["point"])
        self.assertGreaterEqual(result["probability"], 0.0)
        self.assertLessEqual(result["probability"], 1.0)
        self.assertEqual(result["members"], ["PROBIT_SPREAD", "SAHM_RULE"])

    def test_candidate_forecast_alias_and_cache_determinism(self):
        E.clear_cache()
        first = E.candidate_forecast(E.ENSEMBLE_EQUAL, "T1_CPI_YOY", date(2008, 6, 30), 1)
        second = E.candidate_forecast(E.ENSEMBLE_EQUAL, "T1_CPI_YOY", date(2008, 6, 30), 1)
        self.assertEqual(first["point"], second["point"])
        self.assertEqual(first["quantiles"], second["quantiles"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
