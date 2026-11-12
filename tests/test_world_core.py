"""Executable tests for the integrated world core (AMFE-17 stages 2-5).

The suite covers:

* the step-by-step cycle (decisions -> physical production -> accounts -> external
  operations -> invariants -> report) and its typed interfaces;
* the cross-layer invariants: GDP physical-vs-monetary, ``dNW = savings + revaluation``,
  the debt identity from ``amfe.dynamics.debt``, material balances/no double counting,
  external mirroring and the national-wealth bridge;
* the four required scenarios (baseline, demand, energy, devaluation) on 12 and 24
  periods, plus determinism and the absence of pre-shock look-ahead;
* negative cases: invalid configuration, a non-productive IO matrix, zero monetisable
  output, consumption above the household budget, failing external hooks, invalid FX
  and tampered integration checks.

Run with the system interpreter::

    python tests/test_world_core.py
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.accounts import DEFAULT_CHART, check_report as check_accounts_report  # noqa: E402
from amfe.international import RecordingHook  # noqa: E402
from amfe.world import (  # noqa: E402
    CountryConfig,
    CountryPeriodReport,
    IntegrationError,
    PhysicalConfig,
    WorldConfig,
    WorldConfigError,
    WorldCore,
    WorldError,
    WorldPeriodReport,
    WorldRunResult,
    assert_integration,
    baseline_scenario,
    canonical_json,
    demand_shock_scenario,
    devaluation_scenario,
    energy_shock_scenario,
    is_hook,
    report_digest,
    standard_scenarios,
)


def _small_core(physical_a: PhysicalConfig | None = None,
                physical_b: PhysicalConfig | None = None,
                **kwargs) -> WorldCore:
    config = WorldConfig(
        country_a=CountryConfig(name="A", currency="AAA",
                                physical=physical_a or PhysicalConfig()),
        country_b=CountryConfig(name="B", currency="BBB", initial_fx_rate=2.0,
                                physical=physical_b or PhysicalConfig()),
        **kwargs)
    return WorldCore(config)


class TestConfigurationValidation(unittest.TestCase):
    def test_default_config_builds_core(self):
        core = WorldCore(WorldConfig(), baseline_scenario())
        description = core.describe()
        self.assertEqual(description["config"]["numeraire"], "AAA")
        self.assertEqual(description["scenario"]["name"], "baseline")
        self.assertEqual(sorted(description["config"]["countries"]), ["A", "B"])
        self.assertEqual(core.period, 0)
        self.assertEqual(core.reports, ())
        self.assertIsInstance(core.accounts("A").period, str)

    def test_demand_shares_must_sum_to_one(self):
        with self.assertRaises(WorldConfigError):
            PhysicalConfig(household_demand_share=0.5,
                           government_demand_share=0.2,
                           investment_demand_share=0.2)

    def test_non_productive_io_matrix_rejected(self):
        with self.assertRaises(WorldConfigError) as context:
            _small_core(PhysicalConfig(technical_matrix=((1.2, 0.3), (0.1, 0.4))))
        self.assertIn("productive", str(context.exception))

    def test_b2_labour_variant_requires_task_data(self):
        with self.assertRaises(WorldConfigError):
            _small_core(PhysicalConfig(labour_variant="B2"))

    def test_numeraire_country_fx_rate_must_be_one(self):
        with self.assertRaises(WorldConfigError):
            WorldConfig(country_a=CountryConfig(name="A", currency="AAA",
                                                initial_fx_rate=2.0))

    def test_invalid_scenario_factor_rejected(self):
        from amfe.world.scenarios import WorldScenario  # noqa: PLC0415

        for kwargs in ({"demand_factor": -0.1}, {"energy_factor": float("nan")}):
            with self.assertRaises(WorldConfigError):
                WorldScenario(name="bad", **kwargs)

    def test_invalid_fx_rate_path_rejected(self):
        from amfe.world.scenarios import WorldScenario  # noqa: PLC0415

        with self.assertRaises(WorldConfigError):
            WorldScenario(name="bad_fx", fx_rate_path={2: 0.0})
        with self.assertRaises(WorldConfigError):
            WorldScenario(name="bad_fx", fx_rate_path={-1: 1.5})

    def test_invalid_external_hook_rejected(self):
        with self.assertRaises(WorldConfigError):
            WorldCore(WorldConfig(), baseline_scenario(), external_hook=object())


class TestBaselineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = WorldCore(WorldConfig(), baseline_scenario())
        cls.run_result = cls.core.run(12)

    def test_run_reports_are_typed_and_ok(self):
        run = self.run_result
        self.assertIsInstance(run, WorldRunResult)
        self.assertEqual(run.periods, 12)
        self.assertEqual(len(run.reports), 12)
        self.assertTrue(run.ok)
        for report in run.reports:
            self.assertIsInstance(report, WorldPeriodReport)
            self.assertTrue(report.ok)
            self.assertEqual(sorted(report.countries), ["A", "B"])
            for country in report.countries.values():
                self.assertIsInstance(country, CountryPeriodReport)
                self.assertTrue(country.ok)
        self.assertGreater(len(run.reports[0].checks), 20)

    def test_gdp_three_measures_and_physical_valuation(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                country = report.countries[name]
                accounts = country.accounts
                self.assertEqual(accounts.gdp_production, country.nominal_gdp)
                self.assertEqual(accounts.gdp_production, accounts.gdp_expenditure)
                self.assertEqual(accounts.gdp_production, accounts.gdp_income)
                expected = country.price_level * Decimal(
                    str(round(country.physical.value_added, 12)))
                self.assertLess(abs(country.nominal_gdp - expected),
                                Decimal("1e-9"))
                self.assertEqual(country.consumption
                                 + country.government_consumption
                                 + country.investment, country.nominal_gdp)

    def test_net_worth_bridge_saving_plus_revaluation(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                country = report.countries[name]
                self.assertTrue(report.checks[f"{name}.net_worth_bridge_local"])
                self.assertLess(report.residuals[f"{name}.net_worth_bridge_local"],
                                1e-9)
                # The accounts layer itself certifies dNW = savings + revaluation.
                check_accounts_report(country.accounts)

    def test_material_balance_and_no_double_counting(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                country = report.countries[name]
                physical = country.physical
                self.assertLess(abs(physical.material_residual), 1e-9)
                self.assertLess(abs(physical.value_added_residual), 1e-9)
                # Gross output and value added are separate concepts: with an
                # intermediate-use matrix the former is strictly larger.
                self.assertGreater(physical.gross_output, physical.value_added)
                self.assertTrue(report.checks[f"{name}.physical_no_double_counting"])
                # The monetary layer counts value added only, not gross output.
                self.assertLess(country.nominal_gdp,
                                country.price_level * Decimal(
                                    str(round(physical.gross_output, 6))))

    def test_external_mirroring_and_nfa(self):
        for report in self.run_result.reports:
            self.assertTrue(report.external["checks"]["ok"])
            for value in report.external["mirror"].values():
                self.assertEqual(value, 0)
            nfa_a = report.countries["A"].external["iip"]["net"]
            nfa_b = report.countries["B"].external["iip"]["net"]
            self.assertEqual(nfa_a + nfa_b, 0)
            self.assertLess(report.residuals["external_nfa_mirror"], 1e-9)

    def test_national_wealth_bridge(self):
        for report in self.run_result.reports:
            self.assertTrue(report.checks["world_net_worth_equals_real_assets"])
            self.assertLess(report.residuals["world_net_worth_equals_real_assets"],
                            1e-9)
            for name in ("A", "B"):
                self.assertTrue(report.checks[f"{name}.national_wealth_bridge"])
                self.assertLess(report.residuals[f"{name}.national_wealth_bridge"],
                                1e-9)

    def test_debt_identity_and_target(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                self.assertTrue(report.checks[f"{name}.debt_identity"])
                self.assertTrue(report.checks[f"{name}.debt_flow_identity"])
                self.assertLess(report.residuals[f"{name}.debt_identity"], 1e-9)
                self.assertLess(report.residuals[f"{name}.debt_flow_identity"], 1e-9)
        final = self.run_result.last
        for name in ("A", "B"):
            self.assertLess(abs(final.countries[name].debt_ratio - 0.55), 0.05)

    def test_policy_and_expectations_consistency(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                country = report.countries[name]
                self.assertTrue(report.checks[f"{name}.policy_rule_consistent"])
                self.assertTrue(report.checks[f"{name}.expectations_consistent"])
                self.assertGreaterEqual(country.policy_rate, 0.0)
                self.assertLessEqual(country.policy_rate, 20.0)
                self.assertGreater(country.expected_inflation_next, -5.0)
                self.assertLess(country.expected_inflation_next, 15.0)
        dynamics = self.run_result.last.dynamics
        self.assertIn("nk_determinacy", dynamics)
        self.assertEqual(dynamics["nk_determinacy"]["A"]["classification"],
                         "DETERMINATE")
        self.assertIn("caveats", dynamics)
        self.assertEqual(dynamics["mechanism_counts"]["IDENTITY"], 1)

    def test_hook_events_match_trade(self):
        summary = self.run_result.reports[0].external["hook"]
        self.assertEqual(summary["claims"], 2)
        self.assertEqual(summary["flows"], 4)
        total_claims = sum(entry["numeraire_value"]
                           for entry in self.core.hook.claims)
        self.assertGreater(total_claims, 0)
        # Balanced trade: both holders' trade credits carry the same numeraire value.
        by_holder = {}
        for entry in self.core.hook.claims:
            by_holder[entry["holder"]] = (by_holder.get(entry["holder"], Decimal(0))
                                          + entry["numeraire_value"])
        self.assertLess(abs(by_holder["A"] - by_holder["B"]), Decimal("1e-9"))
        self.assertIsInstance(RecordingHook(), object)
        self.assertTrue(is_hook(self.core.hook))

    def test_no_external_double_counting_structural(self):
        # The domestic accounting kernel has no external trade accounts: external
        # flows are observed through the hook and reconciled at the wealth level, so
        # they cannot be counted a second time inside domestic GDP.
        kinds = DEFAULT_CHART.kinds
        self.assertFalse(any("export" in kind or "import" in kind for kind in kinds))
        for report in self.run_result.reports:
            for name in ("A", "B"):
                country = report.countries[name]
                self.assertGreater(country.exports_local, 0)
                self.assertLess(country.exports_local, country.nominal_gdp)
                self.assertGreater(country.exports_numeraire, 0)

    def test_report_is_json_ready_and_digest(self):
        payload = self.run_result.as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertGreater(len(text), 1000)
        self.assertEqual(len(self.run_result.digest), 64)
        self.assertEqual(payload["scenario"], "baseline")
        physical = payload["reports"][0]["countries"]["A"]["physical"]
        self.assertIn("output", physical)
        self.assertEqual(json.loads(canonical_json(payload)), payload)
        self.assertEqual(report_digest(list(self.run_result.reports)), self.run_result.digest)


class TestScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = WorldCore(WorldConfig(), baseline_scenario()).run(12)

    def test_demand_shock_reduces_output_and_keeps_invariants(self):
        shock = WorldCore(WorldConfig(), demand_shock_scenario()).run(12)
        self.assertTrue(shock.ok)
        base_gdp = self.baseline.last.countries["A"].nominal_gdp
        shock_gdp = shock.last.countries["A"].nominal_gdp
        self.assertLess(shock_gdp, base_gdp)
        self.assertLess(shock.last.countries["A"].physical.value_added,
                        self.baseline.last.countries["A"].physical.value_added)

    def test_energy_shock_binds_and_keeps_invariants(self):
        shock = WorldCore(WorldConfig(), energy_shock_scenario()).run(12)
        self.assertTrue(shock.ok)
        last = shock.last.countries["A"]
        self.assertLess(last.physical.result.scale, 1.0)
        self.assertIn("energy", last.physical.result.binding)
        self.assertTrue(shock.last.checks["A.energy_limit_respected"])
        self.assertLess(last.physical.value_added,
                        self.baseline.last.countries["A"].physical.value_added)

    def test_devaluation_revalues_external_positions(self):
        core = WorldCore(WorldConfig(), devaluation_scenario())
        run = core.run(12)
        self.assertTrue(run.ok)
        before = run.reports[2].countries["A"].external["iip"]["net"]
        after = run.reports[3].countries["A"].external["iip"]["net"]
        self.assertLess(abs(before), Decimal("1e-9"))
        self.assertGreater(after, Decimal(0))
        # FX revaluation is not a flow: the current account stays balanced.
        current_account = run.reports[3].countries["A"].external["current_account_total"]
        self.assertEqual(current_account, 0)
        self.assertGreater(len(core.hook.revaluations), 0)
        for report in run.reports:
            self.assertTrue(report.checks["external_iip_mirror"])
            self.assertEqual(report.countries["A"].external["iip"]["net"]
                             + report.countries["B"].external["iip"]["net"], 0)

    def test_devaluation_has_no_look_ahead_before_the_shock(self):
        devaluation = WorldCore(WorldConfig(), devaluation_scenario()).run(12)
        for period in range(3):  # the FX step happens at period index 3
            left = {key: value for key, value in
                    devaluation.reports[period].as_dict().items()
                    if key != "scenario"}
            right = {key: value for key, value in
                     self.baseline.reports[period].as_dict().items()
                     if key != "scenario"}
            self.assertEqual(left, right)

    def test_24_period_run(self):
        config = WorldConfig(max_periods=24)
        run = WorldCore(config, baseline_scenario()).run(24)
        self.assertTrue(run.ok)
        self.assertEqual(run.periods, 24)
        self.assertEqual(run.reports[-1].period, 23)

    def test_standard_scenarios_set(self):
        scenarios = standard_scenarios()
        self.assertEqual([scenario.name for scenario in scenarios],
                         ["baseline", "demand_shock", "energy_shock", "devaluation"])
        self.assertGreater(len({scenario.name for scenario in scenarios}), 3)

    def test_b0_labour_variant_runs(self):
        core = _small_core(PhysicalConfig(labour_variant="B0"),
                           PhysicalConfig(labour_variant="B0"))
        run = core.run(6)
        self.assertTrue(run.ok)
        labour = run.last.countries["A"].physical.result.labour
        self.assertEqual(labour["variant"], "B0")
        self.assertIsNone(labour["binding"])


class TestDeterminismAndInterfaces(unittest.TestCase):
    def test_same_seed_same_digest(self):
        first = WorldCore(WorldConfig(), baseline_scenario()).run(12)
        second = WorldCore(WorldConfig(), baseline_scenario()).run(12)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_different_seed_changes_the_path(self):
        first = WorldCore(WorldConfig(regime_seed=1), baseline_scenario()).run(12)
        second = WorldCore(WorldConfig(regime_seed=2), baseline_scenario()).run(12)
        self.assertNotEqual(first.digest, second.digest)
        self.assertTrue(first.ok and second.ok)

    def test_reset_reproduces_the_run(self):
        core = WorldCore(WorldConfig(), baseline_scenario())
        first = core.run(6).digest
        core.reset()
        second = core.run(6).digest
        self.assertEqual(first, second)

    def test_step_interface_and_report_structure(self):
        core = WorldCore(WorldConfig(), baseline_scenario())
        report = core.step()
        self.assertEqual(core.period, 1)
        self.assertIs(report, core.last_report)
        self.assertEqual(len(core.reports), 1)
        self.assertIsInstance(report.fx, type(report.fx))
        self.assertIn("AAA", report.fx)
        self.assertIsInstance(core.accounts("A"), object)
        self.assertTrue(hasattr(core.accounts("A"), "close_period"))
        self.assertIn("regime_state", report.dynamics)
        self.assertIn("policy_rule", report.dynamics)
        self.assertIn("debt_rule", report.dynamics)
        self.assertEqual(set(report.countries), {"A", "B"})


class TestNegativeCases(unittest.TestCase):
    def test_zero_energy_cannot_be_monetised(self):
        from amfe.world.scenarios import WorldScenario  # noqa: PLC0415

        core = WorldCore(WorldConfig(),
                         WorldScenario(name="zero_energy", energy_factor=0.0))
        with self.assertRaises(WorldError) as context:
            core.step()
        self.assertIn("no monetisable physical output", str(context.exception))

    def test_household_consumption_above_budget_is_rejected(self):
        physical = PhysicalConfig(household_demand_share=0.80,
                                  government_demand_share=0.10,
                                  investment_demand_share=0.10)
        core = _small_core(physical, physical)
        with self.assertRaises(WorldError) as context:
            core.step()
        self.assertIn("accounting invariants", str(context.exception))

    def test_failing_claim_hook_stops_the_core(self):
        class FailingClaimHook:
            def on_external_claim(self, **kwargs):
                raise RuntimeError("claim hook boom")

            def on_external_flow(self, **kwargs):
                pass

            def on_revaluation(self, **kwargs):
                pass

        core = WorldCore(WorldConfig(), baseline_scenario(),
                         external_hook=FailingClaimHook())
        with self.assertRaises(WorldError) as context:
            core.step()
        self.assertIn("claim hook boom", str(context.exception))

    def test_failing_revaluation_hook_stops_the_core(self):
        class FailingRevaluationHook:
            def on_external_claim(self, **kwargs):
                pass

            def on_external_flow(self, **kwargs):
                pass

            def on_revaluation(self, **kwargs):
                raise RuntimeError("revaluation hook boom")

        core = WorldCore(WorldConfig(), devaluation_scenario(),
                         external_hook=FailingRevaluationHook())
        with self.assertRaises(WorldError) as context:
            core.run(6)
        self.assertIn("revaluation hook boom", str(context.exception))

    def test_run_beyond_configured_horizon_is_rejected(self):
        core = WorldCore(WorldConfig(max_periods=3), baseline_scenario())
        with self.assertRaises(WorldError):
            core.run(4)
        core.run(3)
        with self.assertRaises(WorldError):
            core.step()

    def test_integration_assertion_detects_tampering(self):
        run = WorldCore(WorldConfig(), baseline_scenario()).run(3)
        report = run.last
        tampered = dict(report.checks)
        tampered["A.debt_identity"] = False
        with self.assertRaises(IntegrationError):
            assert_integration(tampered, report.residuals)
        with self.assertRaises(IntegrationError):
            assert_integration({}, None)
        with self.assertRaises(IntegrationError):
            assert_integration({"only_check": False}, {"only_check": 1.0})

    def test_external_hook_receives_events(self):
        hook = RecordingHook()
        core = WorldCore(WorldConfig(), devaluation_scenario(), external_hook=hook)
        run = core.run(6)
        self.assertTrue(run.ok)
        self.assertEqual(len(hook.claims), 12)      # two balanced trades per period
        self.assertEqual(len(hook.flows), 24)       # exporter/importer flow per trade
        self.assertGreater(len(hook.revaluations), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
