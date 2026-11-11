"""Candidate models for the first real evaluation.

Candidates are deliberately outside the frozen R3 comparator; they are registered before they are
evaluated (protocol model_class_rules.registry_access).  This first candidate is an equal-weight
ensemble of two R3 baselines (RW and AR_BIC): the candidate's point forecast is the mean of the
members, its predictive distribution is the equal-weight mixture of the members' 19-quantile
forecasts (proper mixture CDF inversion, see amfe.evaluation.distributions).
"""
from __future__ import annotations

from pathlib import Path

from amfe.forecast import forecast
from amfe.forecast.models import ForecastError
from amfe.evaluation.distributions import mixture_quantiles

CANDIDATE_IDS = ("ENSEMBLE_RW_AR",)
CANDIDATE_TARGETS = {"ENSEMBLE_RW_AR": ("T1_CPI_YOY", "T2_GDP_GROWTH")}
CANDIDATE_MEMBERS = {"ENSEMBLE_RW_AR": ("RW", "AR_BIC")}
CANDIDATE_DESCRIPTION = {
    "ENSEMBLE_RW_AR": ("equal-weight ensemble of the RW and AR_BIC baselines: the point is their mean, "
                       "the predictive distribution is the mixture of their 19-quantile distributions"),
}


def candidate_forecast(candidate_id: str, target: str, origin, horizon: int,
                       series_id: str | None = None, mode: str = "auto", data_root=None) -> dict:
    if candidate_id not in CANDIDATE_IDS:
        raise ValueError(f"unknown candidate {candidate_id!r}")
    if target not in CANDIDATE_TARGETS[candidate_id]:
        raise ForecastError(f"{candidate_id} does not apply to {target}")
    members = CANDIDATE_MEMBERS[candidate_id]
    parts = [forecast(m, target, origin, horizon, series_id=series_id, mode=mode,
                      data_root=data_root) for m in members]
    points = [p.point for p in parts if p.point is not None]
    if not points:
        raise ForecastError(f"{candidate_id}: no member produced a point forecast")
    quantiles = [p.quantiles for p in parts if p.quantiles is not None]
    if not quantiles:
        raise ForecastError(f"{candidate_id}: no member produced a distribution")
    data_classes = [str(p.data_class) for p in parts]
    data_class = "REAL_TIME" if all(d == "REAL_TIME" for d in data_classes) else "PSEUDO_REAL_TIME"
    mixed = mixture_quantiles(quantiles)
    return {
        "candidate_id": candidate_id,
        "target": target,
        "origin": origin.isoformat() if hasattr(origin, "isoformat") else str(origin),
        "horizon": int(horizon),
        "point": float(sum(points) / len(points)),
        "quantiles": [float(v) for v in mixed],
        "probability": None,
        "data_class": data_class,
        "member_data_classes": data_classes,
        "members": list(members),
        "notes": ["candidate = equal-weight ensemble of R3 baselines; mixture distribution"],
    }
