#!/usr/bin/env python3
"""Tests for the country forecast layer (task-60 / UC-06/07/08).

Run: python -m unittest tests.test_country_forecast
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.forecast import country as cf  # noqa: E402

WB_GDP = ROOT / "data" / "mrio" / "wb" / "wb_NY.GDP.MKTP.KD.ZG.json"
WB_CPI = ROOT / "data" / "mrio" / "wb" / "wb_FP.CPI.TOTL.ZG.json"


def _numeric_arrays(node, path=""):
    bad = []
    if isinstance(node, dict):
        for key, value in node.items():
            bad.extend(_numeric_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        numbers = [item for item in node if isinstance(item, (int, float)) and not isinstance(item, bool)]
        if len(numbers) > 8:
            bad.append(path or "<root>")
        for index, item in enumerate(node):
            bad.extend(_numeric_arrays(item, f"{path}[{index}]"))
    return bad


class CountryForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.quarterly = cf.build_report(
            freq="Q", generated_at="2026-11-18T00:00:00Z", allow_synthetic=True
        )
        cls.annual = cf.build_report(
            freq="A", generated_at="2026-11-18T00:00:00Z", allow_synthetic=True
        )

    # -- constants / normalisation ------------------------------------------------------
    def test_spec_constants(self):
        self.assertEqual(cf.COUNTRY_IDS, ("EA", "CN", "GB"))
        self.assertEqual(cf.TARGET_IDS, ("GDP", "CPI"))
        self.assertEqual(cf.HORIZONS, (1, 2, 3, 4))
        self.assertIn("RW", cf.MODEL_IDS)
        self.assertIn("AR_BIC", cf.MODEL_IDS)
        self.assertIn("PANEL_AR1", cf.MODEL_IDS)
        self.assertIn("BRIDGE", cf.MODEL_IDS)

    def test_country_aliases(self):
        self.assertEqual(cf.normalize_country("CHN"), "CN")
        self.assertEqual(cf.normalize_country("GBR"), "GB")
        self.assertEqual(cf.normalize_country("uk"), "GB")
        self.assertEqual(cf.normalize_country("Euro Area"), "EA")
        self.assertEqual(cf.normalize_frequency("quarterly"), "Q")
        self.assertEqual(cf.normalize_frequency("annual"), "A")
        with self.assertRaises(ValueError):
            cf.normalize_country("US")

    def test_period_helpers(self):
        self.assertEqual(cf.period_label(2024 * 4 + 0, "Q"), "2024Q1")
        self.assertEqual(cf.period_label(2024, "A"), "2024")
        self.assertEqual(cf.parse_period("2024-Q2", "Q"), 2024 * 4 + 1)
        self.assertEqual(cf.parse_period("2024", "A"), 2024)
        self.assertEqual(cf.period_end(2024 * 4 + 0, "Q"), date(2024, 3, 31))
        self.assertEqual(cf.period_end(2024, "A"), date(2024, 12, 31))

    # -- synthetic fixture and provenance ----------------------------------------------
    def test_synthetic_fixture_is_explicit(self):
        for country in cf.COUNTRY_IDS:
            for target in cf.TARGET_IDS:
                series = cf.synthetic_series(country, target, "Q")
                self.assertEqual(series.source_class, cf.DATA_CLASS_SYNTHETIC)
                self.assertEqual(series.pit_status, cf.PIT_SYNTHETIC)
                self.assertTrue(series.is_synthetic)
                self.assertGreaterEqual(series.n_obs, 100)
                self.assertIn("synthetic", series.licence.lower())
                self.assertIn("NOT_AVAILABLE", " ".join(series.notes))
                for observation in series.observations:
                    self.assertIsNotNone(observation.release_date)
                    self.assertGreaterEqual(observation.release_date, cf.period_end(observation.period, "Q"))

    def test_synthetic_fixture_is_deterministic(self):
        first = cf.synthetic_series("CN", "GDP", "Q")
        second = cf.synthetic_series("CN", "GDP", "Q")
        self.assertEqual(first.values().tolist(), second.values().tolist())
        self.assertEqual(first.source, second.source)
        different = cf.synthetic_series("GB", "GDP", "Q")
        self.assertNotEqual(first.values().tolist(), different.values().tolist())

    # -- PIT ----------------------------------------------------------------------------
    def test_pit_slicing_filters_unreleased_observation(self):
        series = cf.synthetic_series("CN", "GDP", "Q")
        cut = series.observations[30]
        origin = cut.release_date - timedelta(days=1)
        sliced = series.as_of(origin)
        self.assertEqual(sliced.n_obs, 30)
        self.assertLessEqual(sliced.last_release_date, origin)
        self.assertNotIn(cut.period, sliced.periods().tolist())

    def test_pit_future_observation_is_rejected(self):
        series = cf.synthetic_series("CN", "GDP", "Q")
        cut = series.observations[30]
        origin = cut.release_date - timedelta(days=1)
        with self.assertRaises(cf.PITViolationError):
            series.assert_pit_clean(origin)
        series.as_of(origin).assert_pit_clean(origin)

    def test_pit_release_before_period_end_is_rejected(self):
        with self.assertRaises(cf.PITViolationError):
            cf.CountrySeries(
                country="CN", target="GDP", freq="Q",
                observations=(cf.Observation(period=2024 * 4, value=5.0, release_date=date(2024, 3, 1)),),
                source="test", source_id="TEST", source_class=cf.DATA_CLASS_REAL,
                pit_status=cf.PIT_REAL, licence="test",
            )

    def test_pit_strict_requires_release_dates(self):
        with self.assertRaises(cf.PITViolationError):
            cf.CountrySeries(
                country="CN", target="GDP", freq="Q",
                observations=(cf.Observation(period=2024 * 4, value=5.0, release_date=None),),
                source="test", source_id="TEST", source_class=cf.DATA_CLASS_REAL,
                pit_status=cf.PIT_REAL, licence="test",
            )

    def test_pit_lookahead_cannot_change_forecast(self):
        series = cf.synthetic_series("CN", "GDP", "Q")
        origin = series.observations[40].release_date
        base = cf.forecast_one(series, 1, origin, model="AR_BIC").point
        last = series.observations[-1]
        fake_period = last.period + 1
        fake = cf.Observation(
            period=fake_period,
            value=999.0,
            release_date=cf.period_end(fake_period, "Q") + timedelta(days=45),
        )
        augmented = cf.CountrySeries(
            country=series.country, target=series.target, freq=series.freq,
            observations=series.observations + (fake,), source=series.source,
            source_id=series.source_id, source_class=series.source_class,
            pit_status=series.pit_status, licence=series.licence,
        )
        with self.assertRaises(cf.PITViolationError):
            augmented.assert_pit_clean(origin)
        self.assertEqual(cf.forecast_one(augmented, 1, origin, model="AR_BIC").point, base)

    # -- local PIT provider -------------------------------------------------------------
    def test_local_pit_csv_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "country"
            directory.mkdir(parents=True)
            path = directory / "CN_GDP_Q.csv"
            rows = ["period,value,release_date,vintage"]
            for index in range(25):
                period = 2018 * 4 + index
                release = cf.period_end(period, "Q") + timedelta(days=45)
                rows.append(f"{cf.period_label(period, 'Q')},{5.0 + 0.1 * index},{release.isoformat()},v1")
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            series, probes = cf.load_series("CN", "GDP", "Q", external_root=temp, allow_synthetic=False)
            self.assertIsNotNone(series)
            self.assertEqual(series.source_id, "LOCAL_PIT_CSV")
            self.assertEqual(series.pit_status, cf.PIT_REAL)
            self.assertEqual(series.n_obs, 25)
            self.assertTrue(any(probe.provider == "LOCAL_PIT_CSV" and probe.status == "AVAILABLE" for probe in probes))
            origin = cf.period_end(2018 * 4 + 24, "Q") + timedelta(days=46)
            self.assertEqual(series.as_of(origin).n_obs, 25)

    def test_local_csv_without_release_dates_is_final_vintage(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "country"
            directory.mkdir(parents=True)
            (directory / "GB_CPI_Q.csv").write_text(
                "period,value\n2024Q1,3.0\n2024Q2,3.2\n", encoding="utf-8"
            )
            series, _probes = cf.load_series("GB", "CPI", "Q", external_root=temp, allow_synthetic=False)
            self.assertEqual(series.pit_status, cf.PIT_FINAL_VINTAGE)
            self.assertEqual(series.source_class, cf.DATA_CLASS_REAL)

    def test_local_csv_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "country"
            directory.mkdir(parents=True)
            (directory / "CN_CPI_Q.csv").write_text(
                "period,value\n2024Q1,not-a-number\n", encoding="utf-8"
            )
            with self.assertRaises(cf.CountryForecastError):
                cf.load_series("CN", "CPI", "Q", external_root=temp, allow_synthetic=False)

    # -- availability / fail-closed -----------------------------------------------------
    def test_quarterly_real_data_is_not_available(self):
        for country in cf.COUNTRY_IDS:
            with self.assertRaises(cf.DataNotAvailable):
                cf.load_series(country, "GDP", "Q", allow_synthetic=False)
        _series, probes = cf.load_series("CN", "GDP", "Q", allow_synthetic=True)
        statuses = {probe.provider: probe.status for probe in probes}
        self.assertEqual(statuses["WORLD_BANK_WDI"], "FREQUENCY_MISMATCH")
        self.assertEqual(statuses["SYNTHETIC_FIXTURE"], "AVAILABLE")

    def test_require_real_cli_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            result = _run_cli("--frequency", "quarterly", "--countries", "CN", "--targets", "GDP",
                              "--require-real", "--out", str(out),
                              "--generated-at", "2026-11-18T00:00:00Z")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((out / "report.json").exists())
            self.assertIn("NOT_AVAILABLE", result.stderr)

    # -- models -------------------------------------------------------------------------
    def test_rw_forecast_is_last_value_and_target_advances(self):
        series = cf.synthetic_series("GB", "CPI", "Q")
        origin = series.observations[50].release_date
        sliced = series.as_of(origin)
        expected = float(sliced.values()[-1])
        previous_target = None
        for horizon in cf.HORIZONS:
            result = cf.forecast_one(series, horizon, origin, model="RW")
            self.assertAlmostEqual(result.point, expected, places=12)
            self.assertGreater(result.target_period, result.origin_period)
            self.assertLessEqual(result.lo80, result.point)
            self.assertGreaterEqual(result.hi80, result.point)
            if previous_target is not None:
                self.assertEqual(result.target_period, previous_target + 1)
            previous_target = result.target_period

    def test_ar_bic_is_deterministic_and_stable(self):
        series = cf.synthetic_series("EA", "GDP", "Q")
        origin = series.observations[60].release_date
        first = cf.forecast_one(series, 2, origin, model="AR_BIC")
        second = cf.forecast_one(series, 2, origin, model="AR_BIC")
        self.assertEqual(first.point, second.point)
        self.assertEqual((first.lo80, first.hi80), (second.lo80, second.hi80))
        self.assertIsNotNone(first.order)
        self.assertLessEqual(first.order, 4)
        self.assertLess(abs(first.point), 50.0)

    def test_panel_requires_two_countries(self):
        cn = cf.synthetic_series("CN", "GDP", "Q")
        ea = cf.synthetic_series("EA", "GDP", "Q")
        origin = cn.observations[40].release_date
        with self.assertRaises(cf.InsufficientHistory):
            cf.forecast_one(cn, 1, origin, panel={"CN": cn}, model="PANEL_AR1")
        result = cf.forecast_one(cn, 1, origin, panel={"CN": cn, "EA": ea}, model="PANEL_AR1")
        self.assertTrue(result.point == result.point)

    def test_bridge_requires_other_target(self):
        cn_gdp = cf.synthetic_series("CN", "GDP", "Q")
        cn_cpi = cf.synthetic_series("CN", "CPI", "Q")
        origin = cn_gdp.observations[40].release_date
        with self.assertRaises(cf.InsufficientHistory):
            cf.forecast_one(cn_gdp, 1, origin, other=None, model="BRIDGE")
        result = cf.forecast_one(cn_gdp, 1, origin, other=cn_cpi, model="BRIDGE")
        self.assertTrue(result.point == result.point)

    def test_horizon_validation(self):
        series = cf.synthetic_series("CN", "CPI", "Q")
        origin = series.observations[40].release_date
        for horizon in (0, 5, -1):
            with self.assertRaises(ValueError):
                cf.forecast_one(series, horizon, origin, model="RW")
        with self.assertRaises(ValueError):
            cf.build_report(horizons=(0,), generated_at="2026-11-18T00:00:00Z")

    # -- OOS ----------------------------------------------------------------------------
    def test_walk_forward_has_no_future_origin(self):
        series = cf.synthetic_series("CN", "GDP", "Q")
        rows = cf.walk_forward(series, "AR_BIC", 1)
        self.assertGreater(len(rows), 20)
        previous = None
        for row in rows:
            self.assertGreater(row["target_period_index"], row["origin_period_index"])
            self.assertEqual(row["target_period_index"], row["origin_period_index"] + 1)
            self.assertTrue(abs(row["error"]) < 1e6)
            if previous is not None:
                self.assertGreater(row["origin_period_index"], previous)
            previous = row["origin_period_index"]

    def test_oos_metrics_and_relative_rmse(self):
        rows = [{"origin_period": f"2020Q{i}", "origin_period_index": 2020 * 4 + i,
                 "error": float(i - 1), "forecast": 1.0, "actual": 1.0} for i in range(1, 5)]
        metrics = cf.oos_metrics(rows, "Q")
        self.assertEqual(metrics["n_origins"], 4)
        self.assertAlmostEqual(metrics["bias"], 1.5)
        self.assertGreater(metrics["rmse"], 0)
        self.assertEqual(metrics["status"], "INSUFFICIENT_OOS")
        self.assertEqual(cf.oos_metrics([], "Q")["status"], cf.STATUS_NOT_AVAILABLE)

    def test_report_oos_contains_rw_and_relative(self):
        rows = [row for row in self.quarterly["oos"] if row["model"] == "RW"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["rel_rmse_vs_rw"], 1.0)
            self.assertEqual(row["status"], "OK")
        ar_rows = [row for row in self.quarterly["oos"] if row["model"] == "AR_BIC"]
        self.assertTrue(ar_rows)
        for row in ar_rows:
            self.assertIsNotNone(row["rel_rmse_vs_rw"])
            self.assertTrue(abs(row["rel_rmse_vs_rw"] - 1.0) < 1.0)

    # -- report integrity ---------------------------------------------------------------
    def test_report_is_aggregates_only(self):
        text = json.dumps(self.quarterly, ensure_ascii=False)
        self.assertIsNone(re.search(r'"(observations|values|history|raw_values|series_values)"\s*:', text))
        self.assertEqual(_numeric_arrays(self.quarterly), [])
        self.assertEqual(self.quarterly["status"], cf.STATUS_RESEARCH_ONLY)
        self.assertFalse(self.quarterly["admission"])
        self.assertTrue(self.quarterly["checks"]["no_raw_series_in_report"])
        for key, value in self.quarterly["checks"].items():
            if key != "models":
                self.assertIs(value, True, key)

    def test_report_digest_is_deterministic(self):
        first = cf.build_report(freq="Q", generated_at="2026-11-18T00:00:00Z")
        second = cf.build_report(freq="Q", generated_at="2027-01-01T00:00:00Z")
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])

    def test_quarterly_report_is_honest_about_zero_real_countries(self):
        summary = self.quarterly["summary"]
        self.assertEqual(summary["real_country_target_pairs"], 0)
        self.assertEqual(summary["real_quarterly_pairs"], 0)
        self.assertEqual(summary["countries_with_real_data"], 0)
        self.assertEqual(summary["countries_synthetic"], 3)
        self.assertEqual(len(self.quarterly["data_gaps"]), 6)
        for country in self.quarterly["countries"]:
            self.assertEqual(country["status"], cf.STATUS_SYNTHETIC)
            self.assertFalse(country["real"])
        forecast = self.quarterly["forecasts"][0]
        self.assertEqual(forecast["data_status"], cf.DATA_CLASS_SYNTHETIC)
        self.assertFalse(forecast["real"])

    def test_annual_report_uses_real_wdi_when_present(self):
        if not (WB_GDP.exists() and WB_CPI.exists()):
            self.skipTest("WDI cache is not present")
        summary = self.annual["summary"]
        self.assertGreaterEqual(summary["real_country_target_pairs"], 4)
        by_country = {entry["country"]: entry for entry in self.annual["countries"]}
        self.assertEqual(by_country["CN"]["status"], cf.STATUS_REAL)
        self.assertEqual(by_country["GB"]["status"], cf.STATUS_REAL)
        for country in ("CN", "GB"):
            for target in cf.TARGET_IDS:
                entry = by_country[country]["targets"][target]
                self.assertEqual(entry["source_id"], "WORLD_BANK")
                self.assertEqual(entry["pit_status"], cf.PIT_FINAL_VINTAGE)
                self.assertGreaterEqual(entry["n_observations"], 25)
        ea = by_country["EA"]
        self.assertEqual(ea["status"], cf.STATUS_SYNTHETIC)
        self.assertTrue(any(gap["country"] == "EA" for gap in self.annual["data_gaps"]))

    def test_annual_real_oos_is_present(self):
        if not WB_GDP.exists():
            self.skipTest("WDI cache is not present")
        rows = [row for row in self.annual["oos"]
                if row["country"] == "CN" and row["target"] == "GDP"
                and row["model"] == "AR_BIC" and row["horizon"] == 1]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "OK")
        self.assertGreaterEqual(row["n_origins"], 8)
        self.assertIsNotNone(row["rmse"])
        self.assertIsNotNone(row["rel_rmse_vs_rw"])
        self.assertTrue(row["real"])

    def test_report_render_markdown(self):
        markdown = cf.render_markdown(self.quarterly)
        self.assertIn("# Страновой прогноз", markdown)
        self.assertIn("RESEARCH_ONLY", markdown)
        self.assertIn("синтетическ", markdown.lower())
        self.assertIn(self.quarterly["content_sha256"], markdown)
        self.assertNotIn("n_observations", markdown)
        self.assertNotRegex(markdown, r'"(observations|values|history|raw_values)"\s*:')

    # -- CLI ----------------------------------------------------------------------------
    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            result = _run_cli("--frequency", "quarterly", "--countries", "CN",
                              "--targets", "GDP", "--horizons", "1,2",
                              "--models", "RW,AR_BIC", "--out", str(out),
                              "--generated-at", "2026-11-18T00:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], cf.STATUS_RESEARCH_ONLY)
            self.assertEqual(payload["summary"]["real_country_target_pairs"], 0)
            markdown = (out / "report.md").read_text(encoding="utf-8")
            self.assertIn("RESEARCH_ONLY", markdown)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], cf.STATUS_RESEARCH_ONLY)
            self.assertEqual(summary["content_sha256"], payload["content_sha256"])

    def test_cli_json_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            result = _run_cli("--frequency", "quarterly", "--countries", "CN",
                              "--targets", "CPI", "--horizons", "1", "--json",
                              "--out", str(Path(temp) / "out"),
                              "--generated-at", "2026-11-18T00:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], "amfe.country_forecast.v1")
            self.assertGreaterEqual(payload["summary"]["forecasts"], 2)

    def test_cli_require_real_annual_succeeds_for_real_country(self):
        if not (WB_GDP.exists() and WB_CPI.exists()):
            self.skipTest("WDI cache is not present")
        with tempfile.TemporaryDirectory() as temp:
            result = _run_cli("--frequency", "annual", "--countries", "CN",
                              "--targets", "GDP,CPI", "--require-real",
                              "--out", str(Path(temp) / "out"),
                              "--generated-at", "2026-11-18T00:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["real_country_target_pairs"], 2)

    def test_cli_refuses_to_write_under_data(self):
        target = ROOT / "data" / "__country_forecast_test__"
        self.assertFalse(target.exists())
        result = _run_cli("--frequency", "quarterly", "--countries", "CN",
                          "--targets", "GDP", "--horizons", "1",
                          "--out", str(target),
                          "--generated-at", "2026-11-18T00:00:00Z")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())

    def test_cli_rejects_unknown_country_and_bad_horizon(self):
        unknown = _run_cli("--frequency", "quarterly", "--countries", "US", "--out", "build/__tmp_cf")
        self.assertEqual(unknown.returncode, 2)
        bad = _run_cli("--frequency", "quarterly", "--countries", "CN", "--horizons", "5",
                       "--out", "build/__tmp_cf")
        self.assertEqual(bad.returncode, 2)


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "country_forecast.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


if __name__ == "__main__":
    unittest.main()
