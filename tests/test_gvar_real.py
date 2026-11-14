"""GVAR on World Bank data (tools/gvar_real.py): parsing, panel, weights, OOS, fail-closed, determinism.

All tests are offline: the World Bank download is replaced by a fixture payload (or a fake
``download`` callable), so the suite never touches the network.  The synthetic panel mirrors the
real series shape (two variables per country, annual, balanced).
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import gvar_real as gr  # noqa: E402
from amfe.world.gvar import GVAR  # noqa: E402


# --------------------------------------------------------------------------------------- fixtures
def wb_payload(rows):
    """Minimal World Bank indicator payload: [meta, rows]."""
    return [{"page": 1, "pages": 1, "per_page": 50, "total": len(rows),
             "sourceid": "2", "lastupdated": "2026-11-11"}, rows]


def wb_row(code, year, value):
    return {"indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
            "country": {"id": code[:2], "value": code},
            "countryiso3code": code, "date": str(year), "value": value,
            "unit": "", "obs_status": "", "decimal": 1}


COUNTRIES = ("USA", "CHN", "DEU", "FRA", "ITA", "JPN")
YEARS = [1995 + index for index in range(30)]


def synthetic_panel(countries=COUNTRIES, years=YEARS) -> dict:
    panel = {"gdp_growth": {}, "inflation": {}, "gdp_current": {}, "trade_gdp": {},
             "population": {}}
    for index, code in enumerate(countries):
        panel["gdp_growth"][code] = {}
        panel["inflation"][code] = {}
        panel["gdp_current"][code] = {}
        panel["trade_gdp"][code] = {}
        panel["population"][code] = {}
        for step, year in enumerate(years):
            panel["gdp_growth"][code][year] = 2.0 + 1.5 * math.sin(step / 3.0 + index) \
                + 0.05 * index
            panel["inflation"][code][year] = 2.0 + 0.4 * math.cos(step / 4.0 + index) \
                + 0.1 * index
            panel["gdp_current"][code][year] = 1_000.0 * (index + 1) * (1.0 + 0.02 * step)
            panel["trade_gdp"][code][year] = 40.0 + index
            panel["population"][code][year] = 50.0 + index
    return panel


def fake_download_for(panel_like: dict):
    """Return a download callable serving the fixture payload for every indicator."""
    indicators = {name: code for name, code in gr.INDICATORS.items()}

    def download(url: str) -> dict:
        for name, code in indicators.items():
            if code in url:
                rows = [wb_row(country, year, value)
                        for country, series in panel_like[name].items()
                        for year, value in series.items()]
                return wb_payload(rows)
        raise AssertionError(f"unexpected URL {url}")

    return download


def synthetic_dataset(panel: dict, countries=COUNTRIES, years=YEARS) -> dict:
    weights, _ = gr.gdp_weights(panel, countries, years)
    return gr.build_dataset(panel, countries, years, weights)


# ---------------------------------------------------------------------------------- WB parsing
class WorldBankParsing(unittest.TestCase):
    def test_url_contains_countries_indicator_and_dates(self):
        url = gr.wb_url("NY.GDP.MKTP.CD", ["USA", "DEU"], start=1995, end=2024)
        self.assertIn("country/USA;DEU", url)
        self.assertIn("NY.GDP.MKTP.CD", url)
        self.assertIn("date=1995:2024", url)
        self.assertIn("format=json", url)

    def test_parse_payload_maps_iso3_and_years(self):
        payload = wb_payload([wb_row("USA", 2020, 1.5), wb_row("USA", 2021, None),
                              wb_row("DEU", 2020, -3.2)])
        parsed = gr.parse_payload(payload, countries=["USA", "DEU"])
        self.assertEqual(parsed["USA"][2020], 1.5)
        self.assertIsNone(parsed["USA"][2021])
        self.assertEqual(parsed["DEU"][2020], -3.2)

    def test_parse_payload_skips_aggregates_and_unknown_countries(self):
        aggregate = {"indicator": {"id": "X"}, "country": {"id": "1A", "value": "World"},
                     "countryiso3code": "", "date": "2020", "value": 99.0}
        payload = wb_payload([aggregate, wb_row("USA", 2020, 1.0), wb_row("FRA", 2020, 2.0)])
        parsed = gr.parse_payload(payload, countries=["USA"])
        self.assertEqual(list(parsed), ["USA"])
        self.assertEqual(parsed["USA"][2020], 1.0)
        unfiltered = gr.parse_payload(payload)
        self.assertNotIn("", unfiltered)
        self.assertIn("FRA", unfiltered)

    def test_parse_payload_rejects_non_wb_shape(self):
        with self.assertRaises(gr.GVARRealError):
            gr.parse_payload({"not": "the api"})
        with self.assertRaises(gr.GVARRealError):
            gr.parse_payload([{"page": 1}, None])

    def test_fetch_indicator_uses_the_cache(self):
        calls = {"n": 0}
        payload = wb_payload([wb_row("USA", 2020, 1.0)])

        def download(url):
            calls["n"] += 1
            return payload

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            first = gr.fetch_indicator("NY.GDP.MKTP.CD", ["USA"], cache_dir=cache_dir,
                                       download=download)
            second = gr.fetch_indicator("NY.GDP.MKTP.CD", ["USA"], cache_dir=cache_dir,
                                        download=download)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first["payload"], second["payload"])
        self.assertIn("api.worldbank.org", first["source"])
        self.assertIn("retrieved_at", first)

    def test_load_indicators_with_injected_download(self):
        panel = synthetic_panel()
        with tempfile.TemporaryDirectory() as directory:
            loaded = gr.load_indicators(COUNTRIES, cache_dir=Path(directory),
                                        download=fake_download_for(panel), start=1995, end=2024)
        self.assertEqual(set(loaded), set(gr.INDICATORS))
        self.assertEqual(sorted(loaded["gdp_growth"]), sorted(COUNTRIES))
        self.assertEqual(loaded["gdp_growth"]["USA"][2020], panel["gdp_growth"]["USA"][2020])


# ------------------------------------------------------------------------------------- sample
class SampleSelection(unittest.TestCase):
    def test_complete_window_is_selected(self):
        panel = synthetic_panel()
        sample = gr.choose_sample(panel, COUNTRIES, min_countries=5)
        self.assertEqual(sample["countries"], list(COUNTRIES))
        self.assertEqual((sample["first_year"], sample["last_year"]), (1995, 2024))
        self.assertEqual(sample["n_years"], 30)

    def test_incomplete_country_is_dropped_and_few_countries_fail(self):
        panel = synthetic_panel()
        panel["gdp_growth"]["CHN"][2010] = None
        # the longest window that still contains all six countries excludes 2010
        sample = gr.choose_sample(panel, COUNTRIES, min_countries=6)
        self.assertIn("CHN", sample["countries"])
        self.assertNotIn(2010, range(sample["first_year"], sample["last_year"] + 1))
        # with a country that has no growth data at all the six-country window is impossible
        panel = synthetic_panel()
        for year in YEARS:
            panel["gdp_growth"]["CHN"][year] = None
        with self.assertRaises(gr.GVARRealError):
            gr.choose_sample(panel, COUNTRIES, min_countries=6)

    def test_dataset_is_balanced_and_refuses_gaps(self):
        panel = synthetic_panel()
        dataset = synthetic_dataset(panel)
        self.assertEqual(len(dataset["countries"]), 6)
        for country in COUNTRIES:
            self.assertEqual(len(dataset["series"][country]["g"]), 30)
            self.assertEqual(len(dataset["series"][country]["pi"]), 30)
        panel["inflation"]["ITA"][2001] = None
        with self.assertRaises(gr.GVARRealError):
            synthetic_dataset(panel)


# ------------------------------------------------------------------------------------ weights
class Weights(unittest.TestCase):
    def test_gdp_share_weights_are_unit_rows_without_self(self):
        panel = synthetic_panel()
        weights, source = gr.gdp_weights(panel, COUNTRIES, YEARS)
        self.assertEqual(source["method"], "GDP_SHARE_PROXY")
        for country in COUNTRIES:
            row = weights[country]
            self.assertAlmostEqual(math.fsum(row.values()), 1.0, places=12)
            self.assertNotIn(country, row)
            self.assertTrue(all(value >= 0.0 for value in row.values()))
        # the largest economy carries the largest weight from every other country
        self.assertEqual(max(weights["USA"], key=weights["USA"].get), "JPN")

    def test_mrio_weights_are_read_when_present_and_are_read_only(self):
        panel = synthetic_panel()
        countries = ("USA", "CHN", "DEU")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps({"trade_weights": {
                "USA": {"CHN": 0.7, "DEU": 0.3},
                "CHN": {"USA": 0.6, "DEU": 0.4},
                "DEU": {"USA": 0.5, "CHN": 0.5},
            }}), encoding="utf-8")
            before = path.read_bytes()
            weights, source = gr.build_weights(panel, countries, YEARS, mrio_paths=(path,))
            after = path.read_bytes()
        self.assertEqual(source["method"], "MRIO_TRADE_WEIGHTS")
        self.assertEqual(before, after)
        self.assertAlmostEqual(weights["USA"]["CHN"], 0.7, places=12)
        # a missing file falls back to the GDP proxy
        weights, source = gr.build_weights(panel, countries, YEARS,
                                           mrio_paths=(Path("/nonexistent/dataset.json"),))
        self.assertEqual(source["method"], "GDP_SHARE_PROXY")
        self.assertAlmostEqual(math.fsum(weights["USA"].values()), 1.0, places=12)


# ---------------------------------------------------------------------------------------- model
class ModelAndOos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = synthetic_panel()
        cls.dataset = synthetic_dataset(cls.panel)

    def test_gvar_fit_and_stability(self):
        model = GVAR(self.dataset, lags=1)
        self.assertEqual(model.variables, ["g", "pi"])
        self.assertEqual(model.n, 30)
        self.assertEqual(model.countries, list(COUNTRIES))
        stability = model.stability()
        self.assertIn("stable", stability)
        self.assertGreaterEqual(stability["max_modulus"], 0.0)

    def test_ar1_forecast_on_constant_series(self):
        path = gr.ar1_forecast([5.0, 5.0, 5.0, 5.0, 5.0], 3)
        self.assertEqual(len(path), 3)
        for value in path:
            self.assertAlmostEqual(value, 5.0, places=9)

    def test_aggregate_series_is_a_weighted_mean(self):
        series = {"A": [1.0, 2.0], "B": [3.0, 4.0]}
        aggregated = gr.aggregate_series(series, {"A": 0.75, "B": 0.25})
        self.assertEqual(aggregated.tolist(), [1.5, 2.5])

    def test_rolling_oos_metrics_are_complete_and_deterministic(self):
        groups = {"US": ["USA"], "EA": ["DEU", "FRA", "ITA"], "CN": ["CHN"],
                  "WORLD": list(COUNTRIES)}
        weights, _ = gr.gdp_weights(self.panel, COUNTRIES, YEARS)
        first = gr.rolling_oos(self.panel, COUNTRIES, YEARS, weights, groups, lags_list=(1,),
                               max_h=4, n_origins=10)
        second = gr.rolling_oos(self.panel, COUNTRIES, YEARS, weights, groups, lags_list=(1,),
                                max_h=4, n_origins=10)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(set(first["metrics"]), {"GVAR_p1", "AR1", "RW"})
        for model in first["metrics"]:
            for group in groups:
                for variable in ("g", "pi"):
                    for h in (1, 2, 3, 4):
                        entry = first["metrics"][model][group][variable][h]
                        self.assertGreater(entry["n"], 0)
                        self.assertTrue(math.isfinite(entry["rmse"]))
                        self.assertTrue(math.isfinite(entry["mae"]))
        self.assertGreater(len(first["origins"]), 0)
        self.assertIn("USA", first["per_country"]["GVAR_p1"])

    def test_irf_report_has_all_groups_and_horizons(self):
        model = GVAR(self.dataset, lags=1)
        groups = {"US": ["USA"], "EA": ["DEU", "FRA", "ITA"], "CN": ["CHN"],
                  "WORLD": list(COUNTRIES)}
        report = gr.irf_report(model, groups, horizons=6)
        self.assertEqual(report["shock_country"], "USA")
        for group in groups:
            for variable in ("g", "pi"):
                self.assertEqual(len(report["response"][group][variable]), 7)
        self.assertEqual(report["response"]["US"]["g"][0], 1.0)
        again = gr.irf_report(GVAR(self.dataset, lags=1), groups, horizons=6)
        self.assertEqual(report["response"], again["response"])

    def test_fail_closed_on_short_sample(self):
        short_years = YEARS[:10]
        panel = synthetic_panel(years=short_years)
        weights, _ = gr.gdp_weights(panel, COUNTRIES, short_years)
        groups = {"WORLD": list(COUNTRIES)}
        with self.assertRaises(gr.GVARRealError):
            gr.rolling_oos(panel, COUNTRIES, short_years, weights, groups, max_h=4,
                           n_origins=5)
        # the balanced dataset can be built, but the GVAR itself refuses so few observations
        dataset = gr.build_dataset(panel, COUNTRIES, short_years, weights)
        with self.assertRaises(gr.GVARRealError):
            gr._checked_dataset(dataset, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
