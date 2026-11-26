#!/usr/bin/env python3
"""Тесты двух пилотных предложений M1 (tools/pilot_offer_pack.py)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "pilot_offer_pack.py"
spec = importlib.util.spec_from_file_location("pilot_offer_pack", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

OUT = ROOT / "docs" / "pilot_offers"
CATALOGUE = ROOT / "marketing" / "commercial_offers_25" / "offers.json"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180)


class Selection(unittest.TestCase):
    def test_validate_passes(self):
        self.assertEqual(mod.validate(), [])

    def test_two_offers_from_the_catalogue(self):
        picked = mod.select()
        self.assertEqual(len(picked), 2)
        catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        ids = {offer["id"] for offer in catalogue["offers"]}
        for item in picked:
            self.assertIn(item["offer_id"], ids)

    def test_offers_stay_hypotheses_not_clients(self):
        for item in mod.select():
            self.assertEqual(item["offer"]["evidence_status"], "HYPOTHESIS")
            self.assertIs(item["offer"]["not_client"], True)
            self.assertIs(item["offer"]["sent"], False)

    def test_prices_are_positive_integers_within_rate_card(self):
        bands = ((7500, 10000), (15000, 25000), (20000, 30000), (25000, 80000))
        for item in mod.select():
            price = item["offer"]["fixed_price_usd"]
            self.assertIsInstance(price, int)
            self.assertTrue(any(low <= price <= high for low, high in bands), item["offer_id"])

    def test_unknown_offer_is_rejected(self):
        original = mod.SELECTION
        try:
            mod.SELECTION = ({"slug": "pilot_x", "offer_id": "C25-999", "segment_ru": "x",
                              "why_ru": "x", "meeting_ru": "x"},)
            errors = mod.validate()
            self.assertTrue(any("C25-999" in error for error in errors))
        finally:
            mod.SELECTION = original


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = mod.build()

    def test_files_are_written(self):
        for item in mod.select():
            for suffix in (".md", ".docx"):
                path = OUT / f"{item['slug']}{suffix}"
                self.assertTrue(path.is_file(), path.name)
        self.assertTrue((OUT / "README.md").is_file())

    def test_markdown_states_hypothesis_and_not_sent(self):
        for item in mod.select():
            text = (OUT / f"{item['slug']}.md").read_text(encoding="utf-8")
            for token in ("HYPOTHESIS", "не клиент", "не отправлено", "RESEARCH_ONLY",
                          "admission=false"):
                self.assertIn(token, text, item["slug"])

    def test_markdown_has_fixed_price_and_never_promised(self):
        for item in mod.select():
            text = (OUT / f"{item['slug']}.md").read_text(encoding="utf-8")
            price = f"{item['offer']['fixed_price_usd']:,}".replace(",", " ")
            self.assertIn(price, text)
            self.assertIn("Чего мы не обещаем", text)
            self.assertIn("не прогноз", text)

    def test_licence_rule_is_present(self):
        for item in mod.select():
            text = (OUT / f"{item['slug']}.md").read_text(encoding="utf-8")
            self.assertIn("Платные лицензии не приобретаются", text)

    def test_only_owner_mailbox_and_no_personal_contacts(self):
        import re
        for item in mod.select():
            text = (OUT / f"{item['slug']}.md").read_text(encoding="utf-8")
            emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
            self.assertLessEqual(emails, {mod.CONTACT})
            self.assertNotRegex(text, r"\+\d[\d\s().-]{7,}")

    def test_week_word_agrees_with_number(self):
        self.assertEqual(mod._weeks(1), "1 неделя")
        self.assertEqual(mod._weeks(3), "3 недели")
        self.assertEqual(mod._weeks(4), "4 недели")
        self.assertEqual(mod._weeks(5), "5 недель")
        self.assertEqual(mod._weeks(11), "11 недель")
        self.assertEqual(mod._weeks(21), "21 неделя")

    def test_docx_rebuild_is_byte_identical(self):
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            digests = []
            for _ in range(2):
                for item in mod.select():
                    path = target / f"{item['slug']}.docx"
                    mod.build_docx(item, path)
                    digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
            first, second = digests[:2], digests[2:]
            self.assertEqual(first, second, "DOCX rebuild must be deterministic")

    def test_docx_carries_the_same_disclaimers(self):
        from docx import Document
        for item in mod.select():
            document = Document(str(OUT / f"{item['slug']}.docx"))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            for token in ("HYPOTHESIS", "RESEARCH_ONLY", "admission=false",
                          "Платные лицензии не приобретаются"):
                self.assertIn(token, text, item["slug"])


class CommandLine(unittest.TestCase):
    def test_check_cli(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["offers"], 2)
        self.assertEqual(payload["ids"], ["C25-007", "C25-001"])

    def test_without_flags_prints_usage(self):
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
