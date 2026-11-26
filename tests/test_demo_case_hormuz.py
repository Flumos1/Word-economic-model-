#!/usr/bin/env python3
"""Демо-кейс Ормуза: документ не должен разойтись с артефактом (docs/247)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "247_Демо_кейс_Ормуз_бюджет.md"
PACK = ROOT / "build" / "hormuz_client_pack"
REPORT = PACK / "03_Full_Report.json"
MANIFEST = PACK / "manifest.json"


def fmt(value: float, digits: int = 2) -> str:
    """Russian-style decimal comma, matching the document."""
    return f"{value:.{digits}f}".replace(".", ",")


def fmt_grouped(value: float, digits: int = 2) -> str:
    """Same, but with a thin-space thousands separator as written in the document."""
    text = f"{value:,.{digits}f}".replace(",", " ")
    return text.replace(".", ",")


def plain(text: str) -> str:
    """Strip bold/italic markers so wording checks survive formatting."""
    return text.replace("**", "").replace("__", "")


@unittest.skipUnless(REPORT.is_file() and MANIFEST.is_file(),
                     "generated hormuz client pack is not present")
class DemoCaseMatchesArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")
        cls.report = json.loads(REPORT.read_text(encoding="utf-8"))["report"]
        cls.peak = cls.report["peak"]

    def test_document_exists_and_is_honest(self):
        self.assertTrue(DOC.is_file())
        for token in ("HYPOTHESIS", "не клиентский", "admission=false", "не прогноз",
                      "C6"):
            self.assertIn(token, self.text)

    def test_peak_numbers_match_the_artifact(self):
        expected = {
            "brent": fmt(self.peak["brent_usd_bbl"]),
            "gasoline": fmt(self.peak["gasoline_usd_gal"]),
            "diesel": fmt(self.peak["diesel_usd_gal"]),
            "cpi": fmt(self.peak["cpi_headline_pp"]),
        }
        for name, value in expected.items():
            self.assertIn(value, self.text, f"{name}: {value} is not in the document")
        # percentages also come from the artifact
        self.assertIn(fmt(self.peak["brent_delta_pct"]), self.text)
        self.assertIn(fmt(self.peak["gasoline_delta_pct"]), self.text)
        self.assertIn(fmt(self.peak["diesel_delta_pct"]), self.text)

    def test_household_and_social_numbers_match(self):
        self.assertIn(fmt_grouped(self.peak["household_monthly_cost_usd"]), self.text)
        self.assertIn(fmt_grouped(self.peak["household_annual_cost_usd"]), self.text)
        social = self.report["social_political"]
        self.assertIn(str(social["score_0_100"]), self.text)
        self.assertIn(social["band"], self.text)

    def test_gasoline_range_matches(self):
        low = fmt(self.peak["gasoline_range_usd_gal"]["low"])
        high = fmt(self.peak["gasoline_range_usd_gal"]["high"])
        self.assertIn(f"{low}–{high}", self.text)

    def test_document_claims_no_admission_and_no_client(self):
        self.assertNotIn("наша модель допущена", self.text)
        self.assertNotIn("клиент подтвердил", self.text)

    def test_manifest_is_consistent_with_the_files(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertFalse(manifest["admission"])
        self.assertFalse(manifest["causal_claim"])
        self.assertEqual(manifest["oil_scenario"], "hormuz_closed_6m")
        names = {item["path"] for item in manifest["files"]}
        for required in ("01_Executive_Summary.docx", "02_Scenarios_and_Assumptions.xlsx",
                         "03_Full_Report.json", "04_Report.html"):
            self.assertIn(required, names)

    def test_rebuild_command_is_documented(self):
        self.assertIn("tools/hormuz_us_client_pack.py", self.text)
        self.assertIn("hormuz_closed_6m", self.text)


class DemoCaseStandalone(unittest.TestCase):
    """Проверки, которые работают и без артефакта."""

    def test_document_states_borders(self):
        text = plain(DOC.read_text(encoding="utf-8")).lower()
        for token in ("не доверительный интервал", "не вероятность",
                      "не предсказание беспорядков", "правило владельца"):
            self.assertIn(token, text)

    def test_document_mentions_no_paid_licence_sources(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("ICE/NYMEX", text)
        self.assertIn("docs/238", text)


if __name__ == "__main__":
    unittest.main()
