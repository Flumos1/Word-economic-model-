"""Configuration objects and validation for the integrated world core (stages 2-5).

The world core wires four independent prototype layers:

* :mod:`amfe.accounts` - the typed single-country sector accounting kernel (stage 2);
* :mod:`amfe.international` - two countries, two currencies, BoP/IIP (stage 3);
* :mod:`amfe.resources` - physical IO/energy/labour limits (stage 4);
* :mod:`amfe.dynamics` - regimes, expectations, policy and debt identities (stage 5).

All configuration is explicit, immutable and validated up front so that a run cannot fail
half-way because of a misspelled sector or an inconsistent share vector.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Tuple


class WorldConfigError(ValueError):
    """Invalid world-core configuration."""


def _finite(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise WorldConfigError(f"{name} must be finite, got {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise WorldConfigError(f"{name} must be non-negative, got {value!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise WorldConfigError(f"{name} must be positive, got {value!r}")
    return number


def _share(value: object, name: str) -> float:
    number = _finite(value, name)
    if not 0.0 <= number <= 1.0:
        raise WorldConfigError(f"{name} must lie in [0, 1], got {value!r}")
    return number


@dataclass(frozen=True)
class PhysicalConfig:
    """One country's physical layer: a productive Leontief economy with energy and labour."""

    sectors: Tuple[str, ...] = ("goods", "services")
    technical_matrix: Tuple[Tuple[float, ...], ...] = ((0.2, 0.3), (0.1, 0.4))
    base_final_demand: Tuple[float, ...] = (10.0, 20.0)
    energy_intensities: Mapping[str, float] = field(
        default_factory=lambda: {"goods": 2.0, "services": 1.0})
    energy_available: float = 100.0
    energy_resource_id: str = "energy"
    energy_unit: str = "MWh"
    labour_variant: str = "B1"
    hours_per_unit: Mapping[str, float] = field(
        default_factory=lambda: {"goods": 1.0, "services": 2.0})
    accessible_hours: float = 120.0
    task_hours: Mapping[str, Mapping[str, float]] | None = None
    task_skill: Mapping[str, str] | None = None
    skill_accessible: Mapping[str, float] | None = None
    #: share of every sector's final demand that is household consumption
    household_demand_share: float = 0.63
    #: share of every sector's final demand that is government consumption
    government_demand_share: float = 0.20
    #: share of every sector's final demand that is investment (inventory formation)
    investment_demand_share: float = 0.17

    def __post_init__(self) -> None:
        sectors = tuple(str(sector) for sector in self.sectors)
        if not sectors or len(set(sectors)) != len(sectors):
            raise WorldConfigError("sectors must be a non-empty sequence of unique names")
        object.__setattr__(self, "sectors", sectors)
        matrix = tuple(tuple(float(value) for value in row) for row in self.technical_matrix)
        if len(matrix) != len(sectors) or any(len(row) != len(sectors) for row in matrix):
            raise WorldConfigError("technical_matrix must be square and match sectors")
        for row in matrix:
            for value in row:
                if not math.isfinite(value) or value < 0.0:
                    raise WorldConfigError(
                        "technical_matrix coefficients must be finite and non-negative")
        object.__setattr__(self, "technical_matrix", matrix)
        demand = tuple(float(value) for value in self.base_final_demand)
        if len(demand) != len(sectors):
            raise WorldConfigError("base_final_demand must have one value per sector")
        for value in demand:
            _non_negative(value, "base_final_demand")
        object.__setattr__(self, "base_final_demand", demand)
        intensities = {str(key): float(value)
                       for key, value in dict(self.energy_intensities).items()}
        if set(intensities) != set(sectors):
            raise WorldConfigError(
                "energy_intensities must cover exactly the IO sectors")
        for value in intensities.values():
            _non_negative(value, "energy_intensities")
        object.__setattr__(self, "energy_intensities", intensities)
        object.__setattr__(self, "energy_available",
                           _non_negative(self.energy_available, "energy_available"))
        if not isinstance(self.energy_resource_id, str) or not self.energy_resource_id.strip():
            raise WorldConfigError("energy_resource_id must be a non-empty string")
        if not isinstance(self.energy_unit, str) or not self.energy_unit.strip():
            raise WorldConfigError("energy_unit must be a non-empty string")
        variant = str(self.labour_variant).strip().upper()
        if variant not in ("B0", "B1", "B2"):
            raise WorldConfigError("labour_variant must be one of B0, B1, B2")
        object.__setattr__(self, "labour_variant", variant)
        hours = {str(key): float(value)
                 for key, value in dict(self.hours_per_unit).items()}
        if set(hours) != set(sectors):
            raise WorldConfigError("hours_per_unit must cover exactly the IO sectors")
        for value in hours.values():
            _non_negative(value, "hours_per_unit")
        object.__setattr__(self, "hours_per_unit", hours)
        object.__setattr__(self, "accessible_hours",
                           _non_negative(self.accessible_hours, "accessible_hours"))
        if variant == "B2":
            if not self.task_hours or not self.skill_accessible:
                raise WorldConfigError(
                    "labour_variant B2 requires task_hours and skill_accessible")
            tasks = {str(sector): {str(task): float(value) for task, value in dict(hours).items()}
                     for sector, hours in dict(self.task_hours).items()}
            if set(tasks) != set(sectors):
                raise WorldConfigError("task_hours must cover exactly the IO sectors")
            object.__setattr__(self, "task_hours", tasks)
            skills = {str(skill): float(value)
                      for skill, value in dict(self.skill_accessible).items()}
            if not skills:
                raise WorldConfigError("skill_accessible must not be empty")
            object.__setattr__(self, "skill_accessible", skills)
            mapping = ({str(task): str(skill)
                        for task, skill in dict(self.task_skill or {}).items()}
                       if self.task_skill is not None else {})
            object.__setattr__(self, "task_skill", mapping)
        shares = (float(self.household_demand_share),
                  float(self.government_demand_share),
                  float(self.investment_demand_share))
        for value, label in zip(shares, ("household", "government", "investment")):
            _share(value, f"{label}_demand_share")
            if value < 0.0:
                raise WorldConfigError(f"{label}_demand_share must be non-negative")
        if abs(sum(shares) - 1.0) > 1e-12:
            raise WorldConfigError(
                "household/government/investment demand shares must sum to one, "
                f"got {sum(shares)}")
        object.__setattr__(self, "household_demand_share", shares[0])
        object.__setattr__(self, "government_demand_share", shares[1])
        object.__setattr__(self, "investment_demand_share", shares[2])


@dataclass(frozen=True)
class CountryConfig:
    """One country: physical layer, initial stocks, fiscal/pricing rules and trade share."""

    name: str
    currency: str
    physical: PhysicalConfig = field(default_factory=PhysicalConfig)
    initial_fx_rate: float = 1.0
    initial_price_level: float = 1.0
    inflation_target: float = 0.5          # percent per period
    inflation_slope: float = 0.25          # percentage points per unit output gap
    wage_share: float = 0.60
    transfer_rate: float = 0.08
    tax_min: float = 0.05
    tax_max: float = 0.60
    debt_target: float = 0.55
    debt_response: float = 0.10
    initial_debt_ratio: float = 0.50
    working_loan_ratio: float = 0.20
    export_share: float = 0.08
    real_growth: float = 0.004             # per period, for the potential-output trend
    policy_r_star: float = 0.10
    policy_phi_pi: float = 1.5
    policy_phi_y: float = 0.5
    expectations_gain: float = 0.30

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise WorldConfigError("country name must be a non-empty string")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise WorldConfigError("country currency must be a non-empty string")
        if not isinstance(self.physical, PhysicalConfig):
            raise WorldConfigError("physical must be a PhysicalConfig")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "currency", self.currency.strip())
        object.__setattr__(self, "initial_fx_rate",
                           _positive(self.initial_fx_rate, "initial_fx_rate"))
        object.__setattr__(self, "initial_price_level",
                           _positive(self.initial_price_level, "initial_price_level"))
        _share(self.wage_share, "wage_share")
        for name, value in (
            ("inflation_slope", self.inflation_slope),
            ("wage_share", self.wage_share),
            ("transfer_rate", self.transfer_rate),
            ("tax_min", self.tax_min),
            ("tax_max", self.tax_max),
            ("debt_target", self.debt_target),
            ("debt_response", self.debt_response),
            ("initial_debt_ratio", self.initial_debt_ratio),
            ("working_loan_ratio", self.working_loan_ratio),
            ("export_share", self.export_share),
            ("real_growth", self.real_growth),
            ("policy_phi_pi", self.policy_phi_pi),
            ("policy_phi_y", self.policy_phi_y),
            ("expectations_gain", self.expectations_gain),
        ):
            _finite(value, name)
        if not 0.0 <= self.tax_min <= self.tax_max <= 1.0:
            raise WorldConfigError("tax_min/tax_max must satisfy 0 <= min <= max <= 1")
        if not 0.0 <= self.wage_share <= 1.0:
            raise WorldConfigError("wage_share must lie in [0, 1]")
        if not 0.0 <= self.transfer_rate <= 1.0:
            raise WorldConfigError("transfer_rate must lie in [0, 1]")
        if not 0.02 <= self.initial_price_level <= 1e6:
            raise WorldConfigError("initial_price_level is outside a sensible range")
        if self.inflation_target < -5.0 or self.inflation_target > 20.0:
            raise WorldConfigError("inflation_target must lie in [-5, 20] percent")
        if not 0.0 < self.policy_phi_pi:
            raise WorldConfigError("policy_phi_pi must be positive")
        if self.policy_phi_y < 0.0:
            raise WorldConfigError("policy_phi_y must be non-negative")
        if not 0.0 < self.expectations_gain <= 1.0:
            raise WorldConfigError("expectations_gain must lie in (0, 1]")
        object.__setattr__(self, "inflation_target", float(self.inflation_target))
        object.__setattr__(self, "inflation_slope", float(self.inflation_slope))
        object.__setattr__(self, "tax_min", float(self.tax_min))
        object.__setattr__(self, "tax_max", float(self.tax_max))
        object.__setattr__(self, "debt_target", float(self.debt_target))
        object.__setattr__(self, "debt_response", float(self.debt_response))
        object.__setattr__(self, "initial_debt_ratio", float(self.initial_debt_ratio))
        object.__setattr__(self, "working_loan_ratio", float(self.working_loan_ratio))
        object.__setattr__(self, "export_share", float(self.export_share))
        object.__setattr__(self, "real_growth", float(self.real_growth))
        object.__setattr__(self, "policy_r_star", float(self.policy_r_star))
        object.__setattr__(self, "policy_phi_pi", float(self.policy_phi_pi))
        object.__setattr__(self, "policy_phi_y", float(self.policy_phi_y))
        object.__setattr__(self, "expectations_gain", float(self.expectations_gain))


def default_country_a() -> CountryConfig:
    return CountryConfig(name="A", currency="AAA", initial_fx_rate=1.0)


def default_country_b() -> CountryConfig:
    return CountryConfig(name="B", currency="BBB", initial_fx_rate=2.0)


@dataclass(frozen=True)
class WorldConfig:
    """The two-country integrated world configuration."""

    country_a: CountryConfig = field(default_factory=default_country_a)
    country_b: CountryConfig = field(default_factory=default_country_b)
    #: common numeraire: one of the two currencies (the prototype keeps it in country A's unit)
    numeraire: str = "AAA"
    regime_seed: int = 20261112
    regime_contraction_factor: float = 0.93
    transition_matrix: Tuple[Tuple[float, ...], ...] = ((0.92, 0.08), (0.25, 0.75))
    regime_means: Tuple[float, ...] = (0.6, -0.4)
    regime_sigmas: Tuple[float, ...] = (0.6, 1.2)
    max_periods: int = 24

    def __post_init__(self) -> None:
        if not isinstance(self.country_a, CountryConfig) or not isinstance(
                self.country_b, CountryConfig):
            raise WorldConfigError("country_a/country_b must be CountryConfig instances")
        if self.country_a.name == self.country_b.name:
            raise WorldConfigError("country names must be distinct")
        if self.country_a.currency == self.country_b.currency:
            raise WorldConfigError("the two countries must use different currencies")
        if self.numeraire not in (self.country_a.currency, self.country_b.currency):
            raise WorldConfigError("numeraire must be one of the two country currencies")
        numeraire_country = (self.country_a if self.country_a.currency == self.numeraire
                             else self.country_b)
        if abs(numeraire_country.initial_fx_rate - 1.0) > 1e-12:
            raise WorldConfigError(
                "the country whose currency is the numeraire must have initial_fx_rate 1")
        if not isinstance(self.regime_seed, int) or isinstance(self.regime_seed, bool):
            raise WorldConfigError("regime_seed must be an integer")
        factor = float(self.regime_contraction_factor)
        if not 0.0 < factor <= 1.0:
            raise WorldConfigError("regime_contraction_factor must lie in (0, 1]")
        object.__setattr__(self, "regime_contraction_factor", factor)
        if (not 2 <= len(self.transition_matrix) == len(self.regime_means)
                == len(self.regime_sigmas)):
            raise WorldConfigError("regime matrices/means/sigmas must describe >= 2 regimes")
        if int(self.max_periods) < 1 or int(self.max_periods) > 240:
            raise WorldConfigError("max_periods must lie in [1, 240]")
        object.__setattr__(self, "max_periods", int(self.max_periods))

    @property
    def countries(self) -> Tuple[CountryConfig, CountryConfig]:
        return (self.country_a, self.country_b)

    def country(self, name: str) -> CountryConfig:
        for country in self.countries:
            if country.name == name:
                return country
        raise WorldConfigError(f"unknown country {name!r}")


__all__ = [
    "WorldConfigError", "PhysicalConfig", "CountryConfig", "WorldConfig",
    "default_country_a", "default_country_b",
]
