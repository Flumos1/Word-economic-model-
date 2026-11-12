"""Distribution mixtures for the ensemble layer.

The protocol's predictive density is a *mixture of the members' CDFs*, not an average of their
quantiles (``forecast_product.pool``).  The evaluation layer already implements the member CDF
(piecewise linear between the 5% and 95% quantiles, Gaussian tails matched to those quantiles) in
:func:`amfe.evaluation.distributions.mixture_quantiles` for the equal-weight case; the ensemble
needs the same construction with arbitrary non-negative weights.

* :func:`mix_distributions` is the entry point: equal weights (or ``weights=None``) delegate
  directly to ``amfe.evaluation.distributions.mixture_quantiles`` so the control ensemble is
  bit-identical to the protocol comparator; non-uniform weights use
  :func:`weighted_mixture_quantiles` with the same CDF definition.
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

from amfe.evaluation.distributions import QUANTILE_LEVELS as EVALUATION_QUANTILE_LEVELS
from amfe.evaluation.distributions import _member_cdf, mixture_quantiles as equal_weight_mixture

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


def weighted_mixture_quantiles(quantile_vectors, weights=None, levels=QUANTILE_LEVELS,
                               grid_size: int = GRID_SIZE) -> np.ndarray:
    """Quantiles of the weighted mixture of member predictive distributions.

    Identical to the evaluation layer's equal-weight mixture when the weights are uniform; for
    non-uniform weights the CDF grid is the same and the member CDFs are averaged with weights.
    """
    vectors = _validate_vectors(quantile_vectors)
    member_weights = _validate_weights(weights, len(vectors))
    lo = min(float(vector[0]) for vector in vectors)
    hi = max(float(vector[-1]) for vector in vectors)
    span = max(hi - lo, 1e-6)
    grid = np.linspace(lo - 0.75 * span, hi + 0.75 * span, int(grid_size))
    cdf = np.zeros_like(grid)
    for vector, member_weight in zip(vectors, member_weights):
        if member_weight <= 0.0:
            continue
        cdf += member_weight * _member_cdf(vector, grid)
    cdf = np.clip(cdf, 0.0, 1.0)
    out = np.empty(len(levels), dtype=float)
    for index, level in enumerate(levels):
        out[index] = float(np.interp(level, cdf, grid))
    return np.maximum.accumulate(out)


def mix_distributions(quantile_vectors, weights=None) -> np.ndarray:
    """Mixture quantiles; uniform weights delegate to the evaluation-layer implementation."""
    vectors = _validate_vectors(quantile_vectors)
    member_weights = _validate_weights(weights, len(vectors))
    if np.allclose(member_weights, np.full(len(vectors), 1.0 / len(vectors)), atol=1e-15):
        return np.asarray(equal_weight_mixture(vectors), dtype=float)
    return weighted_mixture_quantiles(vectors, member_weights)


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
