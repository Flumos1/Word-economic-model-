"""Market client pack (tools/market_client_pack.py): real oil numbers, formats, offline, fail-closed."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import market_client_pack  # noqa: E402
from amfe.markets import OIL_MODULE  # noqa: E402

FIXED = "2026-11-16T10:00:00Z"
SCENARIO = "hormuz_closed_6m"


def docx_text(path: Path) -> str:
    from docx import Document
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class MarketClientPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "market_hormuz_6m"
        cls.summary = market_client_pack.build_pack(
            cls.out, scenario_id=SCENARIO, client="Demo Bank", generated_at=FIXED,
            make_pdf=True)
        cls.result = OIL_MODULE.run(SCENARIO)
        cls.machine = read_json(cls.out / "04_Machine_Readable.json")
        cls.repro = read_json(cls.out / "07_Reproducibility.json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pack_has_all_formats(self):
        self.assertTrue(self.summary["ok"])
        names = set(self.summary["files"])
        for required in ("01_Executive_Summary.docx", "02_Scenario_Report.docx",
                         "03_Price_Sensitivity.xlsx", "04_Machine_Readable.json",
                         "05_Dashboard.html", "06_Methodology_Appendix.docx",
                         "07_Reproducibility.json", "08_Licence_and_Disclaimer.docx",
                         "10_Module_Passports.json", "README.md"):
            self.assertIn(required, names)
        if self.summary["pdf"]:
            for required in ("01_Executive_Summary.pdf", "02_Scenario_Report.pdf",
                             "06_Methodology_Appendix.pdf", "08_Licence_and_Disclaimer.pdf"):
                self.assertIn(required, names)
        for name in names:
            self.assertGreater((self.out / name).stat().st_size, 100, name)

    def test_executive_summary_has_real_numbers(self):
        text = docx_text(self.out / "01_Executive_Summary.docx")
        peak = self.result.summary["price_peak"]
        low = self.result.summary["brent_range_usd_per_bbl"]["low"]
        high = self.result.summary["brent_range_usd_per_bbl"]["high"]
        for token in ("Demo Bank", SCENARIO, "Research", "Brent",
                      f"{peak:,.2f}".replace(",", " "),
                      f"{low:,.2f}".replace(",", " "),
                      f"{high:,.2f}".replace(",", " ")):
            self.assertIn(str(token), text)
        self.assertIn("не прогноз", text)

    def test_scenario_report_lists_path_and_sensitivity(self):
        text = docx_text(self.out / "02_Scenario_Report.docx")
        for month in ("M1", "M6"):
            self.assertIn(month, text)
        for variant in ("elasticity_more_elastic", "bypass_low", "risk_premium_high"):
            self.assertIn(variant, text)
        for check in self.result.checks:
            self.assertIn(check, text)

    def test_excel_sheets_and_numbers(self):
        from openpyxl import load_workbook
        workbook = load_workbook(self.out / "03_Price_Sensitivity.xlsx")
        for sheet in ("Сводка", "Цена по месяцам", "Чувствительность", "Допущения", "Проверки"):
            self.assertIn(sheet, workbook.sheetnames)
        summary = {row[0].value: row[1].value for row in workbook["Сводка"].iter_rows(min_row=2)}
        self.assertAlmostEqual(float(summary["Центральный пик"]),
                               self.result.summary["price_peak"], places=6)
        path_rows = list(workbook["Цена по месяцам"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(path_rows), len(self.result.price_path))
        self.assertAlmostEqual(float(path_rows[0][1]), self.result.price_path[0], places=6)
        sensitivity = list(workbook["Чувствительность"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(sensitivity), len(self.result.sensitivity))
        assumptions = list(workbook["Допущения"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(assumptions), len(self.result.assumptions))

    def test_machine_json_matches_market_result(self):
        self.assertEqual(self.machine["schema"], market_client_pack.SCHEMA)
        self.assertEqual(self.machine["status"], "RESEARCH_ONLY")
        stored = self.machine["market_result"]
        self.assertEqual(stored["digest"], self.result.digest())
        self.assertEqual(stored["summary"], self.result.as_dict()["summary"])
        self.assertEqual(stored["price_path"], list(self.result.price_path))
        self.assertEqual(len(stored["assumptions"]), len(self.result.assumptions))
        self.assertTrue(self.machine["licence"]["research_only"])
        self.assertNotIn('"Z"', json.dumps(self.machine))
        self.assertNotIn('"observations":', json.dumps(self.machine))

    def test_dashboard_is_static_and_uses_real_range(self):
        html = (self.out / "05_Dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        peak = self.result.summary["brent_range_usd_per_bbl"]
        self.assertIn(f"{peak['low']:,.2f}".replace(",", " "), html)
        self.assertIn("research", html.lower())
        self.assertIn(SCENARIO, html)

    def test_reproducibility_manifest(self):
        self.assertEqual(self.repro["schema"], market_client_pack.REPRO_SCHEMA)
        self.assertEqual(self.repro["result_digest"], self.result.digest())
        self.assertEqual(self.repro["module"]["scenario_id"], SCENARIO)
        self.assertEqual(self.repro["module"]["engine"], self.result.meta["engine"])
        self.assertIn("04_Machine_Readable.json", self.repro["deterministic_artifacts"])
        self.assertIn("05_Dashboard.html", self.repro["deterministic_artifacts"])
        for name, digest in self.repro["deterministic_artifacts"].items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), digest)
        self.assertIn("market_client_pack.py", self.repro["command"])
        # PDFs are produced only when LibreOffice is available (optional), so the lower
        # bound is 9 without them and 12 with them.
        minimum = 12 if self.summary.get("pdf") else 9
        self.assertGreaterEqual(self.repro["file_count"], minimum)

    def test_methodology_and_licence_documents(self):
        methodology = docx_text(self.out / "06_Methodology_Appendix.docx")
        for token in ("Replace-with", "Диапазон", "Ограничения", "assumption"):
            self.assertIn(token, methodology)
        licence = docx_text(self.out / "08_Licence_and_Disclaimer.docx")
        for token in ("RESEARCH_ONLY", "не инвестиционная", "не прогноз", "сырых"):
            self.assertIn(token, licence)
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        self.assertIn(SCENARIO, readme)
        self.assertIn("research", readme.lower())

    def test_numbers_are_not_invented(self):
        numbers = self.summary["numbers"]
        self.assertEqual(numbers["price_peak"], self.result.summary["price_peak"])
        self.assertEqual(numbers["price_mean"], self.result.summary["price_mean"])
        self.assertEqual(numbers["range_peak"],
                         self.result.summary["brent_range_usd_per_bbl"])
        self.assertEqual(numbers["range_mean"],
                         self.result.summary["brent_range_mean_usd_per_bbl"])
        self.assertEqual(numbers["units"], self.result.units)
        self.assertEqual(numbers["assumptions"], len(self.result.assumptions))
        self.assertTrue(numbers["checks_pass"])

    def test_determinism_of_json_html_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            first = market_client_pack.build_pack(out, scenario_id=SCENARIO, client="Demo Bank",
                                                  generated_at=FIXED, make_pdf=False)
            hashes = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                      for name in ("04_Machine_Readable.json", "05_Dashboard.html",
                                   "07_Reproducibility.json")}
            market_client_pack.build_pack(out, scenario_id=SCENARIO, client="Demo Bank",
                                          generated_at=FIXED, make_pdf=False)
            for name, digest in hashes.items():
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(),
                                 digest, name)
            self.assertTrue(first["ok"])

    def test_fail_closed_unknown_scenario_and_empty_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(market_client_pack.MarketPackError):
                market_client_pack.build_pack(Path(tmp) / "pack", scenario_id="nope",
                                              generated_at=FIXED, make_pdf=False)
            self.assertFalse((Path(tmp) / "pack" / "01_Executive_Summary.docx").exists())
            with self.assertRaises(market_client_pack.MarketPackError):
                market_client_pack.build_pack(Path(tmp) / "pack2", client="  ",
                                              generated_at=FIXED, make_pdf=False)

    def test_module_passport_file(self):
        document = read_json(self.out / "10_Module_Passports.json")
        self.assertEqual(document["schema"], market_client_pack.PASSPORTS_SCHEMA)
        self.assertEqual(document["kind"], "module_passports")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["module_ref"], "oil.brent@0.1.0")
        module = document["module"]
        for key in ("module_id", "version", "status", "kind", "limitations", "code_sha256"):
            self.assertIn(key, module)
        self.assertEqual(module["module_id"], self.result.module_id)
        self.assertEqual(module["version"], "0.1.0")
        self.assertEqual(module["status"], "RESEARCH_ONLY")
        self.assertEqual(len(module["code_sha256"]), 64)
        self.assertTrue(module["limitations"])
        registry = market_client_pack.load_module_passports()
        self.assertEqual(len(document["all_modules"]), len(registry["export"]["modules"]))
        self.assertEqual([item["module_id"] for item in document["all_modules"]],
                         [item["module_id"] for item in registry["export"]["modules"]])
        self.assertEqual(document["registry"]["sha256"],
                         market_client_pack._sha256(market_client_pack.DEFAULT_REGISTRY))
        self.assertIn("MarketResult", document["numbers_source"])
        self.assertFalse(document["policy"].get("no_admission") is False)
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        self.assertIn("10_Module_Passports", readme)
        self.assertIn("oil.brent@0.1.0", readme)

    def test_fail_closed_missing_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            missing = Path(tmp) / "no_registry.jsonl"
            with self.assertRaises(market_client_pack.MarketPackError):
                market_client_pack.build_pack(out, generated_at=FIXED, make_pdf=False,
                                              registry_path=missing)
            self.assertFalse(out.exists())
            with tempfile.TemporaryDirectory() as repo_tmp:
                empty = Path(repo_tmp) / "empty.jsonl"
                empty.write_text("", encoding="utf-8")
                with self.assertRaises(market_client_pack.MarketPackError):
                    market_client_pack.build_pack(Path(repo_tmp) / "pack2", generated_at=FIXED,
                                                  make_pdf=False, registry_path=empty)

    def test_cli_help_and_no_pdf_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "market_client_pack.py"),
                 "--out", str(out), "--generated-at", FIXED, "--no-pdf", "--json"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertFalse(summary["pdf"])
            self.assertTrue((out / "04_Machine_Readable.json").is_file())
            help_run = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "market_client_pack.py"), "--help"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(help_run.returncode, 0)
            self.assertIn("--scenario", help_run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
