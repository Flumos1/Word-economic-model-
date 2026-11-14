"""Tests for the GVAR prototype (``src/amfe/world/gvar.py``, docs/123).

The suite uses synthetic data with a known data-generating process: coefficient recovery, global
stability on the stacked companion, IRF signs/decay, forecast consistency with the exported
equations, weight normalisation, fail-closed validation and determinism.  No real data, no admission,
no registry.

Run: ``python tests/test_gvar.py`` or ``python -m unittest tests.test_gvar -v``.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.world.gvar import (  # noqa: E402
    GVAR,
    GVARError,
    MODEL_ID,
    MODEL_VERSION,
    normalize_weights,
)

COUNTRIES = ["A", "B", "C"]
WEIGHTS = {
    "A": {"B": 0.6, "C": 0.4},
    "B": {"A": 0.5, "C": 0.5},
    "C": {"A": 0.3, "B": 0.7},
}
VARIABLES = ["x"]


def synthetic(n=500, noise=0.005, own=0.5, foreign=0.2, seed=7, variables=("x",),
              burn=100, weights=WEIGHTS, unstable=False):
    """Simulate a known stable country VARX(1) process with trade-weighted factors."""
    rng = np.random.default_rng(seed)
    var_list = list(variables)
    k = len(var_list)
    total = int(n) + int(burn)
    own_matrix = np.eye(k) * float(own)
    foreign_matrix = np.eye(k) * float(foreign)
    values = {country: np.zeros((total, k)) for country in COUNTRIES}
    for t in range(1, total):
        for country in COUNTRIES:
            factor = np.zeros(k)
            for other, weight in weights[country].items():
                factor = factor + float(weight) * values[other][t - 1]
            values[country][t] = (own_matrix @ values[country][t - 1]
                                  + foreign_matrix @ factor
                                  + rng.normal(0.0, noise, k))
    series = {country: {variable: [float(v) for v in values[country][burn:, j]]
                        for j, variable in enumerate(var_list)}
              for country in COUNTRIES}
    dataset = {"countries": list(COUNTRIES), "series": series, "weights": weights}
    if unstable:
        # replace with an explicitly unstable process (own coefficient > 1), no foreign term
        values = {country: np.zeros((total, k)) for country in COUNTRIES}
        for t in range(1, total):
            for j, country in enumerate(COUNTRIES):
                values[country][t] = (float(own) * values[country][t - 1]
                                      + 0.0003 * j
                                      + rng.normal(0.0, noise, k))
        dataset["series"] = {country: {variable: [float(v) for v in values[country][burn:, j]]
                                       for j, variable in enumerate(var_list)}
                             for country in COUNTRIES}
    return dataset


def dataset_without(key):
    data = synthetic(n=60)
    data.pop(key)
    return data


class GVARPrototype(unittest.TestCase):
    # ---------------------------------------------------------------- interface
    def test_interface_and_export_shape(self):
        model = GVAR(synthetic(n=120), lags=1)
        self.assertEqual(model.countries, COUNTRIES)
        self.assertEqual(model.variables, VARIABLES)
        self.assertEqual(model.k, 1)
        self.assertEqual(model.lags, 1)
        export = model.export_coefficients()
        self.assertEqual(export["model_id"], MODEL_ID)
        self.assertEqual(export["model_version"], MODEL_VERSION)
        self.assertEqual(export["countries"], COUNTRIES)
        self.assertEqual(export["observations"], 120)
        for key in ("equation", "weights", "equations", "stability", "identification", "caveats"):
            self.assertIn(key, export)
        self.assertNotIn("series", export)  # licence-safe: no raw data in the export
        self.assertEqual(list(export["equations"]), COUNTRIES)
        equation = export["equations"]["A"]
        self.assertEqual(len(equation["intercept"]), 1)
        self.assertEqual(len(equation["own_lags"]), 1)
        self.assertEqual(len(equation["foreign_lags"]), 1)
        json.dumps(export)  # must be JSON serialisable

    # ---------------------------------------------------------------- estimation
    def test_fit_recovers_known_dynamics(self):
        model = GVAR(synthetic(n=2000, noise=0.005, own=0.5, foreign=0.2, seed=11), lags=1)
        for country in COUNTRIES:
            beta = model.coefficients[country]
            self.assertEqual(beta.shape, (1, 3))
            self.assertAlmostEqual(float(beta[0, 0]), 0.0, delta=0.02)   # intercept
            self.assertAlmostEqual(float(beta[0, 1]), 0.5, delta=0.03)   # own lag
            self.assertAlmostEqual(float(beta[0, 2]), 0.2, delta=0.03)   # foreign lag

    def test_fit_multivariate_shapes(self):
        model = GVAR(synthetic(n=300, variables=("x", "z"), seed=3), lags=2)
        self.assertEqual(model.k, 2)
        for country in COUNTRIES:
            self.assertEqual(model.coefficients[country].shape, (2, 1 + 2 * 2 * 2))
        forecast = model.forecast(steps=4)
        self.assertEqual(forecast["steps"], [1, 2, 3, 4])
        for country in COUNTRIES:
            for variable in ("x", "z"):
                self.assertEqual(len(forecast["path"][country][variable]), 4)
                self.assertTrue(all(np.isfinite(forecast["path"][country][variable])))

    # ---------------------------------------------------------------- stability
    def test_stable_system(self):
        model = GVAR(synthetic(n=300, own=0.5, foreign=0.2, seed=5), lags=1)
        self.assertTrue(model.is_stable())
        self.assertTrue(model.stable)
        stability = model.stability()
        self.assertTrue(stability["stable"])
        self.assertLess(stability["max_modulus"], 1.0)
        self.assertEqual(stability["dimension"], len(COUNTRIES) * model.k * model.lags)
        self.assertEqual(len(stability["eigenvalues"]), stability["dimension"])

    def test_unstable_system(self):
        model = GVAR(synthetic(n=80, own=1.05, foreign=0.0, noise=0.0005, seed=9,
                               unstable=True), lags=1)
        self.assertFalse(model.is_stable())
        stability = model.stability()
        self.assertFalse(stability["stable"])
        self.assertGreater(stability["max_modulus"], 1.0)

    def test_lags_validation(self):
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(GVARError):
                GVAR(synthetic(n=80), lags=bad)

    # ---------------------------------------------------------------- IRF
    def test_irf_unit_sign_and_decay(self):
        model = GVAR(synthetic(n=500, own=0.5, foreign=0.2, seed=13), lags=1)
        result = model.irf("A", "x", horizons=40)
        self.assertEqual(result["method"], "unit")
        self.assertEqual(result["horizons"][0], 0)
        self.assertAlmostEqual(result["response"]["A"]["x"][0], 1.0)
        self.assertAlmostEqual(result["response"]["B"]["x"][0], 0.0)
        # own persistence and positive trade spillover one period later
        self.assertGreater(result["response"]["A"]["x"][1], 0.0)
        self.assertGreater(result["response"]["B"]["x"][1], 0.0)
        self.assertGreater(result["response"]["C"]["x"][1], 0.0)
        # stable system: the response decays toward zero
        self.assertLess(abs(result["response"]["A"]["x"][-1]),
                        0.01 * abs(result["response"]["A"]["x"][1]))
        values = [v for country in COUNTRIES for v in result["response"][country]["x"]]
        self.assertTrue(all(np.isfinite(values)))

    def test_irf_cholesky_and_validation(self):
        model = GVAR(synthetic(n=500, seed=17), lags=1)
        result = model.irf("A", "x", horizons=3, method="cholesky")
        self.assertEqual(result["method"], "cholesky")
        diagonal = model.residual_covariance()[0, 0] ** 0.5
        self.assertAlmostEqual(result["response"]["A"]["x"][0], diagonal, places=8)
        self.assertGreater(result["response"]["A"]["x"][0], 0.0)
        self.assertLess(abs(result["response"]["B"]["x"][0]), 0.01)  # independent shocks
        self.assertGreater(result["response"]["B"]["x"][1], 0.0)
        with self.assertRaises(GVARError):
            model.irf("Z", "x")
        with self.assertRaises(GVARError):
            model.irf("A", "missing")
        with self.assertRaises(GVARError):
            model.irf("A", "x", method="ols")
        with self.assertRaises(GVARError):
            model.irf("A", "x", horizons=-1)

    # ---------------------------------------------------------------- weights
    def test_weights_must_be_valid_and_sum_to_one(self):
        data = synthetic(n=60)
        bad_sum = {"A": {"B": 0.5, "C": 0.4}, "B": {"A": 0.5, "C": 0.5},
                   "C": {"A": 0.3, "B": 0.7}}
        with self.assertRaises(GVARError):
            GVAR(dict(data, weights=bad_sum), lags=1)
        negative = {"A": {"B": 1.2, "C": -0.2}, "B": {"A": 0.5, "C": 0.5},
                    "C": {"A": 0.3, "B": 0.7}}
        with self.assertRaises(GVARError):
            GVAR(dict(data, weights=negative), lags=1)
        self_weight = {"A": {"A": 0.1, "B": 0.5, "C": 0.4},
                       "B": {"A": 0.5, "C": 0.5}, "C": {"A": 0.3, "B": 0.7}}
        with self.assertRaises(GVARError):
            GVAR(dict(data, weights=self_weight), lags=1)
        unknown = {"A": {"B": 0.6, "C": 0.2, "Z": 0.2},
                   "B": {"A": 0.5, "C": 0.5}, "C": {"A": 0.3, "B": 0.7}}
        with self.assertRaises(GVARError):
            GVAR(dict(data, weights=unknown), lags=1)

    def test_normalize_weights(self):
        raw = {"A": {"B": 3.0, "C": 1.0}, "B": {"A": 1.0, "C": 1.0},
               "C": {"A": 1.0, "B": 4.0}}
        normalised = normalize_weights(raw, COUNTRIES)
        for country in COUNTRIES:
            self.assertAlmostEqual(sum(normalised[country].values()), 1.0, places=12)
        self.assertAlmostEqual(normalised["A"]["B"], 0.75)
        sparse = {"A": {"B": 1.0}, "B": {"A": 0.5, "C": 0.5}, "C": {"A": 0.3, "B": 0.7}}
        clean = normalize_weights(sparse, COUNTRIES)
        self.assertEqual(clean["A"], {"B": 1.0})

    # ---------------------------------------------------------------- fail-closed
    def test_fail_closed_dataset(self):
        data = synthetic(n=60)
        with self.assertRaises(GVARError):
            GVAR(dataset_without("countries"))
        with self.assertRaises(GVARError):
            GVAR(dict(data, countries=[]))
        with self.assertRaises(GVARError):
            GVAR(dict(data, countries=["A", "B"]))
        with self.assertRaises(GVARError):
            GVAR(dataset_without("weights"))
        with self.assertRaises(GVARError):
            GVAR({**data, "series": {k: v for k, v in data["series"].items() if k != "C"}})
        mismatched = {k: dict(v) for k, v in data["series"].items()}
        mismatched["B"] = {"y": mismatched["B"]["x"]}
        with self.assertRaises(GVARError):
            GVAR(dict(data, series=mismatched))
        length_mismatch = {k: dict(v) for k, v in data["series"].items()}
        length_mismatch["C"] = {"x": length_mismatch["C"]["x"][:-1]}
        with self.assertRaises(GVARError):
            GVAR(dict(data, series=length_mismatch), lags=1)
        with self.assertRaises(GVARError):
            GVAR(synthetic(n=5), lags=1)  # too short for the parameter count
        with self.assertRaises(GVARError):
            GVAR(dict(data, series={"A": {"x": [0.0] * 60}}))
        non_finite = {k: dict(v) for k, v in data["series"].items()}
        non_finite["A"] = {"x": list(non_finite["A"]["x"])}
        non_finite["A"]["x"][3] = float("nan")
        with self.assertRaises(GVARError):
            GVAR(dict(data, series=non_finite))

    # ---------------------------------------------------------------- forecast
    def test_forecast_matches_exported_equations(self):
        model = GVAR(synthetic(n=200, variables=("x",), seed=21), lags=2)
        export = model.export_coefficients()
        forecast = model.forecast(steps=4)
        for country in COUNTRIES:
            equation = export["equations"][country]
            intercept = np.asarray(equation["intercept"], dtype=float)
            own = [np.asarray(block, dtype=float) for block in equation["own_lags"]]
            foreign = [np.asarray(block, dtype=float) for block in equation["foreign_lags"]]
            history = {c: [model._Y[c][model.n - lag, :] for lag in range(1, model.lags + 1)]
                       for c in COUNTRIES}
            for step in range(4):
                value = intercept.copy()
                for lag in range(1, model.lags + 1):
                    value = value + own[lag - 1] @ history[country][lag - 1]
                for lag in range(1, model.lags + 1):
                    factor = np.zeros(model.k)
                    for other, weight in model.weights[country].items():
                        factor = factor + float(weight) * history[other][lag - 1]
                    value = value + foreign[lag - 1] @ factor
                expected = float(value[0])
                self.assertAlmostEqual(forecast["path"][country]["x"][step], expected, places=10)
                for c in COUNTRIES:
                    history[c] = [np.asarray([forecast["path"][c]["x"][step]])] + history[c][:-1]

    def test_forecast_is_deterministic(self):
        data = synthetic(n=150, seed=23)
        first = GVAR(data, lags=1).forecast(steps=4)
        second = GVAR(data, lags=1).forecast(steps=4)
        self.assertEqual(first, second)

    # ---------------------------------------------------------------- determinism
    def test_export_deterministic_and_digest(self):
        data = synthetic(n=150, seed=27)
        first = GVAR(data, lags=1)
        second = GVAR(data, lags=1)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(len(first.digest()), 64)
        self.assertRegex(first.digest(), r"^[0-9a-f]{64}$")
        parsed = json.loads(first.to_json())
        self.assertEqual(parsed["stability"]["stable"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
