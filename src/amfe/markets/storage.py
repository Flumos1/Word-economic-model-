"""Storage state and futures-curve layer for the oil market module (docs/177 M8-A, docs/180).

What this module is
-------------------
The oil module (docs/147, docs/165) clears one monthly balance and reports a price path, but it has
no storage layer: commercial stocks there are an exogenous draw limit, not a state, and there is no
futures curve at all.  That is exactly the gap named in docs/177 §3.1 and in the v3 limitations (the
2020 demand collapse is understated because there is no "tanks are full" mechanism).  This module
adds the missing layer and nothing else:

* **Stocks as state.**  Commercial and strategic (SPR) inventories in ``mb``, a monthly balance
  ``end = start + build - draw - loss`` with a physical ceiling (``capacity_mb``); demand that cannot
  be served is reported as ``unmet`` and never hidden in a negative stock.
* **Futures curve.**  ``F(T) = S * exp((r + c - y(cover)) * T / 12)`` with ``cover`` in days of
  consumption, ``y`` a decreasing convenience yield and ``c`` the storage cost.  The sign of
  ``r + c - y(cover)`` is not free: the module *derives* the normal cover
  ``cover_normal = cover_scale * ln(y_max / (r + c))`` at which the carry is exactly zero, so
  ``cover > cover_normal`` means contango (``F > S``) and ``cover < cover_normal`` means
  backwardation (``F < S``) by construction, not by fitting.
* **Supply shock / outage.**  :class:`SupplyShock` removes supply for a number of months, optionally
  adds bypass/replacement supply, and the stocks absorb the gap up to their draw limits.  The
  resulting stock path feeds the curve, so a disruption raises the convenience yield (backwardation)
  while a demand collapse fills the tanks and flips the curve to contango.

Reuse, not a new engine
-----------------------
The monthly identity is checked with :class:`amfe.resources.energy.ResourceBalance` (docs/38 I01/I04:
a stock change appears exactly once, on the correct side).  The futures curve is a closed-form
function of the state - no solver, no I/O inside the model, no randomness.  Route/routing networks
(M8-B, :mod:`amfe.resources.logistics`) are **not** part of this module.

Honest limits (also in docs/180)
--------------------------------
* Every parameter is an ``assumption`` (``source_class="assumption"``) with a ``replace_with`` hint;
  the only observed input is the EIA monthly Brent spot (public domain, hash-pinned committed CSV).
* The *level* of the spread is not calibrated: with a constant storage cost the model reproduces the
  **sign** and the monotonicity of the curve, not the 2020 magnitude (a convex marginal storage cost
  near full tanks is the next step).
* No negative prices: when tanks are full the unabsorbed supply is reported as ``surplus``
  (curtailed), which is a stated limitation, not a claim.
* Research prototype: ``RESEARCH_ONLY``, not a forecast, not an admission, not investment advice.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from ..resources.energy import ResourceBalance, ResourceBalanceError
from .contract import Assumption, MarketError, RESEARCH_ONLY
from .oil_v3 import load_series

MODULE_ID = "oil.storage_curve"
COMMODITY = "Brent crude oil (storage and futures-curve layer)"
UNITS = "USD/bbl"
SCHEMA_PARAMS = "amfe.markets.storage.params.v1"
SCHEMA_CURVE = "amfe.markets.storage.curve.v1"
SCHEMA_PATH = "amfe.markets.storage.path.v1"
SCHEMA_REPORT = "amfe.markets.storage.report.v1"
DAYS_PER_MONTH = 30.4375
TOLERANCE = 1e-9
REGIMES = ("contango", "backwardation", "flat")
DEFAULT_TENORS = (1, 3, 6, 12)
#: The evidence band of the cross-check with the oil module (docs/177 A5: +-10%).
CROSS_CHECK_BAND = 0.10


class StorageError(MarketError):
    """Fail-closed storage error (bad parameters, non-positive price, impossible stock state)."""


# ------------------------------------------------------------------------------------- helpers
def _finite(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise StorageError(f"{name} must be a finite number, got {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise StorageError(f"{name} must be non-negative, got {value!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise StorageError(f"{name} must be positive, got {value!r}")
    return number


def _q(value: float, digits: int = 9) -> float:
    """Deterministic output rounding (internal arithmetic stays unrounded)."""
    return round(float(value), digits)


def _sha256_json(payload: Mapping) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------ parameters
@dataclass(frozen=True)
class StorageParams:
    """Declared parameters of the storage + curve layer (all assumptions, all replaceable).

    ``r`` is the annual risk-free rate, ``c`` the annual storage cost per unit of value and ``y`` the
    annual convenience yield.  ``y`` and ``c`` are fractions per year, so the carry ``r + c - y`` is
    an annual log rate and ``T`` in the exponent is divided by 12.
    """

    risk_free_rate: float = 0.045
    storage_cost: float = 0.02
    convenience_max: float = 0.55
    cover_scale_days: float = 15.0
    capacity_mb: float = 4000.0
    commercial_max_draw_mb_per_day: float = 1.0
    commercial_max_build_mb_per_day: float = 4.0
    spr_max_draw_mb_per_day: float = 1.5
    loss_fraction_per_month: float = 0.001
    days_per_month: float = DAYS_PER_MONTH
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        r = _non_negative(self.risk_free_rate, "risk_free_rate")
        c = _non_negative(self.storage_cost, "storage_cost")
        y_max = _positive(self.convenience_max, "convenience_max")
        _positive(self.cover_scale_days, "cover_scale_days")
        _positive(self.capacity_mb, "capacity_mb")
        for name in ("commercial_max_draw_mb_per_day", "commercial_max_build_mb_per_day",
                     "spr_max_draw_mb_per_day"):
            _non_negative(getattr(self, name), name)
        loss = _finite(self.loss_fraction_per_month, "loss_fraction_per_month")
        if not 0.0 <= loss < 1.0:
            raise StorageError("loss_fraction_per_month must be in [0, 1)")
        _positive(self.days_per_month, "days_per_month")
        _positive(self.tolerance, "tolerance")
        if y_max <= r + c:
            raise StorageError(
                f"convenience_max ({y_max}) must exceed r + c ({r + c}): otherwise the convenience "
                "yield can never fall below the cost of carry, no normal cover exists and the curve "
                "is contango at every stock level (fail-closed instead of a meaningless model)")
        object.__setattr__(self, "risk_free_rate", r)
        object.__setattr__(self, "storage_cost", c)
        object.__setattr__(self, "convenience_max", y_max)
        object.__setattr__(self, "cover_scale_days", float(self.cover_scale_days))
        object.__setattr__(self, "capacity_mb", float(self.capacity_mb))
        object.__setattr__(self, "commercial_max_draw_mb_per_day",
                           float(self.commercial_max_draw_mb_per_day))
        object.__setattr__(self, "commercial_max_build_mb_per_day",
                           float(self.commercial_max_build_mb_per_day))
        object.__setattr__(self, "spr_max_draw_mb_per_day", float(self.spr_max_draw_mb_per_day))
        object.__setattr__(self, "loss_fraction_per_month", loss)
        object.__setattr__(self, "days_per_month", float(self.days_per_month))
        object.__setattr__(self, "tolerance", float(self.tolerance))

    # -- the curve ---------------------------------------------------------------------
    @property
    def cost_of_carry(self) -> float:
        """``r + c``: the annual cost of holding one unit, before convenience."""
        return self.risk_free_rate + self.storage_cost

    @property
    def normal_cover_days(self) -> float:
        """Cover at which ``y(cover) = r + c`` (zero carry); derived, not a free parameter."""
        return self.cover_scale_days * math.log(self.convenience_max / self.cost_of_carry)

    def convenience_yield(self, cover_days: float) -> float:
        """``y(cover) = y_max * exp(-cover / cover_scale)``, annual, strictly decreasing."""
        cover = _non_negative(cover_days, "cover_days")
        return self.convenience_max * math.exp(-cover / self.cover_scale_days)

    def carry(self, cover_days: float) -> float:
        """Annual log basis ``r + c - y(cover)``; positive = contango, negative = backwardation."""
        return self.cost_of_carry - self.convenience_yield(cover_days)

    def basis(self, cover_days: float, months: float) -> float:
        """``exp(carry * T / 12)``: the multiplier that maps spot to the ``T``-month futures price."""
        horizon = _non_negative(months, "months")
        return math.exp(self.carry(cover_days) * horizon / 12.0)

    def forward_price(self, spot: float, cover_days: float, months: float = 1.0) -> float:
        price = _positive(spot, "spot")
        return price * self.basis(cover_days, months)

    def curve(self, spot: float, cover_days: float,
              tenors: Sequence[float] = DEFAULT_TENORS) -> tuple[float, ...]:
        price = _positive(spot, "spot")
        tenors = tuple(tenors)
        if len(tenors) < 2:
            raise StorageError("curve needs at least two tenors")
        for tenor in tenors:
            _positive(tenor, "tenor")
        return tuple(price * self.basis(cover_days, tenor) for tenor in tenors)

    def implied_forward_rate(self, spot: float, cover_days: float, first: float,
                             second: float) -> float:
        """Annualised forward rate implied by two futures prices (should equal ``carry``)."""
        t1 = _positive(first, "first tenor")
        t2 = _positive(second, "second tenor")
        if t2 <= t1:
            raise StorageError("the second tenor must be longer than the first")
        f1 = self.forward_price(spot, cover_days, t1)
        f2 = self.forward_price(spot, cover_days, t2)
        return 12.0 * math.log(f2 / f1) / (t2 - t1)

    def regime(self, cover_days: float, band: float = 1e-12) -> str:
        """``contango`` / ``backwardation`` / ``flat`` from the sign of the annual carry.

        The tiny default band absorbs floating-point noise exactly at the normal cover, where the
        carry is algebraically zero; it is a numerical tolerance, not an economic dead zone.
        """
        limit = _non_negative(band, "band")
        value = self.carry(cover_days)
        if value > limit:
            return "contango"
        if value < -limit:
            return "backwardation"
        return "flat"

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA_PARAMS,
            "risk_free_rate": _q(self.risk_free_rate),
            "storage_cost": _q(self.storage_cost),
            "convenience_max": _q(self.convenience_max),
            "cover_scale_days": _q(self.cover_scale_days),
            "normal_cover_days": _q(self.normal_cover_days),
            "cost_of_carry": _q(self.cost_of_carry),
            "capacity_mb": _q(self.capacity_mb),
            "commercial_max_draw_mb_per_day": _q(self.commercial_max_draw_mb_per_day),
            "commercial_max_build_mb_per_day": _q(self.commercial_max_build_mb_per_day),
            "spr_max_draw_mb_per_day": _q(self.spr_max_draw_mb_per_day),
            "loss_fraction_per_month": _q(self.loss_fraction_per_month, 12),
            "days_per_month": _q(self.days_per_month),
            "tolerance": _q(self.tolerance, 12),
            "units": {"rate": "1/year", "cover": "days", "stocks": "mb", "flows": "mb/d",
                      "price": UNITS},
        }

    def digest(self) -> str:
        return _sha256_json(self.as_dict())


DEFAULT_PARAMS = StorageParams()

#: Which public source should replace each assumption (mirrors the oil module's ``replace_with``).
ASSUMPTION_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("risk_free_rate", "1/year",
     "annual USD risk-free rate for the carry; replace with a dated 3-month bill quote",
     "FRED TB3MS / ICE Term SOFR"),
    ("storage_cost", "1/year",
     "physical storage + insurance + tank lease cost per unit of value per year; the level drives "
     "the contango magnitude, not its sign",
     "IEA/EIA storage cost surveys; tank lease rates"),
    ("convenience_max", "1/year",
     "convenience yield at zero cover (intercept of the Working curve); declared, not fitted",
     "convenience-yield literature (Working-curve estimates)"),
    ("cover_scale_days", "days",
     "decay scale of the convenience yield in cover days; together with r + c it FIXES the normal "
     "cover, so it is a shape parameter with a visible consequence",
     "assumption: shape of the convenience-yield curve"),
    ("capacity_mb", "mb", "total commercial + SPR storage capacity in the scenario",
     "IEA/EIA storage capacity estimates"),
    ("commercial_max_draw_mb_per_day", "mb/d", "maximum commercial drawdown rate",
     "assumption: logistics"),
    ("commercial_max_build_mb_per_day", "mb/d", "maximum commercial build rate (tank filling)",
     "assumption: logistics"),
    ("spr_max_draw_mb_per_day", "mb/d", "maximum strategic (SPR/IEA) release rate",
     "IEA collective action precedents (1991, 2005, 2011, 2022)"),
    ("loss_fraction_per_month", "1/month", "evaporation/lease loss on the stock",
     "assumption: storage loss"),
)


def assumptions(params: StorageParams | None = None, *,
                state: "StorageState | None" = None,
                spot: float | None = None,
                spot_source: str = "") -> tuple[Assumption, ...]:
    """Declared inputs with provenance: parameters are ``assumption``, the EIA spot is ``observed``."""
    params = params if params is not None else DEFAULT_PARAMS
    values = params.as_dict()
    declared = []
    for name, unit, note, source in ASSUMPTION_SPECS:
        declared.append(Assumption(name=name, value=values.get(name), unit=unit,
                                   source_class="assumption", note=note, source=source))
    if state is not None:
        declared.append(Assumption(
            name="stock_commercial_mb", value=_q(state.stock_commercial_mb), unit="mb",
            source_class="assumption",
            note="starting commercial inventory of the scenario (not an observed EIA stocks series)",
            source="scenario state; replace with EIA weekly stocks / IEA Oil Market Report"))
        declared.append(Assumption(
            name="stock_strategic_mb", value=_q(state.stock_spr_mb), unit="mb",
            source_class="assumption",
            note="starting strategic inventory of the scenario",
            source="scenario state; replace with IEA/DOE SPR data"))
        declared.append(Assumption(
            name="demand_mb_per_day", value=_q(state.demand_mb_per_day), unit="mb/d",
            source_class="assumption",
            note="served demand of the days-of-cover convention",
            source="scenario state; replace with IEA OMR demand"))
    if spot is not None:
        declared.append(Assumption(
            name="spot_usd_per_bbl", value=_q(spot), unit=UNITS, source_class="observed",
            note="observed EIA RBRTE monthly average used as S in the curve",
            source=spot_source or "EIA RBRTE (data/oil/brent_monthly_eia.csv)"))
    return tuple(declared)


# ----------------------------------------------------------------------------------------- state
@dataclass(frozen=True)
class StorageState:
    """Stock state of the storage layer: commercial and strategic inventories plus the demand."""

    stock_commercial_mb: float = 3000.0
    stock_spr_mb: float = 0.0
    demand_mb_per_day: float = 100.0

    def __post_init__(self) -> None:
        _non_negative(self.stock_commercial_mb, "stock_commercial_mb")
        _non_negative(self.stock_spr_mb, "stock_spr_mb")
        _positive(self.demand_mb_per_day, "demand_mb_per_day")
        object.__setattr__(self, "stock_commercial_mb", float(self.stock_commercial_mb))
        object.__setattr__(self, "stock_spr_mb", float(self.stock_spr_mb))
        object.__setattr__(self, "demand_mb_per_day", float(self.demand_mb_per_day))

    @property
    def total_mb(self) -> float:
        return float(self.stock_commercial_mb) + float(self.stock_spr_mb)

    @property
    def cover_days(self) -> float:
        return self.total_mb / float(self.demand_mb_per_day)

    def as_dict(self) -> dict:
        return {"stock_commercial_mb": _q(self.stock_commercial_mb),
                "stock_strategic_mb": _q(self.stock_spr_mb),
                "total_mb": _q(self.total_mb), "demand_mb_per_day": _q(self.demand_mb_per_day),
                "cover_days": _q(self.cover_days)}


@dataclass(frozen=True)
class SupplyShock:
    """Monthly physical shocks: lost supply, replacement supply and a demand shift (mb/d)."""

    lost_supply_mb_per_day: tuple[float, ...]
    added_supply_mb_per_day: tuple[float, ...] = ()
    demand_shift_mb_per_day: tuple[float, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        lost = tuple(float(item) for item in self.lost_supply_mb_per_day)
        if not lost:
            raise StorageError("lost_supply_mb_per_day must not be empty")
        months = len(lost)
        added = tuple(float(item) for item in self.added_supply_mb_per_day) \
            or tuple(0.0 for _ in range(months))
        shift = tuple(float(item) for item in self.demand_shift_mb_per_day) \
            or tuple(0.0 for _ in range(months))
        if not (len(added) == len(shift) == months):
            raise StorageError(f"shock vectors must all have {months} entries; got lost={months}, "
                               f"added={len(added)}, demand_shift={len(shift)}")
        for name, vector in (("lost_supply_mb_per_day", lost),
                             ("added_supply_mb_per_day", added)):
            if any(value < 0.0 for value in vector):
                raise StorageError(f"{name} must be non-negative")
        if any(not math.isfinite(value) for value in shift):
            raise StorageError("demand_shift_mb_per_day must be finite")
        object.__setattr__(self, "lost_supply_mb_per_day", lost)
        object.__setattr__(self, "added_supply_mb_per_day", added)
        object.__setattr__(self, "demand_shift_mb_per_day", shift)
        object.__setattr__(self, "label", str(self.label))

    @property
    def months(self) -> int:
        return len(self.lost_supply_mb_per_day)

    @classmethod
    def outage(cls, months: int, lost_mb_per_day: float, label: str = "") -> "SupplyShock":
        """A flat outage of ``lost_mb_per_day`` for ``months`` months."""
        if not isinstance(months, int) or isinstance(months, bool) or months <= 0:
            raise StorageError("months must be a positive integer")
        return cls(lost_supply_mb_per_day=tuple(float(lost_mb_per_day) for _ in range(months)),
                   label=label)


# ------------------------------------------------------------------------------------------ path
@dataclass(frozen=True)
class StoragePath:
    """Monthly storage path: stocks, flows, cover and the curve regime, with explicit checks."""

    periods: tuple[int, ...]
    demand_mb_per_day: tuple[float, ...]
    supply_mb_per_day: tuple[float, ...]
    commercial_end_mb: tuple[float, ...]
    strategic_end_mb: tuple[float, ...]
    total_end_mb: tuple[float, ...]
    cover_days: tuple[float, ...]
    draw_commercial_mb: tuple[float, ...]
    draw_strategic_mb: tuple[float, ...]
    build_mb: tuple[float, ...]
    loss_mb: tuple[float, ...]
    unmet_mb_per_day: tuple[float, ...]
    surplus_mb_per_day: tuple[float, ...]
    carry_annual: tuple[float, ...]
    regime: tuple[str, ...]
    identity_residuals: tuple[float, ...]
    mass_residual: float
    capacity_mb: float
    checks: Mapping = field(default_factory=dict)
    label: str = ""
    status: str = RESEARCH_ONLY
    schema: str = SCHEMA_PATH

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise StorageError("storage paths are research-only")
        if not self.periods:
            raise StorageError("a storage path needs at least one period")

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(bool(value) for value in self.checks.values())

    @property
    def unmet_total(self) -> float:
        return math.fsum(self.unmet_mb_per_day)

    @property
    def contango_months(self) -> int:
        return sum(1 for item in self.regime if item == "contango")

    @property
    def backwardation_months(self) -> int:
        return sum(1 for item in self.regime if item == "backwardation")

    def rows(self) -> list[dict]:
        keys = ("commercial_end_mb", "strategic_end_mb", "total_end_mb", "cover_days",
                "draw_commercial_mb", "draw_strategic_mb", "build_mb", "loss_mb",
                "unmet_mb_per_day", "surplus_mb_per_day")
        rows = []
        for index, period in enumerate(self.periods):
            row = {"period": period, "demand_mb_per_day": _q(self.demand_mb_per_day[index]),
                   "supply_mb_per_day": _q(self.supply_mb_per_day[index])}
            for key in keys:
                row[key] = _q(getattr(self, key)[index])
            row["capacity_utilisation"] = _q(self.total_end_mb[index] / self.capacity_mb)
            row["carry_annual"] = _q(self.carry_annual[index])
            row["regime"] = self.regime[index]
            rows.append(row)
        return rows

    def summary(self) -> dict:
        return {
            "months": len(self.periods),
            "unmet_mb_per_day_total": _q(self.unmet_total),
            "surplus_mb_per_day_total": _q(math.fsum(self.surplus_mb_per_day)),
            "draw_commercial_mb_total": _q(math.fsum(self.draw_commercial_mb)),
            "draw_strategic_mb_total": _q(math.fsum(self.draw_strategic_mb)),
            "build_mb_total": _q(math.fsum(self.build_mb)),
            "loss_mb_total": _q(math.fsum(self.loss_mb)),
            "commercial_end_mb": _q(self.commercial_end_mb[-1]),
            "strategic_end_mb": _q(self.strategic_end_mb[-1]),
            "cover_days_start": _q(self.cover_days[0]),
            "cover_days_end": _q(self.cover_days[-1]),
            "contango_months": self.contango_months,
            "backwardation_months": self.backwardation_months,
            "mass_residual": self.mass_residual,
            "identity_residual_max": max((abs(item) for item in self.identity_residuals),
                                         default=0.0),
        }

    def digest(self) -> str:
        return _sha256_json({"schema": self.schema, "status": self.status, "label": self.label,
                             "capacity_mb": _q(self.capacity_mb), "rows": self.rows(),
                             "summary": self.summary(),
                             "checks": {key: bool(value)
                                        for key, value in self.checks.items()}})


def simulate_storage(params: StorageParams, state: StorageState, shock: SupplyShock,
                     base_supply_mb_per_day: float) -> StoragePath:
    """Run the monthly storage balance for one shock (deterministic, fail-closed).

    Each month ``demand = state.demand + shift`` and ``supply = base_supply - lost + added``.  A
    deficit is covered from the commercial stock, then from the strategic stock, each capped by its
    maximum monthly rate (and by the stock itself); the remainder is ``unmet``.  A surplus fills the
    commercial stock up to the build rate and the tank capacity; the rest is ``surplus`` (curtailed -
    the model has no negative prices).  Loss applies pro rata to the end-of-month stock.

    ``base_supply_mb_per_day`` is the unshocked supply, a scenario input rather than a state.
    """
    if not isinstance(params, StorageParams):
        raise StorageError("params must be a StorageParams instance")
    if not isinstance(state, StorageState):
        raise StorageError("state must be a StorageState instance")
    if not isinstance(shock, SupplyShock):
        raise StorageError("shock must be a SupplyShock instance")
    base_supply = _positive(base_supply_mb_per_day, "base_supply_mb_per_day")
    if state.total_mb > params.capacity_mb + params.tolerance:
        raise StorageError(f"starting stock {state.total_mb} exceeds capacity "
                           f"{params.capacity_mb} (fail-closed, no silent clipping)")
    dpm = float(params.days_per_month)
    tol = float(params.tolerance)
    commercial = float(state.stock_commercial_mb)
    strategic = float(state.stock_spr_mb)

    series: dict[str, list] = {key: [] for key in (
        "demand", "supply", "commercial", "strategic", "total", "cover", "draw_c", "draw_s",
        "build", "loss", "unmet", "surplus", "carry", "regime")}
    identity: list[float] = []
    resource_residuals: list[float] = []
    for month in range(shock.months):
        period = month + 1
        demand = float(state.demand_mb_per_day) + float(shock.demand_shift_mb_per_day[month])
        if demand <= 0.0:
            raise StorageError(f"month {period}: demand {demand} is not positive")
        supply = (base_supply - float(shock.lost_supply_mb_per_day[month])
                  + float(shock.added_supply_mb_per_day[month]))
        if supply < 0.0:
            if supply < -tol:
                raise StorageError(f"month {period}: supply {supply} is negative (a negative flow "
                                   "cannot be netted out of a stock)")
            supply = 0.0

        start_total = commercial + strategic
        gap = demand - supply                    # mb/d, positive = deficit
        draw_c_rate = draw_s_rate = build_rate = unmet_rate = surplus_rate = 0.0
        if gap > 0.0:
            draw_c_rate = min(gap, params.commercial_max_draw_mb_per_day, commercial / dpm)
            draw_s_rate = min(gap - draw_c_rate, params.spr_max_draw_mb_per_day, strategic / dpm)
            unmet_rate = max(0.0, gap - draw_c_rate - draw_s_rate)
        else:
            room_mb = max(0.0, params.capacity_mb - start_total)
            build_rate = min(-gap, params.commercial_max_build_mb_per_day, room_mb / dpm)
            surplus_rate = max(0.0, (-gap) - build_rate)
        draw_c = draw_c_rate * dpm
        draw_s = draw_s_rate * dpm
        build = build_rate * dpm

        commercial_pre = commercial - draw_c + build
        strategic_pre = strategic - draw_s
        total_pre = commercial_pre + strategic_pre
        loss = params.loss_fraction_per_month * total_pre
        loss_c = loss * (commercial_pre / total_pre) if total_pre > 0.0 else 0.0
        loss_s = loss - loss_c
        commercial = commercial_pre - loss_c
        strategic = strategic_pre - loss_s
        total_end = commercial + strategic

        # per-month physical stock identity (docs/177 A1 for the storage layer)
        residual = total_end - (start_total + build - draw_c - draw_s - loss)
        # stage-4 resource balance on the monthly *flow*: a stock change appears exactly once, on
        # the correct side.  Evaporation/lease loss shrinks the stock itself, so it is checked by
        # the stock identity and the chain mass balance below instead of the flow balance.
        try:
            balance = ResourceBalance(
                resource_id=COMMODITY, unit="mb", period=f"month_{period}",
                supply={"primary_supply": supply * dpm, "stock_draw": draw_c + draw_s},
                use={"served_demand": (demand - unmet_rate) * dpm, "stock_build": build,
                     "curtailed_supply": surplus_rate * dpm},
            )
            resource_residuals.append(balance.validate())
        except ResourceBalanceError as error:  # fail-closed: never patch an identity
            raise StorageError(f"month {period}: resource balance is inconsistent: {error}") \
                from None

        cover = total_end / demand
        series["demand"].append(demand)
        series["supply"].append(supply)
        series["commercial"].append(commercial)
        series["strategic"].append(strategic)
        series["total"].append(total_end)
        series["cover"].append(cover)
        series["draw_c"].append(draw_c)
        series["draw_s"].append(draw_s)
        series["build"].append(build)
        series["loss"].append(loss)
        series["unmet"].append(unmet_rate)
        series["surplus"].append(surplus_rate)
        series["carry"].append(params.carry(cover))
        series["regime"].append(params.regime(cover))
        identity.append(residual)

    initial = float(state.total_mb)
    final = series["total"][-1]
    mass_residual = (final - initial
                     - (math.fsum(series["build"]) - math.fsum(series["draw_c"])
                        - math.fsum(series["draw_s"]) - math.fsum(series["loss"])))
    checks = {
        "stock_identity": max((abs(item) for item in identity), default=0.0) <= tol,
        "mass_balance": abs(mass_residual) <= tol,
        "resource_balance": max((abs(item) for item in resource_residuals), default=0.0) <= tol,
        "stocks_non_negative": all(value >= -tol for value in series["total"]),
        "capacity_respected": all(value <= params.capacity_mb + tol for value in series["total"]),
        "flows_non_negative": all(value >= -tol for value in
                                  series["draw_c"] + series["draw_s"] + series["build"]
                                  + series["loss"] + series["unmet"] + series["surplus"]),
        "cover_non_negative": all(value >= -tol for value in series["cover"]),
        "no_spurious_unmet": all(value >= -tol for value in series["unmet"]),
        "unmet_is_physical": all(value <= row_demand + tol
                                 for value, row_demand in zip(series["unmet"],
                                                              series["demand"])),
    }
    return StoragePath(
        periods=tuple(range(1, shock.months + 1)),
        demand_mb_per_day=tuple(series["demand"]), supply_mb_per_day=tuple(series["supply"]),
        commercial_end_mb=tuple(series["commercial"]),
        strategic_end_mb=tuple(series["strategic"]),
        total_end_mb=tuple(series["total"]), cover_days=tuple(series["cover"]),
        draw_commercial_mb=tuple(series["draw_c"]),
        draw_strategic_mb=tuple(series["draw_s"]),
        build_mb=tuple(series["build"]), loss_mb=tuple(series["loss"]),
        unmet_mb_per_day=tuple(series["unmet"]),
        surplus_mb_per_day=tuple(series["surplus"]),
        carry_annual=tuple(series["carry"]), regime=tuple(series["regime"]),
        identity_residuals=tuple(identity), mass_residual=mass_residual,
        capacity_mb=float(params.capacity_mb), checks=checks, label=shock.label)


# ---------------------------------------------------------------------------------------- curve
@dataclass(frozen=True)
class CurvePoint:
    """One futures tenor of the curve."""

    months: float
    price: float
    basis: float

    def as_dict(self) -> dict:
        return {"months": _q(self.months), "price": _q(self.price), "basis": _q(self.basis)}


@dataclass(frozen=True)
class CurveReport:
    """The futures curve of one (spot, cover) point with its arbitrage identities checked."""

    spot: float
    cover_days: float
    normal_cover_days: float
    convenience_yield: float
    carry_annual: float
    cost_of_carry: float
    regime: str
    points: tuple[CurvePoint, ...]
    residual_multiplicative: float
    residual_forward: float
    capacity_utilisation: float | None = None
    checks: Mapping = field(default_factory=dict)
    status: str = RESEARCH_ONLY
    schema: str = SCHEMA_CURVE

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise StorageError("curve reports are research-only")
        if self.spot <= 0.0:
            raise StorageError("spot must be positive")
        if len(self.points) < 2:
            raise StorageError("a curve needs at least two points")

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(bool(value) for value in self.checks.values())

    @property
    def six_month_spread_usd(self) -> float | None:
        for point in self.points:
            if abs(float(point.months) - 6.0) <= 1e-9:
                return _q(point.price - self.spot)
        return None

    def as_dict(self) -> dict:
        return {
            "schema": self.schema, "status": self.status,
            "spot_usd_per_bbl": _q(self.spot), "cover_days": _q(self.cover_days),
            "normal_cover_days": _q(self.normal_cover_days),
            "convenience_yield": _q(self.convenience_yield),
            "cost_of_carry": _q(self.cost_of_carry), "carry_annual": _q(self.carry_annual),
            "carry_monthly": _q(self.carry_annual / 12.0), "regime": self.regime,
            "capacity_utilisation": (None if self.capacity_utilisation is None
                                     else _q(self.capacity_utilisation)),
            "six_month_spread_usd": self.six_month_spread_usd,
            "points": [point.as_dict() for point in self.points],
            "residuals": {"multiplicative_relative": self.residual_multiplicative,
                          "forward_rate_absolute": self.residual_forward,
                          "tolerance": TOLERANCE},
            "checks": {key: bool(value) for key, value in self.checks.items()},
        }

    def digest(self) -> str:
        return _sha256_json(self.as_dict())


def build_curve(params: StorageParams, spot: float, cover_days: float,
                tenors: Sequence[float] = DEFAULT_TENORS,
                capacity_utilisation: float | None = None) -> CurveReport:
    """``F(T) = S * exp((r + c - y(cover)) * T / 12)`` with the no-arbitrage identities checked.

    The identities are the useful part: ``F(0) = S``; ``F(T1) * F(T2) = F(T1 + T2) * S`` (the
    exponent is linear in ``T``, so the relative residual is the test); the forward rate implied by
    two tenors equals the carry; the curve is monotone with the carry sign and every price is
    positive.
    """
    price = _positive(spot, "spot")
    cover = _non_negative(cover_days, "cover_days")
    tenors = tuple(_positive(item, "tenor") for item in tenors)
    if len(tenors) < 2:
        raise StorageError("build_curve needs at least two tenors")
    if len(set(tenors)) != len(tenors):
        raise StorageError("tenors must be distinct")
    ordered = tuple(sorted(tenors))
    carry = params.carry(cover)
    points = tuple(CurvePoint(months=item, price=price * params.basis(cover, item),
                              basis=params.basis(cover, item)) for item in ordered)

    zero_ok = params.forward_price(price, cover, 0.0) == price
    multiplicative = 0.0
    for first in points:
        for second in points:
            implied = params.forward_price(price, cover, first.months + second.months)
            multiplicative = max(multiplicative,
                                 abs(implied * price - first.price * second.price) / (price * price))
    forward_residual = 0.0
    for index in range(len(ordered) - 1):
        implied = params.implied_forward_rate(price, cover, ordered[index], ordered[index + 1])
        forward_residual = max(forward_residual, abs(implied - carry))
    values = [point.price for point in points]
    regime = params.regime(cover)
    if regime == "contango":
        monotone = all(values[index] <= values[index + 1] + TOLERANCE
                       for index in range(len(values) - 1))
        sign_consistent = all(value > price for value in values)
    elif regime == "backwardation":
        monotone = all(values[index] >= values[index + 1] - TOLERANCE
                       for index in range(len(values) - 1))
        sign_consistent = all(value < price for value in values)
    else:  # flat: the carry is zero up to the floating-point band
        monotone = all(abs(value - price) <= TOLERANCE for value in values)
        sign_consistent = monotone
    checks = {
        "spot_positive": price > 0.0,
        "f0_equals_spot": zero_ok,
        "prices_positive": all(value > 0.0 for value in values),
        "multiplicative_identity": multiplicative <= TOLERANCE,
        "forward_rate_identity": forward_residual <= TOLERANCE,
        "monotone_in_tenor": monotone,
        "regime_matches_sign": sign_consistent,
    }
    return CurveReport(spot=price, cover_days=cover,
                       normal_cover_days=params.normal_cover_days,
                       convenience_yield=params.convenience_yield(cover),
                       carry_annual=carry, cost_of_carry=params.cost_of_carry,
                       regime=regime, points=points,
                       residual_multiplicative=multiplicative,
                       residual_forward=forward_residual,
                       capacity_utilisation=capacity_utilisation, checks=checks)


# ------------------------------------------------------------------------------------- episodes
def _month_key(value: str) -> tuple[int, int]:
    try:
        year, month = str(value).split("-")
        key = (int(year), int(month))
    except (AttributeError, ValueError):
        raise StorageError(f"bad month {value!r}; expected YYYY-MM") from None
    if not 1 <= key[1] <= 12:
        raise StorageError(f"bad month {value!r}; expected YYYY-MM")
    return key


def _sequence(start: str, months: int) -> tuple[tuple[int, int], ...]:
    year, month = _month_key(start)
    keys = []
    for _ in range(int(months)):
        keys.append((year, month))
        month += 1
        if month > 12:
            year += 1
            month = 1
    return tuple(keys)


@dataclass(frozen=True)
class StorageEpisode:
    """One historical window encoded for the curve-sign test (stocks and demand are assumptions)."""

    episode_id: str
    name: str
    start_month: str
    months: int
    stock_start_mb: float
    demand_mb_per_day: float
    supply_mb_per_day: float
    stock_spr_mb: float = 0.0
    demand_shift_mb_per_day: tuple[float, ...] = ()
    lost_supply_mb_per_day: tuple[float, ...] = ()
    required_regime: str = "contango"
    required_months: int = 3
    note: str = ""

    def __post_init__(self) -> None:
        if not str(self.episode_id).strip():
            raise StorageError("episode_id must be non-empty")
        if not isinstance(self.months, int) or isinstance(self.months, bool) or self.months <= 0:
            raise StorageError(f"{self.episode_id}: months must be a positive integer")
        if self.months > 24:
            raise StorageError(f"{self.episode_id}: horizon is unreasonably long")
        _month_key(self.start_month)
        if self.required_regime not in REGIMES:
            raise StorageError(f"{self.episode_id}: unknown required_regime "
                               f"{self.required_regime!r}")
        if not 0 <= int(self.required_months) <= self.months:
            raise StorageError(f"{self.episode_id}: required_months must be in [0, months]")
        _non_negative(self.stock_start_mb, f"{self.episode_id}: stock_start_mb")
        _non_negative(self.stock_spr_mb, f"{self.episode_id}: stock_spr_mb")
        _positive(self.demand_mb_per_day, f"{self.episode_id}: demand_mb_per_day")
        _positive(self.supply_mb_per_day, f"{self.episode_id}: supply_mb_per_day")
        for name in ("demand_shift_mb_per_day", "lost_supply_mb_per_day"):
            vector = getattr(self, name)
            if vector and len(vector) != self.months:
                raise StorageError(f"{self.episode_id}: {name} needs {self.months} entries")

    def shock(self) -> SupplyShock:
        lost = tuple(self.lost_supply_mb_per_day) or tuple(0.0 for _ in range(self.months))
        shift = tuple(self.demand_shift_mb_per_day) or tuple(0.0 for _ in range(self.months))
        return SupplyShock(lost_supply_mb_per_day=lost, demand_shift_mb_per_day=shift,
                           label=self.episode_id)

    def state(self) -> StorageState:
        return StorageState(stock_commercial_mb=self.stock_start_mb,
                            stock_spr_mb=self.stock_spr_mb,
                            demand_mb_per_day=self.demand_mb_per_day)


#: The two windows of docs/177 A2.  Stock levels, demand and the 2020 demand shift are *declared*
#: assumptions (IEA/EIA public order of magnitude, rounded); the spot is observed (EIA RBRTE).
EPISODES: tuple[StorageEpisode, ...] = (
    StorageEpisode(
        episode_id="2020_storage_build",
        name="2020 COVID-19 demand collapse: tanks fill, curve flips to contango (Mar-Jun 2020)",
        start_month="2020-03", months=4, stock_start_mb=2950.0, demand_mb_per_day=100.0,
        supply_mb_per_day=100.0,
        demand_shift_mb_per_day=(-3.0, -13.0, -12.5, -8.5),
        required_regime="contango", required_months=3,
        note="demand shift is a declared round-number assumption (IEA/EIA demand estimates); the "
             "stock build is capped by commercial_max_build_mb_per_day and is not fitted to the "
             "observed 2020 contango magnitude"),
    StorageEpisode(
        episode_id="2021_2022_low_cover",
        name="2021-2022 low cover: backwardation as stocks are drawn (Nov 2021 - Apr 2022)",
        start_month="2021-11", months=6, stock_start_mb=3000.0, demand_mb_per_day=100.0,
        supply_mb_per_day=99.0,
        required_regime="backwardation", required_months=4,
        note="a one mb/d supply deficit forces a stock draw at the declared maximum rate; the "
             "starting stock and the deficit are declared assumptions"),
)
EPISODES_BY_ID = {item.episode_id: item for item in EPISODES}


def episode(episode_id: str) -> StorageEpisode:
    if episode_id not in EPISODES_BY_ID:
        raise StorageError(f"unknown episode {episode_id!r}; available {sorted(EPISODES_BY_ID)}")
    return EPISODES_BY_ID[episode_id]


def episode_spots(target: StorageEpisode, series: Mapping) -> tuple[float, ...]:
    """Observed monthly Brent averages for the episode window (fail-closed on a missing month)."""
    prices = series.get("prices") if isinstance(series, Mapping) else None
    if not isinstance(prices, Mapping) or not prices:
        raise StorageError("episode_spots needs a loaded EIA series (series['prices'])")
    observed = []
    for year, month in _sequence(target.start_month, target.months):
        key = (year, month)
        if key not in prices:
            raise StorageError(f"{target.episode_id}: month {year}-{month:02d} is missing in the "
                               f"EIA series; the episode cannot be scored")
        observed.append(float(prices[key]))
    return tuple(observed)


@dataclass(frozen=True)
class EpisodeResult:
    """One episode: the storage path, the monthly curve sign and the pre-registered verdict."""

    episode: StorageEpisode
    spots: tuple[float, ...]
    path: StoragePath
    curves: tuple[CurveReport, ...]
    regimes: tuple[str, ...]
    matching_months: int
    verdict: str

    def as_dict(self) -> dict:
        keys = _sequence(self.episode.start_month, self.episode.months)
        rows = []
        for index, row in enumerate(self.path.rows()):
            year, month = keys[index]
            rows.append({**row, "month": f"{year:04d}-{month:02d}",
                         "spot_usd_per_bbl": _q(self.spots[index]),
                         "regime": self.regimes[index],
                         "six_month_spread_usd": self.curves[index].six_month_spread_usd})
        summary = self.path.summary()
        return {
            "episode_id": self.episode.episode_id, "name": self.episode.name,
            "start_month": self.episode.start_month, "months": self.episode.months,
            "required_regime": self.episode.required_regime,
            "required_months": self.episode.required_months,
            "matching_months": self.matching_months, "verdict": self.verdict,
            "cover_days_start": summary["cover_days_start"],
            "cover_days_end": summary["cover_days_end"],
            "unmet_mb_per_day_total": summary["unmet_mb_per_day_total"],
            "stock_change_mb": _q(self.path.total_end_mb[-1] - self.path.total_end_mb[0]),
            "checks": {key: bool(value) for key, value in self.path.checks.items()},
            "note": self.episode.note,
            "rows": rows,
        }


def run_episode(params: StorageParams, target: StorageEpisode,
                series: Mapping | None = None) -> EpisodeResult:
    """Run one episode and classify the curve month by month against the required regime."""
    if not isinstance(params, StorageParams):
        raise StorageError("params must be a StorageParams instance")
    if not isinstance(target, StorageEpisode):
        raise StorageError("target must be a StorageEpisode instance")
    series = series if series is not None else load_series()
    spots = episode_spots(target, series)
    path = simulate_storage(params, target.state(), target.shock(), target.supply_mb_per_day)
    if not path.ok:
        raise StorageError(f"{target.episode_id}: storage checks failed {path.checks}")
    curves = []
    regimes = []
    for index, spot in enumerate(spots):
        curve = build_curve(params, spot, path.cover_days[index])
        if not curve.ok:
            raise StorageError(f"{target.episode_id}: curve checks failed at month {index + 1}: "
                               f"{curve.checks}")
        curves.append(curve)
        regimes.append(curve.regime)
    matching = sum(1 for item in regimes if item == target.required_regime)
    verdict = "PASS" if matching >= target.required_months else "FAIL"
    return EpisodeResult(episode=target, spots=spots, path=path, curves=tuple(curves),
                         regimes=tuple(regimes), matching_months=matching, verdict=verdict)


# ---------------------------------------------------------------------- cross-check with oil.py
def _oil_hormuz_shock(horizon: int) -> SupplyShock:
    """Rebuild the oil module's declared Hormuz shock from its own parameters (no copied numbers)."""
    from .oil import DEFAULT_PARAMS as OIL_PARAMS

    closure = min(int(OIL_PARAMS["closure_months"]), horizon)
    lost = [0.0] * horizon
    flow = float(OIL_PARAMS["closure_share"]) * float(OIL_PARAMS["hormuz_transit"])
    for month in range(closure):
        lost[month] = flow
    bypass = []
    for month in range(1, horizon + 1):
        speed = min(1.0, month / float(OIL_PARAMS["bypass_ramp_months"]))
        response = min(1.0, month / float(OIL_PARAMS["supply_response_ramp_months"]))
        bypass.append(round(float(OIL_PARAMS["bypass_capacity"]) * speed
                            + float(OIL_PARAMS["supply_response"]) * response, 12))
    return SupplyShock(lost_supply_mb_per_day=tuple(lost),
                       added_supply_mb_per_day=tuple(bypass), label="hormuz_closed_6m")


def cross_check_with_oil(params: StorageParams | None = None, horizon_months: int = 6) -> dict:
    """Consistency check of two independent encodings of the same declared Hormuz scenario.

    The oil module (docs/147) and this storage layer both describe the same closure: transit
    20.5 mb/d lost, bypass ramped to 4.2 mb/d over three months, a 1.0 mb/d non-Gulf response ramped
    over four months, 150 mb SPR at 1.5 mb/d and 200 mb commercial at 1.0 mb/d.  The check compares
    the quantities both implementations must agree on - the net physical supply loss, the commercial
    drawdown and the strategic release - within the docs/177 A5 band of +-10%.  It validates the
    encoding of the scenario across two code paths; it is **not** a new estimate of the loss.
    """
    from .oil import DEFAULT_PARAMS as OIL_PARAMS, OilModule

    params = params if params is not None else replace(
        DEFAULT_PARAMS,
        commercial_max_draw_mb_per_day=float(OIL_PARAMS["commercial_max_draw"]),
        commercial_max_build_mb_per_day=float(DEFAULT_PARAMS.commercial_max_build_mb_per_day),
        spr_max_draw_mb_per_day=float(OIL_PARAMS["spr_max_draw"]))
    horizon = int(horizon_months)
    if horizon <= 0:
        raise StorageError("horizon_months must be positive")
    result = OilModule().run("hormuz_closed_6m")
    summary = result.summary
    rows = summary["monthly_path"]
    if len(rows) < horizon:
        raise StorageError("the oil path is shorter than the cross-check horizon")
    base_supply = float(OIL_PARAMS["baseline_supply"])
    base_demand = float(OIL_PARAMS["base_demand"])

    shock = _oil_hormuz_shock(horizon)
    state = StorageState(stock_commercial_mb=float(OIL_PARAMS["commercial_stock"]),
                         stock_spr_mb=float(OIL_PARAMS["spr_stock"]),
                         demand_mb_per_day=base_demand)
    path = simulate_storage(params, state, shock, base_supply)
    if not path.ok:
        raise StorageError(f"cross-check storage path failed {path.checks}")

    storage_loss = math.fsum(shock.lost_supply_mb_per_day) - \
        math.fsum(shock.added_supply_mb_per_day)
    oil_loss = math.fsum(base_supply + float(row["strategic_release"])
                         - float(row["supply_available"]) for row in rows[:horizon])
    storage_draw = math.fsum(path.draw_commercial_mb)
    oil_draw = float(OIL_PARAMS["commercial_stock"]) - float(rows[horizon - 1]
                                                            ["commercial_stock_end"])
    storage_release = math.fsum(path.draw_strategic_mb)
    oil_release = DAYS_PER_MONTH * math.fsum(float(row["strategic_release"])
                                             for row in rows[:horizon])
    comparisons = {}
    for name, storage_value, oil_value in (
            ("net_supply_loss_mb_per_day", storage_loss, oil_loss),
            ("strategic_release_mb", storage_release, oil_release)):
        if abs(oil_value) <= 1e-9:
            comparisons[name] = {"storage": _q(storage_value), "oil_module": _q(oil_value),
                                 "ratio": None,
                                 "within_band": abs(storage_value) <= 1e-9}
            continue
        ratio = storage_value / oil_value
        comparisons[name] = {"storage": _q(storage_value), "oil_module": _q(oil_value),
                             "ratio": _q(ratio),
                             "relative_difference": _q(abs(ratio - 1.0), 12),
                             "within_band": abs(ratio - 1.0) <= CROSS_CHECK_BAND}
    structural = {
        "commercial_draw_mb": {
            "storage": _q(storage_draw), "oil_module": _q(oil_draw),
            "note": "structural difference, not a numerical error: the oil engine clears the gap "
                    "with the price (demand elasticity), so its commercial stock stays inert at "
                    "the clearing price, while the storage layer covers the gap from the stocks. "
                    "That is the gap docs/177 §3.1 names ('stocks are an exogenous limit, not a "
                    "state'); the check therefore compares the physical shock and the strategic "
                    "release, not the commercial draw."},
    }
    oil_price_up = float(summary["price_peak"]) > float(OIL_PARAMS["base_price"])
    storage_tight = path.cover_days[-1] < params.normal_cover_days
    checks = {
        "within_band": all(item["within_band"] for item in comparisons.values()),
        "direction_consistent": bool(oil_price_up == storage_tight),
        "band": CROSS_CHECK_BAND,
    }
    return {"oil_module": result.module_id, "oil_scenario": result.scenario_id,
            "horizon_months": horizon, "comparisons": comparisons,
            "structural_differences": structural, "checks": checks,
            "storage_summary": path.summary(),
            "note": "two independent encodings of one declared Hormuz closure: a consistency check "
                    "on the scenario arithmetic, not a new supply-loss estimate"}


# ---------------------------------------------------------------------------------------- report
@dataclass(frozen=True)
class StorageReport:
    """The complete research report: data provenance, parameters, curve, episodes, cross-check."""

    params: StorageParams
    state: StorageState
    data: Mapping
    curve: CurveReport
    episodes: tuple[EpisodeResult, ...]
    cross_check: Mapping
    checks: Mapping = field(default_factory=dict)
    assumptions: tuple[Assumption, ...] = ()
    status: str = RESEARCH_ONLY
    schema: str = SCHEMA_REPORT

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise StorageError("storage reports are research-only")
        if not self.episodes:
            raise StorageError("a storage report needs at least one episode")

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(bool(value) for value in self.checks.values())

    def as_dict(self) -> dict:
        return {
            "schema": self.schema, "status": self.status, "research_only": True,
            "module_id": MODULE_ID, "commodity": COMMODITY, "units": UNITS,
            "note": "storage/futures research prototype: declared assumptions on public EIA data; "
                    "not an admission, not a forecast and not investment advice",
            "data": dict(self.data),
            "params": self.params.as_dict(),
            "state": self.state.as_dict(),
            "assumptions": [item.as_dict() for item in self.assumptions],
            "curve": self.curve.as_dict(),
            "episodes": [item.as_dict() for item in self.episodes],
            "cross_check": dict(self.cross_check),
            "checks": {key: bool(value) for key, value in self.checks.items()},
            "digest": self.digest(),
        }

    def digest(self) -> str:
        return _sha256_json({"params": self.params.as_dict(), "state": self.state.as_dict(),
                             "data": dict(self.data), "curve": self.curve.as_dict(),
                             "episodes": [item.as_dict() for item in self.episodes],
                             "cross_check": dict(self.cross_check),
                             "checks": {key: bool(value)
                                        for key, value in self.checks.items()},
                             "status": self.status})


def load_spot(series: Mapping | None = None, month: str | None = None) -> dict:
    """Observed EIA spot for one month (default: the last published month)."""
    series = series if series is not None else load_series()
    prices = series.get("prices") or {}
    if not prices:
        raise StorageError("the EIA series has no observations")
    if month is None:
        key = max(prices)
    else:
        key = _month_key(month)
        if key not in prices:
            raise StorageError(f"month {month} is not in the EIA series "
                               f"({series.get('first_month')}..{series.get('last_month')})")
    return {"month": f"{key[0]:04d}-{key[1]:02d}", "price": float(prices[key]),
            "series_id": series.get("series_id"), "path": series.get("path"),
            "sha256": series.get("sha256"), "observations": series.get("observations"),
            "first_month": series.get("first_month"), "last_month": series.get("last_month"),
            "source": series.get("source"), "license": series.get("license"),
            "frequency": series.get("frequency")}


def build_report(series: Mapping | None = None, params: StorageParams | None = None, *,
                 spot: float | None = None, month: str | None = None,
                 stock_mb: float | None = None, cover_days: float | None = None,
                 demand_mb_per_day: float = 100.0, spr_mb: float = 0.0) -> StorageReport:
    """Build the full report: observed spot -> state -> curve -> two episodes -> cross-check."""
    params = params if params is not None else DEFAULT_PARAMS
    if not isinstance(params, StorageParams):
        raise StorageError("params must be a StorageParams instance")
    if spot is not None and month is not None:
        raise StorageError("pass either an observed month or an explicit spot, not both")
    series = series if series is not None else load_series()
    observed = load_spot(series, month)
    spot_price = _positive(float(observed["price"]) if spot is None else spot, "spot")

    demand = _positive(demand_mb_per_day, "demand_mb_per_day")
    if stock_mb is not None and cover_days is not None:
        raise StorageError("pass either stock_mb or cover_days, not both")
    if stock_mb is not None:
        total = _non_negative(stock_mb, "stock_mb")
    elif cover_days is not None:
        total = _non_negative(cover_days, "cover_days") * demand
    else:
        total = 3000.0
    spr = _non_negative(spr_mb, "spr_mb")
    if total < spr:
        raise StorageError("stock_mb must be at least spr_mb")
    state = StorageState(stock_commercial_mb=total - spr, stock_spr_mb=spr,
                         demand_mb_per_day=demand)
    if state.total_mb > params.capacity_mb + params.tolerance:
        raise StorageError(f"stock {state.total_mb} exceeds capacity {params.capacity_mb}")

    curve = build_curve(params, spot_price, state.cover_days,
                        capacity_utilisation=state.total_mb / params.capacity_mb)
    episodes = tuple(run_episode(params, item, series) for item in EPISODES)
    cross = cross_check_with_oil(params)
    identity_max = max([abs(value) for item in episodes for value in item.path.identity_residuals]
                       + [abs(item.path.mass_residual) for item in episodes])
    checks = {
        "curve_all_pass": curve.ok,
        "episodes_all_pass": all(item.verdict == "PASS" for item in episodes),
        "curve_identities_within_tolerance": (
            curve.residual_multiplicative <= TOLERANCE
            and curve.residual_forward <= TOLERANCE),
        "episode_identities_within_tolerance": identity_max <= TOLERANCE,
        "cross_check_within_band": bool(cross["checks"]["within_band"]),
        "prices_positive": all(point.price > 0.0 for point in curve.points),
        "deterministic": True,
    }
    data = {
        "series_id": observed["series_id"], "path": observed["path"],
        "sha256": observed["sha256"], "observations": observed["observations"],
        "first_month": observed["first_month"], "last_month": observed["last_month"],
        "source": observed["source"], "license": observed["license"],
        "frequency": observed["frequency"], "spot_month": observed["month"],
        "spot_usd_per_bbl": _q(spot_price),
        "spot_source_class": "observed" if spot is None else "assumption (explicit override)",
        "raw_data_committed": False,
        "provenance": "monthly averages only; no raw EIA archive is stored or committed",
    }
    return StorageReport(params=params, state=state, data=data, curve=curve, episodes=episodes,
                         cross_check=cross, checks=checks,
                         assumptions=assumptions(params, state=state, spot=spot_price,
                                                 spot_source=str(observed["source"])))


def report_markdown(report: StorageReport) -> str:
    """Human-readable report (aggregates and short paths; no EIA series is embedded)."""
    curve = report.curve.as_dict()
    params = report.params
    lines = [
        "# Запасы и фьючерсная кривая (M8-A, блок 8)",
        "",
        f"**Статус:** {report.status} — исследовательский прототип, не допуск, не прогноз и не "
        "инвестиционная рекомендация.",
        f"**Дайджест отчёта:** `{report.digest()}`",
        "",
        "## Данные",
        f"- Спот: **{report.data['spot_usd_per_bbl']:.2f} USD/bbl** за {report.data['spot_month']} "
        f"({report.data['series_id']}, {report.data['source']}; лицензия: {report.data['license']})",
        f"- Файл: `{report.data['path']}`, {report.data['observations']} наблюдений "
        f"({report.data['first_month']}…{report.data['last_month']}), "
        f"SHA-256 `{str(report.data['sha256'])[:16]}…`",
        f"- Сырые данные в git: **нет** (`raw_data_committed="
        f"{str(report.data['raw_data_committed']).lower()}`); запасы, спрос и параметры — "
        "объявленные допущения, а не наблюдения.",
        "",
        "## Параметры и нормальное покрытие",
        f"- r = {params.risk_free_rate:.4f}/год, c = {params.storage_cost:.4f}/год, "
        f"y_max = {params.convenience_max:.4f}/год, масштаб y = {params.cover_scale_days:.1f} дней",
        f"- Нормальное покрытие (нулевой кэрри) выводится: "
        f"`cover_norm = scale·ln(y_max/(r+c)) = {params.normal_cover_days:.2f}` дней; "
        f"ёмкость {params.capacity_mb:.0f} mb",
        f"- Текущее покрытие: {curve['cover_days']:.2f} дней, загрузка ёмкости "
        f"{curve['capacity_utilisation']:.1%}",
        f"- y(cover) = {curve['convenience_yield']:.4f}/год, кэрри r+c−y = "
        f"**{curve['carry_annual']:+.4f}/год** ({curve['carry_annual'] / 12.0:+.4f}/мес.) → "
        f"**{curve['regime']}**",
        "",
        "## Кривая F(T) = S·exp((r + c − y(cover))·T/12)",
        "",
        "| T, мес | F, USD/bbl | F/S |",
        "|---|---|---|",
    ]
    for point in curve["points"]:
        lines.append(f"| {point['months']:g} | {point['price']:.2f} | {point['basis']:.6f} |")
    spread = curve["six_month_spread_usd"]
    lines += [
        f"- Спред 6 мес.: **{spread:+.2f} USD/bbl** при неизменном споте (величина — модельный "
        "выход, а не калибровка 2020).",
        f"- Невязки: мультипликативная {curve['residuals']['multiplicative_relative']:.2e}, "
        f"форвардная ставка {curve['residuals']['forward_rate_absolute']:.2e}, "
        f"допуск {curve['residuals']['tolerance']:.0e}.",
        "",
        "## Эпизоды (знак кривой по покрытию)",
        "",
        "| Эпизод | Окно | Требование | Совпало | Вердикт | Покрытие, дни | Δзапас, mb | Unmet |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in report.episodes:
        document = item.as_dict()
        lines.append(
            f"| {document['episode_id']} | {document['start_month']} +{document['months']} мес. | "
            f"{document['required_months']}+ {document['required_regime']} | "
            f"{document['matching_months']}/{document['months']} | **{document['verdict']}** | "
            f"{document['cover_days_start']:.1f}→{document['cover_days_end']:.1f} | "
            f"{document['stock_change_mb']:+.1f} | {document['unmet_mb_per_day_total']:.2f} |")
    lines += ["", "## Кросс-проверка с нефтяным модулем (docs/177 A5, ±10%)", ""]
    for name, item in report.cross_check["comparisons"].items():
        ratio = "—" if item["ratio"] is None else f"{item['ratio']:.4f}"
        lines.append(f"- {name}: запасы {item['storage']:.3f}, нефтяной модуль "
                     f"{item['oil_module']:.3f}, ratio {ratio}, в полосе ±10%: "
                     f"{'да' if item['within_band'] else 'нет'}")
    lines += [
        f"- Направление согласовано: "
        f"{'да' if report.cross_check['checks']['direction_consistent'] else 'нет'} "
        "(рост цены ↔ покрытие ниже нормы).",
        "- Структурное различие: "
        + "; ".join(f"{name}: запасы {item['storage']:.1f}, нефтяной модуль "
                    f"{item['oil_module']:.1f}" for name, item
                    in report.cross_check["structural_differences"].items())
        + " — нефтяной движок закрывает разрыв ценой (эластичность спроса), поэтому его "
          "коммерческий запас при равновесии не расходуется; слой хранения закрывает разрыв "
          "запасом. Это и есть пробел docs/177 §3.1, а не численная ошибка.",
        "",
        "## Проверки",
    ]
    for key, value in report.checks.items():
        lines.append(f"- {key}: {'PASS' if value else 'FAIL'}")
    lines += [
        "",
        "## Ограничения",
        "1. Величина спреда не калибрована: при постоянной стоимости хранения модель даёт знак и "
        "монотонность кривой, но не масштаб 2020 (у полных резервуаров нужна выпуклая предельная "
        "стоимость хранения — следующий шаг).",
        "2. Запасы, спрос, ёмкость, ставки изъятия/заполнения и стоимость хранения — объявленные "
        "допущения; наблюдаемый вход только один — месячный средний Brent EIA (RBRTE, public "
        "domain). Недельные запасы EIA в репозиторий не заведены, поэтому «покрытие» здесь — "
        "состояние сценария, а не наблюдение.",
        "3. Отрицательных цен нет: если резервуары полны, неразмещённое предложение отдаётся как "
        "`surplus` (сокращение добычи), а не как отрицательная цена.",
        "4. Месячный шаг; внутримесячные пики и точное время публикации не моделируются (PIT — на "
        "уровне месяца публикации EIA).",
        "5. Это слой хранения/кривой, а не замена нефтяного модуля; маршрутная сеть (M8-B) — "
        "отдельная часть и здесь не реализована.",
        "",
    ]
    return "\n".join(lines)
