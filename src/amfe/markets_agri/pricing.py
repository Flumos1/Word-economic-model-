"""Price formation for the agri archetype: balance -> price range, meal and import cost.

No point forecast is produced.  The module solves the deterministic clearing problem twice, at the
two pre-declared corners of the assumption bounds (low-impact and high-impact), and reports the
resulting interval.  The same corners drive soybean/soymeal price ranges and the African import-cost
range, so every number is traceable to one of the two runs and to the documented parameters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .balance import CrushParams, GlobalBalance, effective_elasticity
from .contracts import AgriMarketError, PriceRange
from .solver import SolverResult, solve_price_multiplier


@dataclass(frozen=True)
class ScenarioParameters:
    """Pre-declared assumption bounds; the two corners define the reported range."""

    elasticity_low: float = 0.20
    elasticity_high: float = 0.40
    substitution_low: float = 0.20
    substitution_high: float = 0.70
    friction_low: float = 0.20
    friction_high: float = 0.50
    target_stocks_use_low: float = 0.18
    target_stocks_use_high: float = 0.22
    freight_addon_low: float = 0.10
    freight_addon_high: float = 0.25
    meal_sensitivity_low: float = 1.00
    meal_sensitivity_high: float = 1.10

    def __post_init__(self) -> None:
        pairs = (("elasticity", self.elasticity_low, self.elasticity_high, 0.0),
                 ("substitution", self.substitution_low, self.substitution_high, 0.0),
                 ("friction", self.friction_low, self.friction_high, 0.0),
                 ("target_stocks_use", self.target_stocks_use_low, self.target_stocks_use_high, 0.0),
                 ("freight_addon", self.freight_addon_low, self.freight_addon_high, 0.0),
                 ("meal_sensitivity", self.meal_sensitivity_low, self.meal_sensitivity_high, 0.0))
        for name, low, high, minimum in pairs:
            low, high = float(low), float(high)
            if not (math.isfinite(low) and math.isfinite(high)):
                raise AgriMarketError(f"{name} bounds must be finite")
            if low <= minimum or high <= minimum:
                raise AgriMarketError(f"{name} bounds must be > {minimum}")
            if low > high:
                raise AgriMarketError(f"{name} low bound must not exceed high")
        if self.substitution_high >= 1.0:
            raise AgriMarketError("substitution_high must be < 1")

    def corner(self, name: str) -> dict:
        """Pre-declared low/high-impact corner (no tuning against any evaluation)."""
        if name == "low":
            return {
                "elasticity": float(self.elasticity_high),
                "substitution": float(self.substitution_high),
                "friction": float(self.friction_low),
                "target_stocks_use": float(self.target_stocks_use_high),
                "freight_addon": float(self.freight_addon_low),
                "meal_sensitivity": float(self.meal_sensitivity_low),
            }
        if name == "high":
            return {
                "elasticity": float(self.elasticity_low),
                "substitution": float(self.substitution_low),
                "friction": float(self.friction_high),
                "target_stocks_use": float(self.target_stocks_use_low),
                "freight_addon": float(self.freight_addon_high),
                "meal_sensitivity": float(self.meal_sensitivity_high),
            }
        raise AgriMarketError(f"unknown scenario corner {name!r}")

    def to_dict(self) -> dict:
        return {
            "elasticity": [float(self.elasticity_low), float(self.elasticity_high)],
            "substitution": [float(self.substitution_low), float(self.substitution_high)],
            "export_friction": [float(self.friction_low), float(self.friction_high)],
            "target_stocks_use": [float(self.target_stocks_use_low),
                                  float(self.target_stocks_use_high)],
            "freight_addon": [float(self.freight_addon_low), float(self.freight_addon_high)],
            "meal_sensitivity": [float(self.meal_sensitivity_low),
                                 float(self.meal_sensitivity_high)],
        }


def scenario_available_supply(*, balance: GlobalBalance, shocked_balance: GlobalBalance,
                              restricted_volume_mt: float, friction: float) -> float:
    """Available global supply after a yield shock and an export restriction with trade friction."""
    restricted = float(restricted_volume_mt)
    friction = float(friction)
    if not math.isfinite(restricted) or restricted < 0.0:
        raise AgriMarketError("restricted volume must be finite and non-negative")
    if not math.isfinite(friction) or not (0.0 <= friction < 1.0):
        raise AgriMarketError("friction must be in [0, 1)")
    shocked_balance.validate()
    available = shocked_balance.available_mt - friction * restricted
    if available <= 0.0:
        raise AgriMarketError("effective available supply is not positive after the shocks")
    return available


def solve_corner(*, balance: GlobalBalance, shocked_balance: GlobalBalance,
                 restricted_volume_mt: float, parameters: ScenarioParameters,
                 corner: str) -> dict:
    """Solve the clearing problem at one pre-declared corner; returns multiplier and diagnostics."""
    selection = parameters.corner(corner)
    available = scenario_available_supply(
        balance=balance, shocked_balance=shocked_balance,
        restricted_volume_mt=restricted_volume_mt, friction=selection["friction"])
    elasticity = effective_elasticity(selection["elasticity"], selection["substitution"])
    result: SolverResult = solve_price_multiplier(
        baseline_use_mt=balance.use_mt, available_supply_mt=available,
        target_stocks_use=selection["target_stocks_use"], elasticity=elasticity)
    return {"corner": corner, "parameters": selection, "available_supply_mt": available,
            "elasticity_effective": elasticity, "solver": result,
            "freight_addon": selection["freight_addon"],
            "meal_sensitivity": selection["meal_sensitivity"],
            "target_stocks_use": selection["target_stocks_use"]}


def price_range_from_corners(*, base_price_usd_t: float, low: dict, high: dict) -> PriceRange:
    """Range of ``base_price * multiplier`` at the low/high corners."""
    base = float(base_price_usd_t)
    if not math.isfinite(base) or base <= 0.0:
        raise AgriMarketError("base price must be finite and positive")
    low_price = base * float(low["solver"].multiplier)
    high_price = base * float(high["solver"].multiplier)
    return PriceRange(low=low_price, high=high_price, unit="USD/t", basis="scenario_range")


def meal_price_range(*, base_meal_price_usd_t: float, low: dict, high: dict) -> PriceRange:
    """Soymeal follows the bean multiplier with a pre-declared meal sensitivity band."""
    base = float(base_meal_price_usd_t)
    if not math.isfinite(base) or base <= 0.0:
        raise AgriMarketError("meal base price must be finite and positive")
    low_price = base * float(low["solver"].multiplier) * float(low["meal_sensitivity"])
    high_price = base * float(high["solver"].multiplier) * float(high["meal_sensitivity"])
    return PriceRange(low=low_price, high=high_price, unit="USD/t", basis="scenario_range")


def import_unit_cost(*, price_usd_t: float, freight_usd_t: float,
                     import_margin: float) -> float:
    """CIF + freight + importer margin unit cost (USD/t)."""
    price, freight, margin = float(price_usd_t), float(freight_usd_t), float(import_margin)
    if not all(math.isfinite(value) for value in (price, freight, margin)):
        raise AgriMarketError("import cost inputs must be finite")
    if price <= 0.0 or freight < 0.0:
        raise AgriMarketError("price must be positive and freight non-negative")
    if margin < 1.0:
        raise AgriMarketError("import margin must be >= 1")
    return (price + freight) * margin


def import_bill_range(*, volume_mt: float, price_range: PriceRange, freight_usd_t: float,
                      import_margin: float) -> dict:
    """Import bill at the low/high price corners (USD million)."""
    volume = float(volume_mt)
    if not math.isfinite(volume) or volume < 0.0:
        raise AgriMarketError("import volume must be finite and non-negative")
    low_unit = import_unit_cost(price_usd_t=price_range.low, freight_usd_t=freight_usd_t,
                                import_margin=import_margin)
    high_unit = import_unit_cost(price_usd_t=price_range.high, freight_usd_t=freight_usd_t,
                                 import_margin=import_margin)
    return {"volume_mt": round(volume, 6),
            "unit_cost_low_usd_t": round(low_unit, 6),
            "unit_cost_high_usd_t": round(high_unit, 6),
            "bill_low_usd_mn": round(volume * low_unit, 6),
            "bill_high_usd_mn": round(volume * high_unit, 6)}


__all__ = ["ScenarioParameters", "import_bill_range", "import_unit_cost", "meal_price_range",
           "price_range_from_corners", "scenario_available_supply", "solve_corner"]
