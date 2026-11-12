"""Walk-forward history for ensemble weight calibration, and the member-forecast cache.

The ensemble needs two things at an origin:

1. the current member forecasts (``current_member_parts``), and
2. a *point-in-time* history of member forecasts and resolved actuals from past origins
   (``history_rows``), used by the weighting schemes.

Point-in-time discipline
------------------------
* A history origin is admitted only when its target actual was already resolved and known at the
  current origin: for T1/T2 the first-publication vintage date must be on or before the current
  origin (fallback: the target period end plus the publication lag); for T3 the event interval must
  be at least :data:`T3_RESOLUTION_LAG_MONTHS` months behind the current origin.  A target that is
  not resolved yet is skipped, never filled with a placeholder.
* Actuals are the protocol's first-publication actuals when the evaluation layer can supply them
  (``amfe.evaluation.actuals.actual_pair``); the latest tape is only a documented fallback.
* The regime label is the project's own observable statistic (sign of the GS10-TB3MS spread from
  the monthly treasury averages), not the NBER chronology: NBER dates are published with a lag and
  using the final chronology to classify a past "current regime" would be look-ahead.

The member forecast cache is a pure optimisation: identical (member, target, horizon, origin,
series, mode, data_root) calls return the cached ``to_dict()`` result, so re-running a candidate over
the same origins is cheap and deterministic.  ``clear_cache()`` empties it.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from amfe.forecast import ForecastError, applicable_models, forecast
from amfe.forecast.data import (
    ForecastDataError,
    add_months,
    as_date,
    load_latest,
    month_end_of,
    month_index,
    target_period,
)
from amfe.forecast.models import QUANTILE_LEVELS

from .sources import (
    KIND_DERIVED_STATISTIC,
    KIND_EXTERNAL_FORECAST,
    EnsembleError,
    spf_forecast,
    survey_quarter_for_origin,
)

T3_RESOLUTION_LAG_MONTHS = 12
POSITIVE_SPREAD = "POSITIVE_SPREAD"
INVERTED_OR_FLAT = "INVERTED_OR_FLAT"
UNKNOWN_REGIME = "UNKNOWN"

_MEMBER_FORECAST_CACHE: dict = {}
_MEMBER_CACHE_LIMIT = 20000


def clear_cache() -> None:
    """Empty the member-forecast and regime caches (tests and long evaluation runs)."""
    _MEMBER_FORECAST_CACHE.clear()


# --------------------------------------------------------------------------------------
# Actuals and their point-in-time availability
# --------------------------------------------------------------------------------------
def resolved_actual(target: str, origin, horizon: int, series_id: str | None = None,
                    data_root=None) -> dict | None:
    """First-publication actual if available, otherwise the latest-tape target value.

    Returns ``{"value", "vintage", "source", "period_end"}`` or ``None`` when the target is
    unresolved.  The caller must still check ``actual_known_by`` before using it for calibration.
    """
    origin_date = as_date(origin)
    horizon_i = int(horizon)
    if target == "T3_RECESSION":
        try:
            from amfe.evaluation.actuals import actual_pair

            pair = actual_pair(target, origin_date, horizon_i, series_id=series_id, data_root=data_root)
        except Exception:  # pragma: no cover - evaluation layer is optional for synthetic use
            pair = None
        if pair is not None and pair.get("primary") is not None:
            return {"value": float(pair["primary"]), "vintage": None,
                    "source": "NBER_CHRONOLOGY", "period_end": pair.get("period_end")}
        from amfe.forecast.data import target_actual

        value = target_actual(target, origin_date, horizon_i, series_id=series_id, data_root=data_root)
        if value is None:
            return None
        freq, period = target_period(target, origin_date, horizon_i)
        return {"value": float(value), "vintage": None, "source": "NBER_CHRONOLOGY",
                "period_end": None, "period": int(period), "freq": freq}
    try:
        from amfe.evaluation.actuals import actual_pair

        pair = actual_pair(target, origin_date, horizon_i, series_id=series_id, data_root=data_root)
    except Exception:  # pragma: no cover
        pair = None
    if pair is not None and pair.get("primary") is not None:
        return {
            "value": float(pair["primary"]),
            "vintage": pair.get("primary_vintage"),
            "source": "FIRST_PUBLICATION",
            "period_end": pair.get("period_end"),
        }
    from amfe.forecast.data import target_actual

    value = target_actual(target, origin_date, horizon_i, series_id=series_id, data_root=data_root)
    if value is None:
        return None
    freq, period = target_period(target, origin_date, horizon_i)
    return {
        "value": float(value),
        "vintage": None,
        "source": "LATEST_TAPE_FALLBACK",
        "period_end": _period_end_iso(freq, period),
        "period": int(period),
        "freq": freq,
    }


def _period_end_iso(freq: str, period: int) -> str | None:
    from amfe.forecast.data import month_start_date, quarter_end_month

    if freq == "M":
        d = month_start_date(int(period))
        return month_end_of(date(d.year, d.month, 1)).isoformat()
    if freq == "Q":
        from amfe.evaluation.actuals import period_end_date

        return period_end_date("Q", int(period)).isoformat()
    return None


def actual_known_by(target: str, history_origin, horizon: int, current_origin,
                    record: dict | None) -> bool:
    """True when the history target actual was resolved and known at ``current_origin``."""
    if record is None or record.get("value") is None:
        return False
    current = month_end_of(current_origin)
    if target == "T3_RECESSION":
        event_end = month_index(history_origin) + int(horizon)
        return event_end + T3_RESOLUTION_LAG_MONTHS <= month_index(current)
    vintage = record.get("vintage")
    if isinstance(vintage, str):
        try:
            return date.fromisoformat(vintage[:10]) <= current
        except ValueError:
            pass
    period_end = record.get("period_end")
    if isinstance(period_end, str):
        try:
            end_date = date.fromisoformat(period_end[:10])
        except ValueError:
            return False
        lag = 1 if target == "T1_CPI_YOY" else 3
        return month_index(add_months(end_date, lag)) <= month_index(current)
    return False


def rate_regime(origin, data_root=None) -> str:
    """Observable regime label from the sign of the GS10-TB3MS spread at the origin.

    The monthly treasury averages are published with a one-month lag and are not revised in any
    meaningful way; the label is the project's own *statistic*, not a third-party forecast.
    """
    from amfe.forecast.data import month_index as _month_index

    origin_month = _month_index(as_date(origin))
    try:
        gs10 = load_latest("GS10", data_root)
        tb3 = load_latest("TB3MS", data_root)
    except ForecastDataError:
        return UNKNOWN_REGIME
    month = origin_month - 1  # last published month in the PSEUDO_REAL_TIME convention
    gs10_value = gs10.value_at(month)
    tb3_value = tb3.value_at(month)
    if gs10_value is None or tb3_value is None:
        return UNKNOWN_REGIME
    return INVERTED_OR_FLAT if (gs10_value - tb3_value) <= 0.0 else POSITIVE_SPREAD


# --------------------------------------------------------------------------------------
# Member forecasts (cached)
# --------------------------------------------------------------------------------------
def _cache_key(member_id: str, target: str, horizon: int, origin, series_id: str | None,
               mode: str, data_root) -> tuple:
    origin_iso = as_date(origin).isoformat()
    return (member_id, target, int(horizon), origin_iso, series_id, mode, None if data_root is None else str(data_root))


def internal_member_forecast(member_id: str, target: str, origin, horizon: int,
                             series_id: str | None = None, mode: str = "auto",
                             data_root=None) -> dict | None:
    """One R3 baseline forecast at one origin, cached; ``None`` when the model cannot run."""
    key = _cache_key(member_id, target, horizon, origin, series_id, mode, data_root)
    if key in _MEMBER_FORECAST_CACHE:
        return _MEMBER_FORECAST_CACHE[key]
    try:
        result = forecast(member_id, target, origin, horizon, series_id=series_id,
                          mode=mode, data_root=data_root).to_dict()
    except (ForecastError, ForecastDataError, ValueError):
        result = None
    if len(_MEMBER_FORECAST_CACHE) > _MEMBER_CACHE_LIMIT:
        _MEMBER_FORECAST_CACHE.clear()
    _MEMBER_FORECAST_CACHE[key] = result
    return result


def external_error_quantiles(target: str, origin, horizon: int, *, data_root=None,
                             as_of=None, lookback_quarters: int = 12,
                             min_observations: int = 5) -> dict:
    """Empirical SPF error distribution over past survey deadlines resolved by ``as_of``.

    The SPF is point-only; the ensemble gives it a distribution centred on the current SPF point
    using its own historical forecast errors.  That distribution is a **derived statistic** of the
    project (``DERIVED_STATISTIC``), not a third-party product; the raw SPF values never leave the
    git-ignored data directory.
    """
    origin_d = month_end_of(origin)
    as_of_d = month_end_of(as_of) if as_of is not None else origin_d
    survey_quarter = survey_quarter_for_origin(origin_d)
    result = {"available": False, "quantiles": None, "n_errors": 0,
              "distribution_kind": KIND_DERIVED_STATISTIC, "reason": None,
              "first_survey_quarter": None, "last_survey_quarter": None}
    if survey_quarter is None or target != "T2_GDP_GROWTH":
        result["reason"] = "NO_CURRENT_SPF_SURVEY"
        return result
    from .sources import survey_middle_month, survey_quarter_label

    errors: list[float] = []
    survey_labels: list[str] = []
    for back in range(1, max(1, int(lookback_quarters)) + 1):
        quarter = survey_quarter - back
        middle = survey_middle_month(quarter)
        middle_date = month_end_of(date(middle // 12, middle % 12 + 1, 1))
        if middle_date >= as_of_d:
            continue
        past = spf_forecast(target, middle_date, horizon, data_root=data_root)
        if not past.available or past.value is None:
            continue
        record = resolved_actual(target, middle_date, int(horizon), data_root=data_root)
        if not actual_known_by(target, middle_date, int(horizon), as_of_d, record):
            continue
        errors.append(float(record["value"]) - float(past.value))
        survey_labels.append(survey_quarter_label(quarter))
    if len(errors) >= int(min_observations):
        error_array = np.asarray(errors, dtype=float)
        quantiles = np.quantile(error_array, np.asarray(QUANTILE_LEVELS, dtype=float), method="linear")
        result.update(
            {
                "available": True,
                "quantiles": [float(value) for value in quantiles],
                "n_errors": len(errors),
                "first_survey_quarter": survey_labels[-1] if survey_labels else None,
                "last_survey_quarter": survey_labels[0] if survey_labels else None,
                "mean_error": float(np.mean(error_array)),
            }
        )
    else:
        result["reason"] = f"TOO_FEW_RESOLVED_SPF_ERRORS({len(errors)})"
    return result


def external_member_forecast(member_id: str, target: str, origin, horizon: int, *,
                             data_root=None, as_of=None, with_derived_distribution: bool = True) -> dict | None:
    """A current external-forecast member with its provenance and (derived) distribution."""
    if member_id != "SPF_GDP_MEDIAN":
        raise EnsembleError(f"unknown external member {member_id!r}")
    external = spf_forecast(target, origin, horizon, data_root=data_root)
    if not external.available or external.value is None:
        return None
    point = float(external.value)
    part = {
        "member_id": member_id,
        "kind": KIND_EXTERNAL_FORECAST,
        "provider": external.provider,
        "point": point,
        "quantiles": None,
        "probability": None,
        "data_class": "REAL_TIME",
        "attribution": external.attribution,
        "values_location": external.values_location,
        "external_forecast": external.to_dict(),
        "distribution_kind": None,
        "distribution": None,
    }
    if with_derived_distribution:
        derived = external_error_quantiles(target, origin, horizon, data_root=data_root, as_of=as_of)
        if derived["available"]:
            part["quantiles"] = [point + float(error_quantile) for error_quantile in derived["quantiles"]]
            part["distribution_kind"] = KIND_DERIVED_STATISTIC
            part["distribution"] = {
                "kind": KIND_DERIVED_STATISTIC,
                "derived_from": member_id,
                "method": "SPF historical forecast errors, empirical 19 quantiles, centred on the current SPF median",
                "n_errors": derived["n_errors"],
                "first_survey_quarter": derived["first_survey_quarter"],
                "last_survey_quarter": derived["last_survey_quarter"],
                "note": "distribution is the project's derived statistic; the SPF is point-only",
            }
    return part


# --------------------------------------------------------------------------------------
# History construction
# --------------------------------------------------------------------------------------
def candidate_origins(target: str, current_origin, horizon: int, max_scan: int) -> list[date]:
    """Past origins to scan for calibration history, newest first."""
    current = month_end_of(current_origin)
    step = 3 if (target == "T2_GDP_GROWTH" and int(horizon) > 0) else 1
    return [add_months(current, -step * offset) for offset in range(1, int(max_scan) + 1)]


def default_max_scan(target: str, horizon: int, lookback: int) -> int:
    if target == "T3_RECESSION":
        # the event interval must be resolved: horizon + resolution lag months older than the origin
        return int(max(lookback * 2, lookback + int(horizon) + 24))
    if target == "T2_GDP_GROWTH" and int(horizon) > 0:
        return int(lookback + 8)
    return int(lookback + 24)


def history_rows(
    target: str,
    current_origin,
    horizon: int,
    member_ids,
    *,
    series_id: str | None = None,
    mode: str = "auto",
    data_root=None,
    lookback: int = 12,
    max_scan: int | None = None,
    need_actual: bool = True,
) -> list[dict]:
    """Calibration history, oldest first; only resolved targets known at the current origin."""
    current = month_end_of(current_origin)
    member_ids = list(member_ids)
    if not member_ids:
        raise EnsembleError("history needs at least one member")
    scan = default_max_scan(target, horizon, lookback) if max_scan is None else int(max_scan)
    rows: list[dict] = []
    for history_origin in candidate_origins(target, current, horizon, scan):
        record = resolved_actual(target, history_origin, horizon, series_id=series_id, data_root=data_root)
        if need_actual:
            if not actual_known_by(target, history_origin, horizon, current, record):
                continue
        elif record is None:
            continue
        parts: dict[str, dict] = {}
        for member_id in member_ids:
            if member_id == "SPF_GDP_MEDIAN":
                part = external_member_forecast(member_id, target, history_origin, horizon,
                                                data_root=data_root, as_of=current)
            else:
                part = internal_member_forecast(member_id, target, history_origin, horizon,
                                                series_id=series_id, mode=mode, data_root=data_root)
            if part is not None:
                parts[member_id] = part
        if not parts:
            continue
        rows.append(
            {
                "origin": history_origin.isoformat(),
                "target": target,
                "horizon": int(horizon),
                "actual": float(record["value"]),
                "actual_source": record.get("source"),
                "actual_vintage": record.get("vintage"),
                "regime": rate_regime(history_origin, data_root=data_root),
                "parts": parts,
            }
        )
        if len(rows) >= int(lookback):
            break
    rows.reverse()
    return rows


def history_errors(rows, target: str) -> tuple[dict[str, np.ndarray], np.ndarray, list[str]]:
    """Aligned calibration errors, actuals and regime labels from ``history_rows`` output."""
    if not rows:
        return {}, np.asarray([], dtype=float), []
    member_ids: list[str] = []
    for row in rows:
        for member_id in row["parts"]:
            if member_id not in member_ids:
                member_ids.append(member_id)
    errors = {member_id: np.full(len(rows), np.nan) for member_id in member_ids}
    actuals = np.asarray([float(row["actual"]) for row in rows], dtype=float)
    regimes = [str(row.get("regime", UNKNOWN_REGIME)) for row in rows]
    for index, row in enumerate(rows):
        actual = float(row["actual"])
        for member_id, part in row["parts"].items():
            if target == "T3_RECESSION":
                probability = part.get("probability")
                if probability is None:
                    continue
                errors[member_id][index] = float(probability) - actual
            else:
                point = part.get("point")
                if point is None:
                    continue
                errors[member_id][index] = float(point) - actual
    return errors, actuals, regimes


def current_member_parts(
    target: str,
    origin,
    horizon: int,
    member_ids,
    *,
    series_id: str | None = None,
    mode: str = "auto",
    data_root=None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Member forecasts at the current origin plus the exclusion reasons of the missing ones."""
    parts: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for member_id in member_ids:
        if member_id == "SPF_GDP_MEDIAN":
            part = external_member_forecast(member_id, target, origin, horizon,
                                            data_root=data_root, as_of=month_end_of(origin))
            if part is None:
                external = spf_forecast(target, origin, horizon, data_root=data_root)
                excluded[member_id] = external.reason or "NOT_AVAILABLE"
            else:
                parts[member_id] = part
            continue
        part = internal_member_forecast(member_id, target, origin, horizon, series_id=series_id,
                                        mode=mode, data_root=data_root)
        if part is None:
            excluded[member_id] = "MODEL_CANNOT_RUN_AT_ORIGIN"
        else:
            parts[member_id] = part
    return parts, excluded


def default_member_ids(target: str, horizon: int, *, include_external: bool = True) -> list[str]:
    """Internal R3 models applicable to the target plus the external SPF member for T2."""
    ids = list(applicable_models(target, horizon))
    if include_external and target == "T2_GDP_GROWTH" and int(horizon) in (0, 1, 2, 4):
        ids.append("SPF_GDP_MEDIAN")
    return ids
