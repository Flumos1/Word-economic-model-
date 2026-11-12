"""AMFE stage 4: real economy and physical resources.

An independent module for the physical side of the model, deliberately kept outside the
accounting core (stage 2) and the two-country/currency prototype (stage 3):

* :mod:`amfe.resources.units` - dimensions, units and the POTENTIAL/ACCESSIBLE/UTILIZED states;
* :mod:`amfe.resources.io` - Leontief input-output model, productiveness, gross output;
* :mod:`amfe.resources.logistics` - supply-chain nodes/edges, transport delays and inventories;
* :mod:`amfe.resources.energy` - primary-resource balances and physical output limits;
* :mod:`amfe.resources.labour` - labour endowment and the B0/B1/B2 constraint variants;
* :mod:`amfe.resources.balances` - material balances and the no-double-counting invariant;
* :mod:`amfe.resources.scenario` - combined demand/energy/labour shock scenarios.

Ground rules (docs/35, docs/37, docs/38, docs/42 stage 4):

* physical quantities, not money: no monetary valuation of a physical limit;
* no double counting: gross output, intermediate use and value added are separate vectors;
* productiveness of the IO matrix is checked, never assumed;
* no labour theory of value: hours constrain activity, they do not determine output value;
* labour and resource states are compared only in one dimension, horizon and aggregation;
* the B0/B1/B2 variants are compared on independent parameters and never ranked before an
  out-of-sample test (docs/37 R06).

The package is pure standard-library Python: the CI runner installs no scientific stack.
"""

from .units import (  # noqa: F401
    DEFAULT_UNITS,
    DimensionMismatch,
    ResourceError,
    ResourceQuantity,
    ResourceState,
    StateOrderError,
    Unit,
    UnitRegistry,
    check_state_order,
)
from .io import (  # noqa: F401
    LeontiefError,
    LeontiefModel,
    inverse_matrix,
    matvec,
    solve_linear,
    spectral_radius,
)
from .logistics import (  # noqa: F401
    LogisticsEdge,
    LogisticsError,
    LogisticsNode,
    SupplyChain,
    StepReport,
    chain_topology,
    supply_shock,
)
from .energy import (  # noqa: F401
    InputConstraint,
    ResourceBalance,
    ResourceBalanceError,
    ResourceLimitResult,
    apply_input_limit,
    energy_balance_from_io,
)
from .labour import (  # noqa: F401
    AggregateLabourConstraint,
    LabourError,
    LabourModel,
    LabourStates,
    NoLabourConstraint,
    TaskLabourConstraint,
    compare_labour_variants,
    employment_from_hours,
    unemployment_rate,
)
from .balances import (  # noqa: F401
    BalanceError,
    MaterialBalances,
    ProductBalance,
    balances_from_io,
    check_no_double_counting,
    check_non_negative,
)
from .scenario import (  # noqa: F401
    Scenario,
    ScenarioError,
    ScenarioResult,
    compare_scenarios,
    run_scenario,
    standard_shock_set,
)

__all__ = [
    "DEFAULT_UNITS", "DimensionMismatch", "ResourceError", "ResourceQuantity",
    "ResourceState", "StateOrderError", "Unit", "UnitRegistry", "check_state_order",
    "LeontiefError", "LeontiefModel", "inverse_matrix", "matvec", "solve_linear",
    "spectral_radius",
    "LogisticsEdge", "LogisticsError", "LogisticsNode", "SupplyChain", "StepReport",
    "chain_topology", "supply_shock",
    "InputConstraint", "ResourceBalance", "ResourceBalanceError", "ResourceLimitResult",
    "apply_input_limit", "energy_balance_from_io",
    "AggregateLabourConstraint", "LabourError", "LabourModel", "LabourStates",
    "NoLabourConstraint", "TaskLabourConstraint", "compare_labour_variants",
    "employment_from_hours", "unemployment_rate",
    "BalanceError", "MaterialBalances", "ProductBalance", "balances_from_io",
    "check_no_double_counting", "check_non_negative",
    "Scenario", "ScenarioError", "ScenarioResult", "compare_scenarios", "run_scenario",
    "standard_shock_set",
]
