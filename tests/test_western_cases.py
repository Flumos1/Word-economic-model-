#!/usr/bin/env python3
"""Tests for the 50 western commercial cases (``marketing/western_cases``).

The pack is a set of drafts: nothing is sent and nothing is committed. The tests check that

  * ``cases.json`` holds 50 cases for 25 companies and passed the catalog validator;
  * ids and questions are unique, every segment is covered, every price is inside a declared
    USD band and the duration matches that band;
  * no case carries an e-mail, phone, URL, personal name, revenue or headcount figure;
  * nothing says "клиент", "admitted", "точность" or claims an admitted model;
  * ``catalog.md`` lists all 50 cases, repeats the coverage numbers and is byte-deterministic;
  * ``catalog.pdf`` exists, is a valid PDF of 2-3 pages and matches ``manifest.json``;
  * ``--check`` verifies the manifest, ``--json`` writes nothing, ``--no-pdf`` builds without PDF;
  * an invalid catalog is refused fail-closed: exit code 1 and no files written;
  * ``docs/214`` explains usage, hypotheses and replacement, and ``pricing.md`` is USD only.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "marketing" / "western_cases"
CASES_JSON = CASES / "cases.json"
CATALOG_MD = CASES / "catalog.md"
CATALOG_PDF = CASES / "catalog.pdf"
MANIFEST = CASES / "manifest.json"
TOOL = ROOT / "tools" / "case_catalog.py"
DOCS214 = ROOT / "docs" / "214_50_западных_кейсов.md"
PRICING = ROOT / "marketing" / "pricing.md"

ALLOWED_COMPANIES = {
    "ExxonMobil", "Chevron", "Shell", "BP", "TotalEnergies", "Equinor", "ConocoPhillips", "Eni",
    "Repsol", "Suncor", "Cenovus", "Occidental", "Devon", "EOG", "Hess", "Marathon Petroleum",
    "Phillips 66", "Valero", "Cheniere", "NextDecade", "Venture Global", "Woodside", "Santos",
    "SLB", "Halliburton", "Baker Hughes", "TechnipFMC", "Saipem", "Subsea7", "Transocean",
    "Frontline", "Euronav", "Vitol", "Trafigura", "Glencore", "Gunvor", "Dow", "LyondellBasell",
    "BASF", "Borealis", "Duke Energy", "NextEra", "Enel", "Iberdrola", "RWE", "Engie", "Uniper",
    "Linde", "Air Liquide",
}
SEGMENTS = {"major", "upstream", "lng", "services", "trading", "chemicals", "utilities"}
MODULES = {"Ормуз/бюджет", "FLAKS TCO", "газ/СПГ", "геополитика", "API/Client Pack"}
REQUIRED = {
    "id", "company", "segment", "hq_region", "profile_hypothesis", "pain", "question",
    "solution", "modules", "deliverables", "duration_weeks", "price_usd", "price_note",
    "evidence_status", "not_client",
}
BANDS = ((0, 0), (7500, 10000), (15000, 25000), (20000, 30000), (25000, 80000))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)|(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)")
FIO_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:ович|евич|овна|евна|ична)\b")


def load_tool():
    spec = importlib.util.spec_from_file_location("case_catalog", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases() -> dict:
    return json.loads(CASES_JSON.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )


def all_text(case: dict) -> str:
    parts = [str(case[field]) for field in
             ("company", "hq_region", "profile_hypothesis", "pain", "question", "solution", "price_note")]
    parts += [str(item) for item in case["modules"]]
    parts += [str(item) for item in case["deliverables"]]
    return "\n".join(parts)


class TestCaseData(unittest.TestCase):
    def setUp(self):
        self.catalog = load_cases()
        self.cases = self.catalog["cases"]

    def test_01_pack_and_counts(self):
        self.assertEqual(self.catalog["schema"], "amfe.marketing.western_cases.v1")
        self.assertEqual(self.catalog["status"], "DRAFT")
        self.assertEqual(self.catalog["model_status"], "RESEARCH_ONLY")
        self.assertIs(self.catalog["admission"], False)
        self.assertIs(self.catalog["causal_claim"], False)
        self.assertIs(self.catalog["sent"], False)
        self.assertEqual(len(self.cases), 50)
        self.assertEqual(len({case["company"] for case in self.cases}), 25)
        self.assertEqual(self.catalog["cases_count"], 50)
        self.assertEqual(self.catalog["companies_count"], 25)

    def test_02_required_fields_and_types(self):
        for case in self.cases:
            self.assertTrue(REQUIRED.issubset(case), f"{case.get('id')}: {REQUIRED - set(case)}")
            self.assertIsInstance(case["company"], str)
            self.assertIsInstance(case["duration_weeks"], int)
            self.assertIsInstance(case["price_usd"], int)
            self.assertIsInstance(case["modules"], list)
            self.assertIsInstance(case["deliverables"], list)
            self.assertGreaterEqual(len(case["deliverables"]), 2)
            self.assertLessEqual(len(case["deliverables"]), 5)
            self.assertTrue(case["question"].rstrip().endswith("?"))

    def test_03_unique_ids_and_questions(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [f"wc-{index:03d}" for index in range(1, 51)])
        questions = [" ".join(case["question"].lower().split()) for case in self.cases]
        self.assertEqual(len(questions), len(set(questions)))

    def test_04_companies_pairs_and_whitelist(self):
        self.assertEqual(len(ALLOWED_COMPANIES), 49)
        for case in self.cases:
            self.assertIn(case["company"], ALLOWED_COMPANIES)
        per_company: dict[str, list[dict]] = {}
        for case in self.cases:
            per_company.setdefault(case["company"], []).append(case)
        self.assertEqual(len(per_company), 25)
        for company, group in per_company.items():
            self.assertEqual(len(group), 2, company)
            first, second = group
            self.assertEqual(first["segment"], second["segment"], company)
            self.assertNotEqual(sorted(first["modules"]), sorted(second["modules"]), company)
            self.assertNotEqual(first["id"], second["id"], company)

    def test_05_segment_coverage(self):
        counts = {segment: 0 for segment in SEGMENTS}
        for case in self.cases:
            self.assertIn(case["segment"], SEGMENTS)
            counts[case["segment"]] += 1
        for segment, count in counts.items():
            self.assertGreaterEqual(count, 2, f"{segment} has only {count} cases")
        self.assertEqual(sum(counts.values()), 50)

    def test_06_prices_and_duration_match_bands(self):
        tool = load_tool()
        band_weeks = {(low, high): (weak_low, weak_high)
                      for low, high, (weak_low, weak_high) in tool.BANDS}
        for case in self.cases:
            price = case["price_usd"]
            self.assertGreaterEqual(price, 0)
            self.assertTrue(any(low <= price <= high for low, high in BANDS), f"{case['id']}: {price}")
            allowed: set[int] = set()
            for low, high in BANDS:
                if low <= price <= high:
                    weak_low, weak_high = band_weeks[(low, high)]
                    allowed |= set(range(weak_low, weak_high + 1))
            self.assertIn(case["duration_weeks"], allowed,
                          f"{case['id']}: {price} / {case['duration_weeks']}")
            self.assertGreaterEqual(case["duration_weeks"], 1)
            self.assertLessEqual(case["duration_weeks"], 12)
        free = [case for case in self.cases if case["price_usd"] == 0]
        self.assertEqual(len(free), 2)
        for case in free:
            self.assertEqual(case["duration_weeks"], 1)
            self.assertIn("диагностика", case["price_note"].lower())

    def test_07_price_note_is_usd_and_matches(self):
        for case in self.cases:
            note = case["price_note"]
            self.assertIn("USD", note)
            spelled = f"{case['price_usd']:,}".replace(",", " ")
            self.assertIn(spelled, note, f"{case['id']}: {spelled} not in {note}")
            self.assertNotIn("RUB", note)
            self.assertNotIn("руб", note.lower())

    def test_08_modules_allowed_and_in_solution(self):
        for case in self.cases:
            self.assertTrue(case["modules"])
            self.assertLessEqual(len(case["modules"]), 3)
            for module in case["modules"]:
                self.assertIn(module, MODULES)
                self.assertIn(module, case["solution"], f"{case['id']} / {module}")
        used = {module for case in self.cases for module in case["modules"]}
        self.assertEqual(used, MODULES)

    def test_09_no_contacts_links_or_personal_names(self):
        for case in self.cases:
            for text in all_text(case).splitlines():
                self.assertIsNone(EMAIL_RE.search(text), f"{case['id']}: e-mail")
                self.assertIsNone(URL_RE.search(text), f"{case['id']}: url")
                self.assertIsNone(PHONE_RE.search(text), f"{case['id']}: phone")
                self.assertIsNone(FIO_RE.search(text), f"{case['id']}: personal name")

    def test_10_no_client_or_admission_claims(self):
        for case in self.cases:
            text = all_text(case).lower()
            for claim in ("клиент", "admitted", "точность", "сертифицирован"):
                self.assertNotIn(claim, text, f"{case['id']}: {claim}")
            self.assertIsNone(re.search(r"допущен(?!и)", text), f"{case['id']}: admission")

    def test_11_profile_has_no_revenue_or_headcount(self):
        for case in self.cases:
            self.assertFalse(any(ch.isdigit() for ch in case["profile_hypothesis"]), case["id"])
            lowered = case["profile_hypothesis"].lower()
            self.assertNotIn("выручк", lowered)
            self.assertNotIn("сотрудник", lowered)
            self.assertNotIn("персонал", lowered)

    def test_12_evidence_status_and_not_client(self):
        for case in self.cases:
            self.assertIn(case["evidence_status"], {"HYPOTHESIS", "NOT_FETCHED"})
            self.assertIs(case["not_client"], True)
        statuses = {case["evidence_status"] for case in self.cases}
        self.assertEqual(statuses, {"HYPOTHESIS", "NOT_FETCHED"})

    def test_13_validator_accepts_the_pack(self):
        tool = load_tool()
        self.assertEqual(tool.validate(self.catalog), [])

    def test_14_validator_is_fail_closed(self):
        tool = load_tool()
        base = json.loads(json.dumps(self.catalog))
        mutations = {
            "49 cases": lambda data: data["cases"].pop(),
            "duplicate id": lambda data: data["cases"][1].__setitem__("id", data["cases"][0]["id"]),
            "price out of band": lambda data: data["cases"][0].__setitem__("price_usd", 12345),
            "duration mismatch": lambda data: data["cases"][0].__setitem__("duration_weeks", 2),
            "contact": lambda data: data["cases"][0].__setitem__(
                "pain", data["cases"][0]["pain"] + " звонить на +7 999 123-45-67"),
            "client word": lambda data: data["cases"][0].__setitem__(
                "solution", data["cases"][0]["solution"] + " клиент подтвердил"),
            "admission": lambda data: data["cases"][0].__setitem__(
                "price_note", data["cases"][0]["price_note"] + " модель допущена"),
            "revenue digits": lambda data: data["cases"][0].__setitem__(
                "profile_hypothesis", data["cases"][0]["profile_hypothesis"] + " выручка 10 млрд"),
            "unknown module": lambda data: data["cases"][0]["modules"].append("магия"),
            "unknown segment": lambda data: data["cases"][0].__setitem__("segment", "banks"),
            "segment lost": lambda data: [case.__setitem__("segment", "major")
                                          for case in data["cases"] if case["segment"] == "lng"],
            "client flag": lambda data: data["cases"][0].__setitem__("not_client", False),
        }
        for label, mutate in mutations.items():
            data = json.loads(json.dumps(base))
            mutate(data)
            self.assertTrue(tool.validate(data), f"validator accepted a broken catalog: {label}")

    def test_15_invalid_file_writes_nothing(self):
        broken = json.loads(json.dumps(self.catalog))
        broken["cases"].pop()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "cases.json"
            source.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            out = tmp_path / "out"
            done = run_cli("--cases", str(source), "--out", str(out), "--no-pdf")
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertIn("< 50", done.stderr)
            self.assertFalse(out.exists())


class TestCatalogArtifacts(unittest.TestCase):
    def test_16_catalog_md_lists_all_cases(self):
        text = CATALOG_MD.read_text(encoding="utf-8")
        for case in load_cases()["cases"]:
            self.assertIn(case["id"], text)
            self.assertIn(case["company"], text)
        for heading in ("Что это", "Правила пакета", "Покрытие", "Каталог", "По компаниям",
                        "Как использовать", "Границы"):
            self.assertIn(heading, text)
        for token in ("RESEARCH_ONLY", "admission=false", "not_client=true", "не прогноз"):
            self.assertIn(token, text)

    def test_17_catalog_md_coverage_matches_data(self):
        text = CATALOG_MD.read_text(encoding="utf-8")
        cases = load_cases()["cases"]
        for segment in SEGMENTS:
            count = sum(1 for case in cases if case["segment"] == segment)
            self.assertRegex(text, rf"`{segment}` \([^)]+\) \| {count} \|")
        labels = ("0 — диагностика", "7 500-10 000 — пилот", "15 000-25 000 — полный сценарий, узкий",
                  "20 000-30 000 — полный сценарий", "25 000-80 000 — API и подписка")
        counts = {}
        for label in labels:
            match = re.search(rf"\| {re.escape(label)} \| (\d+) \|", text)
            self.assertIsNotNone(match, label)
            counts[label] = int(match.group(1))
        self.assertEqual(sum(counts.values()), 50)
        self.assertEqual(counts[labels[0]], 2)

    def test_18_catalog_md_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "one"
            second = Path(tmp) / "two"
            for target in (first, second):
                done = run_cli("--out", str(target), "--no-pdf")
                self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((first / "catalog.md").read_bytes(),
                             (second / "catalog.md").read_bytes())
            self.assertEqual((first / "manifest.json").read_bytes(),
                             (second / "manifest.json").read_bytes())
            self.assertFalse((first / "catalog.pdf").exists())
            self.assertEqual((first / "catalog.md").read_bytes(), CATALOG_MD.read_bytes())

    def test_19_pdf_is_valid_and_has_two_or_three_pages(self):
        self.assertTrue(CATALOG_PDF.is_file())
        data = CATALOG_PDF.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 20000)
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover - optional dependency
            self.skipTest("pypdf is not installed")
        reader = PdfReader(str(CATALOG_PDF))
        self.assertIn(len(reader.pages), (2, 3))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        for token in ("RESEARCH_ONLY", "admission=false", "not_client=true"):
            self.assertIn(token, text)
        self.assertIn("050", text)

    def test_20_manifest_matches_disk_and_check_passes(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "amfe.marketing.western_case_catalog.v1")
        self.assertIs(manifest["admission"], False)
        self.assertIs(manifest["rules"]["not_client"], True)
        listed = {entry["path"] for entry in manifest["files"]}
        self.assertEqual(listed, {"cases.json", "catalog.md", "catalog.pdf"})
        for entry in manifest["files"]:
            path = CASES / entry["path"]
            self.assertTrue(path.is_file(), entry["path"])
            self.assertEqual(sha256(path), entry["sha256"], entry["path"])
            self.assertEqual(path.stat().st_size, entry["bytes"], entry["path"])
        self.assertEqual(manifest["source"]["sha256"], sha256(CASES_JSON))
        self.assertEqual(manifest["source"]["cases"], 50)
        done = run_cli("--check")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("OK", done.stdout)

    def test_21_check_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "pack"
            work.mkdir()
            shutil.copy2(CASES_JSON, work / "cases.json")
            done = run_cli("--cases", str(work / "cases.json"), "--out", str(work), "--no-pdf")
            self.assertEqual(done.returncode, 0, done.stderr)
            with (work / "catalog.md").open("a", encoding="utf-8") as handle:
                handle.write("\nподмена\n")
            done = run_cli("--cases", str(work / "cases.json"), "--out", str(work), "--check")
            self.assertEqual(done.returncode, 1)
            self.assertIn("sha256 mismatch", done.stdout)

    def test_22_json_summary_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "never"
            done = run_cli("--json", "--out", str(out))
            self.assertEqual(done.returncode, 0, done.stderr)
            payload = json.loads(done.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["cases"], 50)
            self.assertEqual(payload["companies"], 25)
            self.assertEqual(payload["coverage"]["cases"], 50)
            self.assertEqual(sum(payload["coverage"]["segments"][s]["cases"] for s in SEGMENTS), 50)
            self.assertFalse(out.exists())

    def test_23_cli_help_and_no_pdf(self):
        done = run_cli("--help")
        self.assertEqual(done.returncode, 0)
        for flag in ("--check", "--json", "--out", "--no-pdf", "--cases"):
            self.assertIn(flag, done.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            done = run_cli("--out", str(out), "--no-pdf")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((out / "catalog.md").is_file())
            self.assertFalse((out / "catalog.pdf").exists())
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({entry["path"] for entry in manifest["files"]},
                             {"cases.json", "catalog.md"})
            self.assertEqual((out / "cases.json").read_bytes(), CASES_JSON.read_bytes())


class TestDocumentationAndPricing(unittest.TestCase):
    def test_24_docs214_sections(self):
        self.assertTrue(DOCS214.is_file())
        text = DOCS214.read_text(encoding="utf-8")
        for token in ("Как использовать", "гипотез", "заменить", "Цены", "RESEARCH_ONLY",
                      "not_client", "HYPOTHESIS", "NOT_FETCHED", "case_catalog.py",
                      "25 компаний", "50 кейсов"):
            self.assertIn(token, text)
        for token in ("admitted", "точность выше рынка"):
            self.assertNotIn(token, text)

    def test_25_pricing_is_usd_only(self):
        text = PRICING.read_text(encoding="utf-8")
        self.assertIn("USD", text)
        for band in ("7 500", "10 000", "15 000", "25 000", "20 000", "30 000", "80 000"):
            self.assertIn(band, text)
        for forbidden in ("RUB", "₽", "руб."):
            self.assertNotIn(forbidden, text)
        # a truncated leading "000-30 000" means a lost thousands digit
        self.assertIsNone(re.search(r"(?<![\d ])000[–-]\d", text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
