#!/usr/bin/env python3
"""Tests for the three western pilot one-pagers (``marketing/western_cases/pilot_briefs``).

The pack is a set of drafts: nothing is sent and nothing is committed. The tests check that

  * each brief exists as Markdown and as a one-page A4 PDF, and the builder is present;
  * a brief covers its company, its two catalog cases and the six required sections;
  * it lists 3-5 facts, each with a ``source_id`` that exists in ``cases.json`` and in the
    matching fact pack (``docs/219`` or ``docs/220``);
  * every price, duration and fact value traces back to ``cases.json``, and the "Both cases"
    row is exactly the sum of the two cases;
  * no other number is invented: all 4+-digit figures come from the pack or that sum;
  * no company contacts, no phone, no foreign link, and no "client" wording beyond the
    ``not_client=true`` flag;
  * no accuracy or admission promise, and the RESEARCH_ONLY flags are repeated in Markdown,
    in the PDF body and in the PDF footer;
  * the PDF carries a ``mailto:flumos@gmail.com`` link and its text matches the Markdown;
  * ``README.md`` documents the files and the rules, and ``--check`` passes.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIEF_DIR = ROOT / "marketing" / "western_cases" / "pilot_briefs"
CASES_JSON = ROOT / "marketing" / "western_cases" / "cases.json"
BUILDER = BRIEF_DIR / "build_pilot_briefs.py"
README = BRIEF_DIR / "README.md"
DOCS = {
    "219": ROOT / "docs" / "219_Conoco_факты.md",
    "220": ROOT / "docs" / "220_Shell_Equinor_факты.md",
}

COMPANIES = {
    "equinor": {"name": "Equinor", "cases": ("wc-101", "wc-102"), "doc": "220"},
    "shell": {"name": "Shell", "cases": ("wc-005", "wc-006"), "doc": "220"},
    "conocophillips": {"name": "ConocoPhillips", "cases": ("wc-011", "wc-012"), "doc": "219"},
}
DISCLAIMERS = ("RESEARCH_ONLY", "admission=false", "not_client=true")
SECTIONS = (
    "1. Goal",
    "2. Two pilot cases",
    "3. Public facts and sources",
    "4. Price and timing",
    "5. What we need from you",
    "6. Limits and status",
)
CONTACT = "flumos@gmail.com"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
PHONE_RE = re.compile(r"\+\d[\d\s().-]{7,}\d|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PROMISE_RE = re.compile(
    r"accur|precis|guarante|certif|world[- ]class|best[- ]in[- ]class|breakthrough|proven\b"
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def load_cases() -> dict:
    return json.loads(CASES_JSON.read_text(encoding="utf-8"))


def cases_by_id() -> dict:
    return {case["id"]: case for case in load_cases()["cases"]}


def read_md(stem: str) -> str:
    return (BRIEF_DIR / f"{stem}.md").read_text(encoding="utf-8")


def pdf_text(stem: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(BRIEF_DIR / f"{stem}.pdf"))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def flatten(text: str) -> str:
    """Lower-case and drop all whitespace, so wrapped cells still match."""
    return re.sub(r"\s+", "", text).lower()


def numbers(text: str) -> list[str]:
    """Numeric tokens with commas normalised to dots and thousands spaces removed."""
    squeezed = re.sub(r"\s+", "", DATE_RE.sub(" ", text))
    return [token.replace(",", ".") for token in re.findall(r"\d+(?:[.,]\d+)?", squeezed)]


def digits(token: str) -> str:
    return token.replace(".", "")


def sections(markdown: str) -> dict[str, str]:
    body: dict[str, str] = {}
    current = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            body[current] = []
        elif current is not None:
            body[current].append(line)
    return {key: "\n".join(lines) for key, lines in body.items()}


def table_rows(section_body: str) -> list[list[str]]:
    rows = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
            continue
        rows.append(cells)
    return rows[1:] if rows else rows  # drop the header row


def strip_flag(text: str) -> str:
    return text.replace("not_client", "").replace("NOT_CLIENT", "")


def case_facts(stem: str) -> list[dict]:
    index = cases_by_id()
    facts = []
    for case_id in COMPANIES[stem]["cases"]:
        facts.extend(index[case_id]["facts"])
    return facts


def brief_fact_rows(stem: str) -> list[list[str]]:
    return table_rows(sections(read_md(stem))["3. Public facts and sources"])


def matched_facts(stem: str) -> list[tuple[str, str, str, dict]]:
    """Pair each fact row in the brief with the ``cases.json`` fact it reproduces."""
    pool = case_facts(stem)
    pairs = []
    for label, value, source in brief_fact_rows(stem):
        tokens = tuple(numbers(value))
        for fact in pool:
            if tuple(numbers(fact["value"])) == tokens:
                pairs.append((label, value, source, fact))
                break
    return pairs


class TestBriefFiles(unittest.TestCase):
    def test_01_files_exist_and_are_not_empty(self):
        self.assertTrue(README.is_file(), "README.md is missing")
        self.assertTrue(BUILDER.is_file(), "build_pilot_briefs.py is missing")
        for stem in COMPANIES:
            for suffix in (".md", ".pdf"):
                path = BRIEF_DIR / f"{stem}{suffix}"
                self.assertTrue(path.is_file(), f"missing {path.name}")
                self.assertGreater(path.stat().st_size, 1000, f"{path.name} looks empty")
        for doc in DOCS.values():
            self.assertTrue(doc.is_file(), f"missing source fact pack: {doc.name}")

    def test_02_company_and_case_ids(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            markdown = read_md(stem)
            self.assertIn(meta["name"], markdown)
            for case_id in meta["cases"]:
                self.assertIn(case_id, markdown, f"{stem}: {case_id} missing")
                self.assertEqual(index[case_id]["company"], meta["name"])
            # the two cases of a brief are the only cases it names
            found = set(re.findall(r"wc-\d{3}", markdown))
            self.assertEqual(found, set(meta["cases"]), f"{stem} names {sorted(found)}")

    def test_03_required_sections(self):
        for stem in COMPANIES:
            body = sections(read_md(stem))
            for heading in SECTIONS:
                self.assertIn(heading, body, f"{stem}: section {heading!r} missing")
            self.assertTrue(body["3. Public facts and sources"].strip())
            self.assertTrue(body["4. Price and timing"].strip())

    def test_04_facts_count_and_sources(self):
        for stem, meta in COMPANIES.items():
            rows = table_rows(sections(read_md(stem))["3. Public facts and sources"])
            self.assertGreaterEqual(len(rows), 3, f"{stem}: too few facts")
            self.assertLessEqual(len(rows), 5, f"{stem}: too many facts")
            allowed = {fact["source_id"] for fact in case_facts(stem)}
            labels = [row[0] for row in rows]
            self.assertEqual(len(labels), len(set(labels)), f"{stem}: duplicate fact labels")
            for label, value, source in rows:
                self.assertTrue(value, f"{stem}: empty value for {label}")
                self.assertIn(source, allowed, f"{stem}: unknown source {source}")

    def test_05_fact_values_match_cases_json(self):
        for stem, meta in COMPANIES.items():
            rows = brief_fact_rows(stem)
            pairs = matched_facts(stem)
            self.assertEqual(len(pairs), len(rows),
                             f"{stem}: {len(rows) - len(pairs)} fact(s) not found in cases.json")
            for label, value, source, fact in pairs:
                self.assertTrue(numbers(value), f"{stem}: no numeric value in {label!r}")
                self.assertEqual(source, fact["source_id"],
                                 f"{stem}: {value!r} is attributed to {source}")

    def test_06_prices_and_durations_match_cases_json(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            rows = table_rows(sections(read_md(stem))["4. Price and timing"])
            prices = {}
            for scope, time_cell, price_cell in rows:
                if scope in meta["cases"]:
                    prices[scope] = (numbers(time_cell), numbers(price_cell))
            self.assertEqual(set(prices), set(meta["cases"]), f"{stem}: price rows mismatch")
            for case_id in meta["cases"]:
                times, price = prices[case_id]
                self.assertEqual(len(times), 1, f"{stem}/{case_id}: one duration expected")
                self.assertEqual(int(times[0]), index[case_id]["duration_weeks"])
                self.assertEqual(int(price[0]), index[case_id]["price_usd"])

    def test_07_package_row_is_the_sum(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            rows = table_rows(sections(read_md(stem))["4. Price and timing"])
            totals = [row for row in rows if row[0].lower().startswith("both")]
            self.assertEqual(len(totals), 1, f"{stem}: one 'Both cases' row expected")
            times, price = numbers(totals[0][1]), numbers(totals[0][2])
            expected_weeks = sum(index[case_id]["duration_weeks"] for case_id in meta["cases"])
            expected_price = sum(index[case_id]["price_usd"] for case_id in meta["cases"])
            self.assertEqual(len(times), 1)
            self.assertEqual(int(times[0]), expected_weeks)
            self.assertEqual(int(price[0]), expected_price)

    def test_08_no_invented_numbers(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            allowed = set(numbers(json.dumps([index[c] for c in meta["cases"]], ensure_ascii=False)))
            allowed = {digits(token) for token in allowed if len(digits(token)) >= 4}
            allowed.add(str(sum(index[c]["price_usd"] for c in meta["cases"])))
            used = {digits(token) for token in numbers(read_md(stem)) if len(digits(token)) >= 4}
            self.assertLessEqual(used, allowed,
                                 f"{stem}: figures missing from cases.json: {sorted(used - allowed)}")


class TestBriefCopy(unittest.TestCase):
    def test_09_no_company_contacts_or_links(self):
        for stem in COMPANIES:
            markdown = read_md(stem)
            self.assertEqual(set(EMAIL_RE.findall(markdown)), {CONTACT}, f"{stem}: extra e-mail")
            self.assertIsNone(URL_RE.search(markdown), f"{stem}: external link found")
            self.assertIsNone(PHONE_RE.search(markdown), f"{stem}: phone-like string found")
            lowered = markdown.lower()
            for word in ("linkedin", "tel:", "telephone", "contact person", "call us"):
                self.assertNotIn(word, lowered, f"{stem}: {word!r} found")
            self.assertNotIn("клиент", lowered, f"{stem}: Russian client wording found")

    def test_10_no_client_wording(self):
        for stem in COMPANIES:
            stripped = strip_flag(read_md(stem)).lower()
            self.assertNotIn("client", stripped, f"{stem}: 'client' wording outside the flag")

    def test_11_no_accuracy_or_admission_promise(self):
        for stem in COMPANIES:
            markdown = read_md(stem).lower()
            self.assertIsNone(PROMISE_RE.search(markdown),
                              f"{stem}: accuracy-like promise found")
            self.assertNotIn("admitted model", markdown)
            self.assertNotIn("guaranteed saving", markdown)
            self.assertIn("not a validated model", markdown, f"{stem}: limit sentence missing")

    def test_12_disclaimers_in_markdown(self):
        for stem in COMPANIES:
            markdown = read_md(stem)
            head = markdown.splitlines()[:3]
            for token in DISCLAIMERS:
                self.assertIn(token, markdown, f"{stem}: {token} missing")
                self.assertIn(token, "\n".join(head), f"{stem}: {token} not in the header")
            self.assertIn("hypothesis", markdown.lower())
            self.assertIn(f"mailto:{CONTACT}", markdown, f"{stem}: mailto contact missing")
            self.assertIn("nothing has been sent", markdown.lower())

    def test_13_no_revenue_or_headcount(self):
        for stem in COMPANIES:
            lowered = read_md(stem).lower()
            for word in ("revenue", "headcount", "employees", "turnover", "ebitda"):
                self.assertNotIn(word, lowered, f"{stem}: {word!r} must not appear")

    def test_14_what_we_need_from_you_has_items(self):
        for stem in COMPANIES:
            body = sections(read_md(stem))["5. What we need from you"]
            bullets = [line for line in body.splitlines() if line.strip().startswith("- ")]
            self.assertGreaterEqual(len(bullets), 4, f"{stem}: too few asks")
            self.assertLessEqual(len(bullets), 6, f"{stem}: too many asks")


class TestBriefPdf(unittest.TestCase):
    def test_15_pdf_is_one_a4_page(self):
        from pypdf import PdfReader

        for stem in COMPANIES:
            reader = PdfReader(str(BRIEF_DIR / f"{stem}.pdf"))
            self.assertEqual(len(reader.pages), 1, f"{stem}: PDF is not one page")
            box = reader.pages[0].mediabox
            self.assertAlmostEqual(float(box.width), 595.28, delta=3, msg=f"{stem}: width")
            self.assertAlmostEqual(float(box.height), 841.89, delta=3, msg=f"{stem}: height")

    def test_16_pdf_text_matches_markdown(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            text = flatten(pdf_text(stem))
            self.assertIn(flatten(meta["name"]), text)
            for case_id in meta["cases"]:
                self.assertIn(flatten(case_id), text, f"{stem}: {case_id} missing from PDF")
                self.assertIn(str(index[case_id]["price_usd"]), text)
            pairs = matched_facts(stem)
            self.assertTrue(pairs, f"{stem}: no facts matched")
            numbers_in_pdf = text.replace(".", "").replace(",", "")
            for label, value, source, fact in pairs:
                self.assertIn(flatten(fact["source_id"]), text, f"{stem}: source missing in PDF")
                for token in numbers(fact["value"]):
                    self.assertIn(digits(token), numbers_in_pdf,
                                  f"{stem}: {token} missing from PDF")

    def test_17_pdf_repeats_the_disclaimers(self):
        for stem in COMPANIES:
            text = pdf_text(stem)
            for token in DISCLAIMERS:
                self.assertIn(token, text, f"{stem}: {token} missing from PDF text")
            self.assertIn("nothinghasbeensent", flatten(text))
            self.assertNotIn("client", strip_flag(text).lower())
            # the footer repeats the flags, so the body page has them twice
            self.assertGreaterEqual(text.count("admission=false"), 2, f"{stem}: footer flag missing")

    def test_18_pdf_has_mailto_link(self):
        from pypdf import PdfReader

        for stem in COMPANIES:
            reader = PdfReader(str(BRIEF_DIR / f"{stem}.pdf"))
            page = reader.pages[0]
            uris = []
            for annot in page.get("/Annots", []) or []:
                action = annot.get_object().get("/A")
                if action and action.get("/URI"):
                    uris.append(action["/URI"])
            self.assertIn(f"mailto:{CONTACT}", uris, f"{stem}: mailto link missing")
            self.assertEqual(len(uris), 1, f"{stem}: unexpected extra links")


class TestPackDocs(unittest.TestCase):
    def test_19_readme_documents_the_pack(self):
        text = README.read_text(encoding="utf-8")
        for stem in COMPANIES:
            self.assertIn(stem, text, f"README does not mention {stem}")
        for token in ("cases.json", "219", "220", "RESEARCH_ONLY", "build_pilot_briefs.py",
                      "tests/test_pilot_briefs.py"):
            self.assertIn(token, text, f"README does not mention {token}")
        self.assertIn("отправлено", text.lower())

    def test_20_fact_packs_hold_the_same_sources(self):
        index = cases_by_id()
        for stem, meta in COMPANIES.items():
            doc = DOCS[meta["doc"]].read_text(encoding="utf-8")
            for case_id in meta["cases"]:
                for fact in index[case_id]["facts"]:
                    self.assertIn(fact["source_id"], doc,
                                  f"docs/{meta['doc']} does not list {fact['source_id']}")
                    self.assertIn(fact["value"].split()[0].replace(" ", ""),
                                  doc.replace(" ", ""),
                                  f"docs/{meta['doc']} does not list {fact['value']}")

    def test_21_builder_check_mode_passes(self):
        completed = subprocess.run(
            [sys.executable, str(BUILDER), "--check"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("1 page", completed.stdout)
        for stem in COMPANIES:
            self.assertIn(stem, completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
