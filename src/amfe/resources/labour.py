"""Labour as a structured resource endowment and the B0/B1/B2 constraint variants (docs/37).

The labour layer is a *physical* bound on activity: accessible hours limit how much gross
output can be produced, but hours never determine the value of output.  There is
deliberately no labour theory of value here (docs/35 "Важное теоретическое различение"): the
module does not set prices, does not convert hours into money and does not claim that output
is proportional to labour.  A sector with zero labour input can produce (the automation case
R04), and a labour-abundant economy is not therefore a richer one.

Three model variants follow docs/37 and are compared on the same output vector, with their
own independently specified parameters:

``NoLabourConstraint`` (B0)
    no labour layer in the production constraint; labour is descriptive only.  This is the
    required baseline (R05) and the case that keeps automation possible (R04).
``AggregateLabourConstraint`` (B1)
    minimal labour indicators: one aggregate hours pool and one hours-per-unit coefficient
    per sector.  Sectors are interchangeable in the pool.
``TaskLabourConstraint`` (B2)
    task-occupation structure: each sector needs hours by task, each task is provided by a
    skill group, and accessible hours are per group and cannot be pooled.  The binding
    group can differ from the aggregate B1 answer, which is exactly why the comparison is
    done on independent parameters instead of one "quality" scalar (R03).

Available hours are the ACCESSIBLE state of the resource; the pool can be reduced by a
scenario factor (illness, mobilisation, participation change) through
``with_availability_factor``.  Skill substitution, migration and endogenous participation are
*not* modelled in this prototype: they are hypotheses for later stages (docs/37, docs/42).
"""
from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

TOLERANCE = 1e-9


class LabourError(ValueError):
    """Invalid labour data or an infeasible labour vector."""


def _non_negative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise LabourError(f"{name} must be a finite non-negative number, got {value!r}")
    return number


@dataclass(frozen=True)
class LabourStates:
    """The three states of the labour resource, in hours, by skill group (docs/37).

    ``potential >= accessible >= utilized`` must hold for every group: the comparison is
    meaningful only for one dimension, one horizon and one aggregation, exactly as in
    ``units.check_state_order``.  Groups are skill groups, never a single "quality of a
    person" scalar (R03).
    """

    potential: Mapping[str, float]
    accessible: Mapping[str, float]
    utilized: Mapping[str, float]
    unit: str = "hour"
    horizon: str = "period"

    def __post_init__(self) -> None:
        groups = list(dict(self.potential).keys())
        if not groups:
            raise LabourError("LabourStates needs at least one group")
        for name, values in (("potential", self.potential), ("accessible", self.accessible),
                             ("utilized", self.utilized)):
            if list(dict(values).keys()) != groups:
                raise LabourError(
                    f"{name} must have exactly the groups {groups}; different groups cannot be "
                    "compared (docs/38 §3)"
                )
            object.__setattr__(self, name, {group: _non_negative(values[group], f"{name}[{group}]")
                                            for group in groups})
        for group in groups:
            potential = self.potential[group]
            accessible = self.accessible[group]
            utilized = self.utilized[group]
            if accessible > potential + TOLERANCE:
                raise LabourError(
                    f"group {group!r}: accessible {accessible} exceeds potential {potential}"
                )
            if utilized > accessible + TOLERANCE:
                raise LabourError(
                    f"group {group!r}: utilized {utilized} exceeds accessible {accessible}"
                )

    @property
    def groups(self) -> Tuple[str, ...]:
        return tuple(self.potential.keys())

    def total(self, state: str = "utilized") -> float:
        values = {"potential": self.potential, "accessible": self.accessible,
                  "utilized": self.utilized}.get(str(state).strip().lower())
        if values is None:
            raise LabourError(f"unknown labour state {state!r}")
        return math.fsum(values.values())

    def utilized_share(self) -> float:
        potential = self.total("potential")
        return self.total("utilized") / potential if potential > 0.0 else 0.0

    def group_utilization(self) -> Dict[str, float]:
        """Utilized / accessible per group (0 when nothing is accessible)."""
        return {group: (self.utilized[group] / self.accessible[group]
                        if self.accessible[group] > 0.0 else 0.0)
                for group in self.groups}

    def describe(self) -> dict:
        return {"unit": self.unit, "horizon": self.horizon, "groups": list(self.groups),
                "potential": self.total("potential"), "accessible": self.total("accessible"),
                "utilized": self.total("utilized")}


def _validate_output(output: Mapping[str, float], sectors: Optional[Sequence[str]] = None) -> Dict[str, float]:
    result = {}
    for sector, value in dict(output).items():
        result[str(sector)] = _non_negative(value, f"output[{sector}]")
    if sectors is not None:
        unknown = set(result) - set(sectors)
        if unknown:
            raise LabourError(f"output has sectors without labour coefficients: {sorted(unknown)}")
        result = {sector: result.get(sector, 0.0) for sector in sectors}
    return result


class LabourModel:
    """Common interface of the three labour variants (B0/B1/B2)."""

    variant: ClassVar[str] = "B0"

    def required_hours(self, output: Mapping[str, float]):
        raise NotImplementedError

    def feasible_scale(self, output: Mapping[str, float]) -> float:
        raise NotImplementedError

    def binding(self, output: Mapping[str, float]) -> Optional[str]:
        raise NotImplementedError

    def with_availability_factor(self, factor: float) -> "LabourModel":
        raise NotImplementedError

    def describe(self) -> dict:
        raise NotImplementedError


@dataclass
class NoLabourConstraint(LabourModel):
    """B0: labour is not a production constraint (baseline R05, automation case R04)."""

    descriptive_hours: Mapping[str, float] = field(default_factory=dict)
    variant: ClassVar[str] = "B0"

    def __post_init__(self) -> None:
        self.descriptive_hours = {str(key): _non_negative(value, f"hours[{key}]")
                                  for key, value in dict(self.descriptive_hours).items()}

    def required_hours(self, output: Mapping[str, float]) -> None:
        _validate_output(output)
        return None

    def feasible_scale(self, output: Mapping[str, float]) -> float:
        _validate_output(output)
        return 1.0

    def binding(self, output: Mapping[str, float]) -> Optional[str]:
        return None

    def with_availability_factor(self, factor: float) -> "NoLabourConstraint":
        _non_negative(factor, "factor")
        return NoLabourConstraint({key: value * factor for key, value in self.descriptive_hours.items()})

    def describe(self) -> dict:
        return {"variant": self.variant, "labour_in_production": False}


@dataclass
class AggregateLabourConstraint(LabourModel):
    """B1: one aggregate accessible-hours pool and sector hours-per-unit coefficients."""

    hours_per_unit: Mapping[str, float]
    accessible_hours: float
    variant: ClassVar[str] = "B1"

    def __post_init__(self) -> None:
        if not self.hours_per_unit:
            raise LabourError("hours_per_unit must have at least one sector")
        self.hours_per_unit = {str(key): _non_negative(value, f"hours_per_unit[{key}]")
                               for key, value in dict(self.hours_per_unit).items()}
        self.accessible_hours = _non_negative(self.accessible_hours, "accessible_hours")

    def required_hours(self, output: Mapping[str, float]) -> float:
        values = _validate_output(output, list(self.hours_per_unit))
        return math.fsum(self.hours_per_unit[sector] * values[sector] for sector in self.hours_per_unit)

    def feasible_scale(self, output: Mapping[str, float]) -> float:
        need = self.required_hours(output)
        if need <= 0.0:
            return 1.0
        return min(1.0, self.accessible_hours / need)

    def binding(self, output: Mapping[str, float]) -> Optional[str]:
        return "aggregate_hours" if self.required_hours(output) > self.accessible_hours + TOLERANCE else None

    def slack(self, output: Mapping[str, float]) -> float:
        return self.accessible_hours - self.required_hours(output)

    def with_availability_factor(self, factor: float) -> "AggregateLabourConstraint":
        return AggregateLabourConstraint(self.hours_per_unit, self.accessible_hours * _non_negative(factor, "factor"))

    def describe(self) -> dict:
        return {"variant": self.variant, "labour_in_production": True,
                "accessible_hours": self.accessible_hours, "sectors": sorted(self.hours_per_unit)}


@dataclass
class TaskLabourConstraint(LabourModel):
    """B2: task-occupation structure with per-skill accessible hours that cannot be pooled.

    ``task_hours[sector][task]`` is hours of ``task`` per unit of gross output of ``sector``.
    ``task_skill`` maps a task to a skill group (defaults to the task name, i.e. one skill
    per task).  ``skill_accessible`` holds accessible hours per skill group.  A skill group
    cannot cover another group's hours: full substitution would make the structure
    equivalent to B1 and is therefore *not* assumed; partial substitution is a scenario
    hypothesis left to a later stage.
    """

    task_hours: Mapping[str, Mapping[str, float]]
    skill_accessible: Mapping[str, float]
    task_skill: Optional[Mapping[str, str]] = None
    variant: ClassVar[str] = "B2"

    def __post_init__(self) -> None:
        if not self.task_hours:
            raise LabourError("task_hours must have at least one sector")
        normalized: Dict[str, Dict[str, float]] = {}
        tasks = set()
        for sector, hours in dict(self.task_hours).items():
            if not dict(hours):
                raise LabourError(f"sector {sector!r} has no task coefficients")
            normalized[str(sector)] = {str(task): _non_negative(value, f"task_hours[{sector}][{task}]")
                                       for task, value in dict(hours).items()}
            tasks.update(normalized[str(sector)])
        skills = {str(skill): _non_negative(value, f"skill_accessible[{skill}]")
                  for skill, value in dict(self.skill_accessible).items()}
        if not skills:
            raise LabourError("skill_accessible must have at least one skill group")
        mapping = {str(task): str(skill) for task, skill in dict(self.task_skill or {}).items()}
        for task in tasks:
            mapping.setdefault(task, task)
        unknown = {skill for skill in mapping.values() if skill not in skills}
        if unknown:
            raise LabourError(f"tasks map to skill groups without accessible hours: {sorted(unknown)}")
        self.task_hours = normalized
        self.skill_accessible = skills
        self.task_skill = mapping

    def required_by_task(self, output: Mapping[str, float]) -> Dict[str, float]:
        values = _validate_output(output, list(self.task_hours))
        required: Dict[str, float] = {}
        for sector, tasks in self.task_hours.items():
            for task, coefficient in tasks.items():
                required[task] = required.get(task, 0.0) + coefficient * values[sector]
        return required

    def required_by_skill(self, output: Mapping[str, float]) -> Dict[str, float]:
        by_task = self.required_by_task(output)
        by_skill = {skill: 0.0 for skill in self.skill_accessible}
        for task, hours in by_task.items():
            by_skill[self.task_skill[task]] += hours
        return by_skill

    def required_hours(self, output: Mapping[str, float]) -> Dict[str, float]:
        """B2 keeps the requirement as a mapping by skill group, not as one scalar.

        Returning the vector is the point of the task-occupation variant: the aggregate
        hours can look sufficient while one skill group is binding (R03).
        """
        return self.required_by_skill(output)

    def feasible_scale(self, output: Mapping[str, float]) -> float:
        required = self.required_by_skill(output)
        scales = [1.0]
        for skill, need in required.items():
            if need > 0.0:
                scales.append(min(1.0, self.skill_accessible[skill] / need))
        return min(scales)

    def binding(self, output: Mapping[str, float]) -> Optional[str]:
        required = self.required_by_skill(output)
        candidates = [(self.skill_accessible[skill] / need, skill)
                      for skill, need in required.items()
                      if need > 0.0 and self.skill_accessible[skill] / need < 1.0 - TOLERANCE]
        return min(candidates)[1] if candidates else None

    def with_availability_factor(self, factor: float) -> "TaskLabourConstraint":
        factor = _non_negative(factor, "factor")
        return TaskLabourConstraint(
            self.task_hours,
            {skill: hours * factor for skill, hours in self.skill_accessible.items()},
            self.task_skill,
        )

    def describe(self) -> dict:
        return {"variant": self.variant, "labour_in_production": True,
                "sectors": sorted(self.task_hours), "skills": sorted(self.skill_accessible)}


def compare_labour_variants(output: Mapping[str, float],
                            models: Sequence[LabourModel]) -> List[dict]:
    """Compare B0/B1/B2 on the same output vector, each with its own parameters.

    This is a *structural* comparison, not a ranking: docs/37 R06 forbids declaring a
    variant better before an out-of-sample test.  The rows show what each variant requires
    and which constraint (if any) binds.
    """
    rows = []
    for model in models:
        required = model.required_hours(output)
        rows.append({
            "variant": model.variant,
            "required": (dict(required) if isinstance(required, MappingABC) else required),
            "feasible_scale": round(model.feasible_scale(output), 12),
            "binding": model.binding(output),
        })
    return rows


def employment_from_hours(hours: float, hours_per_worker: float) -> float:
    """Headcount equivalent of an hours volume; a conversion, not a valuation."""
    hours = _non_negative(hours, "hours")
    per_worker = float(hours_per_worker)
    if not math.isfinite(per_worker) or per_worker <= 0.0:
        raise LabourError("hours_per_worker must be positive and finite")
    return hours / per_worker


def unemployment_rate(labour_force: float, employed: float) -> float:
    """``(labour_force - employed) / labour_force``; the definition must be stated (docs/35)."""
    force = _non_negative(labour_force, "labour_force")
    employed = _non_negative(employed, "employed")
    if employed > force + TOLERANCE:
        raise LabourError("employed cannot exceed the labour force under this definition")
    return (force - employed) / force if force > 0.0 else 0.0
