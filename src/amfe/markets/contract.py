"""Market module contracts for AMFE 2.0 (docs/144, docs/145, docs/147).

Every commodity market is a *module* behind one small interface: it declares its inputs (units,
scenario parameters, assumptions with provenance), runs a deterministic balance engine and returns
a :class:`MarketResult` with a price **range**, the assumptions used and the sensitivity table.

Design rules
------------
* **Research-only.**  Every result carries ``status = "RESEARCH_ONLY"``: a scenario calculation, not
  a forecast, not an admission decision and not investment advice (docs/147).
* **No invented precision.**  A parameter is either ``observed`` (a published time series point),
  ``derived`` (computed inside the module) or ``assumption`` (a public-order-of-magnitude input that
  the caller must replace with a sourced value).  Every assumption carries ``source``/``note`` and a
  ``replace_with`` hint in the oil module.
* **Fail-closed.**  Incompatible units, negative stocks, negative supply, non-negative elasticity or
  an unknown scenario raise instead of returning a number.
* **Deterministic.**  No timestamps, no randomness: two runs with the same inputs produce the same
  price path and the same SHA-256 digest.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping

#: Provenance classes of a parameter.
SOURCE_CLASSES = ("observed", "derived", "assumption")
#: Status carried by every market result (research prototype, not an admission).
RESEARCH_ONLY = "RESEARCH_ONLY"
#: Canonical result schema.
RESULT_SCHEMA = "amfe.markets.result.v1"


class MarketError(ValueError):
    """Base class for market-module errors."""


class ScenarioError(MarketError):
    """Unknown scenario id or invalid scenario parameters."""


class UnitError(MarketError):
    """Incompatible units between the calibration, the scenario and the module."""


class BalanceError(MarketError):
    """The supply/demand/stock balance cannot be closed (or a stock would go negative)."""


class DataMissing(MarketError):
    """Fail-closed: a required parameter or observation is absent."""


@dataclass(frozen=True)
class Assumption:
    """One model input with explicit provenance (``observed`` / ``derived`` / ``assumption``)."""

    name: str
    value: object
    unit: str
    source_class: str
    note: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.source_class not in SOURCE_CLASSES:
            raise MarketError(f"{self.name}: unknown source_class {self.source_class!r}; "
                              f"allowed {list(SOURCE_CLASSES)}")
        if not str(self.name).strip():
            raise MarketError("Assumption.name must be non-empty")
        if not str(self.unit).strip():
            raise MarketError(f"{self.name}: unit must be non-empty")

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "source_class": self.source_class, "note": self.note, "source": self.source}


@dataclass(frozen=True)
class Scenario:
    """A named shock/parameter set applied by a module."""

    scenario_id: str
    description: str
    parameters: Mapping = field(default_factory=dict)
    horizon_months: int = 6

    def __post_init__(self) -> None:
        if not str(self.scenario_id).strip():
            raise MarketError("scenario_id must be non-empty")
        if int(self.horizon_months) <= 0:
            raise ScenarioError(f"{self.scenario_id}: horizon_months must be positive")
        object.__setattr__(self, "horizon_months", int(self.horizon_months))
        object.__setattr__(self, "parameters", dict(self.parameters))

    def as_dict(self) -> dict:
        return {"scenario_id": self.scenario_id, "description": self.description,
                "parameters": dict(self.parameters), "horizon_months": self.horizon_months}


@dataclass(frozen=True)
class MarketResult:
    """The output of a market module: deterministic price path, range, assumptions, checks."""

    module_id: str
    commodity: str
    scenario_id: str
    units: str
    horizon_months: int
    price_path: tuple[float, ...]
    summary: dict
    assumptions: tuple[Assumption, ...]
    sensitivity: tuple[dict, ...] = ()
    checks: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    status: str = RESEARCH_ONLY
    schema: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise MarketError(f"market results are research-only, not {self.status!r}")
        if not self.price_path:
            raise MarketError("price_path must not be empty")
        if len(self.price_path) != int(self.horizon_months):
            raise MarketError(f"price_path has {len(self.price_path)} entries for a "
                              f"{self.horizon_months}-month horizon")
        if any(value <= 0.0 for value in self.price_path):
            raise MarketError("price_path values must be positive")
        object.__setattr__(self, "price_path", tuple(float(v) for v in self.price_path))
        object.__setattr__(self, "summary", dict(self.summary))
        object.__setattr__(self, "checks", dict(self.checks))
        object.__setattr__(self, "meta", dict(self.meta))

    def digest(self) -> str:
        payload = {"module_id": self.module_id, "commodity": self.commodity,
                   "scenario_id": self.scenario_id, "units": self.units,
                   "horizon_months": self.horizon_months,
                   "price_path": list(self.price_path), "summary": self.summary,
                   "assumptions": [item.as_dict() for item in self.assumptions],
                   "sensitivity": list(self.sensitivity), "checks": self.checks,
                   "status": self.status}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "schema": self.schema, "status": self.status,
            "module_id": self.module_id, "commodity": self.commodity,
            "scenario_id": self.scenario_id, "units": self.units,
            "horizon_months": self.horizon_months,
            "price_path": list(self.price_path), "summary": dict(self.summary),
            "assumptions": [item.as_dict() for item in self.assumptions],
            "sensitivity": [dict(item) for item in self.sensitivity],
            "checks": dict(self.checks), "meta": dict(self.meta),
            "digest": self.digest(),
        }

    def assumptions_by_class(self, source_class: str) -> tuple[Assumption, ...]:
        if source_class not in SOURCE_CLASSES:
            raise MarketError(f"unknown source_class {source_class!r}")
        return tuple(item for item in self.assumptions if item.source_class == source_class)


@dataclass(frozen=True)
class CostShock:
    """An MRIO-ready cost shock produced by a market module (no world-core changes needed)."""

    shock_type: str
    commodity: str
    brent_delta_usd: float
    brent_peak_usd: float
    brent_delta_pct: float
    sector_cost_multipliers: dict
    default_multiplier: float
    assumptions: tuple[Assumption, ...] = ()
    notes: str = ""
    status: str = RESEARCH_ONLY

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise MarketError("cost shocks are research-only")
        if not self.sector_cost_multipliers:
            raise MarketError("sector_cost_multipliers must not be empty")
        if any(value < 0.0 for value in self.sector_cost_multipliers.values()):
            raise MarketError("cost multipliers must be non-negative")

    def as_dict(self) -> dict:
        return {
            "schema": "amfe.markets.cost_shock.v1", "status": self.status,
            "shock_type": self.shock_type, "commodity": self.commodity,
            "brent_delta_usd": self.brent_delta_usd, "brent_peak_usd": self.brent_peak_usd,
            "brent_delta_pct": self.brent_delta_pct,
            "sector_cost_multipliers": dict(self.sector_cost_multipliers),
            "default_multiplier": self.default_multiplier,
            "assumptions": [item.as_dict() for item in self.assumptions],
            "notes": self.notes,
        }


class MarketModule(ABC):
    """Contract of one commodity module (passport-style metadata + scenarios + run)."""

    module_id: str = ""
    commodity: str = ""
    units: str = ""
    status: str = RESEARCH_ONLY

    def describe(self) -> dict:
        """Module passport: identity, units, scenarios, status, result schema (no values)."""
        scenarios = self.scenarios()
        return {
            "module_id": self.module_id,
            "commodity": self.commodity,
            "units": self.units,
            "status": self.status,
            "schema": RESULT_SCHEMA,
            "scenarios": [scenario.as_dict() for scenario in scenarios],
            "default_scenario": self.default_scenario().scenario_id,
            "assumption_classes": list(SOURCE_CLASSES),
            "research_only": True,
            "note": "research prototype: scenario arithmetic, not a forecast or an admission",
        }

    @abstractmethod
    def scenarios(self) -> tuple[Scenario, ...]:
        """The scenarios the module supports (deterministic order)."""

    def default_scenario(self) -> Scenario:
        scenarios = self.scenarios()
        if not scenarios:
            raise ScenarioError(f"{self.module_id}: no scenarios defined")
        return scenarios[0]

    def scenario(self, scenario_id: str) -> Scenario:
        for scenario in self.scenarios():
            if scenario.scenario_id == scenario_id:
                return scenario
        raise ScenarioError(f"{self.module_id}: unknown scenario {scenario_id!r}; "
                            f"available {[item.scenario_id for item in self.scenarios()]}")

    @abstractmethod
    def run(self, scenario_id: str | None = None, **overrides) -> MarketResult:
        """Run one scenario deterministically; returns a :class:`MarketResult`."""

    def prepare_cost_shock(self, result: MarketResult, **parameters) -> CostShock:
        """Translate a price result into an MRIO cost shock (module-specific)."""
        raise NotImplementedError(f"{self.module_id}: no cost-shock hook")


# ---------------------------------------------------------------------------------------
# Minimal module registry (framework-level, no data)
# ---------------------------------------------------------------------------------------
_MODULES: dict[str, MarketModule] = {}


def register(module: MarketModule) -> MarketModule:
    """Register a module instance under its ``module_id`` (idempotent for the same object)."""
    if not isinstance(module, MarketModule):
        raise MarketError("register expects a MarketModule instance")
    if not module.module_id:
        raise MarketError("module_id must be non-empty")
    existing = _MODULES.get(module.module_id)
    if existing is not None and existing is not module:
        raise MarketError(f"module {module.module_id!r} already registered")
    _MODULES[module.module_id] = module
    return module


def get_module(module_id: str) -> MarketModule:
    if module_id not in _MODULES:
        raise MarketError(f"unknown market module {module_id!r}; "
                          f"registered {sorted(_MODULES)}")
    return _MODULES[module_id]


def registered_modules() -> tuple[MarketModule, ...]:
    return tuple(_MODULES[module_id] for module_id in sorted(_MODULES))


def modules_document() -> list[dict]:
    return [module.describe() for module in registered_modules()]
