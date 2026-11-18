#!/usr/bin/env python3
"""Tests of the forecast -> world bridge (tools/forecast_world_bridge.py, docs/178, task-55).

Offline and hermetic: every world artefact (index, two scenario reports, quantity-bridge report)
and every product feed is a small synthetic fixture in a temporary directory; the module constants
pointing at the real `build/` artefacts are patched per test.  One integration test compares the
bridge with the real repository artefacts and is skipped when they are absent (they are generated,
not committed).

Covered: feed validation (interval/point/monotone/JSON), fail-closed strict mode, partial mode with
a blocked scenario, the three declared scenarios, both channel calibrations, the country x industry
allocation balances, determinism, the absence of raw data in the payload, the research-only flags
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

import forecast_world_bridge as fw  # noqa: E402

T1_POINT, T1_LOW, T1_HIGH = 3.0, 2.0, 4.0
T2_POINT, T2_LOW, T2_HIGH = 2.0, 1.0, 3.0
T3_POINT, T3_LOW, T3_HIGH = 0.2, 0.1, 0.4

BASE_OUTPUT = 200.0
BASE_FINAL_DEMAND = 100.0          # M = 2.0
PRICE_RELATIVE = 0.05
COMBINED_LONG = -0.02              # S = -0.4
GENERATED_AT = "2026-11-18T00:00:00Z"


def quantiles(low: float, high: float) -> list:
    step = (high - low) / (fw.QUANTILE_STEP - 1)
    return [round(low + position * step, 9) for position in range(fw.QUANTILE_STEP)]


def feed_document(target: str, point: float, low: float, high: float, horizon: int = 1) -> dict:
    return {
        "schema_version": "1.0.0",
        "generated_at": "2026-11-14T00:00:00Z",
        "status": "CANDIDATE",
        "target": target,
        "horizon": horizon,
        "origin": "2026-11",
        "point": point,
        "quantiles_5_to_95_step_5": quantiles(low, high),
        "candidate": "ENSEMBLE_INVERSE_MSE",
        "data_class": "REAL_TIME_RECONSTRUCTED",
    }


def write_json(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


class Fixture:
    """Minimal but structurally faithful world context + three product feeds."""

    def __init__(self, root: Path, *, dataset_total: float = -1000.0,
                 dataset_sha: str = "deadbeef") -> None:
        self.root = root
        self.reports = root / "reports"
        self.dataset_sha = dataset_sha
        self.dataset_total = dataset_total
        self._world(dataset_total)
        self.feeds = {
            "T1": self._feed("t1.json", "T1_CPI_YOY", T1_POINT, T1_LOW, T1_HIGH),
            "T2": self._feed("t2.json", "T2_GDP_GROWTH", T2_POINT, T2_LOW, T2_HIGH),
            "T3": self._feed("t3.json", "T3_RECESSION", T3_POINT, T3_LOW, T3_HIGH, horizon=3),
        }

    def _feed(self, name, target, point, low, high, horizon=1) -> Path:
        return write_json(self.root / "feeds" / name,
                          feed_document(target, point, low, high, horizon))

    def _world(self, total: float) -> None:
        by_country = {"AA": total * 0.5, "BB": total * 0.3, "CC": total * 0.2}
        by_industry = {"I1": total * 0.6, "I2": total * 0.3, "I3": total * 0.1}
        for scenario_id, role in (("import_plus20", "цена"), ("energy_minus5", "рост")):
            write_json(self.reports / f"{scenario_id}.json", {
                "schema": "amfe.world.scenario_report.v1",
                "status": "RESEARCH_ONLY",
                "scenario": {"id": scenario_id, "type": role, "description": "fixture"},
                "baseline": {"total_output": BASE_OUTPUT,
                             "total_final_demand": BASE_FINAL_DEMAND},
                "result": {"total_delta_output": total,
                           "relative_delta_output": total / BASE_OUTPUT},
                "impacts": {"by_country": by_country, "by_industry": by_industry},
                "checks": {"feasible": True, "validation_pass": True},
            })
        self.index = write_json(self.root / "report_index.json", {
            "schema": "amfe.world.scenario_index.v1",
            "status": "RESEARCH_ONLY",
            "checks": {"all_feasible": True, "all_sfc_pass": True,
                       "all_validations_pass": True, "max_abs_material_residual": 1e-8,
                       "scenario_count": 2},
            "dataset": {"dataset_id": "fixture_2019", "source": "fixture", "countries": 3,
                        "industries": 3, "sectors": 9, "years": [2019], "units": "USD_mn",
                        "sha256": self.dataset_sha},
            "scenarios": [
                {"id": "import_plus20", "headline": {"total_delta_output": total,
                                                     "relative_delta_output": total / BASE_OUTPUT}},
                {"id": "energy_minus5", "headline": {"total_delta_output": total,
                                                     "relative_delta_output": total / BASE_OUTPUT}},
            ],
            "sfc": {"pass": True, "countries": 3, "units": "USD_mn",
                    "checks": {"sfc_pass": True}}},
        )
        long_block = {"relative_delta_output": COMBINED_LONG}
        write_json(self.root / "quantity_report.json", {
            "schema": "amfe.quantity_bridge.v1",
            "status": "RESEARCH_ONLY",
            "demand_model": {"schema": "amfe.markets.demand.v1", "formula": "fixture",
                             "horizon_months": 6, "ramp_months": 6},
            "datasets": {"figaro": {
                "status": "OK",
                "dataset": {"sha256": self.dataset_sha, "countries": 3, "industries": 3},
                "variants": {"0.6": {
                    "pass_through": 0.6,
                    "price_stage": {"aggregate_relative": PRICE_RELATIVE},
                    "channels": {
                        "final_demand": {"long": long_block, "short": long_block},
                        "intermediate_input": {"long": long_block, "short": long_block},
                        "combined": {"long": long_block, "short": long_block,
                                     "groups": {"energy": 3}},
                    }}},
            }},
        })


class BridgeTestCase(unittest.TestCase):
    """Patches the module constants onto a fixture world context."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root)
        self.patch(self.fixture)

    def patch(self, fixture: Fixture) -> None:
        self._saved = (fw.DATASETS, fw.QUANTITY_REPORT)
        fw.DATASETS = {"figaro": {"label": "fixture", "index": fixture.index,
                                  "reports": fixture.reports, "quantity_key": "figaro"}}
        fw.QUANTITY_REPORT = fixture.root / "quantity_report.json"

    def tearDown(self) -> None:
        fw.DATASETS, fw.QUANTITY_REPORT = self._saved

    def run_bridge(self, feeds=None, **kwargs) -> dict:
        feeds = feeds if feeds is not None else list(self.fixture.feeds.values())
        options = {"dataset": "figaro", "pass_through": 0.6, "channel": "combined",
                   "horizon": "long", "price_template": "import_plus20",
                   "growth_template": "energy_minus5", "top_n": 5,
                   "out_dir": self.root / "out", "generated_at": GENERATED_AT}
        options.update(kwargs)
        return fw.build_report(feeds=[Path(item) for item in feeds], **options)

    def scenario(self, report: dict, scenario_id: str) -> dict:
        return next(item for item in report["scenarios"] if item["id"] == scenario_id)


class FeedValidationTest(BridgeTestCase):
    def test_quantile_feed_is_normalised(self):
        feeds = fw.load_feeds(list(self.fixture.feeds.values()))
        t1 = feeds["T1_CPI_YOY"]
        self.assertEqual((t1["point"], t1["low"], t1["high"]),
                         (T1_POINT, T1_LOW, T1_HIGH))
        self.assertEqual(t1["median"], quantiles(T1_LOW, T1_HIGH)[9])

    def test_feed_without_interval_is_refused(self):
        document = feed_document("T1_CPI_YOY", T1_POINT, T1_LOW, T1_HIGH)
        del document["quantiles_5_to_95_step_5"]
        path = write_json(self.root / "feeds" / "no_interval.json", document)
        with self.assertRaises(fw.ForecastWorldError):
            fw.parse_feed(document, path)

    def test_inverted_interval_is_refused(self):
        document = feed_document("T2_GDP_GROWTH", 2.0, T2_LOW, T2_HIGH)
        document["point"] = 2.0
        document["quantiles_5_to_95_step_5"] = quantiles(T2_LOW, T2_HIGH)[::-1]
        path = write_json(self.root / "feeds" / "inverted.json", document)
        with self.assertRaises(fw.ForecastWorldError):
            fw.parse_feed(document, path)

    def test_point_outside_interval_is_refused(self):
        document = feed_document("T2_GDP_GROWTH", 5.0, T2_LOW, T2_HIGH)
        path = write_json(self.root / "feeds" / "outside.json", document)
        with self.assertRaises(fw.ForecastWorldError):
            fw.parse_feed(document, path)

    def test_broken_json_is_refused(self):
        path = self.root / "feeds" / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(fw.ForecastWorldError):
            fw.load_feeds([path])

    def test_duplicate_target_is_refused(self):
        with self.assertRaises(fw.ForecastWorldError):
            fw.load_feeds([self.fixture.feeds["T1"], self.fixture.feeds["T1"]])

    def test_unknown_target_is_refused(self):
        document = feed_document("T9_UNKNOWN", 1.0, 0.5, 1.5)
        path = write_json(self.root / "feeds" / "unknown.json", document)
        with self.assertRaises(fw.ForecastWorldError):
            fw.parse_feed(document, path)


class FailClosedTest(BridgeTestCase):
    def test_strict_mode_blocks_without_t2_and_t3(self):
        with self.assertRaises(fw.MissingInput) as context:
            self.run_bridge(feeds=[self.fixture.feeds["T1"]])
        message = str(context.exception)
        self.assertIn("T2_GDP_GROWTH", message)
        self.assertIn("T3_RECESSION", message)
        self.assertFalse((self.root / "out" / "forecast_world.json").exists(),
                         "fail-closed run must not write a report")

    def test_strict_mode_blocks_a_missing_feed_file(self):
        with self.assertRaises(fw.MissingInput):
            self.run_bridge(feeds=[self.fixture.feeds["T1"], self.fixture.feeds["T2"],
                                   self.root / "feeds" / "absent.json"])

    def test_partial_mode_blocks_only_the_growth_scenario(self):
        outcome = self.run_bridge(feeds=[self.fixture.feeds["T1"]], partial=True)
        report = outcome["report"]
        self.assertEqual(report["status"], "PARTIAL_INPUTS")
        self.assertEqual(self.scenario(report, "inflation_plus_1pp")["status"], "OK")
        growth = self.scenario(report, "growth_minus_1pp")
        self.assertEqual(growth["status"], "BLOCKED_INPUT")
        self.assertIsNone(growth["combined"]["delta_output_usd_mn"])
        self.assertEqual(growth["countries"], [])
        self.assertEqual(report["checks"]["scenarios_blocked"], ["growth_minus_1pp"])
        self.assertFalse(report["checks"]["fail_closed"])
        self.assertTrue((self.root / "out" / "forecast_world.json").exists())

    def test_inconsistent_index_headline_is_refused(self):
        document = json.loads(self.fixture.index.read_text(encoding="utf-8"))
        document["scenarios"][0]["headline"]["total_delta_output"] = 42.0
        write_json(self.fixture.index, document)
        with self.assertRaises(fw.ForecastWorldError):
            self.run_bridge()

    def test_dataset_sha_mismatch_between_artifacts_is_refused(self):
        quantity = json.loads((self.root / "quantity_report.json").read_text(encoding="utf-8"))
        quantity["datasets"]["figaro"]["dataset"]["sha256"] = "another-hash"
        write_json(self.root / "quantity_report.json", quantity)
        with self.assertRaises(fw.ForecastWorldError):
            self.run_bridge()

    def test_missing_quantity_dataset_is_refused(self):
        quantity = json.loads((self.root / "quantity_report.json").read_text(encoding="utf-8"))
        quantity["datasets"]["figaro"]["status"] = "BLOCKED_DATA"
        write_json(self.root / "quantity_report.json", quantity)
        with self.assertRaises(fw.MissingInput):
            self.run_bridge()


class ScenarioTest(BridgeTestCase):
    def test_exactly_three_declared_scenarios(self):
        report = self.run_bridge()["report"]
        self.assertEqual([item["id"] for item in report["scenarios"]],
                         ["base", "inflation_plus_1pp", "growth_minus_1pp"])
        shifts = [(item["shifts"]["cpi_pp"], item["shifts"]["growth_pp"])
                  for item in report["scenarios"]]
        self.assertEqual(shifts, [(0.0, 0.0), (1.0, 0.0), (0.0, -1.0)])
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertTrue(report["checks"]["fail_closed"])
        self.assertEqual(report["checks"]["scenarios_ok"],
                         ["base", "inflation_plus_1pp", "growth_minus_1pp"])

    def test_base_scenario_carries_the_forecast_and_zero_effect(self):
        report = self.run_bridge()["report"]
        base = self.scenario(report, "base")
        self.assertEqual(base["combined"]["delta_output_usd_mn"], 0.0)
        self.assertEqual(base["countries"], [])
        self.assertEqual(report["forecast"]["T1"]["point"], T1_POINT)
        self.assertEqual(report["forecast"]["T2"]["interval"], [T2_LOW, T2_HIGH])
        self.assertEqual(report["forecast"]["T3"]["point"], T3_POINT)

    def test_price_channel_matches_the_quantity_calibration(self):
        report = self.run_bridge()["report"]
        inflation = self.scenario(report, "inflation_plus_1pp")
        expected_sensitivity = COMBINED_LONG / PRICE_RELATIVE
        self.assertAlmostEqual(inflation["price_channel"]["sensitivity"],
                               expected_sensitivity, places=12)
        self.assertAlmostEqual(inflation["combined"]["relative_output"],
                               expected_sensitivity * 0.01, places=12)
        self.assertAlmostEqual(inflation["combined"]["delta_output_usd_mn"],
                               expected_sensitivity * 0.01 * BASE_OUTPUT, places=6)
        self.assertEqual(inflation["growth_channel"]["relative_output"], 0.0)

    def test_growth_channel_matches_the_demand_multiplier(self):
        report = self.run_bridge()["report"]
        growth = self.scenario(report, "growth_minus_1pp")
        self.assertAlmostEqual(growth["growth_channel"]["multiplier"],
                               BASE_OUTPUT / BASE_FINAL_DEMAND, places=12)
        self.assertAlmostEqual(growth["combined"]["relative_output"], -0.02, places=12)
        self.assertAlmostEqual(growth["combined"]["delta_output_usd_mn"],
                               -0.02 * BASE_OUTPUT, places=6)
        self.assertEqual(growth["price_channel"]["relative_output"], 0.0)

    def test_interval_is_ordered_and_contains_the_point(self):
        report = self.run_bridge()["report"]
        for scenario_id in ("inflation_plus_1pp", "growth_minus_1pp"):
            combined = self.scenario(report, scenario_id)["combined"]
            interval = combined["interval"]
            self.assertLessEqual(interval["delta_low"], combined["delta_output_usd_mn"])
            self.assertGreaterEqual(interval["delta_high"], combined["delta_output_usd_mn"])
            self.assertLess(interval["delta_low"], interval["delta_high"])

    def test_channel_and_horizon_are_configurable(self):
        report = self.run_bridge(channel="final_demand", horizon="short")["report"]
        self.assertEqual(report["model"]["channel_used"], "final_demand")
        self.assertEqual(report["model"]["horizon"], "short")
        self.assertEqual(report["model"]["price_channel"]["channel"], "final_demand")

    def test_risk_overlay_does_not_touch_the_numbers(self):
        report = self.run_bridge()["report"]
        self.assertEqual(report["risk_overlay"]["probability"], T3_POINT)
        self.assertEqual(report["risk_overlay"]["flag"], "NOT_ARMED")
        strict = self.run_bridge(partial=False)["report"]
        self.assertEqual(strict["scenarios"][1]["combined"],
                         report["scenarios"][1]["combined"])


class AllocationTest(BridgeTestCase):
    def test_full_cross_conserves_the_total_and_both_margins(self):
        outcome = self.run_bridge(full_cross=True, top_n=3)
        report = outcome["report"]
        for scenario_id in ("inflation_plus_1pp", "growth_minus_1pp"):
            block = self.scenario(report, scenario_id)
            matrix = block["country_industry_matrix"]
            values = [value for row in matrix["values"] for value in row]
            total = block["combined"]["delta_output_usd_mn"]
            self.assertEqual(len(values), len(matrix["countries"]) * len(matrix["industries"]))
            self.assertAlmostEqual(sum(values), total, places=3)
            country_sums = [sum(row) for row in matrix["values"]]
            by_name = {item["name"]: item["delta_output"] for item in block["countries"]}
            for name, value in zip(matrix["countries"], country_sums):
                self.assertAlmostEqual(value, by_name[name], places=3)
            self.assertTrue(block["checks"]["conservation"])
            self.assertTrue(block["checks"]["country_margins"])
            self.assertTrue(block["checks"]["industry_margins"])
            self.assertTrue(block["checks"]["relative_matches_channel"])
            self.assertAlmostEqual(block["checks"]["pair_sum"], total, places=3)

    def test_industry_margin_matches_the_allocated_cells(self):
        report = self.run_bridge()["report"]
        block = self.scenario(report, "inflation_plus_1pp")
        industry_sum = sum(item["delta_output"] for item in block["industries"])
        self.assertAlmostEqual(industry_sum, block["combined"]["delta_output_usd_mn"], places=2)

    def test_top_cells_are_deterministic_and_ranked_by_abs(self):
        first = self.run_bridge()["report"]["scenarios"][1]["top_country_industry"]
        second = self.run_bridge()["report"]["scenarios"][1]["top_country_industry"]
        self.assertEqual(first, second)
        magnitudes = [abs(item["delta_output"]) for item in first]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))

    def test_template_scale_is_reported(self):
        report = self.run_bridge()["report"]
        block = self.scenario(report, "growth_minus_1pp")
        self.assertEqual(block["allocation"]["template"], "energy_minus5")
        self.assertEqual(block["checks"]["template"], "energy_minus5")
        expected = block["combined"]["delta_output_usd_mn"] / self.fixture.dataset_total
        self.assertAlmostEqual(block["allocation"]["scale_vs_template"], expected, places=9)


class PayloadTest(BridgeTestCase):
    def test_payload_has_no_raw_data_markers(self):
        outcome = self.run_bridge(full_cross=True)
        payload = outcome["report_path"]
        text = Path(payload).read_text(encoding="utf-8")
        self.assertEqual(fw.scan_for_raw(json.loads(text)), [])
        for marker in ("data/mrio", "dataset.json", "Z_sparse"):
            self.assertNotIn(marker, text)
        self.assertTrue(json.loads(text)["checks"]["no_raw_data"])

    def test_report_is_research_only(self):
        report = self.run_bridge()["report"]
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["admission"])
        self.assertTrue(report["checks"]["research_only"])
        self.assertTrue(report["checks"]["no_point_forecast"])
        self.assertFalse(report["checks"]["admission"])
        self.assertIn("НЕ прогноз", report["statement"])

    def test_determinism_byte_identical(self):
        first = self.run_bridge(out_dir=self.root / "out_a")
        second = self.run_bridge(out_dir=self.root / "out_b")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["report_path"]).read_bytes(),
                         Path(second["report_path"]).read_bytes())
        # the timestamp is the only wall-clock field: with --generated-at it is fixed
        self.assertEqual(json.loads(Path(first["report_path"]).read_text(encoding="utf-8"))
                         ["generated_at"], GENERATED_AT)

    def test_markdown_lists_scenarios_and_limits(self):
        outcome = self.run_bridge()
        text = Path(outcome["summary_path"]).read_text(encoding="utf-8")
        for marker in ("Инфляция +1 пп", "Рост −1 пп", "Базовый", "Ограничения", "SFC: PASS"):
            self.assertIn(marker, text)

    def test_checks_reuse_existing_world_checks(self):
        report = self.run_bridge()["report"]
        self.assertTrue(report["checks"]["sfc_pass"])
        self.assertTrue(report["checks"]["world_checks"]["all_feasible"])
        self.assertTrue(report["checks"]["dataset_sha_matches_quantity_report"])
        self.assertTrue(report["checks"]["template_totals_match_index"])
        self.assertEqual(report["model"]["templates"]["price"]["scenario_id"], "import_plus20")


class CliTest(BridgeTestCase):
    def test_strict_cli_returns_2_and_writes_nothing(self):
        code = fw.main(["--feed", str(self.fixture.feeds["T1"]), "--dataset", "figaro",
                        "--out", str(self.root / "out_cli")])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "out_cli" / "forecast_world.json").exists())

    def test_partial_cli_returns_3_and_writes_the_report(self):
        code = fw.main(["--feed", str(self.fixture.feeds["T1"]), "--feed",
                        str(self.fixture.feeds["T2"]), "--feed", str(self.fixture.feeds["T3"]),
                        "--dataset", "figaro", "--out", str(self.root / "out_cli"),
                        "--generated-at", GENERATED_AT])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "out_cli" / "forecast_world.json").exists())
        # T1 + T2 but no T3: every scenario runs, the envelope is still incomplete -> 3
        code = fw.main(["--feed", str(self.fixture.feeds["T1"]), "--feed",
                        str(self.fixture.feeds["T2"]), "--dataset", "figaro",
                        "--partial", "--out", str(self.root / "out_cli2"),
                        "--generated-at", GENERATED_AT])
        self.assertEqual(code, 3)
        self.assertTrue((self.root / "out_cli2" / "forecast_world.json").exists())
        # T1 only in partial mode: growth stays blocked, exit 3
        code = fw.main(["--feed", str(self.fixture.feeds["T1"]), "--dataset", "figaro",
                        "--partial", "--out", str(self.root / "out_cli3"),
                        "--generated-at", GENERATED_AT])
        self.assertEqual(code, 3)
        report = json.loads((self.root / "out_cli3" / "forecast_world.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["checks"]["scenarios_blocked"], ["growth_minus_1pp"])


REAL_ARTEFACTS = (
    fw.QUANTITY_REPORT.is_file()
    and (ROOT / "build" / "world" / "reports" / "import_plus20.json").is_file()
    and (ROOT / "build" / "world" / "reports" / "energy_minus5.json").is_file()
)


class RealArtefactsTest(unittest.TestCase):
    """Integration check against the generated world/quantity artefacts (skipped when absent)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @unittest.skipUnless(REAL_ARTEFACTS, "generated build/ artefacts are not present")
    def test_bridge_is_consistent_with_the_real_reports(self):
        feeds = []
        for name, target, point, low, high in (
                ("t1", "T1_CPI_YOY", 3.6257264569782937, 2.45788896937228, 4.797070272153592),
                ("t2", "T2_GDP_GROWTH", 2.0, 1.0, 3.0),
                ("t3", "T3_RECESSION", 0.2, 0.1, 0.4)):
            feeds.append(write_json(self.root / f"{name}.json",
                                    feed_document(target, point, low, high)))
        outcome = fw.build_report(feeds=feeds, dataset="figaro", pass_through=0.6,
                                  channel="combined", horizon="long",
                                  price_template="import_plus20",
                                  growth_template="energy_minus5", top_n=5,
                                  out_dir=self.root / "out", generated_at=GENERATED_AT)
        report = outcome["report"]
        price_template = report["model"]["templates"]["price"]
        if price_template["scenario_id"] == "import_plus20":
            source = json.loads((ROOT / "build" / "world" / "reports" / "import_plus20.json")
                                .read_text(encoding="utf-8"))
            self.assertAlmostEqual(price_template["total_delta_output"],
                                   source["result"]["total_delta_output"], places=6)
        growth = next(item for item in report["scenarios"]
                      if item["id"] == "growth_minus_1pp")
        self.assertTrue(growth["checks"]["conservation"])
        self.assertTrue(report["checks"]["sfc_pass"])
        self.assertTrue(report["checks"]["no_raw_data"])


if __name__ == "__main__":
    unittest.main()
