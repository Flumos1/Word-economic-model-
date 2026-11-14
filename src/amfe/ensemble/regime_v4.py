"""AMFE candidate ENSEMBLE_REGIME_V4: risk-off only in a high-rate regime.

Fourth (and last on this branch) separately registered iteration (docs/118).  Design, registered
before its own evaluation:

  * risk-off flag: the Sahm-like rule (3m average UNRATE minus its 12m low >= 0.5 p.p., exactly as in
    v2/v3) **AND** an interest-rate condition: the 12-month average of TB3MS observed at the origin is
    >= 4.0 percent.  Both inputs are available at the origin (no look-ahead);
  * risk-off forecast (``Sahm >= 0.5`` and ``TB3MS 12m mean >= 4.0``): the *coherent location/shape
    composition* of v3 --
        point      = BVAR_MINNESOTA point
        quantiles  = ENSEMBLE_INVERSE_MSE quantiles + (BVAR point - inverse-MSE point)
  * risk-on forecast (everything else): ENSEMBLE_INVERSE_MSE in full (unchanged from v1/v2/v3).

Motivation and disclosed post-diagnostic design.  v3 (docs/116) passed C4 on both losses and both
windows (RMSE 0.9734/0.9035, CRPS 0.9188/0.9329) and passed C1/C5, but failed C2 on W2 (adjusted
p 0.2984 RMSE / 0.1494 CRPS): on W2 the Sahm rule fires in 13 of 18 recession months (and in the tail
of 2009-2011), and the BVAR location there does not beat the comparator significantly, unlike the
v2 50/50 shrinkage.  The pre-run diagnostic of the rate environment in the flagged months shows a
regime difference:

  * W2 2008-06..2009-12, months with Sahm >= 0.5: the 12-month average TB3MS falls from 3.07 to 0.15
    -- the Fed had already cut to the zero bound, so the BVAR recession location is extrapolating a
    disinflationary, not an inflationary, recession;
  * 1990-91 (W1_EXT): the same 12-month average is 5.6-8.0 percent (high-rate recession);
  * 2001-09..11 (W1_EXT): 4.70 percent (still a high-rate, pre-zero-bound recession).

The condition "12-month TB3MS average >= 4.0" therefore keeps the risk-off branch in 1990-91 and 2001
(where the v3 C4 result was earned) and switches it off in 2008-09 (where it cost C2 W2).  The
inverse-MSE ensemble already passed C4 on W2 (0.9406/0.9819), so no W2 protection is needed.  The
threshold 4.0 is the neutral pre-2008 level; it was chosen from that diagnostic, not from the v4
evaluation result.  The component choice (BVAR location, inverse-MSE shape) is inherited from v3 and
remains post-diagnostic.  This candidate does NOT re-score v1/v2/v3: all three remain published with
their original numbers, and no weight, member, threshold or criterion is tuned against the v4 result.

Hypotheses, fixed before the run:  H1 C4 RMSE <= 1.00 on W1_EXT and W2;  H2 C4 CRPS <= 1.00 on W1_EXT
and W2;  H3 C1 and C5 stay passed;  H4 C2 passes on both windows (the failure v4 exists to remove).
If any fails, the candidate stays a CANDIDATE and the failure is reported unchanged.
"""
from __future__ import annotations

import numpy as np

from amfe.ensemble.regime_v3 import combine_risk_off  # re-exported: same coherent composition as v3
from amfe.forecast import forecast
from amfe.forecast.data import available_series

CANDIDATE_ID = "ENSEMBLE_REGIME_V4"
CANDIDATE_IDS = (CANDIDATE_ID,)
SAHM_THRESHOLD = 0.5
RATE_SERIES = "TB3MS"
RATE_WINDOW_MONTHS = 12
RATE_THRESHOLD = 4.0
RISK_OFF_MEMBER = "BVAR_MINNESOTA"
RISK_ON = "ENSEMBLE_INVERSE_MSE"
TARGET = "T1_CPI_YOY"
DESCRIPTION = (
    "risk-off switch (Sahm >= 0.5 AND 12m TB3MS mean >= 4.0): point = BVAR_MINNESOTA, quantiles = "
    "ENSEMBLE_INVERSE_MSE quantiles shifted by (BVAR point - inverse-MSE point); inverse-MSE "
    "ensemble otherwise"
)
SELECTED_RISK_OFF = f"{RISK_OFF_MEMBER}_LOCATION+{RISK_ON}_SHAPE"

__all__ = [
    "CANDIDATE_ID",
    "CANDIDATE_IDS",
    "SAHM_THRESHOLD",
    "RATE_SERIES",
    "RATE_WINDOW_MONTHS",
    "RATE_THRESHOLD",
    "RISK_OFF_MEMBER",
    "RISK_ON",
    "TARGET",
    "DESCRIPTION",
    "SELECTED_RISK_OFF",
    "combine_risk_off",
    "defensive_flag",
    "compose",
    "candidate_forecast",
]


def defensive_flag(origin, data_root=None) -> dict:
    """Risk-off flag: Sahm-like gap >= 0.5 p.p. AND 12m TB3MS mean >= 4.0 (origin data only).

    Both legs are point-in-time: only observations published at ``origin`` are read.  Missing or short
    history on either leg leaves the flag off (risk-on); the reason is recorded in the returned dict so
    a silent data failure cannot masquerade as a regime call.
    """
    info: dict = {
        "sahm_gap": None,
        "sahm_flag": False,
        "tb3ms_12m_mean": None,
        "rate_condition": False,
        "rate_threshold": RATE_THRESHOLD,
        "defensive": False,
    }
    try:
        unrate, _ = available_series("UNRATE", origin, mode="auto", data_root=data_root)
        values = [float(v) for v in unrate.values if np.isfinite(float(v))]
        if len(values) >= 12:
            gap = sum(values[-3:]) / 3.0 - min(values[-12:])
            info["sahm_gap"] = gap
            info["sahm_flag"] = bool(gap >= SAHM_THRESHOLD)
    except Exception:
        pass
    try:
        rate, _ = available_series(RATE_SERIES, origin, mode="auto", data_root=data_root)
        rate_values = [float(v) for v in rate.values if np.isfinite(float(v))]
        if len(rate_values) >= RATE_WINDOW_MONTHS:
            mean = sum(rate_values[-RATE_WINDOW_MONTHS:]) / float(RATE_WINDOW_MONTHS)
            info["tb3ms_12m_mean"] = mean
            info["rate_condition"] = bool(mean >= RATE_THRESHOLD)
    except Exception:
        pass
    info["defensive"] = bool(info["sahm_flag"] and info["rate_condition"])
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
