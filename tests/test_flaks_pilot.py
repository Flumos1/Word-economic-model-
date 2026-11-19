"""Tests for the FLAKS pilot intake (tools/flaks_pilot.py, docs/189).

The suite does not trust the report: it re-reads the templates, mutates them field by field, and
checks the fail-closed branches, the quality report, unit conversion, anonymization (no source
identifier may survive in JSON/Markdown/DOCX or in the DOCX XML), deterministic bytes, manifest
hashes and the integration with the existing scoring/TCO/budget engine.

All tests are offline and write only to temporary directories.
"""
import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import flaks_pilot as fpl  # noqa: E402
import flaks_procurement as fp  # noqa: E402

TEMPLATES = ROOT / "pilot" / "flaks"
SUPPLIERS_TEMPLATE = TEMPLATES / "suppliers_template.csv"
CONTRACTS_TEMPLATE = TEMPLATES / "contracts_template.csv"
GENERATED_AT = "2026-11-19T00:00:00Z"


# ---------------------------------------------------------------------------------------- helpers
def _rows(path: Path) -> list:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _supplier_rows() -> list:
    return _rows(SUPPLIERS_TEMPLATE)


def _contract_rows() -> list:
    return _rows(CONTRACTS_TEMPLATE)


def _write_table(path: Path, columns: tuple, rows: list) -> Path:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})
    return path


def _paths(tmp: str, suppliers: list | None = None, contracts: list | None = None) -> tuple:
    suppliers = _supplier_rows() if suppliers is None else suppliers
    contracts = _contract_rows() if contracts is None else contracts
    suppliers_path = _write_table(Path(tmp) / "suppliers.csv", fpl.SUPPLIER_COLUMNS, suppliers)
    contracts_path = _write_table(Path(tmp) / "contracts.csv", fpl.CONTRACT_COLUMNS, contracts)
    return suppliers_path, contracts_path


def _build(suppliers: list | None = None, contracts: list | None = None, **kwargs) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        suppliers_path, contracts_path = _paths(tmp, suppliers, contracts)
        return fpl.build_pilot(suppliers_path, contracts_path, generated_at=GENERATED_AT, **kwargs)


def _run_cli(argv) -> tuple:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = fpl.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        chunks = [archive.read(name).decode("utf-8", "ignore")
                  for name in archive.namelist()
                  if name.startswith("word/") and name.endswith(".xml")]
    return " ".join(chunks)


class TemplateTest(unittest.TestCase):
    def test_templates_match_the_declared_schema(self):
        supplier_header = _rows(SUPPLIERS_TEMPLATE)[0].keys()
        contract_header = _rows(CONTRACTS_TEMPLATE)[0].keys()
        self.assertEqual(tuple(supplier_header), fpl.SUPPLIER_COLUMNS)
        self.assertEqual(tuple(contract_header), fpl.CONTRACT_COLUMNS)
        self.assertEqual(len(_supplier_rows()), 4)
        self.assertEqual(len(_contract_rows()), 4)

    def test_real_templates_pass_intake_and_quality(self):
        intake = fpl.load_intake(SUPPLIERS_TEMPLATE, CONTRACTS_TEMPLATE)
        self.assertEqual(intake["schema"], fpl.INTAKE_SCHEMA)
        self.assertEqual(len(intake["suppliers"]), 4)
        self.assertEqual(len(intake["contracts"]), 4)
        self.assertEqual(intake["quality"]["status"], "OK")
        self.assertEqual(intake["quality"]["errors"], 0)
        self.assertEqual(intake["quality"]["outliers"], [])
        self.assertEqual(sum(intake["quality"]["missing"]["suppliers"].values()), 0)

    def test_templates_run_end_to_end(self):
        outcome = _build()
        report = outcome["report"]
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["admission"])
        self.assertEqual(report["schema"], fpl.SCHEMA)
        self.assertEqual(report["intake"]["suppliers"]["rows"], 4)
        self.assertEqual(report["procurement"]["dataset"]["counts"]["categories"], 2)
        self.assertEqual(report["procurement"]["dataset"]["source_class"], "anonymized_fixture")
        self.assertAlmostEqual(report["procurement"]["budget"]["base_total_eur"], 1_074_500.0,
                               delta=1e-6)
        self.assertTrue(report["checks"]["model_checks_passed"])

    def test_xlsx_intake_matches_csv(self):
        try:
            from openpyxl import Workbook
        except ImportError:  # pragma: no cover
            self.skipTest("openpyxl недоступен")
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Workbook()
            sheet = workbook.active
            numeric = {"annual_volume", "unit_price", "freight_per_unit", "lead_time_days",
                       "otif", "defect_ppm", "audit_score", "risk_financial", "risk_supply",
                       "risk_compliance", "cpi_share", "ppi_share", "fx_share", "cap_pct",
                       "payment_days"}
            sheet.append(list(fpl.SUPPLIER_COLUMNS))
            for row in _supplier_rows():
                sheet.append([float(row[key]) if key in numeric else row[key]
                              for key in fpl.SUPPLIER_COLUMNS])
            suppliers_path = Path(tmp) / "suppliers.xlsx"
            workbook.save(suppliers_path)
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(list(fpl.CONTRACT_COLUMNS))
            for row in _contract_rows():
                sheet.append([row[key].upper() == "TRUE" if key == "floor_zero"
                              else float(row[key]) if key in numeric else row[key]
                              for key in fpl.CONTRACT_COLUMNS])
            contracts_path = Path(tmp) / "contracts.xlsx"
            workbook.save(contracts_path)
            xlsx = fpl.build_pilot(suppliers_path, contracts_path, generated_at=GENERATED_AT)
        csv_result = _build()
        self.assertEqual(xlsx["report"]["procurement"]["scoring"],
                         csv_result["report"]["procurement"]["scoring"])
        self.assertEqual(xlsx["report"]["procurement"]["budget"],
                         csv_result["report"]["procurement"]["budget"])
        self.assertEqual(xlsx["report"]["intake"]["suppliers"]["format"], "xlsx")


class ValidationTest(unittest.TestCase):
    def _assert_refused(self, suppliers=None, contracts=None):
        suppliers = _supplier_rows() if suppliers is None else suppliers
        contracts = _contract_rows() if contracts is None else contracts
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, suppliers, contracts)
            with self.assertRaises(fpl.IntakeError):
                fpl.load_intake(suppliers_path, contracts_path)

    def test_missing_required_column_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppliers = _supplier_rows()
            columns = tuple(name for name in fpl.SUPPLIER_COLUMNS if name != "unit_price")
            for row in suppliers:
                row.pop("unit_price")
            path = _write_table(Path(tmp) / "suppliers.csv", columns, suppliers)
            with self.assertRaises(fpl.IntakeError) as context:
                fpl.load_intake(path, CONTRACTS_TEMPLATE)
            self.assertIn("unit_price", str(context.exception))

    def test_unknown_column_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppliers = _supplier_rows()
            for row in suppliers:
                row["comment"] = "лишняя колонка"
            path = _write_table(Path(tmp) / "suppliers.csv",
                                fpl.SUPPLIER_COLUMNS + ("comment",), suppliers)
            with self.assertRaises(fpl.IntakeError) as context:
                fpl.load_intake(path, CONTRACTS_TEMPLATE)
            self.assertIn("comment", str(context.exception))

    def test_bad_number_reports_row_and_column(self):
        suppliers = _supplier_rows()
        suppliers[0]["unit_price"] = "дорого"
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            with self.assertRaises(fpl.IntakeError) as context:
                fpl.load_intake(suppliers_path, contracts_path)
        columns = [item["column"] for item in context.exception.issues]
        self.assertIn("unit_price", columns)
        self.assertTrue(any(item["row"] == 2 for item in context.exception.issues))

    def test_duplicate_supplier_id_is_refused(self):
        suppliers = _supplier_rows()
        suppliers[1]["supplier_id"] = suppliers[0]["supplier_id"]
        self._assert_refused(suppliers=suppliers)

    def test_duplicate_contract_id_is_refused(self):
        contracts = _contract_rows()
        contracts[1]["contract_id"] = contracts[0]["contract_id"]
        self._assert_refused(contracts=contracts)

    def test_orphan_contract_is_refused(self):
        contracts = _contract_rows()
        contracts[0]["supplier_id"] = "NO-SUCH-SUPPLIER"
        self._assert_refused(contracts=contracts)

    def test_supplier_without_contract_is_refused(self):
        self._assert_refused(contracts=_contract_rows()[:3])

    def test_bad_status_and_boolean_are_refused(self):
        contracts = _contract_rows()
        contracts[0]["status"] = "maybe"
        self._assert_refused(contracts=[dict(item) for item in contracts])
        contracts = _contract_rows()
        contracts[0]["floor_zero"] = "perhaps"
        self._assert_refused(contracts=contracts)

    def test_share_sum_and_fx_currency_are_refused(self):
        contracts = _contract_rows()
        contracts[0]["cpi_share"] = "0.8"
        contracts[0]["ppi_share"] = "0.8"
        self._assert_refused(contracts=contracts)
        contracts = _contract_rows()
        contracts[0]["fx_share"] = "0.2"  # supplier 001 is EUR
        self._assert_refused(contracts=contracts)

    def test_single_supplier_category_is_refused(self):
        suppliers = [row for row in _supplier_rows() if row["supplier_id"] != "EX-SUP-003"]
        contracts = [row for row in _contract_rows() if row["supplier_id"] != "EX-SUP-003"]
        with self.assertRaises(fpl.IntakeError):
            _build(suppliers, contracts)

    def test_category_without_active_contract_is_refused(self):
        contracts = _contract_rows()
        for row in contracts:
            if row["supplier_id"] in ("EX-SUP-001", "EX-SUP-002"):
                row["status"] = "candidate"
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, contracts=contracts)
            with self.assertRaises(fpl.IntakeError):
                fpl.load_intake(suppliers_path, contracts_path)

    def test_unknown_scenario_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp)
            with self.assertRaises(fpl.PilotError):
                fpl.build_pilot(suppliers_path, contracts_path, scenario_id="hyperinflation",
                                generated_at=GENERATED_AT)


class QualityTest(unittest.TestCase):
    def test_defaults_and_missing_are_reported(self):
        suppliers = _supplier_rows()
        suppliers[1]["name"] = ""
        suppliers[1]["currency"] = ""
        suppliers[1]["unit"] = ""
        suppliers[1]["freight_per_unit"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            intake = fpl.load_intake(suppliers_path, contracts_path)
        quality = intake["quality"]
        self.assertEqual(quality["missing"]["suppliers"]["name"], 1)
        self.assertEqual(quality["missing"]["suppliers"]["currency"], 1)
        self.assertEqual(quality["missing"]["suppliers"]["unit"], 1)
        self.assertEqual(quality["missing"]["suppliers"]["freight_per_unit"], 1)
        self.assertEqual(len(quality["defaults_applied"]), 4)
        self.assertEqual(quality["units"]["canonical"].get("кг"), 4)
        self.assertEqual(intake["suppliers"][1]["currency"], "EUR")
        self.assertEqual(intake["suppliers"][1]["unit_price"], 1.03)

    def test_tonnes_are_converted_to_kilograms(self):
        suppliers = _supplier_rows()
        suppliers[0]["unit"] = "т"
        suppliers[0]["annual_volume"] = "500"
        suppliers[0]["unit_price"] = "950"
        suppliers[0]["freight_per_unit"] = "30"
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            intake = fpl.load_intake(suppliers_path, contracts_path)
        record = next(item for item in intake["suppliers"] if item["_source_id"] == "EX-SUP-001")
        self.assertAlmostEqual(record["annual_volume"], 500_000.0, delta=1e-9)
        self.assertAlmostEqual(record["unit_price"], 0.95, delta=1e-12)
        self.assertAlmostEqual(record["freight_per_unit"], 0.03, delta=1e-12)
        self.assertEqual(len(intake["quality"]["units"]["conversions"]), 1)
        self.assertEqual(intake["quality"]["units"]["conversions"][0]["factor"], 1000.0)
        outcome = _build(suppliers)
        self.assertAlmostEqual(outcome["report"]["procurement"]["budget"]["base_total_eur"],
                               1_074_500.0, delta=1e-6)

    def test_incompatible_unit_is_refused(self):
        suppliers = _supplier_rows()
        suppliers[0]["category"] = "contract_logistics"  # unit stays кг
        with tempfile.TemporaryDirectory() as tmp:
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            with self.assertRaises(fpl.IntakeError) as context:
                fpl.load_intake(suppliers_path, contracts_path)
        self.assertTrue(any(item["column"] == "unit" for item in context.exception.issues))

    def test_duplicate_rows_are_flagged_not_blocked(self):
        suppliers = _supplier_rows()
        duplicate = dict(suppliers[0])
        duplicate["supplier_id"] = "EX-SUP-099"
        duplicate["contract_id"] = "EX-CT-099"  # ignored by the supplier writer
        suppliers.append(duplicate)
        contracts = _contract_rows()
        contracts.append({"contract_id": "EX-CT-099", "supplier_id": "EX-SUP-099",
                          "status": "active", "cpi_share": "0.4", "ppi_share": "0.4",
                          "fx_share": "0", "cap_pct": "6", "floor_zero": "TRUE",
                          "payment_days": "45"})
        outcome = _build(suppliers, contracts)
        codes = [item["code"] for item in outcome["report"]["quality"]["warnings"]]
        self.assertIn("DUPLICATE_ROW", codes)

    def test_outliers_use_iqr_and_do_not_block(self):
        records = []
        for position, price in enumerate((1.00, 1.02, 1.04, 1.06, 1.08, 1.10, 50.0), start=2):
            records.append({"category": "metal_rolled", "_row": position,
                            "unit_price": price, "annual_volume": 1000.0,
                            "freight_per_unit": 0.02, "lead_time_days": 10.0})
        found = fpl._outliers(records)
        self.assertTrue(any(item["column"] == "unit_price" and item["row"] == 8
                            for item in found))
        self.assertEqual([item["column"] for item in found], ["unit_price"])


class AnonymizationTest(unittest.TestCase):
    def test_source_identifiers_do_not_survive_in_any_artifact(self):
        outcome = _build()
        payload = json.dumps(outcome["report"], ensure_ascii=False)
        markdown = outcome["markdown"]
        docx_text = fpl._docx_text(outcome["docx"])
        for label, text in (("json", payload), ("markdown", markdown), ("docx", docx_text)):
            for source in ("EX-SUP-001", "EX-SUP-002", "EX-CT-001", "Stal Polska",
                           "Stahlwerk Ost", "Anadolu Kimya"):
                self.assertNotIn(source, text, f"{label}: утёк {source}")
        self.assertIn("ANON-SUP-001", payload)
        self.assertIn("Анонимный поставщик 001", payload)
        self.assertEqual(outcome["report"]["anonymization"]["leak_scan"], "PASS")
        self.assertEqual(fpl.leak_scan({"json": payload},
                                       {"ex-sup-001"}), [])

    def test_pseudonyms_are_stable_when_row_order_changes(self):
        suppliers = _supplier_rows()
        contracts = _contract_rows()
        shuffled_suppliers = [suppliers[2], suppliers[0], suppliers[3], suppliers[1]]
        shuffled_contracts = [contracts[2], contracts[0], contracts[3], contracts[1]]
        first = _build()
        second = _build(shuffled_suppliers, shuffled_contracts)
        self.assertEqual(first["report"]["procurement"]["scoring"],
                         second["report"]["procurement"]["scoring"])
        self.assertEqual(first["report"]["procurement"]["budget"],
                         second["report"]["procurement"]["budget"])
        self.assertEqual(first["report"]["anonymization"]["strategy"],
                         second["report"]["anonymization"]["strategy"])

    def test_leak_scanner_detects_a_planted_identifier(self):
        problems = fpl.leak_scan({"json": "supplier EX-SUP-001 appears here"},
                                 {"ex-sup-001"})
        self.assertTrue(problems)
        self.assertNotIn("EX-SUP-001", " ".join(problems))


class FailClosedCliTest(unittest.TestCase):
    def test_cli_refuses_bad_data_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppliers = _supplier_rows()
            suppliers[1]["supplier_id"] = suppliers[0]["supplier_id"]
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            out = Path(tmp) / "out"
            code, _, stderr = _run_cli(["--suppliers", str(suppliers_path),
                                        "--contracts", str(contracts_path),
                                        "--out", str(out), "--generated-at", GENERATED_AT])
            self.assertEqual(code, 2)
            self.assertIn("BLOCKED_INPUT", stderr)
            self.assertFalse(out.exists())

    def test_check_mode_validates_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "never"
            code, stdout, _ = _run_cli(["--suppliers", str(SUPPLIERS_TEMPLATE),
                                        "--contracts", str(CONTRACTS_TEMPLATE),
                                        "--check", "--out", str(out)])
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "CHECK_OK")
            self.assertEqual(payload["quality_status"], "OK")
            self.assertFalse(out.exists())

    def test_check_mode_fails_on_bad_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppliers = _supplier_rows()
            suppliers[0]["unit_price"] = "-5"
            suppliers_path, contracts_path = _paths(tmp, suppliers)
            code, _, stderr = _run_cli(["--suppliers", str(suppliers_path),
                                        "--contracts", str(contracts_path), "--check"])
            self.assertEqual(code, 2)
            self.assertIn("BLOCKED_INPUT", stderr)

    def test_unknown_scenario_exits_two(self):
        code, _, stderr = _run_cli(["--suppliers", str(SUPPLIERS_TEMPLATE),
                                    "--contracts", str(CONTRACTS_TEMPLATE),
                                    "--scenario", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED_INPUT", stderr)


class ArtifactsTest(unittest.TestCase):
    def _run_into(self, tmp: Path) -> tuple:
        code, stdout, stderr = _run_cli(["--suppliers", str(SUPPLIERS_TEMPLATE),
                                         "--contracts", str(CONTRACTS_TEMPLATE),
                                         "--scenario", "base", "--out", str(tmp),
                                         "--anonymize", "--generated-at", GENERATED_AT])
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout), Path(tmp) / "base"

    def test_manifest_hashes_match_every_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, target = self._run_into(Path(tmp))
            manifest = json.loads((target / "flaks_pilot_manifest.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], fpl.MANIFEST_SCHEMA)
            self.assertTrue(manifest["anonymized"])
            self.assertEqual(len(manifest["files"]), 3)
            self.assertNotIn("name", manifest["inputs"]["suppliers"])
            for item in manifest["files"]:
                path = target / item["name"]
                self.assertTrue(path.exists())
                self.assertEqual(fpl._sha256_file(path), item["sha256"])
                self.assertEqual(path.stat().st_size, item["bytes"])
            self.assertEqual({item["name"] for item in manifest["files"]},
                             {"flaks_pilot.json", "flaks_pilot.md", "flaks_pilot_cfo.docx"})
            self.assertEqual(summary["leak_scan"], "PASS")

    def test_json_markdown_and_docx_are_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self._run_into(Path(first))
            self._run_into(Path(second))
            for name in ("flaks_pilot.json", "flaks_pilot.md", "flaks_pilot_cfo.docx",
                         "flaks_pilot_manifest.json"):
                self.assertEqual((Path(first) / "base" / name).read_bytes(),
                                 (Path(second) / "base" / name).read_bytes(), name)

    def test_docx_is_valid_and_contains_cfo_sections(self):
        try:
            import docx  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("python-docx недоступен")
        with tempfile.TemporaryDirectory() as tmp:
            _, target = self._run_into(Path(tmp))
            path = target / "flaks_pilot_cfo.docx"
            with zipfile.ZipFile(path) as archive:
                self.assertIn("word/document.xml", archive.namelist())
                self.assertIn("docProps/core.xml", archive.namelist())
            text = _docx_text(path)
            for heading in ("Резюме для CFO", "Качество данных", "Скоринг поставщиков",
                            "TCO", "Бюджет закупок", "Индексация контрактов",
                            "Проверки и происхождение", "Ограничения",
                            "Воспроизводимость"):
                self.assertIn(heading, text)
            self.assertIn("RESEARCH_ONLY", text)
            self.assertNotIn("EX-SUP", text)

    def test_cli_json_prints_the_full_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, _ = _run_cli(["--suppliers", str(SUPPLIERS_TEMPLATE),
                                        "--contracts", str(CONTRACTS_TEMPLATE),
                                        "--out", str(Path(tmp) / "out"), "--json",
                                        "--generated-at", GENERATED_AT])
            self.assertEqual(code, 0)
            report = json.loads(stdout)
            self.assertEqual(report["schema"], fpl.SCHEMA)
            self.assertEqual(report["scenario"], "base")
            self.assertEqual(len(report["procurement"]["scoring"]["categories"]), 2)
            self.assertFalse(report["admission"])

    def test_base_budget_equals_active_contracts_and_checks_pass(self):
        report = _build()["report"]
        expected = 500_000 * 0.95 + 400_000 * 1.03 + 150_000 * 1.25
        budget = report["procurement"]["budget"]
        self.assertAlmostEqual(budget["base_total_eur"], expected, delta=1e-6)
        self.assertAlmostEqual(budget["focus_delta_eur"], 0.0, delta=1e-6)
        self.assertTrue(report["checks"]["model_checks_passed"])
        markdown = _build()["markdown"]
        self.assertNotIn("| FAIL |", markdown)
        self.assertIn("RESEARCH_ONLY", markdown)


if __name__ == "__main__":
    unittest.main()
