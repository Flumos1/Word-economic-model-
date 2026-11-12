"""Tests for the world-core calibration on the 19-series data plan (``tools/calibrate_world.py``).

The suite checks the calibration report (V1): structure, determinism, content hash, absence of raw
series, parameter ranges and mapping into ``WorldConfig``, the three-status V2 vocabulary of
docs/92, the data-plan/licence coverage, the missing-data mode and the world-core baseline digest.
It does not re-estimate anything with real data by hand: it reads the local git-ignored
``data/raw`` through the same tool (the calibration is fast, < 1 s) and is skipped when the data are
absent.

Run from the repository root: ``python -m unittest tests.test_world_calibration -v``.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import calibrate_world as calibration  # noqa: E402
from amfe.world import PhysicalConfig, WorldConfig, WorldCore, baseline_scenario  # noqa: E402

LOCAL_DATA = (ROOT / "data" / "raw" / "GDPC1" / "latest.csv").exists()
HEX64 = set("0123456789abcdef")


def is_hex_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def _recompute_content_sha(report: dict) -> str:
    content = {key: value for key, value in report.items()
               if key not in ("content_sha256", "generated_at")}
    return calibration._content_sha(content)


@unittest.skipUnless(LOCAL_DATA, "local data/raw download is not available")
class ReportStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = calibration.calibrate()

    def test_required_sections_and_status(self):
        report = self.report
        self.assertEqual(report["calibration_id"], "AMFE-WORLD-CALIBRATION")
        self.assertEqual(report["status"], "COMPLETE")
        self.assertFalse(report["raw_data_committed"])
        for key in ("version", "tool", "plan", "windows", "sources", "parameters",
                    "derived_aggregates", "v2_checks", "criteria", "world_core",
                    "missing_series", "limitations", "next_steps", "content_sha256"):
            self.assertIn(key, report)
        self.assertEqual(report["missing_series"], [])
        self.assertEqual(len(report["sources"]), len(calibration.load_plan()["series"]))
        self.assertGreaterEqual(len(report["parameters"]), 15)
        self.assertGreaterEqual(len(report["v2_checks"]), 12)
        self.assertGreaterEqual(len(report["limitations"]), 5)

    def test_determinism_and_content_hash(self):
        first = calibration.calibrate()
        second = calibration.calibrate()
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertTrue(is_hex_digest(first["content_sha256"]))
        self.assertEqual(first["content_sha256"], _recompute_content_sha(first))

    def test_only_aggregates_no_raw_values(self):
        report = self.report
        self.assertEqual(calibration.find_raw_values(report), [])
        text = json.dumps(report, ensure_ascii=False)
        for forbidden in ('"raw_values"', '"series_values"', '"observations_values"'):
            self.assertNotIn(forbidden, text)
        self.assertNotIn('"values": [', text)

    def test_sources_follow_the_data_plan(self):
        plan = calibration.load_plan()
        plan_ids = {entry["id"] for entry in plan["series"]}
        source_ids = {source["series"] for source in self.report["sources"]}
        self.assertEqual(source_ids, plan_ids)
        for source in self.report["sources"]:
            self.assertIsNotNone(source["register"])
            self.assertIsNotNone(source["licence_label"])
            self.assertIn("Public Domain", source["licence_label"])
            self.assertTrue(is_hex_digest(source["file_sha256"]))
            self.assertGreater(source["rows"], 0)
            self.assertLess(source["first_observation"], source["last_observation"])
        self.assertEqual(self.report["plan"]["version"], plan["version"])


@unittest.skipUnless(LOCAL_DATA, "local data/raw download is not available")
class ParametersAndMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = calibration.calibrate()
        cls.parameters = {parameter["id"]: parameter for parameter in cls.report["parameters"]}

    def test_parameter_ranges(self):
        params = self.parameters
        for field in ("household_demand_share", "investment_demand_share",
                      "government_demand_share"):
            self.assertIsNotNone(params[field]["value"], field)
            self.assertGreater(params[field]["value"], 0.0)
            self.assertLess(params[field]["value"], 1.0)
        self.assertAlmostEqual(params["household_demand_share"]["value"]
                               + params["investment_demand_share"]["value"]
                               + params["government_demand_share"]["value"], 1.0, delta=2e-6)
        self.assertGreater(params["real_growth_annualized"]["value"], -2.0)
        self.assertLess(params["real_growth_annualized"]["value"], 6.0)
        for field in ("inflation_core_cpi_yoy", "inflation_core_pce_yoy",
                      "inflation_gdp_deflator_yoy"):
            self.assertGreater(params[field]["value"], -2.0)
            self.assertLess(params[field]["value"], 10.0)
        self.assertGreater(params["nairu_proxy"]["value"], 3.0)
        self.assertLess(params["nairu_proxy"]["value"], 8.0)
        self.assertGreater(params["productivity_trend"]["value"], 0.0)
        self.assertLess(params["productivity_trend"]["value"], 4.0)
        self.assertLess(params["net_export_share"]["value"], 0.05)
        self.assertGreater(params["net_export_share"]["value"], -0.15)
        # cross-check against the earlier stage-2 calibration where the two overlap
        core = calibration.load_core_calibration()
        core_values = {parameter["id"]: parameter["value"]
                       for parameter in core.get("parameters", [])}
        if core_values.get("nairu_proxy") is not None:
            self.assertAlmostEqual(params["nairu_proxy"]["value"],
                                   core_values["nairu_proxy"], places=3)
        if core_values.get("productivity_trend") is not None:
            self.assertAlmostEqual(params["productivity_trend"]["value"],
                                   core_values["productivity_trend"], places=3)
        # every mapped parameter carries a value for the world-core configuration
        for parameter in self.report["parameters"]:
            if parameter.get("world_core_target"):
                self.assertIn("world_core_value", parameter)

    def test_world_config_is_rebuilt_and_valid(self):
        config = calibration.load_world_config(self.report)
        self.assertIsNotNone(config)
        self.assertIsInstance(config, WorldConfig)
        physical = config.country_a.physical
        self.assertIsInstance(physical, PhysicalConfig)
        self.assertAlmostEqual(physical.household_demand_share
                               + physical.government_demand_share
                               + physical.investment_demand_share, 1.0, places=12)
        params = self.parameters
        self.assertAlmostEqual(physical.household_demand_share,
                               params["household_demand_share"]["value"], places=5)
        self.assertAlmostEqual(config.country_a.real_growth,
                               params["real_growth_annualized"]["value"] / 400.0, places=6)
        self.assertAlmostEqual(config.country_a.export_share,
                               params["export_share"]["value"], places=6)
        self.assertGreater(config.country_a.inflation_target, 0.0)
        # country B stays the default placeholder (no non-US data in the plan)
        default_b = calibration.default_country_b()
        self.assertEqual(config.country_b.name, default_b.name)

    def test_growth_definitions(self):
        self.assertAlmostEqual(calibration._growth({0: 100.0, 1: 101.0})[1],
                               100.0 * (1.01 ** 4 - 1.0), places=9)
        self.assertAlmostEqual(calibration._yoy({0: 100.0, 4: 110.0})[4], 10.0, places=9)
        self.assertEqual(calibration._growth({0: 100.0, 1: 0.0}), {})


@unittest.skipUnless(LOCAL_DATA, "local data/raw download is not available")
class V2Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = calibration.calibrate()
        cls.checks = {check["id"]: check for check in cls.report["v2_checks"]}

    def test_three_status_vocabulary_and_required_checks(self):
        statuses = {check["status"] for check in self.report["v2_checks"]}
        self.assertEqual(statuses, {"PASS", "CONDITIONAL", "NOT_APPLICABLE"})
        for check_id in ("V2_savings_investment", "V2_savings_investment_growth",
                         "V2_inflation_measures", "V2_nairu_sanity", "V2_nairu_inflation",
                         "V2_income_expenditure", "V2_gdp_growth_series",
                         "V2_labour_activity", "V2_industry_activity",
                         "V2_retail_consumption", "V2_structural_parameters",
                         "V2_nominal_identity", "V2_world_aggregation"):
            self.assertIn(check_id, self.checks)
        for check in self.report["v2_checks"]:
            self.assertIn(check["status"], ("PASS", "CONDITIONAL", "NOT_APPLICABLE"))
            self.assertTrue(check["name"])
            self.assertIn("statistic", check)
            self.assertIn("threshold", check)
            self.assertIsInstance(check["note"], str)
        self.assertEqual(self.report["criteria"]["v2_thresholds"],
                         calibration.V2_THRESHOLDS)

    def test_savings_investment_cross_checks(self):
        level = self.checks["V2_savings_investment"]
        growth = self.checks["V2_savings_investment_growth"]
        self.assertIn(level["status"], ("PASS", "CONDITIONAL"))
        self.assertLess(level["statistic"]["mean_abs_residual_over_gdp"], 0.01)
        self.assertGreater(level["statistic"]["quarters"], 100)
        self.assertEqual(growth["status"], "PASS")
        self.assertLess(growth["statistic"]["max_abs_residual"], 0.02)

    def test_growth_series_cross_check(self):
        check = self.checks["V2_gdp_growth_series"]
        self.assertEqual(check["status"], "PASS")
        self.assertLessEqual(check["statistic"]["max_abs_diff_pp"],
                             calibration.V2_THRESHOLDS["growth_series_max_abs_diff_pp"])
        self.assertGreaterEqual(check["statistic"]["quarters"], 100)

    def test_nairu_and_inflation_checks(self):
        sanity = self.checks["V2_nairu_sanity"]
        self.assertEqual(sanity["status"], "PASS")
        self.assertGreater(sanity["statistic"]["proxy"], sanity["statistic"]["observed_min"])
        self.assertLess(sanity["statistic"]["proxy"], sanity["statistic"]["observed_max"])
        measures = self.checks["V2_inflation_measures"]
        self.assertIn(measures["status"], ("PASS", "CONDITIONAL"))
        for correlation in measures["statistic"]["correlations"].values():
            self.assertIsNotNone(correlation)
            self.assertGreaterEqual(correlation, -1.0)
            self.assertLessEqual(correlation, 1.0)
        phillips = self.checks["V2_nairu_inflation"]
        self.assertIn(phillips["status"], ("PASS", "CONDITIONAL"))
        # the descriptive slope is expected to be negative (higher unemployment gap -> lower inflation)
        self.assertLess(phillips["statistic"]["slope_pp_per_pp"], 0.0)

    def test_not_applicable_checks_carry_reasons(self):
        for check_id in ("V2_income_expenditure", "V2_structural_parameters",
                         "V2_nominal_identity", "V2_world_aggregation",
                         "V2_capital_energy_io"):
            check = self.checks[check_id]
            self.assertEqual(check["status"], "NOT_APPLICABLE")
            self.assertGreater(len(check["note"]), 20)


@unittest.skipUnless(LOCAL_DATA, "local data/raw download is not available")
class WorldCoreBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = calibration.calibrate()
        cls.world = cls.report["world_core"]

    def test_baseline_digest(self):
        self.assertEqual(self.world["status"], "COMPLETE")
        self.assertTrue(is_hex_digest(self.world["digest"]))
        self.assertTrue(is_hex_digest(self.world["config_sha256"]))
        self.assertTrue(self.world["ok"])
        self.assertTrue(self.world["all_periods_ok"])
        self.assertEqual(self.world["periods_reported"], self.world["periods"])
        self.assertEqual(self.world["calibrated_country"], "country_a")

    def test_baseline_run_is_deterministic(self):
        config = calibration.load_world_config(self.report)
        first = WorldCore(config, baseline_scenario()).run(periods=self.world["periods"])
        second = WorldCore(config, baseline_scenario()).run(periods=self.world["periods"])
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.digest, self.world["digest"])
        self.assertTrue(first.ok)


class MissingDataAndCli(unittest.TestCase):
    def test_missing_data_mode_returns_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = calibration.calibrate(Path(directory), allow_missing=True)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertGreater(len(report["missing_series"]), 0)
        self.assertEqual(report["world_core"]["status"], "NOT_APPLICABLE")
        self.assertEqual(report["sources"], [])
        for check in report["v2_checks"]:
            self.assertIn(check["status"], ("PASS", "CONDITIONAL", "NOT_APPLICABLE"))
        self.assertTrue(is_hex_digest(report["content_sha256"]))
        self.assertEqual(calibration.find_raw_values(report), [])

    def test_strict_mode_raises_on_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(calibration.CalibrationError):
                calibration.calibrate(Path(directory))

    @unittest.skipUnless(LOCAL_DATA, "local data/raw download is not available")
    def test_cli_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "world_calibration.json"
            code = calibration.main(["--skip-world-core", "--no-timestamp", "--out", str(out)])
            self.assertEqual(code, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "COMPLETE")
        self.assertNotIn("generated_at", report)
        self.assertEqual(report["world_core"]["status"], "SKIPPED")
        self.assertEqual(calibration.find_raw_values(report), [])

    def test_hygiene_scanner_catches_leaks(self):
        self.assertTrue(calibration.find_raw_values({"raw": [1.0]}))
        self.assertTrue(calibration.find_raw_values({"nested": {"series_values": [1, 2]}}))
        self.assertTrue(calibration.find_raw_values({"tail": list(range(100))}))
        self.assertEqual(calibration.find_raw_values({"statistic": {"observations": 120}}), [])


REPORT_PATH = ROOT / "protocols" / "world_calibration.json"


@unittest.skipUnless(REPORT_PATH.exists(), f"report not generated yet: {REPORT_PATH.name}")
class CommittedReport(unittest.TestCase):
    def test_committed_report_is_self_consistent_and_aggregate_only(self):
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(report["calibration_id"], "AMFE-WORLD-CALIBRATION")
        self.assertEqual(report["status"], "COMPLETE")
        self.assertIn("generated_at", report)
        self.assertEqual(calibration.find_raw_values(report), [])
        content = {key: value for key, value in report.items()
                   if key not in ("content_sha256", "generated_at")}
        self.assertEqual(report["content_sha256"], calibration._content_sha(content))
        self.assertEqual(len(report["sources"]), len(calibration.load_plan()["series"]))
        self.assertTrue(is_hex_digest(report["world_core"]["digest"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
