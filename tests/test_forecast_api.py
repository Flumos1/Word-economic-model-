"""Forecast API contract v2 (tools/forecast_api.py): oil + gas envelopes, 9 passports, fail-closed."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import forecast_api  # noqa: E402
from amfe.markets import OIL_MODULE  # noqa: E402
from amfe.markets import gas as gas_market  # noqa: E402
from amfe.markets.contract import MarketModule, MarketResult  # noqa: E402

GAS_MODULE = gas_market.GAS_MODULE
FIXED = "2026-11-16T10:00:00Z"
OTHER_TS = "2026-11-16T11:00:00Z"
OIL_SCENARIOS = [scenario.scenario_id for scenario in OIL_MODULE.scenarios()]
GAS_SCENARIOS = [scenario.scenario_id for scenario in GAS_MODULE.scenarios()]
REGISTRY_REQUIRED = {"module_id", "version", "status", "kind", "limitations", "code_sha256"}
RAW_KEYS = {"raw", "raw_data", "raw_values", "observations", "observed_values", "source_rows",
            "source_series", "series", "series_values", "third_party_data", "data_points"}
LEGACY_OIL_ENVELOPE = {"price_path", "price_peak", "price_mean", "price_trough",
                       "price_peak_month", "range_peak", "range_mean", "sensitivity", "checks",
                       "units"}


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ensure_registry(base: Path) -> Path:
    """Use the workspace registry, or build one in the temp dir if it is absent."""
    source = forecast_api.DEFAULT_REGISTRY
    if source.is_file():
        target = Path(base) / "modules.jsonl"
        shutil.copy2(source, target)
        return target
    target = Path(base) / "modules.jsonl"
    subprocess.run([sys.executable, str(ROOT / "tools" / "register_modules.py"),
                    "--registry", str(target), "--date", "2026-11-16"],
                   cwd=str(ROOT), check=True, capture_output=True, text=True, encoding="utf-8")
    return target


def walk(value, path=()):
    """Yield ``(key_path, key, leaf)`` for every mapping entry in a JSON document."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield path, str(key), child
            yield from walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (str(index),))


def assert_no_raw(testcase: unittest.TestCase, document, label: str) -> None:
    for path, key, _value in walk(document):
        lowered = key.lower()
        testcase.assertNotIn(lowered, RAW_KEYS, f"{label}: raw key {key!r} at {path}")
        testcase.assertFalse(lowered.startswith("raw_"), f"{label}: raw key {key!r}")
        testcase.assertFalse(lowered.endswith("_raw"), f"{label}: raw key {key!r}")


class FakeStorageModule(MarketModule):
    """A layer module (kind ``storage``) that has no oil summary fields: the generic fallback."""

    module_id = "storage.demo"
    commodity = "Demo storage layer"
    units = "USD/bbl"

    def scenarios(self):
        from amfe.markets.contract import Scenario
        return (Scenario("demo_state", "demo", {}, horizon_months=2),)

    def run(self, scenario_id=None, **overrides):
        return MarketResult(
            module_id=self.module_id, commodity=self.commodity, scenario_id="demo_state",
            units=self.units, horizon_months=2, price_path=(10.0, 11.0),
            summary={"price_peak": 11.0, "price_mean": 10.5, "storage_end_mb": 100.0},
            assumptions=(), sensitivity=({"variant": "central", "price_peak": 11.0},),
            checks={"deterministic": True}, meta={})


class ForecastApiV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.registry = ensure_registry(cls.base)
        cls.out = cls.base / "api"
        cls.summary = forecast_api.build_api(cls.out, generated_at=FIXED,
                                             registry_path=cls.registry)
        cls.index = read_json(cls.out / "index.json")
        cls.registry_export = forecast_api.load_registry(cls.registry)
        cls.oil_passport = cls.registry_export["by_id"]["oil.brent"]
        cls.gas_passport = cls.registry_export["by_id"]["gas.lng_eu"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_index_schema_and_counts(self):
        index = self.index
        self.assertEqual(index["schema"], "amfe.forecast_api.v2")
        self.assertEqual(index["api_version"], "v2")
        self.assertEqual(index["api_versions"], ["v1", "v2"])
        self.assertEqual(index["backward_compatible_with"], "amfe.forecast_api.v1")
        self.assertEqual(index["status"], "RESEARCH_ONLY")
        passports = self.registry_export["export"]["modules"]
        self.assertEqual(len(passports), 9)
        self.assertEqual(index["counts"], {
            "modules": len(passports), "market_modules": 2,
            "scenarios": len(OIL_SCENARIOS) + len(GAS_SCENARIOS), "forecasts": 2})
        self.assertEqual(index["registry"]["counts"]["modules"], 9)
        self.assertEqual(index["registry"]["counts"]["by_kind"]["market"], 3)
        self.assertTrue(index["policy"]["module_passports"])
        self.assertTrue(index["policy"]["no_admission"])
        self.assertTrue(index["policy"]["no_point_forecast"])
        self.assertTrue(index["policy"]["no_probability_forecast"])
        self.assertEqual(index["registry"]["sha256"], forecast_api._sha256(self.registry))

    def test_modules_have_required_passport_fields(self):
        modules = self.index["modules"]
        self.assertEqual(len(modules), 9)
        for passport in modules:
            self.assertTrue(REGISTRY_REQUIRED.issubset(passport), passport.get("module_id"))
            self.assertTrue(passport["limitations"], passport.get("module_id"))
            self.assertIn(passport["status"], ("RESEARCH_ONLY", "CANDIDATE", "LIMITED", "ACTIVE"))
            self.assertEqual(len(passport["code_sha256"]), 64)
        by_id = {item["module_id"]: item for item in modules}
        self.assertEqual(by_id["oil.brent"]["version"], self.oil_passport["version"])
        self.assertEqual(by_id["oil.brent"]["code_sha256"], self.oil_passport["code_sha256"])
        self.assertEqual(by_id["oil.brent"]["kind"], "market")
        self.assertEqual(by_id["gas.lng_eu"]["version"], self.gas_passport["version"])
        self.assertEqual(by_id["gas.lng_eu"]["kind"], "market")
        self.assertEqual(by_id["oil.storage_curve"]["kind"], "market")

    def test_modules_endpoint_document(self):
        path = self.out / "modules.json"
        self.assertTrue(path.is_file())
        document = read_json(path)
        self.assertEqual(document["schema"], "amfe.forecast_api.v2")
        self.assertEqual(document["kind"], "modules")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["registry"]["sha256"], forecast_api._sha256(self.registry))
        self.assertEqual(len(document["modules"]), 9)
        self.assertEqual(document["counts"]["modules"], 9)
        self.assertIn("RESEARCH_ONLY", document["counts"]["by_status"])
        for module_id in ("oil.brent", "gas.lng_eu", "oil.storage_curve",
                          "world.institutions", "world.technology"):
            single = read_json(self.out / "modules" / f"{module_id}.json")
            self.assertEqual(single["kind"], "module")
            self.assertEqual(single["module"]["module_id"], module_id)
            self.assertEqual(single["status"], "RESEARCH_ONLY")
        by_id = {item["module_id"]: item for item in document["modules"]}
        self.assertEqual(by_id["world.institutions"]["kind"], "world")
        self.assertEqual(by_id["world.technology"]["kind"], "world")

    def test_scenarios_link_to_registered_module_versions(self):
        for module, passport, scenario_ids in (
                (OIL_MODULE, self.oil_passport, OIL_SCENARIOS),
                (GAS_MODULE, self.gas_passport, GAS_SCENARIOS)):
            for scenario_id in scenario_ids:
                document = read_json(
                    self.out / "scenarios" / f"{module.module_id}__{scenario_id}.json")
                self.assertEqual(document["schema"], "amfe.forecast_api.v2")
                self.assertEqual(document["api_version"], "v2")
                self.assertEqual(document["module_ref"]["module_id"], module.module_id)
                self.assertEqual(document["module_ref"]["version"], passport["version"])
                self.assertEqual(document["module_ref"]["code_sha256"],
                                 passport["code_sha256"])
                self.assertEqual(document["links"]["modules"], "modules.json")
                expected = module.run(scenario_id).as_dict()
                self.assertEqual(document["result"]["price_path"], expected["price_path"])
                self.assertEqual(document["result"]["summary"], expected["summary"])
                self.assertEqual(document["result"]["digest"], expected["digest"])
        for entry in self.index["scenarios"]:
            self.assertTrue(entry["module_ref"]["version"])
            self.assertIn("module_version", entry)

    def test_oil_forecast_backward_compatible_envelope(self):
        document = read_json(self.out / "forecasts" / "oil_brent.json")
        expected = OIL_MODULE.run(OIL_MODULE.default_scenario().scenario_id).as_dict()
        self.assertEqual(document["kind"], "forecast_envelope")
        self.assertEqual(document["target"], "oil_brent")
        self.assertEqual(document["module_id"], "oil.brent")
        self.assertEqual(document["module_ref"]["version"], self.oil_passport["version"])
        self.assertTrue(document["no_point_forecast"])
        self.assertFalse(document["point_estimate"])
        envelope = document["envelope"]
        self.assertEqual(set(envelope), LEGACY_OIL_ENVELOPE)
        self.assertEqual(envelope["range_peak"],
                         expected["summary"]["brent_range_usd_per_bbl"])
        self.assertEqual(envelope["range_mean"],
                         expected["summary"]["brent_range_mean_usd_per_bbl"])
        self.assertEqual(envelope["price_peak"], expected["summary"]["price_peak"])
        self.assertNotIn("point", envelope)
        self.assertNotIn("interval", envelope)
        self.assertEqual(document["result_digest"], expected["digest"])

    def test_gas_forecast_envelope_point_interval_no_raw(self):
        document = read_json(self.out / "forecasts" / "gas_ttf.json")
        expected = GAS_MODULE.run(GAS_MODULE.default_scenario().scenario_id).as_dict()
        summary = expected["summary"]
        self.assertEqual(document["schema"], "amfe.forecast_api.v2")
        self.assertEqual(document["kind"], "forecast_envelope")
        self.assertEqual(document["target"], "gas_ttf")
        self.assertEqual(document["module_id"], "gas.lng_eu")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["module_ref"]["module_id"], "gas.lng_eu")
        self.assertEqual(document["module_ref"]["version"], self.gas_passport["version"])
        definition = document["target_definition"]
        self.assertEqual(definition["default_scenario"], "eu_pipeline_shock_6m")
        self.assertIn("eu_pipeline_shock_6m", definition["scenario_ids"])
        self.assertEqual(set(definition["scenario_ids"]), set(GAS_SCENARIOS))
        self.assertEqual(definition["units"], "USD/MMBtu")
        self.assertEqual(definition["forecast_metric"], "ttf_peak_usd_per_mmbtu")
        self.assertFalse(definition["point_estimate"])
        envelope = document["envelope"]
        self.assertEqual(envelope["price_path"], expected["price_path"])
        self.assertEqual(envelope["price_peak"], summary["ttf_peak_usd_per_mmbtu"])
        self.assertEqual(envelope["price_mean"], summary["ttf_mean_usd_per_mmbtu"])
        self.assertEqual(envelope["price_trough"], summary["ttf_trough_usd_per_mmbtu"])
        self.assertEqual(envelope["range_peak"], summary["ranges"]["ttf_peak"])
        self.assertNotIn("point", envelope)  # no point forecast, only a sensitivity interval
        interval = envelope["interval"]
        self.assertEqual(interval["low"], summary["ranges"]["ttf_peak"]["low"])
        self.assertEqual(interval["central"], summary["ranges"]["ttf_peak"]["central"])
        self.assertEqual(interval["high"], summary["ranges"]["ttf_peak"]["high"])
        self.assertFalse(interval["confidence_interval"])
        self.assertEqual(envelope["secondary"]["jkm"]["price_peak"],
                         summary["jkm_peak_usd_per_mmbtu"])
        self.assertEqual(envelope["secondary"]["diversion_bcm_y"],
                         summary["arbitrage"]["diversion"]["diversion_bcm_y"])
        self.assertFalse(document["point_estimate"])
        self.assertTrue(document["no_point_forecast"])
        self.assertEqual(document["result_digest"], expected["digest"])
        assert_no_raw(self, document, "gas forecast")
        self.assertNotIn("assumptions", document)
        self.assertNotIn("meta", document["envelope"])

    def test_gas_target_aliases_resolve_to_the_same_module(self):
        for alias in ("gas_ttf", "gas.lng_eu", "gas_lng_eu", "ttf"):
            document = forecast_api.build_forecast(GAS_MODULE, alias, FIXED,
                                                   self.registry_export)
            self.assertEqual(document["module_id"], "gas.lng_eu", alias)
            self.assertEqual(document["target"], alias)
            self.assertEqual(document["envelope"]["price_path"],
                             list(GAS_MODULE.run().price_path), alias)
        with self.assertRaises(forecast_api.ForecastApiError):
            forecast_api.build_forecast(GAS_MODULE, "oil_brent", FIXED, self.registry_export)

    def test_generic_envelope_for_a_storage_kind_module(self):
        registry = {"path": "fake", "by_id": {"storage.demo": {
            "module_id": "storage.demo", "version": "0.0.1", "status": "RESEARCH_ONLY",
            "kind": "storage", "code_sha256": "0" * 64}}}
        document = forecast_api.build_forecast(FakeStorageModule(), "storage_demo", FIXED,
                                               registry)
        self.assertEqual(document["target_definition"]["forecast_metric"], "price_peak")
        self.assertFalse(document["point_estimate"])
        envelope = document["envelope"]
        self.assertEqual(envelope["price_peak"], 11.0)
        self.assertEqual(envelope["summary"]["storage_end_mb"], 100.0)
        self.assertIn("sensitivity", envelope)
        for hardcoded in ("brent_range_usd_per_bbl", "ttf_peak_usd_per_mmbtu"):
            self.assertNotIn(hardcoded, envelope)

    def test_unknown_target_fails_closed_not_keyerror(self):
        for module in (OIL_MODULE, GAS_MODULE):
            with self.assertRaises(forecast_api.ForecastApiError) as caught:
                forecast_api.build_forecast(module, "nope", FIXED, self.registry_export)
            self.assertIn("unknown forecast target", str(caught.exception))

    def test_openapi_v2(self):
        document = read_json(self.out / "openapi.json")
        self.assertEqual(document["openapi"], "3.0.3")
        self.assertEqual(document["info"]["version"], "2.0.0")
        self.assertIn("/v1/modules", document["paths"])
        self.assertIn("ModulePassport", document["components"]["schemas"])
        self.assertIn("ModuleRef", document["components"]["schemas"])
        self.assertEqual(document["x-api-versions"], ["v1", "v2"])
        operation = document["paths"]["/v1/modules"]["get"]["operationId"]
        self.assertEqual(operation, "listModules")
        forecast = document["components"]["schemas"]["Forecast"]
        self.assertIn("point", forecast["properties"]["envelope"]["properties"])
        self.assertIn("interval", forecast["properties"]["envelope"]["properties"])
        target_enum = (document["paths"]["/v1/forecast/{target}"]["get"]["parameters"][0]
                       ["schema"]["enum"])
        self.assertIn("gas_ttf", target_enum)
        self.assertIn("oil_brent", target_enum)
        self.assertEqual(set(document["x-forecast-targets"]), {"oil_brent", "gas_ttf"})
        self.assertFalse(document["x-forecast-targets"]["gas_ttf"]["point_estimate"])

    def test_v1_backward_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            v1 = Path(tmp) / "v1"
            summary = forecast_api.build_api(v1, generated_at=FIXED, api_version="v1")
            self.assertEqual(summary["api_version"], "v1")
            index = read_json(v1 / "index.json")
            self.assertEqual(index["schema"], "amfe.forecast_api.v1")
            self.assertEqual(index["api_version"], "v1")
            self.assertNotIn("modules.json", index)
            self.assertFalse((v1 / "modules.json").exists())
            self.assertEqual(index["counts"], {"modules": 1, "scenarios": len(OIL_SCENARIOS),
                                               "forecasts": 1})
            self.assertEqual([item["module_id"] for item in index["modules"]], ["oil.brent"])
            self.assertIsInstance(index["modules"][0]["scenarios"], list)
            self.assertEqual(sorted(path.name for path in (v1 / "forecasts").glob("*.json")),
                             ["oil_brent.json"])
            self.assertFalse(any("gas" in path.name for path in (v1 / "scenarios").iterdir()))
            scenario = read_json(v1 / "scenarios" / "oil.brent__hormuz_closed_6m.json")
            self.assertEqual(scenario["schema"], "amfe.forecast_api.v1")
            self.assertNotIn("module_ref", scenario)
            forecast = read_json(v1 / "forecasts" / "oil_brent.json")
            self.assertNotIn("module_ref", forecast)
            self.assertNotIn("point_estimate", forecast)
            self.assertNotIn("scenario_ids", forecast["target_definition"])
            self.assertNotIn("kind", index["forecasts"][0])
            self.assertNotIn("no_probability_forecast", index["policy"])
            self.assertNotIn("gas_ttf", json.dumps(index))
            openapi = read_json(v1 / "openapi.json")
            self.assertNotIn("/v1/modules", openapi["paths"])
            self.assertNotIn("module_id", openapi["components"]["schemas"]["Forecast"]["required"])
            v2 = read_json(self.out / "scenarios" / "oil.brent__hormuz_closed_6m.json")
            self.assertEqual(scenario["result"]["summary"], v2["result"]["summary"])

    def test_v2_keeps_every_v1_field(self):
        scenario = read_json(self.out / "scenarios" / "oil.brent__hormuz_closed_6m.json")
        for key in ("schema", "kind", "api_version", "generated_at", "status", "disclaimer",
                    "id", "module", "scenario", "result"):
            self.assertIn(key, scenario)
        for path in ("oil_brent", "gas_ttf"):
            forecast = read_json(self.out / "forecasts" / f"{path}.json")
            for key in ("schema", "kind", "api_version", "target", "target_definition",
                        "module_id", "envelope", "research_only", "no_point_forecast",
                        "result_digest"):
                self.assertIn(key, forecast, path)

    def test_no_admission_claims_or_raw_data(self):
        files = [self.out / "index.json", self.out / "modules.json", self.out / "openapi.json",
                 *sorted((self.out / "scenarios").glob("*.json")),
                 *sorted((self.out / "forecasts").glob("*.json")),
                 *sorted((self.out / "modules").glob("*.json"))]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"ACTIVE"', text, path.name)
            self.assertNotIn('"LIMITED"', text, path.name)
            self.assertNotIn("http://", text, path.name)
            self.assertNotIn("https://", text, path.name)
            assert_no_raw(self, read_json(path), path.name)
        self.assertFalse(self.index["policy"].get("admission"))

    def test_file_hashes_are_consistent(self):
        for entry in self.index["files"]:
            path = self.out / entry["path"]
            self.assertEqual(forecast_api._sha256(path), entry["sha256"], entry["path"])
            self.assertEqual(path.stat().st_size, entry["bytes"])
        self.assertEqual(self.summary["index_sha256"],
                         forecast_api._sha256(self.out / "index.json"))
        self.assertEqual(self.summary["registry"]["sha256"], forecast_api._sha256(self.registry))

    def test_determinism_except_generated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first, second, third = base / "a", base / "b", base / "c"
            for out in (first, second):
                forecast_api.build_api(out, generated_at=FIXED, registry_path=self.registry)
            forecast_api.build_api(third, generated_at=OTHER_TS, registry_path=self.registry)
            for name in ("openapi.json", "modules.json",
                         "scenarios/oil.brent__hormuz_closed_6m.json",
                         "scenarios/gas.lng_eu__eu_pipeline_shock_6m.json",
                         "forecasts/oil_brent.json", "forecasts/gas_ttf.json",
                         "modules/oil.brent.json", "modules/gas.lng_eu.json"):
                left = (first / name).read_text(encoding="utf-8")
                right = (second / name).read_text(encoding="utf-8")
                self.assertEqual(left, right, name)
                later = (third / name).read_text(encoding="utf-8")
                self.assertEqual(left.replace(FIXED, "<ts>"), later.replace(OTHER_TS, "<ts>"), name)
            def normalised(path):
                document = read_json(path)
                document.pop("generated_at", None)
                document.pop("files", None)
                return document
            self.assertEqual(normalised(first / "index.json"), normalised(third / "index.json"))

    def test_fail_closed_missing_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no_registry.jsonl"
            with self.assertRaises(forecast_api.ForecastApiError):
                forecast_api.build_api(Path(tmp) / "api", generated_at=FIXED,
                                       registry_path=missing)
            self.assertFalse((Path(tmp) / "api" / "index.json").exists())
            with self.assertRaises(forecast_api.ForecastApiError):
                forecast_api.build_api(Path(tmp) / "api2", generated_at=FIXED, modules=[])

    def test_cli_v2_v1_and_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "api"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--out", str(out),
                 "--generated-at", FIXED, "--registry", str(self.registry)],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["api_version"], "v2")
            self.assertTrue((out / "modules.json").is_file())
            self.assertTrue((out / "forecasts" / "gas_ttf.json").is_file())
            v1 = Path(tmp) / "v1"
            legacy = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--out", str(v1),
                 "--generated-at", FIXED, "--api-version", "v1"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertFalse((v1 / "modules.json").exists())
            self.assertFalse((v1 / "forecasts" / "gas_ttf.json").exists())
            help_run = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--help"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(help_run.returncode, 0)
            self.assertIn("--api-version", help_run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
