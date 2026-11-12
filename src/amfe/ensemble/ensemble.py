"""Adaptive ensemble candidates over the R3 baselines and external forecasts.

The module implements the stage-7 ensemble layer: given the frozen R3 baselines
(``amfe.forecast``), the automated SPF median forecasts (third-party values in the git-ignored
``data/raw/spf``) and a point-in-time calibration history, it produces an ensemble candidate
forecast in exactly the shape the admission harness consumes:

    {"candidate_id", "target", "origin", "horizon",
     "point": float, "quantiles": [19 floats], "probability": None,   # T1/T2
     "point": None,   "quantiles": None,        "probability": float, # T3
     "data_class": "REAL_TIME" | "PSEUDO_REAL_TIME", ...}

Control ensembles (declared before any evaluation, see docs/81)
---------------------------------------------------------------
``ENSEMBLE_EQUAL``        equal weights over all available members.
``ENSEMBLE_INVERSE_MSE``  inverse historical MSE weights.
``ENSEMBLE_BAYES``        Bayesian Dirichlet update over past "closest member" outcomes.
``ENSEMBLE_ADAPTIVE``     the main candidate: inverse-MSE + shrinkage-covariance minimum variance +
                          Bayesian blend, shrunk towards equal weights with the horizon and
                          re-estimated on the current observable regime subset.

No member selection by results
------------------------------
Members are the *mechanically applicable* R3 models at the (target, horizon) pair plus the external
SPF member when its definition maps and it is published at the origin.  The only research choices
are the weighting scheme constants, which are declared in this module and documented in docs/81:
they must be fixed by the candidate registration (code hash) before evaluation, never tuned on the
evaluation windows.

PIT safety
----------
Current member forecasts are point-in-time (``amfe.forecast`` guarantees it).  Calibration history
contains only targets resolved and known before the current origin (``amfe.ensemble.history``).
The regime label is our own observable statistic (treasury spread sign), not the NBER chronology.
"""

from __future__ import annotations

import numpy as np

from amfe.forecast import TARGET_IDS

from .history import (
    current_member_parts,
    default_member_ids,
    history_errors,
    history_rows,
    rate_regime,
)
from .mixture import expected_quantile_bounds, mix_distributions, pool_probabilities
from .sources import (
    KIND_DERIVED_STATISTIC,
    KIND_EXTERNAL_FORECAST,
    KIND_INTERNAL_MODEL,
    EnsembleError,
    external_sources_status,
    member_kind,
)
from .weights import (
    SCHEME_ADAPTIVE,
    SCHEME_BAYES,
    SCHEME_EQUAL,
    SCHEME_INVERSE_MSE,
    WeightResult,
    equal_weights,
    renormalize_available,
    weights_for_scheme,
)

ENSEMBLE_EQUAL = "ENSEMBLE_EQUAL"
ENSEMBLE_INVERSE_MSE = "ENSEMBLE_INVERSE_MSE"
ENSEMBLE_BAYES = "ENSEMBLE_BAYES"
ENSEMBLE_ADAPTIVE = "ENSEMBLE_ADAPTIVE"
ENSEMBLE_IDS = (ENSEMBLE_EQUAL, ENSEMBLE_INVERSE_MSE, ENSEMBLE_BAYES, ENSEMBLE_ADAPTIVE)
CONTROL_ENSEMBLE_IDS = (ENSEMBLE_EQUAL, ENSEMBLE_INVERSE_MSE, ENSEMBLE_BAYES)

ENSEMBLE_SCHEMES = {
    ENSEMBLE_EQUAL: SCHEME_EQUAL,
    ENSEMBLE_INVERSE_MSE: SCHEME_INVERSE_MSE,
    ENSEMBLE_BAYES: SCHEME_BAYES,
    ENSEMBLE_ADAPTIVE: SCHEME_ADAPTIVE,
}

ENSEMBLE_DESCRIPTIONS = {
    ENSEMBLE_EQUAL: "control: equal weights over all available members",
    ENSEMBLE_INVERSE_MSE: "control: inverse historical MSE weights",
    ENSEMBLE_BAYES: "control: Bayesian Dirichlet update over past closest-member outcomes",
    ENSEMBLE_ADAPTIVE: ("main candidate: inverse-MSE + shrinkage-covariance minimum variance + "
                        "Bayesian blend, horizon shrinkage and observable-regime conditioning"),
}

#: Declared weighting constants of the current revision (freeze before evaluation, see docs/81).
ADAPTIVE_OPTIONS = {
    "blend": (0.45, 0.30, 0.25),
    "shrink": 0.5,
    "bayes_decay": 0.05,
    "horizon_scale": 12.0,
    "max_horizon_shrink": 0.5,
    "regime_weight": 0.5,
    "min_regime_observations": 4,
    "prior_strength": 0.02,
    "robust_clip": 0.99,
}

DEFAULT_LOOKBACK = {"T1_CPI_YOY": 12, "T2_GDP_GROWTH": 8, "T3_RECESSION": 10}
MIN_HISTORY_ROWS = 4


class EnsembleForecastError(EnsembleError):
    """The ensemble cannot produce a forecast for the request."""


# --------------------------------------------------------------------------------------
# Weight fitting on a history
# --------------------------------------------------------------------------------------
def fit_weights(
    ensemble_id: str,
    target: str,
    horizon: int,
    rows,
    *,
    member_ids=None,
    current_regime: str | None = None,
    weight_options: dict | None = None,
) -> WeightResult:
    """Estimate weights for one ensemble id from ``history_rows`` output.

    ``member_ids`` is the current member set: members without any calibration history keep the
    prior share instead of being silently dropped from the output.
    """
    if ensemble_id not in ENSEMBLE_IDS:
        raise EnsembleForecastError(
            f"unknown ensemble {ensemble_id!r}; known: {', '.join(ENSEMBLE_IDS)}"
        )
    if target not in TARGET_IDS:
        raise EnsembleForecastError(f"unknown target {target!r}")
    if member_ids is None:
        derived: list[str] = []
        for row in rows:
            for member_id in row["parts"]:
                if member_id not in derived:
                    derived.append(member_id)
        member_ids = derived
    member_ids = list(member_ids)
    scheme = ENSEMBLE_SCHEMES[ensemble_id]
    if scheme == SCHEME_EQUAL or not rows or not member_ids:
        return equal_weights(member_ids or target_member_ids(target, horizon))
    errors, _actuals, regimes = history_errors(rows, target)
    options = dict(ADAPTIVE_OPTIONS)
    if weight_options:
        options.update(weight_options)
    return weights_for_scheme(
        scheme,
        member_ids,
        errors,
        target=target,
        horizon=int(horizon),
        regime_labels=regimes,
        current_regime=current_regime if current_regime is not None else rate_regime(rows[-1]["origin"]),
        **options,
    )


# --------------------------------------------------------------------------------------
# Combination
# --------------------------------------------------------------------------------------
def _data_class_of(parts) -> tuple[str, dict[str, str]]:
    classes = {member_id: str(part.get("data_class", "UNSTATED")) for member_id, part in parts.items()}
    if any(value == "REAL_TIME" for value in classes.values()) and all(
        value == "REAL_TIME" for value in classes.values()
    ):
        return "REAL_TIME", classes
    return "PSEUDO_REAL_TIME", classes


def _external_details(parts, publish_external_values: bool) -> list[dict]:
    details = []
    for member_id, part in parts.items():
        if part.get("kind") != KIND_EXTERNAL_FORECAST:
            continue
        external = dict(part.get("external_forecast") or {})
        if not publish_external_values:
            external.pop("value", None)
        external["member_id"] = member_id
        external["attribution"] = part.get("attribution")
        external["included_as"] = "point forecast; distribution is a derived statistic" if (
            part.get("distribution_kind") == KIND_DERIVED_STATISTIC
        ) else "point forecast only"
        details.append(external)
    return details


def combine_member_forecasts(
    target: str,
    origin,
    horizon: int,
    parts: dict[str, dict],
    weights: dict[str, float] | WeightResult,
    *,
    ensemble_id: str,
    excluded_members: dict[str, str] | None = None,
    history_meta: dict | None = None,
    publish_external_values: bool = False,
    notes=None,
) -> dict:
    """Combine current member forecasts into the harness-compatible candidate dictionary.

    The point forecast is the weighted mean of the available points; for T1/T2 the predictive
    distribution is the weighted mixture of the members' CDFs (``amfe.ensemble.mixture``); for T3
    the event probability is the weighted linear pool.  Missing members have their weights
    redistributed proportionally; the output weights always sum to one.
    """
    if target not in TARGET_IDS:
        raise EnsembleForecastError(f"unknown target {target!r}")
    if not parts:
        raise EnsembleForecastError("no member forecasts are available at this origin")
    if isinstance(weights, WeightResult):
        weight_result = weights
        raw_weights = dict(weights.weights)
    else:
        raw_weights = {member_id: float(value) for member_id, value in weights.items()}
        weight_result = WeightResult(
            weights=raw_weights,
            method=f"{ensemble_id}_EXPLICIT_WEIGHTS",
            member_ids=tuple(raw_weights),
        )
    available = list(parts)
    missing_weight = [member_id for member_id in raw_weights if member_id not in parts]
    weights_available = renormalize_available(raw_weights, available)
    point_members = [m for m in available if parts[m].get("point") is not None]
    distribution_members = [m for m in available if parts[m].get("quantiles") is not None]
    probability_members = [m for m in available if parts[m].get("probability") is not None]
    data_class, part_classes = _data_class_of(parts)
    result: dict = {
        "candidate_id": ensemble_id,
        "ensemble_id": ensemble_id,
        "target": target,
        "origin": origin.isoformat() if hasattr(origin, "isoformat") else str(origin),
        "horizon": int(horizon),
        "point": None,
        "quantiles": None,
        "probability": None,
        "data_class": data_class,
        "member_data_classes": part_classes,
        "members": available,
        "member_weights": {m: float(w) for m, w in weights_available.items()},
        "member_weights_all": {m: float(w) for m, w in raw_weights.items()},
        "member_kinds": {m: str(member_kind(m)) for m in available},
        "distribution_members": distribution_members,
        "point_members": point_members,
        "probability_members": probability_members,
        "excluded_members": dict(excluded_members or {}),
        "weight_method": weight_result.method,
        "weight_diagnostics": weight_result.diagnostics,
        "effective_n": float(1.0 / np.sum(np.asarray(list(weights_available.values())) ** 2)),
        "external_forecasts": _external_details(parts, publish_external_values),
        "external_sources": external_sources_status(),
        "history": dict(history_meta or {}),
        "notes": list(notes or []),
    }
    if missing_weight:
        result["notes"].append(
            f"weights of unavailable members redistributed over available members: {sorted(missing_weight)}"
        )
    if target == "T3_RECESSION":
        if probability_members:
            probabilities = [float(parts[m]["probability"]) for m in probability_members]
            pool_weights = renormalize_available(weights_available, probability_members)
            result["probability"] = pool_probabilities(
                probabilities, [pool_weights[m] for m in probability_members], method="linear"
            )
            result["point"] = None
            result["quantiles"] = None
            result["probability_members"] = probability_members
            result["member_probabilities"] = {
                member_id: float(parts[member_id]["probability"]) for member_id in probability_members
            }
            result["notes"].append("T3: weighted linear pool of member event probabilities")
        else:
            raise EnsembleForecastError("T3 ensemble has no member event probabilities")
        return result
    if not point_members:
        raise EnsembleForecastError("ensemble has no member point forecasts")
    point_weights = renormalize_available(weights_available, point_members)
    result["point"] = float(sum(point_weights[m] * float(parts[m]["point"]) for m in point_members))
    result["member_points"] = {member_id: float(parts[member_id]["point"]) for member_id in point_members}
    if distribution_members:
        distribution_weights = renormalize_available(weights_available, distribution_members)
        vectors = [np.asarray(parts[m]["quantiles"], dtype=float) for m in distribution_members]
        mixed = mix_distributions(vectors, [distribution_weights[m] for m in distribution_members])
        result["quantiles"] = [float(value) for value in mixed]
        result["distribution_weights"] = distribution_weights
        result["distribution_diagnostics"] = {
            "n_members": len(distribution_members),
            "weights": distribution_weights,
            "bounds": {
                key: [float(value) for value in values]
                for key, values in expected_quantile_bounds(vectors).items()
                if key != "levels"
            },
        }
    else:
        result["notes"].append("no member has a predictive distribution: quantiles are None")
    if target == "T1_CPI_YOY":
        result["notes"].append("T1: external SPF is not used (no monthly y/y CPI forecast in SPF)")
    return result


# --------------------------------------------------------------------------------------
# Public forecast API
# --------------------------------------------------------------------------------------
def target_member_ids(target: str, horizon: int) -> list[str]:
    """Mechanically applicable internal members plus the external SPF member for T2."""
    return default_member_ids(target, horizon, include_external=True)


def ensemble_forecast(
    ensemble_id: str,
    target: str,
    origin,
    horizon: int,
    *,
    series_id: str | None = None,
    mode: str = "auto",
    data_root=None,
    lookback: int | None = None,
    min_history: int = MIN_HISTORY_ROWS,
    member_ids=None,
    weight_options: dict | None = None,
    publish_external_values: bool = False,
) -> dict:
    """One ensemble candidate forecast at one origin (harness-compatible dict)."""
    if ensemble_id not in ENSEMBLE_IDS:
        raise EnsembleForecastError(f"unknown ensemble {ensemble_id!r}")
    if target not in TARGET_IDS:
        raise EnsembleForecastError(f"unknown target {target!r}")
    if member_ids is None:
        member_ids = target_member_ids(target, horizon)
    member_ids = list(member_ids)
    if not member_ids:
        raise EnsembleForecastError("empty member list")
    from amfe.forecast.data import month_end_of

    origin_date = month_end_of(origin)
    parts, excluded = current_member_parts(
        target, origin_date, int(horizon), member_ids,
        series_id=series_id, mode=mode, data_root=data_root,
    )
    if not parts:
        raise EnsembleForecastError(
            f"no member is available for {target} h={horizon} at {origin_date.isoformat()}: {excluded}"
        )
    current_regime = rate_regime(origin_date, data_root=data_root)
    history_meta = {
        "n_rows": 0,
        "requested_lookback": None,
        "weight_source": "EQUAL_NO_HISTORY",
        "current_regime": current_regime,
        "actual_sources": [],
    }
    notes: list[str] = []
    scheme = ENSEMBLE_SCHEMES[ensemble_id]
    if scheme == SCHEME_EQUAL:
        weights = equal_weights(list(parts))
        history_meta["weight_source"] = "EQUAL_DECLARED_CONTROL"
    else:
        look = int(lookback) if lookback is not None else DEFAULT_LOOKBACK[target]
        history_meta["requested_lookback"] = look
        rows = history_rows(
            target, origin_date, int(horizon), list(parts),
            series_id=series_id, mode=mode, data_root=data_root, lookback=look,
        )
        history_meta.update(
            {
                "n_rows": len(rows),
                "first_origin": rows[0]["origin"] if rows else None,
                "last_origin": rows[-1]["origin"] if rows else None,
                "actual_sources": sorted({str(row.get("actual_source")) for row in rows}),
                "regimes": sorted({str(row.get("regime")) for row in rows}),
            }
        )
        if len(rows) < int(min_history):
            weights = equal_weights(list(parts))
            history_meta["weight_source"] = "EQUAL_FALLBACK_INSUFFICIENT_HISTORY"
            notes.append(
                f"only {len(rows)} resolved calibration origins (< {min_history}); equal weights used"
            )
        else:
            weights = fit_weights(
                ensemble_id, target, int(horizon), rows, member_ids=list(parts),
                current_regime=current_regime, weight_options=weight_options,
            )
            history_meta["weight_source"] = "HISTORY"
    result = combine_member_forecasts(
        target, origin_date, int(horizon), parts, weights,
        ensemble_id=ensemble_id, excluded_members=excluded, history_meta=history_meta,
        publish_external_values=publish_external_values, notes=notes,
    )
    result["weight_scheme"] = ENSEMBLE_SCHEMES[ensemble_id]
    result["description"] = ENSEMBLE_DESCRIPTIONS[ensemble_id]
    result["declared_options"] = dict(ADAPTIVE_OPTIONS)
    result["notes"].append(
        "ensemble candidate; external forecasts enter only as third-party point forecasts and "
        "their distributions are project-derived statistics"
    )
    return result


def candidate_forecast(ensemble_id: str, target: str, origin, horizon: int,
                       series_id: str | None = None, mode: str = "auto", data_root=None) -> dict:
    """Signature-compatible with ``amfe.candidates.models.candidate_forecast`` (harness runner)."""
    return ensemble_forecast(ensemble_id, target, origin, horizon, series_id=series_id,
                             mode=mode, data_root=data_root)


# --------------------------------------------------------------------------------------
# Diagnostics (derived statistics, never forecasts)
# --------------------------------------------------------------------------------------
def ensemble_diagnostics(result: dict) -> dict:
    """Concentration/agreement statistics of an ensemble forecast.

    Everything here is a ``DERIVED_STATISTIC`` of the project: weights, concentration (HHI),
    effective number of members, the internal-external gap and member agreement.  It is reported for
    audit only and never enters a forecast.
    """
    weights = dict(result.get("member_weights") or {})
    if not weights:
        return {"kind": KIND_DERIVED_STATISTIC, "error": "NO_WEIGHTS"}
    values = np.asarray(list(weights.values()), dtype=float)
    hhi = float(np.sum(values ** 2))
    members = list(weights)
    kinds = {member_id: member_kind(member_id) for member_id in members}
    external_share = float(
        sum(weight for member_id, weight in weights.items()
            if kinds[member_id] == KIND_EXTERNAL_FORECAST)
    )
    points = dict(result.get("member_points") or {})
    internal_values = [points[m] for m in points if kinds.get(m) == KIND_INTERNAL_MODEL]
    external_values = [points[m] for m in points if kinds.get(m) == KIND_EXTERNAL_FORECAST]
    probabilities = dict(result.get("member_probabilities") or {})
    diagnostics = {
        "kind": KIND_DERIVED_STATISTIC,
        "classification": "DERIVED_STATISTIC: computed from member weights/forecasts, not a forecast",
        "n_members": len(weights),
        "hhi": hhi,
        "effective_n": float(1.0 / hhi) if hhi > 0 else None,
        "external_weight_share": external_share,
        "internal_weight_share": float(1.0 - external_share),
        "members": members,
        "weights": {member_id: float(w) for member_id, w in weights.items()},
        "member_kinds": kinds,
        "data_class": result.get("data_class"),
    }
    if points:
        diagnostics["member_point_mean"] = float(np.mean(list(points.values())))
        diagnostics["member_point_agreement_std"] = float(np.std(list(points.values())))
    if internal_values and external_values:
        diagnostics["internal_external_gap"] = float(np.mean(internal_values) - np.mean(external_values))
        diagnostics["note_gap"] = "mean internal member point minus external forecast point"
    if probabilities:
        diagnostics["member_probability_mean"] = float(np.mean(list(probabilities.values())))
        diagnostics["member_probability_agreement_std"] = float(np.std(list(probabilities.values())))
    return diagnostics
