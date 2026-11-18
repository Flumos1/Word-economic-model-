"""Client Pack v2: structure, formats, artefact-only numbers, fail-closed, determinism, licence.

Most tests build a small synthetic artefact tree (the same schemas as the real build/ artefacts with
known numbers), so the aggregation logic is tested without depending on the generated build/ folder.
One optional integration test runs the pack on the real ``build/`` artefacts when they are present.
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
                module_passport("AGRI_SOY", "0.1.0", "agri", "f" * 64)]
    (build / "registry").mkdir(parents=True, exist_ok=True)
    (build / "registry" / "modules.jsonl").write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in registry) + "\n",
        encoding="utf-8")
    write_json(build / "api" / "index.json", {
        "schema": "amfe.forecast_api.v2", "api_version": "v2", "base_path": "/v1",
        "status": "RESEARCH_ONLY", "generated_at": "2026-11-16T00:00:00Z",
        "counts": {"modules": 2, "scenarios": 1, "forecasts": 1, "market_modules": 1},
        "endpoints": [
            {"method": "GET", "path": "/v1/modules", "description": "module passports"},
            {"method": "GET", "path": "/v1/scenarios/{scenario_id}", "description": "scenario"},
            {"method": "GET", "path": "/v1/forecast/{target}", "description": "forecast"},
        ],
        "modules": [
            {"module_id": "oil.brent", "version": "0.2.0", "status": "RESEARCH_ONLY",
             "kind": "market"},
            {"module_id": "AGRI_SOY", "version": "0.1.0", "status": "RESEARCH_ONLY",
             "kind": "agri"},
        ],
        "scenarios": [{"id": "oil.brent/hormuz_closed_6m", "module": "oil.brent",
                       "horizon_months": 6,
                       "file": "scenarios/oil.brent__hormuz_closed_6m.json"}],
        "forecasts": [{"target": "oil_brent", "module_id": "oil.brent",
                       "module_version": "0.2.0", "file": "forecasts/oil_brent.json"}],
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
    return root


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
        values = {"inputs_root": self.build, "generated_at": FIXED, "root": self.root}
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
        self.assertEqual(module["count"], 2)  # two latest modules, history collapsed
        self.assertEqual(module["registry_sha256"], sha(self.build / "registry" / "modules.jsonl"))
        latest = {entry["module_id"]: entry for entry in registry}
        self.assertEqual(latest["oil.brent"]["version"], "0.2.0")
        for item in module["items"]:
            self.assertEqual(item["status"], "RESEARCH_ONLY", item)
            self.assertRegex(item["code_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(item["version"], latest[item["module_id"]]["version"])
        self.assertEqual([item["module_id"] for item in module["items"]],
                         ["AGRI_SOY", "oil.brent"])

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
        self.assertEqual(section["forecast"]["target"], forecast["target"])
        self.assertEqual(section["forecast"]["price_peak"],
                         forecast["envelope"]["price_peak"])
        self.assertEqual(section["forecast"]["result_digest"], forecast["result_digest"])

    def test_sources_carry_real_digests(self):
        self.make_pack()
        document = self.machine()
        self.assertEqual(len(document["sources"]), 5)
        for item in document["sources"]:
            path = self.root / item["path"]
            self.assertTrue(path.is_file(), item["path"])
            self.assertEqual(item["sha256"], sha(path), item["path"])
            self.assertEqual(item["bytes"], path.stat().st_size)


class OfficeDocuments(FixtureCase):
    def test_xlsx_sheets_and_numbers_match_the_json(self):
        from openpyxl import load_workbook

        self.make_pack()
        document = self.machine()
        workbook = load_workbook(self.out / "02_Numbers.xlsx", data_only=True)
        for sheet in ("Summary", "Candidate", "Modules", "Scenario_Summary",
                      "Scenario_Path", "Scenario_Sensitivity", "API", "Sources"):
            self.assertIn(sheet, workbook.sheetnames, sheet)
        summary_values = {row[0]: row[1] for row in
                          workbook["Summary"].iter_rows(values_only=True) if row[0]}
        self.assertEqual(float(summary_values["Точечный прогноз"]), document["summary"]["point"])
        self.assertEqual(float(summary_values["Пик сценария"]),
                         document["summary"]["scenario_peak"])
        self.assertEqual(int(summary_values["Модулей"]), document["summary"]["modules_total"])
        module_rows = list(workbook["Modules"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(module_rows), len(document["modules"]["items"]))
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
        self.assertIn("не прогноз", text.lower())
        self.assertIn(str(document["summary"]["modules_total"]), text)

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
        self.assertIn(str(document["summary"]["modules_total"]), html)

    def test_readme_lists_files_status_and_limitations(self):
        self.make_pack()
        text = (self.out / "README.md").read_text(encoding="utf-8")
        for token in ("Client Pack v2", "RESEARCH_ONLY", "CANDIDATE", "C4", "C6",
                      "01_Executive_Summary.docx", "07_Reproducibility.json",
                      "client_pack_v2.py"):
            self.assertIn(token, text, token)


class Reproducibility(FixtureCase):
    def test_manifest_digests_match_the_files(self):
        self.make_pack()
        manifest = read_json(self.out / "07_Reproducibility.json")
        self.assertEqual(manifest["schema"], pack.REPRO_SCHEMA)
        self.assertEqual(manifest["status"], "RESEARCH_ONLY")
        self.assertEqual(manifest["generated_at"], FIXED)
        self.assertIn("client_pack_v2.py", manifest["command"])
        self.assertEqual(len(manifest["inputs"]), 5)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
