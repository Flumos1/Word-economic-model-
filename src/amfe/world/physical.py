"""Physical layer adapter for the integrated world core (stage 4).

The adapter owns nothing that is already implemented in :mod:`amfe.resources`; it only

* builds the validated Leontief model, energy constraint and labour variant from a
  :class:`~amfe.world.config.PhysicalConfig`;
* runs one scenario-period and returns the physical outcome;
* exposes the physical checks (material balance, no double counting, limit feasibility)
  in a form the integration layer can compare with the monetary side.

Units are physical units.  No price, money or valuation appears here: the monetary block
in :mod:`amfe.world.model` is responsible for valuation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from amfe.resources import (
    AggregateLabourConstraint,
    InputConstraint,
    LeontiefModel,
    NoLabourConstraint,
    Scenario,
    ScenarioResult,
    TaskLabourConstraint,
    check_no_double_counting,
    run_scenario,
)

from .config import PhysicalConfig, WorldConfigError

TOLERANCE = 1e-9


@dataclass(frozen=True)
class PhysicalOutcome:
    """One period of the physical layer plus its checks."""

    result: ScenarioResult
    material_residual: float
    value_added_residual: float
    gross_output: float
    intermediate_use: float
    value_added: float
    final_uses: float

    @property
    def output(self) -> dict[str, float]:
        return dict(self.result.output)

    @property
    def final_demand(self) -> dict[str, float]:
        return dict(self.result.final_demand)

    def as_dict(self) -> dict:
        return {
            "scenario": self.result.scenario,
            "scale": round(self.result.scale, 12),
            "binding": list(self.result.binding),
            "output": {sector: round(value, 12)
                       for sector, value in self.result.output.items()},
            "final_demand": {sector: round(value, 12)
                             for sector, value in self.result.final_demand.items()},
            "value_added": round(self.value_added, 12),
            "gross_output": round(self.gross_output, 12),
            "intermediate_use": round(self.intermediate_use, 12),
            "material_residual": self.material_residual,
            "value_added_residual": self.value_added_residual,
            "energy": self.result.energy,
            "labour": self.result.labour,
        }


class CountryPhysical:
    """The physical layer of one country (stage 4) adapted to the world-core cycle."""

    def __init__(self, config: PhysicalConfig):
        if not isinstance(config, PhysicalConfig):
            raise WorldConfigError("physical config must be a PhysicalConfig")
        self.config = config
        try:
            self.model = LeontiefModel(config.technical_matrix, config.sectors)
        except Exception as exc:  # pragma: no cover - defensive mapping
            raise WorldConfigError(f"invalid IO model: {exc}") from exc
        if not self.model.is_productive():
            raise WorldConfigError(
                "the IO matrix is not productive (spectral radius must be below 1); "
                "the physical layer refuses to produce a negative output")
        self.energy = InputConstraint(
            dict(config.energy_intensities), config.energy_available,
            config.energy_resource_id, config.energy_unit)
        self.labour = self._build_labour()
        #: baseline physical value added at the configured base final demand
        self.baseline = self.run(Scenario.baseline())

    # ------------------------------------------------------------------ build
    def _build_labour(self):
        config = self.config
        if config.labour_variant == "B0":
            return NoLabourConstraint(descriptive_hours=dict(config.hours_per_unit))
        if config.labour_variant == "B1":
            return AggregateLabourConstraint(dict(config.hours_per_unit),
                                             config.accessible_hours)
        if config.task_hours is None or config.skill_accessible is None:
            raise WorldConfigError(
                "labour variant B2 requires task_hours and skill_accessible")
        return TaskLabourConstraint(dict(config.task_hours), dict(config.skill_accessible),
                                    {str(task): str(skill)
                                     for task, skill in (config.task_skill or {}).items()})

    # -------------------------------------------------------------------- run
    def run(self, scenario: Scenario) -> PhysicalOutcome:
        if not isinstance(scenario, Scenario):
            raise WorldConfigError("physical run expects a resources.Scenario")
        result = run_scenario(self.model, list(self.config.base_final_demand), scenario,
                              energy=self.energy, labour=self.labour)
        vector = [result.output[sector] for sector in self.model.sector_ids]
        demand = [result.final_demand[sector] for sector in self.model.sector_ids]
        double_counting = check_no_double_counting(self.model, vector, demand)
        if not math.isfinite(result.material_residual) or result.material_residual > TOLERANCE:
            raise WorldConfigError(
                f"physical material balance residual {result.material_residual} exceeds tolerance")
        return PhysicalOutcome(
            result=result,
            material_residual=result.material_residual,
            value_added_residual=double_counting["residual"],
            gross_output=double_counting["gross_output"],
            intermediate_use=double_counting["intermediate_use"],
            value_added=double_counting["value_added"],
            final_uses=double_counting["final_uses"],
        )

    # ------------------------------------------------------------- diagnostics
    def energy_feasible(self, outcome: PhysicalOutcome, factor: float) -> bool:
        energy = self.energy
        available = energy.available * factor
        required = energy.required(outcome.result.output)
        return required <= available + 1e-9 * max(1.0, available)

    def labour_feasible(self, outcome: PhysicalOutcome, factor: float) -> bool:
        labour = self.labour.with_availability_factor(factor)
        output = outcome.result.output
        if labour.variant == "B0":
            return True
        required = labour.required_hours(output)
        if isinstance(required, dict):
            accessible = labour.skill_accessible  # type: ignore[attr-defined]
            return all(value <= accessible[skill] + 1e-9 * max(1.0, accessible[skill])
                       for skill, value in required.items())
        return required <= labour.accessible_hours + 1e-9 * max(1.0, labour.accessible_hours)  # type: ignore[attr-defined]

    def describe(self) -> dict:
        return {
            "sectors": list(self.config.sectors),
            "productive": self.model.is_productive(),
            "spectral_radius": round(self.model.spectral_radius(), 12),
            "base_final_demand": list(self.config.base_final_demand),
            "baseline_value_added": round(self.baseline.value_added, 12),
            "energy": self.energy.describe(),
            "labour": self.labour.describe(),
        }


__all__ = ["PhysicalOutcome", "CountryPhysical", "TOLERANCE"]
