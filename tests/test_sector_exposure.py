#!/usr/bin/env python3
"""Tests of the sector exposure MVP (tools/sector_exposure.py, docs/179, task-61).

Offline and hermetic: the forecast -> world bridge report, the price bridge and every report are
synthetic fixtures in a temporary directory.  One integration test runs on the real repository
artefacts and is skipped when they are absent (they are generated, not committed).

Covered: the 15-row ISIC/NACE/SIC crosswalk and its uniqueness, loading and fail-closed checks of
both source artefacts, scenario selection, output-side bucket arithmetic and balances, country
allocation, the price-bridge cost incidence (and its fallbacks), the uniform cost mode, conditional
companies, the demo on three industries and two companies, determinism, the absence of raw data,
and the CLI exit codes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sector_exposure as se  # noqa: E402

DATASET = "oecd_2025"
BASE_OUTPUT = 1000.0
TOTAL_DELTA = -100.0
T1_SHIFT = 0.02
GENERATED_AT = "2026-11-18T00:00:00Z"


def write_json(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def scenario_document(scenario_id: str = "inflation_plus_1pp", *,
                      total: float = TOTAL_DELTA, base: float = BASE_OUTPUT,
                      with_price: bool = True) -> dict:
    codes = se.mapped_codes(DATASET)
    share = total / len(codes)
    industries = [{"name": code, "delta_output": share,
                   "share_of_abs": round(abs(share) / abs(total), 9)}
                  for code in codes]
    countries = [{"name": "AA", "delta_output": total * 0.5},
                 {"name": "BB", "delta_output": total * 0.3},
                 {"name": "CC", "delta_output": total * 0.2}]
    price_channel = ({"status": "OK", "relative_price_shock": T1_SHIFT,
                      "relative_price_shock_interval": [-0.01, 0.05]}
                     if with_price else
                     {"status": "NOT_APPLIED", "relative_price_shock": 0.0,
                      "relative_price_shock_interval": [0.0, 0.0]})
    return {
        "id": scenario_id,
        "label": "Инфляция +1 пп" if with_price else "Рост −1 пп",
        "status": "OK",
        "shifts": {"cpi_pp": 1.0 if with_price else 0.0,
                   "growth_pp": 0.0 if with_price else -1.0},
        "combined": {
            "base_output_usd_mn": base,
            "delta_output_usd_mn": total,
            "relative_output": total / base,
            "interval": {"delta_low": -1.5 * abs(total), "delta_high": 0.5 * abs(total)},
        },
        "price_channel": price_channel,
        "growth_channel": {"status": "NOT_APPLIED", "relative_demand_shock": 0.0},
        "industries": industries,
        "countries": countries,
    }


class Fixture:
    """Minimal but structurally faithful bridge + price-bridge artefacts."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.forecast_world = write_json(root / "forecast_world.json", {
            "schema": "amfe.forecast_world_bridge.v1",
            "status": "RESEARCH_ONLY",
            "admission": False,
            "generated_at": GENERATED_AT,
            "model": {"dataset": DATASET},
            "checks": {"no_raw_data": True, "fail_closed": True},
            "scenarios": [
                scenario_document(),
                scenario_document("growth_minus_1pp", total=-50.0, with_price=False),
                {"id": "base", "label": "Базовый", "status": "OK",
                 "shifts": {"cpi_pp": 0.0, "growth_pp": 0.0},
                 "combined": {"base_output_usd_mn": BASE_OUTPUT,
                              "delta_output_usd_mn": 0.0, "relative_output": 0.0,
                              "interval": {"delta_low": 0.0, "delta_high": 0.0}},
                 "price_channel": {"status": "NOT_APPLIED", "relative_price_shock": 0.0},
                 "growth_channel": {"status": "NOT_APPLIED"},
                 "industries": [], "countries": []},
                {"id": "blocked_scenario", "status": "BLOCKED_INPUT",
                 "reason": "нет прогноза T2", "combined": {"delta_output_usd_mn": None},
                 "industries": [], "countries": []},
            ],
        })
        self.price_bridge = write_json(root / "price_bridge.json", {
            "schema": "amfe.markets.mrio_bridge.v1",
            "status": "RESEARCH_ONLY",
            "admission": False,
            "price_results": {
                DATASET: {
                    "status": "OK",
                    "variants": {
                        "0.3": {"pass_through": 0.3,
                                "results": {"aggregate_price_index_pct": 2.5,
                                            "top_industries": []}},
                        "0.6": {"pass_through": 0.6,
                                "results": {"aggregate_price_index_pct": 5.0,
                                            "top_industries": [
                                                {"name": "D", "delta_price_relative": 0.10},
                                                {"name": "B05", "delta_price_relative": 0.05},
                                                {"name": "B07", "delta_price_relative": 0.15},
                                                {"name": "C19", "delta_price_relative": 0.20},
                                            ]}},
                    },
                },
            },
        })

    def forecast_with(self, mutate) -> Path:
        document = json.loads(self.forecast_world.read_text(encoding="utf-8"))
        mutate(document)
        return write_json(self.root / "forecast_modified.json", document)


class SectorExposureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)

    def run_report(self, **kwargs) -> dict:
        options = {
            "forecast_world": self.fixture.forecast_world,
            "price_bridge": self.fixture.price_bridge,
            "dataset": DATASET,
            "scenario_id": "inflation_plus_1pp",
            "pass_through": 0.6,
            "cost_mode": "price_bridge",
            "top_n": 5,
            "demo": False,
            "out_dir": None,
            "generated_at": GENERATED_AT,
        }
        options.update(kwargs)
        return se.build_report(**options)["report"]

    def bucket(self, report: dict, bucket_id: str) -> dict:
        return next(item for item in report["industries"] if item["id"] == bucket_id)


class CrosswalkTest(unittest.TestCase):
    def test_crosswalk_has_fifteen_unique_buckets(self):
        ids = [entry["id"] for entry in se.SECTOR_CROSSWALK]
        self.assertEqual(len(ids), 15)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(se.DEMO_INDUSTRIES), 3)
        self.assertEqual(len(se.DEFAULT_COMPANIES), 2)

    def test_codes_are_unique_per_dataset(self):
        for dataset in se.DATASETS:
            mapping = se.code_map(dataset)
            self.assertEqual(len(mapping), len(set(mapping)))
            self.assertGreaterEqual(len(mapping), 25)

    def test_forward_and_reverse_mapping(self):
        self.assertEqual(se.bucket_for_code("B06", DATASET), "mining_extraction")
        self.assertEqual(se.bucket_for_code("D", DATASET), "electricity_gas_water")
        self.assertEqual(se.bucket_for_code("G", DATASET), "trade_repair")
        self.assertEqual(se.bucket_for_code("CPA_B", "figaro"), "mining_extraction")
        self.assertEqual(se.bucket_for_code("CPA_C24", "figaro"), "metals")

    def test_unknown_code_and_bucket(self):
        self.assertIsNone(se.bucket_for_code("ZZZ", DATASET))
        with self.assertRaises(se.MappingError):
            se.crosswalk_entry("not_a_sector")

    def test_mapping_table_is_json_ready(self):
        table = se.mapping_table(DATASET)
        self.assertEqual(len(table), 15)
        for item in table:
            self.assertTrue(item["mrio_codes"])
            json.dumps(item, ensure_ascii=False)


class FailClosedTest(SectorExposureTestCase):
    def test_missing_forecast_world_is_refused(self):
        with self.assertRaises(se.MissingInput):
            self.run_report(forecast_world=self.root / "absent.json")

    def test_non_research_source_is_refused(self):
        path = self.fixture.forecast_with(lambda doc: doc.update({"status": "DRAFT"}))
        with self.assertRaises(se.SectorExposureError):
            self.run_report(forecast_world=path)

    def test_admission_true_is_refused(self):
        path = self.fixture.forecast_with(lambda doc: doc.update({"admission": True}))
        with self.assertRaises(se.SectorExposureError):
            self.run_report(forecast_world=path)

    def test_no_raw_data_flag_is_required(self):
        path = self.fixture.forecast_with(
            lambda doc: doc["checks"].update({"no_raw_data": False}))
        with self.assertRaises(se.SectorExposureError):
            self.run_report(forecast_world=path)

    def test_missing_scenario_is_refused(self):
        with self.assertRaises(se.MissingInput):
            self.run_report(scenario_id="not_a_scenario")

    def test_blocked_scenario_is_refused_without_writing(self):
        out = self.root / "out"
        with self.assertRaises(se.MissingInput):
            self.run_report(scenario_id="blocked_scenario", out_dir=out)
        self.assertFalse((out / "sector_exposure.json").exists(),
                         "fail-closed run must not write a report")

    def test_missing_price_bridge_is_refused(self):
        with self.assertRaises(se.MissingInput):
            self.run_report(price_bridge=self.root / "absent.json")

    def test_price_bridge_unknown_dataset_is_refused(self):
        document = json.loads(self.fixture.price_bridge.read_text(encoding="utf-8"))
        document["price_results"] = {"another_dataset": {"status": "OK", "variants": {}}}
        path = write_json(self.root / "price_modified.json", document)
        with self.assertRaises(se.MissingInput):
            self.run_report(price_bridge=path)

    def test_dataset_mismatch_is_refused(self):
        with self.assertRaises(se.SectorExposureError):
            self.run_report(dataset="figaro")

    def test_scenario_relative_mismatch_is_refused(self):
        def mutate(document):
            document["scenarios"][0]["combined"]["relative_output"] = -0.5
        path = self.fixture.forecast_with(mutate)
        with self.assertRaises(se.SectorExposureError):
            self.run_report(forecast_world=path)

    def test_industry_margin_mismatch_is_refused(self):
        def mutate(document):
            document["scenarios"][0]["industries"][0]["delta_output"] += 10.0
        path = self.fixture.forecast_with(mutate)
        with self.assertRaises(se.SectorExposureError):
            self.run_report(forecast_world=path)

    def test_mapping_code_absent_from_margins_is_refused(self):
        def mutate(document):
            items = document["scenarios"][0]["industries"]
            removed = next(item for item in items if item["name"] == "B06")
            document["scenarios"][0]["industries"] = [
                item for item in items if item["name"] != "B06"]
            document["scenarios"][0]["industries"][0]["delta_output"] += removed["delta_output"]
        path = self.fixture.forecast_with(mutate)
        with self.assertRaises(se.MappingError):
            self.run_report(forecast_world=path)

    def test_invalid_bounds_are_refused(self):
        for options in ({"pass_through": -0.1}, {"pass_through": 1.1}, {"top_n": 0},
                        {"cost_mode": "not_a_mode"}):
            with self.subTest(options=options), self.assertRaises(se.SectorExposureError):
                self.run_report(**options)

    def test_zero_shock_scenario_is_allowed(self):
        report = self.run_report(scenario_id="base")
        self.assertEqual(len(report["industries"]), 15)
        for item in report["industries"]:
            self.assertEqual(item["delta_output_usd_mn"], 0.0)
            self.assertEqual(item["cost"]["status"], "NOT_APPLIED")
        self.assertTrue(report["checks"]["bucket_deltas_match_margins"])


class OutputSideTest(SectorExposureTestCase):
    def test_bucket_output_matches_bridge_margin(self):
        report = self.run_report()
        codes = se.crosswalk_entry("electricity_gas_water")["mrio"][DATASET]
        expected = TOTAL_DELTA / len(se.mapped_codes(DATASET)) * len(codes)
        bucket = self.bucket(report, "electricity_gas_water")
        self.assertAlmostEqual(bucket["delta_output_usd_mn"], expected, places=6)
        self.assertAlmostEqual(bucket["relative_to_world_output"], expected / BASE_OUTPUT,
                               places=12)
        self.assertAlmostEqual(bucket["share_of_scenario_change"], expected / TOTAL_DELTA,
                               places=12)

    def test_all_buckets_sum_to_mapped_margin(self):
        report = self.run_report()
        total = sum(item["delta_output_usd_mn"] for item in report["industries"])
        self.assertAlmostEqual(total, TOTAL_DELTA, delta=1e-5)
        self.assertTrue(report["checks"]["bucket_deltas_match_margins"])

    def test_country_cells_sum_to_bucket(self):
        report = self.run_report(top_n=10)
        bucket = self.bucket(report, "electricity_gas_water")
        cells = sum(item["delta_output_usd_mn"] for item in bucket["top_countries"])
        self.assertAlmostEqual(cells, bucket["delta_output_usd_mn"], delta=1e-5)
        self.assertEqual([item["country"] for item in bucket["top_countries"]],
                         ["AA", "BB", "CC"])

    def test_interval_is_ordered_and_scaled(self):
        report = self.run_report()
        bucket = self.bucket(report, "electricity_gas_water")
        low, high = bucket["interval_usd_mn"]
        self.assertLessEqual(low, high)
        factor = bucket["delta_output_usd_mn"] / TOTAL_DELTA
        self.assertAlmostEqual(low, min(factor * -150.0, factor * 50.0), places=6)


class CostSideTest(SectorExposureTestCase):
    def test_price_bridge_incidence_and_cost_change(self):
        report = self.run_report()
        electricity = self.bucket(report, "electricity_gas_water")["cost"]
        self.assertEqual(electricity["status"], "OK")
        self.assertAlmostEqual(electricity["incidence"], 2.0, places=9)
        self.assertAlmostEqual(electricity["relative_change"], 2.0 * T1_SHIFT, places=12)
        self.assertEqual(electricity["matched_codes"], ["D"])
        self.assertEqual(electricity["fallback_codes"], [])

    def test_partial_fallback_uses_aggregate_incidence(self):
        report = self.run_report()
        extraction = self.bucket(report, "mining_extraction")["cost"]
        self.assertEqual(extraction["status"], "PARTIAL_FALLBACK")
        self.assertAlmostEqual(extraction["incidence"], 1.4, places=9)
        self.assertEqual(extraction["fallback_codes"], ["B06", "B08", "B09"])

    def test_aggregate_fallback_when_no_code_matched(self):
        report = self.run_report()
        trade = self.bucket(report, "trade_repair")["cost"]
        self.assertEqual(trade["status"], "AGGREGATE_FALLBACK")
        self.assertAlmostEqual(trade["incidence"], 1.0, places=12)
        self.assertAlmostEqual(trade["relative_change"], T1_SHIFT, places=12)

    def test_uniform_cost_mode(self):
        report = self.run_report(cost_mode="uniform")
        for item in report["industries"]:
            self.assertEqual(item["cost"]["status"], "UNIFORM")
            self.assertAlmostEqual(item["cost"]["incidence"], 1.0, places=12)
            self.assertAlmostEqual(item["cost"]["relative_change"], T1_SHIFT, places=12)

    def test_cost_not_applied_without_price_shock(self):
        report = self.run_report(scenario_id="growth_minus_1pp")
        bucket = self.bucket(report, "electricity_gas_water")
        self.assertEqual(bucket["cost"]["status"], "NOT_APPLIED")
        self.assertIsNone(bucket["cost"]["relative_change"])
        self.assertAlmostEqual(bucket["delta_output_usd_mn"], -50.0 / 30.0, places=6)


class CompanyTest(SectorExposureTestCase):
    def test_company_weights_and_weighted_effects(self):
        report = self.run_report(demo=True)
        companies = {item["id"]: item for item in report["companies"]}
        self.assertEqual(len(companies), 2)
        company = companies["conditional_energy_intensive"]
        expected_cost = (0.35 * 1.4 * T1_SHIFT + 0.45 * 2.0 * T1_SHIFT
                         + 0.20 * 1.0 * T1_SHIFT)
        self.assertAlmostEqual(company["weight_sum"], 1.0, places=12)
        self.assertAlmostEqual(company["weighted_cost_relative"], expected_cost, places=12)
        self.assertEqual(company["status"], "CONDITIONAL")

    def test_company_with_bad_weights_is_refused(self):
        report = self.run_report(demo=True)
        buckets = {item["id"]: item for item in report["industries"]}
        with self.assertRaises(se.MappingError):
            se.company_profile({"id": "bad", "label_ru": "bad",
                                "weights": {"mining_extraction": 0.5, "trade_repair": 0.2}},
                               buckets)
        with self.assertRaises(se.MappingError):
            se.company_profile({"id": "bad", "label_ru": "bad",
                                "weights": {"unknown_bucket": 1.0}}, buckets)

    def test_demo_has_three_industries_and_two_companies(self):
        report = self.run_report(demo=True)
        self.assertEqual(report["mode"], "demo")
        self.assertEqual([item["id"] for item in report["industries"]],
                         list(se.DEMO_INDUSTRIES))
        self.assertEqual(len(report["companies"]), 2)

    def test_full_report_covers_all_fifteen_buckets(self):
        report = self.run_report()
        self.assertEqual(report["mode"], "full")
        self.assertEqual(len(report["industries"]), 15)
        self.assertEqual(report["mapping"]["coverage"]["mapped_codes"], 30)


class DeterminismAndPayloadTest(SectorExposureTestCase):
    def test_determinism_byte_identical(self):
        first = se.build_report(
            forecast_world=self.fixture.forecast_world, price_bridge=self.fixture.price_bridge,
            dataset=DATASET, scenario_id="inflation_plus_1pp", pass_through=0.6,
            cost_mode="price_bridge", top_n=5, demo=False,
            out_dir=self.root / "out_a", generated_at=GENERATED_AT)
        second = se.build_report(
            forecast_world=self.fixture.forecast_world, price_bridge=self.fixture.price_bridge,
            dataset=DATASET, scenario_id="inflation_plus_1pp", pass_through=0.6,
            cost_mode="price_bridge", top_n=5, demo=False,
            out_dir=self.root / "out_b", generated_at=GENERATED_AT)
        self.assertEqual(Path(first["report_path"]).read_bytes(),
                         Path(second["report_path"]).read_bytes())
        self.assertEqual(Path(first["summary_path"]).read_bytes(),
                         Path(second["summary_path"]).read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])

    def test_payload_has_no_raw_markers(self):
        report = self.run_report()
        self.assertEqual(se.scan_for_raw(report), [])
        payload = json.dumps(report, ensure_ascii=False)
        for marker in ("data/mrio", "dataset.json", "F:\\\\", "F:/"):
            self.assertNotIn(marker, payload)

    def test_scan_detects_planted_raw_markers(self):
        self.assertTrue(se.scan_for_raw({"Z": [[1.0]]}))
        self.assertTrue(se.scan_for_raw({"path": "data/mrio/figaro/dataset.json"}))
        self.assertTrue(se.scan_for_raw({"nested": {"x": [1.0, 2.0]}}))

    def test_research_only_flags(self):
        report = self.run_report()
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertIs(report["admission"], False)
        self.assertTrue(report["checks"]["research_only"])
        self.assertTrue(report["checks"]["no_raw_data"])
        self.assertTrue(report["checks"]["fail_closed"])
        self.assertTrue(report["limitations"])


class CliTest(SectorExposureTestCase):
    def test_cli_success_writes_both_files(self):
        out = self.root / "cli"
        code = se.main([
            "--forecast-world", str(self.fixture.forecast_world),
            "--price-bridge", str(self.fixture.price_bridge),
            "--dataset", DATASET, "--scenario", "inflation_plus_1pp",
            "--out", str(out), "--generated-at", GENERATED_AT,
        ])
        self.assertEqual(code, 0)
        self.assertTrue((out / "sector_exposure.json").is_file())
        self.assertTrue((out / "sector_exposure.md").is_file())

    def test_cli_blocked_scenario_returns_2_and_writes_nothing(self):
        out = self.root / "cli_blocked"
        code = se.main([
            "--forecast-world", str(self.fixture.forecast_world),
            "--price-bridge", str(self.fixture.price_bridge),
            "--dataset", DATASET, "--scenario", "blocked_scenario",
            "--out", str(out), "--generated-at", GENERATED_AT,
        ])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())


class RealArtefactTest(unittest.TestCase):
    """Integration check on the generated repository artefacts (skipped when absent)."""

    @unittest.skipUnless(
        se.DATASETS["oecd_2025"]["forecast_world"].is_file()
        and se.DEFAULT_PRICE_BRIDGE.is_file(),
        "real build/ artefacts are absent (generated, not committed)")
    def test_real_bridge_consistency(self):
        outcome = se.build_report(
            forecast_world=se.DATASETS["oecd_2025"]["forecast_world"],
            price_bridge=se.DEFAULT_PRICE_BRIDGE, dataset="oecd_2025",
            scenario_id="inflation_plus_1pp", pass_through=0.6,
            cost_mode="price_bridge", top_n=5, demo=False, out_dir=None,
            generated_at=GENERATED_AT)
        report = outcome["report"]
        source = json.loads(
            se.DATASETS["oecd_2025"]["forecast_world"].read_text(encoding="utf-8"))
        scenario = next(item for item in source["scenarios"]
                        if item["id"] == "inflation_plus_1pp")
        self.assertEqual(report["scenario"]["delta_output_usd_mn"],
                         scenario["combined"]["delta_output_usd_mn"])
        self.assertTrue(report["checks"]["bucket_deltas_match_margins"])
        self.assertEqual(len(report["industries"]), 15)
        self.assertEqual(len(report["companies"]), 2)
        self.assertGreater(report["mapping"]["coverage"]["mapped_codes"], 25)


if __name__ == "__main__":
    unittest.main()
