"""Tests for the FLAKS procurement MVP (tools/flaks_procurement.py, docs/184).

The tests do not trust the report: they re-derive the composite risk, the TCO components, the
quality blend and the weighted score from the published components, force cap/floor clamping,
check that the base-scenario budget equals the active contracts, verify determinism byte by byte,
and inspect every fail-closed branch (bad dataset, real-data marker, unknown scenario, bad weights)
including the guarantee that nothing is written when the run is refused.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import flaks_procurement as fp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "flaks_procurement_demo.json"
GENERATED_AT = "2026-11-18T00:00:00Z"


def _demo() -> dict:
    return json.loads(json.dumps(fp.demo_dataset(), ensure_ascii=False))


def _supplier(document: dict, supplier_id: str) -> dict:
    for entry in document["suppliers"]:
        if entry["id"] == supplier_id:
            return entry
    raise KeyError(supplier_id)


def _write(document: dict, directory: str) -> Path:
    path = Path(directory) / "suppliers.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _report(document: dict | None = None, **kwargs) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        source = None if document is None else _write(document, tmp)
        outcome = fp.build_report(suppliers=source, generated_at=GENERATED_AT, **kwargs)
        return outcome["report"]


def _run_cli(argv) -> tuple:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = fp.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _category(report: dict, category_id: str) -> dict:
    for entry in report["scoring"]["categories"]:
        if entry["id"] == category_id:
            return entry
    raise KeyError(category_id)


class DemoDatasetTest(unittest.TestCase):
    def test_builtin_demo_is_synthetic_and_complete(self):
        report = _report()
        self.assertEqual(report["dataset"]["source_class"], "synthetic_demo")
        self.assertFalse(report["dataset"]["real_data_allowed"])
        self.assertEqual(report["dataset"]["counts"]["suppliers"], 12)
        self.assertEqual(report["dataset"]["counts"]["categories"], 4)
        self.assertEqual(report["dataset"]["counts"]["active"], 8)
        self.assertEqual(report["dataset"]["counts"]["candidates"], 4)
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["admission"])
        for category in report["scoring"]["categories"]:
            self.assertGreaterEqual(len(category["suppliers"]), 2)
            self.assertGreaterEqual(
                sum(1 for item in category["suppliers"]
                    if item["contract"]["status"] == "active"), 1)

    def test_fixture_matches_builtin_demo_and_builds_identically(self):
        self.assertTrue(FIXTURE.exists(), "fixture не сгенерирован: --dump-suppliers")
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture, fp.demo_dataset())
        from_builtin = _report()
        from_fixture = _report(fixture)
        self.assertEqual(from_builtin["scoring"], from_fixture["scoring"])
        self.assertEqual(from_builtin["budget"], from_fixture["budget"])
        self.assertEqual(from_fixture["dataset"]["source_class"], "synthetic_demo")

    def test_dump_suppliers_cli_is_valid_dataset(self):
        code, stdout, _ = _run_cli(["--dump-suppliers"])
        self.assertEqual(code, 0)
        document = json.loads(stdout)
        normalized = fp.normalize_dataset(document, origin="dump")
        self.assertEqual(normalized["counts"]["suppliers"], 12)


class WeightsTest(unittest.TestCase):
    def test_default_weights_are_declared_and_sum_to_one(self):
        report = _report()
        weights = report["weights"]["values"]
        self.assertEqual(tuple(weights), fp.WEIGHT_KEYS)
        self.assertAlmostEqual(sum(weights.values()), 1.0, delta=1e-12)
        self.assertEqual(weights["tco"], 0.30)
        self.assertTrue(report["checks"]["weights_sum_to_one"])

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(fp.WeightsError):
            fp.build_report(suppliers="demo", weights={"price": 0.5, "tco": 0.5},
                            generated_at=GENERATED_AT)
        bad = dict(fp.DEFAULT_WEIGHTS)
        bad["price"] = 0.9
        with self.assertRaises(fp.WeightsError):
            fp.build_report(suppliers="demo", weights=bad, generated_at=GENERATED_AT)

    def test_weights_reject_unknown_missing_and_nonpositive(self):
        with self.assertRaises(fp.WeightsError):
            fp.build_report(suppliers="demo", weights={**fp.DEFAULT_WEIGHTS, "brand": 0.1},
                            generated_at=GENERATED_AT)
        zero = dict(fp.DEFAULT_WEIGHTS)
        zero["delivery"] = 0.10
        zero["price"] = 0.0
        with self.assertRaises(fp.WeightsError):
            fp.build_report(suppliers="demo", weights=zero, generated_at=GENERATED_AT)
        with self.assertRaises(fp.WeightsError):
            fp.parse_weights("price=abc")

    def test_price_heavy_weights_change_the_recommendation(self):
        base = _category(_report(), "metal_rolled")
        self.assertEqual(base["recommended"]["supplier_id"], "sup-de-stahl")
        price_heavy = {"price": 0.94, "tco": 0.02, "risk": 0.02, "quality": 0.01,
                       "delivery": 0.01}
        report = _report(weights=price_heavy)
        self.assertEqual(_category(report, "metal_rolled")["recommended"]["supplier_id"],
                         "sup-cn-metal")

    def test_score_equals_weighted_components(self):
        report = _report()
        weights = report["weights"]["values"]
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                expected = sum(weights[key] * entry["components"][key] for key in fp.WEIGHT_KEYS)
                self.assertAlmostEqual(entry["score"], expected, delta=1e-9)
                self.assertTrue(report["checks"]["score_equals_contributions"])


class ScoringTest(unittest.TestCase):
    def test_contributions_sum_to_score(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                self.assertAlmostEqual(sum(entry["contributions"].values()), entry["score"],
                                       delta=1e-9)
                for key in fp.WEIGHT_KEYS:
                    self.assertAlmostEqual(entry["contributions"][key],
                                           report["weights"]["values"][key]
                                           * entry["components"][key], delta=1e-12)

    def test_components_are_in_unit_interval_and_best_is_one(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                for key in fp.WEIGHT_KEYS:
                    self.assertGreater(entry["components"][key], 0.0)
                    self.assertLessEqual(entry["components"][key], 1.0 + 1e-12)
            self.assertAlmostEqual(max(entry["components"]["price"] for entry in category["suppliers"]),
                                   1.0, delta=1e-12)
            self.assertAlmostEqual(max(entry["components"]["tco"] for entry in category["suppliers"]),
                                   1.0, delta=1e-12)

    def test_ranking_sorted_by_score_then_id(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            entries = category["suppliers"]
            self.assertEqual([entry["rank"] for entry in entries],
                             list(range(1, len(entries) + 1)))
            keys = [(-entry["score"], entry["id"]) for entry in entries]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(entries[0]["delta_to_best"], 0.0)
            self.assertTrue(all(entry["delta_to_best"] <= 0
                                for entry in entries[1:]))
        ranking = report["scoring"]["ranking"]
        self.assertEqual(len(ranking), 12)
        scores = [item["score"] for item in ranking]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_bands_follow_declared_thresholds(self):
        report = _report()
        thresholds = {item["letter"]: item["threshold"] for item in report["scoring"]["bands"]}
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                letter = entry["band"]["letter"]
                self.assertGreaterEqual(entry["score"], thresholds[letter])
                if letter != "A":
                    self.assertLess(entry["score"], thresholds["A"])

    def test_composite_risk_matches_declared_weights(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                parts = entry["risk_parts"]
                expected = (fp.RISK_WEIGHTS["country"] * parts["country"]
                            + fp.RISK_WEIGHTS["financial"] * parts["financial"]
                            + fp.RISK_WEIGHTS["supply"] * parts["supply"]
                            + fp.RISK_WEIGHTS["compliance"] * parts["compliance"])
                self.assertAlmostEqual(entry["risk_composite"], expected, delta=1e-12)

    def test_quality_blend_is_recomputed(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            entries = category["suppliers"]
            min_defect = min(entry["defect_ppm"] for entry in entries)
            max_otif = max(entry["otif"] for entry in entries)
            max_audit = max(entry["audit_score"] for entry in entries)
            for entry in entries:
                expected = (fp.QUALITY_WEIGHTS["defect"] * min_defect / entry["defect_ppm"]
                            + fp.QUALITY_WEIGHTS["otif"] * entry["otif"] / max_otif
                            + fp.QUALITY_WEIGHTS["audit"] * entry["audit_score"] / max_audit)
                self.assertAlmostEqual(entry["components"]["quality"], expected, delta=1e-9)


class TcoTest(unittest.TestCase):
    def test_tco_components_sum_and_are_consistent_with_formulas(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                tco = entry["tco"]
                total = sum(tco[key] for key in fp.TCO_KEYS)
                self.assertAlmostEqual(tco["total"], total, delta=1e-9)
                self.assertAlmostEqual(tco["duty"], entry["unit_price"] * entry["duty_rate"],
                                       delta=1e-9)
                self.assertAlmostEqual(
                    tco["risk"],
                    entry["unit_price"] * entry["risk_composite"] * fp.RISK_COST_RATE,
                    delta=1e-9)
                self.assertAlmostEqual(
                    tco["quality"],
                    entry["unit_price"] * entry["defect_ppm"] / 1e6 * fp.REWORK_MULTIPLIER,
                    delta=1e-9)
                self.assertAlmostEqual(
                    tco["carrying"],
                    entry["unit_price"] * entry["lead_time_days"] / 365.0
                    * fp.CARRYING_RATE, delta=1e-9)
                self.assertAlmostEqual(
                    tco["late"],
                    entry["unit_price"] * (1.0 - entry["otif"]) * fp.LATE_PENALTY_RATE,
                    delta=1e-9)
                self.assertGreater(tco["total"], entry["unit_price"])
                self.assertTrue(report["checks"]["tco_components_sum"])

    def test_duty_is_positive_only_for_non_eu_countries(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                expected = entry["unit_price"] * fp.COUNTRIES[entry["country"]]["duty_rate"]
                self.assertAlmostEqual(entry["tco"]["duty"], expected, delta=1e-9)
                if entry["country"] in ("PL", "DE", "CZ", "IT", "RO"):
                    self.assertEqual(entry["tco"]["duty"], 0.0)
                else:
                    self.assertGreater(entry["tco"]["duty"], 0.0)

    def test_cheapest_price_is_not_cheapest_tco(self):
        report = _report()
        film = _category(report, "packaging_film")
        self.assertEqual(film["best_price_supplier"], "sup-vn-film")
        self.assertEqual(film["best_tco_supplier"], "sup-cz-film")
        polymer = _category(report, "polymer_granulate")
        self.assertNotEqual(polymer["best_price_supplier"], polymer["best_tco_supplier"])
        for category in report["scoring"]["categories"]:
            tco_rank = sorted(category["suppliers"], key=lambda item: item["tco"]["total"])
            self.assertEqual(tco_rank[0]["id"], category["best_tco_supplier"])

    def test_savings_are_nonnegative_and_consistent(self):
        report = _report()
        for category in report["scoring"]["categories"]:
            savings = category["savings"]
            self.assertGreaterEqual(savings["saving_eur"], 0.0)
            self.assertAlmostEqual(savings["saving_eur"],
                                   savings["current_tco_eur"] - savings["best_single_source_tco_eur"],
                                   delta=1e-5)
            expected_pct = savings["saving_eur"] / savings["current_tco_eur"] * 100.0
            self.assertAlmostEqual(savings["saving_pct"], expected_pct, delta=1e-4)


class ScenarioTest(unittest.TestCase):
    def test_scenario_set_is_declared_and_focus_is_exactly_one(self):
        report = _report()
        scenarios = report["budget"]["scenarios"]
        self.assertEqual([item["id"] for item in scenarios], list(fp.SCENARIO_ORDER))
        self.assertEqual(sum(1 for item in scenarios if item["focus"]), 1)
        self.assertEqual(report["budget"]["focus_scenario"], "base")
        for item in scenarios:
            self.assertEqual(item["base_total_eur"], report["budget"]["base_total_eur"])

    def test_unknown_scenario_fails_closed(self):
        with self.assertRaises(fp.ScenarioError):
            fp.build_report(suppliers="demo", scenario_id="hyperinflation",
                            generated_at=GENERATED_AT)
        with self.assertRaises(fp.ScenarioError):
            fp.scenario_drivers("nope")

    def test_base_scenario_budget_equals_active_contracts(self):
        report = _report(scenario_id="base")
        expected = 0.0
        for category in report["scoring"]["categories"]:
            for entry in category["suppliers"]:
                if entry["contract"]["status"] == "active":
                    expected += entry["annual_volume"] * entry["unit_price"]
        self.assertAlmostEqual(report["budget"]["focus_total_eur"], expected, delta=1e-6)
        self.assertAlmostEqual(report["budget"]["focus_delta_eur"], 0.0, delta=1e-6)
        self.assertTrue(report["checks"]["budget_base_equals_contract_sum"])

    def test_cpi_scenario_price_change_equals_cpi_incidence(self):
        report = _report(scenario_id="inflation_plus_1pp")
        for item in report["scenario"]["category_price_changes"]:
            self.assertAlmostEqual(item["price_change_pct"], item["incidences"]["cpi"],
                                   delta=1e-9)
            self.assertEqual(item["components_pct"]["oil"], 0.0)
            self.assertEqual(item["components_pct"]["freight"], 0.0)

    def test_oil_scenario_hits_commodity_categories_more_than_metal(self):
        report = _report(scenario_id="oil_plus_20")
        changes = {item["id"]: item["price_change_pct"]
                   for item in report["scenario"]["category_price_changes"]}
        self.assertGreater(changes["polymer_granulate"], changes["packaging_film"])
        self.assertGreater(changes["packaging_film"], changes["metal_rolled"])
        self.assertGreater(changes["contract_logistics"], changes["polymer_granulate"])
        polymer = next(item for item in report["scenario"]["category_price_changes"]
                       if item["id"] == "polymer_granulate")
        # oil driver = 20 % * 2.364 % / 100 = 0.4728 % at aggregate incidence
        self.assertAlmostEqual(polymer["components_pct"]["oil"],
                               fp.CATEGORY_BY_ID["polymer_granulate"]["oil_incidence"]
                               * 20.0 * fp.OIL_PRICE_ELASTICITY_PCT_PER_100 / 100.0, delta=1e-9)

    def test_hormuz_scenario_uses_bridge_market_shock(self):
        drivers = fp.scenario_drivers("hormuz_6m")
        self.assertAlmostEqual(drivers["oil_delta_pct"], 125.14112, delta=1e-12)
        self.assertAlmostEqual(drivers["oil_price_pct"], 2.95838, delta=5e-5)
        self.assertAlmostEqual(drivers["cpi_pp"], 1.5 + 125.14112 * fp.OIL_TO_CPI_PP_PER_PCT,
                               delta=1e-9)
        report = _report(scenario_id="hormuz_6m")
        log_contract = next(item for item in report["budget"]["contracts"]
                            if item["contract_id"] == "CT-LOG-01")
        self.assertEqual(log_contract["flags"], ["CAP_BINDING"])
        self.assertAlmostEqual(log_contract["applied_pct"], 5.0, delta=1e-12)
        self.assertGreater(log_contract["raw_pct"], 5.0)

    def test_interval_is_ordered_and_floor_zero_binds_on_the_low_side(self):
        report = _report(scenario_id="base")
        interval = report["budget"]["interval_eur"]
        self.assertLessEqual(interval["low_eur"], interval["point_eur"])
        self.assertLessEqual(interval["point_eur"], interval["high_eur"])
        self.assertTrue(report["checks"]["interval_ordered"])
        self.assertEqual(interval["floor_bound_contracts"], 8)
        self.assertEqual(interval["low_eur"], interval["point_eur"])

    def test_volume_effect_is_separate_from_price_effect(self):
        report = _report(scenario_id="hormuz_6m")
        budget = report["budget"]
        self.assertGreater(budget["focus_total_eur"], budget["base_total_eur"])
        self.assertLess(budget["focus_total_with_volume_eur"], budget["focus_total_eur"])
        self.assertAlmostEqual(budget["focus_volume_effect_eur"],
                               budget["focus_total_with_volume_eur"] - budget["focus_total_eur"],
                               delta=1e-6)

    def test_floor_zero_blocks_negative_indexation(self):
        dataset = fp.load_dataset(None)["dataset"]
        record = next(entry for entry in dataset["suppliers"] if entry["id"] == "sup-pl-stal")
        drivers = fp.scenario_drivers("base", cpi_override=-2.0)
        item = fp.contract_indexation(record, drivers)
        self.assertLess(item["raw_pct"], 0.0)
        self.assertEqual(item["applied_pct"], 0.0)
        self.assertEqual(item["flags"], ["FLOOR_BINDING"])
        self.assertEqual(item["indexation_eur"], 0.0)

    def test_every_contract_indexation_matches_its_shares(self):
        report = _report(scenario_id="stress_combo")
        for item in report["budget"]["contracts"]:
            shares = item["shares"]
            self.assertAlmostEqual(shares["cpi"] + shares["ppi"] + shares["fx"]
                                   + shares["fixed"], 1.0, delta=1e-9)
            expected_raw = sum(item["driver_parts_pct"].values())
            self.assertAlmostEqual(item["raw_pct"], expected_raw, delta=1e-8)
            self.assertLessEqual(item["applied_pct"], item["cap_pct"] + 1e-12)
            if item["floor_zero"]:
                self.assertGreaterEqual(item["applied_pct"], 0.0)
            self.assertAlmostEqual(item["forecast_eur"],
                                   item["base_eur"] + item["indexation_eur"], delta=1e-6)


class FailClosedTest(unittest.TestCase):
    def _assert_dataset_rejected(self, mutate):
        document = _demo()
        mutate(document)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(document, tmp)
            with self.assertRaises(fp.DatasetError):
                fp.build_report(suppliers=path, generated_at=GENERATED_AT)

    def test_real_data_class_is_refused(self):
        self._assert_dataset_rejected(
            lambda document: document.update({"source_class": "real_customer_data"}))
        self._assert_dataset_rejected(lambda document: document.update({"schema": "other"}))

    def test_broken_supplier_fields_are_refused(self):
        cases = [
            lambda document: _supplier(document, "sup-pl-stal").update({"unit_price": 0}),
            lambda document: _supplier(document, "sup-pl-stal").update({"unit_price": -1}),
            lambda document: _supplier(document, "sup-pl-stal").update({"annual_volume": 0}),
            lambda document: _supplier(document, "sup-pl-stal").update({"lead_time_days": 0}),
            lambda document: _supplier(document, "sup-pl-stal").update({"otif": 1.4}),
            lambda document: _supplier(document, "sup-pl-stal").update({"defect_ppm": 0}),
            lambda document: _supplier(document, "sup-pl-stal").update({"country": "XX"}),
            lambda document: _supplier(document, "sup-pl-stal").update({"category": "toys"}),
            lambda document: _supplier(document, "sup-pl-stal").update({"id": "sup-de-stahl"}),
            lambda document: _supplier(document, "sup-pl-stal").update({"annual_volum": 10}),
            lambda document: _supplier(document, "sup-pl-stal")["contract"].update(
                {"cpi_share": 0.9, "ppi_share": 0.9}),
            lambda document: _supplier(document, "sup-pl-stal")["contract"].update(
                {"cap_pct": 40}),
            lambda document: _supplier(document, "sup-pl-stal")["contract"].update(
                {"floor_zero": "yes"}),
            lambda document: _supplier(document, "sup-cn-metal").update({"currency": "EUR"}),
            lambda document: document.update({"suppliers": []}),
            lambda document: document.update({"suppliers": "nope"}),
            lambda document: document.update({"currency": "USD"}),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                self._assert_dataset_rejected(mutate)

    def test_single_supplier_category_is_refused(self):
        def mutate(document):
            document["suppliers"] = [entry for entry in document["suppliers"]
                                     if entry["category"] != "packaging_film"
                                     or entry["id"] == "sup-cz-film"]
        self._assert_dataset_rejected(mutate)

    def test_category_without_active_contract_is_refused(self):
        def mutate(document):
            for entry in document["suppliers"]:
                if entry["category"] == "contract_logistics":
                    entry["contract"]["status"] = "candidate"
        self._assert_dataset_rejected(mutate)

    def test_missing_and_broken_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fp.ProcurementError):
                fp.build_report(suppliers=Path(tmp) / "absent.json", generated_at=GENERATED_AT)
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(fp.ProcurementError):
                fp.build_report(suppliers=broken, generated_at=GENERATED_AT)

    def test_cli_writes_nothing_when_refused(self):
        document = _demo()
        document["source_class"] = "real_customer_data"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(document, tmp)
            out_dir = Path(tmp) / "out"
            code, _, stderr = _run_cli(["--suppliers", str(path), "--report", str(out_dir),
                                        "--generated-at", GENERATED_AT])
            self.assertEqual(code, 2)
            self.assertIn("BLOCKED_INPUT", stderr)
            self.assertFalse((out_dir / "base" / "flaks_procurement.json").exists())
            self.assertFalse(out_dir.exists())

    def test_cli_refuses_bad_weights_and_scenario(self):
        code, _, stderr = _run_cli(["--weights", "price=0.5,tco=0.5", "--dry-run",
                                    "--generated-at", GENERATED_AT])
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED_INPUT", stderr)
        code, _, stderr = _run_cli(["--scenario", "hyperinflation", "--dry-run",
                                    "--generated-at", GENERATED_AT])
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED_INPUT", stderr)


class CliTest(unittest.TestCase):
    def test_cli_success_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_cli(["--scenario", "oil_plus_20", "--report", tmp,
                                        "--generated-at", GENERATED_AT])
            self.assertEqual(code, 0)
            summary = json.loads(stdout)
            self.assertEqual(summary["status"], "RESEARCH_ONLY")
            self.assertEqual(summary["scenario"], "oil_plus_20")
            json_path = Path(tmp) / "oil_plus_20" / "flaks_procurement.json"
            md_path = Path(tmp) / "oil_plus_20" / "flaks_procurement.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["schema"], fp.SCHEMA)
            self.assertEqual(report["generated_at"], GENERATED_AT)
            self.assertTrue(report["checks"]["no_raw_data"])
            self.assertEqual(summary["sha256"], fp.sha256_file(json_path))
            self.assertTrue(summary["checks"]["budget_base_equals_contract_sum"])

    def test_cli_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "reports"
            code, stdout, _ = _run_cli(["--dry-run", "--report", str(out_dir),
                                        "--generated-at", GENERATED_AT])
            self.assertEqual(code, 0)
            summary = json.loads(stdout)
            self.assertIsNone(summary["report"])
            self.assertFalse(out_dir.exists())

    def test_cli_json_flag_prints_full_report(self):
        code, stdout, _ = _run_cli(["--json", "--dry-run", "--generated-at", GENERATED_AT])
        self.assertEqual(code, 0)
        report = json.loads(stdout)
        self.assertEqual(report["schema"], fp.SCHEMA)
        self.assertEqual(len(report["scoring"]["categories"]), 4)


class RawAndDeterminismTest(unittest.TestCase):
    def test_report_has_no_raw_markers(self):
        report = _report()
        self.assertEqual(fp.scan_for_raw(report), [])
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        for marker in fp.FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(marker, payload)
        self.assertNotIn("Z_sparse", payload)

    def test_scan_for_raw_detects_planted_markers(self):
        self.assertTrue(fp.scan_for_raw({"a": [{"Z": 1}]}))
        self.assertTrue(fp.scan_for_raw({"path": "data/mrio/figaro/dataset.json"}))
        self.assertTrue(fp.scan_for_raw({"path": "F:/data/x"}))
        self.assertEqual(fp.scan_for_raw({"path": "build/product/forecast_latest.json"}), [])

    def test_determinism_byte_identical(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            fp.build_report(suppliers="demo", scenario_id="hormuz_6m", out_dir=first,
                            generated_at=GENERATED_AT)
            fp.build_report(suppliers="demo", scenario_id="hormuz_6m", out_dir=second,
                            generated_at=GENERATED_AT)
            payload_one = (Path(first) / "hormuz_6m" / "flaks_procurement.json").read_bytes()
            payload_two = (Path(second) / "hormuz_6m" / "flaks_procurement.json").read_bytes()
            self.assertEqual(payload_one, payload_two)
            markdown_one = (Path(first) / "hormuz_6m" / "flaks_procurement.md").read_bytes()
            markdown_two = (Path(second) / "hormuz_6m" / "flaks_procurement.md").read_bytes()
            self.assertEqual(markdown_one, markdown_two)

    def test_markdown_has_cfo_sections_and_no_failed_checks(self):
        report = _report(scenario_id="stress_combo")
        markdown = fp.render_markdown(report)
        for heading in ("Резюме для CFO", "Скоринг поставщиков", "TCO: из чего складывается",
                        "Бюджет закупок по сценариям", "Индексация контрактов", "Проверки",
                        "Ограничения", "Воспроизводимость"):
            self.assertIn(heading, markdown)
        self.assertNotIn("| FAIL |", markdown)
        self.assertIn("RESEARCH_ONLY", markdown)
        self.assertIn("synthetic_demo", markdown)


class AnchorTest(unittest.TestCase):
    @unittest.skipUnless(fp.DEFAULT_FEED.exists() and fp.DEFAULT_PRICE_BRIDGE.exists(),
                         "build-артефакты отсутствуют")
    def test_anchors_match_published_artefacts(self):
        anchors = fp.verify_anchors()
        self.assertTrue(anchors["verified"])
        self.assertAlmostEqual(anchors["feed"]["point"], 3.6257264569782937, delta=1e-9)
        self.assertAlmostEqual(anchors["price_bridge"]["oil_price_elasticity_pct_per_100"],
                               2.364, delta=1e-3)
        report = _report(anchors=anchors)
        self.assertTrue(report["macro"]["anchors_verified"])
        self.assertEqual(report["macro"]["verification"]["verified"], True)

    @unittest.skipUnless(fp.DEFAULT_FEED.exists(), "продуктовый фид отсутствует")
    def test_anchor_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = json.loads(fp.DEFAULT_FEED.read_text(encoding="utf-8"))
            feed["point"] = 99.0
            path = Path(tmp) / "feed.json"
            path.write_text(json.dumps(feed), encoding="utf-8")
            with self.assertRaises(fp.ProcurementError):
                fp.verify_anchors(path, fp.DEFAULT_PRICE_BRIDGE)


if __name__ == "__main__":
    unittest.main()
