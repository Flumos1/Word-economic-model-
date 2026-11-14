"""Tests for the D4 alternative-dynamics study (``tools/gvar_alt.py``, docs/135).

Everything is synthetic and offline: no F: data is touched, WDI cache fixtures are created in a
temporary directory where needed.  The suite checks the Minnesota-prior BVAR, the PCA factor model,
local projections (including a known-impulse recovery), the OOS engine, IRF sign/decay,
determinism, fail-closed behaviour and the CLI.

Run: ``python tests/test_gvar_alt.py`` or ``python -m unittest tests.test_gvar_alt -v``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import gvar_alt as alt  # noqa: E402

COUNTRIES = ("USA", "DEU", "FRA", "ITA", "CHN", "JPN")
YEARS = list(range(1995, 2025))


def ar1_panel(countries=COUNTRIES, years=YEARS, seed=7) -> dict:
    """Country panel with noisy stationary (g, pi) AR(1) series."""
    rng = np.random.default_rng(seed)
    panel = {name: {} for name in ("gdp_growth", "inflation", "gdp_current", "trade_gdp",
                                   "population")}
    for index, code in enumerate(countries):
        growth, inflation = 2.0, 2.0
        for step, year in enumerate(years):
            growth = 0.6 * growth + 0.4 * 2.0 + float(rng.normal(0.0, 0.6))
            inflation = 0.5 * inflation + 0.5 * 2.0 + float(rng.normal(0.0, 0.4))
            panel["gdp_growth"].setdefault(code, {})[year] = growth
            panel["inflation"].setdefault(code, {})[year] = inflation
            panel["gdp_current"].setdefault(code, {})[year] = 1000.0 * (index + 1) * (1.02 ** step)
            panel["trade_gdp"].setdefault(code, {})[year] = 40.0 + index
            panel["population"].setdefault(code, {})[year] = 50.0 + index
    return panel


def var1_data(rows=400, seed=3, a=((0.5, 0.1), (0.0, 0.4)), noise=0.01):
    rng = np.random.default_rng(seed)
    matrix = np.asarray(a, dtype=float)
    values = np.zeros((rows, 2))
    for t in range(1, rows):
        values[t] = matrix @ values[t - 1] + rng.normal(0.0, noise, 2)
    return values


def core_data(rows=300, seed=5):
    """4 groups x (g, pi) core system with a positive US -> others growth spillover."""
    rng = np.random.default_rng(seed)
    values = np.zeros((rows, 8))
    for t in range(1, rows):
        us = values[t - 1, 0]
        values[t, 0] = 0.5 * us + rng.normal(0.0, 0.2)
        values[t, 1] = 0.4 * values[t - 1, 1] + rng.normal(0.0, 0.2)
        for group in range(1, 4):
            values[t, group * 2] = (0.4 * values[t - 1, group * 2] + 0.15 * us
                                    + rng.normal(0.0, 0.2))
            values[t, group * 2 + 1] = (0.3 * values[t - 1, group * 2 + 1]
                                        + rng.normal(0.0, 0.2))
    return values


class MinnesotaBVAR(unittest.TestCase):
    def test_recovers_stable_var1_and_forecast_matches_recursion(self):
        data = var1_data()
        model = alt.fit_minnesota_var(data, lags=1, lam=0.2)
        beta = model["coefficients"]
        self.assertEqual(beta.shape, (2, 3))
        self.assertAlmostEqual(float(beta[0, 1]), 0.5, delta=0.08)
        self.assertAlmostEqual(float(beta[1, 2]), 0.4, delta=0.08)
        self.assertLess(model["max_modulus"], 1.0)
        forecast = alt.bvar_forecast(model, data, steps=3)["path"]
        manual = []
        history = [data[-1].copy()]
        for step in range(3):
            value = beta[:, 0].copy()
            for lag in range(1, 2):
                value = value + beta[:, 1 + (lag - 1) * 2:1 + lag * 2] @ history[lag - 1]
            manual.append(value)
            history = [value] + history[:-1]
        self.assertEqual(forecast.shape, (3, 2))
        for step in range(3):
            for variable in range(2):
                self.assertAlmostEqual(float(forecast[step, variable]),
                                       float(manual[step][variable]), places=10)

    def test_prior_shrinkage_and_determinism(self):
        data = var1_data()
        tight = alt.fit_minnesota_var(data, lags=1, lam=0.01)
        loose = alt.fit_minnesota_var(data, lags=1, lam=5.0)
        # a tighter prior shrinks the own-lag coefficient towards the prior mean 1 less than a
        # loose prior moves it towards the OLS value; both stay finite and deterministic
        self.assertTrue(np.all(np.isfinite(tight["coefficients"])))
        self.assertTrue(np.all(np.isfinite(loose["coefficients"])))
        again = alt.fit_minnesota_var(data, lags=1, lam=0.01)
        gap = np.abs(tight["coefficients"] - again["coefficients"]).max()
        self.assertLess(gap, 1e-12)
        # the tight prior pins the own lag near the prior mean 1; the loose prior moves to OLS
        self.assertGreater(float(tight["coefficients"][0, 1]), 0.8)
        self.assertAlmostEqual(float(loose["coefficients"][0, 1]), 0.5, delta=0.12)

    def test_lag_two_and_fail_closed(self):
        model = alt.fit_minnesota_var(var1_data(rows=200), lags=2, lam=0.2)
        self.assertEqual(model["coefficients"].shape, (2, 5))
        self.assertEqual(len(model["sigmas"]), 2)
        for kwargs in ({"lags": 3}, {"lags": 0}, {"lam": 0.0}, {"lam": -1.0}):
            with self.assertRaises(alt.GVARAltError):
                alt.fit_minnesota_var(var1_data(rows=100), **kwargs)
        with self.assertRaises(alt.GVARAltError):
            alt.fit_minnesota_var(var1_data(rows=5), lags=1, lam=0.2)


class FactorModel(unittest.TestCase):
    def test_pca_recovers_one_common_factor(self):
        rng = np.random.default_rng(11)
        factor = rng.normal(0.0, 1.0, size=300)
        loadings = np.array([0.9, 0.8, 0.7, -0.6, 0.5])
        z = np.outer(factor, loadings) + rng.normal(0.0, 0.3, size=(300, 5))
        z = (z - z.mean(axis=0)) / z.std(axis=0)
        model = alt.fit_factor_model(z, factors=1)
        self.assertGreater(model["explained_variance"], 0.8)
        forecast = alt.factor_forecast(model, z[-1], steps=4)["standardized"]
        self.assertEqual(forecast.shape, (4, 5))
        self.assertTrue(np.all(np.isfinite(forecast)))
        again = alt.fit_factor_model(z, factors=1)
        self.assertAlmostEqual(model["explained_variance"], again["explained_variance"], places=12)
        self.assertEqual(forecast.tolist(),
                         alt.factor_forecast(again, z[-1], steps=4)["standardized"].tolist())

    def test_factor_count_validation(self):
        z = np.random.default_rng(2).normal(size=(50, 6))
        for factors in (0, 4, -1):
            with self.assertRaises(alt.GVARAltError):
                alt.fit_factor_model(z, factors=factors)
        with self.assertRaises(alt.GVARAltError):
            alt.fit_factor_model(z[:3], factors=1)


class LocalProjections(unittest.TestCase):
    def test_known_impulse_is_recovered(self):
        rng = np.random.default_rng(13)
        us = rng.normal(0.0, 1.0, size=400)
        target = np.zeros(400)
        for t in range(1, 400):
            target[t] = 0.8 * us[t - 1] + float(rng.normal(0.0, 0.05))
        responses = alt.lp_irf(us, target, horizons=3)
        self.assertEqual(responses[0], 1.0)
        self.assertAlmostEqual(responses[1], 0.8, delta=0.1)
        self.assertAlmostEqual(responses[2], 0.0, delta=0.2)

    def test_lp_forecast_and_fail_closed(self):
        series = {"g": np.asarray(var1_data()[:, 0]), "pi": np.asarray(var1_data()[:, 1])}
        forecast = alt.local_projection_forecast(series, steps=4)
        self.assertEqual(len(forecast["g"]), 4)
        self.assertEqual(len(forecast["pi"]), 4)
        self.assertTrue(all(np.isfinite(forecast["g"])))
        with self.assertRaises(alt.GVARAltError):
            alt.local_projection_forecast({"g": np.arange(4.0), "pi": np.arange(4.0)}, steps=4)
        with self.assertRaises(alt.GVARAltError):
            alt.lp_irf(np.arange(5.0), np.arange(6.0))


class OOSEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = ar1_panel()
        cls.shares = {"US": {"USA": 1.0},
                      "EA": {"DEU": 0.4, "FRA": 0.35, "ITA": 0.25},
                      "CN": {"CHN": 1.0},
                      "WORLD": {code: 1.0 / len(COUNTRIES) for code in COUNTRIES}}

    def _run(self):
        return alt.rolling_oos(self.panel, COUNTRIES, YEARS, self.shares, max_h=4,
                               n_origins=8, lambdas=(0.2,), factor_counts=(1,))

    def test_metrics_complete_and_deterministic(self):
        first = self._run()
        second = self._run()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        expected = {"BVAR_p1_l0.2", "BVAR_p2_l0.2", "FACTOR_q1", "LP", "AR1", "RW"}
        self.assertEqual(set(first["models"]), expected)
        for model in expected:
            for group in ("US", "EA", "CN", "WORLD"):
                for variable in ("g", "pi"):
                    for h in (1, 2, 3, 4):
                        entry = first["metrics"][model][group][variable][h]
                        self.assertGreater(entry["n"], 0)
                        self.assertTrue(np.isfinite(entry["rmse"]))
                        self.assertTrue(np.isfinite(entry["mae"]))
        self.assertIn("USA", first["per_country"]["BVAR_p1_l0.2"])
        self.assertGreater(first["diagnostics"]["LP"]["fits"], 0)

    def test_short_sample_fails(self):
        with self.assertRaises(alt.GVARAltError):
            alt.rolling_oos(self.panel, COUNTRIES, YEARS[:10], self.shares, max_h=4,
                            n_origins=3)

    def test_aggregate_and_compare(self):
        share = {"A": 0.75, "B": 0.25}
        series = {"A": {"g": np.array([1.0, 2.0]), "pi": np.array([0.0, 0.0])},
                  "B": {"g": np.array([3.0, 4.0]), "pi": np.array([0.0, 0.0])}}
        self.assertEqual(alt.aggregate(series, share, "g").tolist(), [1.5, 2.5])
        oos = self._run()
        comparison = alt.compare(oos, "AR1")
        self.assertIn("BVAR_p1_l0.2", comparison)
        self.assertIn("LP", comparison)
        entry = comparison["BVAR_p1_l0.2"]
        self.assertEqual(entry["cells"], 32)
        self.assertEqual(entry["better_cells"] + entry["worse_cells"] + entry["ties"], 32)


class IRF(unittest.TestCase):
    def test_bvar_core_irf_sign_and_decay(self):
        core = core_data()
        irf = alt.bvar_core_irf(core, lags=2, lam=0.2, horizons=8)
        matrix = irf["matrix"]
        self.assertEqual(matrix.shape, (9, 8))
        self.assertAlmostEqual(float(matrix[0, 0]), 1.0, places=8)
        world = matrix[:, 6]
        self.assertGreater(float(world[1]), 0.0)      # positive spillover at h=1
        self.assertLess(abs(float(world[8])), abs(float(world[1])) * 0.5)  # decays
        again = alt.bvar_core_irf(core, lags=2, lam=0.2, horizons=8)
        self.assertLess(np.abs(matrix - again["matrix"]).max(), 1e-12)

    def test_factor_and_lp_irf_shapes(self):
        panel = ar1_panel()
        countries = tuple(panel["gdp_growth"])
        series = alt.panel_series(panel, countries, YEARS)
        shares = {"US": {"USA": 1.0},
                  "EA": {"DEU": 0.4, "FRA": 0.35, "ITA": 0.25},
                  "CN": {"CHN": 1.0},
                  "WORLD": {code: 1.0 / len(countries) for code in countries}}
        factor = alt.factor_irf(series, shares, factors=1, horizons=4)
        self.assertIn("WORLD", factor["group_growth"])
        self.assertAlmostEqual(factor["group_growth"]["US"][0], 1.0, places=6)
        for path in factor["group_growth"].values():
            self.assertTrue(all(np.isfinite(path)))
        lp = alt.lp_irf(alt.aggregate(series, shares["US"], "g"),
                        alt.aggregate(series, shares["WORLD"], "g"), horizons=4)
        self.assertEqual(lp[0], 1.0)
        self.assertEqual(len(lp), 5)


class DataAndCLI(unittest.TestCase):
    def test_load_panel_fail_closed_without_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(alt.GVARAltError):
                alt.load_panel(Path(directory) / "mrio", cache_dir=Path(directory) / "empty")

    def test_panel_series_fail_closed_on_gaps(self):
        panel = ar1_panel()
        panel["gdp_growth"]["USA"][2000] = None
        with self.assertRaises(alt.GVARAltError):
            alt.panel_series(panel, COUNTRIES, YEARS)

    def test_cli_help(self):
        import subprocess
        tool = ROOT / "tools" / "gvar_alt.py"
        result = subprocess.run([sys.executable, str(tool), "--help"],
                                capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0)
        for token in ("--origins", "--max-h", "--lambdas", "--factors", "--report"):
            self.assertIn(token, result.stdout)
        self.assertIn("BVAR", result.stdout)
        # invalid values are rejected before any data is touched
        with self.assertRaises(SystemExit):
            alt.main(["--origins", "1"])
        with self.assertRaises(SystemExit):
            alt.main(["--lambdas", "0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
