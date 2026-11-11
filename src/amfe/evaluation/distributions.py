"""Distribution helpers for the evaluation layer (not frozen by R3).

The protocol's density comparator is an equal-weight mixture of the members' CDFs, not an average
of their quantiles.  ``mixture_quantiles`` builds the mixture CDF from the 19-quantile member
forecasts (piecewise linear between 5% and 95%, Gaussian tails matched to those quantiles) and
inverts it at the protocol quantile levels.
"""
from __future__ import annotations

import math

import numpy as np

QUANTILE_LEVELS = np.round(np.arange(0.05, 0.96, 0.05), 2)
SQRT2 = math.sqrt(2.0)


def _normal_cdf_array(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    flat = np.array([math.erf(float(v) / SQRT2) for v in np.ravel(x)], dtype=float)
    return (0.5 * (1.0 + flat)).reshape(x.shape)


def _normal_inv(p: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(float(p))


def _member_cdf(q: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Piecewise-linear CDF between p5 and p95, Gaussian tails outside."""
    q = np.asarray(q, dtype=float)
    lo, hi = float(q[0]), float(q[-1])
    out = np.empty_like(x, dtype=float)
    sigma_lo = (q[1] - q[0]) / (_normal_inv(0.10) - _normal_inv(0.05))
    sigma_hi = (q[-1] - q[-2]) / (_normal_inv(0.95) - _normal_inv(0.90))
    below = x <= lo
    above = x >= hi
    inside = ~(below | above)
    if np.any(below):
        if sigma_lo > 0:
            out[below] = 0.05 * _normal_cdf_array((x[below] - lo) / sigma_lo)
        else:
            out[below] = 0.0
    if np.any(above):
        if sigma_hi > 0:
            out[above] = 0.95 + 0.05 * _normal_cdf_array((x[above] - hi) / sigma_hi)
        else:
            out[above] = 1.0
    if np.any(inside):
        out[inside] = np.interp(x[inside], q, QUANTILE_LEVELS)
    return np.clip(out, 0.0, 1.0)


def mixture_quantiles(quantile_vectors: list, levels=QUANTILE_LEVELS, grid_size: int = 4001) -> np.ndarray:
    """Quantiles of the equal-weight mixture of the members' predictive distributions."""
    qs = [np.asarray(q, dtype=float) for q in quantile_vectors]
    if not qs:
        raise ValueError("no distributions to mix")
    lo = min(float(q[0]) for q in qs)
    hi = max(float(q[-1]) for q in qs)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, grid_size)
    cdf = np.zeros_like(grid)
    for q in qs:
        cdf += _member_cdf(q, grid)
    cdf /= len(qs)
    out = np.empty(len(levels), dtype=float)
    for i, level in enumerate(levels):
        out[i] = float(np.interp(level, cdf, grid))
    return np.maximum.accumulate(out)
