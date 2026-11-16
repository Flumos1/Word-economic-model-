"""Agri-market archetype + soybean-in-Africa pilot (docs/144, docs/145, docs/148).

Autonomous research prototype for the commodity-agri archetype: yield/weather, seasonality,
stocks-to-use balance, crush, substitutes, export restrictions and freight.  The pilot is a range
sensitivity for soybeans/soymeal and the African import bill; it is **not a forecast** and not an
admission candidate.

Public API::

    from amfe.markets_agri import SoyAgriModule, SoyConfig, SoyScenario, default_regions
    module = SoyAgriModule()
    module.passport(), module.validate()
    result = module.run()            # AgriModuleResult
    result.to_dict(), result.to_json(), result.digest()
    # result.price_ranges["soybeans"].low/high, result.effects["africa"], ...

The optional ``amfe.markets`` framework (track B) is imported through a guarded try/except in
:mod:`amfe.markets_agri.contracts`; the package works without it.
"""
from .africa import AfricaConfig, compute_africa_impact  # noqa: F401
from .balance import (CrushParams, GlobalBalance, SoyRegion, build_global_balance,  # noqa: F401
                      crush_margin, effective_elasticity)
from .contracts import (AgriMarketError, AgriMarketModule, AgriModuleResult,  # noqa: F401
                        MARKET_FRAMEWORK_AVAILABLE, MODULE_ID, MODULE_VERSION, NOT_A_FORECAST,
                        PriceRange, SCENARIO_KINDS, SOURCE_CLASSES, ScenarioShock,
                        market_framework_available)
from .pricing import (ScenarioParameters, import_bill_range, import_unit_cost,  # noqa: F401
                      meal_price_range, price_range_from_corners)
from .seasonality import describe_seasonality, seasonal_factor, seasonal_path  # noqa: F401
from .solver import SolverResult, solve_price_multiplier  # noqa: F401
from .soy import (SoyAgriModule, SoyConfig, SoyScenario, default_regions,  # noqa: F401
                  module_passport, run_pilot)

__all__ = [
    "AfricaConfig", "AgriMarketError", "AgriMarketModule", "AgriModuleResult", "CrushParams",
    "GlobalBalance", "MARKET_FRAMEWORK_AVAILABLE", "MODULE_ID", "MODULE_VERSION", "NOT_A_FORECAST",
    "PriceRange", "SCENARIO_KINDS", "SOURCE_CLASSES", "ScenarioParameters", "ScenarioShock",
    "SolverResult", "SoyAgriModule", "SoyConfig", "SoyRegion", "SoyScenario",
    "build_global_balance", "compute_africa_impact", "crush_margin", "default_regions",
    "describe_seasonality", "effective_elasticity", "import_bill_range", "import_unit_cost",
    "market_framework_available", "meal_price_range", "module_passport",
    "price_range_from_corners", "run_pilot", "seasonal_factor", "seasonal_path",
    "solve_price_multiplier",
]
