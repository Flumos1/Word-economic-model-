"""Physical units, dimensions and the three resource states (docs/37, docs/38 §3).

The stage-4 prototype works with *physical* quantities (hours, MWh, tonnes, pieces).
Money is deliberately absent here: a physical limit must not be replaced by a monetary
valuation (docs/38 E03).  A quantity is therefore only comparable with a quantity of the
same dimension, and the three resource states (potential / accessible / utilized) may be
ordered only when resource, unit, horizon and aggregation are identical (docs/38 §3).

Public API
----------
``Unit``, ``UnitRegistry``
    units with a dimension and a conversion factor to the dimension's base unit;
    ``UnitRegistry.convert`` refuses a cross-dimension conversion.
``ResourceState``
    ``POTENTIAL`` / ``ACCESSIBLE`` / ``UTILIZED`` - the three states every resource has.
``ResourceQuantity``
    a number that always carries resource, unit, state and horizon.
``check_state_order``
    the only legal ordering: ``0 <= utilized <= accessible <= potential`` for equal
    resource, unit and horizon; anything else raises :class:`StateOrderError`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Tuple


class ResourceError(ValueError):
    """Base error of the resources package."""


class DimensionMismatch(ResourceError):
    """Two quantities belong to different dimensions and cannot be compared."""


class StateOrderError(ResourceError):
    """The three resource states are not ordered consistently."""


class ResourceState(Enum):
    """The three states of a resource (docs/37 "Три состояния любого ресурса")."""

    POTENTIAL = "potential"
    ACCESSIBLE = "accessible"
    UTILIZED = "utilized"


@dataclass(frozen=True)
class Unit:
    """A physical unit with a dimension and a positive conversion factor.

    ``factor_to_base`` converts a value in this unit into the dimension's base unit, so
    ``GJ`` with ``factor_to_base=1 / 3.6`` converts to ``MWh`` (``1 GJ = 12 / 43.2 MWh``
    is not used; the canonical example is ``1 MWh = 3.6 GJ``).
    """

    name: str
    dimension: str
    factor_to_base: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ResourceError("unit name must be a non-empty string")
        if not isinstance(self.dimension, str) or not self.dimension.strip():
            raise ResourceError(f"unit {self.name!r}: dimension must be a non-empty string")
        factor = float(self.factor_to_base)
        if not math.isfinite(factor) or factor <= 0.0:
            raise ResourceError(f"unit {self.name!r}: factor_to_base must be positive and finite")
        object.__setattr__(self, "factor_to_base", factor)


#: Base units of the prototype: physical dimensions only.
DEFAULT_UNITS: Tuple[Unit, ...] = (
    Unit("hour", "labour_time"),
    Unit("person", "headcount"),
    Unit("MWh", "energy"),
    Unit("GJ", "energy", 1.0 / 3.6),
    Unit("tonne", "mass"),
    Unit("kg", "mass", 0.001),
    Unit("unit", "product"),
    Unit("m3", "volume"),
)


class UnitRegistry:
    """A small registry that converts within a dimension and rejects everything else."""

    def __init__(self, units: Iterable[Unit] = DEFAULT_UNITS) -> None:
        self._units: Dict[str, Unit] = {}
        for unit in units:
            self.register(unit)

    def register(self, unit: Unit) -> Unit:
        existing = self._units.get(unit.name)
        if existing is not None and existing.dimension != unit.dimension:
            raise ResourceError(
                f"unit {unit.name!r} already registered in dimension {existing.dimension!r}"
            )
        self._units[unit.name] = unit
        return unit

    def unit(self, name: str) -> Unit:
        try:
            return self._units[name]
        except KeyError as exc:
            raise ResourceError(f"unknown unit {name!r}") from exc

    def dimension(self, name: str) -> str:
        return self.unit(name).dimension

    def convert(self, amount: float, from_unit: str, to_unit: str) -> float:
        """Convert ``amount`` from one unit into another unit of the same dimension."""
        source = self.unit(from_unit)
        target = self.unit(to_unit)
        if source.dimension != target.dimension:
            raise DimensionMismatch(
                f"cannot convert {from_unit!r} ({source.dimension}) to "
                f"{to_unit!r} ({target.dimension})"
            )
        value = float(amount)
        if not math.isfinite(value):
            raise ResourceError("amount must be finite")
        return value * source.factor_to_base / target.factor_to_base

    def same_dimension(self, first: str, second: str) -> bool:
        return self.dimension(first) == self.dimension(second)


def _as_state(state: "ResourceState | str") -> ResourceState:
    if isinstance(state, ResourceState):
        return state
    try:
        return ResourceState(str(state).strip().lower())
    except ValueError as exc:
        raise ResourceError(f"unknown resource state {state!r}") from exc


@dataclass(frozen=True)
class ResourceQuantity:
    """A physical quantity of a resource in one of the three states.

    ``horizon`` makes the state explicit over time: a potential is always a potential
    *at a horizon and under a scenario* (docs/37), never a fixed absolute maximum.
    """

    resource: str
    amount: float
    unit: str
    state: ResourceState
    horizon: str = "period"

    def __post_init__(self) -> None:
        if not isinstance(self.resource, str) or not self.resource.strip():
            raise ResourceError("resource must be a non-empty string")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ResourceError("unit must be a non-empty string")
        if not isinstance(self.horizon, str) or not self.horizon.strip():
            raise ResourceError("horizon must be a non-empty string")
        amount = float(self.amount)
        if not math.isfinite(amount):
            raise ResourceError(f"resource {self.resource!r}: amount must be finite")
        if amount < 0.0:
            raise ResourceError(f"resource {self.resource!r}: amount must be non-negative")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "state", _as_state(self.state))

    def in_unit(self, registry: UnitRegistry, unit: str) -> "ResourceQuantity":
        return ResourceQuantity(
            resource=self.resource,
            amount=registry.convert(self.amount, self.unit, unit),
            unit=unit,
            state=self.state,
            horizon=self.horizon,
        )


def _same_scope(first: ResourceQuantity, other: ResourceQuantity) -> None:
    if first.resource != other.resource:
        raise StateOrderError(
            f"different resources: {first.resource!r} vs {other.resource!r}"
        )
    if first.horizon != other.horizon:
        raise StateOrderError(
            f"different horizons: {first.horizon!r} vs {other.horizon!r}; the states are "
            "only comparable for the same horizon and scenario"
        )


def check_state_order(
    potential: ResourceQuantity,
    accessible: ResourceQuantity,
    utilized: ResourceQuantity,
    registry: Optional[UnitRegistry] = None,
    tolerance: float = 1e-9,
) -> None:
    """Verify ``0 <= utilized <= accessible <= potential`` for one resource.

    The comparison is only allowed when resource and horizon match and the amounts are
    expressed in the same dimension.  If a :class:`UnitRegistry` is given, amounts in
    different units of the same dimension are converted before comparison; without a
    registry the units must be identical.  This is the operational form of docs/38 §3:
    the numeric ordering is meaningless across different definitions and units.
    """
    if potential.state is not ResourceState.POTENTIAL:
        raise StateOrderError("the first quantity must be in the POTENTIAL state")
    if accessible.state is not ResourceState.ACCESSIBLE:
        raise StateOrderError("the second quantity must be in the ACCESSIBLE state")
    if utilized.state is not ResourceState.UTILIZED:
        raise StateOrderError("the third quantity must be in the UTILIZED state")
    _same_scope(potential, accessible)
    _same_scope(potential, utilized)
    if registry is not None:
        base = potential.unit
        a = registry.convert(accessible.amount, accessible.unit, base)
        u = registry.convert(utilized.amount, utilized.unit, base)
        p = potential.amount
    else:
        if accessible.unit != potential.unit or utilized.unit != potential.unit:
            raise DimensionMismatch(
                "cannot compare resource states in different units without a UnitRegistry: "
                f"{potential.unit!r}, {accessible.unit!r}, {utilized.unit!r}"
            )
        p, a, u = potential.amount, accessible.amount, utilized.amount
    if not (a <= p + tolerance):
        raise StateOrderError(
            f"accessible {a} exceeds potential {p} for resource {potential.resource!r}"
        )
    if not (u <= a + tolerance):
        raise StateOrderError(
            f"utilized {u} exceeds accessible {a} for resource {potential.resource!r}"
        )
    if min(p, a, u) < 0.0:
        raise StateOrderError("resource states must be non-negative")
