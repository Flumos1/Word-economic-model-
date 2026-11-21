#!/usr/bin/env python3
"""Tests for the commercial case pack (``marketing/cases/``).

The pack is a set of drafts: nothing is sent and nothing is committed. The tests check that

  * the deliverables exist and are the only files in the pack (3 Markdown + 3 two-page PDF + manifest);
  * the manifest hashes and sizes match the bytes on disk and are reproducible from the builder;
  * every case follows the required structure (вопрос → подход → числа/допущения → решение → границы → CTA);
  * the key numbers in each case really exist in the source documents (docs/184, 186, 189, 190);
  * the demo set is never presented as client data, no company facts are invented, nothing claims an
    admitted/certified model, and no raw rows, local paths or URLs leaked into the texts;
  * the refining case says explicitly that there is no refinery-margin module and carries no
    invented margin numbers;
  * docs/208 explains usage, prohibitions and how to replace the demo with client data.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "marketing" / "cases"
MANIFEST = CASES / "manifest.json"
DOCS208 = ROOT / "docs" / "208_Коммерческие_кейсы.md"
BUILDER = ROOT / "tools" / "build_cases.py"
SOURCES = {
    "184": ROOT / "docs" / "184_FLAKS_Procurement_MVP.md",
    "186": ROOT / "docs" / "186_Ормуз_нефть_США_последствия.md",
    "189": ROOT / "docs" / "189_FLAKS_пилот.md",
    "190": ROOT / "docs" / "190_Клиентский_пакет_Ормуз_США.md",
}

STEMS = ("case_tatneft_budget", "case_taneco_refining", "case_flaks_procurement")
EXPECTED_FILES = {
    "case_tatneft_budget.md",
    "case_tatneft_budget.pdf",
    "case_taneco_refining.md",
    "case_taneco_refining.pdf",
    "case_flaks_procurement.md",
    "case_flaks_procurement.pdf",
    "manifest.json",
}

DISCLAIMERS = {
    "case_tatneft_budget": ("RESEARCH_ONLY", "admission=false", "не прогноз", "не данные Татнефти",
                            "гипотеза, не факт"),
    "case_taneco_refining": ("RESEARCH_ONLY", "admission=false", "не прогноз", "не данные ТАНЕКО",
                             "модуля маржи НПЗ у нас нет"),
    "case_flaks_procurement": ("RESEARCH_ONLY", "admission=false", "не прогноз", "не данные клиента",
                               "синтетический демо-набор"),
}

# Case number -> source document that must contain the very same token.
ANCHORS = {
    "case_tatneft_budget": {
        "168,86": "190", "139,04": "190", "268,52": "190", "6,62": "190", "8,04": "190",
        "6,93": "190", "+129,36": "190", "25,50": "190", "10,00": "190", "+3,91": "190",
        "+5,62": "190", "211,18": "190", "2 534,11": "190", "6,06": "190", "5,45": "186",
        "130,1": "186", "168,9": "186", "123,5": "186", "4,31": "186", "5,26": "186",
        "+109": "186", "112,5": "186", "4,04": "186", "4,92": "186", "+84": "186",
        "180,3": "186", "+4,346": "184", "3 552 342": "184", "3 481 295": "184",
        "+17,32": "184", "6,091": "184", "36 900": "184", "738 000": "184", "3 404 400": "184",
    },
    "case_taneco_refining": {
        "168,86": "190", "6,62": "190", "8,04": "190", "6,93": "190", "+129,36": "190",
        "25,50": "190", "10,00": "190", "130,1": "186", "139,04": "190", "268,52": "190",
        "+112": "190", "+177": "190", "+4,35": "184", "+17,32": "184", "6,091": "184",
        "5,0": "184",
    },
    "case_flaks_procurement": {
        "3 404 400": "184", "3 419 781": "184", "3 426 296": "184", "3 552 342": "184",
        "3 486 089": "184", "+15 381": "184", "+21 896": "184", "+147 942": "184",
        "+81 689": "184", "+0,452": "184", "+0,643": "184", "+4,346": "184", "+2,400": "184",
        "3 481 295": "184", "3 311 785": "184", "3 537 827": "184", "3 566 345": "184",
        "0,9289": "184", "0,9456": "184", "0,9320": "184", "0,9261": "184", "1,86": "184",
        "2,2290": "184", "1,16": "184", "1,3196": "184", "+8,28": "184", "+7,32": "184",
        "+7,24": "184", "5,000": "184", "36 900": "184", "738 000": "184", "2,364": "184",
        "0,12": "184", "0,09": "184", "0,35": "184",
    },
}

FORBIDDEN_CLIENT_FACTS = (
    "выручка", "выручк", "сотрудник", "млрд", "доля рынка", "капитализац",
    "прибыль компании", "наш клиент", "клиент компании", "по данным компании",
)
FORBIDDEN_CLAIMS = ("admitted", "допущенн", "сертифицирован", "верифицирован", "аттестован")
FORBIDDEN_RAW = (
    "C:\\", "/home/", "data/mrio", "/data/raw", "observations", "source_rows",
    "raw_data", "http://", "https://",
)
FORBIDDEN_PROMISES = ("гарантируем", "гарантированн", "точность выше", "магическ", "X% экономии")

try:  # optional: only used to read the rendered PDF text
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional dependency
    PdfReader = None


def md_text(stem: str) -> str:
    return (CASES / f"{stem}.md").read_text(encoding="utf-8")


def pdf_text(stem: str) -> str:
    if PdfReader is None:
        raise unittest.SkipTest("pypdf is not installed")
    reader = PdfReader(str(CASES / f"{stem}.pdf"))
    pages = [page.extract_text() or "" for page in reader.pages]
    # Justified text comes back with runs of spaces; collapse them but keep line breaks.
    return "\n".join(
        " ".join(line.split()) for page in pages for line in page.splitlines()
    )


def sections(markdown: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"^## \d+\.\s+(.+)$", markdown, re.M)]


def section_body(markdown: str, title_part: str) -> str:
    pattern = re.compile(r"^## \d+\.\s+([^\n]+)$", re.M)
    matches = list(pattern.finditer(markdown))
    for index, match in enumerate(matches):
        if title_part.lower() in match.group(1).lower():
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            return markdown[start:end]
    return ""


class CasePackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.md = {stem: md_text(stem) for stem in STEMS}
        cls.sources = {key: path.read_text(encoding="utf-8") for key, path in SOURCES.items()}
        cls.pdf = {}
        if PdfReader is not None:
            cls.pdf = {stem: pdf_text(stem) for stem in STEMS}

    def test_required_files_exist(self):
        for name in EXPECTED_FILES:
            self.assertTrue((CASES / name).is_file(), name)
        self.assertTrue(DOCS208.is_file())
        self.assertTrue(BUILDER.is_file())
        self.assertTrue(Path(__file__).is_file())

    def test_no_extra_files_in_cases_dir(self):
        on_disk = {path.name for path in CASES.iterdir() if path.is_file()}
        self.assertEqual(on_disk, EXPECTED_FILES)

    def test_manifest_sha256_matches_disk(self):
        manifest = self.manifest
        self.assertEqual(manifest["schema"], "amfe.marketing.cases.v1")
        self.assertEqual(manifest["created"], "2026-11-21")
        self.assertEqual(manifest["model_status"], "RESEARCH_ONLY")
        self.assertFalse(manifest["admission"])
        self.assertFalse(manifest["causal_claim"])
        self.assertFalse(manifest["status"]["sent"])
        self.assertFalse(manifest["status"]["client_specific"])
        self.assertTrue(manifest["rules"]["demo_never_presented_as_client_data"])
        self.assertTrue(manifest["rules"]["manifest_excludes_itself"])

        listed = {entry["path"] for entry in manifest["files"]}
        on_disk = {path.name for path in CASES.iterdir() if path.is_file() and path.name != "manifest.json"}
        self.assertEqual(listed, on_disk)
        self.assertNotIn("manifest.json", listed)

        for entry in manifest["files"]:
            path = CASES / entry["path"]
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest(), entry["path"])
            self.assertEqual(entry["bytes"], path.stat().st_size, entry["path"])

    def test_manifest_is_reproducible_from_builder(self):
        spec = importlib.util.spec_from_file_location("build_cases", BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rebuilt = json.dumps(module.build_manifest(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(rebuilt, json.dumps(self.manifest, ensure_ascii=False, sort_keys=True))

    def test_pdfs_are_valid_and_two_pages(self):
        pages_in_manifest = {
            entry["path"]: entry.get("pages")
            for entry in self.manifest["files"]
            if entry["kind"] == "pdf"
        }
        self.assertEqual(len(pages_in_manifest), 3)
        for stem in STEMS:
            path = CASES / f"{stem}.pdf"
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"%PDF-"), path.name)
            self.assertIn(b"%%EOF", raw[-2048:], path.name)
            self.assertGreater(len(raw), 50_000, path.name)
            self.assertEqual(pages_in_manifest[f"{stem}.pdf"], 2, path.name)
            if PdfReader is not None:
                self.assertEqual(len(PdfReader(str(path)).pages), 2, path.name)

    def test_case_markdown_has_required_sections(self):
        for stem in STEMS:
            titles = sections(self.md[stem])
            self.assertTrue(titles, stem)
            self.assertIn("вопрос", titles[0].lower(), stem)
            joined = " | ".join(titles).lower()
            for required in ("подход", "числа", "что решил бы клиент", "границы"):
                self.assertIn(required, joined, f"{stem}: {required}")
            self.assertIn("cta", titles[-1].lower(), stem)
            self.assertLess(
                titles.index(next(t for t in titles if "что решил бы клиент" in t.lower())),
                titles.index(next(t for t in titles if "границы" in t.lower())),
                f"{stem}: decision must precede limits",
            )

    def test_case_states_three_decisions(self):
        for stem in STEMS:
            body = section_body(self.md[stem], "Что решил бы клиент")
            numbered = re.findall(r"^\d+\.\s+\S", body, flags=re.M)
            self.assertEqual(len(numbered), 3, f"{stem}: expected three decisions")
            self.assertIn("гипотеза", body.lower(), stem)

    def test_disclaimer_in_markdown_and_pdf(self):
        for stem in STEMS:
            for token in DISCLAIMERS[stem]:
                self.assertIn(token, self.md[stem], f"{stem}.md: {token}")
                if self.pdf:
                    self.assertIn(token, self.pdf[stem], f"{stem}.pdf: {token}")

    def test_numbers_match_sources(self):
        for stem, tokens in ANCHORS.items():
            text = self.md[stem]
            for token, source_key in tokens.items():
                self.assertIn(token, text, f"{stem}: missing {token}")
                self.assertIn(token, self.sources[source_key], f"{source_key} lacks {token}")

    def test_pdf_contains_key_numbers(self):
        if not self.pdf:
            self.skipTest("pypdf is not installed")
        required = {
            "case_tatneft_budget": ("168,86", "8,04", "+5,62", "211,18"),
            "case_taneco_refining": ("168,86", "6,93", "+129,36", "25,50"),
            "case_flaks_procurement": ("3 404 400", "3 552 342", "+4,346", "36 900"),
        }
        for stem, tokens in required.items():
            for token in tokens:
                self.assertIn(token, self.pdf[stem], f"{stem}.pdf: {token}")

    def test_no_fake_client_claims(self):
        for stem in STEMS:
            lowered = self.md[stem].lower()
            for forbidden in FORBIDDEN_CLIENT_FACTS:
                self.assertNotIn(forbidden, lowered, f"{stem}: {forbidden}")
        self.assertIn("не данные клиента", self.md["case_flaks_procurement"].lower())
        self.assertIn("не данные", self.md["case_tatneft_budget"].lower())
        self.assertIn("не данные", self.md["case_taneco_refining"].lower())

    def test_no_admitted_or_certified_claims(self):
        for stem in STEMS:
            lowered = self.md[stem].lower()
            for forbidden in FORBIDDEN_CLAIMS:
                self.assertNotIn(forbidden, lowered, f"{stem}: {forbidden}")
            for forbidden in FORBIDDEN_PROMISES:
                self.assertNotIn(forbidden, lowered, f"{stem}: {forbidden}")
            self.assertIn("admission=false", self.md[stem])

    def test_no_raw_data_or_paths(self):
        for stem in STEMS:
            texts = [self.md[stem]] + ([self.pdf[stem]] if self.pdf else [])
            for text in texts:
                for forbidden in FORBIDDEN_RAW:
                    self.assertNotIn(forbidden, text, f"{stem}: {forbidden}")
                self.assertLess(max(len(line) for line in text.splitlines()), 1200, stem)

    def test_taneco_has_no_refining_margin_numbers(self):
        text = self.md["case_taneco_refining"]
        lowered = text.lower()
        self.assertIn("отдельного модуля маржи нпз у нас нет", lowered)
        self.assertIn("маржа = сумма по фракциям", lowered)
        # no numeric margin claims anywhere in the refining case
        self.assertIsNone(
            re.search(r"марж[а-я]*[^.!?\n]{0,40}\d", lowered),
            "refining case must not state a numeric margin",
        )
        self.assertNotIn("маржа нпз", lowered)
        self.assertIn("прогнать 2–3 конфигурации", lowered)

    def test_flaks_case_has_demo_budget_tco_cap_floor(self):
        text = self.md["case_flaks_procurement"]
        for token in ("база", "3 404 400", "3 552 342", "+4,346", "3 481 295",
                      "0,9289", "0,9456", "0,9320", "0,9261", "CAP_BINDING", "FLOOR_BINDING",
                      "36 900", "5,0", "синтетический"):
            self.assertIn(token, text, token)
        self.assertIn("синтетический демо-набор", text.lower())

    def test_tatneft_case_covers_cfo_numbers(self):
        text = self.md["case_tatneft_budget"]
        for token in ("168,86", "139,04–268,52", "8,04", "+3,91", "+5,62", "211,18",
                      "+129,36", "6,091", "180,3"):
            self.assertIn(token, text, token)
        lowered = text.lower()
        self.assertIn("дизель", lowered)
        self.assertIn("cpi", lowered)
        self.assertIn("домохозяйств", lowered)
        self.assertIn("capex мы не считаем", lowered)

    def test_docs208_explains_usage_and_replacement(self):
        text = DOCS208.read_text(encoding="utf-8")
        for phrase in ("Как использовать", "Что нельзя", "Как заменить демо на данные клиента",
                       "RESEARCH_ONLY", "admission=false", "python tools/build_cases.py",
                       "tests/test_cases.py", "манифест"):
            self.assertIn(phrase, text, phrase)
        for stem in STEMS:
            self.assertIn(f"{stem}.md", text, stem)
            self.assertIn(f"{stem}.pdf", text, stem)


if __name__ == "__main__":
    unittest.main(verbosity=2)
