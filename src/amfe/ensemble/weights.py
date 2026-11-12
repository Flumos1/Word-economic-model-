"""Weighting schemes for the AMFE ensemble (non-negative, sum-to-one).

Every scheme takes the *same* interface: a list of member ids and a dict
``{member_id: errors}`` of aligned 1-D error arrays over the calibration history (NaN marks a
missing member at that origin).  Errors are forecast errors ``forecast - actual``; T3 uses
``probability - event`` so that squaring gives the Brier loss differential.

Schemes
-------
* :func:`equal_weights` - the equal-weight control ensemble.
* :func:`inverse_mse_weights` - inverse historical MSE, with a floor and an optional prior blend.
* :func:`correlation_weights` - long-only minimum-variance weights from a *regularised* error
  covariance (Ledoit-Wolf style shrinkage to the diagonal + ridge), solved by projected gradient on
  the simplex.  This is the correlation-aware scheme: two nearly identical members do not both get
  a large weight as they would under inverse MSE.
* :func:`bayesian_update_weights` - Dirichlet-multinomial Bayesian update: at every past origin the
  member(s) with the smallest absolute error receive a pseudo-count (optionally discounted), so the
  posterior mean is the weight vector.  This is a proper Bayesian update, robust to a single
  catastrophic origin (one observation can change the winner but not erase the prior).
* :func:`adaptive_weights` - the main scheme: blend of inverse-MSE, shrinkage-covariance and
  Bayesian weights, shrunk towards the equal weights with the horizon (fewer effective independent
  observations at longer horizons), and re-estimated on the current regime subset when there is
  enough regime history.

Regularisation and missing data
-------------------------------
All schemes accept a *prior* (uniform by default) and a ``prior_strength``; a member with no
calibration history keeps the prior share instead of being silently dropped.  At combination time
a member that is not available at the current origin has its weight redistributed proportionally to
the available members (``renormalize_available``), so the output weights always sum to one.

The calibration history must contain only *resolved* targets known before the origin; that rule is
enforced by ``amfe.ensemble.history``, not by this module (weights are pure arithmetic on arrays).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .sources import EnsembleError

EPS = 1e-12


@dataclass
class WeightResult:
    """Weights plus the diagnostics needed to audit how they were produced."""

    weights: dict[str, float]
    method: str
    member_ids: tuple[str, ...]
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = tuple(self.weights)
        if set(ids) != set(self.member_ids):
            raise EnsembleError("weight keys do not match member ids")
        values = np.asarray([self.weights[m] for m in self.member_ids], dtype=float)
        if not np.all(np.isfinite(values)):
            raise EnsembleError("non-finite weight")
        if np.any(values < -1e-12):
            raise EnsembleError("negative weight")
        total = float(values.sum())
        if total <= 0:
            raise EnsembleError("weights sum to zero")
        self.weights = {m: float(v / total) for m, v in zip(self.member_ids, values)}

    @property
    def effective_n(self) -> float:
        values = np.asarray(list(self.weights.values()), dtype=float)
        return float(1.0 / np.sum(values ** 2))

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "weights": {m: float(w) for m, w in self.weights.items()},
            "member_ids": list(self.member_ids),
            "effective_n": self.effective_n,
            "diagnostics": self.diagnostics,
        }


# --------------------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------------------
def _validate_errors(member_ids, errors: dict | None) -> tuple[tuple[str, ...], np.ndarray]:
    ids = tuple(member_ids)
    if not ids:
        raise EnsembleError("no members to weight")
    if len(set(ids)) != len(ids):
        raise EnsembleError(f"duplicate member ids: {ids}")
    if errors is None:
        raise EnsembleError("this weight scheme needs historical errors")
    arrays = []
    length = None
    for member_id in ids:
        array = np.asarray(errors.get(member_id, np.full(0, np.nan)), dtype=float).ravel()
        if length is None and array.size:
            length = array.size
        arrays.append(array)
    if length is None or length == 0:
        raise EnsembleError("historical errors are empty")
    matrix = np.full((length, len(ids)), np.nan)
    for column, array in enumerate(arrays):
        if array.size == 0:
            continue
        if array.size != length:
            raise EnsembleError("error arrays must be aligned to the same length")
        matrix[:, column] = array
    return ids, matrix


def _uniform(ids) -> dict[str, float]:
    n = len(ids)
    return {member_id: 1.0 / n for member_id in ids}


def _as_prior(ids, prior) -> np.ndarray:
    if prior is None:
        values = np.ones(len(ids), dtype=float)
    else:
        values = np.asarray([float(prior.get(m, 1.0 / len(ids))) for m in ids], dtype=float)
        if np.any(values < 0) or not np.all(np.isfinite(values)):
            raise EnsembleError("prior weights must be finite and non-negative")
    if values.sum() <= 0:
        values = np.ones(len(ids), dtype=float)
    return values / values.sum()


def _blend_prior(values: np.ndarray, prior: np.ndarray, strength: float) -> np.ndarray:
    strength = float(min(max(strength, 0.0), 1.0))
    values = np.asarray(values, dtype=float)
    total = float(values.sum())
    if total <= 0 or not np.all(np.isfinite(values)):
        values = prior.copy()
    else:
        values = values / total
    return (1.0 - strength) * values + strength * prior


def winsorize_errors(errors: dict[str, np.ndarray], clip: float = 0.99) -> dict[str, np.ndarray]:
    """Clip every error at the global ``clip`` quantile of |error| (outlier robustness)."""
    if not (0.5 < float(clip) <= 1.0):
        raise EnsembleError("winsorize clip must be in (0.5, 1.0]")
    matrix = np.concatenate(
        [np.asarray(v, dtype=float).ravel() for v in errors.values() if np.asarray(v).size]
    )
    finite = np.abs(matrix[np.isfinite(matrix)])
    if finite.size == 0:
        return errors
    threshold = float(np.quantile(finite, clip))
    if threshold <= 0:
        threshold = EPS
    return {
        member_id: np.clip(np.asarray(values, dtype=float), -threshold, threshold)
        for member_id, values in errors.items()
    }


def renormalize_available(weights: dict[str, float], available_members) -> dict[str, float]:
    """Redistribute the weight of unavailable members proportionally to the available ones."""
    available = [m for m in available_members if m in weights]
    if not available:
        raise EnsembleError("none of the weighted members is available")
    total = float(sum(weights[m] for m in available))
    if total <= 0:
        return {m: 1.0 / len(available) for m in available}
    return {m: float(weights[m] / total) for m in available}


# --------------------------------------------------------------------------------------
# Equal weights (control)
# --------------------------------------------------------------------------------------
def equal_weights(member_ids, errors: dict | None = None) -> WeightResult:
    ids = tuple(member_ids)
    if not ids:
        raise EnsembleError("no members to weight")
    return WeightResult(
        weights=_uniform(ids),
        method="EQUAL_WEIGHTS",
        member_ids=ids,
        diagnostics={"n_members": len(ids)},
    )


# --------------------------------------------------------------------------------------
# Inverse MSE
# --------------------------------------------------------------------------------------
def inverse_mse_weights(
    member_ids,
    errors: dict | None,
    *,
    floor: float | None = None,
    robust_clip: float | None = None,
    prior_strength: float = 0.0,
    prior: dict | None = None,
) -> WeightResult:
    """Weights proportional to ``1 / (MSE + floor)`` on the calibration history."""
    ids, matrix = _validate_errors(member_ids, errors)
    if robust_clip is not None:
        clipped = winsorize_errors({m: matrix[:, i] for i, m in enumerate(ids)}, robust_clip)
        matrix = np.column_stack([clipped[m] for m in ids])
    n_obs = np.sum(np.isfinite(matrix), axis=0).astype(int)
    mse = np.full(len(ids), np.nan)
    for column in range(len(ids)):
        column_errors = matrix[:, column]
        finite = column_errors[np.isfinite(column_errors)]
        if finite.size:
            mse[column] = float(np.mean(finite ** 2))
    observed = mse[np.isfinite(mse)]
    if observed.size == 0:
        weights = _as_prior(ids, prior)
        return WeightResult(
            weights={m: float(w) for m, w in zip(ids, weights)},
            method="INVERSE_MSE",
            member_ids=ids,
            diagnostics={"fallback": "NO_HISTORY", "n_obs": n_obs.tolist(),
                         "prior_strength": float(prior_strength)},
        )
    scale = float(np.median(observed))
    if floor is None:
        floor = max(EPS, 0.01 * scale)
    raw = np.where(np.isfinite(mse), 1.0 / (mse + floor), np.nan)
    fallback_raw = float(np.median(raw[np.isfinite(raw)]))
    raw = np.where(np.isfinite(raw), raw, fallback_raw)
    weights = _blend_prior(raw, _as_prior(ids, prior), prior_strength)
    return WeightResult(
        weights={m: float(w) for m, w in zip(ids, weights)},
        method="INVERSE_MSE",
        member_ids=ids,
        diagnostics={
            "mse": {m: (None if not np.isfinite(mse[i]) else float(mse[i])) for i, m in enumerate(ids)},
            "n_obs": {m: int(n_obs[i]) for i, m in enumerate(ids)},
            "floor": float(floor),
            "robust_clip": robust_clip,
            "prior_strength": float(prior_strength),
            "missing_members": [m for i, m in enumerate(ids) if not np.isfinite(mse[i])],
        },
    )


# --------------------------------------------------------------------------------------
# Correlation-aware shrinkage-covariance weights
# --------------------------------------------------------------------------------------
def shrinkage_covariance(
    errors_matrix,
    *,
    shrink: float = 0.5,
    ridge: float = 1e-8,
    center: bool = True,
) -> np.ndarray:
    """Regularised covariance: ``(1-s)S + s*diag(S) + ridge*trace/m*I``.

    ``shrink=1`` is a pure diagonal (members treated as uncorrelated), ``shrink=0`` is the raw
    sample covariance (can be singular when members are nearly identical).
    """
    matrix = np.asarray(errors_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 2 or matrix.shape[1] < 1:
        raise EnsembleError("covariance needs a 2-D matrix with at least two rows")
    if not (0.0 <= shrink <= 1.0):
        raise EnsembleError("shrink must lie in [0, 1]")
    if not np.all(np.isfinite(matrix)):
        raise EnsembleError("covariance matrix contains non-finite errors")
    n = matrix.shape[0]
    centred = matrix - matrix.mean(axis=0, keepdims=True) if center else matrix
    sample = centred.T @ centred / max(n - 1, 1)
    diagonal = np.diag(np.diag(sample))
    covariance = (1.0 - shrink) * sample + shrink * diagonal
    m = covariance.shape[0]
    covariance = covariance + float(ridge) * max(float(np.trace(covariance)) / m, EPS) * np.eye(m)
    return (covariance + covariance.T) / 2.0


def _project_simplex(vector: np.ndarray) -> np.ndarray:
    """Euclidean projection of a vector onto the unit simplex."""
    values = np.asarray(vector, dtype=float).ravel()
    n = values.size
    ordered = np.sort(values)[::-1]
    cumulative = np.cumsum(ordered)
    positions = np.arange(1, n + 1)
    condition = ordered * positions > (cumulative - 1.0)
    rho = int(np.nonzero(condition)[0][-1])
    theta = (cumulative[rho] - 1.0) / (rho + 1.0)
    return np.maximum(values - theta, 0.0)


def long_only_min_variance(covariance, iterations: int = 4000, tolerance: float = 1e-12) -> np.ndarray:
    """Minimise ``w' Σ w`` on the simplex by projected gradient descent (w >= 0, sum w = 1)."""
    sigma = np.asarray(covariance, dtype=float)
    m = sigma.shape[0]
    if sigma.shape != (m, m):
        raise EnsembleError("covariance must be square")
    weights = np.full(m, 1.0 / m)
    largest = float(np.max(np.linalg.eigvalsh((sigma + sigma.T) / 2.0)))
    if largest <= 0:
        return weights
    step = 1.0 / (2.0 * largest)
    previous = np.inf
    for _ in range(int(iterations)):
        gradient = 2.0 * sigma @ weights
        weights = _project_simplex(weights - step * gradient)
        objective = float(weights @ sigma @ weights)
        if abs(previous - objective) < tolerance:
            break
        previous = objective
    return weights


def correlation_weights(
    member_ids,
    errors: dict | None,
    *,
    shrink: float = 0.5,
    ridge: float = 1e-8,
    prior_strength: float = 0.05,
    prior: dict | None = None,
    min_complete_rows: int | None = None,
) -> WeightResult:
    """Long-only minimum-variance weights on a regularised error covariance."""
    ids, matrix = _validate_errors(member_ids, errors)
    n_members = len(ids)
    if min_complete_rows is None:
        min_complete_rows = max(4, n_members + 2)
    complete = np.all(np.isfinite(matrix), axis=1)
    n_complete = int(np.count_nonzero(complete))
    prior_values = _as_prior(ids, prior)
    if n_complete < min_complete_rows:
        fallback = inverse_mse_weights(ids, errors, prior_strength=prior_strength, prior=prior)
        return WeightResult(
            weights=fallback.weights,
            method="INVERSE_MSE_FALLBACK_CORRELATION",
            member_ids=ids,
            diagnostics=dict(
                fallback="INSUFFICIENT_COMPLETE_ROWS" if n_complete else "NO_COMPLETE_ROWS",
                n_complete_rows=n_complete,
                min_complete_rows=int(min_complete_rows),
                correlation_components=None,
            ),
        )
    covariance = shrinkage_covariance(matrix[complete], shrink=shrink, ridge=ridge)
    raw = long_only_min_variance(covariance)
    weights = _blend_prior(raw, prior_values, prior_strength)
    eigenvalues = np.linalg.eigvalsh(covariance)
    return WeightResult(
        weights={m: float(w) for m, w in zip(ids, weights)},
        method="MIN_VARIANCE_SHRINKAGE_COVARIANCE",
        member_ids=ids,
        diagnostics={
            "shrink": float(shrink),
            "ridge": float(ridge),
            "n_complete_rows": n_complete,
            "min_complete_rows": int(min_complete_rows),
            "cov_min_eigenvalue": float(np.min(eigenvalues)),
            "cov_max_eigenvalue": float(np.max(eigenvalues)),
            "cov_condition_number": float(np.max(eigenvalues) / max(np.min(eigenvalues), EPS)),
            "prior_strength": float(prior_strength),
            "raw_weights": {m: float(w) for m, w in zip(ids, raw)},
        },
    )


# --------------------------------------------------------------------------------------
# Bayesian updating of weights
# --------------------------------------------------------------------------------------
def bayesian_update_weights(
    member_ids,
    errors: dict | None,
    *,
    prior: dict | None = None,
    prior_strength: float = 1.0,
    decay: float = 0.0,
    robust_clip: float | None = None,
    tie_tolerance: float = 1e-12,
) -> WeightResult:
    """Posterior mean of a Dirichlet-multinomial over "which member was closest".

    Row ``i`` of the error matrix is one past origin (oldest first).  The member(s) with the
    smallest absolute error at that origin receive one pseudo-count; ``decay`` discounts older rows
    geometrically (``decay=0`` means no discount).  The prior is ``prior_strength * prior_weights``,
    so a member with no history keeps a non-zero posterior share and one catastrophic origin cannot
    erase a member that was good everywhere else.
    """
    ids, matrix = _validate_errors(member_ids, errors)
    if not (0.0 <= decay < 1.0):
        raise EnsembleError("decay must lie in [0, 1)")
    if robust_clip is not None:
        clipped = winsorize_errors({m: matrix[:, i] for i, m in enumerate(ids)}, robust_clip)
        matrix = np.column_stack([clipped[m] for m in ids])
    prior_values = _as_prior(ids, prior)
    n_rows = matrix.shape[0]
    wins = np.zeros(len(ids), dtype=float)
    rows_used = 0
    total_row_weight = 0.0
    for row in range(n_rows):
        values = matrix[row]
        available = np.isfinite(values)
        if not np.any(available):
            continue
        # geometric discount: the last row is the newest
        row_weight = (1.0 - decay) ** (n_rows - 1 - row)
        if row_weight <= 0:
            continue
        absolute = np.where(available, np.abs(values), np.inf)
        best = float(np.min(absolute))
        winners = np.where(available & (absolute <= best + tie_tolerance))[0]
        wins[winners] += row_weight / winners.size
        rows_used += 1
        total_row_weight += row_weight
    alpha = float(prior_strength) * prior_values + wins
    if not np.any(np.isfinite(alpha)) or alpha.sum() <= 0:
        alpha = prior_values.copy()
    weights = alpha / alpha.sum()
    return WeightResult(
        weights={m: float(w) for m, w in zip(ids, weights)},
        method="BAYES_DIRICHLET_UPDATE",
        member_ids=ids,
        diagnostics={
            "wins": {m: float(wins[i]) for i, m in enumerate(ids)},
            "rows_used": int(rows_used),
            "row_weight_total": float(total_row_weight),
            "decay": float(decay),
            "prior_strength": float(prior_strength),
            "robust_clip": robust_clip,
        },
    )


# --------------------------------------------------------------------------------------
# Adaptive scheme
# --------------------------------------------------------------------------------------
def _shrink_to_uniform(weights: np.ndarray, lam: float) -> np.ndarray:
    lam = float(min(max(lam, 0.0), 1.0))
    uniform = np.full(weights.size, 1.0 / weights.size)
    out = (1.0 - lam) * weights + lam * uniform
    return out / out.sum()


def _blend_schemes(parts: list[tuple[np.ndarray, float]]) -> np.ndarray:
    total = float(sum(weight for _, weight in parts))
    if total <= 0:
        raise EnsembleError("scheme blend weights must sum to a positive number")
    out = np.zeros_like(parts[0][0], dtype=float)
    for weights, share in parts:
        out = out + (share / total) * np.asarray(weights, dtype=float)
    total_out = float(out.sum())
    if total_out <= 0:
        return np.full(out.size, 1.0 / out.size)
    return out / total_out


def adaptive_weights(
    member_ids,
    errors: dict | None,
    *,
    target: str,
    horizon: int,
    regime_labels=None,
    current_regime: str | None = None,
    blend=(0.45, 0.30, 0.25),
    shrink: float = 0.5,
    bayes_decay: float = 0.05,
    horizon_scale: float = 12.0,
    max_horizon_shrink: float = 0.5,
    regime_weight: float = 0.5,
    min_regime_observations: int = 4,
    prior_strength: float = 0.02,
    robust_clip: float | None = 0.99,
) -> WeightResult:
    """Error + correlation + horizon + regime adaptive weights (the main ensemble scheme).

    The scheme is deliberately transparent:
      * inverse-MSE and shrinkage-covariance weights capture the average error and the redundancy
        of correlated members;
      * the Bayesian component captures the recent winner frequency (and keeps a prior);
      * the blend is shrunk towards equal weights with the horizon: at long horizons the errors
        overlap and the effective number of independent calibration observations is small;
      * when the current regime has at least ``min_regime_observations`` historical origins, a
        regime-specific re-estimation is blended in (recession vs expansion is *not* used here:
        the regime label is the project's own observable statistic, see ``history.rate_regime``).
    """
    ids = tuple(member_ids)
    if not ids:
        raise EnsembleError("no members to weight")
    inv = inverse_mse_weights(ids, errors, robust_clip=robust_clip, prior_strength=prior_strength)
    covariance = correlation_weights(ids, errors, shrink=shrink, prior_strength=prior_strength)
    bayes = bayesian_update_weights(ids, errors, decay=bayes_decay, robust_clip=robust_clip)
    pooled = _blend_schemes(
        [
            (np.asarray([inv.weights[m] for m in ids]), blend[0]),
            (np.asarray([covariance.weights[m] for m in ids]), blend[1]),
            (np.asarray([bayes.weights[m] for m in ids]), blend[2]),
        ]
    )
    lam = min(float(max_horizon_shrink), 0.5 * float(horizon) / float(horizon_scale))
    pooled = _shrink_to_uniform(pooled, lam)
    diagnostics = {
        "components": {
            "inverse_mse": inv.weights,
            "correlation": covariance.weights,
            "bayesian": bayes.weights,
        },
        "blend": [float(x) for x in blend],
        "horizon": int(horizon),
        "horizon_scale": float(horizon_scale),
        "horizon_shrink": float(lam),
        "max_horizon_shrink": float(max_horizon_shrink),
        "regime_used": False,
        "regime": current_regime,
        "regime_observations": 0,
        "correlation_method": covariance.method,
    }
    weights = pooled
    if regime_labels is not None and current_regime is not None:
        labels = np.asarray(list(regime_labels), dtype=object)
        _ids, error_matrix = _validate_errors(ids, errors)
        if labels.shape[0] == error_matrix.shape[0]:
            mask = np.asarray([str(label) == str(current_regime) for label in labels])
            n_regime = int(np.count_nonzero(mask))
            diagnostics["regime_observations"] = n_regime
            if n_regime >= int(min_regime_observations):
                subset = {
                    m: np.where(mask, error_matrix[:, i], np.nan) for i, m in enumerate(ids)
                }
                inv_r = inverse_mse_weights(ids, subset, robust_clip=robust_clip,
                                            prior_strength=prior_strength)
                cov_r = correlation_weights(ids, subset, shrink=shrink,
                                            prior_strength=prior_strength)
                bay_r = bayesian_update_weights(ids, subset, decay=bayes_decay,
                                                robust_clip=robust_clip)
                regime_weights = _blend_schemes(
                    [
                        (np.asarray([inv_r.weights[m] for m in ids]), blend[0]),
                        (np.asarray([cov_r.weights[m] for m in ids]), blend[1]),
                        (np.asarray([bay_r.weights[m] for m in ids]), blend[2]),
                    ]
                )
                regime_weights = _shrink_to_uniform(regime_weights, lam)
                weights = _blend_schemes(
                    [(pooled, 1.0 - float(regime_weight)), (regime_weights, float(regime_weight))]
                )
                diagnostics.update(
                    {
                        "regime_used": True,
                        "regime_components": {
                            "inverse_mse": inv_r.weights,
                            "correlation": cov_r.weights,
                            "bayesian": bay_r.weights,
                        },
                    }
                )
            else:
                diagnostics["regime_fallback"] = "INSUFFICIENT_REGIME_OBSERVATIONS"
        else:
            diagnostics["regime_fallback"] = "REGIME_LABELS_LENGTH_MISMATCH"
    return WeightResult(
        weights={m: float(w) for m, w in zip(ids, weights)},
        method="ADAPTIVE_HORIZON_REGIME_ERROR_CORRELATION",
        member_ids=ids,
        diagnostics=diagnostics,
    )


# --------------------------------------------------------------------------------------
# Dispatch used by the ensemble candidates
# --------------------------------------------------------------------------------------
SCHEME_EQUAL = "EQUAL"
SCHEME_INVERSE_MSE = "INVERSE_MSE"
SCHEME_BAYES = "BAYES"
SCHEME_ADAPTIVE = "ADAPTIVE"
WEIGHT_SCHEMES = (SCHEME_EQUAL, SCHEME_INVERSE_MSE, SCHEME_BAYES, SCHEME_ADAPTIVE)


def weights_for_scheme(
    scheme: str,
    member_ids,
    errors: dict | None = None,
    *,
    target: str,
    horizon: int,
    regime_labels=None,
    current_regime: str | None = None,
    **options,
) -> WeightResult:
    if scheme == SCHEME_EQUAL:
        return equal_weights(member_ids)
    if scheme == SCHEME_INVERSE_MSE:
        return inverse_mse_weights(
            member_ids,
            errors,
            prior_strength=options.get("prior_strength", 0.02),
            robust_clip=options.get("robust_clip", 0.99),
        )
    if scheme == SCHEME_BAYES:
        return bayesian_update_weights(
            member_ids,
            errors,
            decay=options.get("bayes_decay", 0.05),
            robust_clip=options.get("robust_clip", 0.99),
        )
    if scheme == SCHEME_ADAPTIVE:
        return adaptive_weights(
            member_ids,
            errors,
            target=target,
            horizon=horizon,
            regime_labels=regime_labels,
            current_regime=current_regime,
            shrink=options.get("shrink", 0.5),
            bayes_decay=options.get("bayes_decay", 0.05),
            horizon_scale=options.get("horizon_scale", 12.0),
            max_horizon_shrink=options.get("max_horizon_shrink", 0.5),
            regime_weight=options.get("regime_weight", 0.5),
            min_regime_observations=options.get("min_regime_observations", 4),
            robust_clip=options.get("robust_clip", 0.99),
        )
    raise EnsembleError(f"unknown weight scheme {scheme!r}; known: {', '.join(WEIGHT_SCHEMES)}")
