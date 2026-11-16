"""Market modules for AMFE 2.0: the commodity-market framework and its first module (oil).

The framework is deliberately small (docs/144, docs/145, docs/147):

* :mod:`amfe.markets.contract` -- ``MarketModule`` contract, assumptions with provenance,
  scenarios, ``MarketResult`` (price path + range + sensitivity + checks), the ``CostShock`` hook
  for the MRIO layer, and a module registry;
* :mod:`amfe.markets.engine` -- the deterministic monthly balance engine
  (supply / demand / bypass / stocks / price), with fail-closed unit and stock checks;
* :mod:`amfe.markets.oil` -- the first module: Brent response to a Strait of Hormuz closure,
  declared as assumptions (``source_class="assumption"``) and reported as a **range**.

Everything produced here is ``RESEARCH_ONLY``: scenario arithmetic, not a forecast, not an
admission decision and not investment advice.  No third-party observations are stored in the
package; parameters are declared assumptions with a ``replace_with`` list.
"""
from __future__ import annotations

from .contract import (
    RESEARCH_ONLY,
    RESULT_SCHEMA,
    SOURCE_CLASSES,
    Assumption,
    BalanceError,
    CostShock,
    DataMissing,
    MarketError,
    MarketModule,
    MarketResult,
    Scenario,
    ScenarioError,
    UnitError,
    get_module,
    modules_document,
    register,
    registered_modules,
)
from .engine import (
    PRICE_UNIT,
    QUANTITY_UNIT,
    STOCK_UNIT,
    MarketCalibration,
    MarketPath,
    Shock,
    flat,
    price_range,
    ramp,
    run_variants,
    simulate,
)
from .oil import (
    ASSUMPTION_SPECS,
    COMMODITY as OIL_COMMODITY,
    DEFAULT_PARAMS as OIL_DEFAULT_PARAMS,
    MODULE_ID as OIL_MODULE_ID,
    SENSITIVITY_VARIANTS,
    UNITS as OIL_UNITS,
    OilModule,
    assumptions as oil_assumptions,
    make_shock as oil_make_shock,
)

__all__ = [
    "ASSUMPTION_SPECS",
    "Assumption",
    "BalanceError",
    "COMMODITY",
    "CostShock",
    "DataMissing",
    "DEFAULT_PARAMS",
    "MODULE_ID",
    "MarketCalibration",
    "MarketError",
    "MarketModule",
    "MarketPath",
    "MarketResult",
    "OIL_COMMODITY",
    "OIL_DEFAULT_PARAMS",
    "OIL_MODULE_ID",
    "OIL_UNITS",
    "OilModule",
    "PRICE_UNIT",
    "QUANTITY_UNIT",
    "RESEARCH_ONLY",
    "RESULT_SCHEMA",
    "SENSITIVITY_VARIANTS",
    "SOURCE_CLASSES",
    "STOCK_UNIT",
    "Scenario",
    "ScenarioError",
    "Shock",
    "UnitError",
    "flat",
    "get_module",
    "make_shock",
    "modules_document",
    "oil_assumptions",
    "oil_make_shock",
    "price_range",
    "ramp",
    "register",
    "registered_modules",
    "run_variants",
    "simulate",
]

#: The default oil module instance, registered so the framework has one module out of the box.
OIL_MODULE = register(OilModule())
DEFAULT_MODULE_ID = OIL_MODULE_ID
