"""Client Pack v2: structure, formats, artefact-only numbers, fail-closed, determinism, licence.

Most tests build a small synthetic artefact tree (the same schemas as the real build/ artefacts with
known numbers), so the aggregation logic is tested without depending on the generated build/ folder.
The optional forecast→world bridge block is tested the same way: present/absent/corrupt/foreign,
its table in XLSX/JSON/DOCX/HTML, the SHA pinning and the raw-data refusal. One optional integration
test runs the pack on the real ``build/`` artefacts when they are present.
"""
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import client_pack_v2 as pack  # noqa: E402

FIXED = "2026-11-17T00:00:00Z"
QUANTILES = [round(1.0 + 0.05 * index, 4) for index in range(19)]
REAL_ARTIFACTS = ROOT / "build" / "product" / "forecast_latest.json"


# ----------------------------------------------------------------------------------------- fixture
def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def module_passport(module_id: str, version: str, kind: str, code: str) -> dict:
    return {
        "action": "REGISTER", "module_id": module_id,
        "display_name": f"fixture {module_id}", "version": version, "kind": kind,
        "status": "RESEARCH_ONLY", "owner": "AMFE", "created_at": "2026-11-16",
        "summary": f"fixture module {module_id}", "limitations": ["research prototype"],
        "code_sha256": code, "data_sha256": None, "data_status": "ASSUMPTION_BASED",
        "components": ["central"], "entrypoints": [f"amfe.fixture.{module_id}"],
        "docs": ["docs/171_Client_Pack_v2.md"], "tests": ["tests/test_client_pack_v2.py"],
        "event_id": f"fixture-{module_id}-{version}",
    }


def build_fixture(base: Path) -> Path:
    root = base / "repo"
    build = root / "build"
    write_json(build / "product" / "forecast_latest.json", {
        "schema_version": "1.0.0", "generated_at": "2026-11-16T00:00:00Z",
        "status": "CANDIDATE", "status_label": "CANDIDATE", "conditional_data_class": True,
        "data_class": "REAL_TIME_RECONSTRUCTED", "report_id": "AMFE-FIRST-REAL-EVALUATION",
        "protocol": {"file": "protocols/evaluation_protocol_v5.json", "version": "5.0.1",
                     "sha256": "a" * 64},
        "candidate": "ENSEMBLE_INVERSE_MSE", "target": "T1_CPI_YOY", "horizon": 1,
        "origin": "2026-11", "point": 3.5, "quantiles_5_to_95_step_5": QUANTILES,
        "controls": {"RW": {"point": 1.0}, "EQUAL_WEIGHT_POINT": {"point": 2.0},
                     "EQUAL_WEIGHT_DENSITY": {"point": 2.1}},
        "c4_status": {"pass": False, "rmse_pass": False, "recession_origins": 16,
                      "min_recession_origins": 12},
        "c6_status": {"pass": False, "status": "NO_RESOLVED_FORECASTS", "n_resolved": 0,
                      "min_forecasts": 12},
        "source_hashes": {"protocol": "a" * 64, "report": "b" * 64, "journal": "c" * 64},
        "limitations": ["Статус CANDIDATE: SHADOW_TEST, LIMITED_DEPLOYMENT и ACTIVE не достигнуты; "
                        "пакет не является допуском."],
    })
    registry = [module_passport("oil.brent", "0.1.0", "market", "d" * 64),
                module_passport("oil.brent", "0.2.0", "market", "e" * 64),
                module_passport("gas.lng_eu", "0.1.0", "market", "9" * 64),
                module_passport("AGRI_SOY", "0.1.0", "agri", "f" * 64)]
    (build / "registry").mkdir(parents=True, exist_ok=True)
    (build / "registry" / "modules.jsonl").write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in registry) + "\n",
        encoding="utf-8")
    write_json(build / "api" / "index.json", {
        "schema": "amfe.forecast_api.v2", "api_version": "v2", "base_path": "/v1",
        "status": "RESEARCH_ONLY", "generated_at": "2026-11-16T00:00:00Z",
        "counts": {"modules": 3, "scenarios": 1, "forecasts": 2, "market_modules": 2},
        "endpoints": [
            {"method": "GET", "path": "/v1/modules", "description": "module passports"},
            {"method": "GET", "path": "/v1/scenarios/{scenario_id}", "description": "scenario"},
            {"method": "GET", "path": "/v1/forecast/{target}", "description": "forecast"},
        ],
        "modules": [
            {"module_id": "oil.brent", "version": "0.2.0", "status": "RESEARCH_ONLY",
             "kind": "market"},
            {"module_id": "gas.lng_eu", "version": "0.1.0", "status": "RESEARCH_ONLY",
             "kind": "market"},
            {"module_id": "AGRI_SOY", "version": "0.1.0", "status": "RESEARCH_ONLY",
             "kind": "agri"},
        ],
        "market_modules": [
            {"module_id": "oil.brent", "module_version": "0.2.0",
             "commodity": "Brent crude oil", "default_scenario": "hormuz_closed_6m",
             "forecast_target": "oil_brent", "status": "RESEARCH_ONLY", "units": "USD/bbl",
             "scenarios": [{"scenario_id": "hormuz_closed_6m", "horizon_months": 6}]},
            {"module_id": "gas.lng_eu", "module_version": "0.1.0",
             "commodity": "Natural gas / LNG", "default_scenario": "eu_pipeline_shock_6m",
             "forecast_target": "gas_ttf", "status": "RESEARCH_ONLY", "units": "USD/MMBtu",
             "scenarios": [{"scenario_id": "eu_pipeline_shock_6m", "horizon_months": 6}]},
        ],
        "scenarios": [{"id": "oil.brent/hormuz_closed_6m", "module": "oil.brent",
                       "horizon_months": 6,
                       "file": "scenarios/oil.brent__hormuz_closed_6m.json"}],
        "forecasts": [
            {"target": "oil_brent", "module_id": "oil.brent",
             "module_version": "0.2.0", "file": "forecasts/oil_brent.json",
             "units": "USD/bbl"},
            {"target": "gas_ttf", "module_id": "gas.lng_eu",
             "module_version": "0.1.0", "file": "forecasts/gas_ttf.json",
             "units": "USD/MMBtu"},
        ],
    })
    write_json(build / "api" / "scenarios" / "oil.brent__hormuz_closed_6m.json", {
        "schema": "amfe.forecast_api.v2", "api_version": "v2", "status": "RESEARCH_ONLY",
        "generated_at": "2026-11-16T00:00:00Z",
        "scenario": {"scenario_id": "hormuz_closed_6m", "description": "fixture scenario",
                     "horizon_months": 6, "parameters": {"closure_months": 6}},
        "module_ref": {"module_id": "oil.brent", "version": "0.2.0",
                       "status": "RESEARCH_ONLY", "kind": "market", "code_sha256": "e" * 64},
        "result": {
            "schema": "amfe.markets.result.v1", "module_id": "oil.brent",
            "scenario_id": "hormuz_closed_6m", "commodity": "Brent crude oil",
            "units": "USD/bbl", "horizon_months": 6,
            "summary": {"price_peak": 170.0, "price_mean": 130.0, "price_trough": 110.0,
                        "brent_range_usd_per_bbl": {"low": 150.0, "central": 170.0,
                                                    "high": 190.0},
                        "brent_range_mean_usd_per_bbl": {"low": 120.0, "central": 130.0,
                                                         "high": 140.0}},
            "price_path": [170.0, 150.0, 140.0, 130.0, 120.0, 110.0],
            "sensitivity": [{"variant": "central", "price_peak": 170.0},
                            {"variant": "high", "price_peak": 190.0}],
            "assumptions": [{"name": "closure", "source": "declared"} for _ in range(3)],
            "checks": {"price_positive": True, "range_ordered": True},
            "digest": "1" * 64, "meta": {"engine": "fixture"},
        },
    })
    write_json(build / "api" / "forecasts" / "oil_brent.json", {
        "schema": "amfe.forecast_api.v2", "api_version": "v2", "status": "RESEARCH_ONLY",
        "generated_at": "2026-11-16T00:00:00Z", "kind": "forecast", "target": "oil_brent",
        "module_id": "oil.brent", "no_point_forecast": True, "research_only": True,
        "result_digest": "1" * 64, "assumptions_count": 3,
        "envelope": {"price_peak": 170.0, "price_mean": 130.0, "price_trough": 110.0,
                     "price_peak_month": 0, "checks": {"price_positive": True}},
        "module_ref": {"module_id": "oil.brent", "version": "0.2.0",
                       "status": "RESEARCH_ONLY"},
    })
    write_json(build / "api" / "forecasts" / "gas_ttf.json", {
        "schema": "amfe.forecast_api.v2", "api_version": "v2", "status": "RESEARCH_ONLY",
        "generated_at": "2026-11-16T00:00:00Z", "kind": "forecast", "target": "gas_ttf",
        "module_id": "gas.lng_eu", "no_point_forecast": False, "research_only": True,
        "result_digest": "2" * 64, "assumptions_count": 5,
        "envelope": {"price_peak": 20.0, "price_mean": 16.0, "price_trough": 15.0,
                     "price_peak_month": 0, "checks": {"ttf_price_positive": True},
                     "interval": {"central": 20.0, "low": 15.0, "high": 28.0,
                                  "units": "USD/MMBtu",
                                  "note": "sensitivity variants, not a confidence interval"}},
        "module_ref": {"module_id": "gas.lng_eu", "version": "0.1.0",
                       "status": "RESEARCH_ONLY"},
    })
    write_blocks(build)
    return root


def write_blocks(build: Path) -> dict:
    """Fixture artefacts of blocks 15/17 (same schemas as tools/institutions_* and technology_*)."""
    institutions = {
        "schema": "amfe.world.institutions.report.v1",
        "status": "RESEARCH_ONLY",
        "status_note": "fixture institutions: research only",
        "admission": False, "gating": False, "causal_claim": False,
        "data_status": "ASSUMPTION_BASED", "not_calibrated": True,
        "index": {
            "schema": "amfe.world.institutions.index.v1",
            "status": "RESEARCH_ONLY", "admission": False, "gating": False,
            "causal_claim": False, "data_status": "ASSUMPTION_BASED",
            "not_calibrated": True, "tool_version": "1.0.0",
            "profile": {"name": "EXAMPLE-MID", "source": "ILLUSTRATIVE",
                        "score": 51.5, "dispersion": 2.4324,
                        "note": "geometric example, not a country"},
            "parameters": {"horizon_years": 5, "beta_bps_per_point": 4.0,
                           "gamma_bps_per_point_dispersion": 2.0},
            "scenarios": [
                {"id": "STATUS-QUO", "title_ru": "Статус-кво", "kind": "status_quo",
                 "magnitude": 1.0, "ramp_years": 1, "digest": "1" * 64,
                 "headline": {"delta_score_points": 0.0, "delta_risk_premium_bps": 0.0,
                              "delta_investment_pp": 0.0, "delta_growth_pp": 0.0}},
                {"id": "REFORM", "title_ru": "Реформа", "kind": "reform",
                 "magnitude": 1.0, "ramp_years": 4, "digest": "2" * 64,
                 "headline": {"delta_score_points": 6.0, "delta_risk_premium_bps": -23.48,
                              "delta_investment_pp": 0.24, "delta_growth_pp": 0.12}},
                {"id": "POLARIZATION", "title_ru": "Поляризация", "kind": "polarization",
                 "magnitude": 1.0, "ramp_years": 2, "digest": "3" * 64,
                 "headline": {"delta_score_points": -4.17, "delta_risk_premium_bps": 25.6,
                              "delta_investment_pp": -0.167, "delta_growth_pp": -0.083}},
            ],
            "checks": {"scenario_count": 3, "all_score_bounds": True,
                       "all_no_raw_data": True},
            "assumptions": [
                {"id": "A1", "kind": "ASSUMPTION_BASED", "statement": "six 0-100 indicators"},
                {"id": "A4", "kind": "ASSUMPTION_BASED", "statement": "beta and gamma"},
            ],
            "limitations": ["no data, no calibration", "no country table"],
        },
        "scenarios": [],
    }
    technology = {
        "schema": "amfe.world.technology_report.v1",
        "status": "RESEARCH_ONLY", "admission": False, "research_only": True,
        "data_class": "ASSUMPTION_BASED", "calibration": "NOT_CALIBRATED", "block": 17,
        "module": {"module_id": "world.technology_diffusion", "version": "0.1.0",
                   "status": "RESEARCH_ONLY"},
        "horizon_years": 15,
        "scenario_ids": ["baseline", "ai_acceleration"],
        "sectors": ["manufacturing", "services", "agriculture", "energy", "construction"],
        "growth": {"capital_growth": 0.02, "labour_growth": 0.004},
        "scenarios": [
            {"scenario_id": "baseline", "title_ru": "Базовая диффузия",
             "aggregate": {"horizon": {
                 "years": 15, "adoption": 0.613, "tfp_gain_pct": 15.32,
                 "output_gain_pct": 32.2, "energy_change_pct": 9.83,
                 "energy_intensity_change_pct": -16.92,
                 "contributions_log": {"tfp": 0.1425, "capital": 0.0961,
                                       "labour": 0.0405, "total": 0.2791}}}},
            {"scenario_id": "ai_acceleration", "title_ru": "Ускорение ИИ-внедрения",
             "aggregate": {"horizon": {
                 "years": 15, "adoption": 0.683, "tfp_gain_pct": 23.0,
                 "output_gain_pct": 41.0, "energy_change_pct": 10.87,
                 "energy_intensity_change_pct": -21.37,
                 "contributions_log": {"tfp": 0.207, "capital": 0.0961,
                                       "labour": 0.0405, "total": 0.3436}}}},
        ],
        "assumptions": [
            {"name": "ceiling.services", "value": 0.8, "unit": "доля",
             "source_class": "assumption"},
            {"name": "tfp_elasticity.services", "value": 0.3, "unit": "ln TFP",
             "source_class": "assumption"},
        ],
        "checks": {"no_raw_series": True, "all_scenario_checks_pass": True},
        "limitations": ["declared assumptions only", "no MRIO bridge"],
    }
    write_json(Path(build) / "institutions" / "report.json", institutions)
    write_json(Path(build) / "technology_scenarios" / "report.json", technology)
    return {"institutions": institutions, "technology": technology}


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def bridge_report(dataset: str = "oecd_2025", status: str = "PARTIAL_INPUTS", **overrides) -> dict:
    """A minimal but schema-faithful ``amfe.forecast_world_bridge.v1`` payload."""
    report = {
        "schema": pack.BRIDGE_SCHEMA,
        "status": status,
        "admission": False,
        "generated_at": "2026-11-18T00:00:00Z",
        "statement": "fixture bridge: reduced-form, research only",
        "forecast": {"T1": {"target": "T1_CPI_YOY", "point": 3.5, "interval": [2.4, 4.8]},
                     "T2": {"target": "T2_GDP_GROWTH", "status": "NOT_AVAILABLE"}},
        "model": {
            "dataset": dataset,
            "dataset_label": "OECD ICIO 2025, 2019 (81 country x 50 industries)",
            "dataset_info": {"countries": 81, "industries": 50, "sectors": 4050,
                             "source": "OECD Inter-Country Input-Output tables (ICIO)",
                             "units": "USD_mn"},
            "price_channel": {"sensitivity": -0.392772, "relative_output": -0.025111,
                              "pass_through": 0.6, "channel": "combined", "horizon": "long"},
            "growth_channel": {"value": 2.031873, "base_output": 168480917.49},
            "channel_used": "combined", "horizon": "long", "pass_through": 0.6,
            "templates": {},
        },
        "inputs": {"feeds": [],
                   "world_index": {"path": "build/world_oecd_2025/report_index.json"},
                   "quantity_report": {"path": "build/quantity_bridge/quantity_report.json"},
                   "missing_targets": ["T2_GDP_GROWTH", "T3_RECESSION"]},
        "scenarios": [
            {"id": "base", "label": "база", "status": "OK",
             "shifts": {"cpi_pp": 0.0, "growth_pp": 0.0},
             "price_channel": {"relative_price_shock": 0.0},
             "combined": {"delta_output_usd_mn": 0.0, "relative_output": 0.0,
                          "base_output_usd_mn": 168480917.49,
                          "interval": {"delta_low": 0.0, "delta_high": 0.0}},
             "countries": [], "industries": [], "top_country_industry": [],
             "checks": {"computed": True, "fail_closed": False}},
            {"id": "inflation_plus_1pp", "label": "инфляция +1 пп", "status": "OK",
             "shifts": {"cpi_pp": 1.0, "growth_pp": 0.0},
             "price_channel": {"relative_price_shock": 0.01},
             "combined": {"delta_output_usd_mn": -661745.206052682,
                          "relative_output": -0.003927716063701749,
                          "base_output_usd_mn": 168480917.49,
                          "interval": {"delta_low": -775131.1543317123,
                                       "delta_high": 772810.858871888}},
             "countries": [], "industries": [],
             "top_country_industry": [
                 {"country": "CHN", "industry": "G", "delta_output": -9073.292972,
                  "share_of_abs": 0.013711158},
                 {"country": "USA", "industry": "G", "delta_output": -6750.7,
                  "share_of_abs": 0.0102},
             ],
             "checks": {"computed": True, "fail_closed": False, "conservation": True,
                        "country_margins": True, "industry_margins": True,
                        "relative_matches_channel": True, "cells": 4050}},
            {"id": "growth_minus_1pp", "label": "рост −1 пп", "status": "BLOCKED_INPUT",
             "reason": "нет прогноза для ['T2_GDP_GROWTH']",
             "shifts": {"cpi_pp": 0.0, "growth_pp": -1.0},
             "price_channel": {"status": "BLOCKED_INPUT"},
             "combined": {"status": "BLOCKED_INPUT", "delta_output_usd_mn": None,
                          "relative_output": None},
             "countries": [], "industries": [], "top_country_industry": [],
             "checks": {"computed": False, "fail_closed": True}},
        ],
        "risk_overlay": {"target": "T3_RECESSION", "status": "NOT_AVAILABLE",
                         "probability": None, "interval": None, "threshold": 0.5,
                         "flag": "NOT_AVAILABLE", "effect_on_numbers": "нет"},
        "checks": {"mode": "partial", "scenarios_ok": ["base", "inflation_plus_1pp"],
                   "scenarios_blocked": ["growth_minus_1pp"], "no_raw_data": True,
                   "admission": False, "fail_closed": False},
        "limitations": ["сравнительная статика", "не допуск"],
    }
    report.update(overrides)
    return report


def write_bridge(build: Path, **overrides) -> Path:
    path = Path(build) / "forecast_world_bridge" / "report.json"
    write_json(path, bridge_report(**overrides))
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def docx_text(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    chunks = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            chunks.extend(cell.text for cell in row.cells)
    return "\n".join(chunks)


def numeric_lists(value, found=None, threshold=19) -> list:
    found = [] if found is None else found
    if isinstance(value, dict):
        for child in value.values():
            numeric_lists(child, found, threshold)
    elif isinstance(value, list):
        numbers = [item for item in value if isinstance(item, (int, float))
                   and not isinstance(item, bool)]
        if len(numbers) > threshold:
            found.append(len(numbers))
        for child in value:
            numeric_lists(child, found, threshold)
    return found


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = build_fixture(self.base)
        self.build = self.root / "build"
        self.out = self.base / "pack"

    def tearDown(self):
        self.tmp.cleanup()

    def make_pack(self, out: Path | None = None, **overrides) -> dict:
        values = {"inputs_root": self.build, "generated_at": FIXED, "root": self.root,
                  "make_pdf": False}
        values.update(overrides)
        return pack.build_pack(out or self.out, **values)

    def machine(self) -> dict:
        return read_json(self.out / "04_Machine_Readable.json")


class PackFormats(FixtureCase):
    def test_all_formats_and_readme(self):
        summary = self.make_pack()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["status"], "RESEARCH_ONLY")
        names = sorted(path.name for path in self.out.iterdir())
        for required in ("01_Executive_Summary.docx", "02_Numbers.xlsx",
                         "03_Dashboard.html", "04_Machine_Readable.json",
                         "05_Methodology_and_Sources.docx", "06_Licence_and_Disclaimer.docx",
                         "07_Reproducibility.json", "README.md"):
            self.assertIn(required, names, required)
        for name in names:
            self.assertGreater((self.out / name).stat().st_size, 100, name)

    def test_machine_json_identity_and_status(self):
        self.make_pack()
        document = self.machine()
        self.assertEqual(document["schema"], pack.SCHEMA)
        self.assertEqual(document["kind"], "client_pack")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["generated_at"], FIXED)
        self.assertEqual(document["client"], pack.DEFAULT_CLIENT)
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["candidate_status"], "CANDIDATE")
        self.assertRegex(document["document_digest"], r"^[0-9a-f]{64}$")
        for key in ("product_feed", "candidates", "modules", "market_scenario",
                    "forecast_api", "licence", "sources", "summary"):
            self.assertIn(key, document, key)

    def test_no_admission_is_claimed(self):
        self.make_pack()
        text = json.dumps(self.machine(), ensure_ascii=False)
        self.assertNotIn('"admitted": true', text)
        self.assertNotIn("ADMITTED", text)
        document = self.machine()
        self.assertEqual(document["candidates"]["no_admission"], True)
        self.assertEqual(document["product_feed"]["no_admission"], True)
        self.assertEqual(document["candidates"]["items"][0]["status"], "CANDIDATE")
        self.assertTrue(any("не является допуском" in item
                            for item in document["product_feed"]["limitations"]))


class NumbersFromArtefacts(FixtureCase):
    def test_candidate_numbers_come_from_the_feed(self):
        self.make_pack()
        feed = read_json(self.build / "product" / "forecast_latest.json")
        document = self.machine()
        section = document["product_feed"]
        self.assertEqual(section["candidate"], feed["candidate"])
        self.assertEqual(section["status"], feed["status"])
        self.assertEqual(section["point"], feed["point"])
        self.assertEqual(section["target"], feed["target"])
        self.assertEqual(section["horizon"], feed["horizon"])
        self.assertEqual(section["origin"], feed["origin"])
        self.assertEqual(section["quantiles_5_to_95_step_5"], QUANTILES)
        self.assertEqual(document["summary"]["point"], feed["point"])
        self.assertEqual(document["summary"]["interval_5_95"], [QUANTILES[0], QUANTILES[-1]])

    def test_module_passports_come_from_the_registry(self):
        self.make_pack()
        registry = [json.loads(line) for line in
                    (self.build / "registry" / "modules.jsonl").read_text(encoding="utf-8")
                    .splitlines() if line.strip()]
        document = self.machine()
        module = document["modules"]
        self.assertEqual(module["count"], 3)  # latest records, history collapsed
        self.assertEqual(module["registry_sha256"], sha(self.build / "registry" / "modules.jsonl"))
        latest = {entry["module_id"]: entry for entry in registry}
        self.assertEqual(latest["oil.brent"]["version"], "0.2.0")
        for item in module["items"]:
            self.assertEqual(item["status"], "RESEARCH_ONLY", item)
            self.assertRegex(item["code_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(item["version"], latest[item["module_id"]]["version"])
        self.assertEqual([item["module_id"] for item in module["items"]],
                         ["AGRI_SOY", "gas.lng_eu", "oil.brent"])

    def test_scenario_numbers_come_from_the_api_artefact(self):
        self.make_pack()
        scenario = read_json(self.build / "api" / "scenarios"
                             / "oil.brent__hormuz_closed_6m.json")
        result = scenario["result"]
        document = self.machine()
        section = document["market_scenario"]
        self.assertEqual(section["scenario_id"], result["scenario_id"])
        self.assertEqual(section["module_id"], result["module_id"])
        self.assertEqual(section["summary"]["price_peak"], result["summary"]["price_peak"])
        self.assertEqual(section["summary"]["price_mean"], result["summary"]["price_mean"])
        self.assertEqual(section["price_path"], result["price_path"])
        self.assertEqual(len(section["sensitivity"]), len(result["sensitivity"]))
        self.assertEqual(section["assumptions_count"], len(result["assumptions"]))
        self.assertEqual(section["result_digest"], result["digest"])

    def test_forecast_api_slice_comes_from_the_api_artefact(self):
        self.make_pack()
        index = read_json(self.build / "api" / "index.json")
        forecast = read_json(self.build / "api" / "forecasts" / "oil_brent.json")
        document = self.machine()
        section = document["forecast_api"]
        self.assertEqual(section["schema"], index["schema"])
        self.assertEqual(section["api_version"], index["api_version"])
        self.assertEqual(len(section["endpoints"]), len(index["endpoints"]))
        self.assertEqual(len(section["modules"]), len(index["modules"]))
        self.assertEqual(len(section["market_modules"]), len(index["market_modules"]))
        self.assertEqual(len(section["forecasts"]), len(index["forecasts"]))
        self.assertEqual(section["forecast"]["target"], forecast["target"])
        self.assertEqual(section["forecast"]["price_peak"],
                         forecast["envelope"]["price_peak"])
        self.assertEqual(section["forecast"]["result_digest"], forecast["result_digest"])

    def test_sources_carry_real_digests(self):
        self.make_pack()
        document = self.machine()
        self.assertEqual(len(document["sources"]), 6)  # 4 fixed inputs + 2 forecast contracts
        for item in document["sources"]:
            path = self.root / item["path"]
            self.assertTrue(path.is_file(), item["path"])
            self.assertEqual(item["sha256"], sha(path), item["path"])
            self.assertEqual(item["bytes"], path.stat().st_size)

    def test_market_modules_and_gas_contract_are_in_the_pack(self):
        self.make_pack()
        document = self.machine()
        summary = document["summary"]
        self.assertEqual(summary["modules_total"], 3)
        self.assertEqual(summary["market_modules"], 2)
        self.assertEqual(summary["market_module_ids"], ["oil.brent", "gas.lng_eu"])
        self.assertEqual(summary["forecasts"], 2)
        self.assertEqual(summary["forecast_targets"], ["gas_ttf", "oil_brent"])
        gas = next(item for item in document["forecasts"]["items"]
                   if item["target"] == "gas_ttf")
        self.assertEqual(gas["module_id"], "gas.lng_eu")
        self.assertEqual(gas["units"], "USD/MMBtu")
        self.assertEqual(gas["price_peak"], 20.0)
        self.assertEqual(gas["status"], "RESEARCH_ONLY")
        self.assertIn("build/api/forecasts/gas_ttf.json",
                      [item["path"] for item in document["sources"]])

    def test_legacy_one_forecast_index_still_builds(self):
        index_path = self.build / "api" / "index.json"
        index = read_json(index_path)
        index["forecasts"] = [item for item in index["forecasts"]
                              if item["target"] == "oil_brent"]
        index["counts"]["forecasts"] = 1
        index.pop("market_modules", None)
        index["counts"].pop("market_modules", None)
        write_json(index_path, index)
        self.make_pack()
        document = self.machine()
        self.assertEqual(document["summary"]["forecasts"], 1)
        self.assertEqual(document["summary"]["forecast_targets"], ["oil_brent"])
        self.assertEqual(document["summary"]["market_modules"], 0)
        self.assertEqual(document["forecast_api"]["forecast"]["target"], "oil_brent")


class OfficeDocuments(FixtureCase):
    def test_xlsx_sheets_and_numbers_match_the_json(self):
        from openpyxl import load_workbook

        self.make_pack()
        document = self.machine()
        workbook = load_workbook(self.out / "02_Numbers.xlsx", data_only=True)
        for sheet in ("Summary", "Candidate", "Modules", "Market_Modules", "Forecasts",
                      "Scenario_Summary", "Scenario_Path", "Scenario_Sensitivity", "API",
                      "Sources"):
            self.assertIn(sheet, workbook.sheetnames, sheet)
        summary_values = {row[0]: row[1] for row in
                          workbook["Summary"].iter_rows(values_only=True) if row[0]}
        self.assertEqual(float(summary_values["Точечный прогноз"]), document["summary"]["point"])
        self.assertEqual(float(summary_values["Пик сценария"]),
                         document["summary"]["scenario_peak"])
        self.assertEqual(int(summary_values["Модулей"]), document["summary"]["modules_total"])
        module_rows = list(workbook["Modules"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(module_rows), len(document["modules"]["items"]))
        market_rows = list(workbook["Market_Modules"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(market_rows), document["summary"]["market_modules"])
        forecast_rows = list(workbook["Forecasts"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([row[0] for row in forecast_rows],
                         document["summary"]["forecast_targets"])
        self.assertEqual(int(summary_values["Рыночных модулей"]),
                         document["summary"]["market_modules"])
        path_rows = list(workbook["Scenario_Path"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([float(row[1]) for row in path_rows],
                         [float(value) for value in document["market_scenario"]["price_path"]])
        api_values = {row[0]: row[1] for row in
                      workbook["API"].iter_rows(values_only=True) if row[0]}
        self.assertEqual(api_values["api_version"], document["forecast_api"]["api_version"])
        source_rows = list(workbook["Sources"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(source_rows), len(document["sources"]))

    def test_executive_summary_uses_artefact_numbers(self):
        self.make_pack()
        text = docx_text(self.out / "01_Executive_Summary.docx")
        document = self.machine()
        self.assertIn("ENSEMBLE_INVERSE_MSE", text)
        self.assertIn("CANDIDATE", text)
        self.assertIn("3.5", text.replace(",", "."))
        self.assertIn("NO_RESOLVED_FORECASTS", text)
        self.assertIn("hormuz_closed_6m", text)
        self.assertIn("170", text)
        self.assertIn("gas_ttf", text)
        self.assertIn("не прогноз", text.lower())
        self.assertIn(str(document["summary"]["modules_total"]), text)
        self.assertIn(str(document["summary"]["market_modules"]), text)

    def test_methodology_lists_sources_and_determinism(self):
        self.make_pack()
        text = docx_text(self.out / "05_Methodology_and_Sources.docx")
        document = self.machine()
        for item in document["sources"]:
            self.assertIn(item["path"], text)
        self.assertIn("детерминизм", text.lower())
        self.assertIn("fail-closed", text.lower())
        self.assertIn("docProps/core.xml", text)

    def test_licence_document_has_disclaimers(self):
        self.make_pack()
        text = docx_text(self.out / "06_Licence_and_Disclaimer.docx")
        for token in ("RESEARCH_ONLY", "CANDIDATE", "C4", "C6", "не инвестиционная"):
            self.assertIn(token, text, token)
        self.assertIn("protocols/licence_register.json", text)
        self.assertIn("docs/84", text)


class DashboardAndReadme(FixtureCase):
    def test_dashboard_is_static_and_uses_artefact_numbers(self):
        self.make_pack()
        html = (self.out / "03_Dashboard.html").read_text(encoding="utf-8")
        document = self.machine()
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn("RESEARCH_ONLY", html)
        self.assertIn(document["market_scenario"]["scenario_id"], html)
        self.assertIn("ENSEMBLE_INVERSE_MSE", html)
        self.assertIn("gas_ttf", html)
        self.assertIn("oil.brent", html)
        self.assertIn(str(document["summary"]["modules_total"]), html)
        self.assertIn(str(document["summary"]["market_modules"]), html)

    def test_readme_lists_files_status_and_limitations(self):
        self.make_pack()
        text = (self.out / "README.md").read_text(encoding="utf-8")
        for token in ("Client Pack v2", "RESEARCH_ONLY", "CANDIDATE", "C4", "C6",
                      "01_Executive_Summary.docx", "07_Reproducibility.json",
                      "client_pack_v2.py", "gas_ttf", "Forecast-контракты"):
            self.assertIn(token, text, token)


class Reproducibility(FixtureCase):
    def test_manifest_digests_match_the_files(self):
        self.make_pack()
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertEqual(manifest["schema"], pack.REPRO_SCHEMA)
        self.assertEqual(manifest["status"], "RESEARCH_ONLY")
        self.assertEqual(manifest["generated_at"], FIXED)
        self.assertIn("client_pack_v2.py", manifest["command"])
        self.assertEqual(len(manifest["inputs"]), 6)
        for item in manifest["inputs"]:
            self.assertEqual(item["sha256"], sha(self.root / item["path"]), item["path"])
        for name, digest in manifest["deterministic_artifacts"].items():
            self.assertEqual(sha(self.out / name), digest, name)
        self.assertEqual(manifest["file_count"], len(manifest["deterministic_artifacts"]))
        self.assertEqual(manifest["file_count"], 7)
        self.assertIn("zip entry timestamps", manifest["normalization"]["docx"])

    def test_two_builds_are_byte_identical(self):
        first = self.base / "first"
        second = self.base / "second"
        self.make_pack(out=first)
        self.make_pack(out=second)
        names = sorted(path.name for path in first.iterdir())
        self.assertEqual(names, sorted(path.name for path in second.iterdir()))
        for name in names:
            self.assertEqual(sha(first / name), sha(second / name), name)

    def test_changed_input_changes_the_pack(self):
        self.make_pack()
        before = self.machine()
        feed_path = self.build / "product" / "forecast_latest.json"
        feed = read_json(feed_path)
        feed["point"] = 9.75
        write_json(feed_path, feed)
        self.make_pack(out=self.base / "pack2")
        after = read_json(self.base / "pack2" / "04_Machine_Readable.json")
        self.assertNotEqual(before["document_digest"], after["document_digest"])
        self.assertEqual(after["summary"]["point"], 9.75)


class FailClosed(FixtureCase):
    def test_missing_feed_fails_without_output(self):
        (self.build / "product" / "forecast_latest.json").unlink()
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_corrupt_json_fails_without_output(self):
        (self.build / "api" / "index.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_admitted_candidate_status_fails(self):
        feed_path = self.build / "product" / "forecast_latest.json"
        feed = read_json(feed_path)
        feed["status"] = "ACTIVE"
        feed["status_label"] = "ACTIVE"
        write_json(feed_path, feed)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_registry_admitted_module_fails(self):
        registry_path = self.build / "registry" / "modules.jsonl"
        lines = registry_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["status"] = "ADMITTED"
        lines[0] = json.dumps(entry, ensure_ascii=False)
        registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()

    def test_unknown_scenario_fails(self):
        with self.assertRaises(pack.ClientPackError):
            self.make_pack(scenario_id="nope")

    def test_missing_forecast_contract_fails_closed(self):
        (self.build / "api" / "forecasts" / "gas_ttf.json").unlink()
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_inconsistent_api_counts_fail_closed(self):
        index_path = self.build / "api" / "index.json"
        index = read_json(index_path)
        index["counts"]["forecasts"] = 9
        write_json(index_path, index)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_unsafe_forecast_path_fails_closed(self):
        index_path = self.build / "api" / "index.json"
        index = read_json(index_path)
        index["forecasts"][0]["file"] = "../index.json"
        write_json(index_path, index)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()

    def test_bad_quantiles_fail(self):
        feed_path = self.build / "product" / "forecast_latest.json"
        feed = read_json(feed_path)
        feed["quantiles_5_to_95_step_5"] = list(reversed(QUANTILES))
        write_json(feed_path, feed)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()

    def test_cli_invalid_generated_at_returns_two(self):
        code = pack.main(["--root", str(self.root), "--inputs-root", str(self.build),
                          "--out", str(self.out), "--generated-at", "not-a-date"])
        self.assertEqual(code, 2)
        self.assertFalse(self.out.exists())


class NoRawData(FixtureCase):
    def test_no_third_party_observations(self):
        self.make_pack()
        document = self.machine()
        text = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("data/raw", text)
        self.assertNotIn("external/", text)
        self.assertNotIn('"observations": [', text)
        self.assertIsNone(re.search(r'https?://', text))
        self.assertEqual(numeric_lists(document), [])
        html = (self.out / "03_Dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("data/raw", html)


class BridgeSection(FixtureCase):
    """Optional forecast→world bridge: present/absent/corrupt, tables in all formats, SHA, no raw."""

    def setUp(self):
        super().setUp()
        self.bridge = write_bridge(self.build)

    def test_bridge_available_with_sha_dataset_and_explicit_path(self):
        self.make_pack()
        document = self.machine()
        section = document["bridge"]
        self.assertTrue(section["available"])
        self.assertTrue(section["optional"])
        self.assertEqual(section["sha256"], sha(self.bridge))
        self.assertEqual(section["sha256_verified"], "file")
        self.assertEqual(section["source"], "build/forecast_world_bridge/report.json")
        self.assertEqual(section["schema"], pack.BRIDGE_SCHEMA)
        self.assertEqual(section["artefact_status"], "PARTIAL_INPUTS")
        self.assertFalse(section["admission"])
        self.assertEqual(section["dataset"]["id"], "oecd_2025")
        self.assertAlmostEqual(section["calibration"]["price_sensitivity"], -0.392772)
        self.assertAlmostEqual(section["calibration"]["growth_multiplier"], 2.031873)
        self.assertEqual([item["id"] for item in section["scenarios"]],
                         ["base", "inflation_plus_1pp", "growth_minus_1pp"])
        self.assertEqual(section["checks"]["scenarios_blocked"], ["growth_minus_1pp"])
        self.assertTrue(section["checks"]["no_raw_data"])
        self.assertEqual(len(section["effects"]), 2)
        self.assertEqual(document["summary"]["bridge_available"], True)
        self.assertEqual(document["summary"]["bridge_effects"], 2)
        self.assertEqual(document["summary"]["sources"], 7)
        self.assertIn("build/forecast_world_bridge/report.json",
                      [item["path"] for item in document["sources"]])
        # An explicit --bridge may point at the bridge tool's output directory.
        explicit_out = self.base / "explicit"
        self.make_pack(out=explicit_out, bridge_path=str(self.build / "forecast_world_bridge"))
        explicit = read_json(explicit_out / "04_Machine_Readable.json")
        self.assertEqual(explicit["bridge"]["sha256"], section["sha256"])
        # the same explicit path through the CLI: --bridge <repo-relative file>
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pack.main(["--root", str(self.root), "--inputs-root", str(self.build),
                              "--out", str(self.base / "cli"), "--generated-at", FIXED,
                              "--bridge", "build/forecast_world_bridge/report.json", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["numbers"]["bridge"]["available"])

    def test_bridge_table_in_xlsx_json_docx_html(self):
        from openpyxl import load_workbook

        self.make_pack()
        document = self.machine()
        effects = document["bridge"]["effects"]
        self.assertEqual([(item["scenario"], item["country"], item["industry"])
                          for item in effects],
                         [("inflation_plus_1pp", "CHN", "G"),
                          ("inflation_plus_1pp", "USA", "G")])
        workbook = load_workbook(self.out / "02_Numbers.xlsx", data_only=True)
        for sheet in ("Bridge", "Bridge_Scenarios", "Bridge_Effects"):
            self.assertIn(sheet, workbook.sheetnames, sheet)
        rows = list(workbook["Bridge_Effects"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([(row[0], row[1], row[2]) for row in rows],
                         [(item["scenario"], item["country"], item["industry"])
                          for item in effects])
        for row, item in zip(rows, effects):
            self.assertAlmostEqual(float(row[3]), item["delta_output_usd_mn"], places=6)
            self.assertAlmostEqual(float(row[4]), item["share_of_abs"], places=9)
        scenario_rows = list(workbook["Bridge_Scenarios"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([row[0] for row in scenario_rows],
                         [item["id"] for item in document["bridge"]["scenarios"]])
        bridge_values = {row[0]: row[1] for row in
                         workbook["Bridge"].iter_rows(values_only=True) if row[0]}
        self.assertEqual(bridge_values["available"], True)
        self.assertEqual(bridge_values["sha256"], document["bridge"]["sha256"])
        self.assertEqual(int(bridge_values["effects"]), len(effects))
        text = docx_text(self.out / "01_Executive_Summary.docx")
        for token in ("Мост «прогноз → мир»", "CHN", "inflation_plus_1pp", "reduced-form"):
            self.assertIn(token, text, token)
        html = (self.out / "03_Dashboard.html").read_text(encoding="utf-8")
        for token in ("Мост «прогноз → мир»", "CHN", "BLOCKED_INPUT", "S_price"):
            self.assertIn(token, html, token)
        self.assertIn(document["bridge"]["sha256"], html)

    def test_bridge_absent_is_explicit_and_pack_builds(self):
        self.bridge.unlink()
        summary = self.make_pack()
        document = self.machine()
        section = document["bridge"]
        self.assertFalse(section["available"])
        self.assertEqual(section["status"], "NOT_AVAILABLE")
        self.assertTrue(section["optional"])
        self.assertIn("не найден", section["reason"])
        self.assertIn("build/forecast_world_bridge/report.json", section["searched"])
        self.assertEqual(section["scenarios"], [])
        self.assertEqual(section["effects"], [])
        self.assertEqual(len(summary["files"]), 8)
        self.assertFalse(summary["numbers"]["bridge"]["available"])
        self.assertIsNone(document["summary"]["bridge_sha256"])
        self.assertNotIn("bridge", [item["kind"] for item in document["sources"]])
        self.assertIn("Раздел недоступен",
                      docx_text(self.out / "01_Executive_Summary.docx"))
        self.assertIn("Раздел недоступен",
                      (self.out / "03_Dashboard.html").read_text(encoding="utf-8"))

    def test_bridge_corrupt_or_foreign_artefact_fails_closed(self):
        self.bridge.write_text("{not json", encoding="utf-8")
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())
        # a foreign artefact at the bridge path is an error, not a silently skipped block
        write_json(self.bridge, {"schema": "amfe.something_else.v9", "status": "OK"})
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())
        write_json(self.bridge, bridge_report(admission=True))
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_bridge_unsafe_path_is_refused(self):
        for bad in ("../outside.json", str(self.base / "outside.json")):
            with self.assertRaises(pack.ClientPackError):
                self.make_pack(bridge_path=bad)
        self.assertFalse(self.out.exists())

    def test_bridge_raw_rows_are_refused(self):
        report = read_json(self.bridge)
        report["inputs"]["world_index"]["path"] = "data/mrio/Z.csv"
        write_json(self.bridge, report)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())
        # an artefact that stops declaring checks.no_raw_data=true is refused too
        report["inputs"]["world_index"]["path"] = "build/world/report_index.json"
        report["checks"]["no_raw_data"] = False
        write_json(self.bridge, report)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack()
        self.assertFalse(self.out.exists())

    def test_bridge_sidecar_sha_is_enforced(self):
        sidecar = Path(str(self.bridge) + ".sha256")
        digest = sha(self.bridge)
        sidecar.write_text(f"{digest}  report.json\n", encoding="utf-8")
        self.make_pack()
        section = self.machine()["bridge"]
        self.assertEqual(section["sha256"], digest)
        self.assertEqual(section["sha256_verified"], "sidecar")
        bad = self.base / "bad"
        sidecar.write_text("0" * 64 + "  report.json\n", encoding="utf-8")
        with self.assertRaises(pack.ClientPackError):
            self.make_pack(out=bad)
        self.assertFalse(bad.exists())

    def test_no_bridge_pack_is_deterministic_and_bridge_changes_the_digest(self):
        self.bridge.unlink()
        first, second = self.base / "legacy1", self.base / "legacy2"
        self.make_pack(out=first)
        self.make_pack(out=second)
        for name in sorted(path.name for path in first.iterdir()):
            self.assertEqual(sha(first / name), sha(second / name), name)
        legacy = read_json(first / "04_Machine_Readable.json")
        self.assertFalse(legacy["bridge"]["available"])
        self.assertEqual(legacy["bridge"]["searched"],
                         ["build/forecast_world_bridge/report.json",
                          "build/forecast_world_bridge/forecast_world.json"])
        self.assertNotIn("bridge", [item["kind"] for item in legacy["sources"]])
        write_bridge(self.build)
        with_bridge, again = self.base / "with_bridge", self.base / "with_bridge_again"
        self.make_pack(out=with_bridge)
        self.make_pack(out=again)
        document = read_json(with_bridge / "04_Machine_Readable.json")
        self.assertNotEqual(document["document_digest"], legacy["document_digest"])
        self.assertTrue(document["bridge"]["available"])
        self.assertEqual(sha(with_bridge / "04_Machine_Readable.json"),
                         sha(again / "04_Machine_Readable.json"))
        self.assertEqual(sha(with_bridge / "03_Dashboard.html"),
                         sha(again / "03_Dashboard.html"))



class BlocksSection(FixtureCase):
    """Optional blocks 15/17: absent by default, numbers from artefacts, SHA, no forecast claim."""

    def test_blocks_are_not_included_by_default(self):
        summary = self.make_pack()
        document = self.machine()
        section = document["blocks"]
        self.assertFalse(section["included"])
        self.assertFalse(section["available"])
        self.assertEqual(section["status"], "NOT_REQUESTED")
        self.assertIn("--include-blocks", section["reason"])
        self.assertEqual(section["blocks"], [])
        self.assertFalse(document["summary"]["blocks_included"])
        self.assertEqual(document["summary"]["blocks_count"], 0)
        self.assertEqual(len(document["sources"]), 6)
        self.assertNotIn("block:block_15_institutions",
                         [item["kind"] for item in document["sources"]])
        self.assertEqual(len(summary["files"]), 8)
        # XLSX always carries the (empty) block sheets, so the layout is stable across builds
        from openpyxl import load_workbook

        workbook = load_workbook(self.out / "02_Numbers.xlsx", data_only=True)
        self.assertIn("Blocks", workbook.sheetnames)
        self.assertIn("Blocks_Scenarios", workbook.sheetnames)
        self.assertEqual(list(workbook["Blocks_Scenarios"]
                              .iter_rows(min_row=2, values_only=True)), [])

    def test_blocks_with_flag_carry_numbers_and_sha(self):
        summary = self.make_pack(include_blocks=True)
        document = self.machine()
        section = document["blocks"]
        self.assertTrue(section["included"])
        self.assertTrue(section["available"])
        self.assertEqual(section["status"], "OK")
        blocks = {entry["id"]: entry for entry in section["blocks"]}
        self.assertEqual(sorted(blocks), ["block_15_institutions", "block_17_technology"])
        institutions = blocks["block_15_institutions"]
        self.assertEqual(institutions["sha256"],
                         sha(self.build / "institutions" / "report.json"))
        self.assertEqual(institutions["source"], "build/institutions/report.json")
        self.assertEqual(institutions["profile"], "EXAMPLE-MID")
        self.assertEqual(institutions["status"], "RESEARCH_ONLY")
        self.assertFalse(institutions["admission"])
        self.assertEqual(institutions["assumptions_count"], 2)
        reform = next(item for item in institutions["scenarios"] if item["id"] == "REFORM")
        self.assertAlmostEqual(reform["delta_score_points"], 6.0)
        self.assertAlmostEqual(reform["delta_risk_premium_bps"], -23.48)
        self.assertAlmostEqual(reform["delta_investment_pp"], 0.24)
        technology = blocks["block_17_technology"]
        self.assertEqual(technology["sha256"],
                         sha(self.build / "technology_scenarios" / "report.json"))
        self.assertEqual(technology["horizon_years"], 15)
        self.assertEqual(len(technology["scenarios"]), 2)
        self.assertAlmostEqual(technology["scenarios"][1]["output_gain_pct"], 41.0)
        self.assertAlmostEqual(technology["scenarios"][1]["energy_intensity_change_pct"], -21.37)
        self.assertEqual(document["summary"]["blocks_status"], "OK")
        self.assertEqual(document["summary"]["blocks_count"], 2)
        self.assertEqual(document["summary"]["blocks_scenarios"], 5)
        self.assertEqual(document["summary"]["sources"], 8)
        kinds = [item["kind"] for item in document["sources"]]
        self.assertIn("block:block_15_institutions", kinds)
        self.assertIn("block:block_17_technology", kinds)
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertEqual(len(manifest["inputs"]), 8)
        self.assertIn("--include-blocks", manifest["command"])
        self.assertEqual(summary["numbers"]["blocks"]["count"], 2)
        # the CLI flag produces the same block numbers
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pack.main(["--root", str(self.root), "--inputs-root", str(self.build),
                              "--out", str(self.base / "cli_blocks"), "--generated-at", FIXED,
                              "--include-blocks", "--json"])
        self.assertEqual(code, 0)
        printed = json.loads(buffer.getvalue())
        self.assertTrue(printed["numbers"]["blocks"]["available"])
        self.assertEqual(printed["numbers"]["blocks"]["count"], 2)

    def test_blocks_missing_artefacts_are_unavailable(self):
        (self.build / "institutions" / "report.json").unlink()
        (self.build / "technology_scenarios" / "report.json").unlink()
        summary = self.make_pack(include_blocks=True)
        document = self.machine()
        section = document["blocks"]
        self.assertTrue(section["included"])
        self.assertFalse(section["available"])
        self.assertEqual(section["status"], "NOT_AVAILABLE")
        for entry in section["blocks"]:
            self.assertFalse(entry["available"])
            self.assertEqual(entry["status"], "NOT_AVAILABLE")
            self.assertIn("не найден", entry["reason"])
            self.assertTrue(entry["searched"])
        self.assertEqual(len(summary["files"]), 8)
        self.assertEqual(document["summary"]["blocks_count"], 0)
        self.assertEqual(len(document["sources"]), 6)
        self.assertIn("Раздел недоступен",
                      docx_text(self.out / "01_Executive_Summary.docx"))
        self.assertIn("Раздел недоступен",
                      (self.out / "03_Dashboard.html").read_text(encoding="utf-8"))
        self.assertIn("недоступен", (self.out / "README.md").read_text(encoding="utf-8"))

    def test_blocks_in_xlsx_docx_html_readme(self):
        from openpyxl import load_workbook

        self.make_pack(include_blocks=True)
        document = self.machine()
        workbook = load_workbook(self.out / "02_Numbers.xlsx", data_only=True)
        rows = list(workbook["Blocks_Scenarios"].iter_rows(min_row=2, values_only=True))
        expected = [item["id"] for entry in document["blocks"]["blocks"]
                    for item in entry["scenarios"]]
        self.assertEqual([row[2] for row in rows], expected)
        self.assertEqual([row[0] for row in rows], [15, 15, 15, 17, 17])
        blocks_rows = {row[1]: row for row in
                       workbook["Blocks"].iter_rows(min_row=2, values_only=True) if row[1]}
        self.assertEqual(blocks_rows["block_15_institutions"][6],
                         document["blocks"]["blocks"][0]["sha256"])
        self.assertEqual(int(blocks_rows["block_17_technology"][9]), 2)  # scenarios per block
        text = docx_text(self.out / "01_Executive_Summary.docx")
        for token in ("Блоки 15/17", "REFORM", "EXAMPLE-MID", "ai_acceleration",
                      "Пять агрегированных отраслей"):
            self.assertIn(token, text, token)
        html = (self.out / "03_Dashboard.html").read_text(encoding="utf-8")
        for token in ("Блоки 15/17", "REFORM", "ai_acceleration",
                      document["blocks"]["blocks"][0]["sha256"]):
            self.assertIn(token, html, token)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        self.assertIn("Блоки 15/17", readme)
        self.assertIn("--include-blocks", readme)
        self.assertEqual(numeric_lists(document), [])

    def test_blocks_bad_artefacts_fail_closed_and_never_claim_a_forecast(self):
        path = self.build / "institutions" / "report.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(pack.ClientPackError):
            self.make_pack(include_blocks=True)
        self.assertFalse(self.out.exists())
        write_json(path, {"schema": "amfe.something_else.v9", "status": "OK"})
        with self.assertRaises(pack.ClientPackError):
            self.make_pack(include_blocks=True)
        self.assertFalse(self.out.exists())
        write_blocks(self.build)
        report = read_json(self.build / "technology_scenarios" / "report.json")
        report["admission"] = True
        write_json(self.build / "technology_scenarios" / "report.json", report)
        with self.assertRaises(pack.ClientPackError):
            self.make_pack(include_blocks=True)
        self.assertFalse(self.out.exists())
        write_blocks(self.build)
        self.make_pack(include_blocks=True)
        section = self.machine()["blocks"]
        self.assertEqual(section["schema"], pack.BLOCKS_SCHEMA)
        self.assertFalse(section["admission"])
        self.assertIn("не прогноз", section["note"])
        self.assertNotIn("прогноз:", json.dumps(section, ensure_ascii=False))
        for entry in section["blocks"]:
            self.assertEqual(entry["status"], "RESEARCH_ONLY")
            self.assertFalse(entry["admission"])


class CommandLine(FixtureCase):
    def test_cli_success_prints_summary(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pack.main(["--root", str(self.root), "--inputs-root", str(self.build),
                              "--out", str(self.out), "--generated-at", FIXED, "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(buffer.getvalue())
        self.assertEqual(summary["status"], "RESEARCH_ONLY")
        self.assertEqual(summary["scenario_id"], "hormuz_closed_6m")
        self.assertEqual(len(summary["files"]), 8)
        self.assertTrue((self.out / "04_Machine_Readable.json").is_file())

    def test_cli_fail_closed_returns_two(self):
        import contextlib
        import io

        (self.build / "product" / "forecast_latest.json").unlink()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = pack.main(["--root", str(self.root), "--inputs-root", str(self.build),
                              "--out", str(self.out), "--generated-at", FIXED])
        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "BLOCKED_DATA")
        self.assertFalse(self.out.exists())


class DefaultTimestamp(FixtureCase):
    def test_default_generated_at_comes_from_the_inputs(self):
        summary = self.make_pack(generated_at=None)
        self.assertEqual(summary["generated_at"], "2026-11-16T00:00:00Z")


class PdfExport(FixtureCase):
    """PDF export is optional and non-deterministic; it must never affect the byte-identical manifest."""

    def _build_with_pdf(self):
        return self.make_pack(make_pdf=True)

    def test_pdf_files_match_docx_sources_when_soffice_present(self):
        summary = self._build_with_pdf()
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertIn("pdf_file_count", manifest)
        if pack._soffice() is None:
            self.skipTest("LibreOffice not available")
        self.assertEqual(summary["pdfs"], ["01_Executive_Summary.pdf",
                                           "05_Methodology_and_Sources.pdf",
                                           "06_Licence_and_Disclaimer.pdf"])
        self.assertEqual(manifest["pdf_file_count"], 3)
        self.assertEqual(len(manifest["non_deterministic_artifacts"]), 3)
        for name, meta in manifest["non_deterministic_artifacts"].items():
            pdf = self.out / name
            self.assertTrue(pdf.is_file(), name)
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"), name)
            self.assertEqual(meta["sha256"], sha(pdf), name)
            self.assertIn("source", meta)
            self.assertGreater(meta["bytes"], 100, name)
            self.assertGreater(meta["pages"], 0, name)

    def test_no_pdf_keeps_deterministic_manifest_unchanged(self):
        self.make_pack(make_pdf=False)
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertEqual(manifest["pdf_file_count"], 0)
        self.assertEqual(manifest["non_deterministic_artifacts"], {})
        names = sorted(path.name for path in self.out.iterdir())
        self.assertNotIn("01_Executive_Summary.pdf", names)

    def test_deterministic_artifacts_exclude_pdfs(self):
        self._build_with_pdf()
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertEqual(manifest["file_count"], 7)
        self.assertEqual(manifest["file_count"], len(manifest["deterministic_artifacts"]))
        self.assertNotIn("01_Executive_Summary.pdf", manifest["deterministic_artifacts"])

    def test_readme_lists_pdfs_when_present(self):
        self._build_with_pdf()
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        if pack._soffice() is None:
            self.assertNotIn(".pdf", readme)
        else:
            self.assertIn("01_Executive_Summary.pdf", readme)
            self.assertIn("недетерминированный артефакт", readme)


@unittest.skipUnless(REAL_ARTIFACTS.is_file(), "generated build/ artefacts are not present")
class RealArtifacts(unittest.TestCase):
    def test_pack_builds_from_the_real_artefacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            summary = pack.build_pack(out, inputs_root=ROOT / "build",
                                      generated_at=FIXED, root=ROOT)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["status"], "RESEARCH_ONLY")
            document = read_json(out / "04_Machine_Readable.json")
            self.assertEqual(document["schema"], pack.SCHEMA)
            self.assertIn(document["summary"]["candidate_status"],
                          ("CANDIDATE", "RESEARCH_ONLY"))
            registry_entries = [json.loads(line) for line in
                                (ROOT / "build" / "registry" / "modules.jsonl")
                                .read_text(encoding="utf-8").splitlines() if line.strip()]
            latest = {}
            for entry in registry_entries:  # append-only log: the latest record per module wins
                latest[entry["module_id"]] = entry
            self.assertEqual(document["modules"]["count"], len(latest))
            self.assertIn("oil.brent", latest)
            self.assertEqual(document["market_scenario"]["scenario_id"], "hormuz_closed_6m")
            self.assertTrue(document["sources"])
            # The published G5 slice: one passport per registry module (9), 2 market modules.
            self.assertEqual(document["summary"]["modules_total"], len(latest))
            self.assertEqual(document["summary"]["market_modules"], 2)
            self.assertEqual(document["summary"]["market_module_ids"], ["gas.lng_eu", "oil.brent"])
            self.assertEqual(document["summary"]["forecast_targets"], ["gas_ttf", "oil_brent"])
            gas = next(item for item in document["forecasts"]["items"]
                       if item["target"] == "gas_ttf")
            self.assertEqual(gas["module_id"], "gas.lng_eu")
            self.assertEqual(gas["status"], "RESEARCH_ONLY")
            self.assertGreater(gas["price_peak"], 0)
            self.assertIn("build/api/forecasts/gas_ttf.json",
                          [item["path"] for item in document["sources"]])
            self.assertEqual(len(document["sources"]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
