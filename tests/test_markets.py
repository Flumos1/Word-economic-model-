"""Tests for the AMFE 2.0 market framework and the oil/Hormuz module (docs/147).

The tests are offline and synthetic: they exercise the contract, the deterministic balance engine,
the oil module scenarios, the sensitivity ranges, the fail-closed paths and the MRIO cost hook.
No third-party data is read and no time-dependent state is used.

Run from the repository root: ``python tests/test_markets.py``.
"""
from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets import (  # noqa: E402
    ASSUMPTION_SPECS,
    OIL_MODULE_ID,
    RESEARCH_ONLY,
    SOURCE_CLASSES,
    Assumption,
    BalanceError,
    MarketCalibration,
    MarketError,
    MarketModule,
    MarketResult,
    Scenario,
    ScenarioError,
    Shock,
    UnitError,
    flat,
    get_module,
    modules_document,
    oil,
    price_range,
    ramp,
    registered_modules,
    run_variants,
    simulate,
)
from amfe.markets.contract import CostShock  # noqa: E402


def calibration(**overrides) -> MarketCalibration:
    params = dict(
        commodity="test", base_price=75.0, base_demand=100.0, baseline_supply=100.0,
        elasticity_short=-0.25, elasticity_long=-0.55, elasticity_ramp_months=6,
        risk_premium=0.0, commercial_stock=200.0, commercial_max_draw=1.0,
        strategic_stock=300.0, strategic_max_draw=1.5,
    )
    params.update(overrides)
    return MarketCalibration(**params)


def shock(lost=(20.0, 15.0, 10.0, 5.0, 0.0, 0.0), added=(2.0,) * 6,
          release=(1.5,) * 6) -> Shock:
    return Shock(lost_supply=tuple(lost), added_supply=tuple(added),
                 strategic_release=tuple(release), label="test")


# ---------------------------------------------------------------------------------------
# 1. Contract
# ---------------------------------------------------------------------------------------
class ContractTests(unittest.TestCase):
    def test_assumption_requires_valid_source_class(self):
        Assumption(name="p", value=1.0, unit="mb/d", source_class="assumption")
        for bad in ("given", "official", "", None):
            with self.assertRaises(MarketError):
                Assumption(name="p", value=1.0, unit="mb/d", source_class=bad)
        with self.assertRaises(MarketError):
            Assumption(name="", value=1.0, unit="mb/d", source_class="assumption")
        with self.assertRaises(MarketError):
            Assumption(name="p", value=1.0, unit="", source_class="assumption")

    def test_scenario_validation(self):
        with self.assertRaises(MarketError):
            Scenario(scenario_id="", description="x")
        with self.assertRaises(ScenarioError):
            Scenario(scenario_id="s", description="x", horizon_months=0)

    def test_result_rejects_inconsistent_paths_and_status(self):
        base = dict(module_id="m", commodity="c", scenario_id="s", units="USD/bbl",
                    horizon_months=2, price_path=(10.0, 11.0), summary={}, assumptions=())
        MarketResult(**base)
        with self.assertRaises(MarketError):
            MarketResult(**{**base, "price_path": (10.0,)})
        with self.assertRaises(MarketError):
            MarketResult(**{**base, "price_path": (10.0, -1.0)})
        with self.assertRaises(MarketError):
            MarketResult(**{**base, "status": "ADMITTED"})

    def test_registry_contains_the_oil_module(self):
        module = get_module(OIL_MODULE_ID)
        self.assertIsInstance(module, MarketModule)
        self.assertEqual(module.status, RESEARCH_ONLY)
        document = modules_document()
        ids = [item["module_id"] for item in document]
        self.assertIn(OIL_MODULE_ID, ids)
        self.assertEqual(document[0]["status"], RESEARCH_ONLY)
        self.assertEqual({module.module_id for module in registered_modules()}, set(ids))
        with self.assertRaises(MarketError):
            get_module("unknown.module")


# ---------------------------------------------------------------------------------------
# 2. Engine
# ---------------------------------------------------------------------------------------
class EngineTests(unittest.TestCase):
    def test_calibration_validation_is_fail_closed(self):
        for overrides in ({"base_price": 0.0}, {"base_demand": -1.0},
                          {"strategic_stock": -5.0}, {"commercial_max_draw": -1.0},
                          {"elasticity_short": 0.1}, {"elasticity_long": 0.2},
                          {"elasticity_short": -0.8, "elasticity_long": -0.5},
                          {"price_floor_fraction": 0.0}, {"price_ceiling_multiple": 1.0},
                          {"tolerance": 0.0}):
            with self.assertRaises(MarketError, msg=str(overrides)):
                calibration(**overrides)
        with self.assertRaises(UnitError):
            calibration(quantity_unit="million bbl/day")

    def test_ramp_and_flat_vectors(self):
        self.assertEqual(ramp(3.0, 4, 3), (1.0, 2.0, 3.0, 3.0))
        self.assertEqual(flat(1.5, 3), (1.5, 1.5, 1.5))
        with self.assertRaises(MarketError):
            ramp(-1.0, 3, 2)
        with self.assertRaises(MarketError):
            flat(-1.0, 3)

    def test_balance_identity_and_stock_accounting(self):
        path = simulate(calibration(), shock())
        self.assertTrue(path.ok, path.checks)
        for index, row in enumerate(path.rows()):
            residual = (row["supply_available"] + row["commercial_draw"] - row["demand"]
                        - row["shortage"] + row["surplus"])
            self.assertLessEqual(abs(residual), 1e-6, index)
        self.assertTrue(all(value >= -1e-9 for value in path.commercial_stock_end))
        self.assertTrue(all(value >= -1e-9 for value in path.strategic_stock_end))
        # the declared strategic plan fits the stock (1.5 mb/d * 6 months * 30.4375 = 274 mb)
        self.assertLessEqual(sum(path.strategic_release) * 30.4375, 300.0 + 1e-9)
        self.assertGreater(max(path.price), 75.0)

    def test_determinism(self):
        first = simulate(calibration(), shock())
        second = simulate(calibration(), shock())
        self.assertEqual(first.price, second.price)
        self.assertEqual(first.rows(), second.rows())
        self.assertEqual(first.checks, second.checks)

    def test_price_rises_with_a_bigger_supply_loss(self):
        small = simulate(calibration(), shock(lost=(5.0,) * 6))
        large = simulate(calibration(), shock(lost=(20.0,) * 6))
        self.assertGreater(max(large.price), max(small.price))

    def test_more_elastic_demand_lowers_the_peak(self):
        inelastic = simulate(calibration(elasticity_short=-0.10, elasticity_long=-0.30), shock())
        elastic = simulate(calibration(elasticity_short=-0.35, elasticity_long=-0.75), shock())
        self.assertLess(max(elastic.price), max(inelastic.price))

    def test_strategic_release_above_the_stock_is_refused(self):
        with self.assertRaises(BalanceError):
            simulate(calibration(strategic_stock=10.0), shock(release=(1.5,) * 6))
        with self.assertRaises(BalanceError):
            simulate(calibration(strategic_max_draw=1.0), shock(release=(1.5,) * 6))

    def test_sensitivity_variants_and_range(self):
        central = simulate(calibration(), shock())
        variants = run_variants(calibration(), shock(), [
            ("inelastic", {"elasticity_short": -0.10, "elasticity_long": -0.30}),
            ("elastic", {"elasticity_short": -0.35, "elasticity_long": -0.75}),
        ])
        self.assertEqual([item["variant"] for item in variants], ["elastic", "inelastic"])
        for item in variants:
            self.assertIn("price_peak", item)
            self.assertIn("delta_vs_central", item)
        rng = price_range(central, variants)
        self.assertLessEqual(rng["low"], rng["central"])
        self.assertLessEqual(rng["central"], rng["high"])
        self.assertEqual(rng["metric"], "price_peak")
        with self.assertRaises(MarketError):
            price_range(central, variants, metric="nonsense")
        with self.assertRaises(MarketError):
            price_range(central, [])


# ---------------------------------------------------------------------------------------
# 3. Oil module
# ---------------------------------------------------------------------------------------
class OilModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = oil.OilModule()
        cls.result = cls.module.run()

    def test_scenarios_are_declared_and_run(self):
        scenarios = self.module.scenarios()
        self.assertEqual([item.scenario_id for item in scenarios],
                         ["hormuz_closed_6m", "hormuz_closed_3m", "hormuz_closed_6m_no_spr"])
        for scenario in scenarios:
            result = self.module.run(scenario.scenario_id)
            self.assertEqual(result.scenario_id, scenario.scenario_id)
            self.assertEqual(result.status, RESEARCH_ONLY)
            self.assertEqual(len(result.price_path), scenario.horizon_months)
            self.assertTrue(all(result.checks.values()), result.checks)
            self.assertEqual(len(result.digest()), 64)
        self.assertEqual(self.module.default_scenario().scenario_id, "hormuz_closed_6m")

    def test_central_scenario_numbers(self):
        summary = self.result.summary
        self.assertGreater(summary["price_peak"], 75.0)
        self.assertGreater(summary["price_peak"], summary["price_trough"])
        rng = summary["brent_range_usd_per_bbl"]
        self.assertLessEqual(rng["low"], rng["central"])
        self.assertLessEqual(rng["central"], rng["high"])
        self.assertGreater(rng["high"] - rng["low"], 0.0)
        self.assertEqual(rng["units"], "USD/bbl")
        self.assertEqual(summary["closure_months"], 6)
        self.assertGreater(summary["consumed_strategic_stock"], 0.0)
        self.assertLessEqual(summary["consumed_strategic_stock"], oil.DEFAULT_PARAMS["spr_stock"] + 1e-6)
        self.assertEqual(len(summary["monthly_path"]), self.result.horizon_months)

    def test_three_month_closure_recovers_faster_than_six(self):
        three = self.module.run("hormuz_closed_3m")
        self.assertLess(three.summary["price_mean"], self.result.summary["price_mean"])
        self.assertLess(three.price_path[-1], self.result.price_path[-1])

    def test_no_spr_release_is_more_expensive(self):
        no_spr = self.module.run("hormuz_closed_6m_no_spr")
        self.assertGreater(no_spr.summary["price_peak"], self.result.summary["price_peak"])
        self.assertGreaterEqual(no_spr.summary["brent_range_usd_per_bbl"]["high"],
                                self.result.summary["brent_range_usd_per_bbl"]["high"])

    def test_results_are_deterministic(self):
        again = self.module.run()
        self.assertEqual(self.result.price_path, again.price_path)
        self.assertEqual(self.result.digest(), again.digest())
        self.assertEqual(self.result.sensitivity, again.sensitivity)

    def test_assumptions_are_declared_with_replace_with(self):
        assumptions = self.result.assumptions
        self.assertGreaterEqual(len(assumptions), 12)
        self.assertTrue(all(item.source_class in SOURCE_CLASSES for item in assumptions))
        self.assertTrue(all(item.source_class == "assumption" for item in assumptions))
        names = {item.name for item in assumptions}
        for required in ("hormuz_transit", "bypass_capacity", "spr_stock", "elasticity_short",
                         "elasticity_long", "risk_premium", "closure_months"):
            self.assertIn(required, names)
        replace_with = self.result.meta["replace_with"]
        self.assertGreaterEqual(len(replace_with), 12)
        self.assertTrue(all("source" in item for item in replace_with))
        self.assertEqual(len({spec[0] for spec in ASSUMPTION_SPECS}), len(ASSUMPTION_SPECS))

    def test_sensitivity_table_covers_the_declared_variants(self):
        variants = [item["variant"] for item in self.result.sensitivity]
        for name in ("elasticity_more_elastic", "elasticity_less_elastic", "bypass_low",
                     "bypass_high", "risk_premium_high", "closure_9m"):
            self.assertIn(name, variants)
        peaks = [item["price_peak"] for item in self.result.sensitivity]
        self.assertEqual(peaks, sorted(peaks))  # table is ordered by peak price

    def test_unknown_scenario_and_override_are_refused(self):
        with self.assertRaises(ScenarioError):
            self.module.run("hormuz_never")
        with self.assertRaises(ScenarioError):
            self.module.run("hormuz_closed_6m", not_a_parameter=1.0)

    def test_parameter_overrides_are_applied(self):
        cheap = self.module.run("hormuz_closed_6m", risk_premium=0.0)
        self.assertLess(cheap.summary["price_peak"], self.result.summary["price_peak"])

    # -- MRIO hook ---------------------------------------------------------------------
    def test_cost_shock_hook(self):
        cost = self.module.prepare_cost_shock(self.result)
        self.assertIsInstance(cost, CostShock)
        self.assertEqual(cost.shock_type, "energy_price")
        self.assertEqual(cost.status, RESEARCH_ONLY)
        self.assertGreater(cost.brent_delta_usd, 0.0)
        self.assertEqual(cost.brent_peak_usd, self.result.summary["price_peak"])
        self.assertGreater(cost.brent_delta_pct, 0.0)
        self.assertTrue(all(value >= 1.0 for value in cost.sector_cost_multipliers.values()))
        self.assertGreater(cost.sector_cost_multipliers["energy"],
                           cost.sector_cost_multipliers["services"])
        self.assertGreaterEqual(cost.default_multiplier, 1.0)
        document = cost.as_dict()
        self.assertEqual(document["schema"], "amfe.markets.cost_shock.v1")
        with self.assertRaises(MarketError):
            self.module.prepare_cost_shock(self.result, pass_through=1.5)

    def test_result_document_is_aggregate_only(self):
        document = self.result.as_dict()
        self.assertEqual(document["status"], RESEARCH_ONLY)
        for key in ("price_path", "summary", "assumptions", "sensitivity", "checks", "digest"):
            self.assertIn(key, document)
        text = __import__("json").dumps(document, ensure_ascii=False)
        self.assertNotIn('"matrix"', text)
        self.assertNotIn('"Z"', text)
        self.assertNotIn("timestamp", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
