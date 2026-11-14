"""GVAR prototype: country VARX(p) with trade-weighted global factors (docs/123).

First cross-country dynamic candidate for the AMFE model, the cheap prototype recommended by
docs/56 §2.4 ("GVAR as the first cross-country candidate; trade weights from the MRIO").  It is a
*prototype only*: it is not connected to the admission protocol, has no frozen chain, no registry
entries and no real-data evaluation.

Model (discrete transition, a special case of EQ-DYN-001, docs/53), for every country ``i`` and
variable vector ``y_it`` (K variables):

    y_it = c_i + sum_{l=1..p} A_il y_{i,t-l} + sum_{l=1..p} B_il y*_{i,t-l} + e_it
    y*_{i,t} = sum_{j != i} w_ij y_{j,t},   sum_{j != i} w_ij = 1,   w_ij >= 0,   w_ii = 0

``y*`` is the foreign (global) factor: the trade-weighted average of the other countries' variables.
Weights come from the MRIO trade scheme (they are an *input*, not estimated here).  The observation
layer is the noisy observation of EQ-OBS-001 (``y`` is measured with additive noise); the prototype
does not model vintages.

Estimation: ordinary least squares, equation by equation, on a balanced panel (same variables, same
length for every country).  Weak exogeneity of the foreign factors for a small open economy is the
standard GVAR assumption (disclosed, not tested here).  Stability is checked on the *global*
companion form of dimension ``N*K*p``: the country equations are stacked into one VAR(1) in the
state ``[Y_t, Y_{t-1}, ..., Y_{t-p+1}]``, so cross-country spillovers through ``w_ij`` are included
in the eigenvalues.  IRFs are propagated with the same global companion; ``method="unit"`` is a
one-unit shock to one observed series, ``method="cholesky"`` is a one-standard-deviation structural
shock identified by the lower Cholesky factor of the global residual covariance (countries in the
dataset order, variables sorted).

Public API::

    from amfe.world.gvar import GVAR
    model = GVAR(dataset, lags=2)
    model.is_stable(), model.stability()
    model.irf("US", "gdp", horizons=12)
    model.forecast(steps=4)
    model.export_coefficients(), model.to_json()

Dataset schema::

    {"countries": ["A", "B", "C"],
     "series": {"A": {"gdp": [..], "cpi": [..]}, ...},
     "weights": {"A": {"B": 0.6, "C": 0.4}, ...}}
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

import numpy as np

MODEL_ID = "GVAR_PROTOTYPE"
MODEL_VERSION = "0.1.0"
WEIGHT_TOLERANCE = 1e-6
ROUND_DIGITS = 12


class GVARError(ValueError):
    """Fail-closed validation/estimation error of the GVAR prototype."""


# --------------------------------------------------------------------------- validation
def _as_float_vector(values, where: str) -> np.ndarray:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GVARError(f"{where} must be a 1-D sequence of numbers")
    array = np.asarray([float(v) for v in values], dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise GVARError(f"{where} must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(array)):
        raise GVARError(f"{where} contains non-finite values")
    return array


def normalize_weights(weights, countries) -> dict:
    """Validate a trade-weight matrix and return a row-normalised copy.

    Fails closed on unknown countries, negative weights, a non-zero self weight or an empty row.
    Missing country pairs are treated as zero (sparse MRIO rows); each row is divided by its sum, so
    raw trade shares can be passed directly.  :func:`validate_dataset` additionally requires the
    input rows to sum to one already (a silent renormalisation of the evaluation weights is not
    wanted there).
    """
    return _checked_weight_rows(weights, countries, require_unit_sum=False)


def _checked_weight_rows(weights, countries, *, require_unit_sum: bool) -> dict:
    if not isinstance(weights, Mapping):
        raise GVARError("dataset.weights must be a mapping country -> {other: weight}")
    countries = [str(c) for c in countries]
    known = set(countries)
    out: dict = {}
    for country in countries:
        row = weights.get(country)
        if not isinstance(row, Mapping):
            raise GVARError(f"weights for country {country!r} are missing")
        clean: dict = {}
        for other, value in row.items():
            other = str(other)
            if other not in known:
                raise GVARError(f"weights[{country!r}] refers to unknown country {other!r}")
            weight = float(value)
            if not np.isfinite(weight):
                raise GVARError(f"weights[{country!r}][{other!r}] is not finite")
            if other == country:
                if abs(weight) > WEIGHT_TOLERANCE:
                    raise GVARError(f"weights[{country!r}][{country!r}] must be zero "
                                    "(self-influence is not allowed)")
                continue
            if weight < 0.0:
                raise GVARError(f"weights[{country!r}][{other!r}] is negative")
            if weight > 0.0:
                clean[other] = weight
        total = float(sum(clean.values()))
        if total <= 0.0:
            raise GVARError(f"weights for country {country!r} have no positive entries")
        if require_unit_sum and abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise GVARError(
                f"weights for country {country!r} sum to {total:.6f}, not 1 "
                f"(tolerance {WEIGHT_TOLERANCE}); use normalize_weights() for raw trade shares")
        out[country] = {other: value / total for other, value in clean.items()}
    return out


def validate_dataset(dataset, lags: int = 1) -> dict:
    """Fail-closed validation of the dataset; returns a normalised, ordered copy."""
    if not isinstance(dataset, Mapping):
        raise GVARError("dataset must be a mapping with 'countries', 'series', 'weights'")
    countries_raw = dataset.get("countries")
    if not isinstance(countries_raw, (list, tuple)) or not countries_raw:
        raise GVARError("dataset.countries must be a non-empty list")
    countries = [str(c) for c in countries_raw]
    if len(set(countries)) != len(countries):
        raise GVARError("dataset.countries contains duplicates")
    series = dataset.get("series")
    weights = dataset.get("weights")
    if not isinstance(series, Mapping) or not series:
        raise GVARError("dataset.series must be a non-empty mapping country -> {variable: series}")
    if not isinstance(weights, Mapping) or not weights:
        raise GVARError("dataset.weights must be a non-empty mapping country -> {other: weight}")
    missing = [c for c in countries if c not in series]
    extra = [c for c in series if c not in set(countries)]
    if missing or extra:
        raise GVARError(f"series countries do not match dataset.countries "
                        f"(missing={missing}, unknown={extra})")
    missing = [c for c in countries if c not in weights]
    extra = [c for c in weights if c not in set(countries)]
    if missing or extra:
        raise GVARError(f"weight countries do not match dataset.countries "
                        f"(missing={missing}, unknown={extra})")
    variables = sorted(str(v) for v in series[countries[0]])
    if not variables:
        raise GVARError(f"country {countries[0]!r} has no variables")
    for country in countries:
        row = series[country]
        if not isinstance(row, Mapping) or not row:
            raise GVARError(f"series for country {country!r} must be a non-empty mapping")
        if sorted(str(v) for v in row) != variables:
            raise GVARError(f"country {country!r} variables {sorted(row)} "
                            f"differ from {variables}")
    clean_series: dict = {}
    lengths = set()
    for country in countries:
        clean_series[country] = {}
        for variable in variables:
            array = _as_float_vector(series[country][variable],
                                     f"series[{country!r}][{variable!r}]")
            clean_series[country][variable] = array
            lengths.add(int(array.size))
    if len(lengths) != 1:
        raise GVARError(f"all series must have the same length, got {sorted(lengths)}")
    n = lengths.pop()
    k = len(variables)
    n_params = k * (2 * lags) + 1
    if n <= lags + 2:
        raise GVARError(f"series too short for lags={lags}: n={n}")
    if n - lags < n_params + 2:
        raise GVARError(
            f"series too short for {k} variables and lags={lags}: n={n}, "
            f"parameters per equation={n_params}; need n - lags >= {n_params + 2}")
    clean_weights = _checked_weight_rows(weights, countries, require_unit_sum=True)
    return {"countries": countries, "variables": variables, "series": clean_series,
            "weights": clean_weights, "n": n, "lags": int(lags)}


# --------------------------------------------------------------------------- model
class GVAR:
    """Country VARX(p) with trade-weighted global factors, estimated by OLS."""

    def __init__(self, dataset, lags: int = 1):
        if isinstance(lags, bool) or not isinstance(lags, int) or lags < 1:
            raise GVARError("lags must be a positive integer")
        self.lags = int(lags)
        data = validate_dataset(dataset, self.lags)
        self.countries: list = list(data["countries"])
        self.variables: list = list(data["variables"])
        self.weights: dict = data["weights"]
        self.n: int = int(data["n"])
        self.k: int = len(self.variables)
        self._Y: dict = {c: np.column_stack([data["series"][c][v] for v in self.variables])
                         for c in self.countries}
        self._Ystar: dict = self._foreign_factors()
        self.coefficients: dict = {}
        self.residuals: dict = {}
        self._fit()
        self._global: np.ndarray = self._global_companion()
        self._eigenvalues: np.ndarray = np.linalg.eigvals(self._global)
        self._covariance: np.ndarray = self._residual_covariance()

    # -- construction --------------------------------------------------------
    def _foreign_factors(self) -> dict:
        out = {}
        for country in self.countries:
            weighted = np.zeros_like(self._Y[country])
            for other, weight in self.weights[country].items():
                weighted = weighted + float(weight) * self._Y[other]
            out[country] = weighted
        return out

    def _design(self, country: str) -> np.ndarray:
        y, ystar, p = self._Y[country], self._Ystar[country], self.lags
        rows = self.n - p
        columns = [np.ones((rows, 1))]
        for lag in range(1, p + 1):
            columns.append(y[p - lag:self.n - lag, :])
        for lag in range(1, p + 1):
            columns.append(ystar[p - lag:self.n - lag, :])
        return np.column_stack(columns)

    def _fit(self) -> None:
        for country in self.countries:
            x = self._design(country)
            y = self._Y[country][self.lags:, :]
            beta, *_ = np.linalg.lstsq(x, y, rcond=None)
            # beta comes back as (columns, k); store it as (k, 1 + 2*k*p)
            self.coefficients[country] = np.asarray(beta, dtype=float).T
            self.residuals[country] = y - x @ beta

    def _global_companion(self) -> np.ndarray:
        n, k, p = len(self.countries), self.k, self.lags
        block = n * k
        companion = np.zeros((block * p, block * p), dtype=float)
        for i, country in enumerate(self.countries):
            beta = self.coefficients[country]
            own = [beta[:, 1 + l * k:1 + (l + 1) * k] for l in range(p)]
            foreign = [beta[:, 1 + (p + l) * k:1 + (p + l + 1) * k] for l in range(p)]
            for lag in range(p):
                for j, other in enumerate(self.countries):
                    if i == j:
                        piece = own[lag]
                    else:
                        weight = float(self.weights[country].get(other, 0.0))
                        if weight == 0.0:
                            continue
                        piece = weight * foreign[lag]
                    row = slice(i * k, (i + 1) * k)
                    col = slice(lag * block + j * k, lag * block + (j + 1) * k)
                    companion[row, col] = piece
        for lag in range(1, p):
            rows = slice(lag * block, (lag + 1) * block)
            cols = slice((lag - 1) * block, lag * block)
            companion[rows, cols] = np.eye(block)
        return companion

    def _residual_covariance(self) -> np.ndarray:
        rows = self.n - self.lags
        matrix = np.column_stack([self.residuals[c] for c in self.countries])
        return (matrix.T @ matrix) / float(rows)

    # -- diagnostics ---------------------------------------------------------
    def stability(self) -> dict:
        """Global stability: eigenvalues of the stacked companion matrix (N*K*p)."""
        modulus = np.abs(self._eigenvalues)
        maximum = float(modulus.max()) if modulus.size else 0.0
        order = np.argsort(-modulus, kind="stable")
        return {
            "stable": bool(maximum < 1.0),
            "max_modulus": maximum,
            "dimension": int(self._global.shape[0]),
            "eigenvalues": [
                {"real": round(float(self._eigenvalues[i].real), ROUND_DIGITS),
                 "imag": round(float(self._eigenvalues[i].imag), ROUND_DIGITS)}
                for i in order],
        }

    def is_stable(self) -> bool:
        return bool(np.abs(self._eigenvalues).max() < 1.0)

    @property
    def stable(self) -> bool:
        return self.is_stable()

    def residual_covariance(self) -> np.ndarray:
        return self._covariance.copy()

    def _structural_matrix(self) -> np.ndarray:
        """Lower Cholesky factor of the global residual covariance (deterministic fallback)."""
        covariance = (self._covariance + self._covariance.T) / 2.0
        try:
            return np.linalg.cholesky(covariance)
        except np.linalg.LinAlgError:
            values, vectors = np.linalg.eigh(covariance)
            values = np.clip(values, 0.0, None)
            return vectors @ np.diag(np.sqrt(values))

    # -- IRF -----------------------------------------------------------------
    def irf(self, shock_country: str, shock_var: str, horizons: int = 12,
            shock_size: float = 1.0, method: str = "unit") -> dict:
        """Impulse response of every series to a shock, on the global companion form.

        ``method="unit"``: a one-unit increase in ``shock_var`` of ``shock_country`` (h=0 own
        response is exactly ``shock_size``).  ``method="cholesky"``: a one-standard-deviation
        structural shock, identified by the lower Cholesky factor of the global residual covariance.
        """
        if shock_country not in self.countries:
            raise GVARError(f"unknown shock country {shock_country!r}")
        if shock_var not in self.variables:
            raise GVARError(f"unknown shock variable {shock_var!r}")
        if isinstance(horizons, bool) or not isinstance(horizons, int) or horizons < 0:
            raise GVARError("horizons must be a non-negative integer")
        if method not in ("unit", "cholesky"):
            raise GVARError("method must be 'unit' or 'cholesky'")
        size = float(shock_size)
        if not np.isfinite(size):
            raise GVARError("shock_size must be finite")
        block = len(self.countries) * self.k
        state = np.zeros(block * self.lags, dtype=float)
        position = self.countries.index(shock_country) * self.k + self.variables.index(shock_var)
        if method == "unit":
            state[position] = size
        else:
            state[:block] = self._structural_matrix()[:, position] * size
        response: dict = {}
        for country in self.countries:
            offset = self.countries.index(country) * self.k
            response[country] = {variable: [] for variable in self.variables}
            for index, variable in enumerate(self.variables):
                response[country][variable].append(float(state[offset + index]))
        current = state
        for _ in range(horizons):
            current = self._global @ current
            for country in self.countries:
                offset = self.countries.index(country) * self.k
                for index, variable in enumerate(self.variables):
                    response[country][variable].append(float(current[offset + index]))
        return {
            "method": method,
            "shock_country": shock_country,
            "shock_var": shock_var,
            "shock_size": size,
            "horizons": list(range(horizons + 1)),
            "response": response,
            "note": ("unit shock to the observed series" if method == "unit" else
                     "one-sd structural shock, lower Cholesky, countries in dataset order"),
        }

    # -- forecast ------------------------------------------------------------
    def forecast(self, steps: int = 4) -> dict:
        """Deterministic h=1..``steps`` forecast paths for every country and variable."""
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise GVARError("steps must be a positive integer")
        p, k = self.lags, self.k
        # history[c][-lag] is y_{c, t-lag+1}: [-1] = y_t, [-2] = y_{t-1}, ...
        history = {c: [self._Y[c][self.n - lag, :].copy() for lag in range(p, 0, -1)]
                   for c in self.countries}
        path = {c: {v: [] for v in self.variables} for c in self.countries}
        for _ in range(steps):
            updated = {}
            for country in self.countries:
                beta = self.coefficients[country]
                value = beta[:, 0].copy()
                for lag in range(1, p + 1):
                    value = value + beta[:, 1 + (lag - 1) * k:1 + lag * k] @ \
                        history[country][-lag]
                for lag in range(1, p + 1):
                    factor = sum(
                        float(weight) * history[other][-lag]
                        for other, weight in self.weights[country].items())
                    value = value + beta[:, 1 + (p + lag - 1) * k:1 + (p + lag) * k] @ factor
                updated[country] = value
            for country in self.countries:
                history[country].append(updated[country])
                for index, variable in enumerate(self.variables):
                    path[country][variable].append(float(updated[country][index]))
        return {"steps": list(range(1, steps + 1)), "path": path,
                "lags": p, "variables": list(self.variables),
                "countries": list(self.countries)}

    # -- export --------------------------------------------------------------
    def _equation(self, country: str) -> dict:
        beta, k, p = self.coefficients[country], self.k, self.lags

        def block(column: int) -> list:
            matrix = beta[:, column:column + k]
            return [[round(float(value), ROUND_DIGITS) for value in row] for row in matrix]

        own = [block(1 + lag * k) for lag in range(p)]
        foreign = [block(1 + (p + lag) * k) for lag in range(p)]
        return {
            "intercept": [round(float(v), ROUND_DIGITS) for v in beta[:, 0]],
            "own_lags": own,
            "foreign_lags": foreign,
            "residual_std": [round(float(v), ROUND_DIGITS)
                             for v in np.std(self.residuals[country], axis=0, ddof=1)],
        }

    def export_coefficients(self) -> dict:
        """JSON-serialisable parameter export (no raw series, licence-safe)."""
        stability = self.stability()
        return {
            "model_id": MODEL_ID,
            "model_version": MODEL_VERSION,
            "equation": ("y_it = c_i + sum_l A_il y_i,t-l + sum_l B_il y*_i,t-l + e_it; "
                         "y*_it = sum_j!=i w_ij y_jt"),
            "lags": self.lags,
            "countries": list(self.countries),
            "variables": list(self.variables),
            "observations": self.n,
            "weights": {c: {o: round(float(w), ROUND_DIGITS) for o, w in row.items()}
                        for c, row in self.weights.items()},
            "equations": {c: self._equation(c) for c in self.countries},
            "stability": {"stable": stability["stable"],
                          "max_modulus": round(stability["max_modulus"], ROUND_DIGITS),
                          "dimension": stability["dimension"]},
            "identification": {
                "estimation": "equation-by-equation OLS",
                "foreign_factor_exogeneity": "weak exogeneity (small open economy assumption)",
                "structural_irf": ("lower Cholesky of the global residual covariance; order = "
                                   "countries in dataset order, variables sorted"),
                "cross_country_restrictions": "none",
            },
            "caveats": [
                "prototype only: not connected to the admission protocol, no real-data result",
                "trade weights are an input (MRIO scheme), not estimated; time-invariant here",
                "no financial stocks, no exchange rates, no cointegration, no PIT vintages",
                "OLS inference is not robust to serial correlation; only point estimates exported",
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.export_coefficients(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


__all__ = ["GVAR", "GVARError", "MODEL_ID", "MODEL_VERSION", "normalize_weights",
           "validate_dataset"]
