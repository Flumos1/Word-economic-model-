"""Forecast API contract (tools/forecast_api.py): schema, real fields, OpenAPI, determinism, fail-closed."""
import json
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
SCENARIOS = [scenario.scenario_id for scenario in OIL_MODULE.scenarios()]


def build(out: Path, generated_at: str = FIXED, modules=None) -> dict:
    return forecast_api.build_api(out, generated_at=generated_at, modules=modules)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class ForecastApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "api"
        cls.summary = build(cls.out)
        cls.index = read_json(cls.out / "index.json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_index_schema_and_counts(self):
        index = self.index
        self.assertEqual(index["schema"], forecast_api.SCHEMA)
        self.assertEqual(index["kind"], "index")
        self.assertEqual(index["api_version"], "v1")
        self.assertEqual(index["status"], "RESEARCH_ONLY")
        self.assertEqual(index["counts"], {"modules": 1, "scenarios": len(SCENARIOS),
                                           "forecasts": 1})
        self.assertEqual(index["modules"][0]["module_id"], "oil.brent")
        self.assertEqual(index["forecasts"][0]["target"], "oil_brent")
        self.assertTrue(index["policy"]["research_only"])
        self.assertTrue(index["policy"]["no_admission"])
        self.assertTrue(index["policy"]["no_point_forecast"])
        self.assertIn("not a forecast", index["disclaimer"].lower())

    def test_all_endpoints_have_files(self):
        for endpoint in self.index["endpoints"]:
            self.assertIn("path", endpoint)
            self.assertIn("file", endpoint)
        for scenario in self.index["scenarios"]:
            self.assertTrue((self.out / scenario["file"]).is_file(), scenario["id"])
        for forecast in self.index["forecasts"]:
            self.assertTrue((self.out / forecast["file"]).is_file(), forecast["target"])
        self.assertTrue((self.out / "openapi.json").is_file())
        self.assertTrue((self.out / "README.md").is_file())

    def test_scenario_documents_use_real_market_result(self):
        for scenario_id in SCENARIOS:
            document = read_json(self.out / "scenarios" / f"oil.brent__{scenario_id}.json")
            expected = OIL_MODULE.run(scenario_id).as_dict()
            self.assertEqual(document["schema"], forecast_api.SCHEMA)
            self.assertEqual(document["kind"], "scenario")
            self.assertEqual(document["id"], f"oil.brent/{scenario_id}")
            self.assertEqual(document["status"], "RESEARCH_ONLY")
            self.assertEqual(document["scenario"]["scenario_id"], scenario_id)
            self.assertEqual(document["result"]["price_path"], expected["price_path"])
            self.assertEqual(document["result"]["summary"], expected["summary"])
            self.assertEqual(document["result"]["digest"], expected["digest"])
            self.assertEqual(len(document["result"]["assumptions"]),
                             len(expected["assumptions"]))
            self.assertIn("assumption", document["result"]["assumptions"][0]["source_class"])

    def test_forecast_document_has_real_range_and_no_point(self):
        document = read_json(self.out / "forecasts" / "oil_brent.json")
        expected = OIL_MODULE.run(OIL_MODULE.default_scenario().scenario_id).as_dict()
        self.assertEqual(document["kind"], "forecast_envelope")
        self.assertEqual(document["target"], "oil_brent")
        self.assertTrue(document["no_point_forecast"])
        envelope = document["envelope"]
        self.assertEqual(envelope["price_path"], expected["price_path"])
        self.assertEqual(envelope["range_peak"], expected["summary"]["brent_range_usd_per_bbl"])
        self.assertEqual(envelope["range_mean"], expected["summary"]["brent_range_mean_usd_per_bbl"])
        self.assertNotIn("point", envelope)
        self.assertEqual(document["result_digest"], expected["digest"])
        self.assertEqual(document["assumptions_count"], len(expected["assumptions"]))
        # alias accepted, distinct target id is rejected
        alias = forecast_api.build_forecast(OIL_MODULE, "brent", FIXED)
        self.assertEqual(alias["target"], "brent")
        with self.assertRaises(forecast_api.ForecastApiError):
            forecast_api.build_forecast(OIL_MODULE, "nope", FIXED)

    def test_openapi_describes_all_endpoints(self):
        document = read_json(self.out / "openapi.json")
        self.assertEqual(document["openapi"], "3.0.3")
        self.assertIn("/v1/scenarios", document["paths"])
        self.assertIn("/v1/scenarios/{scenario_id}", document["paths"])
        self.assertIn("/v1/forecast/{target}", document["paths"])
        schemas = document["components"]["schemas"]
        for name in ("Index", "Scenario", "Forecast", "Error"):
            self.assertIn(name, schemas)
        operation_id = document["paths"]["/v1/forecast/{target}"]["get"]["operationId"]
        self.assertEqual(operation_id, "getForecast")
        self.assertIn("oil_brent",
                      document["paths"]["/v1/forecast/{target}"]["get"]["parameters"][0]
                      ["schema"]["enum"])

    def test_no_admission_no_raw_data_and_no_external_urls(self):
        for path in [self.out / "index.json", *sorted((self.out / "scenarios").glob("*.json")),
                     *sorted((self.out / "forecasts").glob("*.json"))]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"ADMITTED"', text)
            self.assertNotIn('"ACTIVE"', text)
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
        scenario_text = (self.out / "scenarios" / f"oil.brent__{SCENARIOS[0]}.json").read_text(
            encoding="utf-8")
        for raw_key in ('"Z"', '"observations"', '"raw"'):
            self.assertNotIn(raw_key, scenario_text)

    def test_index_file_hashes_are_consistent(self):
        for entry in self.index["files"]:
            path = self.out / entry["path"]
            self.assertEqual(forecast_api._sha256(path), entry["sha256"], entry["path"])
            self.assertEqual(path.stat().st_size, entry["bytes"])
        self.assertEqual(self.summary["index_sha256"],
                         forecast_api._sha256(self.out / "index.json"))

    def test_determinism_except_generated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            third = Path(tmp) / "third"
            build(first)
            build(second)
            build(third, generated_at="2026-11-16T11:00:00Z")
            for name in ("openapi.json", "scenarios/oil.brent__hormuz_closed_6m.json",
                         "forecasts/oil_brent.json"):
                left = (first / name).read_text(encoding="utf-8")
                right = (second / name).read_text(encoding="utf-8")
                self.assertEqual(left, right, name)
                later = (third / name).read_text(encoding="utf-8")
                self.assertEqual(left.replace(FIXED, "<ts>"), later.replace(
                    "2026-11-16T11:00:00Z", "<ts>"), name)
            # the index hashes the response files, so its own hashes move with generated_at;
            # the contract content (modules/scenarios/forecasts) must still be identical
            def normalised_index(path):
                document = json.loads(Path(path).read_text(encoding="utf-8"))
                document.pop("generated_at", None)
                document.pop("files", None)
                return document
            self.assertEqual(normalised_index(first / "index.json"),
                             normalised_index(third / "index.json"))

    def test_fail_closed_without_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(forecast_api.ForecastApiError):
                forecast_api.build_api(Path(tmp) / "api", generated_at=FIXED, modules=[])

    def test_cli_writes_files_and_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "api"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"),
                 "--out", str(out), "--generated-at", FIXED],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "OK")
            self.assertTrue((out / "index.json").is_file())
            help_run = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--help"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(help_run.returncode, 0)
            self.assertIn("--out", help_run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
