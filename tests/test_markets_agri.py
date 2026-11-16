"""Tests for the agri archetype and soybean-in-Africa pilot (docs/148).

Offline only: the tests exercise the pure deterministic model, the assumption ranges, seasonality,
solver, crush, Africa projection, fail-closed validation, determinism and the offline data-source
template in ``tools/agri_data.py``.  No network, no raw third-party data, no F: dependency.

Run: ``python tests/test_markets_agri.py`` or ``python -m unittest tests.test_markets_agri -v``.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets_agri import (  # noqa: E402
    AfricaConfig,
    AgriMarketError,
    CrushParams,
    PriceRange,
    ScenarioParameters,
    ScenarioShock,
    SoyAgriModule,
    SoyConfig,
    SoyScenario,
    build_global_balance,
    compute_africa_impact,
    crush_margin,
    default_regions,
    describe_seasonality,
    effective_elasticity,
    import_bill_range,
    import_unit_cost,
    market_framework_available,
    meal_price_range,
    module_passport,
    run_pilot,
    seasonal_factor,
    seasonal_path,
    solve_price_multiplier,
)
import agri_data  # noqa: E402


class ContractAndPassport(unittest.TestCase):
    def test_passport_and_contract(self):
        passport = module_passport()
        self.assertEqual(passport["module_id"], "AGRI_SOY")
        self.assertEqual(passport["version"], "0.1.0")
        self.assertEqual(passport["status"], "research_prototype")
        self.assertEqual(passport["source_class"], "assumption")
        self.assertTrue(passport["not_a_forecast"])
        self.assertIn("tests/test_markets_agri.py", passport["tests"])
        self.assertIsInstance(market_framework_available(), bool)
        module = SoyAgriModule()
        for method in ("passport", "validate", "run"):
            self.assertTrue(callable(getattr(module, method)))
        validation = module.validate()
        self.assertTrue(validation["baseline_balance_clears"])
        self.assertTrue(validation["crush_mass_within_one"])
        self.assertEqual(validation["source_class"], "assumption")

    def test_scenario_shock_validation(self):
        self.assertEqual(ScenarioShock(kind="weather_yield_shock", target="Brazil",
                                       magnitude=-0.15).magnitude, -0.15)
        with self.assertRaises(AgriMarketError):
            ScenarioShock(kind="unknown", target="Brazil", magnitude=-0.1)
        with self.assertRaises(AgriMarketError):
            ScenarioShock(kind="weather_yield_shock", target="Brazil", magnitude=0.1)
        with self.assertRaises(AgriMarketError):
            ScenarioShock(kind="export_restriction", target="Brazil", magnitude=1.0)
        with self.assertRaises(AgriMarketError):
            ScenarioShock(kind="freight_shock", target="route", magnitude=-0.1)


class PriceRangesAndPilot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = SoyAgriModule()
        cls.result = cls.module.run()
        cls.data = cls.result.to_dict()

    def test_pilot_is_a_range_not_a_point(self):
        ranges = self.data["price_ranges"]
        self.assertEqual(set(ranges), {"soybeans", "soymeal"})
        for name, interval in ranges.items():
            self.assertLess(interval["low"], interval["high"], name)
            self.assertFalse(interval["point_estimate"])
            self.assertTrue(interval["not_a_forecast"])
            self.assertNotIn("point_price", self.result.to_json())
        self.assertTrue(self.data["not_a_forecast"])
        self.assertEqual(self.data["source_class"], "assumption")
        self.assertIn("not_a_forecast", self.result.to_json())
        self.assertEqual(len(self.result.digest()), 64)
        self.assertEqual(self.data["scenario"]["name"],
                         "Brazil_weather_shock_-15pct_plus_export_restriction")

    def test_scenario_numbers_are_registered_and_ordered(self):
        effects = self.data["effects"]
        self.assertAlmostEqual(effects["brazil_production_loss_mt"], 22.95, places=6)
        self.assertAlmostEqual(effects["brazil_restricted_exports_mt"], 35.0, places=6)
        loss = effects["effective_supply_loss_mt"]
        self.assertLessEqual(loss["low"], loss["high"])
        self.assertGreater(loss["low"], effects["brazil_production_loss_mt"])
        multiplier = effects["price_multiplier_range"]
        self.assertLessEqual(multiplier["low"], multiplier["high"])
        self.assertGreater(multiplier["low"], 1.0)
        soy = self.data["price_ranges"]["soybeans"]
        self.assertGreater(soy["low"], 420.0)  # above the assumption base price
        meal = self.data["price_ranges"]["soymeal"]
        self.assertGreater(meal["low"], 330.0)
        africa = effects["africa"]
        self.assertLessEqual(africa["import_bill_change_usd_mn"]["low"],
                             africa["import_bill_change_usd_mn"]["high"])
        self.assertGreater(africa["import_bill_change_usd_mn"]["low"], 0.0)
        food = africa["food_inflation_pp_range"]
        self.assertLessEqual(food["low"], food["high"])
        self.assertGreaterEqual(food["low"], 0.0)

    def test_balance_identity_and_stocks_to_use(self):
        balance = self.data["balance"]
        baseline = balance["baseline"]
        self.assertAlmostEqual(baseline["identity_residual_mt"], 0.0, places=9)
        self.assertGreater(baseline["ending_stocks_mt"], 0.0)
        self.assertGreater(baseline["stocks_to_use"], 0.05)
        self.assertLess(baseline["stocks_to_use"], 0.5)
        scenario = balance["scenario"]
        self.assertGreater(scenario["ending_stocks_mt"], 0.0)
        self.assertLess(scenario["ending_stocks_mt"], baseline["ending_stocks_mt"])
        self.assertAlmostEqual(balance["identity_check"]["baseline_residual_mt"], 0.0, places=9)

    def test_weather_shock_reduces_production(self):
        regions = default_regions()
        balance = build_global_balance(regions)
        shocked = dict(regions)
        shocked["Brazil"] = regions["Brazil"].with_yield_shock(-0.15)
        shocked_balance = build_global_balance(shocked)
        self.assertLess(shocked_balance.production_mt, balance.production_mt)
        expected_loss = 0.15 * regions["Brazil"].production_mt
        self.assertAlmostEqual(balance.production_mt - shocked_balance.production_mt,
                               expected_loss, places=6)

    def test_export_restriction_increases_price(self):
        without = SoyAgriModule(scenario=SoyScenario(brazil_export_cut=0.0)).run().to_dict()
        with_restriction = self.data
        self.assertGreater(with_restriction["price_ranges"]["soybeans"]["high"],
                           without["price_ranges"]["soybeans"]["high"])
        self.assertGreater(with_restriction["effects"]["effective_supply_loss_mt"]["high"],
                           without["effects"]["effective_supply_loss_mt"]["high"])

    def test_determinism(self):
        first = SoyAgriModule().run()
        second = SoyAgriModule().run()
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(run_pilot(), run_pilot())


class SeasonalityAndSolver(unittest.TestCase):
    def test_seasonality_profile(self):
        description = describe_seasonality()
        factors = description["monthly_factors"]
        self.assertEqual(len(factors), 12)
        self.assertAlmostEqual(math.fsum(factors) / 12.0, 1.0, places=12)
        self.assertTrue(all(value > 0.0 for value in factors))
        self.assertEqual(seasonal_factor(1), factors[0])
        path = seasonal_path(100.0)
        self.assertEqual(len(path), 12)
        self.assertAlmostEqual(sum(path) / 12.0, 100.0, places=6)
        with self.assertRaises(AgriMarketError):
            seasonal_factor(0)
        with self.assertRaises(AgriMarketError):
            seasonal_factor(13)
        with self.assertRaises(AgriMarketError):
            seasonal_path(-1.0)

    def test_solver_monotonicity_and_fail_closed(self):
        base = dict(baseline_use_mt=385.0, available_supply_mt=420.0,
                    target_stocks_use=0.20, elasticity=0.4)
        solved = solve_price_multiplier(**base)
        self.assertGreater(solved.multiplier, 1.0)
        more_supply = solve_price_multiplier(**dict(base, available_supply_mt=440.0))
        self.assertLess(more_supply.multiplier, solved.multiplier)
        less_elastic = solve_price_multiplier(**dict(base, elasticity=0.2))
        self.assertGreater(less_elastic.multiplier, solved.multiplier)
        with self.assertRaises(AgriMarketError):
            solve_price_multiplier(**dict(base, available_supply_mt=0.0))
        with self.assertRaises(AgriMarketError):
            solve_price_multiplier(**dict(base, elasticity=0.0))
        with self.assertRaises(AgriMarketError):
            solve_price_multiplier(**dict(base, available_supply_mt=1.0e9))
        self.assertGreater(effective_elasticity(0.6, 0.7), effective_elasticity(0.6, 0.2))
        self.assertGreater(effective_elasticity(0.6, 0.7), 0.6)

    def test_substitutes_lower_the_price_effect(self):
        high_substitution = effective_elasticity(0.30, 0.70)
        low_substitution = effective_elasticity(0.30, 0.20)
        self.assertGreater(high_substitution, low_substitution)
        kwargs = dict(baseline_use_mt=385.0, available_supply_mt=420.0, target_stocks_use=0.20)
        substituted = solve_price_multiplier(**kwargs, elasticity=high_substitution)
        unsubstituted = solve_price_multiplier(**kwargs, elasticity=low_substitution)
        self.assertLess(substituted.multiplier, unsubstituted.multiplier)


class CrushAndAfrica(unittest.TestCase):
    def test_crush_products_and_margin(self):
        params = CrushParams()
        products = params.products(100.0)
        self.assertAlmostEqual(products["oil_mt"], 18.5, places=9)
        self.assertAlmostEqual(products["meal_mt"], 79.0, places=9)
        self.assertLessEqual(sum(products.values()), 100.0 + 1e-9)
        margin = crush_margin(bean_price_usd_t=420.0, oil_price_usd_t=1000.0,
                              meal_price_usd_t=330.0, params=params)
        expected = 0.185 * 1000.0 + 0.79 * 330.0 - 420.0
        self.assertAlmostEqual(margin["gross_margin_usd_t"], expected, places=6)
        with self.assertRaises(AgriMarketError):
            CrushParams(oil_yield=0.7, meal_yield=0.7)

    def test_import_cost_and_freight(self):
        low = import_unit_cost(price_usd_t=500.0, freight_usd_t=55.0, import_margin=1.05)
        high = import_unit_cost(price_usd_t=500.0, freight_usd_t=70.0, import_margin=1.05)
        self.assertGreater(high, low)
        bill = import_bill_range(volume_mt=4.0,
                                 price_range=PriceRange(low=500.0, high=600.0),
                                 freight_usd_t=55.0, import_margin=1.05)
        self.assertLess(bill["bill_low_usd_mn"], bill["bill_high_usd_mn"])
        with self.assertRaises(AgriMarketError):
            import_unit_cost(price_usd_t=500.0, freight_usd_t=55.0, import_margin=0.9)

    def test_africa_impact_is_a_range(self):
        result = SoyAgriModule().run()
        africa = result.effects["africa"]
        self.assertEqual(africa["source_class"], "assumption")
        self.assertLess(africa["baseline_import_bill_usd_mn"],
                        africa["scenario_import_bill_usd_mn"]["low"])
        self.assertLess(africa["scenario_import_bill_usd_mn"]["low"],
                        africa["scenario_import_bill_usd_mn"]["high"])
        food = africa["food_inflation_pp_range"]
        self.assertLessEqual(food["low"], food["high"])
        with self.assertRaises(AgriMarketError):
            AfricaConfig(soybean_imports_mt=-1.0)


class FailClosed(unittest.TestCase):
    def test_invalid_configuration_and_scenario(self):
        with self.assertRaises(AgriMarketError):
            SoyConfig(regions={})
        with self.assertRaises(AgriMarketError):
            SoyConfig(regions={"United States": default_regions()["United States"]})
        with self.assertRaises(AgriMarketError):
            SoyConfig(regions=default_regions(), base_soy_price_usd_t=-1.0)
        with self.assertRaises(AgriMarketError):
            SoyConfig(regions=default_regions(), base_freight_usd_t=-1.0)
        with self.assertRaises(AgriMarketError):
            SoyScenario(brazil_yield_shock=0.10)
        with self.assertRaises(AgriMarketError):
            SoyScenario(brazil_export_cut=1.5)
        with self.assertRaises(AgriMarketError):
            SoyAgriModule(scenario="not a scenario")

    def test_invalid_regions_and_balance(self):
        regions = default_regions()
        broken = dict(regions)
        broken["Brazil"] = type(regions["Brazil"])(
            name="Brazil", area_mha=45.0, yield_t_ha=3.4, beginning_stocks_mt=15.0,
            crush_mt=65.0, other_use_mt=20.0, exports_mt=160.0)
        with self.assertRaises(AgriMarketError):
            build_global_balance(broken)
        with self.assertRaises(AgriMarketError):
            PriceRange(low=100.0, high=90.0)
        with self.assertRaises(AgriMarketError):
            PriceRange(low=-1.0, high=10.0)
        with self.assertRaises(AgriMarketError):
            ScenarioParameters(elasticity_low=0.5, elasticity_high=0.4)
        with self.assertRaises(AgriMarketError):
            meal_price_range(base_meal_price_usd_t=-1.0,
                             low={"solver": None, "meal_sensitivity": 1.0},
                             high={"solver": None, "meal_sensitivity": 1.0})


class DataTemplate(unittest.TestCase):
    def test_sources_and_licenses(self):
        description = agri_data.describe_sources()
        self.assertTrue(description["offline"])
        source_ids = {source["source_id"] for source in description["sources"]}
        self.assertEqual(source_ids, {"USDA_PSD", "USDA_FAS_EXPORT_SALES", "FAO_FAOSTAT",
                                      "WORLD_BANK", "UN_COMTRADE"})
        for source in description["sources"]:
            self.assertTrue(source["license"].strip())
            self.assertTrue(source["url"].startswith("https://"))
            self.assertTrue(source["required_fields"])
        self.assertIn("non-commercial", agri_data.source_by_id("FAO_FAOSTAT")["license"])
        self.assertIn("CC BY 4.0", agri_data.source_by_id("WORLD_BANK")["license"])
        self.assertIn("not a forecast", description["disclaimer"])

    def test_record_validation_and_template(self):
        good = {"source_id": "USDA_PSD", "commodity": "soybeans", "region": "Brazil",
                "period": "2024/25", "period_type": "marketing_year",
                "license": agri_data.source_by_id("USDA_PSD")["license"],
                "vintage": "2026-01-01", "source_class": "observed",
                "production_mt": 153.0, "area_mha": 45.0, "yield_t_ha": 3.4}
        checked = agri_data.validate_record(good)
        self.assertEqual(checked["commodity"], "soybeans")
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_record({key: value for key, value in good.items()
                                       if key != "source_id"})
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_record(dict(good, source_id="UNKNOWN"))
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_record(dict(good, license="some other licence"))
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_record(dict(good, commodity="wheat"))
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_record(dict(good, production_mt=float("nan")))
        with self.assertRaises(agri_data.AgriDataError):
            agri_data.validate_records([])
        summary = agri_data.validate_records([good])
        self.assertEqual(summary["n_records"], 1)
        with tempfile.TemporaryDirectory() as directory:
            template = agri_data.write_template(Path(directory) / "agri_template.json")
            payload = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"]["version"], "0.1.0")

    def test_cli_help_and_describe(self):
        import subprocess
        tool = ROOT / "tools" / "agri_data.py"
        help_result = subprocess.run([sys.executable, str(tool), "--help"],
                                     capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--describe", help_result.stdout)
        describe = subprocess.run([sys.executable, str(tool), "--describe"],
                                  capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(describe.returncode, 0)
        payload = json.loads(describe.stdout)
        self.assertTrue(payload["offline"])
        self.assertEqual(len(payload["sources"]), 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.json"
            source = agri_data.source_by_id("WORLD_BANK")
            path.write_text(json.dumps([{
                "source_id": "WORLD_BANK", "commodity": "soybeans", "region": "world",
                "period": "2024-01", "period_type": "month", "license": source["license"],
                "vintage": "2026-01-01", "source_class": "observed", "price_usd_t": 420.0}]),
                encoding="utf-8")
            checked = subprocess.run([sys.executable, str(tool), "--check", str(path)],
                                     capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(checked.returncode, 0)
            self.assertIn("n_records", checked.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
