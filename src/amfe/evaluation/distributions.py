"""Distribution helpers for the evaluation layer (not frozen by R3).

The protocol's density comparator is an equal-weight mixture of the members' CDFs, not an average
of their quantiles.  ``mixture_quantiles`` builds the mixture CDF from the 19-quantile member
forecasts (piecewise linear between 5% and 95%, Gaussian tails matched to those quantiles) and
inverts it at the protocol quantile levels.

Robustness (docs/94)
--------------------
A mixture is only as sane as its members: the protocol quantile of the mixture lies inside the
member envelope, so **one broken member vector is enough to poison the whole mixture** (the 2026-11
T2 incident: an upstream unit error produced member q95 around 18 600 and the mixture followed).
The module therefore has a robust mode (default) with three layers, all inactive on well-formed
inputs (the output is then bit-identical to the plain protocol mixture):

1. **member winsorization** - every member vector is clipped to
   ``q50 +- winsor_k * IQR`` (own robust scale) and to a *consensus envelope* built from the
   across-member median q05/q95 expanded by ``consensus_k * median member IQR``.  A member that is
   off-scale relative to its peers (a units bug, a degenerate density) cannot enter the mixture at
   full strength.
2. **bounded tail scale** - the Gaussian tail of ``_member_cdf`` is fitted to the endpoint gaps but
   its sigma is capped at ``tail_sigma_k * IQR``, so tail extrapolation can never run away.
3. **final spread guard** - after inversion the mixture quantiles are clipped to
   ``q50 +- final_spread_k * (q75 - q25)`` and made monotone, so ``q95 > k * (q75 - q25)`` is
   impossible by construction.

The robust mode changes the *inputs* of the protocol mixture (a documented assumption, not a
different pooling rule): between winsorized vectors the equal-weight mixture of CDFs is computed
exactly as before.  ``robust=False`` reproduces the historical plain behaviour.
"""
from __future__ import annotations

import math

import numpy as np

QUANTILE_LEVELS = np.round(np.arange(0.05, 0.96, 0.05), 2)
SQRT2 = math.sqrt(2.0)

#: Robust-mixture constants (documented in docs/94; change only through a protocol note).
TAIL_SIGMA_K = 2.0
WINSOR_K = 10.0
CONSENSUS_K = 6.0
FINAL_SPREAD_K = 25.0
MIN_SCALE = 1e-12

INDEX_Q05, INDEX_Q25, INDEX_Q50, INDEX_Q75, INDEX_Q95 = 0, 4, 9, 14, 18

ROBUST_ASSUMPTIONS = {
    "winsor_k": WINSOR_K,
    "consensus_k": CONSENSUS_K,
    "tail_sigma_k": TAIL_SIGMA_K,
    "final_spread_k": FINAL_SPREAD_K,
    "rule": ("a member quantile vector whose location/spread is more than consensus_k times the "
             "across-member scale is winsorized; Gaussian tails are capped at tail_sigma_k * IQR; "
             "the mixture output is clipped to q50 +- final_spread_k * (q75 - q25)"),
}


def _normal_cdf_array(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    flat = np.array([math.erf(float(v) / SQRT2) for v in np.ravel(x)], dtype=float)
    return (0.5 * (1.0 + flat)).reshape(x.shape)


def _normal_inv(p: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(float(p))


def _robust_scale(q: np.ndarray) -> float:
    """Robust scale of a quantile vector: IQR, falling back to half the 5-95 span, then a floor."""
    q = np.asarray(q, dtype=float)
    iqr = float(q[INDEX_Q75] - q[INDEX_Q25])
    if np.isfinite(iqr) and iqr > MIN_SCALE:
        return iqr
    span = float(q[INDEX_Q95] - q[INDEX_Q05])
    if np.isfinite(span) and span > MIN_SCALE:
        return span / 2.0
    return max(abs(float(q[INDEX_Q50])) * 1e-6, MIN_SCALE)


def _winsorize_vector(q: np.ndarray, winsor_k: float = WINSOR_K) -> np.ndarray:
    """Clip a member quantile vector to ``q50 +- winsor_k * IQR`` and restore monotonicity."""
    q = np.asarray(q, dtype=float)
    center = float(q[INDEX_Q50])
    scale = _robust_scale(q)
    clipped = np.clip(q, center - winsor_k * scale, center + winsor_k * scale)
    return np.maximum.accumulate(clipped)


def _consensus_envelope(vectors: list, consensus_k: float = CONSENSUS_K) -> tuple[float, float, float]:
    """Across-member consensus envelope: median q05/q95 widened by consensus_k * median IQR."""
    scales = [_robust_scale(vector) for vector in vectors]
    median_scale = float(np.median(scales))
    median_q05 = float(np.median([vector[INDEX_Q05] for vector in vectors]))
    median_q95 = float(np.median([vector[INDEX_Q95] for vector in vectors]))
    margin = consensus_k * max(median_scale, MIN_SCALE)
    return median_q05 - margin, median_q95 + margin, median_scale


def _robust_vectors(quantile_vectors, winsor_k: float = WINSOR_K,
                    consensus_k: float = CONSENSUS_K) -> tuple[list, dict]:
    """Apply the member-level winsorization; return the cleaned vectors and a diagnostics dict."""
    vectors = [np.asarray(vector, dtype=float) for vector in quantile_vectors]
    for vector in vectors:
        if vector.ndim != 1 or vector.size != QUANTILE_LEVELS.size:
            raise ValueError(f"each member distribution must have {QUANTILE_LEVELS.size} quantiles")
        if not np.all(np.isfinite(vector)):
            raise ValueError("member distribution contains non-finite quantiles")
    envelope = None
    median_scale = float("nan")
    if len(vectors) > 1:
        low, high, median_scale = _consensus_envelope(vectors, consensus_k)
        envelope = (low, high)
        if not high > low:
            envelope = None
    cleaned = []
    flags = []
    for vector in vectors:
        member = _winsorize_vector(vector, winsor_k)
        changed = not np.allclose(member, vector, rtol=0.0, atol=0.0)
        if envelope is not None:
            bounded = np.clip(member, envelope[0], envelope[1])
            bounded = np.maximum.accumulate(bounded)
            changed = changed or not np.allclose(bounded, member, rtol=0.0, atol=0.0)
            member = bounded
        cleaned.append(member)
        flags.append(bool(changed))
    diagnostics = {
        "n_members": len(vectors),
        "median_scale": median_scale if np.isfinite(median_scale) else _robust_scale(vectors[0]),
        "consensus_envelope": list(envelope) if envelope is not None else None,
        "winsorized_members": [index for index, flag in enumerate(flags) if flag],
        "winsor_k": float(winsor_k),
        "consensus_k": float(consensus_k),
    }
    return cleaned, diagnostics


def _member_cdf(q: np.ndarray, x: np.ndarray, tail_sigma_k: float | None = TAIL_SIGMA_K) -> np.ndarray:
    """Piecewise-linear CDF between p5 and p95, Gaussian tails outside.

    ``tail_sigma_k`` caps the Gaussian tail sigma at ``tail_sigma_k * IQR`` (robust mode); ``None``
    keeps the historical uncapped extrapolation (used by ``robust=False`` and legacy tests).
    """
    q = np.asarray(q, dtype=float)
    lo, hi = float(q[0]), float(q[-1])
    out = np.empty_like(x, dtype=float)
    sigma_lo = (q[1] - q[0]) / (_normal_inv(0.10) - _normal_inv(0.05))
    sigma_hi = (q[-1] - q[-2]) / (_normal_inv(0.95) - _normal_inv(0.90))
    if tail_sigma_k is not None:
        cap = float(tail_sigma_k) * _robust_scale(q)
        if np.isfinite(cap) and cap > 0:
            sigma_lo = min(sigma_lo, cap) if sigma_lo > 0 else sigma_lo
            sigma_hi = min(sigma_hi, cap) if sigma_hi > 0 else sigma_hi
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


def _plain_mixture(vectors, levels, grid_size: int) -> np.ndarray:
    """The historical protocol mixture: no member winsorization, Gaussian tails uncapped."""
    lo = min(float(q[0]) for q in vectors)
    hi = max(float(q[-1]) for q in vectors)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, grid_size)
    cdf = np.zeros_like(grid)
    for q in vectors:
        cdf += _member_cdf(q, grid, tail_sigma_k=None)
    cdf /= len(vectors)
    out = np.empty(len(levels), dtype=float)
    for index, level in enumerate(levels):
        out[index] = float(np.interp(level, cdf, grid))
    return np.maximum.accumulate(out)


def mixture_quantiles(quantile_vectors: list, levels=QUANTILE_LEVELS, grid_size: int = 4001,
                      *, robust: bool = True, winsor_k: float = WINSOR_K,
                      consensus_k: float = CONSENSUS_K, tail_sigma_k: float = TAIL_SIGMA_K,
                      final_spread_k: float = FINAL_SPREAD_K,
                      return_diagnostics: bool = False):
    """Quantiles of the equal-weight mixture of the members' predictive distributions.

    ``robust=True`` (default) applies the member winsorization, the capped Gaussian tails and the
    final spread guard described in the module docstring; on well-formed inputs the result equals
    the plain protocol mixture.  ``robust=False`` reproduces the historical behaviour exactly.
    With ``return_diagnostics=True`` returns ``(quantiles, diagnostics)``.
    """
    qs = [np.asarray(q, dtype=float) for q in quantile_vectors]
    if not qs:
        raise ValueError("no distributions to mix")
    if not robust:
        out = _plain_mixture(qs, levels, int(grid_size))
        diagnostics = {"robust": False, "n_members": len(qs), "winsorized_members": [],
                       "consensus_envelope": None,
                       "q05": float(out[0]), "q50": float(out[len(out) // 2]),
                       "q95": float(out[-1])}
        if return_diagnostics:
            return out, diagnostics
        return out
    vectors, diagnostics = _robust_vectors(qs, winsor_k=winsor_k, consensus_k=consensus_k)
    diagnostics.update({"robust": True, "tail_sigma_k": float(tail_sigma_k),
                        "final_spread_k": float(final_spread_k),
                        "assumptions": dict(ROBUST_ASSUMPTIONS)})
    lo = min(float(q[0]) for q in vectors)
    hi = max(float(q[-1]) for q in vectors)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, grid_size)
    cdf = np.zeros_like(grid)
    for q in vectors:
        cdf += _member_cdf(q, grid, tail_sigma_k=tail_sigma_k)
    cdf /= len(vectors)
    out = np.empty(len(levels), dtype=float)
    for index, level in enumerate(levels):
        out[index] = float(np.interp(level, cdf, grid))
    out = np.maximum.accumulate(out)
    q50 = float(np.interp(0.50, cdf, grid))
    q25 = float(np.interp(0.25, cdf, grid))
    q75 = float(np.interp(0.75, cdf, grid))
    scale = q75 - q25
    if not np.isfinite(scale) or scale <= MIN_SCALE:
        scale = (float(out[-1]) - float(out[0])) / 2.0
    scale = max(scale, MIN_SCALE)
    low_bound = q50 - final_spread_k * scale
    high_bound = q50 + final_spread_k * scale
    guarded = np.clip(out, low_bound, high_bound)
    guarded = np.maximum.accumulate(guarded)
    diagnostics["final_guard_triggered"] = bool(
        not np.allclose(guarded, out, rtol=0.0, atol=0.0))
    diagnostics["final_bounds"] = [float(low_bound), float(high_bound)]
    out = guarded
    diagnostics["q05"], diagnostics["q50"], diagnostics["q95"] = (
        float(out[0]), float(out[len(out) // 2]), float(out[-1]))
    if return_diagnostics:
        return out, diagnostics
    return out
