"""Forecast API contract v2 (tools/forecast_api.py): module passports, v1 compatibility, fail-closed."""
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

FIXED = "2026-11-16T10:00:00Z"
OTHER_TS = "2026-11-16T11:00:00Z"
SCENARIOS = [scenario.scenario_id for scenario in OIL_MODULE.scenarios()]
REGISTRY_REQUIRED = {"module_id", "version", "status", "kind", "limitations", "code_sha256"}


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
        self.assertEqual(index["counts"], {"modules": len(self.registry_export["export"]["modules"]),
                                           "market_modules": 1, "scenarios": len(SCENARIOS),
                                           "forecasts": 1})
        self.assertTrue(index["policy"]["module_passports"])
        self.assertEqual(index["registry"]["sha256"], forecast_api._sha256(self.registry))

    def test_modules_have_required_passport_fields(self):
        modules = self.index["modules"]
        self.assertEqual(len(modules), len(self.registry_export["export"]["modules"]))
        for passport in modules:
            self.assertTrue(REGISTRY_REQUIRED.issubset(passport), passport.get("module_id"))
            self.assertTrue(passport["limitations"], passport.get("module_id"))
            self.assertIn(passport["status"], ("RESEARCH_ONLY", "CANDIDATE", "LIMITED", "ACTIVE"))
            self.assertEqual(len(passport["code_sha256"]), 64)
        oil = next(item for item in modules if item["module_id"] == "oil.brent")
        self.assertEqual(oil["version"], self.oil_passport["version"])
        self.assertEqual(oil["code_sha256"], self.oil_passport["code_sha256"])
        self.assertEqual(oil["kind"], "market")

    def test_modules_endpoint_document(self):
        path = self.out / "modules.json"
        self.assertTrue(path.is_file())
        document = read_json(path)
        self.assertEqual(document["schema"], "amfe.forecast_api.v2")
        self.assertEqual(document["kind"], "modules")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["registry"]["sha256"], forecast_api._sha256(self.registry))
        self.assertEqual(len(document["modules"]), len(self.index["modules"]))
        self.assertEqual(document["counts"]["modules"], len(document["modules"]))
        self.assertIn("RESEARCH_ONLY", document["counts"]["by_status"])
        single = read_json(self.out / "modules" / "oil.brent.json")
        self.assertEqual(single["kind"], "module")
        self.assertEqual(single["module"]["module_id"], "oil.brent")
        self.assertEqual(single["status"], "RESEARCH_ONLY")

    def test_scenarios_link_to_registered_module_version(self):
        for scenario_id in SCENARIOS:
            document = read_json(self.out / "scenarios" / f"oil.brent__{scenario_id}.json")
            self.assertEqual(document["schema"], "amfe.forecast_api.v2")
            self.assertEqual(document["api_version"], "v2")
            self.assertEqual(document["module_ref"]["module_id"], "oil.brent")
            self.assertEqual(document["module_ref"]["version"], self.oil_passport["version"])
            self.assertEqual(document["module_ref"]["code_sha256"],
                             self.oil_passport["code_sha256"])
            self.assertEqual(document["links"]["modules"], "modules.json")
            expected = OIL_MODULE.run(scenario_id).as_dict()
            self.assertEqual(document["result"]["price_path"], expected["price_path"])
            self.assertEqual(document["result"]["summary"], expected["summary"])
            self.assertEqual(document["result"]["digest"], expected["digest"])
        for entry in self.index["scenarios"]:
            self.assertEqual(entry["module_ref"]["version"], self.oil_passport["version"])
            self.assertIn("module_version", entry)

    def test_forecast_links_and_real_envelope(self):
        document = read_json(self.out / "forecasts" / "oil_brent.json")
        expected = OIL_MODULE.run(OIL_MODULE.default_scenario().scenario_id).as_dict()
        self.assertEqual(document["kind"], "forecast_envelope")
        self.assertEqual(document["api_version"], "v2")
        self.assertEqual(document["module_ref"]["module_id"], "oil.brent")
        self.assertEqual(document["module_ref"]["version"], self.oil_passport["version"])
        self.assertEqual(document["links"]["modules"], "modules.json")
        self.assertTrue(document["no_point_forecast"])
        self.assertEqual(document["envelope"]["range_peak"],
                         expected["summary"]["brent_range_usd_per_bbl"])
        self.assertNotIn("point", document["envelope"])
        self.assertEqual(document["result_digest"], expected["digest"])

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
            self.assertEqual(index["counts"]["modules"], 1)  # v1 semantics: market modules
            self.assertEqual(index["modules"][0]["module_id"], "oil.brent")
            self.assertIsInstance(index["modules"][0]["scenarios"], list)
            scenario = read_json(v1 / "scenarios" / "oil.brent__hormuz_closed_6m.json")
            self.assertEqual(scenario["schema"], "amfe.forecast_api.v1")
            self.assertNotIn("module_ref", scenario)
            forecast = read_json(v1 / "forecasts" / "oil_brent.json")
            self.assertNotIn("module_ref", forecast)
            openapi = read_json(v1 / "openapi.json")
            self.assertNotIn("/v1/modules", openapi["paths"])
            # numbers are identical between v1 and v2
            v2 = read_json(self.out / "scenarios" / "oil.brent__hormuz_closed_6m.json")
            self.assertEqual(scenario["result"]["summary"], v2["result"]["summary"])

    def test_v2_keeps_every_v1_field(self):
        scenario = read_json(self.out / "scenarios" / "oil.brent__hormuz_closed_6m.json")
        for key in ("schema", "kind", "api_version", "generated_at", "status", "disclaimer",
                    "id", "module", "scenario", "result"):
            self.assertIn(key, scenario)
        forecast = read_json(self.out / "forecasts" / "oil_brent.json")
        for key in ("schema", "kind", "api_version", "target", "target_definition", "module_id",
                    "envelope", "research_only", "no_point_forecast", "result_digest"):
            self.assertIn(key, forecast)

    def test_no_admission_claims_or_raw_data(self):
        for path in [self.out / "index.json", self.out / "modules.json",
                     self.out / "openapi.json", *sorted((self.out / "scenarios").glob("*.json")),
                     *sorted((self.out / "forecasts").glob("*.json")),
                     *sorted((self.out / "modules").glob("*.json"))]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"ACTIVE"', text)
            self.assertNotIn('"LIMITED"', text)
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
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
                         "forecasts/oil_brent.json", "modules/oil.brent.json"):
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
            v1 = Path(tmp) / "v1"
            legacy = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--out", str(v1),
                 "--generated-at", FIXED, "--api-version", "v1"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertFalse((v1 / "modules.json").exists())
            help_run = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--help"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(help_run.returncode, 0)
            self.assertIn("--api-version", help_run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
