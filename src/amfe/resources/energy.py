"""Primary-resource balances and output limits: energy and raw materials (docs/38 block 09).

Two separate objects, because they answer two different questions:

``ResourceBalance``
    the physical supply-use identity of one resource in one period (docs/38 I01/I04):

        domestic_extraction + imports + stock_draw
            = intermediate_use + final_consumption + exports + losses + stock_build.

    Stock changes appear exactly once, on the correct side: a stock draw is a supply, a
    stock build is a use; both signs in one period for one homogeneous resource are
    rejected as a bookkeeping conflict.  ``validate`` raises on a non-zero residual, so an
    inconsistent balance cannot silently enter the model.

``InputConstraint``
    the physical limit that a primary resource puts on gross output:

        sum_s intensity[s] * x[s] <= available,

    with ``intensity`` in resource units per unit of gross output.  The limit is a physical
    bound, not a price and not a monetary valuation (docs/38 E03); scarcity shows up as a
    reduced feasible output vector, never as an implicit currency.  Because the constraint
    is linear in output, the feasible uniform scale is ``available / required`` and no
    search is needed; ``scenario`` combines it with the labour limits.

The prototype does not model resource prices, substitution between energy carriers, or
extraction dynamics: those are hypotheses for later stages (docs/42 stage 5, docs/38 H).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

TOLERANCE = 1e-9


class ResourceBalanceError(ValueError):
    """The resource balance is inconsistent or a limit is invalid."""


def _finite_non_negative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ResourceBalanceError(f"{name} must be a finite non-negative number, got {value!r}")
    return number


@dataclass
class ResourceBalance:
    """Supply-use identity of one primary resource in one period and one territory.

    ``supply`` keys are free labels (``domestic_extraction``, ``imports``, ``stock_draw`` or
    any other component); ``use`` keys likewise (``intermediate_use``, ``final_consumption``,
    ``exports``, ``losses``, ``stock_build``).  The only enforced structure is the sum
    identity, non-negativity and the stock-change rule.  Keep one dimension/unit per
    balance: energy in MWh, ore in tonnes, etc.
    """

    resource_id: str
    unit: str
    supply: Mapping[str, float]
    use: Mapping[str, float]
    territory: str = "domestic"
    period: str = "period"
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ResourceBalanceError("resource_id must be a non-empty string")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ResourceBalanceError("unit must be a non-empty string")
        self.supply = {str(key): _finite_non_negative(value, f"supply[{key}]")
                       for key, value in dict(self.supply).items()}
        self.use = {str(key): _finite_non_negative(value, f"use[{key}]")
                    for key, value in dict(self.use).items()}
        if not self.supply or not self.use:
            raise ResourceBalanceError("both supply and use must have at least one component")
        draw = self.supply.get("stock_draw", 0.0)
        build = self.use.get("stock_build", 0.0)
        if draw > self.tolerance and build > self.tolerance:
            raise ResourceBalanceError(
                "stock_draw and stock_build are both positive for one homogeneous resource; "
                "a stock change must appear exactly once (docs/38 I04)"
            )
        if self.tolerance <= 0.0:
            raise ResourceBalanceError("tolerance must be positive")

    @property
    def supply_total(self) -> float:
        return math.fsum(self.supply.values())

    @property
    def use_total(self) -> float:
        return math.fsum(self.use.values())

    @property
    def residual(self) -> float:
        """``supply_total - use_total``; zero means the physical balance holds."""
        return self.supply_total - self.use_total

    def validate(self, tolerance: Optional[float] = None) -> float:
        """Raise unless supply equals use; returns the residual."""
        limit = self.tolerance if tolerance is None else float(tolerance)
        residual = self.residual
        if abs(residual) > limit:
            raise ResourceBalanceError(
                f"{self.resource_id} balance in {self.period}: supply {self.supply_total} != "
                f"use {self.use_total} (residual {residual:.6g})"
            )
        return residual

    def describe(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "unit": self.unit,
            "territory": self.territory,
            "period": self.period,
            "supply_total": self.supply_total,
            "use_total": self.use_total,
            "stock_draw": self.supply.get("stock_draw", 0.0),
            "stock_build": self.use.get("stock_build", 0.0),
        }


@dataclass
class InputConstraint:
    """A physical limit ``sum_s intensity[s] * x[s] <= available`` on gross output.

    ``intensities`` maps every sector of the IO model to resource units per unit of gross
    output; sectors without a coefficient are treated as zero.  The constraint is a
    resource *limit*, not a cost: it does not require the resource to be used.
    """

    intensities: Mapping[str, float]
    available: float
    resource_id: str = "energy"
    unit: str = "MWh"

    def __post_init__(self) -> None:
        if not self.intensities:
            raise ResourceBalanceError("intensities must have at least one sector")
        self.intensities = {
            str(key): _finite_non_negative(value, f"intensity[{key}]")
            for key, value in dict(self.intensities).items()
        }
        self.available = _finite_non_negative(self.available, "available")

    def required(self, gross_output: Mapping[str, float],
                 tolerance: float = TOLERANCE) -> float:
        """Resource required by a gross-output vector; unknown sectors must be zero."""
        unknown = set(gross_output) - set(self.intensities)
        if unknown:
            raise ResourceBalanceError(f"output has sectors without an intensity: {sorted(unknown)}")
        total = 0.0
        for sector, coefficient in self.intensities.items():
            output = _finite_non_negative(gross_output.get(sector, 0.0), f"output[{sector}]")
            total += coefficient * output
        return total

    def slack(self, gross_output: Mapping[str, float]) -> float:
        return self.available - self.required(gross_output)

    def binding(self, gross_output: Mapping[str, float], tolerance: float = 1e-6) -> bool:
        """``True`` when the output vector needs the resource at (or beyond) its availability.

        A zero requirement never binds, even when availability is zero: the resource does not
        restrict anything (the constraint is trivially satisfied by every output).
        """
        need = self.required(gross_output)
        return need > 0.0 and need >= self.available - tolerance

    def feasible_scale(self, gross_output: Mapping[str, float]) -> float:
        """Largest ``s in [0, 1]`` with ``s * required <= available`` (1 when unconstrained)."""
        need = self.required(gross_output)
        if need <= 0.0:
            return 1.0
        return min(1.0, self.available / need)

    def scaled_output(self, gross_output: Mapping[str, float], scale: float) -> Dict[str, float]:
        scale = float(scale)
        if not math.isfinite(scale) or scale < 0.0:
            raise ResourceBalanceError("scale must be finite and non-negative")
        return {sector: _finite_non_negative(value, f"output[{sector}]") * scale
                for sector, value in gross_output.items()}

    def describe(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "unit": self.unit,
            "available": self.available,
            "sectors": sorted(self.intensities),
            "max_intensity": max(self.intensities.values()),
        }


@dataclass
class ResourceLimitResult:
    """Outcome of applying an input limit to an IO solution (physical units only)."""

    scale: float
    binding: bool
    available: float
    required_at_full_output: float
    required_at_scaled_output: float
    output: Dict[str, float]
    value_added: float
    resource_id: str = "energy"
    unit: str = "MWh"

    def describe(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "unit": self.unit,
            "scale": round(self.scale, 12),
            "binding": self.binding,
            "available": round(self.available, 12),
            "required_at_full_output": round(self.required_at_full_output, 12),
            "required_at_scaled_output": round(self.required_at_scaled_output, 12),
            "value_added": round(self.value_added, 12),
        }


def apply_input_limit(model, final_demand: Sequence[float],
                      constraint: InputConstraint) -> ResourceLimitResult:
    """Scale final demand until the resource limit holds, and report the limit.

    ``model`` is a :class:`amfe.resources.io.LeontiefModel`.  The scale is uniform across
    sectors (a rationing rule, explicitly a scenario assumption): the prototype does not
    solve an optimal allocation problem, it only enforces physical feasibility.
    """
    gross_full = model.gross_output(final_demand)
    output_full = dict(zip(model.sector_ids, gross_full))
    required_full = constraint.required(output_full)
    scale = constraint.feasible_scale(output_full)
    output_scaled = constraint.scaled_output(output_full, scale)
    scaled_vector = [output_scaled[sector] for sector in model.sector_ids]
    required_scaled = constraint.required(output_scaled)
    value_added = model.total_value_added(scaled_vector)
    return ResourceLimitResult(
        scale=scale,
        binding=constraint.binding(output_full),
        available=constraint.available,
        required_at_full_output=required_full,
        required_at_scaled_output=required_scaled,
        output=output_scaled,
        value_added=value_added,
        resource_id=constraint.resource_id,
        unit=constraint.unit,
    )


def energy_balance_from_io(io_result: ResourceLimitResult, domestic_extraction: float,
                           imports: float = 0.0, exports: float = 0.0,
                           losses: float = 0.0) -> ResourceBalance:
    """Build the energy balance implied by an IO solution (closed economy by default).

    The intermediate energy use is the amount the resource limit counted; final energy
    consumption is whatever the remaining supply covers.  This keeps the balance and the
    output limit on one accounting basis instead of two disagreeing ones.
    """
    intermediate = io_result.required_at_scaled_output
    final = domestic_extraction + imports - intermediate - exports - losses
    if final < -TOLERANCE:
        raise ResourceBalanceError(
            f"energy supply {domestic_extraction + imports} is below the implied intermediate "
            f"use {intermediate}: the balance cannot be closed"
        )
    return ResourceBalance(
        resource_id=io_result.resource_id,
        unit=io_result.unit,
        supply={"domestic_extraction": domestic_extraction, "imports": imports},
        use={"intermediate_use": intermediate, "final_consumption": max(0.0, final),
             "exports": exports, "losses": losses},
    )
