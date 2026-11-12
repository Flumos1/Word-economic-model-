"""Combined resource scenarios: demand, energy and labour shocks on one IO model.

A scenario is a set of *multiplicative* assumptions on quantities, each of which can be
justified or refuted independently:

* ``demand_factors`` - final-demand multipliers by sector (a demand or supply-chain shock at
  the level of final uses);
* ``energy_factor`` - the share of the primary-resource availability that remains;
* ``labour_factor`` - the share of accessible hours that remains (illness, mobilisation,
  participation change).

The scenario does not pick a winner: it returns the feasible output, the uniform rationing
scale, the binding constraints and the physical balances, so a test or a report can compare
scenarios on their own parameters.  Uniform rationing is an explicit, simple rule; an
optimal allocation with substitution belongs to a later stage (docs/38 H, docs/42 stage 5).
"""
from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .energy import InputConstraint, ResourceLimitResult, apply_input_limit
from .io import LeontiefError, LeontiefModel
from .labour import LabourModel

TOLERANCE = 1e-9


class ScenarioError(ValueError):
    """Invalid scenario definition or inconsistent scenario output."""


def _factor(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ScenarioError(f"{name} must be a finite non-negative multiplier, got {value!r}")
    return number


@dataclass(frozen=True)
class Scenario:
    """Multiplicative assumptions applied to a baseline period."""

    name: str
    demand_factors: Mapping[str, float] = field(default_factory=dict)
    energy_factor: float = 1.0
    labour_factor: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ScenarioError("scenario name must be a non-empty string")
        object.__setattr__(self, "demand_factors", {
            str(sector): _factor(value, f"demand_factors[{sector}]")
            for sector, value in dict(self.demand_factors).items()
        })
        _factor(self.energy_factor, "energy_factor")
        _factor(self.labour_factor, "labour_factor")

    @classmethod
    def baseline(cls) -> "Scenario":
        return cls(name="baseline")

    def describe(self) -> dict:
        return {"name": self.name, "demand_factors": dict(self.demand_factors),
                "energy_factor": self.energy_factor, "labour_factor": self.labour_factor}


@dataclass
class ScenarioResult:
    """Physical outcome of one scenario (values are physical units, never money)."""

    scenario: str
    output: Dict[str, float]
    final_demand: Dict[str, float]
    scale: float
    binding: Tuple[str, ...]
    value_added: float
    energy: Optional[dict]
    labour: Optional[dict]
    material_residual: float

    def describe(self) -> dict:
        return {
            "scenario": self.scenario,
            "scale": round(self.scale, 12),
            "binding": list(self.binding),
            "value_added": round(self.value_added, 12),
            "energy": self.energy,
            "labour": self.labour,
            "material_residual": self.material_residual,
            "output": {sector: round(value, 12) for sector, value in self.output.items()},
        }


def run_scenario(model: LeontiefModel, base_final_demand: Sequence[float],
                 scenario: Scenario,
                 energy: Optional[InputConstraint] = None,
                 labour: Optional[LabourModel] = None,
                 tolerance: float = TOLERANCE) -> ScenarioResult:
    """Apply a scenario and return the feasible physical outcome.

    The model is solved first at full demand, then the most restrictive physical limit gives
    a uniform scale in ``[0, 1]``.  Linearity of the Leontief system makes the scaled output
    the exact solution for the scaled final demand, so the material balance stays satisfied
    after rationing; this is checked in the result, not assumed.
    """
    sectors = list(model.sector_ids)
    demand = [float(value) for value in base_final_demand]
    if len(demand) != model.size:
        raise ScenarioError(f"base_final_demand must have {model.size} entries")
    if any(not math.isfinite(value) or value < 0.0 for value in demand):
        raise ScenarioError("base_final_demand must be finite and non-negative")
    if energy is None and scenario.energy_factor != 1.0:
        raise ScenarioError("energy_factor is set but no energy constraint was supplied")
    if labour is None and scenario.labour_factor != 1.0:
        raise ScenarioError("labour_factor is set but no labour model was supplied")
    unknown = set(scenario.demand_factors) - set(sectors)
    if unknown:
        raise ScenarioError(f"demand factors for unknown sectors: {sorted(unknown)}")
    shocked_demand = [demand[i] * scenario.demand_factors.get(sector, 1.0)
                      for i, sector in enumerate(sectors)]
    gross_full = model.gross_output(shocked_demand)
    output_full = dict(zip(sectors, gross_full))

    limits: List[Tuple[str, float]] = [("demand", 1.0)]
    energy_info: Optional[dict] = None
    if energy is not None:
        available = energy.available * scenario.energy_factor
        constraint = InputConstraint(dict(energy.intensities), available,
                                     energy.resource_id, energy.unit)
        limited: ResourceLimitResult = apply_input_limit(model, shocked_demand, constraint)
        limits.append(("energy", limited.scale))
        energy_info = {
            "resource_id": constraint.resource_id,
            "unit": constraint.unit,
            "available": round(available, 12),
            "required_at_full_output": round(limited.required_at_full_output, 12),
            "scale": round(limited.scale, 12),
            "binding": limited.binding,
            "io": limited.describe(),
        }
    labour_info: Optional[dict] = None
    labour_model: Optional[LabourModel] = None
    if labour is not None:
        labour_model = labour.with_availability_factor(scenario.labour_factor)
        labour_scale = labour_model.feasible_scale(output_full)
        limits.append(("labour", labour_scale))
        labour_info = {"variant": labour_model.variant,
                       "scale": round(labour_scale, 12),
                       "binding": labour_model.binding(output_full),
                       "required": _labour_required(labour_model, output_full)}
    scale = min(value for _, value in limits)
    output_scaled = {sector: output_full[sector] * scale for sector in sectors}
    demand_scaled = [shocked_demand[i] * scale for i in range(model.size)]
    scaled_vector = [output_scaled[sector] for sector in sectors]
    material_residual = model.check_material_balance(scaled_vector, demand_scaled,
                                                     tolerance=tolerance)
    value_added = model.total_value_added(scaled_vector)
    if energy is not None and energy_info is not None:
        energy_info["required_at_scaled_output"] = round(
            energy.required(output_scaled), 12)
    if labour_model is not None and labour_info is not None:
        labour_info["required_at_scaled_output"] = _labour_required(labour_model, output_scaled)
        labour_info["binding_at_scaled_output"] = labour_model.binding(output_scaled)
    binding = tuple(name for name, value in limits if name != "demand" and value < 1.0 - tolerance)
    return ScenarioResult(
        scenario=scenario.name,
        output=output_scaled,
        final_demand=dict(zip(sectors, demand_scaled)),
        scale=scale,
        binding=binding,
        value_added=value_added,
        energy=energy_info,
        labour=labour_info,
        material_residual=material_residual,
    )


def _labour_required(model: LabourModel, output: Mapping[str, float]):
    required = model.required_hours(output)
    if isinstance(required, MappingABC):
        return {key: round(value, 12) for key, value in required.items()}
    return round(required, 12) if required is not None else None


def compare_scenarios(model: LeontiefModel, base_final_demand: Sequence[float],
                      scenarios: Sequence[Scenario],
                      energy: Optional[InputConstraint] = None,
                      labour: Optional[LabourModel] = None) -> List[dict]:
    """Run every scenario and return the comparable rows (no ranking, no admission)."""
    return [run_scenario(model, base_final_demand, scenario, energy, labour).describe()
            for scenario in scenarios]


def standard_shock_set(sectors: Sequence[str], demand_sector: Optional[str] = None) -> List[Scenario]:
    """The three prototype shocks of stage 4: demand, energy, labour, plus the combination."""
    demand_target = demand_sector or sectors[0]
    return [
        Scenario.baseline(),
        Scenario(name="demand_shock", demand_factors={demand_target: 0.8}),
        Scenario(name="energy_shock", energy_factor=0.7),
        Scenario(name="labour_shock", labour_factor=0.75),
        Scenario(name="combined_shock", demand_factors={demand_target: 0.8},
                 energy_factor=0.7, labour_factor=0.75),
    ]
