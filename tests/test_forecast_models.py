"""Tests for the AMFE baseline forecast models (``src/amfe/forecast``).

The suite has three layers:

* synthetic unit tests - no data files needed: point-in-time contracts, target
  definitions, quantile monotonicity/size, rolling-window metadata, model applicability
  and refusal of invalid origins;
* a specification test - the code's quantile grid and model list mirror
  ``protocols/r3_specification.json`` (the specification itself is never modified);
* a smoke run on the local, git-ignored ``data/raw`` download - skipped when the files
  are absent.  The smoke run checks that every local forecast is built from slices with no
  observation after the origin and that the output shapes are right.

Run from the repository root: ``python tests/test_forecast_models.py``.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.forecast import data as fd  # noqa: E402
from amfe.forecast import models as fm  # noqa: E402

SPEC = ROOT / "protocols" / "r3_specification.json"
DATA_RAW = ROOT / "data" / "raw"
LOCAL_DATA = all((DATA_RAW / name / "latest.csv").exists() for name in ("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS", "USREC"))


# --------------------------------------------------------------------------------------
# Synthetic data: deterministic series with known history and recession dates
# --------------------------------------------------------------------------------------
RECESSIONS = (
    ("1973-11", "1975-03"),
    ("1981-07", "1982-11"),
    ("1990-07", "1991-03"),
    ("2001-03", "2001-11"),
    ("2007-12", "2009-06"),
    ("2020-02", "2020-04"),
)


def _month_range(start: date, end: date) -> np.ndarray:
    return np.arange(fd.month_index(start), fd.month_index(end) + 1, dtype=np.int64)


def _synthetic_monthly(name: str, seed: int) -> fd.Series:
    rng = np.random.default_rng(seed)
    index = _month_range(date(1960, 1, 31), date(2024, 12, 31))
    n = index.size
    values = 100.0 + 0.15 * np.arange(n) + 2.5 * np.sin(2.0 * np.pi * np.arange(n) / 60.0)
    values = values + rng.normal(0.0, 0.15, n)
    return fd.Series(name, "M", index, values)


def _synthetic_gdp() -> fd.Series:
    rng = np.random.default_rng(42)
    index = np.arange(fd.quarter_index(date(1960, 1, 31)), fd.quarter_index(date(2024, 12, 31)) + 1)
    n = index.size
    growth = 3.0 + 1.5 * np.sin(2.0 * np.pi * np.arange(n) / 20.0) + rng.normal(0.0, 0.2, n)
    levels = 1000.0 * np.exp(np.cumsum(growth / 400.0))
    return fd.Series("GDPC1", "Q", index.astype(np.int64), levels)


def _synthetic_usrec() -> fd.Series:
    index = _month_range(date(1960, 1, 31), date(2024, 12, 31))
    values = np.zeros(index.size)
    for start, end in RECESSIONS:
        mask = (index >= fd.month_index(start)) & (index <= fd.month_index(end))
        values[mask] = 1.0
    return fd.Series("USREC", "M", index, values)


def _quarter_cutoff(origin: date, lag_months: int) -> int:
    limit = fd.month_index(origin) - lag_months
    q = max(0, limit // 3)
    while fd.quarter_end_month(q + 1) <= limit:
        q += 1
    while q > 0 and fd.quarter_end_month(q) > limit:
        q -= 1
    return q


def synthetic_context(origin: date = date(2015, 12, 31)) -> fd.DataContext:
    """Pseudo-real-time context: latest tape truncated with the publication lags."""
    origin = fd.month_end_of(origin)
    om = fd.month_index(origin)
    series: dict[str, fd.Series] = {}
    meta: dict[str, fd.SeriesMeta] = {}
    for seed, name in enumerate(("CPIAUCSL", "CPILFESL", "UNRATE", "GS10", "TB3MS")):
        lag = fd.RELEASE_LAG_MONTHS[name]
        full = _synthetic_monthly(name, seed)
        trimmed = full.trimmed_to(om - lag)
        series[name] = trimmed
        meta[name] = fd.SeriesMeta(name, fd.MODE_LATEST, fd.DATA_CLASS_PSEUDO, None, lag, trimmed.last_index)
    usrec = _synthetic_usrec().trimmed_to(om - fd.NBER_ANNOUNCEMENT_LAG_MONTHS)
    series["USREC"] = usrec
    meta["USREC"] = fd.SeriesMeta(
        "USREC", fd.MODE_LATEST, fd.DATA_CLASS_PSEUDO, None, fd.NBER_ANNOUNCEMENT_LAG_MONTHS, usrec.last_index
    )
    gdp = _synthetic_gdp().trimmed_to(_quarter_cutoff(origin, fd.RELEASE_LAG_MONTHS["GDPC1"]))
    series["GDPC1"] = gdp
    meta["GDPC1"] = fd.SeriesMeta(
        "GDPC1", fd.MODE_LATEST, fd.DATA_CLASS_PSEUDO, None, fd.RELEASE_LAG_MONTHS["GDPC1"], gdp.last_index
    )
    return fd.DataContext(origin=origin, series=series, meta=meta, requested_mode=fd.MODE_LATEST)


Q19 = 19


class PointInTimeContract(unittest.TestCase):
    def test_context_slices_never_contain_data_after_the_origin(self):
        ctx = synthetic_context()
        for series in ctx.series.values():
            fd.assert_no_lookahead(series, ctx.origin)  # must not raise

    def test_context_rejects_a_slice_with_future_observations(self):
        ctx = synthetic_context()
        future = _synthetic_monthly("CPIAUCSL", 0).trimmed_to(ctx.origin_month + 6)
        with self.assertRaises(fd.LookAheadError):
            fd.DataContext(
                origin=ctx.origin,
                series={"CPIAUCSL": future},
                meta={"CPIAUCSL": fd.SeriesMeta("CPIAUCSL", fd.MODE_LATEST, fd.DATA_CLASS_PSEUDO)},
                requested_mode=fd.MODE_LATEST,
            )

    def test_assert_no_lookahead_detects_a_future_month(self):
        origin = date(2010, 6, 30)
        future = _synthetic_monthly("CPIAUCSL", 0).trimmed_to(fd.month_index(origin) + 1)
        with self.assertRaises(fd.LookAheadError):
            fd.assert_no_lookahead(future, origin)

    def test_assert_no_lookahead_quarterly_uses_the_quarter_end(self):
        origin = date(2010, 6, 30)
        quarterly = fd.Series(
            "GDPC1", "Q", np.asarray([fd.quarter_index(origin)]), np.asarray([100.0], dtype=float)
        )
        fd.assert_no_lookahead(quarterly, origin)  # quarter ends exactly at the origin
        later = fd.Series(
            "GDPC1", "Q", np.asarray([fd.quarter_index(origin) + 1]), np.asarray([101.0], dtype=float)
        )
        with self.assertRaises(fd.LookAheadError):
            fd.assert_no_lookahead(later, origin)


class TargetDefinitions(unittest.TestCase):
    def test_target_period_calendar_alignment(self):
        origin = date(2008, 6, 30)
        self.assertEqual(fd.target_period("T1_CPI_YOY", origin, 1), ("M", fd.month_index(origin)))
        self.assertEqual(fd.target_period("T1_CPI_YOY", origin, 12), ("M", fd.month_index(origin) + 11))
        self.assertEqual(fd.target_period("T2_GDP_GROWTH", origin, 0), ("Q", fd.quarter_index(origin)))
        self.assertEqual(fd.target_period("T2_GDP_GROWTH", origin, 4), ("Q", fd.quarter_index(origin) + 4))
        with self.assertRaises(ValueError):
            fd.target_period("T1_CPI_YOY", origin, 2)  # not on the frozen horizon grid

    def test_cpi_yoy_definition(self):
        index = np.arange(fd.month_index(date(2000, 1, 31)), fd.month_index(date(2002, 1, 31)) + 1)
        levels = 100.0 * (1.0 + 0.01) ** (index - index[0])
        yoy = fd.cpi_yoy(fd.Series("CPIAUCSL", "M", index, levels))
        self.assertAlmostEqual(yoy.value_at(fd.month_index(date(2001, 1, 31))), 100.0 * (1.01 ** 12 - 1.0), places=8)

    def test_gdp_growth_definition(self):
        q = np.arange(fd.quarter_index(date(2000, 1, 31)), fd.quarter_index(date(2001, 1, 31)) + 1)
        levels = 1000.0 * (1.01) ** (q - q[0])
        growth = fd.gdp_growth(fd.Series("GDPC1", "Q", q, levels))
        self.assertAlmostEqual(float(growth.values[-1]), 400.0 * np.log(1.01), places=8)

    def test_usrec_event_window_is_open_left_closed_right(self):
        usrec = _synthetic_usrec()
        # The 2007-12 recession begins exactly at the first month after a 2007-11 origin.
        self.assertEqual(fd.usrec_event(usrec, fd.month_index(date(2007, 11, 30)), 3), 1)
        # A recession that starts after the interval is not counted.
        self.assertEqual(fd.usrec_event(usrec, fd.month_index(date(2007, 8, 31)), 3), 0)
        # A recession that began before the origin still counts inside the interval.
        self.assertEqual(fd.usrec_event(usrec, fd.month_index(date(2009, 1, 31)), 6), 1)
        # A missing month makes the label unresolved, never 0.
        sparse = fd.Series("USREC", "M", np.asarray([fd.month_index(date(2001, 3, 31))]), np.asarray([1.0]))
        self.assertIsNone(fd.usrec_event(sparse, fd.month_index(date(2001, 1, 31)), 6))


class ModelShapesOnSyntheticData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = synthetic_context()

    def _check_distribution(self, forecast, horizon, expected_window, expected_unit):
        self.assertEqual(forecast.horizon, horizon)
        self.assertIsNotNone(forecast.point)
        self.assertTrue(np.isfinite(forecast.point))
        self.assertEqual(forecast.quantiles.shape, (Q19,))
        self.assertTrue(np.all(np.diff(forecast.quantiles) >= -1e-9), "quantiles must be monotone")
        self.assertTrue(np.all(np.isfinite(forecast.quantiles)))
        self.assertLessEqual(forecast.quantiles[0], forecast.quantiles[9])
        self.assertLessEqual(forecast.quantiles[9], forecast.quantiles[-1])
        self.assertEqual(forecast.estimation_window, expected_window)
        self.assertEqual(forecast.estimation_unit, expected_unit)
        self.assertIsNotNone(forecast.effective_steps)
        self.assertGreaterEqual(forecast.effective_steps, 1)

    def test_t1_models_return_nineteen_monotone_quantiles(self):
        for model_id in ("RW", "AO", "AR_BIC", "BVAR_MINNESOTA"):
            with self.subTest(model=model_id):
                forecast = fm.MODEL_FUNCS[model_id](self.ctx, "T1_CPI_YOY", 1, "CPIAUCSL")
                self._check_distribution(forecast, 1, 180, "months")
                self.assertEqual(forecast.target, "T1_CPI_YOY")
                self.assertEqual(forecast.series_id, "CPIAUCSL")
                self.assertEqual(forecast.data_class, fd.DATA_CLASS_PSEUDO)

    def test_t1_core_series_is_supported(self):
        for model_id in ("RW", "AO", "AR_BIC", "BVAR_MINNESOTA"):
            forecast = fm.MODEL_FUNCS[model_id](self.ctx, "T1_CPI_YOY", 3, "CPILFESL")
            self.assertEqual(forecast.series_id, "CPILFESL")
            self.assertEqual(forecast.quantiles.shape, (Q19,))

    def test_t2_models_return_nineteen_monotone_quantiles(self):
        for model_id in ("AO", "AR_BIC", "BVAR_MINNESOTA"):
            with self.subTest(model=model_id):
                forecast = fm.MODEL_FUNCS[model_id](self.ctx, "T2_GDP_GROWTH", 1)
                self._check_distribution(forecast, 1, 60, "quarters")

    def test_own_nowcast_bridge_horizon_zero(self):
        forecast = fm.own_nowcast_bridge(self.ctx, "T2_GDP_GROWTH", 0)
        self._check_distribution(forecast, 0, 60, "quarters")
        self.assertEqual(forecast.effective_steps, 1)  # last published quarter is Q-1

    def test_t3_models_return_probabilities_without_quantiles(self):
        for model_id in ("PROBIT_SPREAD", "SAHM_RULE"):
            with self.subTest(model=model_id):
                forecast = fm.MODEL_FUNCS[model_id](self.ctx, "T3_RECESSION", 6)
                self.assertIsNotNone(forecast.probability)
                self.assertGreaterEqual(forecast.probability, 0.0)
                self.assertLessEqual(forecast.probability, 1.0)
                self.assertAlmostEqual(forecast.point, forecast.probability)
                self.assertIsNone(forecast.quantiles)
                self.assertEqual(forecast.horizon, 6)
                self.assertEqual(forecast.estimation_window, 240)

    def test_rw_point_is_the_last_published_target_value(self):
        forecast = fm.rw(self.ctx, "T1_CPI_YOY", 1)
        levels = self.ctx.require("CPIAUCSL")
        yoy = fd.cpi_yoy(levels)
        self.assertAlmostEqual(forecast.point, float(yoy.values[-1]), places=10)

    def test_ao_t2_point_is_the_mean_of_the_last_four_quarters(self):
        forecast = fm.ao(self.ctx, "T2_GDP_GROWTH", 1)
        growth = fd.gdp_growth(self.ctx.require("GDPC1"))
        self.assertAlmostEqual(forecast.point, float(np.mean(growth.values[-4:])), places=10)

    def test_different_horizons_give_different_effective_steps(self):
        short = fm.rw(self.ctx, "T1_CPI_YOY", 1)
        long = fm.rw(self.ctx, "T1_CPI_YOY", 12)
        self.assertEqual(long.effective_steps - short.effective_steps, 11)

    def test_forecast_all_returns_every_applicable_model(self):
        # forecast_all builds its own local context; on synthetic data use the registry directly
        produced = {
            model_id: fm.MODEL_FUNCS[model_id](self.ctx, "T1_CPI_YOY", 1)
            for model_id in fm.applicable_models("T1_CPI_YOY", 1)
        }
        self.assertEqual(set(produced), {"RW", "AO", "AR_BIC", "BVAR_MINNESOTA"})
        self.assertEqual(fm.applicable_models("T2_GDP_GROWTH", 1), ("AO", "AR_BIC", "BVAR_MINNESOTA"))
        self.assertIn("OWN_NOWCAST_BRIDGE", fm.applicable_models("T2_GDP_GROWTH", 0))


class RefusalAndApplicability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = synthetic_context()

    def test_model_target_mismatch_raises(self):
        with self.assertRaises(fm.ForecastError):
            fm.rw(self.ctx, "T3_RECESSION", 3)
        with self.assertRaises(fm.ForecastError):
            fm.probit_spread(self.ctx, "T1_CPI_YOY", 3)
        with self.assertRaises(fm.ForecastError):
            fm.own_nowcast_bridge(self.ctx, "T2_GDP_GROWTH", 1)

    def test_mid_quarter_target_horizon_rejected(self):
        with self.assertRaises(ValueError):
            fm.rw(self.ctx, "T1_CPI_YOY", 2)

    def test_missing_input_series_raises(self):
        partial = fd.DataContext(
            origin=self.ctx.origin,
            series={"CPIAUCSL": self.ctx.require("CPIAUCSL")},
            meta={"CPIAUCSL": self.ctx.meta["CPIAUCSL"]},
            requested_mode=fd.MODE_LATEST,
        )
        with self.assertRaises(fd.SeriesUnavailable):
            fm.bvar_minnesota(partial, "T1_CPI_YOY", 1)

    def test_short_history_raises(self):
        short = fd.DataContext(
            origin=date(1961, 6, 30),
            series={"CPIAUCSL": _synthetic_monthly("CPIAUCSL", 0).trimmed_to(fd.month_index(date(1961, 5, 31)))},
            meta={"CPIAUCSL": fd.SeriesMeta("CPIAUCSL", fd.MODE_LATEST, fd.DATA_CLASS_PSEUDO)},
            requested_mode=fd.MODE_LATEST,
        )
        with self.assertRaises(fm.InsufficientData):
            fm.rw(short, "T1_CPI_YOY", 1)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_origin_before_the_data_span_is_rejected(self):
        with self.assertRaises(fd.OriginOutOfRangeError):
            fd.DataContext.from_local(date(1900, 1, 31), mode=fd.MODE_LATEST)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_origin_after_the_data_span_is_rejected(self):
        with self.assertRaises(fd.OriginOutOfRangeError):
            fd.DataContext.from_local(date(2035, 1, 31), mode=fd.MODE_LATEST)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            fm.forecast("NO_SUCH_MODEL", "T1_CPI_YOY", date(2008, 6, 30), 1)


class SpecificationMapping(unittest.TestCase):
    def test_quantile_grid_matches_the_r3_specification(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertEqual([float(q) for q in spec["quantile_grid"]], [float(q) for q in fm.QUANTILE_LEVELS])
        self.assertEqual(len(fm.QUANTILE_LEVELS), Q19)

    def test_implemented_models_match_the_specification_models(self):
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        spec_models = {m["id"] for m in spec["models"] if m["status"] in ("PROPOSED", "PROPOSED_T2_ONLY")}
        for model_id in fm.MODEL_IDS:
            self.assertIn(model_id, spec_models)
        # DFM_FREDMD is specified but blocked on the licence register; SPF is not implemented in R3
        self.assertNotIn("DFM_FREDMD", fm.MODEL_IDS)
        self.assertNotIn("SPF_MEDIAN", fm.MODEL_IDS)

    def test_targets_and_horizons_match_the_protocol(self):
        protocol = json.loads((ROOT / "protocols" / "evaluation_protocol_v4.json").read_text(encoding="utf-8"))
        protocol_horizons = {
            t["id"]: tuple(t.get("horizons_months") or t.get("horizons_quarters")) for t in protocol["targets"]
        }
        for target in fd.TARGET_IDS:
            self.assertEqual(tuple(fd.TARGET_SPEC[target]["horizons"]), protocol_horizons[target])


@unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
class LocalDataSmoke(unittest.TestCase):
    """Smoke run over the local download; no look-ahead, sane shapes on real data."""

    ORIGINS = (date(1998, 6, 30), date(2008, 6, 30), date(2016, 6, 30))

    def _assert_pit(self, ctx):
        for series in ctx.series.values():
            fd.assert_no_lookahead(series, ctx.origin)
        self.assertIn(ctx.data_class, (fd.DATA_CLASS_REAL_TIME, fd.DATA_CLASS_PSEUDO))

    def test_t1_models_on_local_data(self):
        for origin in self.ORIGINS:
            ctx = fd.DataContext.from_local(origin)
            self._assert_pit(ctx)
            for model_id in ("RW", "AO", "AR_BIC", "BVAR_MINNESOTA"):
                for horizon in (1, 12):
                    with self.subTest(origin=origin, model=model_id, horizon=horizon):
                        forecast = fm.MODEL_FUNCS[model_id](ctx, "T1_CPI_YOY", horizon, "CPIAUCSL")
                        self.assertTrue(np.isfinite(forecast.point))
                        self.assertEqual(forecast.quantiles.shape, (Q19,))
                        self.assertTrue(np.all(np.diff(forecast.quantiles) >= -1e-9))
                        self.assertGreaterEqual(forecast.effective_steps, horizon)
                        self.assertEqual(forecast.estimation_window, 180)

    def test_t2_models_on_local_data(self):
        for origin in self.ORIGINS:
            ctx = fd.DataContext.from_local(origin)
            self._assert_pit(ctx)
            for model_id in ("AO", "AR_BIC", "BVAR_MINNESOTA"):
                with self.subTest(origin=origin, model=model_id):
                    forecast = fm.MODEL_FUNCS[model_id](ctx, "T2_GDP_GROWTH", 1)
                    self.assertTrue(np.isfinite(forecast.point))
                    self.assertEqual(forecast.quantiles.shape, (Q19,))
                    self.assertEqual(forecast.estimation_window, 60)
            nowcast = fm.own_nowcast_bridge(ctx, "T2_GDP_GROWTH", 0)
            self.assertTrue(np.isfinite(nowcast.point))
            self.assertEqual(nowcast.quantiles.shape, (Q19,))

    def test_t3_models_on_local_data(self):
        for origin in (date(2004, 6, 30), date(2015, 6, 30)):
            ctx = fd.DataContext.from_local(origin)
            self._assert_pit(ctx)
            for model_id in ("PROBIT_SPREAD", "SAHM_RULE"):
                for horizon in (3, 12):
                    with self.subTest(origin=origin, model=model_id, horizon=horizon):
                        forecast = fm.MODEL_FUNCS[model_id](ctx, "T3_RECESSION", horizon)
                        self.assertGreaterEqual(forecast.probability, 0.0)
                        self.assertLessEqual(forecast.probability, 1.0)
                        self.assertIsNone(forecast.quantiles)
                        self.assertEqual(forecast.estimation_window, 240)

    def test_local_available_data_has_no_future_rows(self):
        origin = date(2008, 6, 30)
        slices = fd.available_data(origin)
        self.assertTrue(slices)
        for name, series in slices.items():
            fd.assert_no_lookahead(series, origin)
            self.assertIsNotNone(series.last_index, name)

    def test_forecast_helper_and_rolling_runner(self):
        forecast = fm.forecast("RW", "T1_CPI_YOY", date(2008, 6, 30), 1)
        self.assertEqual(forecast.model_id, "RW")
        self.assertEqual(forecast.origin, fd.month_end_of(date(2008, 6, 30)))
        records = fm.run_rolling("RW", "T1_CPI_YOY", 1, [date(2008, 1, 31), date(2035, 1, 31)])
        self.assertIn("point", records[0])
        self.assertIn("error", records[1])  # the runner records a refusal, it does not crash

    def test_rw_point_matches_the_vintage_on_real_data(self):
        origin = date(2008, 6, 30)
        ctx = fd.DataContext.from_local(origin)
        vintage = ctx.require("CPIAUCSL").vintage_date
        self.assertIsNotNone(vintage)
        forecast = fm.rw(ctx, "T1_CPI_YOY", 1)
        levels = ctx.require("CPIAUCSL")
        self.assertAlmostEqual(forecast.point, float(fd.cpi_yoy(levels).values[-1]), places=10)
        self.assertEqual(forecast.vintage_date, vintage.isoformat())


class LocalRealTimeVintageMode(unittest.TestCase):
    """REAL_TIME class: every slope comes from a vintage available at the origin."""

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_vintage_mode_is_used_where_a_vintage_exists(self):
        ctx = fd.DataContext.from_local(date(2008, 6, 30), mode=fd.MODE_VINTAGE)
        self.assertEqual(ctx.data_class, fd.DATA_CLASS_REAL_TIME)
        self.assertIsNotNone(ctx.meta["CPIAUCSL"].vintage_date)
        forecast = fm.rw(ctx, "T1_CPI_YOY", 1)
        self.assertEqual(forecast.data_class, fd.DATA_CLASS_REAL_TIME)
        # the yearly vintage grid is stale: the effective horizon exceeds the protocol horizon
        self.assertGreater(forecast.effective_steps, 1)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_pseudo_real_time_mode_is_flagged(self):
        ctx = fd.DataContext.from_local(date(2008, 6, 30), mode=fd.MODE_LATEST)
        self.assertEqual(ctx.data_class, fd.DATA_CLASS_PSEUDO)
        forecast = fm.rw(ctx, "T1_CPI_YOY", 1)
        self.assertEqual(forecast.data_class, fd.DATA_CLASS_PSEUDO)
        self.assertEqual(forecast.effective_steps, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
