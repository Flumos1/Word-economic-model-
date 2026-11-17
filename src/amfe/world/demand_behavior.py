#!/usr/bin/env python3
"""Behavioural demand model MVP for the price -> demand -> output channel (G4-B, docs/167).

The reduced-form channel (:mod:`amfe.markets.demand`, docs/159) maps a relative price change to a
relative demand change through *declared elasticities*.  This module is a small **behavioural**
alternative: households maximise a CRRA intertemporal utility over a CES basket of demand groups
under a present-value budget constraint, and firms minimise the cost of a CES energy/value-added
input bundle.  The output is shaped like the reduced-form response (same ``by_sector`` /
``aggregate`` / ``monthly`` fields) so it can be swapped in as an *optional* mode; the default mode
is the unchanged reduced form, and no existing caller is modified.

Status: ``RESEARCH_ONLY``, ``admission = false``.  This is a prototype, not a forecast and not an
estimate of the effect of a shock.  All behavioural parameters are explicit **assumptions** with a
provenance class; the G4 acceptance criterion ("elasticities from data, not assumptions") is NOT
met here (see docs/167).

Model in one page
-----------------
Household (final demand)
    CES basket over groups ``i`` with substitution elasticity ``sigma_h``:
        U(C_1..C_n) = (sum_i alpha_i^(1/sigma_h) C_i^((sigma_h-1)/sigma_h))^(sigma_h/(sigma_h-1))
    Demand at month t: ``q_i,t = alpha_i (p_i,t / P_t)^(-sigma_h) C_t``,
        ``P_t = (sum_i alpha_i p_i,t^(1-sigma_h))^(1/(1-sigma_h))``.
    Intertemporal CRRA problem with intertemporal elasticity ``eis`` and discount ``beta``:
        max sum_t beta^t C_t^(1-1/eis)/(1-1/eis)
        s.t. sum_t P_t C_t / (1+r_m)^t = W + sum_t Y / (1+r_m)^t
        => C_t = C_0 (beta (1+r_m))^(eis t), C_0 from the present-value budget.
    The household is calibrated so that with unchanged prices it demands exactly the base basket
    (``alpha_i`` = base expenditure shares, ``Y`` = base expenditure, ``W`` = declared wealth).

Firms (intermediate demand)
    CES production of a fixed output from energy ``E`` and a value-added composite ``V`` with
    substitution elasticity ``sigma_f`` and base energy cost share ``a_E``:
        P_f,t = (a_E p_E,t^(1-sigma_f) + (1-a_E))^(1/(1-sigma_f))
        E_t / E_0 = (p_E,t / P_f,t)^(-sigma_f)
    Only the energy input is modelled (MVP); other intermediate inputs are unchanged.

Aggregation and frictions
    Total base demand is split into a household part and a firm part with the declared
    ``firm_share_by_group`` (assumption); per-group demand is the sum of both parts.
    Slow adjustment ("habits/contracts") is an explicit assumption: the deviation from the base is
    scaled by ``lambda_m``, which equals ``short_run_fraction`` in month 1 and reaches 1 in
    ``ramp_months``.  This replaces the reduced-form short->long ramp with a behavioural
    long-run anchor and a declared frictional path.

Determinism: pure ``math``, sorted iteration, rounded outputs; no random numbers and no clocks.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Sequence

STATUS = "RESEARCH_ONLY"
SCHEMA = "amfe.world.demand_behavior.v1"
ADMISSION = False
#: The optional integration flag: the default keeps the existing reduced-form numbers unchanged.
DEFAULT_MODE = "reduced_form"
MODE_REDUCED_FORM = "reduced_form"
MODE_BEHAVIORAL = "behavioral"
MODES = (MODE_REDUCED_FORM, MODE_BEHAVIORAL)

#: Demand groups of the quantity channel (same buckets as ``amfe.markets.demand``).
GROUPS = ("energy", "transport", "manufacturing", "services")

#: Option A: declared assumption.  Share of the base demand of a group that is firm/intermediate
#: demand rather than household final demand.  These are order-of-magnitude guesses, not estimates.
FIRM_SHARE_BY_GROUP: dict[str, float] = {
    "energy": 0.55,
    "transport": 0.45,
    "manufacturing": 0.50,
    "services": 0.20,
}

#: Assumption provenance classes used by the shared contract (docs/144).
SOURCE_CLASSES = ("observed", "derived", "assumption")

#: Fail-closed bounds of the model's own parameters.
SIGMA_BOUNDS = (1e-6, 3.0)
EIS_BOUNDS = (1e-6, 10.0)
MAX_RELATIVE_DEMAND_CHANGE = 0.5
PRICE_BOUNDS = (-0.99, 5.0)


class DemandBehaviorError(ValueError):
    """Base error: the behavioural model refuses to produce a number."""


class BehaviorConfigError(DemandBehaviorError):
    """A behavioural parameter or a mode is outside its declared bounds (fail-closed)."""


class BehaviorBoundsError(DemandBehaviorError):
    """An input or a resulting quantity falls outside the declared bounds."""


@dataclass(frozen=True)
class BehavioralDemandConfig:
    """Declared parameters of the behavioural demand MVP (all are assumptions, not estimates).

    Parameters
    ----------
    sigma_household : float
        CES substitution elasticity between demand groups in the household basket.
    sigma_firm : float
        CES substitution elasticity between energy and value added at the firm level.
    eis : float
        Intertemporal elasticity of substitution of the CRRA household.
    beta_monthly : float | None
        Monthly discount factor; ``None`` sets ``beta = 1/(1+r_m)`` so that the deterministic
        baseline path is flat (documented convention).
    real_interest_rate_annual : float
        Annual real interest rate used for the present-value budget (annual, converted monthly).
    horizon_months : int
        Number of months reported (1..N).
    ramp_months : int | None
        Adjustment horizon of the frictional path; ``None`` means ``horizon_months``.
    short_run_fraction : float
        Fraction of the behavioural long-run deviation realised in month 1 (0..1).
    shock_duration_months : int | None
        Months during which the shock persists; ``None`` means the whole horizon (permanent).
    energy_cost_share : float
        Energy cost share in the firm bundle at the base point (0..0.9).
    firm_share_by_group : Mapping[str, float]
        Declared split of base demand into firm/intermediate vs household demand.
    initial_nominal_wealth : float
        Initial nominal wealth in units of one month of household nominal income (default 0).
    max_relative_demand_change : float
        Fail-closed bound on the aggregate relative demand change (0, 1].
    price_bounds : tuple[float, float]
        Allowed relative price change interval (lower > -1, upper finite).
    """

    sigma_household: float = 0.80
    sigma_firm: float = 0.35
    eis: float = 0.50
    beta_monthly: float | None = None
    real_interest_rate_annual: float = 0.025
    horizon_months: int = 6
    ramp_months: int | None = None
    short_run_fraction: float = 0.28
    shock_duration_months: int | None = None
    energy_cost_share: float = 0.05
    firm_share_by_group: Mapping[str, float] = None  # type: ignore[assignment]
    initial_nominal_wealth: float = 0.0
    max_relative_demand_change: float = MAX_RELATIVE_DEMAND_CHANGE
    price_bounds: tuple[float, float] = PRICE_BOUNDS

    def __post_init__(self) -> None:
        shares = dict(self.firm_share_by_group if self.firm_share_by_group is not None
                      else FIRM_SHARE_BY_GROUP)
        unknown = sorted(set(shares) - set(GROUPS))
        if unknown:
            raise BehaviorConfigError(f"unknown firm-share groups: {unknown}")
        missing = sorted(set(GROUPS) - set(shares))
        if missing:
            raise BehaviorConfigError(f"firm_share_by_group misses groups: {missing}")
        for group, value in shares.items():
            if not _finite(value) or not 0.0 <= value <= 1.0:
                raise BehaviorConfigError(f"firm share {group}={value!r} outside [0, 1]")
        for name, value, bounds in (
            ("sigma_household", self.sigma_household, SIGMA_BOUNDS),
            ("sigma_firm", self.sigma_firm, SIGMA_BOUNDS),
            ("eis", self.eis, EIS_BOUNDS),
        ):
            if not _finite(value) or not bounds[0] <= value <= bounds[1]:
                raise BehaviorConfigError(f"{name}={value!r} outside {bounds}")
        if not _finite(self.real_interest_rate_annual) or not -0.99 < self.real_interest_rate_annual <= 1.0:
            raise BehaviorConfigError("real_interest_rate_annual must be in (-0.99, 1.0]")
        if not isinstance(self.horizon_months, int) or self.horizon_months < 1:
            raise BehaviorConfigError("horizon_months must be a positive integer")
        if self.ramp_months is not None and (not isinstance(self.ramp_months, int)
                                             or self.ramp_months < 1):
            raise BehaviorConfigError("ramp_months must be a positive integer or None")
        if not _finite(self.short_run_fraction) or not 0.0 <= self.short_run_fraction <= 1.0:
            raise BehaviorConfigError("short_run_fraction must be in [0, 1]")
        if self.shock_duration_months is not None and (
                not isinstance(self.shock_duration_months, int)
                or not 1 <= self.shock_duration_months <= self.horizon_months):
            raise BehaviorConfigError("shock_duration_months must be an integer in [1, horizon_months]")
        if not _finite(self.energy_cost_share) or not 0.0 <= self.energy_cost_share <= 0.9:
            raise BehaviorConfigError("energy_cost_share must be in [0, 0.9]")
        if self.beta_monthly is not None and (not _finite(self.beta_monthly)
                                              or not 0.0 < self.beta_monthly <= 1.0):
            raise BehaviorConfigError("beta_monthly must be in (0, 1] or None")
        if not _finite(self.initial_nominal_wealth) or self.initial_nominal_wealth < 0.0:
            raise BehaviorConfigError("initial_nominal_wealth must be non-negative")
        if not _finite(self.max_relative_demand_change) or not 0.0 < self.max_relative_demand_change <= 1.0:
            raise BehaviorConfigError("max_relative_demand_change must be in (0, 1]")
        low, high = self.price_bounds
        if not (_finite(low) and _finite(high) and -1.0 < low < high):
            raise BehaviorConfigError("price_bounds must satisfy -1 < low < high < inf")
        object.__setattr__(self, "firm_share_by_group", shares)
        object.__setattr__(self, "ramp_months",
                           self.horizon_months if self.ramp_months is None else self.ramp_months)
        object.__setattr__(self, "shock_duration_months",
                           self.horizon_months if self.shock_duration_months is None
                           else self.shock_duration_months)
        if self.beta_monthly is None:
            monthly_rate = self.monthly_real_rate
            object.__setattr__(self, "beta_monthly", 1.0 / (1.0 + monthly_rate))

    @property
    def monthly_real_rate(self) -> float:
        return (1.0 + self.real_interest_rate_annual) ** (1.0 / 12.0) - 1.0

    @property
    def effective_euler_growth(self) -> float:
        """Baseline gross consumption growth ``(beta (1+r_m))^eis`` (1.0 with the default beta)."""
        return (self.beta_monthly * (1.0 + self.monthly_real_rate)) ** self.eis

    def firm_share(self, group: str) -> float:
        return float(self.firm_share_by_group[group])


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def default_config(**overrides) -> BehavioralDemandConfig:
    """Return the declared default configuration (optionally with overrides)."""
    return BehavioralDemandConfig(**overrides)


def validate_prices(prices: Sequence[float], config: BehavioralDemandConfig | None = None) -> list[float]:
    """Validate relative price changes; fail-closed."""
    cfg = config or BehavioralDemandConfig()
    values = [float(value) for value in prices]
    if not values:
        raise DemandBehaviorError("price_changes must not be empty")
    for index, value in enumerate(values):
        if not _finite(value):
            raise DemandBehaviorError(f"sector {index}: price change is not finite")
        low, high = cfg.price_bounds
        if not low < value <= high:
            raise BehaviorBoundsError(f"sector {index}: price change {value} outside ({low}, {high}]")
    return values


def validate_base(base_quantities: Sequence[float]) -> list[float]:
    """Validate base quantities (non-negative, finite, positive total); fail-closed."""
    values = [float(value) for value in base_quantities]
    if not values:
        raise DemandBehaviorError("base_quantities must not be empty")
    for index, value in enumerate(values):
        if not _finite(value):
            raise DemandBehaviorError(f"sector {index}: base quantity is not finite")
        if value < 0.0:
            raise BehaviorBoundsError(f"sector {index}: base quantity {value} is negative")
    if sum(values) <= 0.0:
        raise BehaviorBoundsError("base quantities sum to zero")
    return values


def validate_groups(groups: Sequence[str]) -> list[str]:
    values = [str(group) for group in groups]
    unknown = sorted(set(values) - set(GROUPS))
    if unknown:
        raise DemandBehaviorError(f"unknown demand groups: {unknown}; known: {list(GROUPS)}")
    return values


def _validate_inputs(price_changes, base_quantities, groups, config):
    price = validate_prices(price_changes, config)
    base = validate_base(base_quantities)
    labels = validate_groups(groups)
    if not (len(price) == len(base) == len(labels)):
        raise DemandBehaviorError(f"price/base/groups lengths differ: "
                                  f"{len(price)}/{len(base)}/{len(labels)}")
    return price, base, labels


# ----------------------------------------------------------------------------------------------
# CES / CRRA primitives (exact, deterministic)
# ----------------------------------------------------------------------------------------------
def ces_price_index(prices: Sequence[float], weights: Sequence[float], sigma: float) -> float:
    """CES price index ``(sum w_i p_i^(1-sigma))^(1/(1-sigma))``; Cobb-Douglas limit at sigma=1."""
    p = [float(value) for value in prices]
    w = [float(value) for value in weights]
    if len(p) != len(w) or not p:
        raise DemandBehaviorError("CES price index needs aligned non-empty prices/weights")
    if any(not _finite(value) or value <= 0.0 for value in p):
        raise BehaviorBoundsError("CES price index needs strictly positive prices")
    total = sum(w)
    if total <= 0.0:
        raise BehaviorBoundsError("CES price index needs positive weights")
    weights = [value / total for value in w]
    if abs(float(sigma) - 1.0) < 1e-12:
        return math.exp(sum(wi * math.log(pi) for wi, pi in zip(weights, p)))
    power = 1.0 - float(sigma)
    inner = sum(wi * pi ** power for wi, pi in zip(weights, p))
    return inner ** (1.0 / power)


def ces_conditional_demand(price: float, bundle_price: float, sigma: float,
                           base_quantity: float) -> float:
    """Energy input calibrated to ``base_quantity`` at a unit bundle price: ``E/E0=(p/Pb)^-sigma``."""
    if price <= 0.0 or bundle_price <= 0.0:
        raise BehaviorBoundsError("CES demand needs strictly positive prices")
    if base_quantity < 0.0 or not _finite(base_quantity):
        raise BehaviorBoundsError("base quantity must be finite and non-negative")
    return float(base_quantity) * (float(price) / float(bundle_price)) ** (-float(sigma))


def household_demand_at_prices(prices: Sequence[float], alphas: Sequence[float],
                               real_consumption: float, sigma: float) -> list[float]:
    """CES household demands ``q_i = alpha_i (p_i/P)^-sigma C`` (exact)."""
    index = ces_price_index(prices, alphas, sigma)
    return [float(alpha) * (float(price) / index) ** (-float(sigma)) * float(real_consumption)
            for alpha, price in zip(alphas, prices)]


# ----------------------------------------------------------------------------------------------
# Implied long-run elasticities (analytic anchors, used only for diagnostics)
# ----------------------------------------------------------------------------------------------
def implied_long_run_elasticities(base_quantities: Sequence[float], groups: Sequence[str],
                                  config: BehavioralDemandConfig | None = None) -> dict:
    """Analytic long-run own-price elasticities implied by the model parameters.

    Household (fixed nominal expenditure): ``eps_h = -sigma_h(1 - s_i) - s_i``.
    Firm energy (fixed output): ``eps_f = -sigma_f(1 - energy_cost_share)``; other firm inputs are
    not modelled, so their aggregate elasticity is the household one.  Duplicate group labels (one
    row per sector, as the quantity bridge passes them) are aggregated by group.
    """
    cfg = config or BehavioralDemandConfig()
    base = validate_base(base_quantities)
    labels = validate_groups(groups)
    if len(base) != len(labels):
        raise DemandBehaviorError("base_quantities and groups lengths differ")
    totals: dict[str, float] = {}
    household_totals: dict[str, float] = {}
    for value, group in zip(base, labels):
        totals[group] = totals.get(group, 0.0) + value
        household_totals[group] = household_totals.get(group, 0.0) + value * (1.0 - cfg.firm_share(group))
    household_total = sum(household_totals.values())
    if household_total <= 0.0:
        raise BehaviorBoundsError("household share of the base demand is zero")
    table = {}
    for group in GROUPS:
        if group not in totals:
            continue
        group_base = totals[group]
        share = household_totals[group] / household_total
        household = -cfg.sigma_household * (1.0 - share) - share
        if group == "energy":
            firm_elasticity = -cfg.sigma_firm * (1.0 - cfg.energy_cost_share)
            firm_share_base = cfg.firm_share(group)
            aggregate = (1.0 - firm_share_base) * household + firm_share_base * firm_elasticity
        else:
            firm_elasticity = 0.0
            aggregate = (1.0 - cfg.firm_share(group)) * household
        table[group] = {
            "household": round(household, 9),
            "firm": round(firm_elasticity, 9),
            "aggregate_long_run": round(aggregate, 9),
            "base_quantity": round(group_base, 6),
        }
    return table


# ----------------------------------------------------------------------------------------------
# Main behavioural response
# ----------------------------------------------------------------------------------------------
def behavioral_demand_response(
    price_changes: Sequence[float],
    base_quantities: Sequence[float],
    groups: Sequence[str],
    *,
    config: BehavioralDemandConfig | None = None,
    horizon_months: int | None = None,
    ramp_months: int | None = None,
    max_relative_change: float | None = None,
) -> dict:
    """Behavioural demand response to relative price changes (deterministic, RESEARCH_ONLY).

    Same input convention as :func:`amfe.markets.demand.demand_response`: ``price_changes[i]`` is the
    relative change of the price of sector ``i`` (0.05 = +5%), ``base_quantities[i]`` is its base
    demand and ``groups[i]`` its demand group.  The output carries the same ``by_sector`` /
    ``aggregate`` / ``monthly`` structure so the optional integration flag can swap the two.
    """
    cfg = config or BehavioralDemandConfig()
    if horizon_months is not None or ramp_months is not None or max_relative_change is not None:
        new_horizon = int(horizon_months) if horizon_months is not None else cfg.horizon_months
        overrides = {"horizon_months": new_horizon}
        if ramp_months is not None:
            overrides["ramp_months"] = int(ramp_months)
        elif config is None or cfg.ramp_months == cfg.horizon_months:
            # the default ramp follows the horizon (same convention as the reduced form)
            overrides["ramp_months"] = new_horizon
        if config is None:
            overrides["shock_duration_months"] = new_horizon  # default: permanent over the horizon
        else:
            overrides["shock_duration_months"] = min(int(cfg.shock_duration_months), new_horizon)
        if max_relative_change is not None:
            overrides["max_relative_demand_change"] = float(max_relative_change)
        cfg = replace(cfg, **overrides)
    price, base, labels = _validate_inputs(price_changes, base_quantities, groups, cfg)
    horizon = cfg.horizon_months
    ramp = int(cfg.ramp_months)
    duration = int(cfg.shock_duration_months)
    total_base = float(sum(base))
    original_price_base = [float(value) for value in price]

    # ---- declared split household / firm
    household_base = [value * (1.0 - cfg.firm_share(group)) for value, group in zip(base, labels)]
    firm_base = [value * cfg.firm_share(group) for value, group in zip(base, labels)]
    household_total = float(sum(household_base))
    if household_total <= 0.0:
        raise BehaviorBoundsError("household share of base demand is zero; behavioural households "
                                  "are not identified")
    alphas = [value / household_total for value in household_base]

    # ---- household intertemporal problem
    monthly_rate = cfg.monthly_real_rate
    beta = float(cfg.beta_monthly)
    euler_growth = (beta * (1.0 + monthly_rate)) ** cfg.eis
    discount = [(1.0 + monthly_rate) ** (-month) for month in range(horizon)]
    nominal_income = household_total  # one month of base household expenditure, constant nominal
    wealth = cfg.initial_nominal_wealth * nominal_income
    price_paths = []  # price paths by month, per group
    price_indices = []  # CES price index by month
    for month in range(horizon):
        shocked = month < duration
        path = [1.0 + value if shocked else 1.0 for value in original_price_base]
        price_paths.append(path)
        price_indices.append(ces_price_index(path, alphas, cfg.sigma_household))
    denominator = sum(price_indices[month] * euler_growth ** month * discount[month]
                      for month in range(horizon))
    present_value_income = nominal_income * sum(discount)
    consumption_0 = (wealth + present_value_income) / denominator
    consumption_path = [consumption_0 * euler_growth ** month for month in range(horizon)]
    household_demands: list[list[float]] = []
    for month in range(horizon):
        household_demands.append(household_demand_at_prices(
            price_paths[month], alphas, consumption_path[month], cfg.sigma_household))

    # ---- firm CES energy demand (other intermediate inputs unchanged in the MVP)
    energy_positions = [index for index, group in enumerate(labels) if group == "energy"]
    firm_energy_paths: dict[int, list[float]] = {}
    cost_share = float(cfg.energy_cost_share)
    for position in energy_positions:
        base_energy = firm_base[position]
        if base_energy <= 0.0:
            firm_energy_paths[position] = [0.0] * horizon
            continue
        path_energy = []
        for month in range(horizon):
            price_energy = price_paths[month][position]
            if abs(cfg.sigma_firm - 1.0) < 1e-12:
                bundle = price_energy ** cost_share
            else:
                power = 1.0 - cfg.sigma_firm
                bundle = (cost_share * price_energy ** power + (1.0 - cost_share)) ** (1.0 / power)
            path_energy.append(ces_conditional_demand(price_energy, bundle, cfg.sigma_firm,
                                                      base_energy))
        firm_energy_paths[position] = path_energy

    # ---- frictional adjustment: lambda at months 1..horizon
    def friction(month_index: int) -> float:
        month = month_index + 1  # 1-based month
        if ramp <= 1:
            return 1.0
        if month >= ramp:
            return 1.0
        return cfg.short_run_fraction + (1.0 - cfg.short_run_fraction) * (month - 1) / (ramp - 1)

    monthly = []
    actual: list[list[float]] = []
    for month in range(horizon):
        lam = friction(month)
        row = []
        for index, group in enumerate(labels):
            household_part = household_base[index] * (
                (price_paths[month][index] / price_indices[month]) ** (-cfg.sigma_household)
                * consumption_path[month] / nominal_income)
            firm_part = firm_energy_paths[index][month] if index in firm_energy_paths else firm_base[index]
            model_value = household_part + firm_part
            row.append(base[index] + lam * (model_value - base[index]))
        actual.append(row)
        delta = sum(row[index] - base[index] for index in range(len(labels)))
        monthly.append({"month": month + 1,
                        "delta_demand": round(delta, 6),
                        "relative": round(delta / total_base, 9)})

    by_sector = []
    for index, group in enumerate(labels):
        short = actual[0][index]
        long = actual[-1][index]
        delta_short = short - base[index]
        delta_long = long - base[index]
        own_price = original_price_base[index]
        by_sector.append({
            "index": index,
            "group": group,
            "price_change": round(own_price, 9),
            "base_quantity": round(base[index], 6),
            "household_base": round(household_base[index], 6),
            "firm_base": round(firm_base[index], 6),
            "delta_short": round(delta_short, 6),
            "delta_long": round(delta_long, 6),
            "relative_short": round(delta_short / base[index], 9) if base[index] else 0.0,
            "relative_long": round(delta_long / base[index], 9) if base[index] else 0.0,
            "implied_short_elasticity": round(delta_short / base[index] / own_price, 9)
            if base[index] and abs(own_price) > 1e-12 else None,
            "implied_long_elasticity": round(delta_long / base[index] / own_price, 9)
            if base[index] and abs(own_price) > 1e-12 else None,
        })

    delta_short_total = sum(item["delta_short"] for item in by_sector)
    delta_long_total = sum(item["delta_long"] for item in by_sector)
    relative_short = delta_short_total / total_base
    relative_long = delta_long_total / total_base
    if abs(relative_short) > cfg.max_relative_demand_change or \
            abs(relative_long) > cfg.max_relative_demand_change:
        raise BehaviorBoundsError(f"aggregate demand change {relative_short:.4f}/{relative_long:.4f} "
                                  f"exceeds the declared bound +/-{cfg.max_relative_demand_change}")
    if any(value < -1e-9 * max(1.0, base[index])
           for row in actual for index, value in enumerate(row)):
        raise BehaviorBoundsError("a sector demand becomes negative after the shock")

    implied = implied_long_run_elasticities(base, labels, cfg)
    household_budget_error = max(
        abs(sum(price_paths[month][index] * household_demands[month][index]
                for index in range(len(labels))) - price_indices[month] * consumption_path[month])
        for month in range(horizon))
    intertemporal_error = abs(
        sum(price_indices[month] * consumption_path[month] * discount[month]
            for month in range(horizon)) - (wealth + present_value_income))
    zero_shock = max(abs(value) for value in original_price_base) <= 1e-12
    energy_rises = [index for index, group in enumerate(labels)
                    if group == "energy" and original_price_base[index] > 1e-12]
    energy_sign_ok = all(by_sector[index]["relative_long"] <= 1e-12 for index in energy_rises)
    permanent = duration >= horizon
    monotone = None
    if permanent:
        monotone = all(abs(actual[-1][index] - base[index]) + 1e-12 >= abs(actual[0][index] - base[index])
                       for index in range(len(labels)))

    output = {
        "schema": SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "model": "behavioural_demand_mvp",
        "horizon_months": horizon,
        "ramp_months": ramp,
        "shock_duration_months": duration,
        "config": {key: value for key, value in asdict(cfg).items()},
        "assumption_parameters": {
            "sigma_household": cfg.sigma_household,
            "sigma_firm": cfg.sigma_firm,
            "eis": cfg.eis,
            "beta_monthly": round(float(cfg.beta_monthly), 9),
            "real_interest_rate_annual": cfg.real_interest_rate_annual,
            "short_run_fraction": cfg.short_run_fraction,
            "energy_cost_share": cfg.energy_cost_share,
            "firm_share_by_group": dict(cfg.firm_share_by_group),
            "initial_nominal_wealth": cfg.initial_nominal_wealth,
        },
        "aggregate": {
            "base_quantity": round(total_base, 6),
            "delta_short": round(delta_short_total, 6),
            "delta_long": round(delta_long_total, 6),
            "relative_short": round(relative_short, 9),
            "relative_long": round(relative_long, 9),
            "household_share_of_base": round(household_total / total_base, 9),
        },
        "monthly": monthly,
        "by_sector": by_sector,
        "implied_elasticities": implied,
        "decomposition": {
            "household": {
                "nominal_income": round(nominal_income, 6),
                "initial_wealth": round(wealth, 6),
                "real_consumption_first_month": round(consumption_path[0], 6),
                "real_consumption_last_month": round(consumption_path[-1], 6),
                "euler_gross_growth": round(euler_growth, 9),
                "relative_consumption_change": round(consumption_path[0] / nominal_income - 1.0, 9),
            },
            "firm": {
                "modelled_input": "energy",
                "energy_cost_share": cost_share,
                "firm_share_by_group": dict(cfg.firm_share_by_group),
                "output_held_fixed": True,
            },
        },
        "checks": {
            "household_within_period_budget": household_budget_error <= 1e-9,
            "household_intertemporal_budget": intertemporal_error <= 1e-9,
            "quantities_non_negative": all(value >= -1e-9 * max(1.0, base[index])
                                           for row in actual for index, value in enumerate(row)),
            "zero_shock_zero_change": (max(abs(item["relative_long"]) for item in by_sector) <= 1e-12
                                       and max(abs(item["relative_short"]) for item in by_sector) <= 1e-12)
            if zero_shock else None,
            "energy_sign_consistent": bool(energy_sign_ok),
            "ramp_monotone_permanent": monotone,
            "deterministic": True,
            "research_only": True,
            "admission": ADMISSION,
            "budget_error_max": round(max(household_budget_error, intertemporal_error), 12),
        },
        "assumptions": [
            {"name": "sigma_household", "source_class": "assumption",
             "note": "CES substitution между группами в корзине домохозяйства; выбрана так, чтобы "
                     "долгосрочная эластичность энергии была близка к допущению reduced-form "
                     "(-0.55) при демонстрационных базах — калибровочный якорь, НЕ оценка",
             "source": "docs/167; docs/159"},
            {"name": "sigma_firm", "source_class": "assumption",
             "note": "CES замещение энергия/добавленная стоимость у фирм; долгосрочная эластичность "
                     "энергии фирмы = -sigma_f*(1-energy_cost_share)",
             "source": "docs/167"},
            {"name": "eis", "source_class": "assumption",
             "note": "межвременная эластичность CRRA; в MVP с постоянным номинальным доходом влияет "
                     "на профиль потребления, а не на масштаб (ограничение модели)",
             "source": "docs/167"},
            {"name": "firm_share_by_group", "source_class": "assumption",
             "note": "доля фирменного/промежуточного спроса в базе; порядок величины, не оценка",
             "source": "docs/167"},
            {"name": "energy_cost_share", "source_class": "assumption",
             "note": "доля энергии в затратах фирмы в базовой точке",
             "source": "docs/167"},
            {"name": "short_run_fraction/ramp", "source_class": "assumption",
             "note": "трение привычек/контрактов: доля долгосрочной реакции в 1-й месяц и ramp; "
                     "не выводится из полезности",
             "source": "docs/167"},
            {"name": "no_supply_response", "source_class": "assumption",
             "note": "цены заданы экзогенно, фирменный выпуск фиксирован, предложение/ожидания не "
                     "моделируются",
             "source": "docs/167"},
        ],
        "limitations": [
            "MVP и RESEARCH_ONLY: параметры — допущения, не оценки; G4-критерий (эластичности из "
            "данных) не выполнен",
            "частичное равновесие: цены и выпуск экзогенны, нет предложения и обратных связей",
            "фирменный канал моделирует только энергию; прочие промежуточные входы неизменны",
            "межвременной канал с постоянным номинальным доходом: EIS влияет на профиль, но не на "
            "масштаб агрегатной реакции (нужны богатство/ограничения заимствования)",
            "CRRA-домохозяйство без неопределённости и без ограничений ликвидности",
            "сравнение с reduced-form — только диагностика непрерывности интеграции, не валидация",
        ],
        "integration": {
            "default_mode": DEFAULT_MODE,
            "optional_mode": MODE_BEHAVIORAL,
            "dispatch": "amfe.world.demand_behavior.demand_dispatch",
            "note": "режим включается флагом; по умолчанию числа reduced-form не меняются",
        },
    }
    return output


# ----------------------------------------------------------------------------------------------
# Optional integration flag: the default path must reproduce the reduced-form numbers exactly
# ----------------------------------------------------------------------------------------------
def demand_dispatch(
    price_changes: Sequence[float],
    base_quantities: Sequence[float],
    groups: Sequence[str],
    *,
    mode: str = DEFAULT_MODE,
    elasticities: Mapping | None = None,
    config: BehavioralDemandConfig | None = None,
    horizon_months: int = 6,
    ramp_months: int | None = None,
    max_relative_change: float = MAX_RELATIVE_DEMAND_CHANGE,
) -> dict:
    """Optional integration flag: ``reduced_form`` (default, unchanged) or ``behavioral``.

    ``mode="reduced_form"`` calls :func:`amfe.markets.demand.demand_response` with the same arguments
    and returns its result **unchanged**, so switching the flag off cannot alter any existing number
    or report.  ``mode="behavioral"`` returns the behavioural MVP output of
    :func:`behavioral_demand_response`.  There is no other mode (fail-closed).
    """
    if mode == MODE_REDUCED_FORM:
        from amfe.markets import demand as reduced_form_demand  # lazy: keep the default path decoupled

        return reduced_form_demand.demand_response(
            price_changes, base_quantities, groups, elasticities=elasticities,
            horizon_months=horizon_months, ramp_months=ramp_months,
            max_relative_change=max_relative_change)
    if mode == MODE_BEHAVIORAL:
        return behavioral_demand_response(
            price_changes, base_quantities, groups, config=config,
            horizon_months=horizon_months, ramp_months=ramp_months,
            max_relative_change=max_relative_change)
    raise BehaviorConfigError(f"unknown demand mode {mode!r}; known: {list(MODES)}")


def compare_to_reduced_form(
    price_changes: Sequence[float],
    base_quantities: Sequence[float],
    groups: Sequence[str],
    *,
    config: BehavioralDemandConfig | None = None,
    elasticities: Mapping | None = None,
    horizon_months: int = 6,
    ramp_months: int | None = None,
) -> dict:
    """Diagnostic only: behavioural vs reduced-form response on the same inputs.

    This is **not validation** and not a claim that either model is right; it exists to document how
    far the optional behavioural mode deviates from the reduced-form channel it can replace.
    """
    behavioural = behavioral_demand_response(
        price_changes, base_quantities, groups, config=config,
        horizon_months=horizon_months, ramp_months=ramp_months)
    reduced = demand_dispatch(price_changes, base_quantities, groups, mode=MODE_REDUCED_FORM,
                              elasticities=elasticities, horizon_months=horizon_months,
                              ramp_months=ramp_months)
    aggregate = {
        key: {
            "behavioural": behavioural["aggregate"][key],
            "reduced_form": reduced["aggregate"][key],
            "difference": round(behavioural["aggregate"][key] - reduced["aggregate"][key], 9),
        }
        for key in ("relative_short", "relative_long", "delta_short", "delta_long")
    }
    by_group = []
    for index, (behaviour_item, reduced_item) in enumerate(
            zip(behavioural["by_sector"], reduced["by_sector"])):
        by_group.append({
            "index": index,
            "group": behaviour_item["group"],
            "relative_short_diff": round(behaviour_item["relative_short"]
                                         - reduced_item["relative_short"], 9),
            "relative_long_diff": round(behaviour_item["relative_long"]
                                        - reduced_item["relative_long"], 9),
        })
    return {
        "schema": "amfe.world.demand_behavior.compare.v1",
        "status": STATUS,
        "diagnostic_only": True,
        "validation": False,
        "note": "diagnostic comparison for integration continuity; NOT validation and not a claim "
                "that the behavioural model is more accurate",
        "aggregate": aggregate,
        "by_group": by_group,
        "behavioural_status": behavioural["status"],
        "reduced_form_status": reduced["status"],
    }
