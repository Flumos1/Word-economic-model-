#!/usr/bin/env python3
"""Tests of the behavioural demand MVP (``src/amfe/world/demand_behavior.py``, docs/167).

The module is offline and deterministic: no data files, no network, no random numbers.  The suite
checks the research-only contract, the budget identities, the behavioural limits (zero shock -> zero,
sign/monotonicity, bounds), the optional integration flag (default must reproduce the unchanged
reduced form), determinism, and a diagnostic-only comparison with the reduced-form channel.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets import demand as reduced_form  # noqa: E402
from amfe.world import demand_behavior as db  # noqa: E402

#: Illustrative base demand of a world final-demand basket (shares 10/10/30/50):
#: energy 1, transport 1, manufacturing 3, services 5.
BASE = [1.0, 1.0, 3.0, 5.0]
GROUPS = list(db.GROUPS)
ZERO = [0.0, 0.0, 0.0, 0.0]
ENERGY_SHOCK = [0.05917, 0.0, 0.0, 0.0]  # +5.917% energy (FIGARO aggregate dp at pass-through 0.6)


def response(price_changes=ENERGY_SHOCK, base_quantities=BASE, **kwargs):
    return db.behavioral_demand_response(price_changes, base_quantities, GROUPS, **kwargs)


class ContractTest(unittest.TestCase):
    def test_status_research_only_and_no_admission(self):
        out = response()
        self.assertEqual(out["schema"], db.SCHEMA)
        self.assertEqual(out["status"], "RESEARCH_ONLY")
        self.assertIs(out["admission"], False)
        self.assertIs(out["checks"]["research_only"], True)
        self.assertIs(out["checks"]["admission"], False)
        self.assertIn("MVP", " ".join(out["limitations"]))
        for item in out["assumptions"]:
            self.assertIn(item["source_class"], db.SOURCE_CLASSES)
            self.assertTrue(item["name"])

    def test_output_is_deterministic_and_json_serialisable(self):
        first = response()
        second = response()
        self.assertEqual(first, second)
        text = json.dumps(first, sort_keys=True, allow_nan=False)
        self.assertGreater(len(text), 1000)
        self.assertNotIn("NaN", text)

    def test_output_shape_is_swappable_with_the_reduced_form(self):
        out = response()
        for key in ("aggregate", "monthly", "by_sector", "checks"):
            self.assertIn(key, out)
        self.assertEqual(len(out["monthly"]), 6)
        for item in out["by_sector"]:
            for key in ("base_quantity", "delta_short", "delta_long",
                        "relative_short", "relative_long", "group"):
                self.assertIn(key, item)
        for item in out["monthly"]:
            for key in ("month", "delta_demand", "relative"):
                self.assertIn(key, item)
        for key in ("base_quantity", "delta_short", "delta_long",
                    "relative_short", "relative_long"):
            self.assertIn(key, out["aggregate"])


class LimitsTest(unittest.TestCase):
    def test_zero_shock_gives_exactly_zero_change(self):
        out = db.behavioral_demand_response(ZERO, BASE, GROUPS)
        self.assertIs(out["checks"]["zero_shock_zero_change"], True)
        for item in out["by_sector"]:
            self.assertEqual(item["relative_short"], 0.0)
            self.assertEqual(item["relative_long"], 0.0)
            self.assertEqual(item["delta_short"], 0.0)
            self.assertEqual(item["delta_long"], 0.0)
        self.assertEqual(out["aggregate"]["relative_short"], 0.0)
        self.assertEqual(out["aggregate"]["relative_long"], 0.0)

    def test_energy_price_rise_reduces_energy_demand_and_checks_sign(self):
        out = response()
        energy = next(item for item in out["by_sector"] if item["group"] == "energy")
        self.assertLess(energy["relative_long"], 0.0)
        self.assertLess(energy["relative_short"], 0.0)
        self.assertIs(out["checks"]["energy_sign_consistent"], True)

    def test_larger_shock_gives_larger_response(self):
        small = response([0.01, 0.0, 0.0, 0.0])
        large = response([0.10, 0.0, 0.0, 0.0])
        self.assertGreater(abs(large["aggregate"]["relative_long"]),
                           abs(small["aggregate"]["relative_long"]))
        small_energy = next(item for item in small["by_sector"] if item["group"] == "energy")
        large_energy = next(item for item in large["by_sector"] if item["group"] == "energy")
        self.assertLess(large_energy["relative_long"], small_energy["relative_long"])

    def test_higher_substitution_elasticity_gives_stronger_response(self):
        low = response(config=db.BehavioralDemandConfig(sigma_household=0.4, sigma_firm=0.2))
        high = response(config=db.BehavioralDemandConfig(sigma_household=1.2, sigma_firm=0.8))
        self.assertLess(high["aggregate"]["relative_long"], low["aggregate"]["relative_long"])
        low_energy = next(item for item in low["by_sector"] if item["group"] == "energy")
        high_energy = next(item for item in high["by_sector"] if item["group"] == "energy")
        self.assertLess(high_energy["relative_long"], low_energy["relative_long"])

    def test_short_run_is_smaller_than_long_run_for_a_permanent_shock(self):
        out = response()
        energy = next(item for item in out["by_sector"] if item["group"] == "energy")
        self.assertGreater(abs(energy["relative_long"]), abs(energy["relative_short"]))
        self.assertGreater(abs(out["aggregate"]["relative_long"]),
                           abs(out["aggregate"]["relative_short"]))
        self.assertIs(out["checks"]["ramp_monotone_permanent"], True)

    def test_ramp_when_shock_is_temporary_reverts(self):
        permanent = response()
        temporary = response(config=db.BehavioralDemandConfig(shock_duration_months=2))
        permanent_energy = next(item for item in permanent["by_sector"] if item["group"] == "energy")
        temporary_energy = next(item for item in temporary["by_sector"] if item["group"] == "energy")
        self.assertLess(abs(temporary_energy["relative_long"]),
                        abs(permanent_energy["relative_long"]))
        # documented: the monotonicity check is not defined for a temporary shock
        self.assertIsNone(temporary["checks"]["ramp_monotone_permanent"])


class BudgetAndAccountingTest(unittest.TestCase):
    def test_household_budget_identities_hold(self):
        out = response()
        self.assertIs(out["checks"]["household_within_period_budget"], True)
        self.assertIs(out["checks"]["household_intertemporal_budget"], True)
        self.assertLessEqual(out["checks"]["budget_error_max"], 1e-9)

    def test_household_and_firm_base_split_sums_to_base(self):
        out = response()
        for item in out["by_sector"]:
            self.assertAlmostEqual(item["household_base"] + item["firm_base"],
                                   item["base_quantity"], places=9)
            self.assertGreaterEqual(item["household_base"], 0.0)
            self.assertGreaterEqual(item["firm_base"], 0.0)
        total = sum(item["base_quantity"] for item in out["by_sector"])
        self.assertAlmostEqual(out["aggregate"]["base_quantity"], total, places=9)

    def test_implied_elasticities_follow_the_analytic_formulas(self):
        config = db.BehavioralDemandConfig()
        table = db.implied_long_run_elasticities(BASE, GROUPS, config)
        household_energy = sum(BASE[index] * (1 - config.firm_share("energy"))
                               for index, group in enumerate(GROUPS) if group == "energy")
        household_total = sum(BASE[index] * (1 - config.firm_share(group))
                              for index, group in enumerate(GROUPS))
        share = household_energy / household_total
        expected_household = -config.sigma_household * (1 - share) - share
        expected_firm = -config.sigma_firm * (1 - config.energy_cost_share)
        expected_aggregate = ((1 - config.firm_share("energy")) * expected_household
                              + config.firm_share("energy") * expected_firm)
        self.assertAlmostEqual(table["energy"]["household"], expected_household, places=9)
        self.assertAlmostEqual(table["energy"]["firm"], expected_firm, places=9)
        self.assertAlmostEqual(table["energy"]["aggregate_long_run"], expected_aggregate, places=9)

    def test_small_shock_matches_the_implied_long_run_elasticity(self):
        tiny = response([1e-4, 0.0, 0.0, 0.0])
        energy = next(item for item in tiny["by_sector"] if item["group"] == "energy")
        implied = tiny["implied_elasticities"]["energy"]["aggregate_long_run"]
        self.assertAlmostEqual(energy["implied_long_elasticity"], implied, places=3)

    def test_energy_implied_elasticity_is_a_calibration_anchor_to_the_reduced_form(self):
        table = db.implied_long_run_elasticities(BASE, GROUPS)
        # the declared anchor: behavioural aggregate energy elasticity ~ reduced-form long-run -0.55
        self.assertAlmostEqual(table["energy"]["aggregate_long_run"],
                               reduced_form.FINAL_DEMAND_ELASTICITIES["energy"]["long"],
                               delta=0.02)
        # and the month-1 frictional anchor is close to the reduced-form short-run -0.15
        out = response()
        energy = next(item for item in out["by_sector"] if item["group"] == "energy")
        self.assertAlmostEqual(energy["implied_short_elasticity"],
                               reduced_form.FINAL_DEMAND_ELASTICITIES["energy"]["short"],
                               delta=0.05)

    def test_firm_energy_cost_share_changes_only_the_firm_elasticity(self):
        low = db.implied_long_run_elasticities(BASE, GROUPS,
                                               db.BehavioralDemandConfig(energy_cost_share=0.05))
        high = db.implied_long_run_elasticities(BASE, GROUPS,
                                                db.BehavioralDemandConfig(energy_cost_share=0.20))
        self.assertGreater(high["energy"]["firm"], low["energy"]["firm"])
        self.assertAlmostEqual(low["transport"]["aggregate_long_run"],
                               high["transport"]["aggregate_long_run"], places=12)


class IntegrationFlagTest(unittest.TestCase):
    def test_default_mode_reproduces_the_reduced_form_exactly(self):
        for price, horizon, ramp in ((ENERGY_SHOCK, 6, None), ([0.02, 0.0, -0.01, 0.0], 6, 3)):
            expected = reduced_form.demand_response(price, BASE, GROUPS, horizon_months=horizon,
                                                    ramp_months=ramp)
            actual = db.demand_dispatch(price, BASE, GROUPS, horizon_months=horizon,
                                        ramp_months=ramp)
            self.assertEqual(actual, expected)
        self.assertEqual(db.DEFAULT_MODE, "reduced_form")
        self.assertEqual(sorted(db.MODES), ["behavioral", "reduced_form"])

    def test_behavioural_mode_is_opt_in_and_unknown_modes_fail_closed(self):
        behavioural = db.demand_dispatch(ENERGY_SHOCK, BASE, GROUPS, mode="behavioral")
        self.assertEqual(behavioural["schema"], db.SCHEMA)
        self.assertEqual(behavioural["status"], "RESEARCH_ONLY")
        with self.assertRaises(db.BehaviorConfigError):
            db.demand_dispatch(ENERGY_SHOCK, BASE, GROUPS, mode="semi-behavioral")

    def test_horizon_override_follows_the_reduced_form_convention(self):
        direct = db.behavioral_demand_response(ENERGY_SHOCK, BASE, GROUPS, horizon_months=12)
        dispatch = db.demand_dispatch(ENERGY_SHOCK, BASE, GROUPS, mode="behavioral",
                                      horizon_months=12)
        self.assertEqual(dispatch, direct)
        self.assertEqual(dispatch["ramp_months"], 12)
        self.assertEqual(dispatch["shock_duration_months"], 12)
        self.assertEqual(len(dispatch["monthly"]), 12)

    def test_comparison_with_reduced_form_is_diagnostic_only(self):
        comparison = db.compare_to_reduced_form(ENERGY_SHOCK, BASE, GROUPS)
        self.assertIs(comparison["diagnostic_only"], True)
        self.assertIs(comparison["validation"], False)
        self.assertIn("NOT validation", comparison["note"])
        for key in ("relative_short", "relative_long"):
            self.assertIn("behavioural", comparison["aggregate"][key])
            self.assertIn("reduced_form", comparison["aggregate"][key])
            self.assertIn("difference", comparison["aggregate"][key])
        self.assertEqual(len(comparison["by_group"]), len(GROUPS))

    def test_importing_the_module_does_not_touch_the_reduced_form_defaults(self):
        before = json.dumps(reduced_form.FINAL_DEMAND_ELASTICITIES, sort_keys=True)
        db.behavioral_demand_response(ENERGY_SHOCK, BASE, GROUPS)
        after = json.dumps(reduced_form.FINAL_DEMAND_ELASTICITIES, sort_keys=True)
        self.assertEqual(before, after)


class FailClosedTest(unittest.TestCase):
    def test_invalid_inputs_are_refused(self):
        bad_cases = [
            ([0.0, 0.0], BASE, GROUPS),  # length mismatch
            (ZERO, [-1.0, 1.0, 3.0, 5.0], GROUPS),  # negative base
            (ZERO, [0.0, 0.0, 0.0, 0.0], GROUPS),  # zero total base
            ([-1.0, 0.0, 0.0, 0.0], BASE, GROUPS),  # price change <= -100%
            ([5.5, 0.0, 0.0, 0.0], BASE, GROUPS),  # price change above the declared bound
            (ZERO, BASE, ["energy", "unknown", "manufacturing", "services"]),
            ([float("nan"), 0.0, 0.0, 0.0], BASE, GROUPS),
        ]
        for price, base, groups in bad_cases:
            with self.assertRaises(db.DemandBehaviorError):
                db.behavioral_demand_response(price, base, groups)

    def test_invalid_configurations_are_refused(self):
        bad = [
            dict(sigma_household=0.0), dict(sigma_household=-1.0), dict(sigma_household=5.0),
            dict(sigma_firm=0.0), dict(eis=0.0), dict(eis=-2.0), dict(beta_monthly=1.5),
            dict(horizon_months=0), dict(ramp_months=0), dict(short_run_fraction=1.5),
            dict(shock_duration_months=99), dict(energy_cost_share=1.2),
            dict(initial_nominal_wealth=-1.0), dict(max_relative_demand_change=0.0),
            dict(real_interest_rate_annual=-2.0),
            dict(firm_share_by_group={"energy": 1.5, "transport": 0.4,
                                      "manufacturing": 0.5, "services": 0.2}),
        ]
        for kwargs in bad:
            with self.assertRaises(db.BehaviorConfigError):
                db.BehavioralDemandConfig(**kwargs)

    def test_aggregate_bound_and_negative_demand_are_refused(self):
        with self.assertRaises(db.BehaviorBoundsError):
            db.behavioral_demand_response([5.0, 5.0, 5.0, 5.0], BASE, GROUPS,
                                          config=db.BehavioralDemandConfig(sigma_household=2.5,
                                                                           sigma_firm=2.5))

    def test_all_firm_demand_split_refused_when_households_absent(self):
        config = db.BehavioralDemandConfig(firm_share_by_group={
            "energy": 1.0, "transport": 1.0, "manufacturing": 1.0, "services": 1.0})
        with self.assertRaises(db.BehaviorBoundsError):
            db.behavioral_demand_response(ZERO, BASE, GROUPS, config=config)


class DeterminismAndSerialisationTest(unittest.TestCase):
    def test_repeated_calls_with_overrides_are_byte_identical(self):
        kwargs = dict(config=db.BehavioralDemandConfig(short_run_fraction=0.4, ramp_months=4,
                                                       shock_duration_months=3),
                      horizon_months=6, ramp_months=4, max_relative_change=0.5)
        first = db.behavioral_demand_response([0.03, 0.01, 0.0, -0.01], BASE, GROUPS, **kwargs)
        second = db.behavioral_demand_response([0.03, 0.01, 0.0, -0.01], BASE, GROUPS, **kwargs)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_results_stay_inside_the_declared_output_bounds(self):
        out = response()
        self.assertGreaterEqual(out["aggregate"]["relative_short"],
                                -out["config"]["max_relative_demand_change"])
        self.assertLessEqual(out["aggregate"]["relative_short"],
                             out["config"]["max_relative_demand_change"])
        for item in out["by_sector"]:
            self.assertGreaterEqual(item["base_quantity"] + item["delta_long"],
                                    -1e-9 * max(1.0, item["base_quantity"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
