#!/usr/bin/env python3
"""Validate the 102 western commercial cases (51 companies x 2) and build both catalogs.

Source of truth is ``marketing/western_cases/cases.json``: 102 bilingual cases (Russian and
English fields) for 51 western companies, two cases per company. The tool validates it
fail-closed, then renders the Russian ``catalog.md`` + 4-6 page ``catalog.pdf`` and the English
``catalog_en.md`` + 4-6 page ``catalog_en.pdf``, plus ``manifest.json`` with SHA-256 for every
file and the page count of both PDFs.

Nothing is sent and nothing is committed: the tool only writes files. Run from the repo root::

    python tools/case_catalog.py                 # 4 files + manifest.json
    python tools/case_catalog.py --no-pdf        # catalog.md + catalog_en.md + manifest.json
    python tools/case_catalog.py --check         # verify manifest, sha256, sizes, pages; write nothing
    python tools/case_catalog.py --json          # summary in stdout, write nothing
    python tools/case_catalog.py --out build/western_cases_demo --no-pdf

Rules baked into the pack:
  * every case is a hypothesis (``not_client=true``), never a client record;
  * prices are USD only and must fall into one of the declared bands;
  * duration must match the price band (a diagnostic is one week, a pilot 2-4);
  * no e-mail, phone, URL, personal name, revenue or headcount anywhere, in either language;
  * percent figures are only the ones traced to docs 186/188/190 (``SOURCED_PERCENT``);
  * nothing claims an admitted model, a client relationship or an "accuracy" figure;
  * the Russian and English fields describe the same case: no new facts in translation;
  * ``catalog.md`` and ``catalog_en.md`` are byte-deterministic, the PDFs are not;
  * an invalid catalog writes no files at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "marketing" / "western_cases" / "cases.json"
DEFAULT_OUT = ROOT / "marketing" / "western_cases"
DOCX_DIR = ROOT / "build" / "western_cases"
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")

SCHEMA = "amfe.marketing.western_case_catalog.v2"
CREATED = "2026-11-21"
PDF_PAGES = (4, 6)
MIN_CASES = 102
COMPANIES_COUNT = 51
CASES_PER_COMPANY = 2

SEGMENTS = (
    "major", "upstream", "lng", "services", "trading", "chemicals",
    "utilities", "industrials", "pipelines",
)
SEGMENT_RU = {
    "major": "мейджор",
    "upstream": "добыча",
    "lng": "СПГ",
    "services": "сервис",
    "trading": "трейдинг",
    "chemicals": "химия",
    "utilities": "энергетика",
    "industrials": "промышленность",
    "pipelines": "трубопроводы",
}
SEGMENT_EN = {
    "major": "integrated major",
    "upstream": "upstream",
    "lng": "LNG",
    "services": "services",
    "trading": "trading",
    "chemicals": "chemicals",
    "utilities": "utilities",
    "industrials": "industrials",
    "pipelines": "pipelines",
}
MODULES = ("Ормуз/бюджет", "FLAKS TCO", "газ/СПГ", "геополитика", "API/Client Pack")
MODULE_EN = {
    "Ормуз/бюджет": "Hormuz/budget",
    "FLAKS TCO": "FLAKS TCO",
    "газ/СПГ": "gas/LNG",
    "геополитика": "geopolitics",
    "API/Client Pack": "API/Client Pack",
}

# (low, high, allowed weeks) — the spec bands overlap on purpose (20 000-25 000 USD).
BANDS = (
    (0, 0, (1, 1)),
    (7500, 10000, (2, 4)),
    (15000, 25000, (4, 6)),
    (20000, 30000, (8, 12)),
    (25000, 80000, (6, 12)),
)
BAND_LABELS = (
    (0, 0, "0 — диагностика"),
    (7500, 10000, "7 500-10 000 — пилот"),
    (15000, 25000, "15 000-25 000 — полный сценарий, узкий"),
    (20000, 30000, "20 000-30 000 — полный сценарий"),
    (25000, 80000, "25 000-80 000 — API и подписка"),
)
BAND_LABELS_EN = (
    (0, 0, "0 — diagnostic"),
    (7500, 10000, "7 500-10 000 — pilot"),
    (15000, 25000, "15 000-25 000 — full scenario, narrow"),
    (20000, 30000, "20 000-30 000 — full scenario"),
    (25000, 80000, "25 000-80 000 — API and subscription"),
)

REQUIRED_TEXT = ("company", "hq_region", "profile_hypothesis", "pain", "question", "solution")
REQUIRED_FIELDS = (
    "id", "company", "segment", "hq_region", "profile_hypothesis", "pain", "question",
    "solution", "modules", "deliverables", "duration_weeks", "price_usd", "price_note",
    "evidence_status", "not_client",
)
EN_FIELDS = (
    "title_en", "pain_en", "question_en", "solution_en", "deliverables_en", "price_note_en",
    "hq_region_en",
)
EVIDENCE = ("HYPOTHESIS", "NOT_FETCHED")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)|(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)")
FIO_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:ович|евич|овна|евна|ична)\b")
FORBIDDEN_CLAIMS = ("клиент", "admitted", "точность", "сертифицирован")
FORBIDDEN_EN = ("admitted", "accuracy", "certified", "guarantee", "guaranteed")
# No revenue, headcount or money-magnitude claims in any language.
MONEY_WORDS = ("млрд", "млн", "трлн", "billion", "million", "trillion", "выручк",
               "revenue", "headcount", "сотрудник", "персонал", "штат")
# Percent figures are allowed only when the number is traceable to docs 186/188/190.
SOURCED_PERCENT = frozenset({"112", "177", "24,0"})
PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
CLIENT_WORD_RE = re.compile(r"\bclients?\b", re.IGNORECASE)
ADMISSION_RE = re.compile(r"допущен(?!и)")
ID_RE = re.compile(r"^wc-\d{3}$")

DISCLAIMER_RU = (
    "AMFE · RESEARCH_ONLY · admission=false · not_client=true · гипотезы, не данные компаний · "
    "не прогноз и не инвестиционная рекомендация"
)
DISCLAIMER_EN = (
    "AMFE · RESEARCH_ONLY · admission=false · not_client=true · hypotheses, not company data · "
    "not a forecast and not an investment recommendation"
)


class CatalogError(RuntimeError):
    """Raised when the catalog cannot be read or written (exit code 2)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fmt_usd(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def load_cases(path: Path = DEFAULT_CASES) -> dict:
    if not path.is_file():
        raise CatalogError(f"cases file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CatalogError(f"cases file is not valid JSON: {path} ({error})") from error
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise CatalogError(f"cases file has no 'cases' list: {path}")
    return data


def _allowed_weeks(price: int) -> set[int]:
    allowed: set[int] = set()
    for low, high, weeks in BANDS:
        if low <= price <= high:
            allowed.update(range(weeks[0], weeks[1] + 1))
    return allowed


def band_of(price: int, weeks: int | None = None) -> str:
    """Pick the band by price and duration, narrowest matching band first.

    The declared bands overlap in 20 000-25 000 USD, so the duration is used as the
    tie-breaker: a 10-week engagement is a full scenario, not a 4-6 week one.
    """
    candidates = []
    for index, (low, high, band_weeks) in enumerate(BANDS):
        if not low <= price <= high:
            continue
        if weeks is not None and not band_weeks[0] <= weeks <= band_weeks[1]:
            continue
        candidates.append((high - low, index))
    if not candidates:
        return "вне полос"
    index = min(candidates)[1]
    return BAND_LABELS[index][2]


def band_index(price: int, weeks: int) -> int | None:
    for index, (low, high, band_weeks) in enumerate(BANDS):
        if low <= price <= high and band_weeks[0] <= weeks <= band_weeks[1]:
            return index
    return None


def _iter_strings(case: dict, lang: str = "ru") -> list[str]:
    if lang == "ru":
        values = [str(case.get(field, "")) for field in REQUIRED_TEXT]
        values.extend(str(item) for item in case.get("modules", []))
        values.extend(str(item) for item in case.get("deliverables", []))
        values.append(str(case.get("price_note", "")))
        return values
    values = [str(case.get(field, "")) for field in
              ("company", "hq_region_en", "title_en", "pain_en", "question_en", "solution_en")]
    values.extend(str(case.get("deliverables_en", [])))
    values.append(str(case.get("price_note_en", "")))
    return values


def _unsourced_percent(text: str) -> list[str]:
    return [match.group(1) for match in PERCENT_RE.finditer(text)
            if match.group(1) not in SOURCED_PERCENT]


def _check_text(label: str, text: str, lang: str, problems: list[str]) -> None:
    """Contact, name, claim and fake-number checks for one text field."""
    if EMAIL_RE.search(text) or URL_RE.search(text) or PHONE_RE.search(text):
        problems.append(f"{label}: contact or link detected in text")
    if FIO_RE.search(text):
        problems.append(f"{label}: personal name detected in text")
    lowered = text.lower()
    if lang == "ru":
        for claim in FORBIDDEN_CLAIMS:
            if claim in lowered:
                problems.append(f"{label}: forbidden claim {claim!r} in text")
        if ADMISSION_RE.search(lowered):
            problems.append(f"{label}: admission claim in text")
    else:
        if CLIENT_WORD_RE.search(text.replace("Client Pack", "")):
            problems.append(f"{label}: client claim in text")
        for claim in FORBIDDEN_EN:
            if claim in lowered:
                problems.append(f"{label}: forbidden claim {claim!r} in text")
    for word in MONEY_WORDS:
        if word in lowered:
            problems.append(f"{label}: revenue/headcount figure word {word!r} in text")
    bad = _unsourced_percent(text)
    if bad:
        problems.append(f"{label}: unsourced percent figure(s) {bad}")


FACT_REQUIRED = ("label", "value", "source_id", "source_date", "evidence")
FACT_EVIDENCE = ("A", "B", "C")


def _validate_facts(case: dict, label: str, problems: list[str]) -> None:
    """Facts are optional, but when present they must be sourced and number-safe."""
    facts = case.get("facts")
    if facts is None:
        return
    if not isinstance(facts, list) or not (1 <= len(facts) <= 12):
        problems.append(f"{label}: facts must be a list of 1-12 items")
        return
    for index, fact in enumerate(facts, 1):
        item = f"{label}: facts[{index}]"
        if not isinstance(fact, dict):
            problems.append(f"{item}: must be an object")
            continue
        missing = [field for field in FACT_REQUIRED if field not in fact]
        if missing:
            problems.append(f"{item}: missing {missing}")
            continue
        for field in ("label", "value", "source_id", "source_date"):
            if not isinstance(fact[field], str) or not fact[field].strip():
                problems.append(f"{item}: {field} must be a non-empty string")
        if not re.match(r"^SRC-[A-Z0-9-]{3,40}$", str(fact["source_id"])):
            problems.append(f"{item}: bad source_id")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(fact["source_date"])):
            problems.append(f"{item}: source_date must be YYYY-MM-DD")
        if fact["evidence"] not in FACT_EVIDENCE:
            problems.append(f"{item}: evidence must be one of {FACT_EVIDENCE}")
        for field in ("label", "value"):
            text = str(fact[field])
            if EMAIL_RE.search(text) or URL_RE.search(text) or PHONE_RE.search(text):
                problems.append(f"{item}: contact/link in {field}")
            lowered = text.lower()
            for word in MONEY_WORDS:
                if word in lowered:
                    problems.append(f"{item}: revenue/headcount word {word!r} in {field}")
            bad = _unsourced_percent(text)
            if bad:
                problems.append(f"{item}: unsourced percent {bad}")


def _validate_english(case: dict, label: str, price, weeks, seen_en_questions: dict,
                      problems: list[str]) -> None:
    for field in EN_FIELDS:
        if field not in case:
            problems.append(f"{label}: missing English field {field}")
    if any(field not in case for field in EN_FIELDS):
        return

    for field in ("title_en", "pain_en", "question_en", "solution_en", "hq_region_en"):
        value = case[field]
        minimum = 8 if field == "title_en" else (2 if field == "hq_region_en" else 12)
        if not isinstance(value, str) or len(value.strip()) < minimum:
            problems.append(f"{label}: {field} must be a text of at least {minimum} characters")

    question = case["question_en"]
    if isinstance(question, str):
        if not question.rstrip().endswith("?"):
            problems.append(f"{label}: question_en must end with '?'")
        if len(question) > 240:
            problems.append(f"{label}: question_en is longer than 240 characters")
        key = " ".join(question.lower().split())
        if key in seen_en_questions:
            problems.append(
                f"{label}: duplicate question_en (same as {seen_en_questions[key]})")
        else:
            seen_en_questions[key] = case.get("id", label)

    deliverables = case["deliverables_en"]
    if not isinstance(deliverables, list) or not 2 <= len(deliverables) <= 5:
        problems.append(f"{label}: deliverables_en must be a list of 2-5 items")
    elif any(not isinstance(item, str) or len(item.strip()) < 6 for item in deliverables):
        problems.append(f"{label}: every deliverable_en must be a text of at least 6 characters")

    modules = case.get("modules") or []
    for module in modules:
        expected = MODULE_EN.get(str(module))
        if expected and isinstance(case["solution_en"], str) and expected not in case["solution_en"]:
            problems.append(f"{label}: solution_en does not mention module {expected!r}")

    if isinstance(price, int) and not isinstance(price, bool):
        note = case["price_note_en"]
        if isinstance(note, str):
            if fmt_usd(price) not in note:
                problems.append(
                    f"{label}: price_note_en must spell the price as {fmt_usd(price)}")
            if "USD" not in note:
                problems.append(f"{label}: price_note_en must be in USD")
            if "RUB" in note or "руб" in note.lower():
                problems.append(f"{label}: price_note_en must not mention RUB")

    for text in _iter_strings(case, "en"):
        _check_text(label, text, "en", problems)


def validate(catalog: dict) -> list[str]:
    """Return a sorted list of problems; empty list means the catalog is valid."""
    problems: list[str] = []

    if catalog.get("schema") != "amfe.marketing.western_cases.v1":
        problems.append("schema must be amfe.marketing.western_cases.v1")
    if catalog.get("model_status") != "RESEARCH_ONLY":
        problems.append("model_status must be RESEARCH_ONLY")
    if catalog.get("admission") is not False:
        problems.append("admission must be false")
    if catalog.get("causal_claim") is not False:
        problems.append("causal_claim must be false")
    if catalog.get("sent") is not False:
        problems.append("sent must be false")
    if catalog.get("status") != "DRAFT":
        problems.append("status must be DRAFT")
    if catalog.get("languages") != ["ru", "en"]:
        problems.append("languages must be ['ru', 'en']")
    rules = catalog.get("rules") or {}
    for key in ("not_client", "evidence_is_hypothesis", "no_contacts",
                "no_revenue_or_headcount", "no_admission_claim", "price_in_usd_only",
                "english_fields"):
        if rules.get(key) is not True:
            problems.append(f"rules.{key} must be true")

    cases = catalog.get("cases")
    if not isinstance(cases, list):
        return problems + ["cases must be a list"]
    if len(cases) != MIN_CASES:
        problems.append(f"cases: {len(cases)} != {MIN_CASES}")
    declared = catalog.get("cases_count")
    if declared != len(cases):
        problems.append(f"cases_count {declared!r} != {len(cases)} cases")

    seen_ids: set[str] = set()
    seen_questions: dict[str, str] = {}
    seen_en_questions: dict[str, str] = {}
    seen_per_company: dict[str, list[set[str]]] = {}
    companies: set[str] = set()
    by_segment: dict[str, int] = {segment: 0 for segment in SEGMENTS}
    by_module: dict[str, int] = {module: 0 for module in MODULES}

    for position, case in enumerate(cases):
        label = case.get("id") if isinstance(case, dict) else None
        label = label or f"case#{position + 1}"
        if not isinstance(case, dict):
            problems.append(f"{label}: case must be an object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in case]
        for field in missing:
            problems.append(f"{label}: missing field {field}")
        if missing:
            continue

        case_id = case["id"]
        if not isinstance(case_id, str) or not ID_RE.match(case_id):
            problems.append(f"{label}: id must look like wc-001")
        if case_id in seen_ids:
            problems.append(f"{label}: duplicate id")
        seen_ids.add(case_id)
        if case_id != f"wc-{position + 1:03d}":
            problems.append(f"{label}: id is out of sequence at position {position + 1}")

        company = case["company"]
        if not isinstance(company, str) or not company.strip():
            problems.append(f"{label}: company must be a non-empty string")
            continue
        companies.add(company)

        segment = case["segment"]
        if segment not in SEGMENTS:
            problems.append(f"{label}: unknown segment {segment!r}")
        else:
            by_segment[segment] += 1

        for field in REQUIRED_TEXT:
            value = case[field]
            minimum = 2 if field in {"company", "hq_region"} else 12
            if not isinstance(value, str) or len(value.strip()) < minimum:
                problems.append(
                    f"{label}: {field} must be a text of at least {minimum} characters")

        question = case["question"]
        if isinstance(question, str):
            if not question.rstrip().endswith("?"):
                problems.append(f"{label}: question must end with '?'")
            if len(question) > 240:
                problems.append(f"{label}: question is longer than 240 characters")
            key = " ".join(question.lower().split())
            if key in seen_questions:
                problems.append(f"{label}: duplicate question (same as {seen_questions[key]})")
            else:
                seen_questions[key] = case_id

        if any(character.isdigit() for character in case["profile_hypothesis"]):
            problems.append(f"{label}: profile_hypothesis must not contain numbers "
                            "(no revenue, no headcount)")

        modules = case["modules"]
        if not isinstance(modules, list) or not modules:
            problems.append(f"{label}: modules must be a non-empty list")
            modules = []
        if len(modules) > 3:
            problems.append(f"{label}: at most 3 modules per case")
        if len({str(item) for item in modules}) != len(modules):
            problems.append(f"{label}: duplicate module in one case")
        for module in modules:
            if module not in MODULES:
                problems.append(f"{label}: unknown module {module!r}")
            else:
                by_module[module] += 1
            if isinstance(case["solution"], str) and str(module) not in case["solution"]:
                problems.append(f"{label}: solution text does not mention module {module!r}")

        deliverables = case["deliverables"]
        if not isinstance(deliverables, list) or not 2 <= len(deliverables) <= 5:
            problems.append(f"{label}: deliverables must be a list of 2-5 items")
        elif any(not isinstance(item, str) or len(item.strip()) < 6 for item in deliverables):
            problems.append(f"{label}: every deliverable must be a text of at least 6 characters")

        weeks = case["duration_weeks"]
        if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= 12:
            problems.append(f"{label}: duration_weeks must be an integer 1-12")

        price = case["price_usd"]
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            problems.append(f"{label}: price_usd must be a non-negative integer")
        else:
            if not any(low <= price <= high for low, high, _ in BANDS):
                problems.append(f"{label}: price {price} USD is outside the declared bands")
            elif isinstance(weeks, int) and weeks not in _allowed_weeks(price):
                problems.append(
                    f"{label}: duration {weeks} weeks does not match the price band of {price} USD"
                )
            note = case.get("price_note", "")
            if isinstance(note, str):
                if fmt_usd(price) not in note:
                    problems.append(f"{label}: price_note must spell the price as {fmt_usd(price)}")
                if "USD" not in note:
                    problems.append(f"{label}: price_note must be in USD")

        if case["evidence_status"] not in EVIDENCE:
            problems.append(f"{label}: evidence_status must be one of {EVIDENCE}")
        if case["not_client"] is not True:
            problems.append(f"{label}: not_client must be true")

        for text in _iter_strings(case):
            _check_text(label, text, "ru", problems)

        _validate_english(case, label, price, weeks, seen_en_questions, problems)
        _validate_facts(case, label, problems)

        module_key = frozenset(str(item) for item in modules)
        seen_per_company.setdefault(company, []).append(module_key)

    if len(companies) != COMPANIES_COUNT:
        problems.append(f"companies: {len(companies)} != {COMPANIES_COUNT}")
    declared_companies = catalog.get("companies_count")
    if declared_companies != len(companies):
        problems.append(f"companies_count {declared_companies!r} != {len(companies)} companies")

    for company, groups in sorted(seen_per_company.items()):
        if len(groups) != CASES_PER_COMPANY:
            problems.append(
                f"{company}: {len(groups)} cases, exactly {CASES_PER_COMPANY} required")
        for index, group in enumerate(groups):
            if group in groups[:index]:
                problems.append(f"{company}: duplicate module set {sorted(group)}")

    for segment in SEGMENTS:
        if by_segment[segment] < 2:
            problems.append(f"segment coverage: {segment} has {by_segment[segment]} cases, need >= 2")
    for module in MODULES:
        if by_module[module] < 1:
            problems.append(f"module coverage: {module} is not used by any case")

    for index, (_, _, label) in enumerate(BAND_LABELS):
        if not any(band_index(case["price_usd"], case["duration_weeks"]) == index for case in cases):
            problems.append(f"price band coverage: {label} has no cases")

    return sorted(set(problems))


def coverage(catalog: dict) -> dict:
    cases = catalog["cases"]
    segments: dict[str, dict[str, object]] = {}
    for segment in SEGMENTS:
        selected = [case for case in cases if case["segment"] == segment]
        segments[segment] = {
            "cases": len(selected),
            "companies": len({case["company"] for case in selected}),
        }
    modules_ru = {module: 0 for module in MODULES}
    modules_en = {MODULE_EN[module]: 0 for module in MODULES}
    bands_ru = {label: 0 for _, _, label in BAND_LABELS}
    evidence = {status: 0 for status in EVIDENCE}
    prices: dict[str, int] = {}
    for case in cases:
        for module in case["modules"]:
            modules_ru[str(module)] += 1
            modules_en[MODULE_EN[str(module)]] += 1
        index = band_index(case["price_usd"], case["duration_weeks"])
        if index is not None:
            bands_ru[BAND_LABELS[index][2]] += 1
        evidence[case["evidence_status"]] += 1
        key = fmt_usd(case["price_usd"])
        prices[key] = prices.get(key, 0) + 1
    return {
        "cases": len(cases),
        "companies": len({case["company"] for case in cases}),
        "segments": segments,
        "modules": dict(modules_ru),
        "modules_en": dict(modules_en),
        "price_bands": dict(bands_ru),
        "price_points": dict(sorted(prices.items(), key=lambda item: int(item[0].replace(" ", "")))),
        "evidence_status": dict(evidence),
        "english": {
            "cases_with_en": sum(1 for case in cases if all(field in case for field in EN_FIELDS)),
            "fields": len(EN_FIELDS),
        },
        "duration_weeks": {
            "min": min(case["duration_weeks"] for case in cases),
            "max": max(case["duration_weeks"] for case in cases),
        },
        "price_usd": {
            "min": min(case["price_usd"] for case in cases),
            "max": max(case["price_usd"] for case in cases),
            "free": sum(1 for case in cases if case["price_usd"] == 0),
        },
    }


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space >= limit - 25:  # cut on a word boundary instead of mid-word
        head = head[:space]
    return head.rstrip(" ,;:-") + "…"



def _fact_rows(cases: list[dict], lang: str) -> list[list[str]]:
    """Sourced numeric facts for the appendix; no URLs or contacts, only source ids."""
    rows: list[list[str]] = []
    for case in cases:
        for fact in case.get("facts", []):
            label = fact.get("label", "")
            value = fact.get("value", "")
            source = fact.get("source_id", "")
            rows.append([case["id"], case["company"], label, value, source])
    return rows


def _module_labels(case: dict, lang: str) -> list[str]:
    if lang == "en":
        return [MODULE_EN[str(module)] for module in case["modules"]]
    return [str(module) for module in case["modules"]]


def render_markdown(catalog: dict, source_sha: str, lang: str = "ru") -> str:
    cases = sorted(catalog["cases"], key=lambda case: case["id"])
    stats = coverage(catalog)
    if lang == "en":
        return _render_markdown_en(catalog, cases, stats, source_sha)
    return _render_markdown_ru(catalog, cases, stats, source_sha)


def _render_markdown_ru(catalog: dict, cases: list[dict], stats: dict, source_sha: str) -> str:
    lines: list[str] = [
        "# Каталог 102 западных кейсов",
        "",
        f"Собрано: {CREATED} · статус DRAFT · `RESEARCH_ONLY` · `admission=false` · "
        "`not_client=true` · ничего не отправлено",
        "",
        f"Источник: `marketing/western_cases/cases.json` · SHA-256 `{source_sha}` · "
        f"{stats['cases']} кейсов, {stats['companies']} компаний · русская и английская версии",
        "",
        "Каталог генерируется `tools/case_catalog.py` из `cases.json`; руками не правится.",
        "Английская версия тех же кейсов — `catalog_en.md` и `catalog_en.pdf`.",
        "",
        "## 1. Что это",
        "",
        "102 кратких коммерческих кейса для западных корпораций: 51 компания по 2 кейса и",
        "9 сегментов. Каждый кейс — **гипотеза** о боли и вопросе, который эта роль могла бы",
        "задать, и о том, какие наши модули её закрывают. Это не описание компании, не КП, не",
        "аудит и не прогноз: `evidence_status` у каждого кейса — `HYPOTHESIS` или `NOT_FETCHED`,",
        "ни одного факта о финансах, штате или контактах внутри нет. Английские поля — перевод и",
        "адаптация тех же формулировок, без новых фактов и цифр.",
        "",
        "## 2. Правила пакета",
        "",
        "- `not_client=true` — ни один кейс не является клиентским, ни один не согласован и не отправлен.",
        "- `profile_hypothesis` — только предположение о роли и контуре решений, без выручки, штата и контактов.",
        "- `pain` и `question` — гипотеза о боли; проверяется на discovery, а не выдаётся за факт.",
        "- Цены — только USD и только из объявленных полос; срок должен соответствовать полосе.",
        "- Английские поля (`title_en`, `pain_en`, `question_en`, `solution_en`, `deliverables_en`,",
        "  `price_note_en`, `hq_region_en`) описывают тот же кейс; расхождений с русским текстом нет.",
        "- `RESEARCH_ONLY`, `admission=false`: это не допуск модели и не инвестиционная рекомендация.",
        "- PDF не байт-детерминирован (метаданные LibreOffice), `catalog.md` и `catalog_en.md` — детерминированы.",
        "",
        "## 3. Покрытие",
        "",
        "### Сегменты",
        "",
        "| Сегмент | Кейсов | Компаний |",
        "|---|---:|---:|",
    ]
    for segment in SEGMENTS:
        entry = stats["segments"][segment]
        lines.append(f"| `{segment}` ({SEGMENT_RU[segment]}) | {entry['cases']} | {entry['companies']} |")
    lines += [
        "",
        "### Модули",
        "",
        "| Модуль | Кейсов |",
        "|---|---:|",
    ]
    for module in MODULES:
        lines.append(f"| {module} | {stats['modules'][module]} |")
    lines += [
        "",
        "### Ценовые полосы, USD",
        "",
        "| Полоса | Кейсов |",
        "|---|---:|",
    ]
    for _, _, label in BAND_LABELS:
        lines.append(f"| {label} | {stats['price_bands'][label]} |")
    lines += [
        "",
        "Полосы перекрываются в диапазоне 20 000-25 000 USD. Кейс относится к самой узкой",
        "подходящей полосе, а срок выбирает полосу при равной ширине, поэтому сумма по строкам",
        "равна числу кейсов.",
        "",
        "### Статус доказательств",
        "",
        "| `evidence_status` | Кейсов | Что значит |",
        "|---|---:|---|",
        f"| `HYPOTHESIS` | {stats['evidence_status']['HYPOTHESIS']} | боль и вопрос выведены из "
        "публичной роли компании, внешние данные не забирались |",
        f"| `NOT_FETCHED` | {stats['evidence_status']['NOT_FETCHED']} | для содержательного ответа "
        "нужны внешние данные, которые в этой сессии не забирались |",
        "",
        "### Сроки, цены и языки",
        "",
        f"- срок: {stats['duration_weeks']['min']}-{stats['duration_weeks']['max']} недель;",
        f"- цена: {fmt_usd(stats['price_usd']['min'])}-{fmt_usd(stats['price_usd']['max'])} USD, "
        f"бесплатных диагностик — {stats['price_usd']['free']};",
        f"- английские поля заполнены у {stats['english']['cases_with_en']} кейсов "
        f"({stats['english']['fields']} полей на кейс);",
        "- цены — ориентиры для пилота, подтверждаются на discovery, без скидок «на глаз».",
        "",
        "## 4. Каталог",
        "",
        "| # | Компания | Сегмент | Вопрос | Модули | Нед. | USD |",
        "|---:|---|---|---|---|---:|---:|",
    ]
    for case in cases:
        lines.append(
            "| {num} | {company} | `{segment}` | {question} | {modules} | {weeks} | {price} |".format(
                num=case["id"].replace("wc-", ""),
                company=case["company"],
                segment=case["segment"],
                question=case["question"].replace("|", "/"),
                modules=", ".join(_module_labels(case, "ru")),
                weeks=case["duration_weeks"],
                price=fmt_usd(case["price_usd"]),
            )
        )
    lines += ["", "## 5. По компаниям", ""]
    by_company: dict[str, list[dict]] = {}
    for case in cases:
        by_company.setdefault(case["company"], []).append(case)
    for company in sorted(by_company, key=lambda name: by_company[name][0]["id"]):
        group = by_company[company]
        head = group[0]
        items = "; ".join(
            f"`{case['id']}` — {case['question']} ({', '.join(_module_labels(case, 'ru'))})"
            for case in group
        )
        lines.append(f"- **{company}** (`{head['segment']}`, {head['hq_region']}): {items}")

    fact_rows = _fact_rows(cases, "ru")
    if fact_rows:
        lines += [
            "",
            "## 5.1 Факт-паки: числа с источниками",
            "",
            "Это не гипотезы: числа взяты из открытых годовых отчётов и проверяются по `source_id`.",
            "Они не делают боль кейса подтверждённой и не являются клиентскими данными.",
            "",
            "| ID | Компания | Факт | Значение | Источник |",
            "|---|---|---|---|---|",
        ]
        for row in fact_rows:
            lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        "## 6. Как использовать",
        "",
        "1. Выбрать сегмент и одну боль — не показывать каталог целиком: 102 кейса читаются как",
        "   прайс, один кейс — как разбор задачи.",
        "2. Проверить гипотезу на discovery: совпадает ли роль, контур решений и срок бюджета.",
        "3. Заменить `profile_hypothesis`, `pain` и `question` на то, что сказано на созвоне, и",
        "   поставить `evidence_status=NOT_FETCHED` до появления публичного подтверждения.",
        "4. Цену и срок брать из полосы; после диагностики фиксировать одну цифру и записать её в",
        "   `price_note` вместе с объёмом работ.",
        "5. Собирать каталог только через `python tools/case_catalog.py`; манифест и PDF не правятся руками.",
        "",
        "## 7. Границы",
        "",
        "- Все 102 кейса — гипотезы; ни один не проверен на компании и не является КП.",
        "- Нет ни одного контакта, телефона, адреса и персонального имени — и не должно быть.",
        "- Нет цифр выручки, штата и доли рынка: такие утверждения требуют источника и проверки.",
        "- Проценты в вопросах — только те, что прослеживаются к docs/186, docs/188 и docs/190.",
        "- `NOT_FETCHED` — честная пометка: внешние данные (фрахт, TARIC, отраслевые ряды) не забирались.",
        "- Модели `RESEARCH_ONLY`, `admission=false`: числа из модулей — сценарии на объявленных",
        "  допущениях, а не прогноз и не вероятность.",
        "- Отправка и договор — только человеком; агент не отправляет и не коммитит.",
        "",
        DISCLAIMER_RU,
        "",
    ]
    return "\n".join(lines)


def _render_markdown_en(catalog: dict, cases: list[dict], stats: dict, source_sha: str) -> str:
    lines: list[str] = [
        "# 102 Western Cases: hypothesis catalog, prices and rules",
        "",
        f"Built: {CREATED} · status DRAFT · `RESEARCH_ONLY` · `admission=false` · "
        "`not_client=true` · nothing sent",
        "",
        f"Source: `marketing/western_cases/cases.json` · SHA-256 `{source_sha}` · "
        f"{stats['cases']} cases, {stats['companies']} companies · Russian and English versions",
        "",
        "Generated by `tools/case_catalog.py` from `cases.json`; never edited by hand.",
        "Russian version of the same cases: `catalog.md` and `catalog.pdf`.",
        "",
        "## 1. What this is",
        "",
        "102 short commercial cases for western corporations: 51 companies with two cases each,",
        "across 9 segments. Every case is a **hypothesis** about a pain, the question that role",
        "might ask, and the modules that would answer it. It is not a company profile, not an",
        "offer, not an audit and not a forecast: every `evidence_status` is `HYPOTHESIS` or",
        "`NOT_FETCHED`, and no revenue, headcount or contact data is included. The English fields",
        "are a translation and adaptation of the same text, with no new facts or figures.",
        "",
        "## 2. Pack rules",
        "",
        "- `not_client=true`: no case is a client record, none is agreed and none is sent.",
        "- `profile_hypothesis` is a guess about the role and the decision loop, with no revenue or headcount.",
        "- `pain` and `question` are hypotheses; they are tested on discovery, never presented as facts.",
        "- Prices are USD only and must come from the declared bands; duration must match the band.",
        "- English fields (`title_en`, `pain_en`, `question_en`, `solution_en`, `deliverables_en`,",
        "  `price_note_en`, `hq_region_en`) describe the same case; they never diverge from the Russian text.",
        "- `RESEARCH_ONLY`, `admission=false`: this is not a model admission and not investment advice.",
        "- PDFs are not byte-deterministic (LibreOffice metadata); the Markdown files are.",
        "",
        "## 3. Coverage",
        "",
        "### Segments",
        "",
        "| Segment | Cases | Companies |",
        "|---|---:|---:|",
    ]
    for segment in SEGMENTS:
        entry = stats["segments"][segment]
        lines.append(f"| `{segment}` ({SEGMENT_EN[segment]}) | {entry['cases']} | {entry['companies']} |")
    lines += [
        "",
        "### Modules",
        "",
        "| Module | Cases |",
        "|---|---:|",
    ]
    for module in MODULES:
        lines.append(f"| {MODULE_EN[module]} | {stats['modules_en'][MODULE_EN[module]]} |")
    lines += [
        "",
        "### Price bands, USD",
        "",
        "| Band | Cases |",
        "|---|---:|",
    ]
    for index, (_, _, _label) in enumerate(BAND_LABELS):
        lines.append(f"| {BAND_LABELS_EN[index][2]} | {stats['price_bands'][BAND_LABELS[index][2]]} |")
    lines += [
        "",
        "Bands overlap in the 20 000-25 000 USD range. A case belongs to the narrowest matching",
        "band, and duration breaks ties, so the rows add up to the number of cases.",
        "",
        "### Evidence status",
        "",
        "| `evidence_status` | Cases | Meaning |",
        "|---|---:|---|",
        f"| `HYPOTHESIS` | {stats['evidence_status']['HYPOTHESIS']} | pain and question are derived "
        "from the public role; no external data was fetched |",
        f"| `NOT_FETCHED` | {stats['evidence_status']['NOT_FETCHED']} | a substantive answer needs "
        "external data that was not fetched in this session |",
        "",
        "### Duration, prices and languages",
        "",
        f"- duration: {stats['duration_weeks']['min']}-{stats['duration_weeks']['max']} weeks;",
        f"- price: {fmt_usd(stats['price_usd']['min'])}-{fmt_usd(stats['price_usd']['max'])} USD, "
        f"free diagnostics: {stats['price_usd']['free']};",
        f"- English fields are present in {stats['english']['cases_with_en']} cases "
        f"({stats['english']['fields']} fields per case);",
        "- prices are pilot anchors confirmed on discovery, never guessed discounts.",
        "",
        "## 4. Catalog",
        "",
        "| # | Company | Segment | Question (hypothesis) | Modules | Wks | USD |",
        "|---:|---|---|---|---|---:|---:|",
    ]
    for case in cases:
        lines.append(
            "| {num} | {company} | `{segment}` | {question} | {modules} | {weeks} | {price} |".format(
                num=case["id"].replace("wc-", ""),
                company=case["company"],
                segment=case["segment"],
                question=case["question_en"].replace("|", "/"),
                modules=", ".join(_module_labels(case, "en")),
                weeks=case["duration_weeks"],
                price=fmt_usd(case["price_usd"]),
            )
        )
    lines += ["", "## 5. By company", ""]
    by_company: dict[str, list[dict]] = {}
    for case in cases:
        by_company.setdefault(case["company"], []).append(case)
    for company in sorted(by_company, key=lambda name: by_company[name][0]["id"]):
        group = by_company[company]
        head = group[0]
        items = "; ".join(
            f"`{case['id']}` — {case['title_en']} ({', '.join(_module_labels(case, 'en'))})"
            for case in group
        )
        lines.append(
            f"- **{company}** (`{head['segment']}`, {head['hq_region_en']}): {items}")

    fact_rows = _fact_rows(cases, "en")
    if fact_rows:
        lines += [
            "",
            "## 5.1 Fact packs: numbers with sources",
            "",
            "These are not hypotheses: the numbers come from open annual reports and are traceable by `source_id`.",
            "They do not confirm the case pain and are not client data.",
            "",
            "| ID | Company | Fact | Value | Source |",
            "|---|---|---|---|---|",
        ]
        for row in fact_rows:
            lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        "## 6. How to use",
        "",
        "1. Pick a segment and one pain — never show the whole catalog: 102 rows read as a price",
        "   list, one row reads as a task breakdown.",
        "2. Test the hypothesis on discovery: does the role, the decision loop and the budget cycle match?",
        "3. Replace `profile_hypothesis`, `pain` and `question` with what was said on the call, and set",
        "   `evidence_status=NOT_FETCHED` until a public confirmation exists.",
        "4. Take price and duration from the band; after the diagnostic fix one number and record it in",
        "   `price_note` together with the scope.",
        "5. Rebuild only through `python tools/case_catalog.py`; never edit the manifest or the PDFs.",
        "",
        "## 7. Boundaries",
        "",
        "- All 102 cases are hypotheses; none is verified on a company and none is an offer.",
        "- There is no contact, phone number, address or personal name in the pack, and there must be none.",
        "- There is no revenue, headcount or market-share figure: such claims need a source and a check.",
        "- Percent figures in questions are only those traceable to docs/186, docs/188 and docs/190.",
        "- `NOT_FETCHED` is an honest marker: external data (freight, TARIC, industry series) was not fetched.",
        "- The models are `RESEARCH_ONLY`, `admission=false`: module numbers are scenarios on declared",
        "  assumptions, not a forecast and not a probability.",
        "- Sending and contracting is done by a human; the agent neither sends nor commits.",
        "",
        DISCLAIMER_EN,
        "",
    ]
    return "\n".join(lines)


# --- PDF -------------------------------------------------------------------

def _docx_setup(document):
    from docx.shared import Cm, Pt, RGBColor

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(8)
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.05
    for name, size in (("Heading 1", 11.0), ("Title", 16.0)):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
        style.paragraph_format.space_before = Pt(6)
        style.paragraph_format.space_after = Pt(2)
    for section in document.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.left_margin = section.right_margin = Cm(1.5)
        section.top_margin = Cm(1.2)
        section.bottom_margin = Cm(1.0)
        section.header_distance = Cm(0.6)
        section.footer_distance = Cm(0.5)


def _docx_table(document, rows, sizes):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    width = max(len(row) for row in rows)
    rows = [list(row) + [""] * (width - len(row)) for row in rows]
    table = document.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.autofit = False
    for index in range(width):
        table.columns[index].width = Cm(sizes[index])

    margins = OxmlElement("w:tblCellMar")
    for side, value in (("top", 4), ("bottom", 4), ("left", 28), ("right", 28)):
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:w"), str(value))
        element.set(qn("w:type"), "dxa")
        margins.append(element)
    table._tbl.tblPr.append(margins)

    for position, row in enumerate(rows):
        cells = table.add_row().cells
        for index in range(width):
            cells[index].width = Cm(sizes[index])
            cell = cells[index]
            cell.text = ""
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            run = paragraph.add_run(str(row[index]))
            run.font.size = Pt(6.8 if position else 7.0)
            run.font.bold = position == 0
            run.font.name = "Calibri"
            if position == 0:
                run.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
        table.rows[position]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    return table


def _pdf_texts(lang: str) -> dict:
    if lang == "en":
        return {
            "title": "AMFE · 102 western cases",
            "subtitle": (
                "Hypothesis catalog · 102 cases · 51 companies x 2 · 9 segments · "
                f"built {CREATED} · DRAFT · RESEARCH_ONLY · admission=false · not_client=true"),
            "intro": (
                "Short cards to work through one pain on a call: segment, question, our modules, "
                "duration and a price anchor. Every case is a hypothesis, not an offer, not an "
                "audit and not a forecast; no revenue, headcount or contact data is included. "
                "Prices are USD only and only from the declared bands."),
            "segments": "Coverage by segment",
            "segments_head": ["Segment", "Cases", "Companies"],
            "modules": "Modules and price bands, USD",
            "modules_head": ["Module", "Cases", "Band, USD", "Cases"],
            "rules": "Rules and boundaries",
            "rules_items": (
                "not_client=true: no case is a client record, none is agreed and none is sent.",
                "profile_hypothesis, pain and question are hypotheses tested on discovery, not facts.",
                "Duration matches the band: diagnostic 1 week, pilot 2-4, full scenario 4-6 or 8-12, API 6-12.",
                "RESEARCH_ONLY, admission=false: not a model admission, not a forecast, not investment advice.",
                "English fields are a translation of the same case, with no new facts or figures.",
                "catalog.md and catalog_en.md are deterministic; the PDFs carry LibreOffice metadata.",
            ),
            "bands": "What a band includes, USD",
            "bands_head": ["Band", "Duration", "Scope"],
            "bands_rows": (
                ("0 — diagnostic", "1 wk.", "one meeting, review of two decisions, one-pager with the frame"),
                ("7 500-10 000 — pilot", "2-4 wks.", "2-4 TCO categories, score and budget, report for finance"),
                ("15 000-25 000 — scenario", "4-6 wks.", "one market, 2-3 stresses, PDF/XLSX/JSON, workshop"),
                ("20 000-30 000 — full", "8-12 wks.", "3-5 scenarios, country and sector, two workshops"),
                ("25 000-80 000 — API", "6-12 wks.", "keys and quotas, OpenAPI, Client Pack, SLA draft"),
            ),
            "read": "How to read a case",
            "read_items": (
                "Segment and region show the role, not the company structure: a buying-centre guess.",
                "The question is written for a call; if discovery brings a different question, the question changes, not the model.",
                "Modules are how the pain is closed; price and duration come from the band, never from a guess.",
                "NOT_FETCHED means the answer needs external data that was not fetched in this session.",
            ),
            "companies": "Companies in the catalog",
            "facts": "Fact packs: numbers with sources",
            "facts_head": ["ID", "Company", "Fact", "Value", "Source"],
            "cases": "102 cases: company, question, modules, duration, price",
            "cases_head": ["#", "Company", "Segment", "Question (hypothesis)", "Modules", "Wks", "USD"],
            "continuation": "102 cases (continued: {start}-{end})",
            "how": "How to use the catalog",
            "how_items": (
                "Show one case, not the catalog: 102 rows read as a price list, one row as a task.",
                "Test the hypothesis on discovery and replace pain/question with what was said.",
                "After the diagnostic fix one price and the scope in price_note, then rebuild.",
                "Sending, NDA and contracting is done by a human; the agent neither sends nor commits.",
            ),
            "footer": (
                "AMFE · RESEARCH_ONLY · admission=false · not_client=true · hypotheses, not company "
                "data · not a forecast · do not distribute without the disclaimer"),
            "header": f"AMFE · western case catalog (EN) · {CREATED} · source SHA-256 {{sha}}",
            "quote_limit": 120,
        }
    return {
        "title": "AMFE · 102 западных кейса",
        "subtitle": (
            f"Каталог гипотез · {102} кейсов · 51 компания × 2 · 9 сегментов · "
            f"собрано {CREATED} · DRAFT · RESEARCH_ONLY · admission=false · not_client=true"),
        "intro": (
            "Краткие карточки для разбора одной боли на созвоне: сегмент, вопрос, наши модули, срок "
            "и ориентир цены. Каждый кейс — гипотеза, а не КП, не аудит и не прогноз; цифры выручки, "
            "штата и контактов не приводятся. Цены — только USD и только из объявленных полос."),
        "segments": "Покрытие по сегментам",
        "segments_head": ["Сегмент", "Кейсов", "Компаний"],
        "modules": "Модули и ценовые полосы, USD",
        "modules_head": ["Модуль", "Кейсов", "Полоса, USD", "Кейсов"],
        "rules": "Правила и границы",
        "rules_items": (
            "not_client=true: ни один кейс не является клиентским, не согласован и не отправлен.",
            "profile_hypothesis, pain и question — гипотезы; проверяются на discovery, а не выдаются за факт.",
            "Срок соответствует полосе: диагностика 1 неделя, пилот 2-4, полный сценарий 4-6 или 8-12, API 6-12.",
            "RESEARCH_ONLY, admission=false: это не допуск модели, не прогноз и не инвестиционная рекомендация.",
            "Английские поля — перевод того же кейса, без новых фактов и цифр.",
            "catalog.md и catalog_en.md детерминированы; PDF несёт служебные метаданные LibreOffice.",
        ),
        "bands": "Что входит в полосу, USD",
        "bands_head": ["Полоса", "Срок", "Что входит"],
        "bands_rows": (
            ("0 — диагностика", "1 нед.", "одна встреча, разбор двух решений, one-pager с рамкой"),
            ("7 500-10 000 — пилот", "2-4 нед.", "2-4 категории TCO, балл и бюджет, отчёт для финансового контура"),
            ("15 000-25 000 — сценарий", "4-6 нед.", "один рынок, 2-3 стресса, PDF/XLSX/JSON, воркшоп"),
            ("20 000-30 000 — полный", "8-12 нед.", "3-5 сценариев, страна и отрасль, два воркшопа"),
            ("25 000-80 000 — API", "6-12 нед.", "ключи и квоты, OpenAPI, Client Pack, заготовка SLA"),
        ),
        "read": "Как читать кейс",
        "read_items": (
            "Сегмент и регион показывают роль, а не структуру компании: это гипотеза о buying center.",
            "Вопрос — формулировка для созвона; если на discovery звучит другой вопрос, меняется он, а не модель.",
            "Модули — то, чем закрывается боль; цена и срок берутся из полосы, а не «на глаз».",
            "NOT_FETCHED значит: для ответа нужны внешние данные, в этой сессии они не забирались.",
        ),
        "companies": "Компании в каталоге",
        "facts": "Факт-паки: числа с источниками",
        "facts_head": ["ID", "Компания", "Факт", "Значение", "Источник"],
        "cases": "102 кейса: компания, вопрос, модули, срок, цена",
        "cases_head": ["#", "Компания", "Сегмент", "Вопрос (гипотеза)", "Модули", "Нед.", "USD"],
        "continuation": "102 кейса (продолжение: {start}-{end})",
        "how": "Как использовать каталог",
        "how_items": (
            "Показать один кейс, а не каталог: 102 строки читаются как прайс, одна строка — как разбор задачи.",
            "Проверить гипотезу на discovery и заменить pain/question на то, что сказано на созвоне.",
            "После диагностики зафиксировать одну цену и объём работ в price_note и пересобрать каталог.",
            "Отправка, NDA и договор — только человеком; агент не отправляет и не коммитит.",
        ),
        "footer": (
            "AMFE · RESEARCH_ONLY · admission=false · not_client=true · гипотезы, не данные компаний · "
            "не прогноз · не для распространения без дисклеймера"),
        "header": f"AMFE · каталог западных кейсов · {CREATED} · SHA-256 источника {{sha}}",
        "quote_limit": 105,
    }


def render_pdf(catalog: dict, source_sha: str, pdf_path: Path, lang: str = "ru") -> int:
    """Render the catalog to a 4-6 page PDF; return the page count."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    texts = _pdf_texts(lang)
    cases = sorted(catalog["cases"], key=lambda case: case["id"])
    stats = coverage(catalog)

    document = Document()
    _docx_setup(document)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = title.add_run(texts["title"])
    run.font.size = Pt(17)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
    title.paragraph_format.space_after = Pt(0)

    subtitle = document.add_paragraph()
    run = subtitle.add_run(texts["subtitle"])
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    intro = document.add_paragraph(texts["intro"])
    intro.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    document.add_heading(texts["segments"], level=1)
    rows = [list(texts["segments_head"])]
    for segment in SEGMENTS:
        entry = stats["segments"][segment]
        name = SEGMENT_EN[segment] if lang == "en" else f"{segment} / {SEGMENT_RU[segment]}"
        rows.append([name, str(entry["cases"]), str(entry["companies"])])
    _docx_table(document, rows, (6.0, 2.0, 2.2))

    document.add_heading(texts["modules"], level=1)
    rows = [list(texts["modules_head"])]
    band_labels = [label for _, _, label in (BAND_LABELS_EN if lang == "en" else BAND_LABELS)]
    for index, module in enumerate(MODULES):
        label = band_labels[index] if index < len(band_labels) else ""
        band_cases = str(stats["price_bands"][BAND_LABELS[index][2]]) if label else ""
        name = MODULE_EN[module] if lang == "en" else module
        rows.append([name, str(stats["modules"][module]), label, band_cases])
    _docx_table(document, rows, (4.6, 1.6, 5.4, 1.6))

    document.add_heading(texts["rules"], level=1)
    for text in texts["rules_items"]:
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    document.add_heading(texts["bands"], level=1)
    rows = [list(texts["bands_head"])]
    rows.extend([list(row) for row in texts["bands_rows"]])
    _docx_table(document, rows, (4.6, 1.6, 11.6))

    document.add_heading(texts["read"], level=1)
    for text in texts["read_items"]:
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    document.add_heading(texts["companies"], level=1)
    order = sorted({case["company"] for case in cases},
                   key=lambda name: min(c["id"] for c in cases if c["company"] == name))
    segment_of = {case["company"]: case["segment"] for case in cases}
    rows = []
    for start in range(0, len(order), 5):
        rows.append([f"{name} ({segment_of[name]})" for name in order[start:start + 5]])
    _docx_table(document, rows, (3.4, 3.4, 3.4, 3.4, 3.4))

    fact_rows = _fact_rows(cases, lang)
    if fact_rows:
        document.add_heading(texts["facts"], level=1)
        rows = [list(texts["facts_head"])] + fact_rows
        _docx_table(document, rows, (1.6, 3.2, 5.2, 4.6, 3.2))

    sizes = (0.9, 3.0, 1.4, 6.9, 2.4, 1.0, 1.3)

    def table_rows(chunk):
        rows = [list(texts["cases_head"])]
        for case in chunk:
            rows.append([
                case["id"].replace("wc-", ""),
                case["company"],
                case["segment"],
                _short(case["question_en"] if lang == "en" else case["question"],
                       texts["quote_limit"]),
                ", ".join(_module_labels(case, lang)),
                str(case["duration_weeks"]),
                fmt_usd(case["price_usd"]),
            ])
        return rows

    chunks = (cases[:34], cases[34:67], cases[67:])
    for index, chunk in enumerate(chunks):
        document.add_page_break()
        if index == 0:
            heading_text = texts["cases"]
        else:
            heading_text = texts["continuation"].format(
                start=chunk[0]["id"].replace("wc-", ""), end=chunk[-1]["id"].replace("wc-", ""))
        heading = document.add_heading(heading_text, level=1)
        heading.paragraph_format.space_before = Pt(0)
        _docx_table(document, table_rows(chunk), sizes)

    document.add_heading(texts["how"], level=1)
    for text in texts["how_items"]:
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    footer = document.sections[0].footer.paragraphs[0]
    footer.text = texts["footer"]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for footer_run in footer.runs:
        footer_run.font.size = Pt(6.5)
        footer_run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    header = document.sections[0].header.paragraphs[0]
    header.text = texts["header"].format(sha=source_sha[:12])
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for header_run in header.runs:
        header_run.font.size = Pt(6.5)
        header_run.font.color.rgb = RGBColor(0x77, 0x77, 0x77)

    DOCX_DIR.mkdir(parents=True, exist_ok=True)
    docx_path = DOCX_DIR / ("catalog_en.docx" if lang == "en" else "catalog.docx")
    document.save(docx_path)
    return _convert_pdf(docx_path, pdf_path)


def _page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return len(PdfReader(str(path)).pages)


def _convert_pdf(docx_path: Path, pdf_path: Path) -> int:
    if not SOFFICE.exists():
        raise CatalogError(f"LibreOffice not found: {SOFFICE}")
    if pdf_path.exists():
        pdf_path.unlink()
    last_error = ""
    for attempt in range(3):
        try:
            done = subprocess.run(
                [str(SOFFICE), "--headless", "--nolockcheck", "--norestore",
                 "--convert-to", "pdf", "--outdir", str(pdf_path.parent), str(docx_path)],
                capture_output=True, text=True, timeout=240,
            )
            last_error = (done.stderr or done.stdout or "").strip() or f"exit {done.returncode}"
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        deadline = time.time() + 120
        while time.time() < deadline:
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                pages = _page_count(pdf_path)
                if pages is None:
                    return 0
                if not PDF_PAGES[0] <= pages <= PDF_PAGES[1]:
                    pdf_path.unlink(missing_ok=True)
                    raise CatalogError(
                        f"catalog PDF must be {PDF_PAGES[0]}-{PDF_PAGES[1]} pages, got {pages}; "
                        "adjust the layout"
                    )
                return pages
            time.sleep(1.5)
        time.sleep(1.5 * (attempt + 1))
    raise CatalogError(f"PDF conversion failed for {docx_path.name}: {last_error}")


# --- manifest --------------------------------------------------------------

CATALOG_FILES = ("cases.json", "catalog.md", "catalog_en.md", "catalog.pdf", "catalog_en.pdf")


def build_manifest(out_dir: Path, cases_path: Path) -> dict:
    entries = []
    for name in CATALOG_FILES:
        path = out_dir / name
        if not path.is_file():
            continue
        entry = {
            "path": name,
            "lang": "en" if name.endswith("_en.md") or name.endswith("_en.pdf") else (
                "json" if path.suffix == ".json" else "ru"),
            "kind": "pdf" if path.suffix == ".pdf" else (
                "json" if path.suffix == ".json" else "markdown"),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "deterministic": path.suffix != ".pdf",
        }
        if path.suffix == ".pdf":
            pages = _page_count(path)
            if pages is not None:
                entry["pages"] = pages
        entries.append(entry)

    catalog = load_cases(cases_path)
    stats = coverage(catalog)
    source = out_dir / "cases.json"
    return {
        "schema": SCHEMA,
        "created": CREATED,
        "status": "DRAFT",
        "model_status": "RESEARCH_ONLY",
        "admission": False,
        "causal_claim": False,
        "sent": False,
        "languages": ["ru", "en"],
        "source": {
            "path": "marketing/western_cases/cases.json",
            "sha256": sha256_file(source) if source.is_file() else sha256_file(cases_path),
            "cases": stats["cases"],
            "companies": stats["companies"],
        },
        "coverage": {
            "segments": stats["segments"],
            "modules": stats["modules"],
            "modules_en": stats["modules_en"],
            "price_bands": stats["price_bands"],
            "evidence_status": stats["evidence_status"],
            "price_usd": stats["price_usd"],
            "duration_weeks": stats["duration_weeks"],
            "english": stats["english"],
        },
        "rules": {
            "not_client": True,
            "hypotheses_only": True,
            "no_contacts": True,
            "no_revenue_or_headcount": True,
            "no_admission_claim": True,
            "nothing_sent": True,
            "price_in_usd_only": True,
            "english_fields": True,
            "bilingual": True,
            "markdown_deterministic": True,
            "pdf_deterministic": False,
            "manifest_excludes_itself": True,
            "invalid_catalog_writes_nothing": True,
        },
        "files": entries,
    }


def write_manifest(out_dir: Path, cases_path: Path) -> Path:
    manifest = build_manifest(out_dir, cases_path)
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def check(out_dir: Path, cases_path: Path) -> int:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"manifest is not valid JSON: {error}", file=sys.stderr)
        return 1

    catalog = load_cases(cases_path)
    problems = validate(catalog)
    if problems:
        for problem in problems:
            print(f"cases.json: {problem}", file=sys.stderr)
        return 1

    ok = True
    if manifest.get("schema") != SCHEMA:
        print(f"manifest schema is not {SCHEMA}")
        ok = False
    if manifest.get("languages") != ["ru", "en"]:
        print("manifest languages must be ['ru', 'en']")
        ok = False
    if not manifest.get("rules", {}).get("english_fields"):
        print("manifest rules.english_fields must be true")
        ok = False
    if manifest.get("source", {}).get("cases") != len(catalog["cases"]):
        print("manifest source.cases does not match cases.json")
        ok = False

    listed = {entry["path"] for entry in manifest.get("files", [])}
    for name in sorted(listed):
        path = out_dir / name
        if not path.is_file():
            print(f"missing: {name}")
            ok = False
    for required in ("cases.json", "catalog.md", "catalog_en.md"):
        if required not in listed:
            print(f"manifest does not list {required}")
            ok = False
    for optional in ("catalog.pdf", "catalog_en.pdf"):
        if (out_dir / optional).is_file() and optional not in listed:
            print(f"manifest does not list {optional} although the file exists")
            ok = False

    for entry in manifest.get("files", []):
        path = out_dir / entry["path"]
        if not path.is_file():
            continue
        if sha256_file(path) != entry["sha256"]:
            print(f"sha256 mismatch: {entry['path']}")
            ok = False
        if path.stat().st_size != entry["bytes"]:
            print(f"size mismatch: {entry['path']}")
            ok = False
        if path.suffix == ".pdf" and "pages" in entry:
            pages = _page_count(path)
            if pages is not None and pages != entry["pages"]:
                print(f"page count mismatch: {entry['path']} ({pages} != {entry['pages']})")
                ok = False
            if pages is not None and not PDF_PAGES[0] <= pages <= PDF_PAGES[1]:
                print(f"page count out of range: {entry['path']} ({pages})")
                ok = False

    print("OK" if ok else "FAIL")
    return 0 if ok else 1


def build(cases_path: Path, out_dir: Path, with_pdf: bool) -> dict:
    catalog = load_cases(cases_path)
    problems = validate(catalog)
    if problems:
        for problem in problems:
            print(f"cases.json: {problem}", file=sys.stderr)
        raise SystemExit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    source_copy = out_dir / "cases.json"
    source_bytes = cases_path.read_bytes()
    if cases_path.resolve() != source_copy.resolve():
        source_copy.write_bytes(source_bytes)
    source_sha = sha256_bytes(source_bytes)

    for name, lang in (("catalog.md", "ru"), ("catalog_en.md", "en")):
        markdown = render_markdown(catalog, source_sha, lang)
        (out_dir / name).write_text(markdown, encoding="utf-8", newline="\n")

    pages: dict[str, int | None] = {"ru": None, "en": None}
    if with_pdf:
        pages["ru"] = render_pdf(catalog, source_sha, out_dir / "catalog.pdf", "ru")
        pages["en"] = render_pdf(catalog, source_sha, out_dir / "catalog_en.pdf", "en")
    else:
        for name in ("catalog.pdf", "catalog_en.pdf"):
            stale = out_dir / name
            if stale.is_file():
                stale.unlink()
    manifest = write_manifest(out_dir, cases_path)

    return {
        "ok": True,
        "out": str(out_dir),
        "cases": len(catalog["cases"]),
        "companies": len({case["company"] for case in catalog["cases"]}),
        "catalog_md_sha256": sha256_file(out_dir / "catalog.md"),
        "catalog_md_bytes": (out_dir / "catalog.md").stat().st_size,
        "catalog_en_md_sha256": sha256_file(out_dir / "catalog_en.md"),
        "catalog_en_md_bytes": (out_dir / "catalog_en.md").stat().st_size,
        "pdf": (out_dir / "catalog.pdf").is_file(),
        "pdf_pages": pages["ru"],
        "pdf_en": (out_dir / "catalog_en.pdf").is_file(),
        "pdf_en_pages": pages["en"],
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "coverage": coverage(catalog),
        "status": "RESEARCH_ONLY",
    }


def summary_json(cases_path: Path) -> int:
    catalog = load_cases(cases_path)
    problems = validate(catalog)
    try:
        source = str(cases_path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        source = cases_path.name
    payload = {
        "ok": not problems,
        "schema": catalog.get("schema"),
        "status": "RESEARCH_ONLY",
        "admission": False,
        "languages": catalog.get("languages"),
        "source": source,
        "source_sha256": sha256_file(cases_path),
        "problems": problems,
    }
    if not problems:
        payload.update({
            "cases": len(catalog["cases"]),
            "companies": len({case["company"] for case in catalog["cases"]}),
            "coverage": coverage(catalog),
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and build the AMFE western case catalog (RU + EN)")
    parser.add_argument("--check", action="store_true",
                        help="verify manifest, sha256, sizes and pages; write nothing")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable summary and write nothing")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--no-pdf", action="store_true", help="skip the LibreOffice PDFs")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="path to cases.json")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    cases_path = Path(args.cases).resolve()

    if args.check:
        try:
            return check(out_dir, cases_path)
        except CatalogError as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            return 1

    if args.json:
        try:
            return summary_json(cases_path)
        except CatalogError as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            return 1

    try:
        result = build(cases_path, out_dir, with_pdf=not args.no_pdf)
    except CatalogError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    pages = f", {result['pdf_pages']} pages" if result["pdf_pages"] else ""
    pages_en = f", EN {result['pdf_en_pages']} pages" if result["pdf_en_pages"] else ""
    print(f"catalog: {result['out']} ({result['cases']} cases, {result['companies']} companies"
          f"{pages}{pages_en})")
    print(f"manifest: {result['manifest_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
