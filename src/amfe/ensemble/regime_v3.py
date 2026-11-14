"""AMFE candidate ENSEMBLE_REGIME_V3: BVAR location + inverse-MSE shape in risk-off.

Third, separately registered iteration (docs/116).  Design (registered before its own evaluation):

  * risk-off flag: the Sahm-like rule only (3m average UNRATE minus its 12m low >= 0.5 p.p.), exactly
    as in v2 (docs/115): no yield-curve trigger, no threshold change.  Both inputs are available at
    the origin (no look-ahead);
  * risk-off forecast (``Sahm >= 0.5``): a *coherent location/shape composition*
        point      = BVAR_MINNESOTA point
        quantiles  = ENSEMBLE_INVERSE_MSE quantiles + (BVAR point - inverse-MSE point)
    i.e. the shape of the inverse-MSE distribution with its location anchored to the BVAR point;
  * risk-on forecast: ENSEMBLE_INVERSE_MSE in full (unchanged from v1/v2).

Motivation (disclosed as post-diagnostic): the first two iterations exposed a point/distribution
trade-off.  v1 (docs/112) selected BVAR_MINNESOTA in flagged origins and fixed C4 RMSE
(0.9734/0.9035) but failed C4 CRPS (1.0019/1.0133) and C1 W2 (0.9705).  v2 (docs/115) shrank the
whole risk-off forecast 50/50 toward the frozen comparator, which recovered C1/C2 but lost C4 RMSE on
W1_EXT (1.0277) and still failed C4 CRPS on W2 (1.0019).  The diagnostics therefore say: the BVAR
point is the better location in recessions, the inverse-MSE distribution is the better shape.
v3 keeps those two and combines them, instead of averaging the point and the distribution together.
The component choice is post-diagnostic (it uses the C4 diagnostic and the published v1/v2 results);
it is not a re-score of v1 or v2, which remain published as they are.  No weight, no threshold and no
member is tuned against the v3 evaluation result.

Hypotheses tested (fixed before the run): H1 C4 RMSE <= 1.00 on W1_EXT and W2 (the BVAR location in
flagged recession months); H2 C4 CRPS <= 1.00 on W1_EXT and W2 (the inverse-MSE shape); H3 C1/C2/C5
stay passed as in v2.  If any fails, the candidate stays a CANDIDATE and the failure is reported.
"""
from __future__ import annotations

import numpy as np

from amfe.forecast import forecast
from amfe.forecast.data import available_series

CANDIDATE_ID = "ENSEMBLE_REGIME_V3"
CANDIDATE_IDS = (CANDIDATE_ID,)
SAHM_THRESHOLD = 0.5
RISK_OFF_MEMBER = "BVAR_MINNESOTA"
RISK_ON = "ENSEMBLE_INVERSE_MSE"
TARGET = "T1_CPI_YOY"
DESCRIPTION = (
    "risk-off switch (Sahm >= 0.5): point = BVAR_MINNESOTA, quantiles = ENSEMBLE_INVERSE_MSE "
    "quantiles shifted by (BVAR point - inverse-MSE point); inverse-MSE ensemble otherwise"
)
SELECTED_RISK_OFF = f"{RISK_OFF_MEMBER}_LOCATION+{RISK_ON}_SHAPE"


def defensive_flag(origin, data_root=None) -> dict:
    """Sahm-like real-time flag: 3m average UNRATE minus 12m low >= 0.5 p.p. (origin data only)."""
    info: dict = {"sahm_gap": None, "defensive": False}
    try:
        unrate, _ = available_series("UNRATE", origin, mode="auto", data_root=data_root)
        values = [float(v) for v in unrate.values]
        if len(values) >= 12:
            gap = sum(values[-3:]) / 3.0 - min(values[-12:])
            info["sahm_gap"] = gap
            info["defensive"] = bool(gap >= SAHM_THRESHOLD)
    except Exception:
        pass
    return info


def combine_risk_off(risk_off_member: dict, shape_candidate: dict) -> dict:
    """Location of ``risk_off_member`` + shape of ``shape_candidate`` (pure, deterministic).

    point := risk_off_member point
    quantiles := shape_candidate quantiles + (risk_off_member point - shape_candidate point)

    The quantile shift is a translation, so a monotone source grid stays monotone.  The point is the
    member point, which may differ slightly from the (shifted) central quantile: the point is a
    member average, the quantile grid is a mixture quantile grid, and the design deliberately anchors
    the location on the point rather than re-centring the grid on the median.
    """
    bvar = dict(risk_off_member)
    shape = dict(shape_candidate)
    bvar_point = float(bvar["point"])
    shape_point = float(shape["point"])
    shift = bvar_point - shape_point
    quantiles = np.asarray(shape["quantiles"], dtype=float) + shift
    if quantiles.ndim != 1 or quantiles.size == 0:
        raise ValueError("shape candidate must provide a non-empty quantile grid")
    if not np.all(np.isfinite(quantiles)):
        raise ValueError("shifted quantiles must be finite")
    if np.any(np.diff(quantiles) < 0.0):
        raise ValueError("shifted quantiles must be monotone non-decreasing")
    result = dict(shape)
    result["point"] = bvar_point
    result["quantiles"] = [float(v) for v in quantiles]
    result["probability"] = None
    classes = [str(bvar.get("data_class")), str(shape.get("data_class"))]
    result["data_class"] = min(classes, key=lambda c: 0 if c == "REAL_TIME" else 1)
    result["vintage_date"] = max(
        (v for v in (bvar.get("vintage_date"), shape.get("vintage_date")) if v), default=None)
    return result


def compose(predict):
    """Compose from cached member/candidate forecasts; ``predict(kind, name, thunk)`` is the cache."""
    def compute(origin, horizon, series_id, mode):
        info = defensive_flag(origin)
        if not info["defensive"]:
            result = dict(predict("candidate", RISK_ON, lambda: _inverse_mse(
                origin, horizon, series_id, mode)))
            result["regime"] = dict(info, selected=RISK_ON)
            return result
        bvar = predict("model", RISK_OFF_MEMBER, lambda: forecast(
            RISK_OFF_MEMBER, TARGET, origin, horizon, series_id=series_id, mode=mode).to_dict())
        shape = predict("candidate", RISK_ON, lambda: _inverse_mse(
            origin, horizon, series_id, mode))
        result = combine_risk_off(bvar, shape)
        result["regime"] = dict(info, selected=SELECTED_RISK_OFF)
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
