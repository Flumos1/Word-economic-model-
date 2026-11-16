"""Soybean price seasonality (docs/148).

Soybeans have two harvest cycles: South America (Brazil/Argentina) harvests around February-May and
the US harvests around September-November.  Harvest pressure lowers prices and the off-season
raises them modestly.  The factors below are *illustrative assumptions* (``source_class="assumption"``)
with an annual mean of exactly 1.0; they are applied only as a relative monthly profile on top of a
scenario range and never create a point forecast.
"""
from __future__ import annotations

import math
from typing import Tuple

from .contracts import AgriMarketError

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Relative monthly profile (mean = 1.0): harvest pressure Feb-May and Sep-Nov.
MONTHLY_FACTORS: Tuple[float, ...] = (
    1.015, 1.000, 0.985, 0.975, 0.985, 0.995,
    1.010, 1.020, 1.015, 1.005, 0.995, 1.000,
)
HARVEST_WINDOWS = {
    "south_america": ("Feb", "Mar", "Apr", "May"),
    "north_america": ("Sep", "Oct", "Nov"),
}


def _validated_factors() -> Tuple[float, ...]:
    if len(MONTHLY_FACTORS) != 12:
        raise AgriMarketError("seasonality profile must contain exactly 12 months")
    if not all(math.isfinite(value) and value > 0.0 for value in MONTHLY_FACTORS):
        raise AgriMarketError("seasonality factors must be finite and positive")
    mean = math.fsum(MONTHLY_FACTORS) / 12.0
    if abs(mean - 1.0) > 1e-9:
        raise AgriMarketError(f"seasonality factors must average to 1.0, got {mean:.12f}")
    return MONTHLY_FACTORS


def seasonal_factor(month: int) -> float:
    """Relative price factor for month 1..12 (assumption profile)."""
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise AgriMarketError("month must be an integer in 1..12")
    return float(_validated_factors()[month - 1])


def seasonal_path(base_price: float, *, start_month: int = 1) -> list:
    """Twelve monthly relative prices (``base_price * factor``) starting at ``start_month``."""
    base = float(base_price)
    if not math.isfinite(base) or base <= 0.0:
        raise AgriMarketError("base price must be finite and positive")
    if isinstance(start_month, bool) or not isinstance(start_month, int) \
            or not 1 <= start_month <= 12:
        raise AgriMarketError("start_month must be an integer in 1..12")
    factors = _validated_factors()
    return [round(base * factors[(start_month - 1 + offset) % 12], 6) for offset in range(12)]


def describe_seasonality() -> dict:
    factors = _validated_factors()
    return {"monthly_factors": [round(value, 6) for value in factors],
            "months": list(MONTHS),
            "annual_mean": round(math.fsum(factors) / 12.0, 12),
            "harvest_windows": {key: list(value) for key, value in HARVEST_WINDOWS.items()},
            "interpretation": ("relative monthly profile only; applied on top of a scenario price "
                               "range, not a standalone forecast"),
            "source_class": "assumption"}


__all__ = ["HARVEST_WINDOWS", "MONTHS", "MONTHLY_FACTORS", "describe_seasonality",
           "seasonal_factor", "seasonal_path"]
