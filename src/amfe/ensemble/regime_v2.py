"""AMFE candidate ENSEMBLE_REGIME_V2: risk-off switch with a shrunk-toward-comparator distribution.

Second, separately registered iteration (docs/115).  Changes from v1 (docs/112):
  * the risk-off flag is the Sahm-like rule only (3m UNRATE gap >= 0.5 p.p.); the yield-curve
    inversion condition is dropped (it did not flag the recession months and added noise);
  * the risk-off forecast is a 50/50 shrinkage of BVAR_MINNESOTA toward the frozen comparator
    distribution: point = 0.5*BVAR + 0.5*comparator point, quantiles = 0.5*BVAR + 0.5*comparator
    mixture quantiles.  Rationale: v1 fixed the point loss in recessions but the BVAR distribution
    was worse (C4 CRPS 1.0019/1.0133); a standard robustification is to shrink to the benchmark;
  * risk-on: inverse-MSE ensemble.

The candidate is registered before its own evaluation; v1 results stay published.
"""
from __future__ import annotations

import numpy as np

from amfe.evaluation.distributions import mixture_quantiles
from amfe.forecast import forecast
from amfe.forecast.data import available_series

CANDIDATE_ID = "ENSEMBLE_REGIME_V2"
CANDIDATE_IDS = (CANDIDATE_ID,)
SAHM_THRESHOLD = 0.5
RISK_OFF_MEMBER = "BVAR_MINNESOTA"
RISK_ON = "ENSEMBLE_INVERSE_MSE"
SHRINK = 0.5
MEMBERS = ("AO", "AR_BIC", "BVAR_MINNESOTA", "RW")
TARGET = "T1_CPI_YOY"
DESCRIPTION = (
    "risk-off switch (Sahm >= 0.5): 50/50 shrinkage of BVAR_MINNESOTA toward the comparator "
    "distribution; inverse-MSE ensemble otherwise"
)


def defensive_flag(origin, data_root=None) -> dict:
    """Sahm-like real-time flag: 3m average UNRATE minus 12m low >= 0.5 p.p. (origin data only)."""
    info: dict = {"sahm_gap": None, "defensive": False}
    try:
        unrate, _ = available_series("UNRATE", origin, mode="auto", data_root=data_root)
        values = [float(v) for v in unrate.values]
        if len(values) >= 12:
            gap = sum(values[-3:]) / 3.0 - min(values[-12:])
            info["sahm_gap"] = gap
            info["defensive"] = gap >= SAHM_THRESHOLD
    except Exception:
        pass
    return info


def compose(predict):
    """Compose from cached member/candidate forecasts; ``predict(kind, name, thunk)`` is the cache."""
    def compute(origin, horizon, series_id, mode):
        info = defensive_flag(origin)
        if not info["defensive"]:
            result = dict(predict("candidate", RISK_ON, lambda: _inverse_mse(
                origin, horizon, series_id, mode)))
            result["regime"] = dict(info, selected=RISK_ON)
            return result
        parts = {}
        for member in MEMBERS:
            parts[member] = predict("model", member, lambda m=member: forecast(
                m, TARGET, origin, horizon, series_id=series_id, mode=mode).to_dict())
        bvar = parts[RISK_OFF_MEMBER]
        comp_point = float(np.mean([float(p["point"]) for p in parts.values()]))
        comp_quantiles = np.asarray(mixture_quantiles(
            [np.asarray(p["quantiles"], dtype=float) for p in parts.values()]), dtype=float)
        bvar_point = float(bvar["point"])
        bvar_quantiles = np.asarray(bvar["quantiles"], dtype=float)
        result = {
            "point": float(SHRINK * bvar_point + (1 - SHRINK) * comp_point),
            "quantiles": [float(v) for v in SHRINK * bvar_quantiles + (1 - SHRINK) * comp_quantiles],
            "data_class": min((str(p["data_class"]) for p in parts.values()),
                              key=lambda c: 0 if c == "REAL_TIME" else 1),
            "probability": None,
            "vintage_date": max((p.get("vintage_date") for p in parts.values()
                                 if p.get("vintage_date")), default=None),
            "regime": dict(info, selected=f"{RISK_OFF_MEMBER}+COMPARATOR_{SHRINK}"),
        }
        return result
    return compute


def _inverse_mse(origin, horizon, series_id, mode):
    from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate
    return ensemble_candidate(RISK_ON, TARGET, origin, horizon, series_id=series_id, mode=mode)


def candidate_forecast(candidate_id: str, target: str, origin, horizon: int,
                       series_id: str | None = None, mode: str = "auto",
                       data_root=None) -> dict:
    """Direct (uncached) API kept for completeness; the runner uses compose() with the cache."""
    if candidate_id != CANDIDATE_ID:
        raise ValueError(f"unknown candidate {candidate_id!r}")

    def predict(kind, name, thunk):
        return thunk()

    return compose(predict)(origin, horizon, series_id, mode)
