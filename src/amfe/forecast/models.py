"""Executable AMFE baselines from ``protocols/r3_specification.json`` (models section).

Models
------
``RW``                 random walk, T1 only
``AO``                 Atkeson-Ohanian, T1 (window mean) and T2 (last four quarters)
``AR_BIC``             univariate autoregression, order by BIC, iterated forecasts
``BVAR_MINNESOTA``     small VAR with a diagonal Minnesota prior, iterated forecasts
``PROBIT_SPREAD``      Estrella-Mishkin yield-curve probit, T3
``SAHM_RULE``          Sahm indicator + logistic calibration, T3
``OWN_NOWCAST_BRIDGE`` project's own T2 h=0 nowcast: monthly PCA factors + bridge regression

Every distribution model returns a point forecast and the 19 protocol quantiles
(5..95 in 5% steps).  T3 models return an event probability only, as the protocol
requires (the 19-quantile representation does not apply to an event target).

No future data can enter a forecast: all inputs come from
:class:`amfe.forecast.data.DataContext`, whose constructor re-checks every slice against
the origin and raises :class:`~amfe.forecast.data.LookAheadError` on a violation.

Documented simplifications (also in ``docs/70_Реализация_базовых_моделей.md``)
--------------------------------------------------------------------------
* Density of RW/AO/AR_BIC is built from empirical h-step errors computed over the rolling
  window with the coefficients/means frozen at the origin (no per-origin refit inside the
  error loop).
* BVAR is a fixed-hyperparameter diagonal Minnesota prior (lambda=0.2) solved as ridge;
  the T2 VAR is estimated on annualised growth rather than the GDPC1 level.  The density
  is Gaussian with the rolling h-step error RMSE; with fewer than 20 errors the RW error
  quantiles are used and the forecast is flagged (protocol rule).
* T3 labels from USREC enter estimation only when the NBER announcement covering them was
  published before the origin (protocol v4 data_depth.nber_rule; dates from
  protocols/nber_dates.json).  A fixed 12-month lag is used only if that file is missing.
  T3 regressors use the last observation published at the origin, so in vintage mode they
  can be older than the origin month.
* OWN_NOWCAST_BRIDGE chooses 1-2 factors by explained variance (Bai-Ng stand-in), fills
  unpublished factor months with a factor AR(1), and its density uses in-sample bridge
  residuals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

import numpy as np

from .data import (
    MODE_AUTO,
    NBER_ANNOUNCEMENT_LAG_MONTHS,
    nber_label_cutoff,
    DataContext,
    ForecastDataError,
    Series,
    SeriesUnavailable,
    T1_SERIES,
    TARGET_IDS,
    TARGET_SPEC,
    align_series,
    cpi_yoy,
    gdp_growth,
    month_label,
    quarter_average_series,
    quarter_start_month,
    target_period,
    usrec_event,
)

QUANTILE_LEVELS: tuple[float, ...] = (
    0.05,
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
)
QGRID = np.asarray(QUANTILE_LEVELS, dtype=float)

MODEL_IDS: tuple[str, ...] = (
    "RW",
    "AO",
    "AR_BIC",
    "BVAR_MINNESOTA",
    "PROBIT_SPREAD",
    "SAHM_RULE",
    "OWN_NOWCAST_BRIDGE",
)

MODEL_TARGETS: dict[str, tuple[str, ...]] = {
    "RW": ("T1_CPI_YOY",),
    "AO": ("T1_CPI_YOY", "T2_GDP_GROWTH"),
    "AR_BIC": ("T1_CPI_YOY", "T2_GDP_GROWTH"),
    "BVAR_MINNESOTA": ("T1_CPI_YOY", "T2_GDP_GROWTH"),
    "PROBIT_SPREAD": ("T3_RECESSION",),
    "SAHM_RULE": ("T3_RECESSION",),
    "OWN_NOWCAST_BRIDGE": ("T2_GDP_GROWTH",),
}

MODEL_ESTIMATION_WINDOW = {
    "RW": 180,
    "AO": 180,
    "AR_BIC": 180,
    "BVAR_MINNESOTA": 180,
    "PROBIT_SPREAD": 240,
    "SAHM_RULE": 240,
    "OWN_NOWCAST_BRIDGE": 180,
}

MIN_OBSERVATIONS = {"T1_CPI_YOY": 60, "T2_GDP_GROWTH": 24, "T3_RECESSION": 60}
MIN_DENSITY_ERRORS = 20
BVAR_LAMBDA = 0.2
NORMAL = NormalDist()


class ForecastError(ForecastDataError):
    """A model cannot produce a valid forecast for the request."""


class InsufficientData(ForecastError):
    """Not enough history at the origin to estimate the model."""


@dataclass
class Forecast:
    """One model forecast for (target, horizon, origin)."""

    model_id: str
    target: str
    series_id: str
    origin: date
    horizon: int
    point: float | None
    quantiles: np.ndarray | None
    probability: float | None
    estimation_window: int
    estimation_unit: str
    effective_steps: int | None
    data_class: str
    vintage_date: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quantiles is not None:
            q = np.asarray(self.quantiles, dtype=float)
            if q.shape != (len(QUANTILE_LEVELS),):
                raise ForecastError(f"{self.model_id}: quantile vector must have 19 entries")
            if not np.all(np.isfinite(q)):
                raise ForecastError(f"{self.model_id}: non-finite quantile")
            if np.any(np.diff(q) < -1e-9):
                raise ForecastError(f"{self.model_id}: quantiles are not monotone")
            object.__setattr__(self, "quantiles", q)
        if self.probability is not None and not (0.0 <= float(self.probability) <= 1.0):
            raise ForecastError(f"{self.model_id}: probability outside [0, 1]")

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "target": self.target,
            "series_id": self.series_id,
            "origin": self.origin.isoformat(),
            "horizon": self.horizon,
            "point": None if self.point is None else float(self.point),
            "quantiles": None if self.quantiles is None else [float(v) for v in self.quantiles],
            "probability": None if self.probability is None else float(self.probability),
            "estimation_window": self.estimation_window,
            "estimation_unit": self.estimation_unit,
            "effective_steps": self.effective_steps,
            "data_class": self.data_class,
            "vintage_date": self.vintage_date,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def _finite_tail(series: Series, n: int) -> tuple[np.ndarray, np.ndarray]:
    index, values = series.finite_index_values()
    if index.size == 0:
        return index, values
    take = min(int(n), index.size)
    return index[-take:], values[-take:]


def _clipped_normal_quantiles(level: float, sd: float) -> float:
    if not np.isfinite(sd) or sd <= 0:
        sd = 1e-6
    return float(NORMAL.inv_cdf(level)) * sd


def _gaussian_quantiles(point: float, sd: float) -> np.ndarray:
    if not np.isfinite(sd) or sd <= 0:
        sd = max(abs(float(point)) * 0.01, 1e-6)
    return np.asarray([point + _clipped_normal_quantiles(q, sd) for q in QUANTILE_LEVELS], dtype=float)


def _quantiles_from_errors(point: float, errors) -> np.ndarray:
    arr = np.asarray([e for e in errors if np.isfinite(e)], dtype=float)
    if arr.size >= 5:
        q = np.quantile(arr, QGRID, method="linear")
    else:
        sd = float(np.std(arr)) if arr.size else max(abs(float(point)) * 0.01, 1e-6)
        q = np.asarray([_clipped_normal_quantiles(level, sd) for level in QUANTILE_LEVELS], dtype=float)
    out = point + np.asarray(q, dtype=float)
    out = np.maximum.accumulate(out)
    if not np.all(np.isfinite(out)):
        raise ForecastError("non-finite predictive quantiles")
    return out


def _h_step_errors_series(series: Series, steps: int, window: int) -> np.ndarray:
    """Empirical errors of the RW forecast at ``steps`` over the last ``window`` origins."""
    index, values = series.finite_index_values()
    position = {int(p): i for i, p in enumerate(index)}
    start = max(0, index.size - int(window))
    errors: list[float] = []
    for i in range(start, index.size):
        target_position = position.get(int(index[i]) + int(steps))
        if target_position is None:
            continue
        errors.append(float(values[target_position] - values[i]))
    return np.asarray(errors, dtype=float)


def _trailing_mean(values: dict[int, float], period: int, window: int, min_obs: int) -> float | None:
    sample = [values.get(period - offset) for offset in range(int(window))]
    finite = [float(v) for v in sample if v is not None and np.isfinite(v)]
    if len(finite) < min_obs:
        return None
    return float(np.mean(finite))


@dataclass
class _Prepared:
    """Target series, rolling window and calendar target for a T1/T2 point model."""

    target: str
    series_id: str
    series: Series
    window: int
    unit: str
    last: int
    target_period: int
    steps: int
    index: np.ndarray
    values: np.ndarray

    def map(self) -> dict[int, float]:
        return {int(p): float(v) for p, v in zip(self.index, self.values)}


def _prepare(ctx: DataContext, target: str, horizon: int, series_id: str | None) -> _Prepared:
    if target not in ("T1_CPI_YOY", "T2_GDP_GROWTH"):
        raise ForecastError(f"{target} is not a point/distribution target")
    if target == "T1_CPI_YOY":
        sid = series_id or "CPIAUCSL"
        if sid not in T1_SERIES:
            raise ForecastError(f"{sid} is not a T1 series")
        series = cpi_yoy(ctx.require(sid))
        window, unit = 180, "months"
    else:
        sid = series_id or "GDPC1"
        series = gdp_growth(ctx.require("GDPC1"))
        window, unit = 60, "quarters"
    last = series.last_index
    if last is None:
        raise InsufficientData(f"{target}: no observations at {ctx.origin.isoformat()}")
    _freq, period = target_period(target, ctx.origin, horizon)
    steps = int(period) - int(last)
    if steps < 1:
        raise ForecastError(
            f"{target} h={horizon}: target is not after the last published value "
            f"(target={period}, last={last})"
        )
    index, values = series.finite_index_values()
    if index.size < MIN_OBSERVATIONS[target]:
        raise InsufficientData(
            f"{target}: {index.size} observations at the origin, need {MIN_OBSERVATIONS[target]}"
        )
    take = min(int(window), index.size)
    return _Prepared(
        target=target,
        series_id=sid,
        series=series,
        window=window,
        unit=unit,
        last=int(last),
        target_period=int(period),
        steps=steps,
        index=index[-take:],
        values=values[-take:],
    )


def _finalize(
    ctx: DataContext,
    model_id: str,
    prepared: _Prepared,
    point: float,
    errors=None,
    sd: float | None = None,
    notes=(),
    horizon: int | None = None,
) -> Forecast:
    if errors is not None:
        quantiles = _quantiles_from_errors(point, errors)
    elif sd is not None:
        quantiles = _gaussian_quantiles(point, sd)
    else:  # pragma: no cover - defensive
        raise ForecastError(f"{model_id}: no predictive distribution inputs")
    return Forecast(
        model_id=model_id,
        target=prepared.target,
        series_id=prepared.series_id,
        origin=ctx.origin,
        horizon=horizon,  # type: ignore[arg-type]
        point=float(point),
        quantiles=quantiles,
        probability=None,
        estimation_window=prepared.window,
        estimation_unit=prepared.unit,
        effective_steps=prepared.steps,
        data_class=ctx.data_class,
        vintage_date=_vintage_label(ctx, prepared.series),
        notes=tuple(notes),
    )


def _vintage_label(ctx: DataContext, series: Series | None = None) -> str | None:
    if series is not None and series.vintage_date is not None:
        return series.vintage_date.isoformat()
    for meta in ctx.meta.values():
        if meta.vintage_date is not None:
            return meta.vintage_date.isoformat()
    return None


# --------------------------------------------------------------------------------------
# RW
# --------------------------------------------------------------------------------------
def rw(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Random walk: the last published target value is the point forecast."""
    if target not in MODEL_TARGETS["RW"]:
        raise ForecastError(f"RW does not apply to {target}")
    prepared = _prepare(ctx, target, horizon, series_id)
    values = prepared.map()
    point = values[prepared.last]
    errors = _h_step_errors_series(prepared.series, prepared.steps, prepared.window)
    return _finalize(ctx, "RW", prepared, point, errors=errors, notes=("empirical rolling h-step errors",), horizon=horizon)


# --------------------------------------------------------------------------------------
# AO
# --------------------------------------------------------------------------------------
def ao(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Atkeson-Ohanian: T1 window mean, T2 mean of the last four quarters."""
    if target not in MODEL_TARGETS["AO"]:
        raise ForecastError(f"AO does not apply to {target}")
    prepared = _prepare(ctx, target, horizon, series_id)
    values = prepared.map()
    window_length = 4 if target == "T2_GDP_GROWTH" else prepared.window
    min_obs = 4 if target == "T2_GDP_GROWTH" else 24
    if target == "T2_GDP_GROWTH":
        point = float(np.mean(prepared.values[-4:]))
    else:
        point = float(np.mean(prepared.values))
    errors: list[float] = []
    for period in prepared.index:
        forecast_value = _trailing_mean(values, int(period), window_length, min_obs)
        actual = values.get(int(period) + prepared.steps)
        if forecast_value is None or actual is None:
            continue
        errors.append(float(actual - forecast_value))
    return _finalize(ctx, "AO", prepared, point, errors=errors, notes=("empirical rolling h-step errors",), horizon=horizon)


# --------------------------------------------------------------------------------------
# AR_BIC
# --------------------------------------------------------------------------------------
def _ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    return beta


def _ar_design(values: np.ndarray, order: int) -> tuple[np.ndarray, np.ndarray]:
    n = values.size
    sample = values[order:]
    columns = [np.ones(sample.size)]
    for lag in range(1, order + 1):
        columns.append(values[order - lag : n - lag])
    return np.column_stack(columns), sample


def _ar_fit(values: np.ndarray, max_order: int) -> tuple[np.ndarray, int]:
    best: tuple[float, np.ndarray, int] | None = None
    n_total = values.size
    for order in range(0, max_order + 1):
        if n_total <= order + 5:
            continue
        x, y = _ar_design(values, order)
        beta = _ols(x, y)
        resid = y - x @ beta
        sse = float(resid @ resid)
        if not np.isfinite(sse) or sse <= 1e-12:
            continue
        n = y.size
        bic = n * math.log(sse / n) + x.shape[1] * math.log(n)
        if best is None or bic < best[0]:
            best = (bic, beta, order)
    if best is None:
        raise InsufficientData("AR_BIC: no admissible lag order on the rolling window")
    return best[1], best[2]


def _ar_forecast(beta: np.ndarray, order: int, history: np.ndarray, steps: int) -> float:
    if order == 0:
        return float(beta[0])
    buffer = [float(v) for v in history[-order:]]
    for _ in range(int(steps)):
        value = float(beta[0]) + sum(float(beta[lag + 1]) * buffer[-lag - 1] for lag in range(order))
        buffer.append(value)
    return buffer[-1]


def ar_bic(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Univariate AR, order by BIC (max 12 monthly / 4 quarterly), iterated forecasts."""
    if target not in MODEL_TARGETS["AR_BIC"]:
        raise ForecastError(f"AR_BIC does not apply to {target}")
    prepared = _prepare(ctx, target, horizon, series_id)
    max_order = 12 if target == "T1_CPI_YOY" else 4
    beta, order = _ar_fit(prepared.values, max_order)
    point = _ar_forecast(beta, order, prepared.values, prepared.steps)
    values = prepared.map()
    index_all = prepared.series.finite_index_values()[0]
    position = {int(p): i for i, p in enumerate(index_all)}
    errors: list[float] = []
    for period in prepared.index:
        i = position[int(period)]
        if i < order:
            continue
        history = prepared.series.finite_index_values()[1][max(0, i - order + 1) : i + 1]
        actual = values.get(int(period) + prepared.steps)
        if actual is None:
            continue
        errors.append(float(actual - _ar_forecast(beta, order, history, prepared.steps)))
    notes = ("order by BIC", f"AR order={order}")
    return _finalize(ctx, "AR_BIC", prepared, point, errors=errors, notes=notes, horizon=horizon)


# --------------------------------------------------------------------------------------
# BVAR_MINNESOTA
# --------------------------------------------------------------------------------------
def _var_prior(p: int, k: int) -> np.ndarray:
    """Minnesota prior mean matrix (ncol x k): unit root prior on each own first lag."""
    prior_mean = np.zeros((1 + p * k, k))
    if p == 0:
        return prior_mean
    for equation in range(k):
        # column of lag-1 coefficient of variable ``equation`` in its own equation
        prior_mean[1 + equation, equation] = 1.0
    return prior_mean


def _var_fit(y: np.ndarray, max_p: int, lam: float = BVAR_LAMBDA) -> tuple[np.ndarray, int]:
    """Fit a VAR by ridge (diagonal Minnesota prior); order by BIC."""
    observations, k = y.shape
    best: tuple[float, np.ndarray, int] | None = None
    for p in range(0, max_p + 1):
        n = observations - p
        if n <= 2 * k * p + 2:
            continue
        columns = [np.ones((n, 1))]
        for lag in range(1, p + 1):
            columns.append(y[p - lag : observations - lag, :])
        x = np.column_stack(columns)
        prior_mean = _var_prior(p, k)
        coef = np.zeros((x.shape[1], k))
        sse = np.zeros(k)
        for equation in range(k):
            penalty = np.zeros(x.shape[1])
            for lag in range(1, p + 1):
                for variable in range(k):
                    column = 1 + (lag - 1) * k + variable
                    own = variable == equation
                    sd = lam / lag if own else lam * 0.5 / lag
                    penalty[column] = 1.0 / (sd ** 2)
            a = x.T @ x + np.diag(penalty)
            b = x.T @ y[p:, equation] + penalty * prior_mean[:, equation]
            try:
                coef[:, equation] = np.linalg.solve(a, b)
            except np.linalg.LinAlgError:  # pragma: no cover - defensive
                coef[:, equation] = _ols(x, y[p:, equation])
            resid = y[p:, equation] - x @ coef[:, equation]
            sse[equation] = max(float(resid @ resid), 1e-12)
        bic = float(np.sum(n * np.log(sse / n))) + x.shape[1] * k * math.log(n)
        if best is None or bic < best[0]:
            best = (bic, coef, p)
    if best is None:
        raise InsufficientData("BVAR_MINNESOTA: no admissible lag order on the rolling window")
    return best[1], best[2]


def _var_forecast(coef: np.ndarray, p: int, k: int, history: np.ndarray, steps: int) -> np.ndarray:
    buffer = [np.asarray(row, dtype=float) for row in history[-p:]] if p else []
    for _ in range(int(steps)):
        row = [1.0]
        for lag in range(1, p + 1):
            row.extend(buffer[-lag].tolist())
        next_row = np.asarray(row, dtype=float) @ coef
        buffer.append(next_row)
    return buffer[-1]


def _var_errors(y: np.ndarray, coef: np.ndarray, p: int, k: int, steps: int) -> np.ndarray:
    observations = y.shape[0]
    errors: list[float] = []
    for t in range(observations - 1, p - 1, -1):
        if t + steps >= observations:
            continue
        history = y[t - p + 1 : t + 1] if p else np.zeros((0, k))
        forecast_value = _var_forecast(coef, p, k, history, steps)[0]
        errors.append(float(y[t + steps, 0] - forecast_value))
    return np.asarray(errors, dtype=float)


def _var_dataset(ctx: DataContext, target: str, series_id: str | None) -> tuple[np.ndarray, np.ndarray, Series, int, int]:
    """Aligned VAR panel, its period index (last row period), target series and steps."""
    if target == "T1_CPI_YOY":
        sid = series_id or "CPIAUCSL"
        if sid not in T1_SERIES:
            raise ForecastError(f"{sid} is not a T1 series")
        target_series = cpi_yoy(ctx.require(sid))
        others = [ctx.require("UNRATE"), ctx.require("GS10"), ctx.require("TB3MS")]
        index, vectors, _freq = align_series(target_series, *others)
        window, unit = 180, "months"
    else:
        target_series = gdp_growth(ctx.require("GDPC1"))
        others = [
            quarter_average_series(ctx.require("UNRATE")),
            quarter_average_series(ctx.require("GS10")),
            quarter_average_series(ctx.require("TB3MS")),
        ]
        index, vectors, _freq = align_series(target_series, *others)
        window, unit = 60, "quarters"
    if index.size < MIN_OBSERVATIONS[target] + 5:
        raise InsufficientData(f"BVAR_MINNESOTA: {index.size} aligned observations at the origin")
    take = min(window + 30, index.size)
    index = index[-take:]
    matrix = np.column_stack([vector[-take:] for vector in vectors])
    return matrix, index, target_series, window, unit


def bvar_minnesota(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Small VAR with a diagonal Minnesota prior, lag order by BIC, iterated forecast."""
    if target not in MODEL_TARGETS["BVAR_MINNESOTA"]:
        raise ForecastError(f"BVAR_MINNESOTA does not apply to {target}")
    matrix, index, target_series, window, unit = _var_dataset(ctx, target, series_id)
    max_p = 12 if target == "T1_CPI_YOY" else 4
    coef, order = _var_fit(matrix, max_p)
    _freq, period = target_period(target, ctx.origin, horizon)
    last = int(index[-1])
    steps = int(period) - last
    if steps < 1:
        raise ForecastError(f"BVAR_MINNESOTA: target is not after the last aligned observation")
    k = matrix.shape[1]
    point_vector = _var_forecast(coef, order, k, matrix, steps)
    point = float(point_vector[0])
    errors = _var_errors(matrix, coef, order, k, steps)
    notes = ["diagonal Minnesota prior (lambda=0.2)", f"VAR order={order}"]
    if errors.size >= MIN_DENSITY_ERRORS:
        sd = float(np.std(errors, ddof=1)) if errors.size > 1 else 0.0
    else:
        rw_errors = _h_step_errors_series(target_series, steps, window)
        sd = float(np.std(rw_errors, ddof=1)) if rw_errors.size > 1 else 0.0
        notes.append("fewer than 20 BVAR errors: RW empirical error scale used (protocol rule)")
    prepared = _Prepared(
        target=target,
        series_id=series_id or ("CPIAUCSL" if target == "T1_CPI_YOY" else "GDPC1"),
        series=target_series,
        window=window,
        unit=unit,
        last=last,
        target_period=int(period),
        steps=steps,
        index=index[-window:],
        values=matrix[-window:, 0],
    )
    return _finalize(ctx, "BVAR_MINNESOTA", prepared, point, sd=sd, notes=tuple(notes), horizon=horizon)


# --------------------------------------------------------------------------------------
# T3 common: label availability and regressor month
# --------------------------------------------------------------------------------------
def _t3_label_limit(ctx: DataContext, horizon: int) -> int:
    """Last training origin month whose recession event label is observable at the origin.

    Uses the recorded NBER announcement dates (protocol v4 data_depth.nber_rule): labels are
    available only after the announcement of the trough that ends the episode, and an event
    label at month m needs the outcome through m + horizon.  The fixed lag is only a fallback.
    """
    cutoff = nber_label_cutoff(ctx.origin)
    if cutoff is not None:
        return int(cutoff) - int(horizon)
    return ctx.origin_month - int(horizon) - NBER_ANNOUNCEMENT_LAG_MONTHS


def _last_available_month(ctx: DataContext, name: str) -> int:
    series = ctx.require(name)
    last = series.last_index
    if last is None:
        raise InsufficientData(f"{name}: no observations at the origin")
    return int(last)


def _probit_fit(x: np.ndarray, y: np.ndarray, iterations: int = 100) -> np.ndarray | None:
    beta = np.zeros(x.shape[1])
    for _ in range(iterations):
        eta = np.clip(x @ beta, -30.0, 30.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        weights = np.clip(mu * (1.0 - mu), 1e-8, None)
        gradient = x.T @ (y - mu)
        hessian = x.T @ (x * weights[:, None])
        try:
            delta = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            return None
        beta = beta + delta
        if not np.all(np.isfinite(beta)):
            return None
        if float(np.max(np.abs(delta))) < 1e-9:
            break
    return beta


# --------------------------------------------------------------------------------------
# PROBIT_SPREAD
# --------------------------------------------------------------------------------------
def probit_spread(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Estrella-Mishkin yield-curve probit on the GS10-TB3MS spread (T10Y3M not used)."""
    if target not in MODEL_TARGETS["PROBIT_SPREAD"]:
        raise ForecastError(f"PROBIT_SPREAD does not apply to {target}")
    horizon = int(horizon)
    if horizon not in TARGET_SPEC["T3_RECESSION"]["horizons"]:
        raise ForecastError(f"PROBIT_SPREAD: unsupported T3 horizon {horizon}")
    window = MODEL_ESTIMATION_WINDOW["PROBIT_SPREAD"]
    notes = ["GS10-TB3MS spread only (T10Y3M dropped: vintages start 2014-01-27)"]
    try:
        months, (gs10_values, tb3_values), _freq = align_series(ctx.require("GS10"), ctx.require("TB3MS"))
    except SeriesUnavailable as exc:
        raise InsufficientData(f"PROBIT_SPREAD: yield inputs unavailable at the origin ({exc})")
    spreads = np.asarray(gs10_values - tb3_values, dtype=float)
    usrec = ctx.require("USREC")
    limit = _t3_label_limit(ctx, horizon)
    x_rows: list[list[float]] = []
    labels: list[float] = []
    for month, spread in zip(months, spreads):
        month = int(month)
        if month > limit or month <= limit - window:
            continue
        event = usrec_event(usrec, month, horizon)
        if event is None:
            continue
        x_rows.append([1.0, float(spread)])
        labels.append(float(event))
    if not labels:  # pragma: no cover - only with a very short local archive
        raise InsufficientData("PROBIT_SPREAD: no labelled observations in the rolling window")
    y = np.asarray(labels, dtype=float)
    base_rate = float(np.mean(y))
    if len(set(labels)) < 2 or len(labels) < 30:
        probability = base_rate
        notes.append("degenerate/insufficient label distribution: window base rate used")
    else:
        beta = _probit_fit(np.asarray(x_rows, dtype=float), y)
        if beta is None:
            probability = base_rate
            notes.append("probit MLE failed (singular Hessian): window base rate used")
        else:
            spread_month = int(months[-1])
            probability = float(NORMAL.cdf(float(beta[0] + beta[1] * float(spreads[-1]))))
            notes.append(f"spread at {month_label(spread_month)}; probit MLE on the rolling 240-month window")
    probability = float(np.clip(probability, 1e-4, 1.0 - 1e-4))
    return Forecast(
        model_id="PROBIT_SPREAD",
        target=target,
        series_id="GS10-TB3MS",
        origin=ctx.origin,
        horizon=horizon,
        point=probability,
        quantiles=None,
        probability=probability,
        estimation_window=window,
        estimation_unit="months",
        effective_steps=None,
        data_class=ctx.data_class,
        vintage_date=_vintage_label(ctx),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------------------
# SAHM_RULE
# --------------------------------------------------------------------------------------
def _sahm_signals(unrate: Series) -> dict[int, int]:
    values = unrate.values_dict()
    signals: dict[int, int] = {}
    for month in values:
        window_ma = [values.get(month - offset) for offset in range(0, 3)]
        window_min = [values.get(month - offset) for offset in range(0, 12)]
        if any(v is None for v in window_ma) or any(v is None for v in window_min):
            continue
        moving_average = float(np.mean(window_ma))
        minimum = float(np.min(window_min))
        signals[int(month)] = 1 if (moving_average - minimum) >= 0.50 else 0
    return signals


def _logit_fit(x: np.ndarray, y: np.ndarray, iterations: int = 100) -> np.ndarray | None:
    beta = np.zeros(x.shape[1])
    for _ in range(iterations):
        eta = np.clip(x @ beta, -30.0, 30.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        weights = np.clip(mu * (1.0 - mu), 1e-8, None)
        gradient = x.T @ (y - mu)
        hessian = x.T @ (x * weights[:, None])
        try:
            delta = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            return None
        beta = beta + delta
        if not np.all(np.isfinite(beta)):
            return None
        if float(np.max(np.abs(delta))) < 1e-9:
            break
    return beta


def sahm_rule(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Sahm rule indicator converted to a probability by logistic calibration."""
    if target not in MODEL_TARGETS["SAHM_RULE"]:
        raise ForecastError(f"SAHM_RULE does not apply to {target}")
    horizon = int(horizon)
    if horizon not in TARGET_SPEC["T3_RECESSION"]["horizons"]:
        raise ForecastError(f"SAHM_RULE: unsupported T3 horizon {horizon}")
    window = MODEL_ESTIMATION_WINDOW["SAHM_RULE"]
    unrate = ctx.require("UNRATE")
    signals = _sahm_signals(unrate)
    usrec = ctx.require("USREC")
    limit = _t3_label_limit(ctx, horizon)
    x_rows: list[list[float]] = []
    labels: list[float] = []
    for month, signal in signals.items():
        if month > limit or month <= limit - window:
            continue
        event = usrec_event(usrec, month, horizon)
        if event is None:
            continue
        x_rows.append([1.0, float(signal)])
        labels.append(float(event))
    if not labels:  # pragma: no cover
        raise InsufficientData("SAHM_RULE: no labelled observations in the rolling window")
    y = np.asarray(labels, dtype=float)
    base_rate = float(np.mean(y))
    last_month = _last_available_month(ctx, "UNRATE")
    current_signal = signals.get(last_month, 0)
    notes = ["Sahm indicator: 3-month UNRATE MA minus 12-month min, fires at 0.50pp"]
    if len(set(labels)) < 2 or len(labels) < 30:
        probability = base_rate
        notes.append("degenerate/insufficient label distribution: window base rate used")
    else:
        beta = _logit_fit(np.asarray(x_rows, dtype=float), y)
        if beta is None:
            probability = base_rate
            notes.append("logistic fit failed (singular Hessian): window base rate used")
        else:
            eta = float(beta[0] + beta[1] * float(current_signal))
            probability = float(1.0 / (1.0 + math.exp(-np.clip(eta, -30.0, 30.0))))
            notes.append(f"indicator value {current_signal} at {month_label(last_month)}; logistic calibration")
    probability = float(np.clip(probability, 1e-4, 1.0 - 1e-4))
    return Forecast(
        model_id="SAHM_RULE",
        target=target,
        series_id="UNRATE",
        origin=ctx.origin,
        horizon=horizon,
        point=probability,
        quantiles=None,
        probability=probability,
        estimation_window=window,
        estimation_unit="months",
        effective_steps=None,
        data_class=ctx.data_class,
        vintage_date=_vintage_label(ctx),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------------------
# OWN_NOWCAST_BRIDGE
# --------------------------------------------------------------------------------------
def own_nowcast_bridge(ctx: DataContext, target: str, horizon: int, series_id: str | None = None) -> Forecast:
    """Own T2 h=0 nowcast: PCA factors on monthly data + quarterly bridge regression."""
    if target not in MODEL_TARGETS["OWN_NOWCAST_BRIDGE"]:
        raise ForecastError(f"OWN_NOWCAST_BRIDGE does not apply to {target}")
    horizon = int(horizon)
    if horizon != 0:
        raise ForecastError("OWN_NOWCAST_BRIDGE is defined for T2 horizon 0 only")
    window_months = MODEL_ESTIMATION_WINDOW["OWN_NOWCAST_BRIDGE"]
    window_quarters = 60
    try:
        months, (unrate_v, gs10_v, tb3_v), _freq = align_series(
            ctx.require("UNRATE"), ctx.require("GS10"), ctx.require("TB3MS")
        )
    except SeriesUnavailable as exc:
        raise InsufficientData(f"OWN_NOWCAST_BRIDGE: monthly inputs unavailable ({exc})")
    if months.size < 60:
        raise InsufficientData("OWN_NOWCAST_BRIDGE: fewer than 60 monthly observations")
    take = min(window_months, months.size)
    months = months[-take:]
    panel = np.column_stack([unrate_v[-take:], gs10_v[-take:], tb3_v[-take:]])
    means = panel.mean(axis=0)
    sd = panel.std(axis=0, ddof=1)
    sd[sd <= 0] = 1.0
    standardised = (panel - means) / sd
    _u, singular_values, vt = np.linalg.svd(standardised, full_matrices=False)
    variance_share = singular_values ** 2 / max(float((singular_values ** 2).sum()), 1e-12)
    factors_count = 1 if variance_share[0] >= 0.85 or variance_share.size == 1 else 2
    factors = standardised @ vt[:factors_count].T  # T x k
    ar_coefficients: list[np.ndarray] = []
    for j in range(factors_count):
        x = np.column_stack([np.ones(factors.shape[0] - 1), factors[:-1, j]])
        ar_coefficients.append(_ols(x, factors[1:, j]))
    factor_at = {int(month): factors[i].copy() for i, month in enumerate(months)}
    last_factor_month = int(months[-1])
    last_factor = factors[-1].copy()

    def factor_vector(month: int) -> np.ndarray:
        if month in factor_at:
            return factor_at[month]
        vector = last_factor.copy()
        for _ in range(int(month) - last_factor_month):
            vector = np.asarray([coef[0] + coef[1] * value for coef, value in zip(ar_coefficients, vector)])
        return vector

    target_quarter = ctx.origin_quarter
    quarter_months = [quarter_start_month(target_quarter) + offset for offset in range(3)]
    factor_bar = np.mean([factor_vector(month) for month in quarter_months], axis=0)
    growth = gdp_growth(ctx.require("GDPC1"))
    growth_values = growth.values_dict()
    last_growth_quarter = growth.last_index
    if last_growth_quarter is None:
        raise InsufficientData("OWN_NOWCAST_BRIDGE: GDPC1 unavailable")
    last_growth_quarter = int(last_growth_quarter)
    if target_quarter <= last_growth_quarter:
        raise ForecastError("OWN_NOWCAST_BRIDGE: target quarter is already published")
    rows: list[list[float]] = []
    outcomes: list[float] = []
    for q in range(last_growth_quarter - window_quarters + 1, last_growth_quarter + 1):
        quarter_months_q = [quarter_start_month(q) + offset for offset in range(3)]
        if any(month not in factor_at for month in quarter_months_q):
            continue
        factor_bar_q = np.mean([factor_at[month] for month in quarter_months_q], axis=0)
        previous_growth = growth_values.get(q - 1)
        current_growth = growth_values.get(q)
        if previous_growth is None or current_growth is None:
            continue
        rows.append([*factor_bar_q.tolist(), previous_growth, 1.0])
        outcomes.append(current_growth)
    if len(rows) < 20:
        raise InsufficientData("OWN_NOWCAST_BRIDGE: fewer than 20 bridge-regression quarters")
    x = np.asarray(rows, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    beta = _ols(x, y)
    last_growth = growth_values[last_growth_quarter]
    point = float(np.asarray([*factor_bar.tolist(), last_growth, 1.0], dtype=float) @ beta)
    residuals = y - x @ beta
    notes = (
        f"PCA factors={factors_count} (explained variance {float(variance_share[0]):.2f})",
        f"bridge on 60 quarters, factors from {window_months} months",
        "density from in-sample bridge residuals",
    )
    prepared = _Prepared(
        target=target,
        series_id="GDPC1",
        series=growth,
        window=window_quarters,
        unit="quarters",
        last=last_growth_quarter,
        target_period=target_quarter,
        steps=target_quarter - last_growth_quarter,
        index=growth.finite_index_values()[0][-window_quarters:],
        values=growth.finite_index_values()[1][-window_quarters:],
    )
    return _finalize(ctx, "OWN_NOWCAST_BRIDGE", prepared, point, errors=residuals, notes=notes, horizon=horizon)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
MODEL_FUNCS = {
    "RW": rw,
    "AO": ao,
    "AR_BIC": ar_bic,
    "BVAR_MINNESOTA": bvar_minnesota,
    "PROBIT_SPREAD": probit_spread,
    "SAHM_RULE": sahm_rule,
    "OWN_NOWCAST_BRIDGE": own_nowcast_bridge,
}


def applicable_models(target: str, horizon: int | None = None) -> tuple[str, ...]:
    """Model ids from the R3 specification that apply to ``target`` (and horizon)."""
    out = []
    for model_id, targets in MODEL_TARGETS.items():
        if target not in targets:
            continue
        if model_id == "OWN_NOWCAST_BRIDGE" and horizon is not None and int(horizon) != 0:
            continue
        out.append(model_id)
    return tuple(out)


def model_specification() -> dict:
    """The code-side mirror of the R3 ``models`` section for the implemented models."""
    return {
        model_id: {
            "targets": MODEL_TARGETS[model_id],
            "estimation_window": MODEL_ESTIMATION_WINDOW[model_id],
            "quantiles": list(QUANTILE_LEVELS) if model_id not in ("PROBIT_SPREAD", "SAHM_RULE") else None,
        }
        for model_id in MODEL_IDS
    }


def forecast(
    model_id: str,
    target: str,
    origin,
    horizon: int,
    series_id: str | None = None,
    mode: str = MODE_AUTO,
    data_root=None,
) -> Forecast:
    """Forecast one model at one origin; builds the PIT context locally."""
    if model_id not in MODEL_FUNCS:
        raise ValueError(f"unknown model {model_id!r}; available: {', '.join(MODEL_IDS)}")
    if target not in TARGET_IDS:
        raise ValueError(f"unknown target {target!r}")
    if target not in MODEL_TARGETS[model_id]:
        raise ForecastError(f"{model_id} does not apply to {target}")
    if model_id == "OWN_NOWCAST_BRIDGE" and int(horizon) != 0:
        raise ForecastError("OWN_NOWCAST_BRIDGE is defined for T2 horizon 0 only")
    ctx = DataContext.from_local(origin, mode=mode, data_root=data_root)
    return MODEL_FUNCS[model_id](ctx, target, int(horizon), series_id)


def forecast_all(
    target: str,
    origin,
    horizon: int,
    series_id: str | None = None,
    mode: str = MODE_AUTO,
    data_root=None,
) -> dict[str, Forecast]:
    """Every applicable model at one origin; a failing model does not stop the rest."""
    ctx = DataContext.from_local(origin, mode=mode, data_root=data_root)
    results: dict[str, Forecast] = {}
    for model_id in applicable_models(target, horizon):
        try:
            results[model_id] = MODEL_FUNCS[model_id](ctx, target, int(horizon), series_id)
        except ForecastError:
            continue
    return results


def run_rolling(
    model_id: str,
    target: str,
    horizon: int,
    origins,
    series_id: str | None = None,
    mode: str = MODE_AUTO,
    data_root=None,
    skip_errors: bool = True,
) -> list[dict]:
    """Run one model over a list of origins (rolling estimation window handled per origin)."""
    records: list[dict] = []
    for origin in origins:
        try:
            records.append(
                forecast(model_id, target, origin, horizon, series_id=series_id, mode=mode, data_root=data_root).to_dict()
            )
        except ForecastDataError as exc:
            if not skip_errors:
                raise
            records.append(
                {
                    "model_id": model_id,
                    "target": target,
                    "origin": str(origin),
                    "horizon": int(horizon),
                    "error": str(exc),
                }
            )
    return records
