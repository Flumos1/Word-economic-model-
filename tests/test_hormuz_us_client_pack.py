#!/usr/bin/env python3
"""Tests for the Hormuz -> US client pack (tools/hormuz_us_client_pack.py).

Coverage: every required format is produced, the SHA-256 of each file matches the manifest, the
content digest of both impact variants can be recomputed, two builds at a fixed ``--generated-at``
are byte-identical (without PDFs), no raw third-party observation or external resource leaks into
the package, the status stays RESEARCH_ONLY / admission=false with no admission claim, the stress
numbers are really the stress numbers of the source report, and the build is fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import hormuz_us_client_pack as pack  # noqa: E402
import hormuz_us_impact as impact  # noqa: E402

FIXED = "2026-11-19T09:00:00Z"
CLIENT = "Demo Bank"
REQUIRED = ("01_Executive_Summary.docx", "02_Scenarios_and_Assumptions.xlsx",
            "03_Full_Report.json", "04_Report.html", "manifest.json", "README.md")
TEXT_FORMATS = ("04_Report.html", "README.md")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def docx_text(path: Path) -> str:
    from docx import Document
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def workbook_rows(path: Path, sheet: str) -> list[tuple]:
    from openpyxl import load_workbook
    return list(load_workbook(path)[sheet].iter_rows(min_row=2, values_only=True))


class HormuzUsClientPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "hormuz_us"
        cls.summary = pack.build_pack(cls.out, scenario="stress", client=CLIENT,
                                      generated_at=FIXED, make_pdf=True)
        cls.manifest = read_json(cls.out / "manifest.json")
        cls.machine = read_json(cls.out / "03_Full_Report.json")
        cls.base = impact.build_report("hormuz_closed_6m", FIXED, stress=False)
        cls.stress = impact.build_report("hormuz_closed_6m", FIXED, stress=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ files and formats
    def test_pack_has_all_required_formats(self):
        self.assertTrue(self.summary["ok"])
        self.assertFalse(self.summary["admission"])
        names = set(self.summary["files"])
        for required in REQUIRED:
            self.assertIn(required, names)
            self.assertGreater((self.out / required).stat().st_size, 200, required)
        if self.summary["pdf"]:
            self.assertIn("01_Executive_Summary.pdf", names)
            self.assertGreater((self.out / "01_Executive_Summary.pdf").stat().st_size, 1000)

    def test_executive_summary_has_numbers_ranges_and_disclaimer(self):
        text = docx_text(self.out / "01_Executive_Summary.docx")
        peak = self.base["oil_module"]["price_peak_usd_bbl"]
        low = self.base["oil_module"]["price_range_usd_bbl"]["low"]
        high = self.base["oil_module"]["price_range_usd_bbl"]["high"]
        for token in (CLIENT, "stress", "hormuz_closed_6m", "RESEARCH_ONLY", "Brent",
                      pack._fmt(peak), pack._fmt(low), pack._fmt(high),
                      pack._money(self.base["peak"]["gasoline_usd_gal"])):
            self.assertIn(str(token), text)
        for phrase in ("не прогноз", "не допуск модели", "не инвестиционная рекомендация",
                       "не доверительный интервал", "admission=false"):
            self.assertIn(phrase, text)

    def test_executive_summary_covers_social_impact_and_actions(self):
        text = docx_text(self.out / "01_Executive_Summary.docx")
        for heading in ("Главное", "Социальные последствия", "Что делать",
                        "Границы применимости"):
            self.assertIn(heading, text)
        for token in ("домохозяйств", "бедных", "одобрен", "SPR", "Дипломатия",
                      "source_class=assumption", "replace-with"):
            self.assertIn(token, text)

    def test_workbook_has_required_sheets(self):
        from openpyxl import load_workbook
        workbook = load_workbook(self.out / "02_Scenarios_and_Assumptions.xlsx")
        # Excel forbids "/" in sheet names: «Сценарии/месяцы» is shipped as «Сценарии и месяцы».
        for sheet in ("Сценарии и месяцы", "Допущения и диапазоны", "CPI и домохозяйства",
                      "Соцриск и триггеры"):
            self.assertIn(sheet, workbook.sheetnames)

    def test_workbook_months_match_base_report(self):
        rows = [row for row in workbook_rows(self.out / "02_Scenarios_and_Assumptions.xlsx",
                                             "Сценарии и месяцы") if row[0] == "base"]
        self.assertEqual(len(rows), len(self.base["monthly"]))
        for row, expected in zip(rows, self.base["monthly"]):
            self.assertAlmostEqual(float(row[3]), expected["gasoline_usd_gal"], places=6)
            self.assertAlmostEqual(float(row[4]), expected["diesel_usd_gal"], places=6)
            self.assertAlmostEqual(float(row[6]), expected["cpi_headline_pp"], places=6)
        stress_rows = [row for row in workbook_rows(
            self.out / "02_Scenarios_and_Assumptions.xlsx", "Сценарии и месяцы")
            if row[0] == "stress"]
        self.assertEqual(len(stress_rows), 1)
        self.assertAlmostEqual(float(stress_rows[0][4]), self.stress["stress"]["diesel_usd_gal"],
                               places=6)
        self.assertAlmostEqual(float(stress_rows[0][5]), self.stress["stress"]["jet_usd_gal"],
                               places=6)

    def test_workbook_assumptions_keep_ranges(self):
        rows = workbook_rows(self.out / "02_Scenarios_and_Assumptions.xlsx",
                             "Допущения и диапазоны")
        base_rows = [row for row in rows if row[0] == "потребительский"]
        stress_rows = [row for row in rows if row[0] == "стресс"]
        self.assertEqual(len(base_rows), len(self.base["assumptions"]))
        self.assertEqual(len(stress_rows), len(self.stress["stress"]["assumptions"]))
        quality = next(row for row in stress_rows if row[1] == "crude_quality_premium_usd_bbl")
        self.assertEqual(quality[2], 12.0)
        self.assertEqual((quality[3], quality[4]), (5.0, 20.0))
        self.assertTrue(any("source_class=assumption" in str(row[0]) for row in rows))

    def test_workbook_cpi_and_households(self):
        rows = workbook_rows(self.out / "02_Scenarios_and_Assumptions.xlsx",
                             "CPI и домохозяйства")
        self.assertEqual(len(rows), len(self.base["monthly"]))
        first = rows[0]
        self.assertAlmostEqual(float(first[6]), self.base["peak"]["cpi_headline_pp"], places=6)
        self.assertAlmostEqual(float(first[7]), self.base["peak"]["household_monthly_cost_usd"],
                               places=6)
        self.assertAlmostEqual(float(first[8]), self.base["peak"]["household_annual_cost_usd"],
                               places=6)
        self.assertAlmostEqual(float(first[9]), self.base["peak"]["low_income_budget_shock_pct"],
                               places=6)

    def test_workbook_social_risk_and_triggers(self):
        rows = workbook_rows(self.out / "02_Scenarios_and_Assumptions.xlsx",
                             "Соцриск и триггеры")
        flat = [row for row in rows if row[0] in ("base", "stress")]
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0][1], self.base["social_political"]["score_0_100"])
        self.assertEqual(flat[1][1], self.stress["stress"]["social_political"]["score_0_100"])
        self.assertEqual(flat[1][2], self.stress["stress"]["social_political"]["band"])
        text = "\n".join(str(cell) for row in rows for cell in row if cell is not None)
        for threshold in ("gasoline_critical", "diesel_critical", "cpi_headline_critical"):
            self.assertIn(threshold, text)
        self.assertIn("Мониторинг", text)
        self.assertIn("не вероятность беспорядков", text)

    # ------------------------------------------------------------------ machine-readable
    def test_machine_json_carries_full_reports(self):
        self.assertEqual(self.machine["schema"], pack.SCHEMA)
        self.assertEqual(self.machine["status"], "RESEARCH_ONLY")
        self.assertFalse(self.machine["admission"])
        self.assertFalse(self.machine["causal_claim"])
        self.assertEqual(self.machine["selected"], "stress")
        self.assertEqual(sorted(self.machine["reports"]), ["base", "stress"])
        for name, expected in (("base", self.base), ("stress", self.stress)):
            stored = self.machine["reports"][name]
            self.assertEqual(stored["scenario"], expected["scenario"])
            self.assertEqual(stored["monthly"], expected["monthly"])
            self.assertEqual(stored["peak"], expected["peak"])
            self.assertAlmostEqual(stored["oil_module"]["price_peak_usd_bbl"],
                                   expected["oil_module"]["price_peak_usd_bbl"], places=6)
        self.assertIsNone(self.machine["reports"]["base"]["stress"])
        self.assertIsNotNone(self.machine["reports"]["stress"]["stress"])

    def test_content_sha256_is_recomputable(self):
        for name in ("base", "stress"):
            report = self.machine["reports"][name]
            self.assertEqual(report["content_sha256"], pack.report_sha256(report))
            self.assertRegex(report["content_sha256"], r"^[0-9a-f]{64}$")
            expected = impact.build_report("hormuz_closed_6m", FIXED,
                                           stress=(name == "stress"))
            expected["content_sha256"] = None
            self.assertEqual(report["content_sha256"], pack.report_sha256(expected))
        self.assertEqual(self.machine["sha256"]["selected_content"],
                         self.machine["reports"]["stress"]["content_sha256"])

    # ------------------------------------------------------------------ manifest / integrity
    def test_manifest_hashes_versions_and_status(self):
        manifest = self.manifest
        self.assertEqual(manifest["schema"], pack.MANIFEST_SCHEMA)
        self.assertEqual(manifest["kind"], "client_pack_manifest")
        self.assertEqual(manifest["status"], "RESEARCH_ONLY")
        self.assertFalse(manifest["admission"])
        self.assertFalse(manifest["causal_claim"])
        self.assertEqual(manifest["generated_at"], FIXED)
        self.assertEqual(manifest["client"], CLIENT)
        self.assertEqual(manifest["scenario"], "stress")
        self.assertEqual(manifest["oil_scenario"], "hormuz_closed_6m")
        versions = manifest["versions"]
        self.assertEqual(versions["pack"], pack.TOOL_VERSION)
        self.assertEqual(versions["pack_schema"], pack.SCHEMA)
        self.assertEqual(versions["impact_schema"], "amfe.hormuz_us_impact.v1")
        self.assertEqual(versions["oil_module"], "oil.brent")
        self.assertEqual(manifest["integrity"]["algorithm"], "sha256")
        self.assertTrue(manifest["integrity"]["manifest_excludes_itself"])
        self.assertEqual(manifest["integrity"]["files"], len(manifest["files"]))
        paths = [entry["path"] for entry in manifest["files"]]
        self.assertNotIn("manifest.json", paths)
        for required in REQUIRED:
            if required != "manifest.json":
                self.assertIn(required, paths)

    def test_manifest_sha256_matches_every_file(self):
        for entry in self.manifest["files"]:
            path = self.out / entry["path"]
            self.assertTrue(path.is_file(), entry["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"],
                             entry["path"])
            self.assertEqual(path.stat().st_size, entry["bytes"], entry["path"])
            self.assertEqual(entry["deterministic"], entry["format"] != "pdf")
        verification = pack.verify_manifest(self.out)
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["status"], "RESEARCH_ONLY")
        self.assertFalse(verification["admission"])
        self.assertEqual(verification["checked"], len(self.manifest["files"]))

    def test_manifest_is_deterministic_at_fixed_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            pack.build_pack(out, scenario="stress", client=CLIENT, generated_at=FIXED,
                            make_pdf=False)
            first = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                     for name in ("01_Executive_Summary.docx",
                                  "02_Scenarios_and_Assumptions.xlsx",
                                  "03_Full_Report.json", "04_Report.html", "README.md",
                                  "manifest.json")}
            pack.build_pack(out, scenario="stress", client=CLIENT, generated_at=FIXED,
                            make_pdf=False)
            for name, digest in first.items():
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest,
                                 name)
            self.assertFalse((out / "01_Executive_Summary.pdf").exists())

    # ------------------------------------------------------------------ honesty / no-raw
    def test_html_is_static_and_uses_real_numbers(self):
        html = (self.out / "04_Report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn(CLIENT, html)
        self.assertIn("RESEARCH_ONLY", html)
        self.assertIn(pack._fmt(self.base["oil_module"]["price_peak_usd_bbl"]), html)
        self.assertIn(pack._fmt(self.stress["stress"]["diesel_usd_gal"]), html)
        self.assertIn(pack._fmt(self.stress["stress"]["jet_usd_gal"]), html)
        self.assertIn("не прогноз", html)

    def test_stress_numbers_are_stress_numbers(self):
        base_peak, stress_block = self.base["peak"], self.stress["stress"]
        # The stress block must actually move the number, not repeat the base peak.
        self.assertGreater(stress_block["diesel_usd_gal"], base_peak["diesel_usd_gal"])
        self.assertGreater(stress_block["cpi_headline_pp"], base_peak["cpi_headline_pp"])
        self.assertGreater(stress_block["jet_usd_gal"], base_peak["diesel_usd_gal"])
        self.assertEqual(stress_block["social_political"]["score_0_100"],
                         self.summary["numbers"]["stress"]["risk"])
        self.assertEqual(self.summary["numbers"]["stress"]["diesel_peak"],
                         stress_block["diesel_usd_gal"])
        self.assertEqual(self.summary["numbers"]["stress"]["jet_peak"],
                         stress_block["jet_usd_gal"])
        self.assertEqual(self.summary["numbers"]["base"]["risk"],
                         self.base["social_political"]["score_0_100"])
        # The documented central stress figures must be the ones in this pack.
        self.assertAlmostEqual(stress_block["diesel_usd_gal"], 8.04, places=2)
        self.assertAlmostEqual(stress_block["jet_usd_gal"], 6.93, places=2)
        self.assertAlmostEqual(stress_block["cpi_headline_pp"], 5.62, places=2)
        self.assertEqual(stress_block["social_political"]["band"], "high")
        self.assertAlmostEqual(base_peak["gasoline_usd_gal"], 5.45, places=2)
        self.assertEqual(self.base["social_political"]["band"], "elevated")

    def test_no_raw_observations_or_admission_claims(self):
        forbidden = ('"observations":', '"raw":', '"admission": true', "admission=true",
                     "model admitted", "допущенная модель", '"approved":', "APPROVED")
        text = json.dumps(self.machine, ensure_ascii=False) + json.dumps(self.manifest,
                                                                        ensure_ascii=False)
        text += "\n".join((self.out / name).read_text(encoding="utf-8")
                          for name in TEXT_FORMATS)
        text += docx_text(self.out / "01_Executive_Summary.docx")
        for token in forbidden:
            self.assertNotIn(token, text, token)
        self.assertEqual(self.machine["policy"]["no_admission"], True)
        self.assertEqual(self.machine["licence"]["research_only"], True)
        self.assertFalse(self.machine["licence"]["admission"])
        self.assertEqual(self.machine["licence"]["no_third_party_observations"], True)
        # Only declared assumptions, no observed series, in the machine-readable package.
        self.assertEqual(sorted(self.machine["reports"]["base"]["assumptions"]),
                         sorted(self.base["assumptions"]))

    # ------------------------------------------------------------------ fail-closed / CLI
    def test_fail_closed_empty_client_and_unknown_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(out, client="   ", generated_at=FIXED, make_pdf=False)
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(out, scenario="extreme", generated_at=FIXED, make_pdf=False)
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(out, oil_scenario="nope", generated_at=FIXED, make_pdf=False)
            self.assertFalse(out.exists())
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(Path(tmp) / "pack2", generated_at="19.11.2026", make_pdf=False)

    def test_fail_closed_bad_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            bad = {"base": dict(self.base), "stress": dict(self.stress)}
            bad["base"] = dict(bad["base"])
            bad["base"]["status"] = "ADMITTED"
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(out, generated_at=FIXED, make_pdf=False, reports=bad)
            self.assertFalse(out.exists())
            missing = {"base": self.base}
            with self.assertRaises(pack.HormuzPackError):
                pack.build_pack(out, generated_at=FIXED, make_pdf=False, reports=missing)

    def test_cli_json_build_and_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cli_pack"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "hormuz_us_client_pack.py"),
                 "--out", str(out), "--client", CLIENT, "--generated-at", FIXED,
                 "--scenario", "base", "--no-pdf", "--json"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertFalse(summary["pdf"])
            self.assertEqual(summary["scenario"], "base")
            self.assertEqual(summary["status"], "RESEARCH_ONLY")
            self.assertFalse(summary["admission"])
            self.assertTrue((out / "manifest.json").is_file())
            self.assertFalse((out / "01_Executive_Summary.pdf").exists())
            help_run = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "hormuz_us_client_pack.py"), "--help"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(help_run.returncode, 0)
            for flag in ("--out", "--client", "--generated-at", "--no-pdf", "--json",
                         "--scenario"):
                self.assertIn(flag, help_run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
