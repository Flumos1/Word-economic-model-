"""Integrated world core for AMFE-17 stages 2-5.

A single executable cycle that wires the four independent prototype layers:

* :mod:`amfe.accounts` - typed single-country sector accounts (stage 2);
* :mod:`amfe.international` - two countries, two currencies, BoP/IIP (stage 3);
* :mod:`amfe.resources` - physical IO/energy/labour limits (stage 4);
* :mod:`amfe.dynamics` - regimes, expectations, policy, debt identity (stage 5).

Public API::

    from amfe.world import WorldCore, WorldConfig, baseline_scenario
    core = WorldCore(WorldConfig(), baseline_scenario())
    result = core.run(periods=12)
    result.ok, result.reports[0].checks

Nothing in the four packages is modified; the integration uses their public APIs only and
reconciles the layers through explicit, testable checks (see
:mod:`amfe.world.integration`).  The full description, including the documented stub
(the domestic accounts kernel is closed-economy), is in ``docs/85_Мировое_ядро_интеграция.md``.
"""
from .config import (
    CountryConfig,
    PhysicalConfig,
    WorldConfig,
    WorldConfigError,
    default_country_a,
    default_country_b,
)
from .integration import (
    CountryWealthState,
    CompositeHook,
    IntegrationError,
    IntegrationHook,
    WorldError,
    assert_integration,
    is_hook,
)
from .model import WorldCore
from .physical import CountryPhysical, PhysicalOutcome
from .report import (
    CountryPeriodReport,
    WorldPeriodReport,
    WorldRunResult,
    canonical_json,
    report_digest,
)
from .scenarios import (
    WorldScenario,
    baseline_scenario,
    demand_shock_scenario,
    devaluation_scenario,
    energy_shock_scenario,
    labour_shock_scenario,
    standard_scenarios,
)

__all__ = [
    "WorldCore", "WorldConfig", "CountryConfig", "PhysicalConfig", "WorldConfigError",
    "WorldScenario", "baseline_scenario", "demand_shock_scenario",
    "energy_shock_scenario", "labour_shock_scenario", "devaluation_scenario",
    "standard_scenarios",
    "WorldError", "IntegrationError", "IntegrationHook", "CompositeHook",
    "CountryWealthState",
    "assert_integration", "is_hook",
    "CountryPhysical", "PhysicalOutcome",
    "CountryPeriodReport", "WorldPeriodReport", "WorldRunResult",
    "canonical_json", "report_digest",
    "default_country_a", "default_country_b",
]
