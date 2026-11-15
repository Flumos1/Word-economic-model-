"""Client pack (tools/client_pack.py): real scenario report -> DOCX/PDF/XLSX/PPTX/HTML/JSON."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import client_pack  # noqa: E402

FIXED = "2026-11-15T09:00:00Z"


def sample_report() -> dict:
    return {
        "schema": "amfe.world.scenario_report.v1",
        "scenario": {"id": "energy_plus50", "type": "energy",
                     "description": "рост конечного спроса на 50% в энергетике",
                     "parameters": {"share": -0.5, "industries": ["ENERGY"]}},
        "baseline": {"total_output": 1000.0, "total_final_demand": 500.0,
                     "total_value_added": 500.0, "productive": True,
                     "spectral_radius": 0.5, "max_abs_material_residual": 1e-9},
        "result": {"total_output": 1010.0, "total_delta_output": 10.0,
                   "relative_delta_output": 0.01, "max_abs_delta_output": 6.0,
                   "output_multiplier_vs_demand": 2.0, "units": "USD_mn"},
        "checks": {"validation_pass": True, "baseline_productive": True,
                   "hawkins_simon_pass": True, "scenario_material_residual": 1e-9},
        "impacts": {"by_country": {"AA": 6.0, "BB": -2.0, "CC": 4.0},
                    "by_industry": {"ENERGY": 5.0, "SERV": 3.0, "AGRI": 2.0}},
        "dataset": {"dataset_id": "figaro_2019", "schema": "amfe.mrio.dataset.v1.1",
                    "countries": 50, "industries": 64, "sectors": 3200,
                    "units": "USD_mn", "sha256": "a" * 64, "bytes": 1000},
        "sfc": {"checks": {"sfc_pass": True}, "assumptions": ["A_c = FD_c"]},
        "limitations": ["сравнительная статика"],
    }


def write_report(directory: Path, report: dict | None = None) -> Path:
    path = Path(directory) / "energy_plus50.json"
    path.write_text(json.dumps(report or sample_report(), ensure_ascii=False), encoding="utf-8")
    return path


def docx_text(path: Path) -> str:
    from docx import Document
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


class ClientPackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.report = write_report(self.base)
        self.out = self.base / "pack"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **overrides):
        options = dict(report_path=self.report, client="Demo Bank", out_dir=self.out,
                       generated_at=FIXED, dashboard=None, make_pdf=False)
        options.update(overrides)
        return client_pack.build_pack(**options)

    def test_pack_has_all_formats(self):
        result = self.build()
        names = set(result["files"])
        for required in ("01_Executive_Summary.docx", "02_Scenario_Report.docx",
                         "03_Analyst_Pack.xlsx", "04_Machine_Readable.json",
                         "05_Dashboard.html", "06_Methodology_Appendix.docx",
                         "07_Reproducibility.json", "08_Licence_and_Disclaimer.docx", "README.md"):
            self.assertIn(required, names)
        self.assertTrue(any(name.startswith("09_Board_Deck") for name in names))
        for name in names:
            self.assertGreater((self.out / name).stat().st_size, 100, name)

    def test_executive_summary_content(self):
        self.build()
        text = docx_text(self.out / "01_Executive_Summary.docx")
        for token in ("Demo Bank", "energy_plus50", "10.00", "1.00%", "research"):
            self.assertIn(token, text)

    def test_scenario_tables_content(self):
        self.build()
        text = docx_text(self.out / "02_Scenario_Report.docx")
        for name in ("AA", "BB", "CC", "ENERGY", "SERV", "AGRI"):
            self.assertIn(name, text)

    def test_excel_sheets_and_numbers(self):
        self.build()
        from openpyxl import load_workbook
        workbook = load_workbook(self.out / "03_Analyst_Pack.xlsx")
        self.assertIn("Сводка", workbook.sheetnames)
        self.assertIn("Страны", workbook.sheetnames)
        self.assertIn("Отрасли", workbook.sheetnames)
        countries = workbook["Страны"]
        pairs = {row[0].value: row[1].value for row in countries.iter_rows(min_row=2)}
        self.assertEqual(pairs["AA"], 6.0)
        self.assertEqual(pairs["BB"], -2.0)

    def test_machine_json_and_reproducibility(self):
        self.build()
        machine = json.loads((self.out / "04_Machine_Readable.json").read_text(encoding="utf-8"))
        self.assertEqual(machine["scenario_report"]["scenario"]["id"], "energy_plus50")
        repro = json.loads((self.out / "07_Reproducibility.json").read_text(encoding="utf-8"))
        self.assertEqual(repro["input"]["sha256"],
                         hashlib.sha256(self.report.read_bytes()).hexdigest())
        self.assertEqual(repro["dataset"]["id"], "figaro_2019")

    def test_dashboard_is_offline(self):
        self.build()
        html = (self.out / "05_Dashboard.html").read_text(encoding="utf-8")
        self.assertIn("10.00", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script", html.lower())

    def test_deterministic_json_html_and_text(self):
        first = self.build()
        json_digest = hashlib.sha256(
            (self.out / "04_Machine_Readable.json").read_bytes()).hexdigest()
        html_digest = hashlib.sha256((self.out / "05_Dashboard.html").read_bytes()).hexdigest()
        text_before = docx_text(self.out / "01_Executive_Summary.docx")
        client_pack.build_pack(self.report, "Demo Bank", self.out, FIXED, None, False)
        self.assertEqual(hashlib.sha256(
            (self.out / "04_Machine_Readable.json").read_bytes()).hexdigest(), json_digest)
        self.assertEqual(hashlib.sha256(
            (self.out / "05_Dashboard.html").read_bytes()).hexdigest(), html_digest)
        self.assertEqual(docx_text(self.out / "01_Executive_Summary.docx"), text_before)
        self.assertTrue(first["ok"])

    def test_fail_closed_cases(self):
        with self.assertRaises(client_pack.ClientPackError):
            client_pack.build_pack(self.base / "missing.json", "Demo", self.out, FIXED, None, False)
        broken = self.base / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaises(client_pack.ClientPackError):
            client_pack.build_pack(broken, "Demo", self.out, FIXED, None, False)
        with self.assertRaises(client_pack.ClientPackError):
            client_pack.build_pack(self.report, "   ", self.out, FIXED, None, False)
        incomplete = dict(sample_report())
        del incomplete["impacts"]
        path = write_report(self.base, incomplete)
        with self.assertRaises(client_pack.ClientPackError):
            client_pack.build_pack(path, "Demo", self.out, FIXED, None, False)

    def test_cli_help(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                client_pack.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--report", output.getvalue())


if __name__ == "__main__":
    unittest.main()
