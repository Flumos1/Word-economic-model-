#!/usr/bin/env python3
"""D4 alternative dynamics: BVAR (Minnesota prior), factor model (PCA + VAR), local projections.

Research track only: this tool is deliberately separate from ``tools/gvar_real.py`` (whose numbers
are frozen, docs/128) and is not connected to admission, the registry or candidates.  It estimates
three alternative dynamic specifications on the *same* panel/windows as the GVAR study (WDI annual
1995-2024, OOS origins 2010-2023, h=1..4, aggregates US/EA/CN/WORLD and all countries), compares
RMSE/MAE with AR(1)/RW and with the legacy GVAR numbers from docs/128, and reports US -> WORLD/EA/CN
impulse responses (sign/decay).

Models
------
* ``BVAR_<p>_l<lambda>``: per-country bivariate VAR(p) on (real GDP growth, CPI inflation) with the
  Litterman/Minnesota prior: own first lag prior mean 1, all other coefficients prior mean 0, prior
  sd ``(lambda / l**2) * sigma_i / sigma_j`` (sigma from univariate AR(1) residuals), diffuse
  intercept. p in {1,2}, lambda in {0.1,0.2,0.5}. Country forecasts are aggregated with the same
  GDP shares as the GVAR study.
* ``FACTOR_q1..q3``: PCA on the standardised country x variable panel (q=1..3 factors, training
  window only), VAR(1) with intercept on the factors, recursive forecast, map back through the
  loadings and un-standardise.
* ``LP``: Jordà local projections, h=1..4, per country and variable: y_{t+h} on a constant,
  y_t (g, pi) and y_{t-1} (g, pi); forecasts from the last training observation.
* ``AR1``/``RW``: univariate AR(1) and random walk (the same benchmarks as the GVAR study).

IRF: BVAR on the 4-group core system (US/EA/CN/WORLD x g,pi, Cholesky ordering with US g first,
normalised to +1 p.p. US growth at h=0), factor model (structural shock to factor 1, normalised by
the implied US growth response), and local projections (coefficient on US growth).  The legacy GVAR
IRF from docs/128 is included as a reference (not recomputed).

Fail-closed: missing cached WDI payloads (this tool never downloads), non-finite/short panels,
unknown model/lambda/factor counts, degenerate standard deviations and too few observations raise
:class:`GVARAltError` instead of silently shrinking the sample.

Usage::

    $env:AMFE_MRIO_ROOT = "F:\\Мировая экономика модель\\data\\mrio"
    python tools/gvar_alt.py --origins 15 --max-h 4
    python tools/gvar_alt.py --help
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import gvar_real as legacy  # noqa: E402  (read-only reuse: panel/weights/benchmarks)

MRIO_ROOT = Path(os.environ.get("AMFE_MRIO_ROOT") or (ROOT / "data" / "mrio"))
MIN_TRAIN = 12
MAX_H = 4
LAMBDAS = (0.1, 0.2, 0.5)
FACTOR_COUNTS = (1, 2, 3)
BVAR_LAG_PRIMARY = 1
BVAR_LAMBDA_PRIMARY = 0.2
MODEL_NAMES = (
    "BVAR_p1_l0.1", "BVAR_p1_l0.2", "BVAR_p1_l0.5", "BVAR_p2_l0.2",
    "FACTOR_q1", "FACTOR_q2", "FACTOR_q3", "LP", "AR1", "RW",
)
# Legacy GVAR_YEAR aggregate RMSE h=1/h=4 and IRF from docs/128 (reference, not recomputed).
LEGACY_GVAR_RMSE = {
    "US": {"g": {1: 2.4233, 4: 2.4384}, "pi": {1: 2.2058, 4: 2.1679}},
    "EA": {"g": {1: 3.1456, 4: 2.8016}, "pi": {1: 2.1326, 4: 2.4856}},
    "CN": {"g": {1: 2.9109, 4: 3.3777}, "pi": {1: 4.1101, 4: 4.3281}},
    "WORLD": {"g": {1: 2.5810, 4: 2.3484}, "pi": {1: 1.9064, 4: 2.3144}},
}
LEGACY_GVAR_IRF = {
    "horizons": list(range(9)),
    "US_g": [1.0, 0.9223, 0.7869, 0.6635, 0.5278, 0.4040, 0.2940, 0.2049, 0.1319],
    "WORLD_g": [0.0189, -0.0119, -0.0235, -0.0092, -0.0030, 0.0009, 0.0001, 0.0010, -0.0001],
}


class GVARAltError(RuntimeError):
    """Invalid data/configuration for the D4 study: do not silently continue."""


# ------------------------------------------------------------------------------------- data
def _mrio_root(data_root=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(os.environ.get("AMFE_MRIO_ROOT") or MRIO_ROOT)


def _read_cached_indicator(cache_dir: Path, code: str, countries) -> dict:
    path = Path(cache_dir) / f"wb_{code}.json"
    if not path.is_file():
        raise GVARAltError(
            f"cached WDI payload {path} is missing; this tool never downloads. Populate the cache "
            "once with `python tools/gvar_real.py` (or copy data/mrio/wb) and re-run")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GVARAltError(f"cannot read cached WDI payload {path}: {exc!r}") from exc
    if not isinstance(document, dict) or "payload" not in document:
        raise GVARAltError(f"cached WDI payload {path} has no payload")
    return legacy.parse_payload(document["payload"], countries=countries)


def load_panel(data_root=None, *, cache_dir=None, countries=legacy.COUNTRIES,
               start: int = legacy.START_YEAR, end: int = legacy.END_YEAR) -> dict:
    """Read the cached WDI panel (offline, fail-closed); never downloads."""
    root = _mrio_root(data_root)
    directory = Path(cache_dir) if cache_dir is not None else root / "wb"
    panel = {name: _read_cached_indicator(directory, code, countries)
             for name, code in legacy.INDICATORS.items()}
    return panel


def panel_series(panel: dict, countries, years) -> dict:
    """Balanced ``{country: {"g": array, "pi": array}}`` from the panel."""
    out = {}
    for code in countries:
        growth = [panel["gdp_growth"].get(code, {}).get(year) for year in years]
        inflation = [panel["inflation"].get(code, {}).get(year) for year in years]
        if any(value is None for value in growth + inflation):
            raise GVARAltError(f"incomplete WDI series for {code}: the panel is not balanced")
        g = np.asarray(growth, dtype=float)
        pi = np.asarray(inflation, dtype=float)
        if not (np.all(np.isfinite(g)) and np.all(np.isfinite(pi))):
            raise GVARAltError(f"non-finite WDI values for {code}")
        out[code] = {"g": g, "pi": pi}
    return out


def aggregate(series: dict, share: dict, variable: str) -> np.ndarray:
    """GDP-weighted aggregate of one variable over a group."""
    total = None
    for code, weight in share.items():
        values = np.asarray(series[code][variable], dtype=float)
        total = weight * values if total is None else total + weight * values
    if total is None:
        raise GVARAltError("empty group share")
    return total


# ---------------------------------------------------------------------------- estimates
def _ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    beta, *_ = np.linalg.lstsq(np.asarray(x, dtype=float), np.asarray(y, dtype=float), rcond=None)
    return np.asarray(beta, dtype=float)


def _ar1_sigma(y: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    if y.size < 4:
        raise GVARAltError("at least four observations are needed for the AR(1) sigma estimate")
    x = np.column_stack([np.ones(y.size - 1), y[:-1]])
    beta = _ols(x, y[1:])
    residual = y[1:] - x @ beta
    sigma = float(np.std(residual, ddof=1))
    if not math.isfinite(sigma) or sigma <= 0.0:
        raise GVARAltError("degenerate AR(1) residual variance; cannot build the Minnesota prior")
    return sigma


def _companion_max_modulus(coefficients: np.ndarray, k: int, lags: int) -> float:
    """Companion matrix of a VAR in ``[c, A_1..A_p]`` (k x (1+k*p)) layout."""
    companion = np.zeros((k * lags, k * lags), dtype=float)
    for lag in range(lags):
        companion[0:k, lag * k:(lag + 1) * k] = coefficients[:, 1 + lag * k:1 + (lag + 1) * k]
    if lags > 1:
        companion[k:, :-k] = np.eye(k * (lags - 1))
    return float(np.abs(np.linalg.eigvals(companion)).max())


def fit_minnesota_var(data: np.ndarray, lags: int = 1, lam: float = 0.2) -> dict:
    """Bivariate VAR(p) with the Litterman/Minnesota prior, one country (or the core aggregate)."""
    y = np.asarray(data, dtype=float)
    if y.ndim != 2:
        raise GVARAltError("data must be a 2-D array (observations x variables)")
    rows, k = y.shape
    lags = int(lags)
    if lags < 1:
        raise GVARAltError("lags must be >= 1")
    if lags not in (1, 2):
        raise GVARAltError("only lags 1..2 are used by this study")
    if not (0.0 < float(lam) < 10.0):
        raise GVARAltError("lambda must be positive and finite")
    parameters = 1 + k * lags
    if rows - lags < parameters + 2:
        raise GVARAltError(
            f"too few observations for BVAR({lags}) with {k} variables: {rows} "
            f"(need more than {parameters + 2})")
    design = [np.ones((rows - lags, 1))]
    for lag in range(1, lags + 1):
        design.append(y[lags - lag:rows - lag, :])
    x = np.column_stack(design)
    target = y[lags:, :]
    sigmas = np.array([_ar1_sigma(y[:, index]) for index in range(k)])
    n_params = x.shape[1]
    beta = np.zeros((k, n_params), dtype=float)
    for equation in range(k):
        prior_mean = np.zeros(n_params)
        prior_var = np.full(n_params, 1e8)  # diffuse intercept
        for lag in range(1, lags + 1):
            for variable in range(k):
                position = 1 + (lag - 1) * k + variable
                prior_mean[position] = 1.0 if (variable == equation and lag == 1) else 0.0
                scale = (float(lam) / (lag ** 2)) * (sigmas[equation] / sigmas[variable])
                prior_var[position] = max(scale ** 2, 1e-12)
        sigma2 = sigmas[equation] ** 2
        precision = (x.T @ x) / sigma2 + np.diag(1.0 / prior_var)
        rhs = (x.T @ target[:, equation]) / sigma2 + prior_mean / prior_var
        beta[equation] = np.linalg.solve(precision, rhs)
    fitted = x @ beta.T
    residual = target - fitted
    return {"coefficients": beta, "lags": lags, "lam": float(lam), "k": k,
            "sigmas": sigmas, "residual": residual,
            "residual_cov": (residual.T @ residual) / max(residual.shape[0] - beta.shape[1], 1),
            "max_modulus": _companion_max_modulus(beta, k, lags),
            "observations": int(rows), "prior": "LITTERMAN_MINNESOTA"}


def bvar_forecast(model: dict, history: np.ndarray, steps: int) -> dict:
    """Recursive forecast of a fitted Minnesota VAR from the last ``lags`` observations."""
    y = np.asarray(history, dtype=float)
    lags, k = model["lags"], model["k"]
    if y.ndim != 2 or y.shape[0] < lags:
        raise GVARAltError("history must contain at least p observations")
    if steps < 1:
        raise GVARAltError("steps must be positive")
    beta = model["coefficients"]
    window = [y[-lag].copy() for lag in range(1, lags + 1)]  # [-1] = y_t
    path = []
    for _ in range(steps):
        value = beta[:, 0].copy()
        for lag in range(1, lags + 1):
            value = value + beta[:, 1 + (lag - 1) * k:1 + lag * k] @ window[lag - 1]
        path.append(value)
        window = [value] + window[:-1]
    return {"path": np.asarray(path, dtype=float)}


def fit_factor_model(data: np.ndarray, factors: int, *, lags: int = 1) -> dict:
    """PCA factor model with a VAR(lags) on the factors; ``data`` is the standardised panel."""
    z = np.asarray(data, dtype=float)
    if z.ndim != 2:
        raise GVARAltError("standardised panel must be 2-D")
    rows, series = z.shape
    factors = int(factors)
    if factors < 1 or factors > min(3, series):
        raise GVARAltError("factors must be in 1..3 and not exceed the number of series")
    if lags != 1:
        raise GVARAltError("the factor VAR uses one lag in this study")
    if rows - lags < 4:
        raise GVARAltError("too few observations for the factor VAR")
    # deterministic PCA: right singular vectors of the training panel
    _, singular, vt = np.linalg.svd(z, full_matrices=False)
    loadings = vt[:factors, :]                     # (q, N)
    factor_values = z @ loadings.T                 # (T, q)
    x = np.column_stack([np.ones(rows - lags), factor_values[:-lags, :]])
    y = factor_values[lags:, :]
    beta = _ols(x, y)
    residual = y - x @ beta
    covariance = (residual.T @ residual) / max(residual.shape[0] - beta.shape[1], 1)
    companion = beta[1:, :].T
    max_modulus = float(np.abs(np.linalg.eigvals(companion)).max()) if companion.size else 0.0
    explained = float(np.sum(singular[:factors] ** 2) / np.sum(singular ** 2))
    return {"loadings": loadings, "factor_values": factor_values, "beta": beta, "lags": lags,
            "factors": factors, "residual_cov": covariance, "max_modulus": max_modulus,
            "explained_variance": explained, "observations": int(rows)}


def factor_forecast(model: dict, last_row: np.ndarray, steps: int) -> dict:
    """Forecast the standardised panel from the last standardised observation row."""
    row = np.asarray(last_row, dtype=float).reshape(1, -1)
    factors = model["factors"]
    beta = model["beta"]
    history = [(row @ model["loadings"].T)[0]]  # 1-D factor vector, (q,)
    path = []
    for _ in range(steps):
        value = beta[0].copy()
        for lag in range(1, model["lags"] + 1):
            value = value + beta[lag] * history[lag - 1]
        path.append(value)
        history = [value] + history[:-1]
    factor_path = np.asarray(path, dtype=float)          # (steps, q)
    standardized = factor_path @ model["loadings"]       # (steps, N)
    return {"standardized": standardized, "factors": factor_path}


def local_projection_forecast(series: dict, steps: int, *, lags: int = 1) -> dict:
    """Jordà local projections: direct h-step forecasts of g and pi from current/past values."""
    if steps < 1:
        raise GVARAltError("steps must be positive")
    out = {}
    for variable in ("g", "pi"):
        target = np.asarray(series[variable], dtype=float)
        growth = np.asarray(series["g"], dtype=float)
        inflation = np.asarray(series["pi"], dtype=float)
        if target.size < lags + 2:
            raise GVARAltError("too few observations for local projections")
        forecast = []
        for horizon in range(1, steps + 1):
            rows = []
            label = []
            for t in range(lags, target.size - horizon):
                rows.append([1.0] + [growth[t - lag] for lag in range(lags)]
                            + [inflation[t - lag] for lag in range(lags)])
                label.append(target[t + horizon])
            if len(label) < 3:
                raise GVARAltError(f"local projection h={horizon} has too few training rows")
            beta = _ols(np.asarray(rows), np.asarray(label))
            x_last = ([1.0] + [growth[-1 - lag] for lag in range(lags)]
                      + [inflation[-1 - lag] for lag in range(lags)])
            forecast.append(float(np.asarray(x_last) @ beta))
        out[variable] = forecast
    return out


def lp_irf(us_growth: np.ndarray, target_growth: np.ndarray, horizons: int = 8) -> list:
    """LP impulse response of target growth to a +1 p.p. US growth shock (h=0 is 1 by construction)."""
    us = np.asarray(us_growth, dtype=float)
    target = np.asarray(target_growth, dtype=float)
    if us.size != target.size or us.size < 6:
        raise GVARAltError("US and target series must have the same length and >= 6 observations")
    responses = [1.0]
    for horizon in range(1, horizons + 1):
        rows, label = [], []
        for t in range(1, target.size - horizon):
            rows.append([1.0, us[t], us[t - 1], target[t - 1]])
            label.append(target[t + horizon])
        if len(label) < 4:
            raise GVARAltError("too few rows for the local-projection IRF")
        beta = _ols(np.asarray(rows), np.asarray(label))
        responses.append(float(beta[1]))
    return [round(value, 6) for value in responses]


# ------------------------------------------------------------------------------- OOS engine
def _model_bvar_names(lambdas, lags_list) -> list:
    return [f"BVAR_p{lags}_l{lam:g}" for lags in lags_list for lam in lambdas]


def rolling_oos(panel: dict, countries, years, shares: dict, *, max_h: int = MAX_H,
                n_origins: int = 15, lambdas=LAMBDAS, factor_counts=FACTOR_COUNTS) -> dict:
    """Expanding-window OOS RMSE/MAE for the alternative models and the AR(1)/RW benchmarks."""
    series = panel_series(panel, countries, years)
    total = len(years)
    if total < MIN_TRAIN + max_h:
        raise GVARAltError(f"sample of {total} years is too short for OOS h<= {max_h}")
    groups = list(shares)
    origins = list(range(max(MIN_TRAIN, total - int(n_origins)), total - 1))
    if not origins:
        raise GVARAltError("no OOS origin: the sample is too short")
    lags_list = (1, 2)
    bvar_names = _model_bvar_names(lambdas, lags_list)
    factor_names = [f"FACTOR_q{q}" for q in factor_counts]
    models = bvar_names + factor_names + ["LP", "AR1", "RW"]
    errors = {model: {group: {variable: {h: [] for h in range(1, max_h + 1)}
                              for variable in ("g", "pi")} for group in groups}
              for model in models}
    country_errors = {model: {code: {variable: {h: [] for h in range(1, max_h + 1)}
                                     for variable in ("g", "pi")} for code in countries}
                      for model in models}
    diagnostics = {model: {"fits": 0, "skipped": 0, "errors": []} for model in models}
    for origin in origins:
        train_slice = slice(0, origin + 1)
        train = {code: {"g": series[code]["g"][train_slice],
                        "pi": series[code]["pi"][train_slice]} for code in countries}
        forecasts = {model: {} for model in models}
        # per-country models
        for code in countries:
            y_train = np.column_stack([train[code]["g"], train[code]["pi"]])
            history = y_train.copy()
            for lags in lags_list:
                for lam in lambdas:
                    name = f"BVAR_p{lags}_l{lam:g}"
                    try:
                        model = fit_minnesota_var(y_train, lags=lags, lam=lam)
                        path = bvar_forecast(model, history, max_h)["path"]
                    except GVARAltError as exc:
                        diagnostics[name]["skipped"] += 1
                        diagnostics[name]["errors"].append(f"origin {int(years[origin])}/{code}: {exc}")
                        continue
                    diagnostics[name]["fits"] += 1
                    forecasts[name].setdefault(code, {})
                    forecasts[name][code]["g"] = [float(v) for v in path[:, 0]]
                    forecasts[name][code]["pi"] = [float(v) for v in path[:, 1]]
            # local projections (per country, direct h-step regressions)
            try:
                lp = local_projection_forecast(train[code], max_h)
                forecasts["LP"].setdefault(code, {})
                forecasts["LP"][code]["g"] = lp["g"]
                forecasts["LP"][code]["pi"] = lp["pi"]
                diagnostics["LP"]["fits"] += 1
            except GVARAltError as exc:
                diagnostics["LP"]["skipped"] += 1
                diagnostics["LP"]["errors"].append(f"origin {int(years[origin])}/{code}: {exc}")
        # factor models: standardise and fit once per origin (all countries jointly)
        means = np.array([train[inner_code][inner_variable].mean()
                          for inner_code in countries for inner_variable in ("g", "pi")])
        stds = np.array([train[inner_code][inner_variable].std(ddof=0)
                         for inner_code in countries for inner_variable in ("g", "pi")])
        if np.any(stds <= 0.0) or not np.all(np.isfinite(stds)):
            raise GVARAltError("degenerate series variance in the factor training window")
        z_train = np.column_stack([
            (train[inner_code][inner_variable] - means[index]) / stds[index]
            for index, (inner_code, inner_variable) in enumerate(
                (c, v) for c in countries for v in ("g", "pi"))])
        last_row = np.array([
            (train[inner_code][inner_variable][-1] - means[index]) / stds[index]
            for index, (inner_code, inner_variable) in enumerate(
                (c, v) for c in countries for v in ("g", "pi"))])
        for factors in factor_counts:
            name = f"FACTOR_q{factors}"
            try:
                model = fit_factor_model(z_train, factors)
                standardized = factor_forecast(model, last_row, max_h)["standardized"]
            except (GVARAltError, np.linalg.LinAlgError) as exc:
                diagnostics[name]["skipped"] += 1
                diagnostics[name]["errors"].append(f"origin {int(years[origin])}: {exc}")
                continue
            diagnostics[name]["fits"] += 1
            for index, (inner_code, inner_variable) in enumerate(
                    (c, v) for c in countries for v in ("g", "pi")):
                forecasts[name].setdefault(inner_code, {})
                forecasts[name][inner_code][inner_variable] = [
                    float(standardized[step, index] * stds[index] + means[index])
                    for step in range(max_h)]
        # benchmarks (same as the GVAR study)
        for code in countries:
            for variable in ("g", "pi"):
                values = train[code][variable]
                forecasts["AR1"].setdefault(code, {})[variable] = legacy.ar1_forecast(values, max_h)
                forecasts["RW"].setdefault(code, {})[variable] = [float(values[-1])] * max_h
        for model in models:
            if model in ("AR1", "RW"):
                diagnostics[model]["fits"] += 1
        # aggregate to groups and collect errors
        for group, share in shares.items():
            for h in range(1, max_h + 1):
                if origin + h >= total:
                    continue
                for variable in ("g", "pi"):
                    actual = math.fsum(weight * float(series[code][variable][origin + h])
                                       for code, weight in share.items())
                    for model in models:
                        forecasts_model = forecasts.get(model) or {}
                        if not forecasts_model or any(code not in forecasts_model
                                                      for code in share):
                            continue
                        forecast = math.fsum(
                            weight * float(forecasts_model[code][variable][h - 1])
                            for code, weight in share.items())
                        errors[model][group][variable][h].append(forecast - actual)
                        for code, weight in share.items():
                            country_errors[model][code][variable][h].append(
                                float(forecasts_model[code][variable][h - 1])
                                - float(series[code][variable][origin + h]))
    metrics = {model: {group: {variable: {h: {
        "rmse": legacy._rmse(errors[model][group][variable][h]),
        "mae": legacy._mae(errors[model][group][variable][h]),
        "n": len(errors[model][group][variable][h])}
        for h in range(1, max_h + 1)} for variable in ("g", "pi")} for group in groups}
        for model in models}
    per_country = {model: {code: {variable: {
        h: {"rmse": legacy._rmse(country_errors[model][code][variable][h]),
            "mae": legacy._mae(country_errors[model][code][variable][h]),
            "n": len(country_errors[model][code][variable][h])}
        for h in range(1, max_h + 1)} for variable in ("g", "pi")}
        for code in countries} for model in models}
    return {"origins": [int(years[index]) for index in origins], "origins_index": origins,
            "models": models, "metrics": metrics, "per_country": per_country,
            "diagnostics": diagnostics,
            "groups": {name: sorted(share) for name, share in shares.items()},
            "group_weights": {name: {code: round(weight, 12) for code, weight in share.items()}
                              for name, share in shares.items()},
            "timeline": {"min_train": MIN_TRAIN, "max_h": int(max_h),
                         "n_origins": len(origins),
                         "first_origin": int(years[origins[0]]),
                         "last_origin": int(years[origins[-1]])}}


def compare(oos: dict, reference: str = "AR1") -> dict:
    """Aggregate/per-country RMSE win counters of every model against a reference."""
    metrics = oos["metrics"]
    groups = [group for group in ("US", "EA", "CN", "WORLD") if group in metrics[reference]]
    horizons = sorted({int(h) for h in metrics[reference][groups[0]]["g"]})
    out = {}
    for model in oos["models"]:
        if model == reference:
            continue
        cells = better = worse = tie = 0
        delta_sum = 0.0
        for group in groups:
            for variable in ("g", "pi"):
                for h in horizons:
                    first = float(metrics[model][group][variable][h]["rmse"])
                    second = float(metrics[reference][group][variable][h]["rmse"])
                    delta = first - second
                    cells += 1
                    delta_sum += delta
                    if delta < -1e-12:
                        better += 1
                    elif delta > 1e-12:
                        worse += 1
                    else:
                        tie += 1
        out[model] = {"better_cells": better, "worse_cells": worse, "ties": tie,
                      "cells": cells, "mean_delta_rmse": round(delta_sum / cells, 6)}
    return out


# ------------------------------------------------------------------------------------ IRF
def _cholesky(covariance: np.ndarray) -> np.ndarray:
    matrix = (np.asarray(covariance, dtype=float) + np.asarray(covariance, dtype=float).T) / 2.0
    try:
        return np.linalg.cholesky(matrix)
    except np.linalg.LinAlgError:
        scale = float(np.trace(matrix) / max(matrix.shape[0], 1)) or 1.0
        for jitter in (1e-10, 1e-8, 1e-6, 1e-4):
            try:
                return np.linalg.cholesky(matrix + jitter * scale * np.eye(matrix.shape[0]))
            except np.linalg.LinAlgError:
                continue
        raise GVARAltError("residual covariance is not positive definite even after jitter")


def _var_companion(coefficients: np.ndarray, k: int, lags: int) -> np.ndarray:
    companion = np.zeros((k * lags, k * lags), dtype=float)
    for lag in range(lags):
        companion[0:k, lag * k:(lag + 1) * k] = coefficients[:, 1 + lag * k:1 + (lag + 1) * k]
    if lags > 1:
        companion[k:, :-k] = np.eye(k * (lags - 1))
    return companion


def bvar_core_irf(core: np.ndarray, *, lags: int = 2, lam: float = BVAR_LAMBDA_PRIMARY,
                  horizons: int = 8, shock_index: int = 0) -> dict:
    """Cholesky IRF of the 4-group core BVAR, normalised to +1 pp on the shocked variable at h=0."""
    model = fit_minnesota_var(core, lags=lags, lam=lam)
    impact = _cholesky(model["residual_cov"])[:, shock_index]
    companion = _var_companion(model["coefficients"], model["k"], lags)
    state = np.zeros(model["k"] * lags)
    state[:model["k"]] = impact
    responses = [state[:model["k"]].copy()]
    current = state
    for _ in range(horizons):
        current = companion @ current
        responses.append(current[:model["k"]].copy())
    matrix = np.asarray(responses, dtype=float)
    scale = matrix[0, shock_index]
    if abs(scale) < 1e-12:
        raise GVARAltError("degenerate Cholesky impact; cannot normalise the IRF")
    return {"matrix": matrix / scale, "scale": float(scale), "max_modulus": model["max_modulus"]}


def factor_irf(panel_series: dict, shares: dict, *, factors: int = 2, horizons: int = 8,
               us_code: str = "USA") -> dict:
    """Factor-model IRF: structural factor shock normalised to +1 pp US growth at h=0."""
    countries = list(shares.get("WORLD") or []) or list(panel_series)
    means, stds, columns, names = [], [], [], []
    for code in countries:
        for variable in ("g", "pi"):
            values = np.asarray(panel_series[code][variable], dtype=float)
            means.append(float(values.mean()))
            stds.append(float(values.std(ddof=0)))
            columns.append(values)
            names.append((code, variable))
    if any(std <= 0.0 for std in stds):
        raise GVARAltError("degenerate series variance in the factor panel")
    z = np.column_stack([(column - mean) / std for column, mean, std
                         in zip(columns, means, stds)])
    model = fit_factor_model(z, factors)
    impact = _cholesky(model["residual_cov"])[:, 0]
    companion = np.asarray(model["beta"][1:, :], dtype=float).T
    factor_state = impact.copy()
    factor_responses = [factor_state.copy()]
    for _ in range(horizons):
        factor_state = companion @ factor_state
        factor_responses.append(factor_state.copy())
    standardized = np.asarray(factor_responses) @ model["loadings"]       # (H+1, N)
    unstandardised = standardized * np.asarray(stds)[None, :] + np.asarray(means)[None, :]
    position = {name: index for index, name in enumerate(names)}
    us_index = position.get((us_code, "g"))
    if us_index is None:
        raise GVARAltError(f"US growth is not in the factor panel ({us_code!r})")
    scale = unstandardised[0, us_index]
    if abs(scale) < 1e-12:
        raise GVARAltError("degenerate US response in the factor IRF; cannot normalise")
    group_paths = {}
    for group in ("US", "EA", "CN", "WORLD"):
        share = shares.get(group) or {}
        if not share:
            continue
        growth = []
        for step in range(horizons + 1):
            growth.append(math.fsum(weight * float(unstandardised[step, position[(code, "g")]])
                                    for code, weight in share.items()))
        group_paths[group] = [round(value / scale, 6) for value in growth]
    return {"group_growth": group_paths, "scale": float(scale),
            "factors": factors, "explained_variance": model["explained_variance"],
            "max_modulus": model["max_modulus"]}


def build_report(*, origins: int = 15, max_h: int = MAX_H, data_root=None,
                 countries=legacy.COUNTRIES, start: int = legacy.START_YEAR,
                 end: int = legacy.END_YEAR, lambdas=LAMBDAS,
                 factor_counts=FACTOR_COUNTS) -> dict:
    root = _mrio_root(data_root)
    panel = load_panel(root)
    sample = legacy.choose_sample(panel, countries, start=start, end=end)
    kept, years = sample["countries"], sample["years"]
    groups = {"US": ["USA"], "EA": [code for code in legacy.EURO_AREA if code in kept],
              "CN": ["CHN"], "WORLD": kept}
    shares = legacy.group_weights(kept, years, panel, groups)
    series = panel_series(panel, kept, years)
    oos = rolling_oos(panel, kept, years, shares, max_h=max_h, n_origins=origins,
                      lambdas=lambdas, factor_counts=factor_counts)
    comparisons = {"vs_AR1": compare(oos, "AR1"), "vs_RW": compare(oos, "RW")}
    alternative_models = [model for model in oos["models"] if model not in ("AR1", "RW")]
    mean_rmse = {}
    for model in alternative_models:
        values = [oos["metrics"][model][group][variable][h]["rmse"]
                  for group in ("US", "EA", "CN", "WORLD")
                  for variable in ("g", "pi") for h in range(1, max_h + 1)]
        mean_rmse[model] = round(float(np.mean(values)), 6)
    best = min(mean_rmse, key=mean_rmse.get)
    # IRF: BVAR core, factor model, LP; US growth +1 p.p. (h=0..8)
    core_columns = []
    for group in ("US", "EA", "CN", "WORLD"):
        for variable in ("g", "pi"):
            core_columns.append(aggregate(series, shares[group], variable))
    core = np.column_stack(core_columns)
    core_names = [f"{group}_{variable}" for group in ("US", "EA", "CN", "WORLD")
                  for variable in ("g", "pi")]
    bvar_irf = bvar_core_irf(core, shock_index=0, horizons=8)
    irfs = {"horizons": list(range(9)), "shock": "US growth +1 p.p.; Cholesky / factor / LP",
            "BVAR_core_max_modulus": round(bvar_irf["max_modulus"], 6),
            "BVAR": {group: [round(float(value), 6) for value in
                             bvar_irf["matrix"][:, core_names.index(f"{group}_g")]]
                     for group in ("US", "EA", "CN", "WORLD")}}
    try:
        factor = factor_irf(series, shares, factors=2, horizons=8)
        irfs["FACTOR"] = factor["group_growth"]
        irfs["FACTOR_explained_variance"] = round(float(factor["explained_variance"]), 6)
    except GVARAltError as exc:
        irfs["FACTOR"] = {"error": str(exc)}
    lp_paths = {}
    for group in ("US", "EA", "CN", "WORLD"):
        target = aggregate(series, shares[group], "g")
        lp_paths[group] = lp_irf(aggregate(series, shares["US"], "g"), target, horizons=8)
    irfs["LP"] = lp_paths
    irfs["LEGACY_GVAR_docs128"] = LEGACY_GVAR_IRF
    report = {
        "report_id": "AMFE-GVAR-ALT-D4",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": ("World Bank WDI cached payloads (CC BY 4.0), read-only; the study never downloads "
                   "and never writes raw data; no MRIO tables are used by these models"),
        "sample": {"first_year": sample["first_year"], "last_year": sample["last_year"],
                   "years": sample["n_years"], "countries": sample["n_countries"],
                   "country_codes": kept},
        "specification": {
            "BVAR": "per-country VAR(p) on (g, pi) with Litterman/Minnesota prior; "
                    "p in {1,2}, lambda in " + str(list(lambdas)) + "; GDP-weighted aggregates",
            "FACTOR": "PCA on the standardised country x variable panel (q in "
                      + str(list(factor_counts)) + "), VAR(1) on factors, recursive forecast",
            "LP": "Jorda local projections h=1..4: y_{t+h} on const, g_t, pi_t, g_{t-1}, pi_{t-1}",
            "benchmarks": "AR(1) and random walk (same as the GVAR study)",
            "refit": "expanding window, per OOS origin, training data only",
        },
        "oos": oos,
        "comparisons": comparisons,
        "selection": {"mean_rmse_32_cells": mean_rmse, "best_alternative": best},
        "legacy_gvar_reference": {"source": "docs/128 (GVAR_YEAR; not recomputed)",
                                  "rmse_h1_h4": LEGACY_GVAR_RMSE},
        "irf": irfs,
        "limits": [
            "annual WDI data, at most 30 observations; OOS origins 2010-2023 use 16-27 years",
            "no point-in-time vintages: GDP/CPI revisions flatter every model equally",
            "reduced-form models; factor shock identification is a Cholesky normalisation, not a "
            "causal US shock; LP controls are parsimonious (one lag)",
            "BVAR Minnesota prior has a fixed lambda grid; no hyperparameter search on OOS",
            "aggregates are GDP-weighted country forecasts/aggregates, not separate equations",
            "research track only: not connected to admission, registry or candidates",
        ],
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps({key: value for key, value in report.items() if key != "report_sha256"},
                   ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report


def _summary(report: dict) -> dict:
    metrics = report["oos"]["metrics"]
    compact = {}
    for model in metrics:
        compact[model] = {}
        for group in ("US", "EA", "CN", "WORLD"):
            entry = {}
            for variable in ("g", "pi"):
                for h in (1, 4):
                    value = metrics[model][group][variable].get(h)
                    entry[f"{variable}_h{h}"] = (
                        {"rmse": round(value["rmse"], 6), "mae": round(value["mae"], 6)}
                        if value and math.isfinite(value["rmse"]) else None)
            compact[model][group] = entry
    return {"report_id": report["report_id"], "sample": report["sample"],
            "selection": report["selection"],
            "metrics": compact,
            "comparisons_vs_AR1": {model: value for model, value in
                                   report["comparisons"]["vs_AR1"].items()
                                   if model in ("BVAR_p1_l0.2", "FACTOR_q1", "FACTOR_q3", "LP",
                                                "BVAR_p2_l0.2")},
            "irf": report["irf"],
            "report_sha256": report["report_sha256"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="D4 alternative dynamics: BVAR (Minnesota), factor model (PCA+VAR), "
                    "local projections vs AR(1)/RW and the legacy GVAR (docs/128)")
    parser.add_argument("--data-root", default=None,
                        help="MRIO data root (default: AMFE_MRIO_ROOT or data/mrio); WDI cache is "
                             "<root>/wb")
    parser.add_argument("--origins", type=int, default=15, help="number of rolling OOS origins")
    parser.add_argument("--max-h", type=int, default=MAX_H, help="OOS horizon (default 4)")
    parser.add_argument("--lambdas", default="0.1,0.2,0.5", help="BVAR Minnesota lambdas")
    parser.add_argument("--factors", default="1,2,3", help="factor counts for the PCA model")
    parser.add_argument("--report", default=None,
                        help="report path (default: <data root>/cache/gvar_alt/gvar_alt_report.json)")
    args = parser.parse_args(argv)
    if args.origins < 2 or args.max_h < 1:
        raise SystemExit("--origins must be >= 2 and --max-h >= 1")
    try:
        lambdas = tuple(sorted({float(part) for part in args.lambdas.split(",") if part.strip()}))
        factors = tuple(sorted({int(part) for part in args.factors.split(",") if part.strip()}))
    except ValueError as exc:
        raise SystemExit(f"invalid --lambdas/--factors: {exc}") from exc
    if not lambdas or any(lam <= 0.0 for lam in lambdas):
        raise SystemExit("--lambdas must be a non-empty list of positive numbers")
    if not factors or any(count < 1 or count > 3 for count in factors):
        raise SystemExit("--factors must be a non-empty subset of 1,2,3")
    root = _mrio_root(args.data_root)
    report = build_report(origins=args.origins, max_h=args.max_h, data_root=root,
                          lambdas=lambdas, factor_counts=factors)
    path = Path(args.report) if args.report else root / "cache" / "gvar_alt" / "gvar_alt_report.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                        newline="\n")
    except OSError as exc:
        raise SystemExit(f"cannot write the report {path}: {exc}") from exc
    summary = _summary(report)
    summary["report"] = str(path)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
