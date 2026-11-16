"""Agri-market archetype: contracts and shared value objects (docs/144, docs/145, docs/148).

The package is deliberately autonomous: the commodities framework (``amfe.markets``, track B) is an
*optional* dependency and is imported through ``try/except``.  If it is absent, local fallback
objects are used, so the agri module can be developed and tested independently.  Nothing in this
package is an admission candidate, a forecast, or a price target: every result is a research
prototype with ``source_class="assumption"`` and an explicit price *range* (never a point claim).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Dict, Mapping, Protocol, Tuple

SOURCE_CLASSES = ("assumption", "observed", "estimated", "model")
SCENARIO_KINDS = ("weather_yield_shock", "export_restriction", "freight_shock")
MODULE_ID = "AGRI_SOY"
MODULE_VERSION = "0.1.0"
NOT_A_FORECAST = True


class AgriMarketError(ValueError):
    """Invalid agri configuration/scenario or a non-clearing balance: fail closed."""


# Optional reuse of the commodity-market framework (track B).  The import must never be required.
try:  # pragma: no cover - depends on the concurrent track B package
    from amfe.markets.contracts import MarketModule as _ExternalMarketModule  # noqa: F401
    MARKET_FRAMEWORK_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "framework not available"
    _ExternalMarketModule = None
    MARKET_FRAMEWORK_AVAILABLE = False


@dataclass(frozen=True)
class PriceRange:
    """An assumption-based price interval; there is deliberately no point estimate."""

    low: float
    high: float
    unit: str = "USD/t"
    basis: str = "assumption_range"

    def __post_init__(self) -> None:
        for name in ("low", "high"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise AgriMarketError(f"price range {name} is not finite")
            if value <= 0.0:
                raise AgriMarketError(f"price range {name} must be positive")
        if float(self.low) > float(self.high):
            raise AgriMarketError("price range low must not exceed high")

    @property
    def width_pct(self) -> float:
        return 100.0 * (self.high - self.low) / self.low

    def to_dict(self) -> dict:
        return {"low": round(float(self.low), 6), "high": round(float(self.high), 6),
                "unit": self.unit, "basis": self.basis,
                "width_pct": round(float(self.width_pct), 6),
                "point_estimate": False, "not_a_forecast": NOT_A_FORECAST}


@dataclass(frozen=True)
class ScenarioShock:
    """One scenario shock in the common scenario language (docs/144 §1)."""

    kind: str
    target: str
    magnitude: float
    unit: str = "fraction"

    def __post_init__(self) -> None:
        if self.kind not in SCENARIO_KINDS:
            raise AgriMarketError(f"unknown scenario shock kind {self.kind!r}")
        magnitude = float(self.magnitude)
        if not math.isfinite(magnitude):
            raise AgriMarketError("scenario magnitude must be finite")
        if self.kind == "weather_yield_shock" and not (-1.0 < magnitude <= 0.0):
            raise AgriMarketError("weather_yield_shock magnitude must be in (-1, 0]")
        if self.kind == "export_restriction" and not (0.0 <= magnitude < 1.0):
            raise AgriMarketError("export_restriction magnitude must be in [0, 1)")
        if self.kind == "freight_shock" and magnitude < 0.0:
            raise AgriMarketError("freight_shock magnitude must be >= 0")
        if not str(self.target).strip():
            raise AgriMarketError("scenario target must be a non-empty string")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "target": self.target,
                "magnitude": round(float(self.magnitude), 12), "unit": self.unit}


@dataclass(frozen=True)
class AgriModuleResult:
    """Deterministic research result: ranges, effects and full disclosure of assumptions."""

    module_id: str
    version: str
    scenario: Mapping
    price_ranges: Mapping[str, PriceRange]
    effects: Mapping
    balance: Mapping
    seasonality: Mapping
    source_class: str = "assumption"
    notes: Tuple[str, ...] = field(default_factory=tuple)
    limitations: Tuple[str, ...] = field(default_factory=tuple)
    not_a_forecast: bool = NOT_A_FORECAST

    def __post_init__(self) -> None:
        if self.source_class not in SOURCE_CLASSES:
            raise AgriMarketError(f"unknown source_class {self.source_class!r}")
        if not self.price_ranges:
            raise AgriMarketError("result must contain at least one price range")
        for name, interval in self.price_ranges.items():
            if not isinstance(interval, PriceRange):
                raise AgriMarketError(f"price range {name!r} is not a PriceRange")

    def to_dict(self) -> dict:
        return {
            "module_id": self.module_id,
            "version": self.version,
            "source_class": self.source_class,
            "not_a_forecast": bool(self.not_a_forecast),
            "scenario": dict(self.scenario),
            "price_ranges": {name: interval.to_dict()
                             for name, interval in self.price_ranges.items()},
            "effects": dict(self.effects),
            "balance": dict(self.balance),
            "seasonality": dict(self.seasonality),
            "notes": list(self.notes),
            "limitations": list(self.limitations),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


class AgriMarketModule(Protocol):
    """Minimal contract analogous to the commodity-framework ``MarketModule`` (local fallback)."""

    module_id: str
    version: str

    def passport(self) -> Dict:  # pragma: no cover - interface only
        ...

    def validate(self) -> Dict:  # pragma: no cover - interface only
        ...

    def run(self) -> AgriModuleResult:  # pragma: no cover - interface only
        ...


def market_framework_available() -> bool:
    """True when the optional track-B ``amfe.markets`` contract could be imported."""
    return bool(MARKET_FRAMEWORK_AVAILABLE)


__all__ = [
    "AgriMarketError", "AgriMarketModule", "AgriModuleResult", "MODULE_ID", "MODULE_VERSION",
    "MARKET_FRAMEWORK_AVAILABLE", "NOT_A_FORECAST", "PriceRange", "SCENARIO_KINDS",
    "SOURCE_CLASSES", "ScenarioShock", "market_framework_available",
]
