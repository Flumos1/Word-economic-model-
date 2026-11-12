"""AMFE candidate ENSEMBLE_REGIME: pre-registered risk-off regime switch (docs/112).

Design (registered before its own evaluation):
  * risk-off flag at the origin is a real-time early-warning rule: the Sahm-like unemployment gap
    (3-month average UNRATE minus its 12-month low) is >= 0.5 p.p., OR the 10y-3m curve is inverted
    (GS10 - TB3MS < 0).  Both inputs are available at the origin (no look-ahead);
  * risk-off forecast: BVAR_MINNESOTA, the comparator member with the best point accuracy in the
    recession months of the C4 diagnostic (this selection is disclosed as post-diagnostic);
  * risk-on forecast: the inverse-MSE ensemble (the best C1 candidate on T1 h=1).

The candidate was designed after the inverse-MSE candidate failed C4 by RMSE (relative loss 1.0775;
1990-91 1.099, 2001 1.067).  The old candidate is not re-scored; this is a new registration.
"""
from __future__ import annotations

from amfe.forecast import forecast
from amfe.forecast.data import available_series

CANDIDATE_ID = "ENSEMBLE_REGIME"
CANDIDATE_IDS = (CANDIDATE_ID,)
SAHM_THRESHOLD = 0.5
RISK_OFF_MEMBER = "BVAR_MINNESOTA"
RISK_ON = "ENSEMBLE_INVERSE_MSE"
DESCRIPTION = (
    "risk-off regime switch: BVAR_MINNESOTA in Sahm-flagged origins (3m UNRATE gap >= 0.5 p.p. or "
    "inverted 10y-3m curve), inverse-MSE ensemble otherwise"
)


def defensive_flag(origin, data_root=None) -> dict:
    """Real-time risk-off flag from unemployment and the yield-curve slope (origin data only)."""
    info: dict = {"sahm_gap": None, "spread": None, "sahm_flag": False, "inversion_flag": False}
    try:
        unrate, _ = available_series("UNRATE", origin, mode="auto", data_root=data_root)
        values = [float(v) for v in unrate.values]
        if len(values) >= 12:
            gap = sum(values[-3:]) / 3.0 - min(values[-12:])
            info["sahm_gap"] = gap
            info["sahm_flag"] = gap >= SAHM_THRESHOLD
    except Exception:
        pass
    try:
        gs10, _ = available_series("GS10", origin, mode="auto", data_root=data_root)
        tb3, _ = available_series("TB3MS", origin, mode="auto", data_root=data_root)
        spread = float(gs10.values[-1]) - float(tb3.values[-1])
        info["spread"] = spread
        info["inversion_flag"] = spread < 0.0
    except Exception:
        pass
    info["defensive"] = bool(info["sahm_flag"] or info["inversion_flag"])
    return info


def compose(predict):
    """Compose the candidate from cached sub-forecasts; ``predict(kind, name, thunk)`` is the cache."""
    def compute(origin, horizon, series_id, mode):
        info = defensive_flag(origin)
        if info["defensive"]:
            part = forecast(RISK_OFF_MEMBER, "T1_CPI_YOY", origin, horizon,
                            series_id=series_id, mode=mode)
            result = part.to_dict()
            result["regime"] = dict(info, selected=RISK_OFF_MEMBER)
            return result
        result = predict("candidate", RISK_ON, lambda: _inverse_mse(origin, horizon, series_id, mode))
        result = dict(result)
        result["regime"] = dict(info, selected=RISK_ON)
        return result
    return compute


def _inverse_mse(origin, horizon, series_id, mode):
    from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate
    return ensemble_candidate(RISK_ON, "T1_CPI_YOY", origin, horizon,
                              series_id=series_id, mode=mode)


def candidate_forecast(candidate_id: str, target: str, origin, horizon: int,
                       series_id: str | None = None, mode: str = "auto",
                       data_root=None) -> dict:
    """Direct (uncached) API kept for completeness; the runner uses compose() with the cache."""
    if candidate_id != CANDIDATE_ID:
        raise ValueError(f"unknown candidate {candidate_id!r}")
    info = defensive_flag(origin, data_root=data_root)
    if info["defensive"]:
        result = forecast(RISK_OFF_MEMBER, target, origin, horizon,
                          series_id=series_id, mode=mode).to_dict()
    else:
        result = _inverse_mse(origin, horizon, series_id, mode)
    result = dict(result)
    result["regime"] = dict(info, selected=RISK_OFF_MEMBER if info["defensive"] else RISK_ON)
    return result
