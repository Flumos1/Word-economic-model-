"""Deterministic commodity-market balance engine for AMFE 2.0 (docs/147).

The engine closes one identity per month::

    demand(price) + commercial_stock_draw(price) = supply_available

where ``supply_available`` is the baseline supply minus a shock loss plus the bypass/spare/strategic
release flows; the price is found by bisection on a monotone residual, and the strategic stock is a
declared release plan (fail-closed if it exceeds the stock).  The effective demand elasticity is
ramped from ``elasticity_short`` to ``elasticity_long`` because non-price rationing, fuel switching
and inventory adjustments build up over time.

Conventions
-----------
* quantities: millions of barrels per day (``mb/d``); stocks: millions of barrels (``mb``);
  prices: ``USD/bbl``; one step = one month (30.4375 days is **not** applied: monthly flows and
  stock draws are in the same mb/d-month convention, documented and consistent).
* every check is explicit; ``MarketPath.ok`` is true only when the mass balance closes, stocks stay
  non-negative, prices stay positive and the elasticity ramp is monotone.
* no randomness, no timestamps, no I/O: the same inputs give the same path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .contract import BalanceError, MarketError, UnitError, RESEARCH_ONLY

QUANTITY_UNIT = "mb/d"
STOCK_UNIT = "mb"
PRICE_UNIT = "USD/bbl"
DEFAULT_TOLERANCE = 1e-9
_BISECTION_ITERATIONS = 200


@dataclass(frozen=True)
class MarketCalibration:
    """The calibrated market state and elasticities for one commodity."""

    commodity: str
    base_price: float
    base_demand: float
    baseline_supply: float
    elasticity_short: float
    elasticity_long: float
    elasticity_ramp_months: int = 6
    risk_premium: float = 0.0
    commercial_stock: float = 0.0
    commercial_max_draw: float = 0.0
    strategic_stock: float = 0.0
    strategic_max_draw: float = 0.0
    price_floor_fraction: float = 0.25
    price_ceiling_multiple: float = 15.0
    quantity_unit: str = QUANTITY_UNIT
    stock_unit: str = STOCK_UNIT
    price_unit: str = PRICE_UNIT
    days_per_month: float = 30.4375
    tolerance: float = DEFAULT_TOLERANCE

    def __post_init__(self) -> None:
        for name in ("base_price", "base_demand", "baseline_supply"):
            if float(getattr(self, name)) <= 0.0:
                raise MarketError(f"{name} must be positive, got {getattr(self, name)!r}")
        for name in ("elasticity_short", "elasticity_long"):
            value = float(getattr(self, name))
            if not -1.0 < value < 0.0:
                raise MarketError(f"{name} must be in (-1, 0), got {value!r} (negative = demand "
                                  "falls when price rises)")
        # elasticities are negative: the short-run magnitude must not exceed the long-run one
        if abs(float(self.elasticity_short)) > abs(float(self.elasticity_long)):
            raise MarketError("elasticity_short must be at most elasticity_long in magnitude "
                              "(the short run is less elastic, e.g. -0.25 vs -0.55)")
        for name in ("commercial_stock", "commercial_max_draw", "strategic_stock",
                     "strategic_max_draw"):
            if float(getattr(self, name)) < 0.0:
                raise MarketError(f"{name} must be non-negative, got {getattr(self, name)!r}")
        if float(self.risk_premium) < 0.0:
            raise MarketError("risk_premium must be non-negative")
        if int(self.elasticity_ramp_months) <= 0:
            raise MarketError("elasticity_ramp_months must be positive")
        if not 0.0 < float(self.price_floor_fraction) < 1.0:
            raise MarketError("price_floor_fraction must be in (0, 1)")
        if float(self.price_ceiling_multiple) <= 1.0:
            raise MarketError("price_ceiling_multiple must be > 1")
        if self.quantity_unit != QUANTITY_UNIT or self.stock_unit != STOCK_UNIT:
            raise UnitError(f"engine units are {QUANTITY_UNIT} and {STOCK_UNIT}, got "
                            f"{self.quantity_unit!r}/{self.stock_unit!r}")
        if not str(self.price_unit).strip():
            raise UnitError("price_unit must be non-empty")
        if float(self.tolerance) <= 0.0:
            raise MarketError("tolerance must be positive")
        if float(self.days_per_month) <= 0.0:
            raise MarketError("days_per_month must be positive")
        object.__setattr__(self, "base_price", float(self.base_price))
        object.__setattr__(self, "base_demand", float(self.base_demand))
        object.__setattr__(self, "baseline_supply", float(self.baseline_supply))
        object.__setattr__(self, "elasticity_short", float(self.elasticity_short))
        object.__setattr__(self, "elasticity_long", float(self.elasticity_long))
        object.__setattr__(self, "risk_premium", float(self.risk_premium))
        object.__setattr__(self, "commercial_stock", float(self.commercial_stock))
        object.__setattr__(self, "commercial_max_draw", float(self.commercial_max_draw))
        object.__setattr__(self, "strategic_stock", float(self.strategic_stock))
        object.__setattr__(self, "strategic_max_draw", float(self.strategic_max_draw))
        object.__setattr__(self, "tolerance", float(self.tolerance))
        object.__setattr__(self, "days_per_month", float(self.days_per_month))

    def elasticity_for_month(self, month: int) -> float:
        """Magnitude of the demand elasticity in month ``month`` (0-based, ramped)."""
        if int(month) < 0:
            raise MarketError("month index must be non-negative")
        short = abs(self.elasticity_short)
        long = abs(self.elasticity_long)
        steps = max(1, int(self.elasticity_ramp_months) - 1)
        fraction = min(1.0, float(month) / float(steps))
        return short + (long - short) * fraction


@dataclass(frozen=True)
class Shock:
    """Monthly shock flows for the engine (length = horizon in months).

    ``demand_shift`` is an optional exogenous demand shift in mb/d (negative = demand collapse,
    positive = demand boom) applied to the base demand before the price elasticity; it is how the
    demand-driven episodes (e.g. 2020) are represented without changing the supply identity.
    """

    lost_supply: tuple[float, ...]
    added_supply: tuple[float, ...] = ()
    strategic_release: tuple[float, ...] = ()
    demand_shift: tuple[float, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        lost = tuple(float(v) for v in self.lost_supply)
        if not lost:
            raise MarketError("lost_supply must not be empty")
        months = len(lost)
        added = tuple(float(v) for v in self.added_supply) or tuple(0.0 for _ in range(months))
        release = tuple(float(v) for v in self.strategic_release) or tuple(0.0 for _ in range(months))
        shift = tuple(float(v) for v in self.demand_shift) or tuple(0.0 for _ in range(months))
        if not (len(added) == len(release) == len(shift) == months):
            raise MarketError(f"shock vectors must all have {months} entries; got "
                              f"lost={months}, added={len(added)}, release={len(release)}, "
                              f"demand_shift={len(shift)}")
        for name, vector in (("lost_supply", lost), ("added_supply", added),
                             ("strategic_release", release)):
            if any(value < 0.0 for value in vector):
                raise MarketError(f"{name} must be non-negative")
        if any(not math.isfinite(value) for value in shift):
            raise MarketError("demand_shift must be finite")
        object.__setattr__(self, "lost_supply", lost)
        object.__setattr__(self, "added_supply", added)
        object.__setattr__(self, "strategic_release", release)
        object.__setattr__(self, "demand_shift", shift)
        object.__setattr__(self, "label", str(self.label))

    @property
    def months(self) -> int:
        return len(self.lost_supply)


def ramp(total: float, months: int, ramp_months: int) -> tuple[float, ...]:
    """A linear ramp from 0 to ``total`` over ``ramp_months``, then flat ``total``."""
    total = float(total)
    if total < 0.0:
        raise MarketError("ramp total must be non-negative")
    if months <= 0 or ramp_months <= 0:
        raise MarketError("months and ramp_months must be positive")
    cap = min(int(ramp_months), int(months))
    values = []
    for month in range(int(months)):
        weight = 1.0 if month >= cap - 1 else float(month + 1) / float(cap)
        values.append(round(total * min(1.0, weight), 12))
    return tuple(values)


def flat(total: float, months: int) -> tuple[float, ...]:
    if float(total) < 0.0:
        raise MarketError("flat total must be non-negative")
    if months <= 0:
        raise MarketError("months must be positive")
    return tuple(float(total) for _ in range(int(months)))


@dataclass(frozen=True)
class MarketPath:
    """The monthly path produced by :func:`simulate` (immutable, aggregate-only)."""

    commodity: str
    price_unit: str
    quantity_unit: str
    months: tuple[int, ...]
    price: tuple[float, ...]
    demand: tuple[float, ...]
    supply_available: tuple[float, ...]
    strategic_release: tuple[float, ...]
    commercial_draw: tuple[float, ...]
    shortage: tuple[float, ...]
    surplus: tuple[float, ...]
    commercial_stock_end: tuple[float, ...]
    strategic_stock_end: tuple[float, ...]
    checks: dict = field(default_factory=dict)
    status: str = RESEARCH_ONLY
    label: str = ""
    days_per_month: float = 30.4375

    @property
    def ok(self) -> bool:
        return all(bool(value) for value in self.checks.values())

    def rows(self) -> list[dict]:
        return [
            {"month": self.months[index], "price": self.price[index],
             "demand": self.demand[index], "supply_available": self.supply_available[index],
             "strategic_release": self.strategic_release[index],
             "commercial_draw": self.commercial_draw[index],
             "shortage": self.shortage[index], "surplus": self.surplus[index],
             "commercial_stock_end": self.commercial_stock_end[index],
             "strategic_stock_end": self.strategic_stock_end[index]}
            for index in range(len(self.months))
        ]

    def summary(self) -> dict:
        peak = max(self.price)
        trough = min(self.price)
        total_shortage = math.fsum(self.shortage)
        return {
            "months": len(self.months),
            "price_peak": round(peak, 6),
            "price_trough": round(trough, 6),
            "price_mean": round(math.fsum(self.price) / len(self.price), 6),
            "price_peak_month": list(self.months)[self.price.index(peak)],
            "shortage_mb_per_day_total": round(total_shortage, 6),
            "consumed_commercial_stock": round(
                self.commercial_stock_end[0] - self.commercial_stock_end[-1], 6)
            if self.commercial_stock_end else 0.0,
            "consumed_strategic_stock": round(
                math.fsum(self.strategic_release) * self.days_per_month, 6),
            "units": self.price_unit,
        }


def _clear_month(calibration: MarketCalibration, month: int, available: float,
                 commercial_stock: float, demand_shift: float = 0.0) -> dict:
    """Find the price that clears ``demand(price) = available + commercial draw``."""
    eps = calibration.elasticity_for_month(month)
    floor = calibration.base_price * calibration.price_floor_fraction
    ceiling = calibration.base_price * calibration.price_ceiling_multiple
    base_demand = calibration.base_demand + float(demand_shift)
    if base_demand <= 0.0:
        raise BalanceError(f"month {month}: base_demand + demand_shift={base_demand:.3f} "
                           f"is not positive")

    def demand_at(price: float) -> float:
        return base_demand * (price / calibration.base_price) ** (-eps)

    def draw_at(price: float) -> float:
        gap = demand_at(price) - available
        stock_cap_rate = commercial_stock / calibration.days_per_month
        return min(calibration.commercial_max_draw, stock_cap_rate, max(0.0, gap))

    def residual(price: float) -> float:
        return demand_at(price) + draw_at(price) - available

    if residual(ceiling) > 0.0:  # even the price ceiling cannot clear: explicit rationing
        price = ceiling
        demand = demand_at(price)
        draw = draw_at(price)
        return {"price": price, "demand": demand, "commercial_draw": draw,
                "shortage": demand + draw - available, "surplus": 0.0}
    if residual(floor) <= 0.0:  # supply is ample at the floor price
        price = floor
        demand = demand_at(price)
        draw = draw_at(price)
        return {"price": price, "demand": demand, "commercial_draw": draw, "shortage": 0.0,
                "surplus": available + draw - demand}
    low, high = floor, ceiling
    for _ in range(_BISECTION_ITERATIONS):
        middle = 0.5 * (low + high)
        if residual(middle) > 0.0:
            low = middle
        else:
            high = middle
    price = 0.5 * (low + high)
    demand = demand_at(price)
    draw = draw_at(price)
    return {"price": price, "demand": demand, "commercial_draw": draw, "shortage": 0.0,
            "surplus": max(0.0, available + draw - demand)}


def simulate(calibration: MarketCalibration, shock: Shock) -> MarketPath:
    """Run the deterministic monthly balance for one shock."""
    months = shock.months
    strategic_stock = float(calibration.strategic_stock)
    commercial_stock = float(calibration.commercial_stock)
    released_total_rate = math.fsum(shock.strategic_release)
    released_total = released_total_rate * calibration.days_per_month
    if released_total > calibration.strategic_stock + calibration.tolerance:
        raise BalanceError(f"strategic release plan {released_total} exceeds the strategic stock "
                           f"{calibration.strategic_stock}")
    if any(value > calibration.strategic_max_draw + calibration.tolerance
           for value in shock.strategic_release):
        raise BalanceError(f"a monthly strategic release exceeds strategic_max_draw "
                           f"{calibration.strategic_max_draw}")

    prices, demands, supplies, releases, draws = [], [], [], [], []
    shortages, surpluses, commercial_end, strategic_end = [], [], [], []
    for month in range(months):
        release = float(shock.strategic_release[month])
        available = (calibration.baseline_supply - shock.lost_supply[month]
                     + shock.added_supply[month] + release)
        if available <= 0.0:
            raise BalanceError(f"month {month}: supply_available={available:.3f} is not positive")
        cleared = _clear_month(calibration, month, available, commercial_stock,
                               shock.demand_shift[month])
        commercial_stock = max(
            0.0, commercial_stock - cleared["commercial_draw"] * calibration.days_per_month)
        strategic_stock = max(
            0.0, strategic_stock - release * calibration.days_per_month)
        prices.append(round(cleared["price"] + calibration.risk_premium, 6))
        demands.append(round(cleared["demand"], 6))
        supplies.append(round(available, 6))
        releases.append(release)
        draws.append(round(cleared["commercial_draw"], 6))
        shortages.append(round(cleared["shortage"], 6))
        surpluses.append(round(cleared["surplus"], 6))
        commercial_end.append(round(commercial_stock, 6))
        strategic_end.append(round(strategic_stock, 6))

    tolerance = calibration.tolerance
    mass_balance = all(
        abs(supplies[index] + draws[index] - demands[index]
            - shortages[index] + surpluses[index]) <= tolerance * max(1.0, supplies[index])
        for index in range(months))
    checks = {
        "mass_balance": mass_balance,
        "stocks_non_negative": all(value >= -tolerance for value in commercial_end + strategic_end),
        "price_positive": all(value > 0.0 for value in prices),
        "strategic_release_feasible": released_total <= calibration.strategic_stock + tolerance,
        "elasticity_ramp_monotone": all(
            calibration.elasticity_for_month(month)
            <= calibration.elasticity_for_month(month + 1) + tolerance
            for month in range(max(1, months - 1))),
        "no_spurious_shortage": all(value >= -tolerance for value in shortages),
    }
    return MarketPath(
        commodity=calibration.commodity, price_unit=calibration.price_unit,
        quantity_unit=calibration.quantity_unit, months=tuple(range(months)),
        price=tuple(prices), demand=tuple(demands), supply_available=tuple(supplies),
        strategic_release=tuple(releases), commercial_draw=tuple(draws),
        shortage=tuple(shortages), surplus=tuple(surpluses),
        commercial_stock_end=tuple(commercial_end), strategic_stock_end=tuple(strategic_end),
        checks=checks, label=shock.label, days_per_month=calibration.days_per_month)


def run_variants(calibration: MarketCalibration, shock: Shock,
                 variants: Sequence[tuple[str, Mapping]]) -> list[dict]:
    """Run the same shock with calibration overrides; returns peak/mean/shortage per variant."""
    base = simulate(calibration, shock)
    base_peak = max(base.price)
    results = []
    for name, overrides in variants:
        merged = {**calibration.__dict__, **dict(overrides)}
        variant = MarketCalibration(**merged)
        path = simulate(variant, shock)
        peak = max(path.price)
        results.append({
            "variant": str(name),
            "calibration_overrides": {key: value for key, value in dict(overrides).items()},
            "price_peak": round(peak, 6),
            "price_mean": round(math.fsum(path.price) / len(path.price), 6),
            "shortage_total": round(math.fsum(path.shortage), 6),
            "delta_vs_central": round(peak - base_peak, 6),
            "delta_pct_vs_central": round(100.0 * (peak - base_peak) / base_peak, 4),
            "checks": dict(path.checks),
        })
    results.sort(key=lambda item: (item["price_peak"], item["variant"]))
    return results


def price_range(central_path: MarketPath, variants: Sequence[Mapping],
                metric: str = "price_peak") -> dict:
    """A range (low / central / high) over the central path and the sensitivity variants."""
    if metric not in ("price_peak", "price_mean"):
        raise MarketError(f"unknown range metric {metric!r}")
    values = [float(item[metric]) for item in variants]
    central = central_path.summary()[metric]
    if not values:
        raise MarketError("price_range needs at least one variant")
    low, high = min(values + [central]), max(values + [central])
    return {
        "metric": metric,
        "low": round(low, 6),
        "central": round(float(central), 6),
        "high": round(high, 6),
        "spread_pct": round(100.0 * (high - low) / float(central), 3) if central else None,
        "units": central_path.price_unit,
        "note": "range over the declared sensitivity variants (not a confidence interval)",
    }
