"""World-core scenarios: baseline, demand, energy and devaluation paths (deterministic).

A :class:`WorldScenario` is a set of *exogenous assumptions* (docs/38, I10: scenario
class): multiplicative demand/energy/labour factors, extra inflation and an FX path.
Scenarios are not causal estimates and do not gate any admission; they only produce
different physical/monetary paths for the same model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from .config import WorldConfigError


@dataclass(frozen=True)
class WorldScenario:
    """Exogenous assumptions of one world-core run."""

    name: str
    demand_factor: float = 1.0
    energy_factor: float = 1.0
    labour_factor: float = 1.0
    extra_inflation: Mapping[str, float] = field(default_factory=dict)
    #: first period at which ``extra_inflation`` applies (0 = every period)
    extra_inflation_start: int = 0
    #: period index -> currency-B rate against the numeraire (positive)
    fx_rate_path: Mapping[int, float] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise WorldConfigError("scenario name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        for name, value in (("demand_factor", self.demand_factor),
                            ("energy_factor", self.energy_factor),
                            ("labour_factor", self.labour_factor)):
            number = float(value)
            if not math.isfinite(number) or number < 0.0:
                raise WorldConfigError(f"{name} must be a finite non-negative multiplier")
            object.__setattr__(self, name, number)
        inflation = {str(country): float(value)
                     for country, value in dict(self.extra_inflation).items()}
        for country, value in inflation.items():
            if not math.isfinite(value):
                raise WorldConfigError(f"extra_inflation[{country}] must be finite")
        object.__setattr__(self, "extra_inflation", inflation)
        start = int(self.extra_inflation_start)
        if start < 0:
            raise WorldConfigError("extra_inflation_start must be non-negative")
        object.__setattr__(self, "extra_inflation_start", start)
        path = {}
        for period, value in dict(self.fx_rate_path).items():
            if not isinstance(period, int) or isinstance(period, bool) or period < 0:
                raise WorldConfigError("fx_rate_path keys must be non-negative period indices")
            number = float(value)
            if not math.isfinite(number) or number <= 0.0:
                raise WorldConfigError("fx_rate_path values must be finite and positive")
            path[int(period)] = number
        object.__setattr__(self, "fx_rate_path", path)
        object.__setattr__(self, "description", str(self.description))

    def factors_for(self, period: int, country: str) -> dict:
        return {
            "demand_factor": self.demand_factor,
            "energy_factor": self.energy_factor,
            "labour_factor": self.labour_factor,
            "extra_inflation": (self.extra_inflation.get(country, 0.0)
                                if period >= self.extra_inflation_start else 0.0),
            "fx_rate": self.fx_rate_path.get(period),
        }

    def describe(self) -> dict:
        return {
            "name": self.name,
            "demand_factor": self.demand_factor,
            "energy_factor": self.energy_factor,
            "labour_factor": self.labour_factor,
            "extra_inflation": dict(self.extra_inflation),
            "extra_inflation_start": self.extra_inflation_start,
            "fx_rate_path": {str(key): value for key, value in self.fx_rate_path.items()},
            "description": self.description,
        }


def baseline_scenario() -> WorldScenario:
    return WorldScenario(name="baseline",
                         description="Base path: full demand, full energy and labour, no FX shocks.")


def demand_shock_scenario(factor: float = 0.85) -> WorldScenario:
    return WorldScenario(
        name="demand_shock", demand_factor=factor,
        description=f"Exogenous final-demand multiplier {factor} in both countries.")


def energy_shock_scenario(factor: float = 0.70) -> WorldScenario:
    return WorldScenario(
        name="energy_shock", energy_factor=factor,
        description=f"Exogenous primary-energy availability factor {factor}.")


def labour_shock_scenario(factor: float = 0.75) -> WorldScenario:
    return WorldScenario(
        name="labour_shock", labour_factor=factor,
        description=f"Exogenous accessible-hours factor {factor}.")


def devaluation_scenario(*, period: int = 3, rate: float = 1.70,
                         extra_inflation_a: float = -0.30,
                         extra_inflation_b: float = 0.80) -> WorldScenario:
    """A one-off devaluation of country B's currency at ``period``.

    The FX path is a step: before ``period`` the rate equals the configured initial rate
    (handled by the caller default of 2.0 only when the path has no value yet), from
    ``period`` on the new rate applies.  The inflation pass-through is an explicit
    *assumption* (scenario), reported as such; it is not an estimated effect.
    """
    return WorldScenario(
        name="devaluation",
        fx_rate_path={int(period): float(rate)},
        extra_inflation={"A": float(extra_inflation_a), "B": float(extra_inflation_b)},
        extra_inflation_start=int(period),
        description=(f"One-off FX step at period {period}: B rate goes to {rate} "
                     "(numeraire units per BBB); asymmetric inflation pass-through "
                     "is an explicit scenario assumption."))


def standard_scenarios() -> list[WorldScenario]:
    """The four scenarios required by the integration task (no ranking, no admission)."""
    return [baseline_scenario(), demand_shock_scenario(),
            energy_shock_scenario(), devaluation_scenario()]


__all__ = [
    "WorldScenario", "baseline_scenario", "demand_shock_scenario",
    "energy_shock_scenario", "labour_shock_scenario", "devaluation_scenario",
    "standard_scenarios",
]
