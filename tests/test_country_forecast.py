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
        # Isolated empty provider roots: the repository cache and any user CSV must not
        # leak into the "no real data" baseline used by the pipeline tests.
        cls._scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        scratch = Path(cls._scratch.name)
        cls.empty_external = scratch / "external"
        cls.empty_cache = scratch / "cache"
        cls.quarterly = cf.build_report(
            freq="Q", generated_at="2026-11-18T00:00:00Z", allow_synthetic=True,
            external_root=cls.empty_external, cache_root=cls.empty_cache,
        )
        cls.annual = cf.build_report(
            freq="A", generated_at="2026-11-18T00:00:00Z", allow_synthetic=True,
            external_root=cls.empty_external, cache_root=cls.empty_cache,
        )

    @classmethod
    def tearDownClass(cls):
        cls._scratch.cleanup()

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
                cf.load_series(country, "GDP", "Q", allow_synthetic=False,
                               external_root=self.empty_external, cache_root=self.empty_cache)
        _series, probes = cf.load_series("CN", "GDP", "Q", allow_synthetic=True,
                                         external_root=self.empty_external, cache_root=self.empty_cache)
        statuses = {probe.provider: probe.status for probe in probes}
        self.assertEqual(statuses["COUNTRY_CACHE"], "NOT_AVAILABLE")
        self.assertEqual(statuses["WORLD_BANK_WDI"], "FREQUENCY_MISMATCH")
        self.assertEqual(statuses["SYNTHETIC_FIXTURE"], "AVAILABLE")
        cache_probe = next(probe for probe in probes if probe.provider == "COUNTRY_CACHE")
        self.assertIn("fetch_country_data.py", cache_probe.detail)

    def test_require_real_cli_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            empty = Path(temp) / "empty"
            result = _run_cli("--frequency", "quarterly", "--countries", "CN", "--targets", "GDP",
                              "--require-real", "--out", str(out),
                              "--data-root", str(empty / "external"),
                              "--cache-root", str(empty / "cache"),
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
        with tempfile.TemporaryDirectory() as temp:
            empty = Path(temp) / "empty"
            first = cf.build_report(freq="Q", generated_at="2026-11-18T00:00:00Z",
                                    external_root=empty / "external", cache_root=empty / "cache")
            second = cf.build_report(freq="Q", generated_at="2027-01-01T00:00:00Z",
                                     external_root=empty / "external", cache_root=empty / "cache")
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
            empty = Path(temp) / "empty"
            result = _run_cli("--frequency", "quarterly", "--countries", "CN",
                              "--targets", "GDP", "--horizons", "1,2",
                              "--models", "RW,AR_BIC", "--out", str(out),
                              "--data-root", str(empty / "external"),
                              "--cache-root", str(empty / "cache"),
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


def _run_fetch(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "fetch_country_data.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


CACHE_KEYS = ("EA_GDP_Q.json", "EA_CPI_Q.json", "CN_GDP_Q.json",
              "CN_CPI_Q.json", "GB_GDP_Q.json", "GB_CPI_Q.json")
CACHE_AVAILABLE = all((cf.DEFAULT_COUNTRY_CACHE / name).exists() for name in CACHE_KEYS)


def _cache_document(**overrides) -> dict:
    """A minimal but valid ``amfe.country_cache.v1`` document (24 quarters)."""
    periods = [f"{2015 + index // 4}Q{index % 4 + 1}" for index in range(24)]
    values = [1.0 + 0.1 * index for index in range(24)]
    document = {
        "schema": cf.COUNTRY_CACHE_SCHEMA,
        "provider": "EUROSTAT",
        "source_id": "EUROSTAT",
        "country": "EA",
        "target": "GDP",
        "frequency": "Q",
        "series_id": "test|series",
        "title": "test series",
        "unit": "percent, y/y",
        "transform": "as published",
        "source_url": "https://example.invalid/test",
        "retrieved_at": "2026-11-18",
        "licence": "test licence",
        "pit": {"status": cf.PIT_FINAL_VINTAGE, "reason": "test fixture"},
        "release_dates": None,
        "periods": periods,
        "values": values,
        "payload_sha256": "0" * 64,
    }
    document.update(overrides)
    return document


def _write_cache(root: Path, document: dict, name: str = "EA_GDP_Q.json") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class CountryCacheAdapterTests(unittest.TestCase):
    """Adapter behaviour for the public quarterly cache (offline, temp files only)."""

    def test_cache_loader_marks_final_vintage_and_real(self):
        with tempfile.TemporaryDirectory() as temp:
            _write_cache(Path(temp), _cache_document())
            series, probe = cf.load_country_cache(temp, "EA", "GDP", "Q")
        self.assertIsNotNone(series)
        self.assertEqual(probe.status, "AVAILABLE")
        self.assertEqual(probe.provider, "EUROSTAT")
        self.assertEqual(series.source_class, cf.DATA_CLASS_REAL)
        self.assertFalse(series.is_synthetic)
        self.assertEqual(series.pit_status, cf.PIT_FINAL_VINTAGE)
        self.assertEqual(series.source_id, "EUROSTAT")
        self.assertEqual(series.n_obs, 24)
        self.assertTrue(any("FINAL_VINTAGE_NO_PIT" in note for note in series.notes))
        self.assertTrue(series.licence)

    def test_cache_with_release_dates_is_real_pit(self):
        periods = [f"2015Q{quarter}" for quarter in range(1, 5)]
        releases = [(cf.period_end(2015 * 4 + quarter - 1, "Q") + timedelta(days=45)).isoformat()
                    for quarter in range(1, 5)]
        with tempfile.TemporaryDirectory() as temp:
            _write_cache(Path(temp), _cache_document(periods=periods, values=[1.0, 1.1, 1.2, 1.3],
                                                     release_dates=releases))
            series, _probe = cf.load_country_cache(temp, "EA", "GDP", "Q")
        self.assertEqual(series.pit_status, cf.PIT_REAL)
        cut = cf.period_end(2015 * 4 + 1, "Q") + timedelta(days=46)
        self.assertEqual(series.as_of(cut).n_obs, 2)
        series.as_of(cut).assert_pit_clean(cut)
        with self.assertRaises(cf.PITViolationError):
            series.assert_pit_clean(cut)

    def test_cache_release_before_period_end_is_rejected(self):
        periods = ["2015Q1"]
        with tempfile.TemporaryDirectory() as temp:
            _write_cache(Path(temp), _cache_document(periods=periods, values=[1.0],
                                                     release_dates=["2015-02-01"]))
            with self.assertRaises(cf.PITViolationError):
                cf.load_country_cache(temp, "EA", "GDP", "Q")

    def test_cache_with_bad_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            _write_cache(Path(temp), _cache_document(schema="amfe.country_cache.v0"))
            with self.assertRaises(cf.CountryForecastError):
                cf.load_country_cache(temp, "EA", "GDP", "Q")

    def test_cache_with_mismatched_country_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            _write_cache(Path(temp), _cache_document(country="CN"))
            with self.assertRaises(cf.CountryForecastError):
                cf.load_country_cache(temp, "EA", "GDP", "Q")

    def test_missing_cache_probe_points_to_fetch_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            series, probe = cf.load_country_cache(Path(temp) / "absent", "GB", "CPI", "Q")
            self.assertIsNone(series)
            self.assertEqual(probe.status, "NOT_AVAILABLE")
            self.assertIn("fetch_country_data.py", probe.detail)
            with self.assertRaises(cf.DataNotAvailable) as raised:
                cf.load_series("GB", "CPI", "Q", allow_synthetic=False,
                               external_root=Path(temp) / "external", cache_root=Path(temp) / "absent")
        self.assertIn("NOT_AVAILABLE", str(raised.exception))

    def test_local_csv_has_priority_over_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache"
            _write_cache(cache, _cache_document())
            external = root / "external" / "country"
            external.mkdir(parents=True)
            rows = ["period,value,release_date"]
            for index in range(24):
                period = 2015 * 4 + index
                release = cf.period_end(period, "Q") + timedelta(days=45)
                rows.append(f"{cf.period_label(period, 'Q')},2.5,{release.isoformat()}")
            (external / "EA_GDP_Q.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            series, probes = cf.load_series("EA", "GDP", "Q", allow_synthetic=False,
                                            external_root=root / "external", cache_root=cache)
        self.assertEqual(series.source_id, "LOCAL_PIT_CSV")
        self.assertEqual(series.pit_status, cf.PIT_REAL)
        self.assertEqual(probes[0].provider, "LOCAL_PIT_CSV")

    def test_empty_cache_root_keeps_synthetic_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            series, probes = cf.load_series("EA", "GDP", "Q", allow_synthetic=True,
                                            external_root=Path(temp) / "external",
                                            cache_root=Path(temp) / "cache")
        self.assertTrue(series.is_synthetic)
        self.assertIn("COUNTRY_CACHE", {probe.provider for probe in probes})


class CountryCacheRealDataTests(unittest.TestCase):
    """Checks that only run when the git-ignored real cache has been populated."""

    @classmethod
    def setUpClass(cls):
        if not CACHE_AVAILABLE:
            raise unittest.SkipTest("real quarterly cache is not present; run tools/fetch_country_data.py")
        cls._scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.empty = Path(cls._scratch.name) / "empty"
        # One report build is expensive (6 real series, 24 OOS cells, up to 262 origins).
        cls.report = cf.build_report(
            freq="Q", generated_at="2026-11-18T00:00:00Z", allow_synthetic=False,
            external_root=cls.empty / "external", cache_root=cf.DEFAULT_COUNTRY_CACHE,
        )

    @classmethod
    def tearDownClass(cls):
        cls._scratch.cleanup()

    def test_real_cache_report_has_six_real_quarterly_pairs(self):
        summary = self.report["summary"]
        self.assertEqual(summary["real_quarterly_pairs"], 6)
        self.assertEqual(summary["real_country_target_pairs"], 6)
        self.assertEqual(summary["synthetic_country_target_pairs"], 0)
        self.assertEqual(summary["countries_with_real_data"], 3)
        self.assertEqual(self.report["data_gaps"], [])
        self.assertEqual(summary["pit_status_counts"], {cf.PIT_FINAL_VINTAGE: 6})
        providers = {source["source_id"] for source in self.report["sources"]}
        self.assertEqual(providers, {"EUROSTAT", "OECD", "ONS"})
        for source in self.report["sources"]:
            self.assertTrue(source["licence"])
            self.assertTrue(source["used_for"])

    def test_real_cache_series_are_official_aggregates(self):
        by_country = {entry["country"]: entry for entry in self.report["countries"]}
        expected = {
            ("EA", "GDP"): "EUROSTAT", ("EA", "CPI"): "EUROSTAT",
            ("CN", "GDP"): "OECD", ("CN", "CPI"): "OECD",
            ("GB", "GDP"): "ONS", ("GB", "CPI"): "ONS",
        }
        for (country, target), provider in expected.items():
            entry = by_country[country]["targets"][target]
            self.assertEqual(entry["source_id"], provider)
            self.assertEqual(entry["status"], cf.STATUS_REAL)
            self.assertEqual(entry["pit_status"], cf.PIT_FINAL_VINTAGE)
            self.assertGreaterEqual(entry["n_observations"], 60)
            self.assertTrue(entry["last_period"])

    def test_real_cache_forecasts_are_real_and_pit_safe(self):
        self.assertTrue(self.report["forecasts"])
        for row in self.report["forecasts"]:
            self.assertTrue(row["real"])
            self.assertEqual(row["data_status"], cf.DATA_CLASS_REAL)
            self.assertGreater(row["target_period_index"], row["origin_period_index"])
            self.assertLessEqual(row["interval80"][0], row["point"])
            self.assertGreaterEqual(row["interval80"][1], row["point"])

    def test_real_cache_oos_covers_every_pair_and_horizon(self):
        rows = {(row["country"], row["target"], row["horizon"], row["model"]): row
                for row in self.report["oos"]}
        for country in cf.COUNTRY_IDS:
            for target in cf.TARGET_IDS:
                for horizon in cf.HORIZONS:
                    rw = rows[(country, target, horizon, "RW")]
                    self.assertEqual(rw["status"], "OK")
                    self.assertGreaterEqual(rw["n_origins"], 60)
                    self.assertAlmostEqual(rw["rel_rmse_vs_rw"], 1.0, places=12)
                    for model in ("AR_BIC", "PANEL_AR1", "BRIDGE"):
                        self.assertIn((country, target, horizon, model), rows)

    def test_real_cache_markdown_is_honest(self):
        markdown = cf.render_markdown(self.report)
        self.assertIn("FINAL_VINTAGE_NO_PIT", markdown)
        self.assertIn("Eurostat", markdown)
        self.assertIn("RESEARCH_ONLY", markdown)
        self.assertNotRegex(markdown, r'"(observations|values|history|raw_values)"\s*:')

    def test_real_cache_digest_is_deterministic(self):
        clone = json.loads(json.dumps(self.report))
        clone["generated_at"] = "2027-01-01T00:00:00Z"
        self.assertEqual(cf.content_digest(clone), self.report["content_sha256"])
        self.assertEqual(clone["content_sha256"], self.report["content_sha256"])

    def test_real_cache_cli_require_real_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            empty = Path(temp) / "empty"
            result = _run_cli("--frequency", "quarterly", "--countries", "EA,CN,GB",
                              "--targets", "GDP,CPI", "--horizons", "1",
                              "--require-real", "--out", str(Path(temp) / "out"),
                              "--data-root", str(empty / "external"),
                              "--cache-root", str(cf.DEFAULT_COUNTRY_CACHE),
                              "--generated-at", "2026-11-18T00:00:00Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
        self.assertEqual(summary["real_country_target_pairs"], 6)
        self.assertEqual(summary["synthetic_country_target_pairs"], 0)


class FetchCountryDataToolTests(unittest.TestCase):
    """Offline checks of the fetch tool (no network in this class)."""

    @classmethod
    def setUpClass(cls):
        if "fetch_country_data" not in sys.modules:
            sys.path.insert(0, str(ROOT / "tools"))
        import fetch_country_data  # noqa: E402
        cls.fcd = fetch_country_data

    def test_catalogue_covers_six_official_series(self):
        catalogue = self.fcd.CATALOGUE
        self.assertEqual(len(catalogue), 6)
        pairs = {(spec.country, spec.target) for spec in catalogue}
        self.assertEqual(pairs, {(c, t) for c in cf.COUNTRY_IDS for t in cf.TARGET_IDS})
        self.assertEqual({spec.provider for spec in catalogue}, {"EUROSTAT", "OECD", "ONS"})
        for spec in catalogue:
            self.assertTrue(spec.url.startswith("https://"))
            self.assertEqual(spec.unit, "percent, y/y")
            self.assertIn(spec.provider, self.fcd.LICENCES)
            self.assertTrue(spec.transform)

    def test_catalogue_cli_lists_all_series(self):
        result = _run_fetch("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 6)
        self.assertTrue(any(line.startswith("EA:GDP") for line in lines))
        self.assertTrue(any("api.db.nomics.world" in line for line in lines))

    def test_monthly_to_quarterly_and_yoy(self):
        months = []
        values = []
        for index in range(24):  # 2020-01 .. 2021-12
            year = 2020 + index // 12
            month = index % 12 + 1
            months.append(f"{year}-{month:02d}")
            values.append(100.0 + index * 1.0)  # linear index
        quarters, quarterly = self.fcd.aggregate_monthly_to_quarterly(months, values)
        self.assertEqual(quarters[0], "2020Q1")
        self.assertEqual(quarters[-1], "2021Q4")
        self.assertEqual(len(quarters), 8)
        first_mean = (100.0 + 101.0 + 102.0) / 3.0
        self.assertAlmostEqual(quarterly[0], first_mean, places=9)
        yoy_periods, yoy_values = self.fcd.yoy_from_quarterly_index(quarters, quarterly)
        self.assertEqual(yoy_periods[0], "2021Q1")  # y/y needs t-4
        self.assertEqual(len(yoy_periods), 4)
        # 2021Q1 index = mean(112, 113, 114) = 113; 2020Q1 index = mean(100, 101, 102) = 101.
        expected = (113.0 / 101.0 - 1.0) * 100.0
        self.assertAlmostEqual(yoy_values[0], expected, places=9)

    def test_incomplete_quarter_is_dropped(self):
        quarters, values = self.fcd.aggregate_monthly_to_quarterly(
            ["2020-01", "2020-02", "2020-04", "2020-05", "2020-06"], [1.0, 2.0, 4.0, 5.0, 6.0]
        )
        self.assertEqual(quarters, ["2020Q2"])
        self.assertAlmostEqual(values[0], 5.0)

    def test_eurostat_jsonstat_parser(self):
        document = {
            "dimension": {"time": {"category": {"index": {"2020-Q1": 0, "2020-Q2": 1}}}},
            "value": {"0": 1.5, "1": 2.5},
        }
        periods, values = self.fcd.parse_eurostat_jsonstat(document)
        self.assertEqual(periods, ["2020-Q1", "2020-Q2"])
        self.assertEqual(values, [1.5, 2.5])

    def test_normalise_rejects_empty_and_short_series(self):
        spec = next(spec for spec in self.fcd.CATALOGUE if spec.country == "GB")
        with self.assertRaises(self.fcd.FetchError):
            self.fcd.normalise(spec, ["2020-Q1"], [1.0])


if __name__ == "__main__":
    unittest.main()
