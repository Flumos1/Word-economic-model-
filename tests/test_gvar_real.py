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

    def test_figaro_export_matrix_and_trade_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_figaro_fixture(
                root / "dataset.json", ["US", "DE", "FR", "ROW"], ["A", "B"],
                {("US", "DE"): 4.0, ("US", "FR"): 12.0, ("US", "ROW"): 100.0,
                 ("DE", "US"): 2.0, ("FR", "US"): 3.0})
            exports = gr.figaro_export_matrix(path)
            self.assertEqual(exports["countries"], ["US", "DE", "FR", "ROW"])
            self.assertEqual(exports["industries"], 2)
            self.assertEqual(len(exports["sha256"]), 64)
            us = exports["countries"].index("US")
            de = exports["countries"].index("DE")
            fr = exports["countries"].index("FR")
            self.assertAlmostEqual(exports["exports"][us][de], 4.0 * 4, places=9)   # 2x2 cells
            self.assertAlmostEqual(exports["exports"][us][fr], 12.0 * 4, places=9)
            weights, source = gr.figaro_trade_weights(
                ["USA", "DEU", "FRA"], dataset_path=path, cache_dir=root)
            self.assertEqual(source["method"], "FIGARO_TRADE_WEIGHTS_2019")
            self.assertAlmostEqual(weights["USA"]["DEU"], 0.25, places=12)   # 16 / (16 + 48)
            self.assertAlmostEqual(weights["USA"]["FRA"], 0.75, places=12)   # 48 / 64
            for code, row in weights.items():
                self.assertAlmostEqual(math.fsum(row.values()), 1.0, places=12)
                self.assertNotIn(code, row, "self weight must be zero")
                self.assertNotIn("ROW", row, "ROW must not be a named partner")
            again, _ = gr.figaro_trade_weights(["USA", "DEU", "FRA"], dataset_path=path,
                                              cache_dir=root)
            self.assertEqual(weights, again)
            self.assertTrue((root / "weights_figaro.json").is_file())

    def test_build_weights_figaro_fallback_and_modes(self):
        panel = synthetic_panel()
        countries = ("USA", "DEU", "CHN")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_figaro_fixture(
                root / "dataset.json", ["US", "DE", "ROW"], ["A"],
                {("US", "DE"): 5.0, ("US", "ROW"): 50.0, ("DE", "US"): 2.0})
            weights, source = gr.build_weights(panel, countries, YEARS, mode="figaro",
                                              figaro_path=path, cache_dir=root)
            self.assertEqual(source["method"], "FIGARO_TRADE_WEIGHTS_2019")
            self.assertEqual(source["detail"]["mode"], "figaro")
            self.assertEqual(source["detail"]["mrio_rows"], 2)
            self.assertEqual(source["detail"]["gdp_fallback_rows"], ["CHN"])
            self.assertNotIn("CHN", source["detail"]["covered_iso3"])
            for code in countries:
                self.assertAlmostEqual(math.fsum(weights[code].values()), 1.0, places=12)
                self.assertNotIn(code, weights[code])
            # CHN is not in the table -> its row is the GDP proxy over all three countries
            self.assertIn("USA", weights["CHN"])
            # explicit FIGARO mode without the dataset fails closed
            with self.assertRaises(gr.GVARRealError):
                gr.build_weights(panel, countries, YEARS, mode="figaro",
                                 figaro_path=root / "missing.json", cache_dir=root)
            # auto mode falls back to the GDP proxy
            weights_auto, source_auto = gr.build_weights(
                panel, countries, YEARS, mode="auto", figaro_path=root / "missing.json",
                cache_dir=root)
            self.assertEqual(source_auto["method"], "GDP_SHARE_PROXY")
            self.assertEqual(source_auto["mode"], "gdp")
            self.assertAlmostEqual(math.fsum(weights_auto["USA"].values()), 1.0, places=12)
            # gdp mode never reads the table
            _, source_gdp = gr.build_weights(panel, countries, YEARS, mode="gdp",
                                             figaro_path=path, cache_dir=root)
            self.assertEqual(source_gdp["method"], "GDP_SHARE_PROXY")

    def test_figaro_weights_fit_gvar_and_are_deterministic(self):
        panel = synthetic_panel()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso2 = {gr.ISO2_TO_ISO3[code[:2]]: code for code in COUNTRIES}
            table = ["US", "CN", "DE", "FR", "IT", "JP", "ROW"]
            blocks = {}
            for exporter in table[:-1]:
                for importer in table:
                    if exporter == importer:
                        blocks[(exporter, importer)] = 100.0
                    else:
                        blocks[(exporter, importer)] = 1.0 + 0.1 * table.index(importer)
            path = write_figaro_fixture(root / "dataset.json", table, ["A"], blocks)
            weights, source = gr.build_weights(panel, COUNTRIES, YEARS, mode="figaro",
                                              figaro_path=path, cache_dir=root)
            self.assertEqual(source["detail"]["mrio_rows"], len(COUNTRIES))
            self.assertEqual(source["detail"]["gdp_fallback_rows"], [])
            dataset = gr.build_dataset(panel, COUNTRIES, YEARS, weights)
            model = GVAR(dataset, lags=1)
            self.assertIn("stable", model.stability())
            weights_again, _ = gr.build_weights(panel, COUNTRIES, YEARS, mode="figaro",
                                               figaro_path=path, cache_dir=root)
            self.assertEqual(weights, weights_again)

    def test_compare_runs_marks_better_and_counts_wins(self):
        def make_run(rmse_by_group, better_variable="g", per_country=None):
            metrics = {model: {group: {variable: {h: {"rmse": 0.0, "mae": 0.0, "n": 1}
                                                   for h in (1, 2, 3, 4)}
                                        for variable in ("g", "pi")}
                               for group in ("US", "EA", "CN", "WORLD")}
                       for model in ("GVAR_p1", "GVAR_p2", "AR1", "RW")}
            for group, values in rmse_by_group.items():
                for variable, per_h in values.items():
                    for h, value in per_h.items():
                        metrics["GVAR_p1"][group][variable][h] = {"rmse": value, "mae": value,
                                                                  "n": 3}
            for model in ("AR1", "RW"):
                for group in metrics[model]:
                    for variable in ("g", "pi"):
                        for h in (1, 2, 3, 4):
                            metrics[model][group][variable][h] = {"rmse": 1.0, "mae": 1.0, "n": 3}
            countries = sorted(per_country or {"USA": {}, "CHN": {}})
            per = {model: {code: {variable: {h: {"rmse": 2.0 if model == "GVAR_p1" else 1.0,
                                                 "mae": 1.0, "n": 3}
                                            for h in (1, 2, 3, 4)}
                                  for variable in ("g", "pi")} for code in countries}
                   for model in ("GVAR_p1", "GVAR_p2", "AR1", "RW")}
            for code, values in (per_country or {}).items():
                for variable, per_h in values.items():
                    for h, value in per_h.items():
                        per["GVAR_p1"][code][variable][h]["rmse"] = value
            return {"oos": {"metrics": metrics, "per_country": per}}

        figaro = make_run({"WORLD": {"g": {1: 2.0, 2: 3.0, 3: 3.0, 4: 3.0}}},
                          per_country={"USA": {"g": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}},
                                       "CHN": {"g": {1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0}}})
        gdp = make_run({"WORLD": {"g": {1: 2.5, 2: 2.0, 3: 3.0, 4: 2.5}}},
                       per_country={"USA": {"g": {1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5}},
                                    "CHN": {"g": {1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0}}})
        comparison = gr.compare_runs(figaro, gdp)
        self.assertTrue(comparison["baseline_identical"])
        self.assertEqual(comparison["aggregate"]["WORLD"]["g"]["1"]["better"], "FIGARO")
        self.assertEqual(comparison["aggregate"]["WORLD"]["g"]["2"]["better"], "GDP_PROXY")
        self.assertEqual(comparison["aggregate"]["WORLD"]["g"]["3"]["better"], "TIE")
        self.assertAlmostEqual(comparison["aggregate"]["WORLD"]["g"]["1"]["delta_rmse"], -0.5,
                               places=9)
        self.assertEqual(comparison["per_country"]["g"]["1"]["better_a"], 1)
        self.assertEqual(comparison["per_country"]["g"]["1"]["better_b"], 1)


def write_figaro_fixture(path: Path, countries, industries, blocks) -> Path:
    """Write a minimal FIGARO-shaped dataset.json: Z is a country-major block matrix."""
    n = len(countries) * len(industries)

    def index(country, industry):
        return countries.index(country) * len(industries) + industry

    Z = [[0.0] * n for _ in range(n)]
    for (exporter, importer), value in blocks.items():
        for industry_a in range(len(industries)):
            for industry_b in range(len(industries)):
                Z[index(exporter, industry_a)][index(importer, industry_b)] = float(value)
    document = {"countries": list(countries), "industries": list(industries), "Z": Z,
                "units": "USD_mn", "schema": "amfe.mrio.dataset.v1"}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


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
