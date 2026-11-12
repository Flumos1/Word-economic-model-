"""AMFE-17 stage 5: dynamic mechanisms (regimes, expectations, policy, debt, diffusion).

The package is a prototype layer for the dynamic blocks of docs/38 (blocks 11-17) and it keeps the
docs/38 (I10) contract explicit: every object is classified as an IDENTITY, a PHYSICAL_LIMIT, an
INSTITUTIONAL_RULE, a HYPOTHESIS or a SCENARIO, with its own admission rule (see
:mod:`amfe.dynamics.base`).  Hypotheses are falsifiable and name their tests; scenarios are
exogenous "what if" paths that never gate admission and never claim causality.

Module map:

* ``base``        - classification contract, ``Mechanism``, ``Scenario``, catalog validation;
* ``regimes``     - Markov regime switching (hypothesis) with a Hamilton filter;
* ``expectations``- adaptive expectations, rational-expectations benchmark, RLS learning;
* ``policy``      - inflation-target regime (institutional rule), Taylor rule (hypothesis) and a
                    small linearised three-equation system for local-stability checks;
* ``stability``   - Jacobian eigenvalues, local stability/instability classification and the
                    explicit caveat that this is not a crisis forecast;
* ``debt``        - the debt-ratio r-g identity and its stress scenario;
* ``geopolitics`` - geopolitical risk as an exogenous scenario with assumed loadings;
* ``diffusion``   - logistic/Bass S-curves (hypothesis) and the adoption-ceiling limit;
* ``catalog``     - the validated catalog of all mechanisms.

Everything is deterministic given an explicit seed and imports only numpy plus the standard
library; the package never reads protocols, data or the forecast/harness layers.
"""
from __future__ import annotations

from .base import (KIND_HYPOTHESIS, KIND_IDENTITY, KIND_INSTITUTIONAL_RULE,
                   KIND_LABELS_RU, KIND_PHYSICAL_LIMIT, KIND_SCENARIO, MECHANISM_KINDS,
                   DynamicsError, Mechanism, Scenario, catalog_table, mechanism_counts,
                   validate_catalog)
from .catalog import MECHANISM_CATALOG, catalog, counts, mechanism_by_id
from .debt import (DEBT_DYNAMICS_MECHANISM, DEBT_STRESS_MECHANISM, debt_path, debt_step,
                   debt_stress_scenario, stabilizing_primary_balance, sustainability)
from .diffusion import (ADOPTION_CEILING_MECHANISM, BASS_DIFFUSION_MECHANISM,
                        LOGISTIC_DIFFUSION_MECHANISM, LogisticDiffusion, bass_peak_time,
                        bass_simulate, diffusion_report)
from .expectations import (ADAPTIVE_EXPECTATIONS_MECHANISM, LEARNING_MECHANISM,
                           RATIONAL_EXPECTATIONS_MECHANISM, AdaptiveExpectations,
                           RationalExpectationsAR1, RecursiveLeastSquaresLearning)
from .geopolitics import (GEOPOLITICAL_RISK_MECHANISM, GeopoliticalRiskScenario,
                          risk_index_from_events, scenario_comparison)
from .policy import (INFLATION_TARGET_MECHANISM, TAYLOR_RULE_MECHANISM, InflationTargetRegime,
                     NewKeynesianDeterminacySystem, TaylorRule)
from .regimes import MARKOV_SWITCHING_MECHANISM, MarkovSwitchingModel
from .stability import (LOCAL_STABILITY_CAVEAT, analyse_jacobian, analyse_system,
                        numerical_jacobian, simulate_linear)

__all__ = [
    "KIND_IDENTITY", "KIND_PHYSICAL_LIMIT", "KIND_INSTITUTIONAL_RULE", "KIND_HYPOTHESIS",
    "KIND_SCENARIO", "MECHANISM_KINDS", "KIND_LABELS_RU", "DynamicsError", "Mechanism",
    "Scenario", "validate_catalog", "catalog_table", "mechanism_counts",
    "MECHANISM_CATALOG", "catalog", "counts", "mechanism_by_id",
    "MARKOV_SWITCHING_MECHANISM", "MarkovSwitchingModel",
    "ADAPTIVE_EXPECTATIONS_MECHANISM", "RATIONAL_EXPECTATIONS_MECHANISM", "LEARNING_MECHANISM",
    "AdaptiveExpectations", "RationalExpectationsAR1", "RecursiveLeastSquaresLearning",
    "TAYLOR_RULE_MECHANISM", "INFLATION_TARGET_MECHANISM", "TaylorRule",
    "InflationTargetRegime", "NewKeynesianDeterminacySystem",
    "LOCAL_STABILITY_CAVEAT", "numerical_jacobian", "analyse_jacobian", "analyse_system",
    "simulate_linear",
    "DEBT_DYNAMICS_MECHANISM", "DEBT_STRESS_MECHANISM", "debt_step", "debt_path",
    "stabilizing_primary_balance", "sustainability", "debt_stress_scenario",
    "GEOPOLITICAL_RISK_MECHANISM", "GeopoliticalRiskScenario", "risk_index_from_events",
    "scenario_comparison",
    "LOGISTIC_DIFFUSION_MECHANISM", "ADOPTION_CEILING_MECHANISM", "BASS_DIFFUSION_MECHANISM",
    "LogisticDiffusion", "bass_simulate", "bass_peak_time", "diffusion_report",
]
