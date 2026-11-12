"""Distribution mixtures for the ensemble layer.

The protocol's predictive density is a *mixture of the members' CDFs*, not an average of their
quantiles (``forecast_product.pool``).  The evaluation layer implements the member CDF (piecewise
linear between the 5% and 95% quantiles, Gaussian tails matched to those quantiles), the equal-weight
mixture and the robust-mixing guards (docs/94) in
:mod:`amfe.evaluation.distributions`; the ensemble needs the same construction with arbitrary
non-negative weights.

* :func:`mix_distributions` is the entry point: equal weights (or ``weights=None``) delegate
  directly to ``amfe.evaluation.distributions.mixture_quantiles`` so the control ensemble is
  bit-identical to the protocol comparator; non-uniform weights use
  :func:`weighted_mixture_quantiles` with the same CDF definition.
* :func:`weighted_mixture_quantiles` uses the *same* robust helper as the equal-weight path
  (docs/104): member winsorization (own IQR and cross-member consensus envelope), capped Gaussian
  tails and the final ``q95 <= k*(q75-q25)`` guard.  ``robust=False`` restores the historical plain
  behaviour exactly; ``return_diagnostics=True`` exposes what was repaired.
* :func:`pool_probabilities` combines T3 event probabilities (linear pool by default; a log-odds
  pool is provided as an explicitly labelled alternative).
* :func:`expected_quantile_bounds` returns the envelope of the members at each quantile level, a
  useful diagnostic: a proper mixture quantile must lie inside it.

No future information enters here: this module only combines numbers that were already produced
from point-in-time forecasts.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

from amfe.evaluation.distributions import FINAL_SPREAD_K
from amfe.evaluation.distributions import INDEX_Q50
from amfe.evaluation.distributions import MIN_SCALE, ROBUST_ASSUMPTIONS
from amfe.evaluation.distributions import CONSENSUS_K, TAIL_SIGMA_K, WINSOR_K
from amfe.evaluation.distributions import QUANTILE_LEVELS as EVALUATION_QUANTILE_LEVELS
from amfe.evaluation.distributions import (
    _member_cdf,
    _robust_vectors,
    mixture_quantiles as equal_weight_mixture,
)

from .sources import EnsembleError

QUANTILE_LEVELS = np.asarray(EVALUATION_QUANTILE_LEVELS, dtype=float)
GRID_SIZE = 4001
LOG_CLIP = (1e-4, 1.0 - 1e-4)


def _validate_vectors(quantile_vectors) -> list[np.ndarray]:
    vectors = [np.asarray(vector, dtype=float).ravel() for vector in quantile_vectors]
    if not vectors:
        raise EnsembleError("no distributions to mix")
    for vector in vectors:
        if vector.size != QUANTILE_LEVELS.size:
            raise EnsembleError("each member distribution must have 19 quantiles (5%..95% step 5)")
        if not np.all(np.isfinite(vector)):
            raise EnsembleError("member distribution contains non-finite quantiles")
        if np.any(np.diff(vector) < -1e-9):
            raise EnsembleError("member distribution quantiles are not monotone")
    return vectors


def _validate_weights(weights, n: int) -> np.ndarray:
    if weights is None:
        return np.full(n, 1.0 / n)
    values = np.asarray(list(weights), dtype=float).ravel()
    if values.size != n:
        raise EnsembleError(f"{n} weights expected, got {values.size}")
    if not np.all(np.isfinite(values)):
        raise EnsembleError("weights contain non-finite values")
    if np.any(values < -1e-12):
        raise EnsembleError("weights must be non-negative")
    values = np.clip(values, 0.0, None)
    total = float(values.sum())
    if total <= 0:
        raise EnsembleError("weights sum to zero")
    return values / total


def _plain_weighted_mixture(vectors, member_weights, levels, grid_size: int) -> np.ndarray:
    """The historical weighted mixture: no winsorization, uncapped Gaussian tails, no final guard."""
    lo = min(float(vector[0]) for vector in vectors)
    hi = max(float(vector[-1]) for vector in vectors)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, int(grid_size))
    cdf = np.zeros_like(grid)
    for vector, member_weight in zip(vectors, member_weights):
        if member_weight <= 0.0:
            continue
        cdf += member_weight * _member_cdf(vector, grid, tail_sigma_k=None)
    cdf = np.clip(cdf, 0.0, 1.0)
    out = np.empty(len(levels), dtype=float)
    for index, level in enumerate(levels):
        out[index] = float(np.interp(level, cdf, grid))
    return np.maximum.accumulate(out)


def weighted_mixture_quantiles(quantile_vectors, weights=None, levels=QUANTILE_LEVELS,
                               grid_size: int = GRID_SIZE, *, robust: bool = True,
                               winsor_k: float = WINSOR_K, consensus_k: float = CONSENSUS_K,
                               tail_sigma_k: float = TAIL_SIGMA_K,
                               final_spread_k: float = FINAL_SPREAD_K,
                               return_diagnostics: bool = False):
    """Quantiles of the weighted mixture of member predictive distributions.

    The CDF definition is the protocol one (piecewise linear between the member 5% and 95% quantiles,
    Gaussian tails outside, weighted average of the member CDFs).  ``robust=True`` (default) routes
    the members through the same robust helper as the equal-weight mixture (docs/94, docs/104):
    winsorization to the own ``q50 +- winsor_k*IQR`` and to the across-member consensus envelope,
    capped tail sigmas and a final ``q50 +- final_spread_k*(q75-q25)`` guard.  On well-formed inputs
    none of the guards is active, so the result equals the historical weighted mixture (uniform
    weights are bit-identical to the evaluation-layer equal-weight mixture).

    ``robust=False`` reproduces the historical plain weighted mixture exactly.  With
    ``return_diagnostics=True`` returns ``(quantiles, diagnostics)``.
    """
    vectors = _validate_vectors(quantile_vectors)
    member_weights = _validate_weights(weights, len(vectors))
    uniform = np.allclose(member_weights, np.full(len(vectors), 1.0 / len(vectors)), atol=1e-15)
    if robust and uniform:
        # single source of truth for the equal-weight protocol mixture
        values = equal_weight_mixture(vectors, levels=levels, grid_size=int(grid_size),
                                      robust=True, winsor_k=winsor_k,
                                      consensus_k=consensus_k, tail_sigma_k=tail_sigma_k,
                                      final_spread_k=final_spread_k,
                                      return_diagnostics=True)
        quantiles, diagnostics = values
        diagnostics = dict(diagnostics)
        diagnostics["method"] = "WEIGHTED_MIXTURE_ROBUST"
        diagnostics["weights"] = [float(weight) for weight in member_weights]
        if return_diagnostics:
            return np.asarray(quantiles, dtype=float), diagnostics
        return np.asarray(quantiles, dtype=float)
    if not robust:
        out = _plain_weighted_mixture(vectors, member_weights, levels, int(grid_size))
        diagnostics = {"robust": False, "n_members": len(vectors),
                       "weights": [float(weight) for weight in member_weights],
                       "winsorized_members": [], "consensus_envelope": None,
                       "final_guard_triggered": False,
                       "method": "WEIGHTED_MIXTURE_PLAIN",
                       "q05": float(out[0]), "q50": float(out[INDEX_Q50]),
                       "q95": float(out[-1])}
        if return_diagnostics:
            return out, diagnostics
        return out
    cleaned, diagnostics = _robust_vectors(vectors, winsor_k=winsor_k, consensus_k=consensus_k)
    diagnostics.update({"robust": True, "n_members": len(vectors),
                        "weights": [float(weight) for weight in member_weights],
                        "tail_sigma_k": float(tail_sigma_k),
                        "final_spread_k": float(final_spread_k),
                        "method": "WEIGHTED_MIXTURE_ROBUST",
                        "assumptions": dict(ROBUST_ASSUMPTIONS)})
    lo = min(float(vector[0]) for vector in cleaned)
    hi = max(float(vector[-1]) for vector in cleaned)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, int(grid_size))
    cdf = np.zeros_like(grid)
    for vector, member_weight in zip(cleaned, member_weights):
        if member_weight <= 0.0:
            continue
        cdf += member_weight * _member_cdf(vector, grid, tail_sigma_k=tail_sigma_k)
    cdf = np.clip(cdf, 0.0, 1.0)
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
    diagnostics["q05"], diagnostics["q50"], diagnostics["q95"] = (
        float(guarded[0]), float(guarded[INDEX_Q50]), float(guarded[-1]))
    if return_diagnostics:
        return guarded, diagnostics
    return guarded


def mix_distributions(quantile_vectors, weights=None, *, robust: bool = True) -> np.ndarray:
    """Mixture quantiles; uniform weights delegate to the evaluation-layer implementation."""
    vectors = _validate_vectors(quantile_vectors)
    member_weights = _validate_weights(weights, len(vectors))
    if np.allclose(member_weights, np.full(len(vectors), 1.0 / len(vectors)), atol=1e-15):
        return np.asarray(equal_weight_mixture(vectors, robust=robust), dtype=float)
    return weighted_mixture_quantiles(vectors, member_weights, robust=robust)


def pool_probabilities(probabilities, weights=None, method: str = "linear") -> float:
    """Pool T3 event probabilities: weighted linear pool (default) or weighted log-odds pool."""
    values = np.asarray(list(probabilities), dtype=float).ravel()
    if values.size == 0:
        raise EnsembleError("no probabilities to pool")
    if not np.all(np.isfinite(values)):
        raise EnsembleError("probabilities contain non-finite values")
    if np.any((values < 0.0) | (values > 1.0)):
        raise EnsembleError("probabilities must lie in [0, 1]")
    member_weights = _validate_weights(weights, values.size)
    if method == "linear":
        pooled = float(np.sum(member_weights * values))
    elif method == "log_odds":
        clipped = np.clip(values, *LOG_CLIP)
        logits = np.log(clipped / (1.0 - clipped))
        pooled = float(1.0 / (1.0 + math.exp(-float(np.sum(member_weights * logits)))))
    else:
        raise EnsembleError(f"unknown probability pool {method!r}")
    return float(min(max(pooled, LOG_CLIP[0]), LOG_CLIP[1]))


def expected_quantile_bounds(quantile_vectors) -> dict:
    """Envelope of the members at every quantile level (a mixture must lie inside it)."""
    vectors = _validate_vectors(quantile_vectors)
    matrix = np.vstack(vectors)
    return {
        "lower": np.min(matrix, axis=0),
        "upper": np.max(matrix, axis=0),
        "levels": [float(level) for level in QUANTILE_LEVELS],
    }


def mixture_diagnostics(quantile_vectors, weights=None) -> dict:
    """Summary of a mixture for reports: weight concentration and member spread at the median."""
    vectors = _validate_vectors(quantile_vectors)
    member_weights = _validate_weights(weights, len(vectors))
    bounds = expected_quantile_bounds(vectors)
    effective_n = 1.0 / float(np.sum(member_weights ** 2))
    return {
        "n_members": len(vectors),
        "weights": [float(weight) for weight in member_weights],
        "effective_n": effective_n,
        "median_lower": float(bounds["lower"][9]),
        "median_upper": float(bounds["upper"][9]),
        "median_spread": float(bounds["upper"][9] - bounds["lower"][9]),
        "mixture": mix_distributions(vectors, member_weights),
    }


def normal_inverse_cdf(level: float) -> float:
    """Normal quantile helper kept public for diagnostics and tests."""
    return float(NormalDist().inv_cdf(float(level)))
