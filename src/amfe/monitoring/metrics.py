"""Rolling error metrics and sequential change detectors for the monitoring layer.

Metrics (mirroring the frozen harness formulas so monitoring and admission speak the same language):
  * MAE / RMSE (point forecasts),
  * CRPS = twice the mean pinball loss over the 19-quantile grid 5%..95% step 5%,
  * Brier and log score with probabilities clipped to [0.01, 0.99] (event forecasts).

Detectors (all deterministic, no external dependencies beyond numpy):
  * EWMA control chart (Roberts) - slow mean drift, calibrated in sigma units;
  * tabular CUSUM (Page) - a persistent mean shift of ``delta`` sigma;
  * Page-Hinkley - cumulative deviation with an allowance, increase/decrease.

Every detector returns a :class:`DetectionResult` with the first alarm index and the statistic at
that point, so the caller can log the evidence instead of a bare boolean.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

QUANTILES = np.round(np.arange(0.05, 1.0, 0.05), 2)          # 19 quantiles, harness grid
LOG_CLIP = (0.01, 0.99)


class MetricError(RuntimeError):
    """Invalid metric input."""


def _as_1d(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise MetricError(f"{name} must be one-dimensional")
    if array.size == 0:
        raise MetricError(f"{name} is empty")
    if not np.all(np.isfinite(array)):
        raise MetricError(f"{name} contains non-finite values")
    return array


# ------------------------------------------------------------------------------- single losses
def absolute_error(point, actual) -> np.ndarray:
    point = _as_1d(point, "point")
    actual = _as_1d(actual, "actual")
    if point.shape != actual.shape:
        raise MetricError("point and actual lengths differ")
    return np.abs(actual - point)


def squared_error(point, actual) -> np.ndarray:
    point = _as_1d(point, "point")
    actual = _as_1d(actual, "actual")
    if point.shape != actual.shape:
        raise MetricError("point and actual lengths differ")
    return (actual - point) ** 2


def pinball_matrix(quantiles, actual) -> np.ndarray:
    """Per-observation pinball loss for every grid quantile, shape (n, 19)."""
    q = np.asarray(quantiles, dtype=float)
    y = _as_1d(actual, "actual")
    if q.ndim != 2 or q.shape != (y.size, len(QUANTILES)):
        raise MetricError(f"quantiles must have shape (n, {len(QUANTILES)})")
    if not np.all(np.isfinite(q)):
        raise MetricError("quantiles contain non-finite values")
    diff = y[:, None] - q
    return np.where(diff >= 0.0, QUANTILES[None, :] * diff,
                    (QUANTILES[None, :] - 1.0) * diff)


def crps(quantiles, actual) -> np.ndarray:
    """Harness CRPS: twice the mean pinball loss over the 19 quantiles, per observation."""
    return 2.0 * pinball_matrix(quantiles, actual).mean(axis=1)


def brier(probability, actual) -> np.ndarray:
    p = _as_1d(probability, "probability")
    y = _as_1d(actual, "actual")
    if p.shape != y.shape:
        raise MetricError("probability and actual lengths differ")
    if np.any((p < 0.0) | (p > 1.0)):
        raise MetricError("probabilities must lie in [0, 1]")
    return (y - p) ** 2


def log_score(probability, actual, clip=LOG_CLIP) -> np.ndarray:
    p = np.clip(_as_1d(probability, "probability"), *clip)
    y = _as_1d(actual, "actual")
    if p.shape != y.shape:
        raise MetricError("probability and actual lengths differ")
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


# ------------------------------------------------------------------------------- rolling means
def rolling(values, window: int = 12, *, aggregate: str = "mean") -> np.ndarray:
    """Rolling mean (or rolling root-mean-square) with NaN before the window is full."""
    values = _as_1d(values, "values")
    if window < 1:
        raise MetricError("window must be positive")
    window = int(window)
    result = np.full(values.size, np.nan)
    if values.size < window:
        return result
    if aggregate == "mean":
        cumulative = np.concatenate([[0.0], np.cumsum(values)])
        result[window - 1:] = (cumulative[window:] - cumulative[:-window]) / window
    elif aggregate == "rms":
        cumulative = np.concatenate([[0.0], np.cumsum(values ** 2)])
        result[window - 1:] = np.sqrt((cumulative[window:] - cumulative[:-window]) / window)
    else:
        raise MetricError(f"unknown aggregate {aggregate!r}")
    return result


def rolling_mae(absolute_errors, window: int = 12) -> np.ndarray:
    return rolling(absolute_errors, window)


def rolling_rmse(squared_errors, window: int = 12) -> np.ndarray:
    """Input is the squared error series; the output is the rolling RMSE (harness aggregation)."""
    return rolling(squared_errors, window, aggregate="rms")


def rolling_crps(quantiles, actual, window: int = 12) -> np.ndarray:
    return rolling(crps(quantiles, actual), window)


def rolling_brier(probability, actual, window: int = 12) -> np.ndarray:
    return rolling(brier(probability, actual), window)


class RollingMetrics:
    """Streaming tracker of the four monitoring losses over a fixed rolling window."""

    def __init__(self, window: int = 12):
        if window < 1:
            raise MetricError("window must be positive")
        self.window = int(window)
        self._series: dict[str, list[float]] = {"MAE": [], "RMSE": [], "CRPS": [], "Brier": []}

    def update(self, actual: float, *, point: float | None = None,
               quantiles=None, probability: float | None = None) -> dict:
        added = {}
        if point is not None:
            error = float(actual) - float(point)
            self._series["MAE"].append(abs(error))
            self._series["RMSE"].append(error ** 2)
            added.update({"MAE": abs(error), "RMSE": error ** 2})
        if quantiles is not None:
            q = np.asarray(quantiles, dtype=float)
            if q.shape != (len(QUANTILES),):
                raise MetricError(f"quantiles must have {len(QUANTILES)} entries")
            value = float(crps(q[None, :], np.array([float(actual)]))[0])
            self._series["CRPS"].append(value)
            added["CRPS"] = value
        if probability is not None:
            value = float(brier(np.array([float(probability)]), np.array([float(actual)]))[0])
            self._series["Brier"].append(value)
            added["Brier"] = value
        if not added:
            raise MetricError("no forecast kind supplied")
        return self.snapshot()

    def snapshot(self) -> dict:
        out = {}
        for name, values in self._series.items():
            if not values:
                continue
            tail = np.asarray(values[-self.window:], dtype=float)
            if name == "RMSE":
                out[name] = float(np.sqrt(np.mean(tail)))
            else:
                out[name] = float(np.mean(tail))
            out[f"{name}_n"] = int(tail.size)
        return out

    def series(self, name: str) -> np.ndarray:
        if name not in self._series:
            raise MetricError(f"unknown metric {name!r}")
        return np.asarray(self._series[name], dtype=float)


# ------------------------------------------------------------------------------------ detectors
def newey_west_variance(values, lags: int | None = None) -> float:
    """Bartlett-kernel HAC estimate of Var(sqrt(n) * mean); the long-run variance under dependence."""
    x = _as_1d(values, "values") - float(np.mean(values))
    n = x.size
    if lags is None:
        lags = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))  # Newey-West rule of thumb
    lags = int(max(0, min(lags, n - 1)))
    total = float(np.dot(x, x)) / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = float(np.dot(x[lag:], x[:-lag])) / n
        total += 2.0 * weight * gamma
    return max(total, 0.0)


def long_run_sigma(values, lags: int | None = None) -> float:
    """Autocorrelation-robust standard deviation (sqrt of the HAC long-run variance).

    Macro series are autocorrelated; a detector calibrated on the ordinary standard deviation would
    fire on the persistent component alone.  The long-run sigma removes that inflation, so the
    in-control average run length is comparable to the independent case.
    """
    variance = newey_west_variance(values, lags)
    if variance <= 0.0:
        raise MetricError("long-run variance is zero; the series is constant")
    return math.sqrt(variance)


@dataclass
class DetectionResult:
    """Outcome of a sequential change detector."""

    detector: str
    detected: bool
    first_index: int | None = None
    statistic: float | None = None
    threshold: float | None = None
    series: np.ndarray | None = field(default=None, repr=False)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"detector": self.detector, "detected": self.detected,
                "first_index": self.first_index, "statistic": self.statistic,
                "threshold": self.threshold, "details": dict(self.details)}


def ewma(values, alpha: float = 0.2) -> np.ndarray:
    """EWMA recursion: s_0 = x_0, s_t = alpha * x_t + (1 - alpha) * s_{t-1}."""
    values = _as_1d(values, "values")
    if not 0.0 < alpha <= 1.0:
        raise MetricError("alpha must be in (0, 1]")
    out = np.empty(values.size)
    state = float(values[0])
    out[0] = state
    for index in range(1, values.size):
        state = alpha * float(values[index]) + (1.0 - alpha) * state
        out[index] = state
    return out


def ewma_detector(values, alpha: float = 0.2, *, target: float | None = None,
                  sigma: float | None = None, threshold: float = 3.0) -> DetectionResult:
    """EWMA control chart in sigma units; detects a shift of the local mean."""
    values = _as_1d(values, "values")
    target = float(np.mean(values)) if target is None else float(target)
    sigma = long_run_sigma(values) if sigma is None else float(sigma)
    if sigma <= 0.0:
        raise MetricError("sigma must be positive")
    smoothed = ewma(values, alpha)
    limit = threshold * sigma * math.sqrt(alpha / (2.0 - alpha))
    deviations = np.abs(smoothed - target)
    alarms = np.flatnonzero(deviations > limit)
    first = int(alarms[0]) if alarms.size else None
    return DetectionResult(detector="EWMA", detected=first is not None, first_index=first,
                           statistic=float(deviations[first]) if first is not None else None,
                           threshold=float(limit), series=smoothed,
                           details={"alpha": alpha, "target": target, "sigma": sigma,
                                    "threshold_sigma": threshold,
                                    "sigma_rule": "HAC long-run sigma"})


def cusum(values, *, target: float | None = None, sigma: float | None = None,
          delta: float = 1.0, threshold: float = 10.0, two_sided: bool = True) -> DetectionResult:
    """Tabular CUSUM for a persistent mean shift of ``delta`` sigma.

    Standardised values z_t = (x_t - target)/sigma; the reference value is k = delta/2 and the
    decision interval is ``threshold``.  ``S_hi`` accumulates upward deviations, ``S_lo`` downward.
    The default threshold 10 corresponds to the usual h = 5 for the one-sided chart (an in-control
    run length of the order of a thousand observations); choose it for the desired false-alarm rate.
    """
    values = _as_1d(values, "values")
    target = float(np.mean(values)) if target is None else float(target)
    sigma = long_run_sigma(values) if sigma is None else float(sigma)
    if sigma <= 0.0:
        raise MetricError("sigma must be positive")
    if delta <= 0.0:
        raise MetricError("delta must be positive")
    reference = delta / 2.0
    z = (values - target) / sigma
    high = np.zeros(values.size)
    low = np.zeros(values.size)
    s_high = s_low = 0.0
    for index, value in enumerate(z):
        s_high = max(0.0, s_high + value - reference)
        s_low = max(0.0, s_low - value - reference)
        high[index] = s_high
        low[index] = s_low
    combined = np.maximum(high, low) if two_sided else high
    alarms = np.flatnonzero(combined > threshold)
    first = int(alarms[0]) if alarms.size else None
    return DetectionResult(detector="CUSUM", detected=first is not None, first_index=first,
                           statistic=float(combined[first]) if first is not None else None,
                           threshold=float(threshold), series=combined,
                           details={"delta": delta, "target": target, "sigma": sigma,
                                    "reference": reference, "two_sided": two_sided,
                                    "up": high, "down": low,
                                    "sigma_rule": "HAC long-run sigma"})


def page_hinkley(values, *, target: float | None = None, sigma: float | None = None,
                 delta: float = 0.005, threshold: float = 3.0,
                 direction: str = "increase") -> DetectionResult:
    """Page-Hinkley test in Brownian scale.

    For an upward shift the oriented standardised deviations z_t = sign * (x_t - target) / sigma are
    accumulated as m_t = sum_i (z_i - delta); the test statistic (m_t - min_{s<=t} m_s) / sqrt(t) is
    O(1) under the null (including autocorrelated series once z is standardised by the long-run
    sigma) and grows with the size of a persistent shift in the requested direction.  ``delta`` is
    small so that slow drift is detected; ``threshold`` 3.0 is calibrated to roughly a 1% false-alarm
    rate over a few hundred observations.
    """
    values = _as_1d(values, "values")
    target = float(np.mean(values)) if target is None else float(target)
    sigma = long_run_sigma(values) if sigma is None else float(sigma)
    if sigma <= 0.0:
        raise MetricError("sigma must be positive")
    if delta < 0.0:
        raise MetricError("delta must be non-negative")
    if direction not in ("increase", "decrease"):
        raise MetricError("direction must be 'increase' or 'decrease'")
    sign = 1.0 if direction == "increase" else -1.0
    z = sign * (values - target) / sigma
    cumulative = np.cumsum(z - delta)
    running_min = np.minimum.accumulate(np.concatenate([[0.0], cumulative[:-1]]))
    statistic = (cumulative - running_min) / np.sqrt(np.arange(1, values.size + 1))
    alarms = np.flatnonzero(statistic > threshold)
    first = int(alarms[0]) if alarms.size else None
    return DetectionResult(detector="PAGE_HINKLEY", detected=first is not None, first_index=first,
                           statistic=float(statistic[first]) if first is not None else None,
                           threshold=float(threshold), series=statistic,
                           details={"direction": direction, "delta": delta, "target": target,
                                    "sigma": sigma, "sigma_rule": "HAC long-run sigma",
                                    "statistic_rule": "(m_t - min m) / sqrt(t)"})


def detect_all(values, *, target: float | None = None, sigma: float | None = None,
               alpha: float = 0.2, cusum_delta: float = 1.0, cusum_threshold: float = 10.0,
               ph_threshold: float = 3.0) -> dict[str, DetectionResult]:
    """Run EWMA, CUSUM and Page-Hinkley on the same series (both directions for Page-Hinkley)."""
    return {
        "EWMA": ewma_detector(values, alpha=alpha, target=target, sigma=sigma),
        "CUSUM": cusum(values, target=target, sigma=sigma, delta=cusum_delta,
                       threshold=cusum_threshold),
        "PAGE_HINKLEY_UP": page_hinkley(values, target=target, sigma=sigma,
                                        threshold=ph_threshold, direction="increase"),
        "PAGE_HINKLEY_DOWN": page_hinkley(values, target=target, sigma=sigma,
                                          threshold=ph_threshold, direction="decrease"),
    }
