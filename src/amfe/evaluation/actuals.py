"""First-publication and 12-month-vintage actuals from the ALFRED vintage archive.

Protocol v4 actuals.rule: the primary actual is the first publication of the target, the secondary
actual is the value in the vintage available 12 months after the reference period.  For T3 the
chronology as of the evaluation date is used for both, so the secondary equals the primary.

The vintage grid is whatever the automated retrieval stored in ``data/raw/<SERIES>/vintages.zip``
(quarterly vintages after the dense-archive step).  "First publication" here means the first
archived vintage that contains the observation; with a quarterly vintage grid this is the first
recorded release at or after the publication, which is a documented approximation of the exact
release date.
"""
from __future__ import annotations

import json
from pathlib import Path

from amfe.forecast.data import (
    SeriesUnavailable,
    add_months,
    as_date,
    cpi_yoy,
    gdp_growth,
    load_vintages,
    month_end_of,
    month_start_date,
    quarter_end_month,
    target_actual,
    target_period,
)

ROOT = Path(__file__).resolve().parents[3]


def period_end_date(freq: str, period: int):
    """Calendar end date of a monthly or quarterly target period."""
    if freq == "M":
        return month_end_of(month_start_date(int(period)))
    if freq == "Q":
        return month_end_of(month_start_date(quarter_end_month(int(period))))
    raise ValueError(freq)


def first_publication(name: str, period: int, data_root=None) -> dict | None:
    """First archived vintage containing ``period`` and its value."""
    try:
        vintages = load_vintages(name, data_root)
    except SeriesUnavailable:
        return None
    for vintage_date in sorted(vintages):
        value = vintages[vintage_date].value_at(int(period))
        if value is not None:
            return {"value": float(value), "vintage_date": vintage_date.isoformat()}
    return None


def vintage_value(name: str, period: int, as_of, data_root=None) -> dict | None:
    """Value of ``period`` in the latest vintage available on or before ``as_of``."""
    try:
        vintages = load_vintages(name, data_root)
    except SeriesUnavailable:
        return None
    cutoff = as_date(as_of)
    usable = [d for d in vintages if d <= cutoff]
    if not usable:
        return None
    vintage_date = max(usable)
    value = vintages[vintage_date].value_at(int(period))
    if value is None:
        return None
    return {"value": float(value), "vintage_date": vintage_date.isoformat()}


def _target_from_series(series, target: str, period: int) -> float | None:
    """Protocol target value computed from one vintage of the source series.

    T1: 100*(CPI_t/CPI_{t-12}-1) (monthly y/y).  T2: 400*ln(GDP_q/GDP_{q-1}) (q/q annualised).
    """
    if target == "T1_CPI_YOY":
        transformed = cpi_yoy(series)
    elif target == "T2_GDP_GROWTH":
        transformed = gdp_growth(series)
    else:
        return None
    return transformed.value_at(int(period))


def _first_publication_target(name: str, target: str, period: int, data_root=None) -> dict | None:
    """First vintage containing ``period``, then the protocol target from that same vintage."""
    try:
        vintages = load_vintages(name, data_root)
    except SeriesUnavailable:
        return None
    for vintage_date in sorted(vintages):
        series = vintages[vintage_date]
        if series.value_at(int(period)) is None:
            continue
        value = _target_from_series(series, target, int(period))
        if value is None:
            continue
        return {"value": float(value), "vintage_date": vintage_date.isoformat()}
    return None


def _vintage_target(name: str, target: str, period: int, as_of, data_root=None) -> dict | None:
    """Protocol target from the latest vintage available on or before ``as_of``."""
    try:
        vintages = load_vintages(name, data_root)
    except SeriesUnavailable:
        return None
    cutoff = as_date(as_of)
    usable = [d for d in vintages if d <= cutoff]
    if not usable:
        return None
    vintage_date = max(usable)
    series = vintages[vintage_date]
    if series.value_at(int(period)) is None:
        return None
    value = _target_from_series(series, target, int(period))
    if value is None:
        return None
    return {"value": float(value), "vintage_date": vintage_date.isoformat()}


def actual_pair(target: str, origin, horizon: int, series_id: str | None = None,
                evaluation_date=None, data_root=None) -> dict:
    """Primary (first publication) and secondary (vintage 12 months later) actuals."""
    if target == "T3_RECESSION":
        value = target_actual(target, origin, horizon, series_id=series_id, data_root=data_root)
        return {"target": target, "origin": str(origin), "horizon": int(horizon),
                "primary": None if value is None else float(value),
                "secondary": None if value is None else float(value),
                "primary_vintage": "NBER chronology as of the evaluation date",
                "secondary_vintage": "same as primary for T3 (protocol actuals.rule)",
                "unit": "event indicator"}
    freq, period = target_period(target, origin, horizon)
    name = series_id or ("CPIAUCSL" if target == "T1_CPI_YOY" else "GDPC1")
    # the protocol actuals are the target values (yoy inflation / annualised growth), not the raw
    # index level: the first-publication value must be transformed with the same vintage
    first = _first_publication_target(name, target, period, data_root=data_root)
    end = period_end_date(freq, period)
    secondary = _vintage_target(name, target, period, add_months(end, 12), data_root=data_root)
    return {
        "target": target, "origin": str(origin), "horizon": int(horizon),
        "period": int(period), "period_end": end.isoformat(), "series": name,
        "primary": None if first is None else first["value"],
        "secondary": None if secondary is None else secondary["value"],
        "primary_vintage": None if first is None else first["vintage_date"],
        "secondary_vintage": None if secondary is None else secondary["vintage_date"],
        "unit": "percent" if target == "T2_GDP_GROWTH" else "percent y/y",
    }


def coverage_report(target: str, origins, horizon: int, series_id: str | None = None,
                    data_root=None) -> dict:
    rows = [actual_pair(target, o, horizon, series_id=series_id, data_root=data_root) for o in origins]
    primary = sum(1 for r in rows if r["primary"] is not None)
    secondary = sum(1 for r in rows if r["secondary"] is not None)
    return {"target": target, "horizon": horizon, "origins": len(rows),
            "primary_available": primary, "secondary_available": secondary,
            "first": next((r["primary_vintage"] for r in rows if r["primary_vintage"]), None)}
