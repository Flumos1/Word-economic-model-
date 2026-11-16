#!/usr/bin/env python3
"""Reduced-form demand response to a price shock (docs/159, G3).

This module is deliberately small and explicit: it converts a vector of **relative price
changes** into **relative demand changes** through declared elasticities,

    ΔQ_i / Q_i = ε_i(m) · Δp_i ,      ε_i(m) = ε_short + (ε_long − ε_short) · min(m / ramp, 1)

and nothing else.  It is a *reduced-form* accounting device, **not** a behavioural demand system:
no utility maximisation, no substitution between goods, no income effect, no supply response and no
expectation formation.  The result carries ``status = RESEARCH_ONLY``; it is not a forecast.

Every elasticity is an :class:`Assumption` with an explicit provenance class (``assumption``) and
a source hint, so the numbers can be replaced by estimated values without touching the code.

Usage
-----
    from amfe.markets import demand
    response = demand.demand_response(price_changes, base_quantities, groups,
                                      elasticities=demand.FINAL_DEMAND_ELASTICITIES,
                                      horizon_months=6, ramp_months=6)
    print(response["aggregate"]["relative_long"])
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

STATUS = "RESEARCH_ONLY"
SCHEMA = "amfe.markets.demand.v1"

#: Allowed elasticity interval: a reduced-form rule outside it is refused (fail-closed).
ELASTICITY_BOUNDS = (-2.0, 0.0)
#: A single shock cannot move aggregate demand by more than this relative amount.
MAX_RELATIVE_DEMAND_CHANGE = 0.5
#: Assumption provenance classes (contract-compatible, docs/144).
SOURCE_CLASSES = ("observed", "derived", "assumption")


class DemandError(ValueError):
    """Base error: the demand channel refuses to produce a number."""


class BoundsError(DemandError):
    """An elasticity, a quantity or a resulting demand falls outside the declared bounds."""


#: Default final-demand elasticities by cost group (short = month 1, long = month 6).
#: Values are public-order-of-magnitude assumptions, not estimates; sources: docs/159.
FINAL_DEMAND_ELASTICITIES: dict[str, dict[str, float]] = {
    "energy": {"short": -0.15, "long": -0.55},
    "transport": {"short": -0.10, "long": -0.40},
    "manufacturing": {"short": -0.05, "long": -0.20},
    "services": {"short": -0.03, "long": -0.12},
}
#: Intermediate-input responses are deliberately weaker than final demand (same reduced form).
INTERMEDIATE_DEMAND_ELASTICITIES: dict[str, dict[str, float]] = {
    "energy": {"short": -0.07, "long": -0.25},
    "transport": {"short": -0.05, "long": -0.20},
    "manufacturing": {"short": -0.03, "long": -0.10},
    "services": {"short": -0.02, "long": -0.06},
}

#: Industry -> cost group (same buckets as docs/152; duplicated so ``src`` does not import tools).
CLASSIFICATION_RULES: dict[str, dict] = {
    "figaro": {
        "energy": ("CPA_B", "CPA_C19", "CPA_D35"),
        "transport": ("CPA_H49", "CPA_H50", "CPA_H51", "CPA_H52", "CPA_H53"),
        "manufacturing_prefixes": ("CPA_C",),
        "manufacturing_exclude": ("CPA_C19",),
    },
    "oecd_2025": {
        "energy": ("B05", "B06", "B07", "B08", "B09", "C19", "D"),
        "transport": ("H49", "H50", "H51", "H52", "H53"),
        "manufacturing_prefixes": ("C",),
        "manufacturing_exclude": ("C19",),
    },
}

ASSUMPTION_NOTES = {
    "energy": "цена энергии -> спрос: краткосрочная эластичность около -0.15, долгосрочная -0.55",
    "transport": "транспорт: -0.10 / -0.40",
    "manufacturing": "промышленные товары: -0.05 / -0.20",
    "services": "услуги: -0.03 / -0.12",
    "intermediate": "промежуточный спрос: те же группы, но половина реакции конечного спроса",
}


def classify(classification: str, industry: str) -> str:
    """Return the cost group of an industry for a known classification (fail-closed)."""
    rules = CLASSIFICATION_RULES.get(str(classification))
    if rules is None:
        raise DemandError(f"unknown industry classification {classification!r}; "
                          f"known: {sorted(CLASSIFICATION_RULES)}")
    industry = str(industry)
    if industry in set(rules["energy"]):
        return "energy"
    if industry in set(rules["transport"]):
        return "transport"
    if industry.startswith(tuple(rules["manufacturing_prefixes"])) and \
            industry not in set(rules["manufacturing_exclude"]):
        return "manufacturing"
    return "services"


def validate_elasticities(elasticities: Mapping | None = None) -> dict:
    """Validate and normalise an elasticity table (short/long, bounds, finite)."""
    table = dict(elasticities or FINAL_DEMAND_ELASTICITIES)
    if not table:
        raise DemandError("elasticity table must not be empty")
    normalised: dict[str, dict[str, float]] = {}
    for group, values in table.items():
        if not isinstance(values, Mapping) or set(values) < {"short"}:
            raise DemandError(f"group {group!r}: expected a mapping with 'short' (and 'long')")
        short = float(values["short"])
        long = float(values.get("long", short))
        for label, value in (("short", short), ("long", long)):
            if not math.isfinite(value):
                raise BoundsError(f"{group}.{label}: elasticity is not finite")
            low, high = ELASTICITY_BOUNDS
            if not low <= value <= high:
                raise BoundsError(f"{group}.{label}: elasticity {value} outside "
                                  f"[{low}, {high}]")
        normalised[group] = {"short": short, "long": long}
    return normalised


def ramp_factor(month: float, horizon_months: int = 6, ramp_months: int | None = None) -> float:
    """Linear short->long ramp: 0 at month 0, 1 once ``ramp_months`` has elapsed."""
    horizon = int(horizon_months)
    if horizon <= 0:
        raise DemandError(f"horizon_months must be positive, got {horizon_months}")
    ramp = int(ramp_months if ramp_months is not None else horizon)
    if ramp <= 0:
        raise DemandError(f"ramp_months must be positive, got {ramp_months}")
    if month < 0:
        raise DemandError(f"month must be non-negative, got {month}")
    return min(float(month) / float(ramp), 1.0)


def elasticity_at(group: str, month: float, *, horizon_months: int = 6,
                  ramp_months: int | None = None,
                  elasticities: Mapping | None = None) -> float:
    """Elasticity of ``group`` at ``month`` (linear short/long ramp)."""
    table = validate_elasticities(elasticities)
    if group not in table:
        raise DemandError(f"unknown demand group {group!r}; known: {sorted(table)}")
    values = table[group]
    factor = ramp_factor(month, horizon_months, ramp_months)
    return values["short"] + (values["long"] - values["short"]) * factor


def elasticity_path(group: str, *, horizon_months: int = 6, ramp_months: int | None = None,
                    elasticities: Mapping | None = None) -> tuple:
    """Elasticity per month (months 1..horizon)."""
    horizon = int(horizon_months)
    if horizon <= 0:
        raise DemandError("horizon_months must be positive")
    return tuple(round(elasticity_at(group, month, horizon_months=horizon,
                                     ramp_months=ramp_months, elasticities=elasticities), 9)
                 for month in range(1, horizon + 1))


def demand_response(price_changes: Sequence[float], base_quantities: Sequence[float],
                    groups: Sequence[str], *, elasticities: Mapping | None = None,
                    horizon_months: int = 6, ramp_months: int | None = None,
                    max_relative_change: float = MAX_RELATIVE_DEMAND_CHANGE) -> dict:
    """Map relative price changes to relative demand changes (reduced form, deterministic).

    ``price_changes[i]`` is the relative change of the price of sector ``i`` (0.05 = +5%),
    ``base_quantities[i]`` its base-period demand in the same units as the caller uses.
    """
    price = [float(value) for value in price_changes]
    base = [float(value) for value in base_quantities]
    labels = [str(group) for group in groups]
    if not (len(price) == len(base) == len(labels)) or not price:
        raise DemandError(f"price_changes/base_quantities/groups lengths differ: "
                          f"{len(price)}/{len(base)}/{len(labels)}")
    table = validate_elasticities(elasticities)
    unknown = sorted(set(labels) - set(table))
    if unknown:
        raise DemandError(f"unknown demand groups: {unknown}")
    for index, value in enumerate(price):
        if not math.isfinite(value):
            raise DemandError(f"sector {index}: price change is not finite")
        if value <= -1.0:
            raise DemandError(f"sector {index}: relative price change {value} <= -100%")
        if value > 5.0:
            raise DemandError(f"sector {index}: relative price change {value} > 500%")
    for index, value in enumerate(base):
        if not math.isfinite(value):
            raise DemandError(f"sector {index}: base quantity is not finite")
        if value < 0.0:
            raise BoundsError(f"sector {index}: base quantity {value} is negative")
    horizon = int(horizon_months)
    if horizon <= 0:
        raise DemandError("horizon_months must be positive")
    if not 0.0 < float(max_relative_change) <= 1.0:
        raise DemandError("max_relative_change must be in (0, 1]")

    total_base = float(sum(base))
    by_sector: list[dict] = []
    for index, (p_change, quantity, group) in enumerate(zip(price, base, labels)):
        short = elasticity_at(group, 1, horizon_months=horizon, ramp_months=ramp_months,
                              elasticities=table)
        long = elasticity_at(group, horizon, horizon_months=horizon, ramp_months=ramp_months,
                             elasticities=table)
        delta_short = short * p_change * quantity
        delta_long = long * p_change * quantity
        by_sector.append({
            "index": index, "group": group, "price_change": round(p_change, 9),
            "base_quantity": round(quantity, 6),
            "elasticity_short": round(short, 9), "elasticity_long": round(long, 9),
            "delta_short": round(delta_short, 6), "delta_long": round(delta_long, 6),
            "relative_short": round(delta_short / quantity, 9) if quantity else 0.0,
            "relative_long": round(delta_long / quantity, 9) if quantity else 0.0,
        })

    monthly = []
    for month in range(1, horizon + 1):
        delta = 0.0
        for p_change, quantity, group in zip(price, base, labels):
            delta += elasticity_at(group, month, horizon_months=horizon,
                                   ramp_months=ramp_months, elasticities=table) * p_change * quantity
        monthly.append({"month": month, "delta_demand": round(delta, 6),
                        "relative": round(delta / total_base, 9) if total_base else 0.0})

    delta_short_total = float(sum(item["delta_short"] for item in by_sector))
    delta_long_total = float(sum(item["delta_long"] for item in by_sector))
    relative_short = delta_short_total / total_base if total_base else 0.0
    relative_long = delta_long_total / total_base if total_base else 0.0
    if abs(relative_short) > max_relative_change or abs(relative_long) > max_relative_change:
        raise BoundsError(f"aggregate demand change {relative_short:.4f}/{relative_long:.4f} "
                          f"exceeds the declared bound +/-{max_relative_change}")
    for item in by_sector:
        if item["base_quantity"] + item["delta_long"] < -1e-9 * max(1.0, item["base_quantity"]):
            raise BoundsError(f"sector {item['index']}: demand becomes negative after the shock")

    return {
        "schema": SCHEMA,
        "status": STATUS,
        "horizon_months": horizon,
        "ramp_months": int(ramp_months if ramp_months is not None else horizon),
        "elasticities": table,
        "aggregate": {
            "base_quantity": round(total_base, 6),
            "delta_short": round(delta_short_total, 6),
            "delta_long": round(delta_long_total, 6),
            "relative_short": round(relative_short, 9),
            "relative_long": round(relative_long, 9),
        },
        "monthly": monthly,
        "by_sector": by_sector,
        "checks": {
            "quantities_non_negative": all(item["base_quantity"] >= 0.0 for item in by_sector),
            "elasticities_within_bounds": all(
                ELASTICITY_BOUNDS[0] <= value <= ELASTICITY_BOUNDS[1]
                for values in table.values() for value in values.values()),
            "aggregate_within_bound": (abs(relative_short) <= max_relative_change
                                       and abs(relative_long) <= max_relative_change),
            "monotone_abs_growth": abs(monthly[-1]["delta_demand"]) >= abs(monthly[0]["delta_demand"]),
            "deterministic": True,
        },
        "assumptions": [
            {"name": "elasticity_short", "source_class": "assumption",
             "note": "краткосрочная эластичность спроса по цене; заменяема на оценённые значения",
             "source": "docs/159; порядок величины по литературе (не оценка AMFE)"},
            {"name": "elasticity_long", "source_class": "assumption",
             "note": "долгосрочная эластичность; линейный ramp от short к long",
             "source": "docs/159"},
            {"name": "linear_price_pass", "source_class": "assumption",
             "note": "ΔQ/Q = ε · Δp (линеаризация первого порядка; малые шоки)", "source": "docs/159"},
            {"name": "no_substitution", "source_class": "assumption",
             "note": "нет замещения между товарами, нет дохода/ожиданий/предложения",
             "source": "docs/159"},
        ],
        "limitations": [
            "reduced-form: не поведенческая модель, без полезности, замещения и дохода",
            "эластичности — допущения порядка величины, не оценки по данным",
            "линеаризация: при больших шоках ошибка растёт (мост ограничен 500%)",
            "результат — RESEARCH_ONLY, не прогноз и не инвестиционная рекомендация",
        ],
    }
