"""Tests for the gas/LNG market module and its CLI (docs/188, UC-24).

Offline and synthetic by construction: the module reads no data, no network and no clock, so every
test here exercises declared assumptions, unit arithmetic, the balance identities, the scenario
signs, the arbitrage solver, determinism, the aggregate-only output and the fail-closed paths.

Run from the repository root: ``python tests/test_gas_scenarios.py``.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets import gas  # noqa: E402
from amfe.markets.contract import (  # noqa: E402
    RESEARCH_ONLY,
    SOURCE_CLASSES,
    MarketError,
    MarketModule,
    MarketResult,
    ScenarioError,
    get_module,
    modules_document,
)

import gas_scenarios  # noqa: E402

SHOCK = "eu_pipeline_shock_6m"
DIVERSION = "eu_pipeline_shock_lng_diversion_6m"
SOFT = "soft_winter_6m"
HARD = "hard_winter_6m"
COMBO = "hard_winter_pipeline_shock_6m"
ALL_SCENARIOS = (SHOCK, DIVERSION, SOFT, HARD, COMBO)
TTF_BASE = gas.eur_per_mwh_to_usd_per_mmbtu(34.0, 1.12)  # 11.1603...


def numeric_lists(node, path: str = "$") -> list[tuple[int, str]]:
    """Every list of numbers in a document, with its length and path (aggregate-only audit)."""
    found: list[tuple[int, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(numeric_lists(value, f"{path}.{key}"))
    elif isinstance(node, list):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in node):
            found.append((len(node), path))
        for index, value in enumerate(node):
            found.extend(numeric_lists(value, f"{path}[{index}]"))
    return found


# ---------------------------------------------------------------------------------------
# 1. Units
# ---------------------------------------------------------------------------------------
class UnitTests(unittest.TestCase):
    def test_bcm_energy_and_oil_equivalent_conversions(self):
        self.assertAlmostEqual(gas.bcm_to_mmbtu(1.0), 36.6213439e6, delta=1.0)
        self.assertAlmostEqual(gas.bcm_to_mboe(1.0), 6.3140248, places=6)
        # the two conversions are consistent by construction (1 boe = 5.8 MMBtu)
        self.assertAlmostEqual(gas.bcm_to_mboe(1.0) * 1e6 * gas.BOE_MMBTU,
                               gas.bcm_to_mmbtu(1.0), delta=1e-3)
        self.assertAlmostEqual(gas.mmbtu_to_bcm(gas.bcm_to_mmbtu(2.5)), 2.5, places=12)
        self.assertAlmostEqual(gas.mboe_to_bcm(gas.bcm_to_mboe(7.0)), 7.0, places=12)

    def test_annual_and_daily_gas_flows(self):
        self.assertAlmostEqual(gas.bcm_y_to_mb_d(380.0), 6.5735053, places=6)
        self.assertAlmostEqual(gas.mb_d_to_bcm_y(gas.bcm_y_to_mb_d(380.0)), 380.0, places=9)
        self.assertAlmostEqual(gas.bcm_day_to_mb_d(0.40), 2.5256099, places=6)
        self.assertAlmostEqual(gas.bcm_y_to_bcm_month(1.0), 30.4375 / 365.0, places=12)

    def test_price_units_roundtrip_and_levels(self):
        usd = gas.eur_per_mwh_to_usd_per_mmbtu(34.0, 1.12)
        self.assertAlmostEqual(usd, 11.160146, places=5)
        self.assertAlmostEqual(gas.usd_per_mmbtu_to_eur_per_mwh(usd, 1.12), 34.0, places=9)
        self.assertAlmostEqual(gas.usd_per_mmbtu_to_usd_per_mcf(3.2), 3.3184, places=4)
        self.assertAlmostEqual(gas.usd_per_mcf_to_usd_per_mmbtu(3.3184), 3.2, places=4)
        for unit in gas.PRICE_UNITS:
            for other in gas.PRICE_UNITS:
                value = gas.convert_price(20.0, unit, other, eur_usd=1.1)
                self.assertAlmostEqual(
                    gas.convert_price(value, other, unit, eur_usd=1.1), 20.0, places=9)

    def test_flow_units_roundtrip(self):
        for unit in gas.FLOW_UNITS:
            for other in gas.FLOW_UNITS:
                value = gas.convert_flow(120.0, unit, other)
                self.assertAlmostEqual(gas.convert_flow(value, other, unit), 120.0, places=9)
        self.assertAlmostEqual(gas.convert_flow(1.0, "bcm/y", "bcm/month"), 30.4375 / 365.0,
                               places=12)

    def test_unknown_and_impossible_units_fail_closed(self):
        for bad in ("USD/boe", "", "EUR/MMBtu", None):
            with self.assertRaises(gas.GasUnitError):
                gas.convert_price(10.0, bad, "USD/MMBtu", eur_usd=1.1)
            with self.assertRaises(gas.GasUnitError):
                gas.convert_flow(10.0, "bcm/y", bad)
        with self.assertRaises(gas.GasUnitError):
            gas.convert_price(-1.0, "USD/MMBtu", "EUR/MWh", eur_usd=1.1)
        with self.assertRaises(gas.GasUnitError):
            gas.convert_price(10.0, "EUR/MWh", "USD/MMBtu", eur_usd=0.0)
        with self.assertRaises(gas.GasUnitError):
            gas.bcm_to_mmbtu(float("nan"))

    def test_hub_table_has_three_conventions(self):
        rows, spreads = gas.hub_table(gas.DEFAULT_PARAMS)
        self.assertEqual([row["hub_id"] for row in rows], ["HENRY_HUB", "TTF", "JKM"])
        by_id = {row["hub_id"]: row for row in rows}
        self.assertEqual(by_id["TTF"]["quote_unit"], "EUR/MWh")
        self.assertEqual(by_id["JKM"]["quote_unit"], "USD/MMBtu")
        self.assertAlmostEqual(by_id["TTF"]["usd_per_mmbtu"], TTF_BASE, places=6)
        self.assertAlmostEqual(spreads["ttf_minus_henry_hub_usd_per_mmbtu"],
                               TTF_BASE - 3.2, places=6)
        self.assertGreater(spreads["jkm_minus_ttf_usd_per_mmbtu"], 0.0)
        self.assertTrue(all(row["source_class"] == "assumption" for row in rows))


# ---------------------------------------------------------------------------------------
# 2. Balances
# ---------------------------------------------------------------------------------------
class BalanceTests(unittest.TestCase):
    def test_european_balance_closes_by_construction(self):
        self.assertAlmostEqual(gas.eu_balance_bcm_y(gas.DEFAULT_PARAMS), 0.0, places=9)
        self.assertAlmostEqual(gas.eu_supply_bcm_y(gas.DEFAULT_PARAMS), 380.0, places=9)
        self.assertAlmostEqual(gas.pipeline_supply_bcm_y(gas.DEFAULT_PARAMS), 220.0, places=9)

    def test_global_lng_pool_adds_up_and_the_diversion_is_a_transfer(self):
        report = gas.build_report([DIVERSION])
        summary = report["scenarios"][0]["summary"]
        flows = summary["lng_flows"]
        for month in range(6):
            self.assertAlmostEqual(flows["global_lng_bcm_y_path"][month], 560.0, places=6)
        diversion = flows["diversion_bcm_y"]
        self.assertGreater(diversion, 0.0)
        self.assertAlmostEqual(flows["eu_lng_imports_bcm_y_path"][-1],
                               160.0 + diversion, places=3)
        self.assertAlmostEqual(flows["asia_lng_imports_bcm_y_path"][-1],
                               350.0 - diversion, places=3)
        self.assertTrue(report["scenarios"][0]["checks"]["diversion_is_a_transfer"])
        self.assertTrue(report["scenarios"][0]["checks"]["global_lng_balance"])

    def test_storage_identity_and_limits(self):
        result = gas.GAS_MODULE.run(SHOCK)
        summary = result.summary
        storage = summary["storage"]
        self.assertAlmostEqual(storage["start_bcm"] - storage["end_bcm"],
                               storage["drawn_bcm"], places=3)
        self.assertGreater(storage["drawn_bcm"], 0.0)
        self.assertGreaterEqual(storage["end_share_of_capacity"],
                                100.0 * float(gas.DEFAULT_PARAMS["storage_min_share"]))
        self.assertLessEqual(max(storage["monthly_release_bcm_day"]),
                             float(gas.DEFAULT_PARAMS["storage_max_draw_bcm_day"]) + 1e-9)
        for key in ("storage_min_respected", "storage_within_capacity",
                    "storage_plan_within_deliverability"):
            self.assertTrue(result.checks[key], key)
        released = sum(storage["monthly_release_bcm_day"]) * gas.DAYS_PER_MONTH
        self.assertAlmostEqual(released, storage["drawn_bcm"], places=2)

    def test_engine_supply_identities_hold_month_by_month(self):
        for scenario_id in ALL_SCENARIOS:
            checks = gas.GAS_MODULE.run(scenario_id).checks
            for key in ("eu_supply_identity", "asia_supply_identity", "ttf_mass_balance",
                        "jkm_mass_balance", "ttf_stocks_non_negative",
                        "jkm_stocks_non_negative"):
                self.assertTrue(checks[key], f"{scenario_id}: {key}")

    def test_calibration_inconsistencies_fail_closed(self):
        with self.assertRaises(MarketError):
            gas.GAS_MODULE.run(SHOCK, eu_demand_bcm_y=381.0)
        with self.assertRaises(MarketError):
            gas.GAS_MODULE.run(SHOCK, global_lng_supply_bcm_y=561.0)
        with self.assertRaises(MarketError):
            gas.GAS_MODULE.run(SHOCK, eu_lng_import_capacity_bcm_y=100.0)


# ---------------------------------------------------------------------------------------
# 3. Scenario signs and ranges
# ---------------------------------------------------------------------------------------
class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {scenario_id: gas.GAS_MODULE.run(scenario_id)
                       for scenario_id in ALL_SCENARIOS}
        cls.summaries = {key: value.summary for key, value in cls.results.items()}

    def test_every_scenario_satisfies_the_contract(self):
        self.assertEqual(len(self.results), 5)
        for scenario_id, result in self.results.items():
            self.assertIsInstance(result, MarketResult)
            self.assertEqual(result.module_id, gas.MODULE_ID)
            self.assertEqual(result.units, "USD/MMBtu")
            self.assertEqual(result.status, RESEARCH_ONLY)
            self.assertEqual(len(result.price_path), result.horizon_months)
            self.assertEqual(len(result.price_path), 6)
            self.assertTrue(all(value > 0.0 for value in result.price_path))
            self.assertTrue(all(result.checks.values()),
                            [key for key, value in result.checks.items() if not value])
            self.assertEqual(result.as_dict()["schema"], "amfe.markets.result.v1")

    def test_pipeline_shock_raises_ttf_and_leaves_jkm_alone(self):
        summary = self.summaries[SHOCK]
        self.assertGreater(summary["ttf_peak_usd_per_mmbtu"], TTF_BASE)
        self.assertGreater(summary["ttf_change_pct_vs_base"], 20.0)
        self.assertEqual(summary["ttf_usd_per_mmbtu_path"][0],
                         summary["ttf_peak_usd_per_mmbtu"])
        self.assertTrue(all(abs(value - 12.0) <= 1e-9
                            for value in summary["jkm_usd_per_mmbtu_path"]))
        self.assertGreater(summary["storage"]["drawn_bcm"], 0.0)

    def test_lng_diversion_moves_the_price_from_europe_to_asia(self):
        shock = self.summaries[SHOCK]
        diversion = self.summaries[DIVERSION]
        self.assertLess(diversion["ttf_peak_usd_per_mmbtu"], shock["ttf_peak_usd_per_mmbtu"])
        self.assertLess(diversion["ttf_mean_usd_per_mmbtu"], shock["ttf_mean_usd_per_mmbtu"])
        self.assertGreater(diversion["jkm_peak_usd_per_mmbtu"], 12.0)
        self.assertGreater(diversion["jkm_peak_usd_per_mmbtu"],
                           shock["jkm_peak_usd_per_mmbtu"])
        self.assertGreater(diversion["arbitrage"]["diversion"]["diversion_bcm_y"], 0.0)
        self.assertLessEqual(diversion["arbitrage"]["diversion"]["diversion_bcm_y"], 40.0)
        # cargoes replace pipeline gas, so less storage has to be drawn
        self.assertLess(diversion["storage"]["drawn_bcm"], shock["storage"]["drawn_bcm"])

    def test_diversion_is_solved_at_the_arbitrage_band(self):
        arbitrage = self.summaries[DIVERSION]["arbitrage"]
        self.assertEqual(arbitrage["diversion"]["binding"], "arbitrage")
        self.assertEqual(arbitrage["diversion"]["iterations"], gas.ARBITRAGE_ITERATIONS)
        self.assertLessEqual(abs(float(arbitrage["diversion"]["residual"])),
                             gas.ARBITRAGE_TOLERANCE)
        bracket = arbitrage["diversion"]["bracket"]
        self.assertGreater(bracket[0], 0.0)   # at zero diversion Europe pays the band
        self.assertLess(bracket[1], 0.0)      # at full capacity Asia pays it
        capacity_case = arbitrage["capacity_case"]
        self.assertEqual(capacity_case["diversion_bcm_y"], 40.0)
        self.assertFalse(capacity_case["within_band"])
        self.assertGreater(capacity_case["jkm_minus_ttf_usd_per_mmbtu"],
                           arbitrage["band_usd_per_mmbtu"])
        self.assertTrue(self.results[DIVERSION].checks["arbitrage_consistent"])

    def test_winter_scenarios_have_the_declared_signs(self):
        soft = self.summaries[SOFT]
        hard = self.summaries[HARD]
        self.assertLess(soft["ttf_trough_usd_per_mmbtu"], TTF_BASE)
        self.assertLess(soft["ttf_peak_usd_per_mmbtu"], TTF_BASE)
        self.assertLess(soft["ttf_mean_change_pct_vs_base"], 0.0)
        self.assertGreater(hard["ttf_peak_usd_per_mmbtu"], TTF_BASE)
        self.assertGreater(hard["ttf_peak_usd_per_mmbtu"], soft["ttf_peak_usd_per_mmbtu"])
        self.assertGreater(hard["storage"]["drawn_bcm"], soft["storage"]["drawn_bcm"])
        self.assertEqual(soft["storage"]["drawn_bcm"], 0.0)  # no gap in a mild winter
        combo = self.summaries[COMBO]
        self.assertGreater(combo["ttf_peak_usd_per_mmbtu"], hard["ttf_peak_usd_per_mmbtu"])
        self.assertGreater(combo["storage"]["drawn_bcm"], hard["storage"]["drawn_bcm"])

    def test_eur_per_mwh_paths_match_the_usd_paths(self):
        for scenario_id, summary in self.summaries.items():
            for usd, eur in zip(summary["ttf_usd_per_mmbtu_path"],
                                summary["ttf_eur_per_mwh_path"]):
                self.assertAlmostEqual(eur, usd * gas.MMBTU_PER_MWH / 1.12, places=3)
            self.assertAlmostEqual(summary["ttf_peak_eur_per_mwh"],
                                   summary["ttf_peak_usd_per_mmbtu"] * gas.MMBTU_PER_MWH / 1.12,
                                   places=3)

    def test_ranges_are_ordered_and_wider_than_the_central_path(self):
        for scenario_id, result in self.results.items():
            for key in ("ttf_peak", "jkm_peak"):
                rng = result.summary["ranges"][key]
                self.assertLessEqual(rng["low"], rng["central"], f"{scenario_id}/{key}")
                self.assertLessEqual(rng["central"], rng["high"], f"{scenario_id}/{key}")
                self.assertEqual(rng["units"], "USD/MMBtu")
                self.assertGreaterEqual(rng["spread_pct"], 0.0)
        peak = self.summaries[SHOCK]["ttf_peak_usd_per_mmbtu"]
        rng = self.summaries[SHOCK]["ranges"]["ttf_peak"]
        self.assertGreaterEqual(peak, rng["low"] - 1e-9)
        self.assertLessEqual(peak, rng["high"] + 1e-9)

    def test_sensitivity_table_covers_the_declared_variants_in_order(self):
        result = self.results[SHOCK]
        names = [item["variant"] for item in result.sensitivity]
        for expected in ("elasticity_more_elastic", "elasticity_less_elastic",
                         "storage_cover_high", "storage_cover_low", "risk_premium_low",
                         "risk_premium_high", "pipeline_loss_9m"):
            self.assertIn(expected, names)
        peaks = [item["ttf_peak"] for item in result.sensitivity]
        self.assertEqual(peaks, sorted(peaks))
        rows = {item["variant"]: item for item in result.sensitivity}
        self.assertLess(rows["elasticity_more_elastic"]["ttf_peak"],
                        rows["elasticity_less_elastic"]["ttf_peak"])
        self.assertLess(rows["storage_cover_high"]["ttf_peak"],
                        rows["storage_cover_low"]["ttf_peak"])
        self.assertLess(rows["risk_premium_low"]["ttf_peak"],
                        rows["risk_premium_high"]["ttf_peak"])
        self.assertEqual(rows["risk_premium_low"]["ttf_peak"],
                         round(self.summaries[SHOCK]["ttf_peak_usd_per_mmbtu"] - 2.0, 6))

    def test_parameter_overrides_are_applied_and_validated(self):
        cheap = gas.GAS_MODULE.run(SHOCK, risk_premium_usd_per_mmbtu=0.0)
        self.assertLess(cheap.summary["ttf_peak_usd_per_mmbtu"],
                        self.summaries[SHOCK]["ttf_peak_usd_per_mmbtu"])
        self.assertAlmostEqual(
            cheap.summary["ttf_peak_usd_per_mmbtu"],
            self.summaries[SHOCK]["ttf_peak_usd_per_mmbtu"] - 2.0, places=4)
        stronger = gas.GAS_MODULE.run(SHOCK, pipeline_loss_bcm_y=70.0)
        self.assertGreater(stronger.summary["ttf_peak_usd_per_mmbtu"],
                           self.summaries[SHOCK]["ttf_peak_usd_per_mmbtu"])
        with self.assertRaises(ScenarioError):
            gas.GAS_MODULE.run(SHOCK, not_a_parameter=1.0)
        with self.assertRaises(ScenarioError):
            gas.GAS_MODULE.run("gas_never_declared")


# ---------------------------------------------------------------------------------------
# 4. Determinism, provenance and aggregate-only output
# ---------------------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_runs_are_byte_identical_and_digests_are_stable(self):
        first = gas.GAS_MODULE.run(DIVERSION).as_dict()
        second = gas.GAS_MODULE.run(DIVERSION).as_dict()
        self.assertEqual(first, second)
        self.assertEqual(len(first["digest"]), 64)
        self.assertEqual(first["digest"], second["digest"])
        payload = gas.build_report([SHOCK, SOFT])
        again = gas.build_report([SHOCK, SOFT])
        self.assertEqual(payload, again)
        self.assertEqual(payload["digest"], again["digest"])
        self.assertEqual(len(payload["digest"]), 64)

    def test_report_is_aggregate_only_and_carries_no_raw_series(self):
        report = gas.build_report()
        text = json.dumps(report, ensure_ascii=False)
        lowered = text.lower()
        for forbidden in ("observations", "csv", "timestamp", "generated_at", "raw_data",
                          "data/raw", "build/gas"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("g:/", lowered)
        self.assertNotIn("c:/", lowered)
        for length, path in numeric_lists(report):
            self.assertLessEqual(length, 12, f"numeric list of {length} values at {path}")
        self.assertNotIn('"matrix"', text)
        self.assertIn("RESEARCH_ONLY", text)

    def test_assumptions_have_provenance_and_replace_with(self):
        result = gas.GAS_MODULE.run(DIVERSION)
        classes = {item.source_class for item in result.assumptions}
        self.assertTrue(classes <= set(SOURCE_CLASSES))
        self.assertIn("assumption", classes)
        self.assertIn("derived", classes)
        self.assertNotIn("observed", classes)  # no third-party gas series is used
        self.assertGreaterEqual(len(result.assumptions), 40)
        values = {item.name: item for item in result.assumptions}
        self.assertEqual(values["ttf_usd_per_mmbtu"].source_class, "derived")
        self.assertAlmostEqual(float(values["ttf_usd_per_mmbtu"].value), TTF_BASE, places=5)
        self.assertEqual(values["eu_demand_bcm_y"].source_class, "assumption")
        replace_with = result.meta["replace_with"]
        self.assertGreaterEqual(len(replace_with), 30)
        self.assertTrue(all(item["source"] for item in replace_with))
        self.assertEqual(len({item["name"] for item in replace_with}), len(replace_with))
        self.assertIn("Henry Hub is a declared reference", result.meta["notes"][0])

    def test_module_registry_and_passport(self):
        module = get_module(gas.MODULE_ID)
        self.assertIs(module, gas.GAS_MODULE)
        self.assertIsInstance(module, MarketModule)
        passport = module.describe()
        self.assertEqual(passport["module_id"], gas.MODULE_ID)
        self.assertEqual(passport["units"], "USD/MMBtu")
        self.assertEqual(passport["status"], RESEARCH_ONLY)
        self.assertTrue(passport["research_only"])
        self.assertEqual(len(passport["scenarios"]), 5)
        self.assertEqual(passport["default_scenario"], SHOCK)
        self.assertIn(gas.MODULE_ID, [item["module_id"] for item in modules_document()])

    def test_report_subset_and_altered_overrides_change_the_digest(self):
        full = gas.build_report()
        subset = gas.build_report([SOFT])
        self.assertEqual(len(subset["scenarios"]), 1)
        self.assertEqual(subset["scenario_ids"], [SOFT])
        self.assertNotEqual(full["digest"], subset["digest"])
        altered = gas.build_report([SOFT], {"ttf_price_eur_per_mwh": 50.0})
        self.assertGreater(altered["scenarios"][0]["summary"]["base_price_usd_per_mmbtu"],
                           full["scenarios"][0]["summary"]["base_price_usd_per_mmbtu"])


# ---------------------------------------------------------------------------------------
# 5. Fail-closed parameter handling
# ---------------------------------------------------------------------------------------
class FailClosedTests(unittest.TestCase):
    def test_impossible_scenario_inputs_are_refused(self):
        cases = (
            {"pipeline_loss_bcm_y": -1.0},
            {"pipeline_loss_bcm_y": 80.0},          # above Russian + other pipeline supply
            {"storage_stock_bcm": 101.0},           # above capacity
            {"storage_capacity_bcm": 0.0},
            {"elasticity_short": 0.1},
            {"elasticity_short": -0.5, "elasticity_long": -0.2},
            {"storage_cover_share": 1.5},
            {"demand_shift_share": -0.8},
            {"lng_diversion_capacity_bcm_y": 400.0},  # above Asian LNG supply
            {"eur_usd": 0.0},
            {"pipeline_loss_months": 0},
            {"jkm_price_usd_per_mmbtu": -1.0},
        )
        for overrides in cases:
            with self.assertRaises(MarketError, msg=str(overrides)):
                gas.GAS_MODULE.run(SHOCK, **overrides)

    def test_diversion_without_capacity_needs_no_arbitrage_solver(self):
        result = gas.GAS_MODULE.run(SOFT, resolve_diversion=True)
        self.assertEqual(result.summary["arbitrage"]["diversion"]["binding"], "none")
        self.assertEqual(result.summary["arbitrage"]["diversion"]["diversion_bcm_y"], 0.0)
        self.assertEqual(result.summary["jkm_peak_usd_per_mmbtu"], 12.0)

    def test_capacity_binding_case_is_reported_not_hidden(self):
        info = gas.arbitrage_clearing_diversion(
            {**gas.DEFAULT_PARAMS, "pipeline_loss_bcm_y": 70.0,
             "lng_diversion_capacity_bcm_y": 5.0}, 6)
        self.assertEqual(info["binding"], "capacity")
        self.assertEqual(info["diversion_bcm_y"], 5.0)
        self.assertGreater(info["residual"], 0.0)

    def test_zero_arbitrage_window_returns_no_diversion(self):
        info = gas.arbitrage_clearing_diversion(
            {**gas.DEFAULT_PARAMS, "pipeline_loss_bcm_y": 0.0,
             "lng_diversion_capacity_bcm_y": 40.0}, 6)
        self.assertEqual(info["binding"], "none")
        self.assertEqual(info["diversion_bcm_y"], 0.0)
        self.assertLessEqual(info["residual"], 0.0)


# ---------------------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gas-scenarios-", dir=str(ROOT / "build")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _out(self, name: str) -> Path:
        return self.tmp / name

    def test_cli_writes_json_and_markdown(self):
        out = self._out("full")
        code = gas_scenarios.main(["--out", str(out)])
        self.assertEqual(code, 0)
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], gas_scenarios.SCHEMA)
        self.assertEqual(report["status"], RESEARCH_ONLY)
        self.assertEqual(len(report["scenarios"]), 5)
        text = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("RESEARCH_ONLY", text)
        for scenario_id in ALL_SCENARIOS:
            self.assertIn(scenario_id, text)
        self.assertIsNone(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text))

    def test_cli_is_deterministic_byte_for_byte(self):
        first, second = self._out("det1"), self._out("det2")
        self.assertEqual(gas_scenarios.main(["--out", str(first), "--quiet"]), 0)
        self.assertEqual(gas_scenarios.main(["--out", str(second), "--quiet"]), 0)
        self.assertEqual((first / "report.json").read_bytes(),
                         (second / "report.json").read_bytes())
        self.assertEqual((first / "report.md").read_bytes(),
                         (second / "report.md").read_bytes())

    def test_cli_json_mode_writes_nothing(self):
        out = self._out("json-only")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = gas_scenarios.main(["--json", "--out", str(out), "--scenario", SOFT])
        self.assertEqual(code, 0)
        self.assertFalse((out / "report.json").exists())
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["scenario_ids"], [SOFT])

    def test_cli_fails_closed_without_writing(self):
        out = self._out("bad")
        stderr = io.StringIO()
        for argv in (["--param", "pipeline_loss_bcm_y=500"],
                     ["--param", "nope=1"],
                     ["--param", "eur_usd"],
                     ["--param", "resolve_diversion=maybe"],
                     ["--scenario", "unknown_scenario"]):
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = gas_scenarios.main(["--out", str(out), *argv])
            self.assertEqual(code, gas_scenarios.EXIT_INVALID, argv)
            self.assertFalse((out / "report.json").exists(), argv)
        self.assertIn("gas scenario error", stderr.getvalue())

    def test_cli_override_typing_and_listing(self):
        overrides = gas_scenarios.parse_overrides(
            ["resolve_diversion=true", "pipeline_loss_months=3", "risk_premium_usd_per_mmbtu=4.5"])
        self.assertIs(overrides["resolve_diversion"], True)
        self.assertEqual(overrides["pipeline_loss_months"], 3)
        self.assertIsInstance(overrides["risk_premium_usd_per_mmbtu"], float)
        with self.assertRaises(MarketError):
            gas_scenarios.parse_overrides(["pipeline_loss_months=2.7"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(gas_scenarios.main(["--list-scenarios"]), 0)
        listing = buffer.getvalue()
        for scenario_id in ALL_SCENARIOS:
            self.assertIn(scenario_id, listing)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(gas_scenarios.main(["--list-parameters"]), 0)
        self.assertIn("ttf_price_eur_per_mwh", buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
