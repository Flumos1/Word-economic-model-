#!/usr/bin/env python3
"""Tests for the 2026-11-21 outreach batch (pilot offers, LEGAT follow-up, plan, CRM memo).

The batch is a set of drafts: nothing is sent. The tests check that the files exist and agree with
each other, that the offers are complete (company, 2–3 use cases, USD prices, limits), that the
manifest hashes match the bytes on disk, and that no invented contacts, company facts or "admitted
model" claims leaked into the texts.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import importlib.util
import json
import re
import unittest
from datetime import date
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "outreach" / "batch_2026-11-21"
MANIFEST = BATCH / "manifest.json"
CSV = ROOT / "marketing" / "crm_pipeline.csv.tmpl"
BUILDER = ROOT / "tools" / "build_outreach_batch.py"

STEMS = ("tatneft", "taneco", "gazpromneft")
COMPANIES = {
    "tatneft": "ПАО «Татнефть»",
    "taneco": "АО «ТАНЕКО»",
    "gazpromneft": "ПАО «Газпром нефть»",
}
ACCOUNTS = ("TATNEFT", "TANECO", "GAZPROMNEFT", "NOVATEK", "IRKUTSK_OIL")

TEXT_FILES = (
    "followup_legat_2026-11-27.md",
    "send_plan.md",
    "crm_update.md",
)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(
    r"\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|\b8\s*\(\s*\d{3}\s*\)\s*\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|\btel:|\bmailto:",
    re.IGNORECASE,
)
ALLOWED_EMAILS = {"flumos@gmail.com", "tpolegat@gmail.com"}

try:  # optional: only used to read the rendered PDF text
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional dependency
    PdfReader = None


def read_text(name: str) -> str:
    return (BATCH / name).read_text(encoding="utf-8")


def docx_text(path: Path) -> str:
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def eml_message():
    return email.message_from_bytes(
        (BATCH / "followup_legat_2026-11-27.eml").read_bytes(), policy=email.policy.default
    )


def batch_texts() -> dict[str, str]:
    """All human-readable batch content, including rendered PDF text when pypdf is available."""
    texts = {name: read_text(name) for name in TEXT_FILES}
    texts["followup_legat_2026-11-27.eml"] = eml_message().get_content()
    for stem in STEMS:
        texts[f"pilot_offer_{stem}.docx"] = docx_text(BATCH / f"pilot_offer_{stem}.docx")
        if PdfReader is not None:
            texts[f"pilot_offer_{stem}.pdf"] = pdf_text(BATCH / f"pilot_offer_{stem}.pdf")
    return texts


class OutreachBatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.texts = batch_texts()

    def test_required_files_exist(self):
        expected = set(TEXT_FILES) | {"followup_legat_2026-11-27.eml", "manifest.json"}
        for stem in STEMS:
            expected |= {f"pilot_offer_{stem}.docx", f"pilot_offer_{stem}.pdf"}
        actual = {path.name for path in BATCH.iterdir() if path.is_file()}
        self.assertEqual(actual, expected)
        for name in expected:
            self.assertTrue((BATCH / name).is_file(), name)

    def test_manifest_sha256_matches_disk(self):
        self.assertEqual(self.manifest["schema"], "amfe.outreach.batch.v1")
        self.assertEqual(self.manifest["batch_id"], "batch_2026-11-21")
        self.assertEqual(self.manifest["created"], "2026-11-21")
        self.assertFalse(self.manifest["status"]["sent"])
        self.assertEqual(self.manifest["status"]["verified_contacts"], 0)
        self.assertEqual(self.manifest["model_status"], "RESEARCH_ONLY")
        self.assertFalse(self.manifest["admission"])

        listed = {entry["path"] for entry in self.manifest["files"]}
        on_disk = {path.name for path in BATCH.iterdir() if path.is_file() and path.name != "manifest.json"}
        self.assertEqual(listed, on_disk)

        for entry in self.manifest["files"]:
            path = BATCH / entry["path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(entry["sha256"], digest, entry["path"])
            self.assertEqual(entry["bytes"], path.stat().st_size, entry["path"])

    def test_manifest_is_reproducible_from_builder(self):
        spec = importlib.util.spec_from_file_location("build_outreach_batch", BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(json.dumps(module.build_manifest(), ensure_ascii=False, sort_keys=True),
                         json.dumps(self.manifest, ensure_ascii=False, sort_keys=True))

    def test_three_offers_are_valid_pdf_and_docx(self):
        for stem in STEMS:
            docx_path = BATCH / f"pilot_offer_{stem}.docx"
            pdf_path = BATCH / f"pilot_offer_{stem}.pdf"
            self.assertGreater(docx_path.stat().st_size, 10_000, docx_path.name)
            raw = pdf_path.read_bytes()
            self.assertTrue(raw.startswith(b"%PDF-"), pdf_path.name)
            self.assertIn(b"%%EOF", raw[-2048:], pdf_path.name)
            self.assertGreater(len(raw), 50_000, pdf_path.name)
            if PdfReader is not None:
                self.assertGreaterEqual(len(PdfReader(str(pdf_path)).pages), 2, pdf_path.name)

    def test_offer_company_status_and_version(self):
        for stem, company in COMPANIES.items():
            text = self.texts[f"pilot_offer_{stem}.docx"]
            self.assertIn(f"AMFE для {company}", text)
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("admission=false", text)
            self.assertIn("не прогноз", text.lower())
            self.assertIn("Границы и ограничения", text)
            self.assertIn("2026-11-21", text)

    def test_offer_has_three_use_cases_and_four_packages(self):
        for stem in STEMS:
            document = Document(str(BATCH / f"pilot_offer_{stem}.docx"))
            cases, packages = document.tables[0], document.tables[1]
            self.assertEqual(len(cases.rows), 4, stem)      # header + 3 use cases
            self.assertEqual(len(packages.rows), 5, stem)   # header + 4 packages
            self.assertEqual(cases.rows[0].cells[0].text, "Направление")
            self.assertEqual(cases.rows[0].cells[-1].text, "Цена, USD")
            self.assertEqual(packages.rows[0].cells[0].text, "Пакет")

    def test_offer_prices_are_usd_only(self):
        for stem in STEMS:
            text = self.texts[f"pilot_offer_{stem}.docx"]
            self.assertIn("7 500", text, stem)
            self.assertIn("Цена, USD", text, stem)
            self.assertNotIn("₽", text, stem)
            self.assertNotIn("руб.", text, stem)

    def test_offer_pdf_text_matches_docx(self):
        if PdfReader is None:
            self.skipTest("pypdf is not installed; PDF text check skipped")
        for stem, company in COMPANIES.items():
            text = self.texts[f"pilot_offer_{stem}.pdf"]
            self.assertIn(company, text)
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("admission=false", text)

    def test_no_fake_emails_or_phones(self):
        for name, text in self.texts.items():
            for match in EMAIL_PATTERN.findall(text):
                self.assertIn(match.lower(), ALLOWED_EMAILS, f"{name}: {match}")
            self.assertIsNone(PHONE_PATTERN.search(text), name)

    def test_no_admitted_model_claims(self):
        for name, text in self.texts.items():
            lowered = text.lower()
            self.assertNotIn("admitted", lowered, name)
            self.assertNotIn("допущенн", lowered, name)
        for stem in STEMS:
            self.assertIn("admission=false", self.texts[f"pilot_offer_{stem}.docx"])

    def test_no_invented_company_facts(self):
        for stem in STEMS:
            text = self.texts[f"pilot_offer_{stem}.docx"]
            self.assertIn("нет утверждений о финансовых показателях", text)
            for forbidden in ("млрд", "тыс. сотрудник", "сотрудник", "выручка компании",
                              "доля рынка", "капитализация"):
                self.assertNotIn(forbidden, text.lower(), f"{stem}: {forbidden}")

    def test_offers_do_not_cross_mention_other_accounts(self):
        forbidden = {
            "tatneft": ("ТАНЕКО", "Газпром", "НОВАТЭК", "ИНК"),
            "taneco": ("Татнефть", "Газпром", "НОВАТЭК", "ИНК"),
            "gazpromneft": ("Татнефть", "ТАНЕКО", "НОВАТЭК", "ИНК"),
        }
        for stem, names in forbidden.items():
            text = self.texts[f"pilot_offer_{stem}.docx"]
            for name in names:
                self.assertNotIn(name, text, f"{stem} mentions {name}")

    def test_followup_is_unsent_and_dated_2026_11_27(self):
        markdown = read_text("followup_legat_2026-11-27.md")
        self.assertIn("НЕ ОТПРАВЛЕНО", markdown)
        self.assertIn("2026-11-27", markdown)
        self.assertIn("20 ноября", markdown)
        self.assertIn("20 минут", markdown)

        message = eml_message()
        self.assertEqual(message["X-Unsent"], "1")
        self.assertEqual(message["To"], "tpolegat@gmail.com")
        self.assertEqual(message["From"], "Flumos <flumos@gmail.com>")
        self.assertTrue(str(message["Subject"]).startswith("Re:"))
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertFalse(message.is_multipart())
        self.assertEqual(list(message.iter_attachments()), [])
        sent_at = email.utils.parsedate_to_datetime(message["Date"])
        self.assertEqual(sent_at.date(), date(2026, 11, 27))

        body = message.get_content()
        self.assertIn("20 ноября", body)
        self.assertIn("20 минут", body)
        self.assertIn("не буду больше напоминать", body)
        self.assertLess(len(body), 1200, "follow-up must stay short")

    def test_followup_md_and_eml_agree(self):
        markdown = read_text("followup_legat_2026-11-27.md")
        body = eml_message().get_content()
        for phrase in ("Короткое напоминание о письме от 20 ноября",
                       "Предлагаю 20 минут",
                       "Удобно на неделе с 30 ноября",
                       "P.S. Это сценарная аналитика, не прогноз"):
            self.assertIn(phrase, body)
            self.assertIn(phrase, markdown)

    def test_send_plan_covers_five_accounts_honestly(self):
        text = read_text("send_plan.md")
        for account in ACCOUNTS:
            self.assertIn(f"`{account}`", text)
        for letter in ("tatneft.md", "taneco.md", "gazpromneft.md", "novatek.md", "irk.md"):
            self.assertIn(letter, text)
        for stem in STEMS:
            self.assertIn(f"pilot_offer_{stem}.pdf", text)
        self.assertIn("НИЧЕГО НЕ ОТПРАВЛЕНО", text)
        self.assertIn("адреса", text.lower())
        self.assertIn("человек", text)
        self.assertIn("почтового канала", text.lower())
        self.assertEqual(len(re.findall(r"^\| \d \|", text, flags=re.MULTILINE)), 5)

    def test_crm_instructions_do_not_change_csv(self):
        text = read_text("crm_update.md")
        for account in ACCOUNTS:
            self.assertIn(f"`{account}`", text)
        for stage in ("CONTACTED", "DISCOVERY", "PILOT", "PROPOSAL", "WON", "LOST"):
            self.assertIn(stage, text)
        self.assertIn("не изменён", text)
        self.assertIn("marketing_crm.py --check", text)

        rows = [line for line in CSV.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        self.assertEqual(len(rows), 21)  # header + 20 accounts
        self.assertTrue(all('"NEW"' in line for line in rows[1:]))
        self.assertTrue(any('"TATNEFT"' in line for line in rows[1:]))

    def test_batch_contains_no_sent_markers_or_secrets(self):
        self.assertFalse((BATCH / ".smtp.env").exists())
        for name, text in self.texts.items():
            lowered = text.lower()
            self.assertNotIn("smtp принял", lowered, name)
            self.assertNotIn("письмо отправлено", lowered, name)
            self.assertNotIn("status: sent", lowered, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
