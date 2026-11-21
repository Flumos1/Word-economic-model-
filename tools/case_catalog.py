#!/usr/bin/env python3
"""Validate the 50 western commercial cases and build the catalog pack.

Source of truth is ``marketing/western_cases/cases.json`` (25 companies x 2 cases).
The tool validates it fail-closed, then renders ``catalog.md``, a 2-3 page
``catalog.pdf`` (LibreOffice) and ``manifest.json`` with SHA-256 for every file.

Nothing is sent and nothing is committed: the tool only writes files. Run from the
repo root::

    python tools/case_catalog.py                 # catalog.md + catalog.pdf + manifest.json
    python tools/case_catalog.py --no-pdf        # catalog.md + manifest.json
    python tools/case_catalog.py --check         # verify manifest, write nothing
    python tools/case_catalog.py --json          # summary in stdout, write nothing
    python tools/case_catalog.py --out build/western_cases_demo --no-pdf

Rules baked into the pack:
  * every case is a hypothesis (``not_client=true``), never a client record;
  * prices are USD only and must fall into one of the declared bands;
  * duration must match the price band (a diagnostic is one week, a pilot 2-4);
  * no e-mail, phone, URL, personal name, revenue or headcount anywhere;
  * nothing claims an admitted model, a client relationship or a "точность" figure;
  * ``catalog.md`` is byte-deterministic, the PDF is not (LibreOffice metadata);
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

SCHEMA = "amfe.marketing.western_case_catalog.v1"
CREATED = "2026-11-21"
PDF_PAGES = (2, 3)
MIN_CASES = 50
MIN_COMPANIES = 20
MAX_CASES_PER_COMPANY = 2

SEGMENTS = ("major", "upstream", "lng", "services", "trading", "chemicals", "utilities")
SEGMENT_RU = {
    "major": "мейджор",
    "upstream": "добыча",
    "lng": "СПГ",
    "services": "сервис",
    "trading": "трейдинг",
    "chemicals": "химия",
    "utilities": "энергетика",
}
MODULES = ("Ормуз/бюджет", "FLAKS TCO", "газ/СПГ", "геополитика", "API/Client Pack")

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

REQUIRED_TEXT = ("company", "hq_region", "profile_hypothesis", "pain", "question", "solution")
REQUIRED_FIELDS = (
    "id", "company", "segment", "hq_region", "profile_hypothesis", "pain", "question",
    "solution", "modules", "deliverables", "duration_weeks", "price_usd", "price_note",
    "evidence_status", "not_client",
)
EVIDENCE = ("HYPOTHESIS", "NOT_FETCHED")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)|(\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)")
FIO_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:ович|евич|овна|евна|ична)\b")
FORBIDDEN_CLAIMS = ("клиент", "admitted", "точность", "сертифицирован")
ADMISSION_RE = re.compile(r"допущен(?!и)")
ID_RE = re.compile(r"^wc-\d{3}$")

DISCLAIMER = (
    "AMFE · RESEARCH_ONLY · admission=false · not_client=true · гипотезы, не данные компаний · "
    "не прогноз и не инвестиционная рекомендация"
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


def _iter_strings(case: dict) -> list[str]:
    values = []
    for field in REQUIRED_TEXT:
        values.append(str(case.get(field, "")))
    values.extend(str(item) for item in case.get("modules", []))
    values.extend(str(item) for item in case.get("deliverables", []))
    values.append(str(case.get("price_note", "")))
    return values


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
    rules = catalog.get("rules") or {}
    for key in ("not_client", "evidence_is_hypothesis", "no_contacts",
                "no_revenue_or_headcount", "no_admission_claim", "price_in_usd_only"):
        if rules.get(key) is not True:
            problems.append(f"rules.{key} must be true")

    cases = catalog.get("cases")
    if not isinstance(cases, list):
        return problems + ["cases must be a list"]
    if len(cases) < MIN_CASES:
        problems.append(f"cases: {len(cases)} < {MIN_CASES}")
    declared = catalog.get("cases_count")
    if declared != len(cases):
        problems.append(f"cases_count {declared!r} != {len(cases)} cases")

    seen_ids: set[str] = set()
    seen_questions: dict[str, str] = {}
    seen_per_company: dict[str, list[set[str]]] = {}
    companies: set[str] = set()
    by_segment: dict[str, int] = {segment: 0 for segment in SEGMENTS}
    by_module: dict[str, int] = {module: 0 for module in MODULES}

    for position, case in enumerate(cases):
        label = case.get("id") or f"case#{position + 1}"
        if not isinstance(case, dict):
            problems.append(f"{label}: case must be an object")
            continue
        for field in REQUIRED_FIELDS:
            if field not in case:
                problems.append(f"{label}: missing field {field}")
        missing = [field for field in REQUIRED_FIELDS if field not in case]
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
            if EMAIL_RE.search(text) or URL_RE.search(text) or PHONE_RE.search(text):
                problems.append(f"{label}: contact or link detected in text")
            if FIO_RE.search(text):
                problems.append(f"{label}: personal name detected in text")
            lowered = text.lower()
            for claim in FORBIDDEN_CLAIMS:
                if claim in lowered:
                    problems.append(f"{label}: forbidden claim {claim!r} in text")
            if ADMISSION_RE.search(lowered):
                problems.append(f"{label}: admission claim in text")

        module_key = frozenset(str(item) for item in modules)
        seen_per_company.setdefault(company, []).append(module_key)

    if len(companies) < MIN_COMPANIES:
        problems.append(f"companies: {len(companies)} < {MIN_COMPANIES}")
    if len(companies) > 50:
        problems.append(f"companies: {len(companies)} > 50")
    declared_companies = catalog.get("companies_count")
    if declared_companies != len(companies):
        problems.append(f"companies_count {declared_companies!r} != {len(companies)} companies")

    for company, groups in sorted(seen_per_company.items()):
        if len(groups) > MAX_CASES_PER_COMPANY:
            problems.append(f"{company}: {len(groups)} cases, at most {MAX_CASES_PER_COMPANY}")
        for index, group in enumerate(groups):
            if group in groups[:index]:
                problems.append(f"{company}: duplicate module set {sorted(group)}")

    for segment in SEGMENTS:
        if by_segment[segment] < 2:
            problems.append(f"segment coverage: {segment} has {by_segment[segment]} cases, need >= 2")

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
    modules = {module: 0 for module in MODULES}
    bands = {label: 0 for _, _, label in BAND_LABELS}
    evidence = {status: 0 for status in EVIDENCE}
    prices: dict[str, int] = {}
    for case in cases:
        for module in case["modules"]:
            modules[str(module)] += 1
        bands[band_of(case["price_usd"], case["duration_weeks"])] += 1
        evidence[case["evidence_status"]] += 1
        key = fmt_usd(case["price_usd"])
        prices[key] = prices.get(key, 0) + 1
    return {
        "cases": len(cases),
        "companies": len({case["company"] for case in cases}),
        "segments": segments,
        "modules": dict(modules),
        "price_bands": dict(bands),
        "price_points": dict(sorted(prices.items(), key=lambda item: int(item[0].replace(" ", "")))),
        "evidence_status": dict(evidence),
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


def render_markdown(catalog: dict, source_sha: str) -> str:
    cases = sorted(catalog["cases"], key=lambda case: case["id"])
    stats = coverage(catalog)
    lines: list[str] = [
        "# Каталог 50 западных кейсов",
        "",
        f"Собрано: {CREATED} · статус DRAFT · `RESEARCH_ONLY` · `admission=false` · "
        "`not_client=true` · ничего не отправлено",
        "",
        f"Источник: `marketing/western_cases/cases.json` · SHA-256 `{source_sha}` · "
        f"{stats['cases']} кейсов, {stats['companies']} компаний",
        "",
        "Каталог генерируется `tools/case_catalog.py` из `cases.json`; руками не правится.",
        "",
        "## 1. Что это",
        "",
        "50 кратких коммерческих кейсов для западных корпораций: 25 компаний по 2 кейса. Каждый",
        "кейс — **гипотеза** о боли и вопросе, который эта роль могла бы задать, и о том, какие",
        "наши модули её закрывают. Это не описание компании, не КП, не аудит и не прогноз:",
        "`evidence_status` у каждого кейса — `HYPOTHESIS` или `NOT_FETCHED`, ни одного факта о",
        "финансах, штате или контактах внутри нет.",
        "",
        "## 2. Правила пакета",
        "",
        "- `not_client=true` — ни один кейс не является клиентским, ни один не согласован и не отправлен.",
        "- `profile_hypothesis` — только предположение о роли и контуре решений, без выручки, штата и контактов.",
        "- `pain` и `question` — гипотеза о боли; проверяется на discovery, а не выдаётся за факт.",
        "- Цены — только USD и только из объявленных полос; срок должен соответствовать полосе.",
        "- `RESEARCH_ONLY`, `admission=false`: это не допуск модели и не инвестиционная рекомендация.",
        "- PDF не байт-детерминирован (метаданные LibreOffice), `catalog.md` — детерминирован.",
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
        "### Сроки и цены",
        "",
        f"- срок: {stats['duration_weeks']['min']}-{stats['duration_weeks']['max']} недель;",
        f"- цена: {fmt_usd(stats['price_usd']['min'])}-{fmt_usd(stats['price_usd']['max'])} USD, "
        f"бесплатных диагностик — {stats['price_usd']['free']};",
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
                modules=", ".join(case["modules"]),
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
            f"`{case['id']}` — {case['question']} ({', '.join(case['modules'])})"
            for case in group
        )
        lines.append(f"- **{company}** (`{head['segment']}`, {head['hq_region']}): {items}")
    lines += [
        "",
        "## 6. Как использовать",
        "",
        "1. Выбрать сегмент и одну боль — не показывать каталог целиком: 50 кейсов читаются как",
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
        "- Все 50 кейсов — гипотезы; ни один не проверен на компании и не является КП.",
        "- Нет ни одного контакта, телефона, адреса и персонального имени — и не должно быть.",
        "- Нет цифр выручки, штата и доли рынка: такие утверждения требуют источника и проверки.",
        "- `NOT_FETCHED` — честная пометка: внешние данные (фрахт, TARIC, отраслевые ряды) не забирались.",
        "- Модели `RESEARCH_ONLY`, `admission=false`: числа из модулей — сценарии на объявленных",
        "  допущениях, а не прогноз и не вероятность.",
        "- Отправка и договор — только человеком; агент не отправляет и не коммитит.",
        "",
        DISCLAIMER,
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

    width = len(rows[0])
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


def render_pdf(catalog: dict, source_sha: str, pdf_path: Path) -> int:
    """Render the catalog to a 2-3 page PDF; return the page count."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    cases = sorted(catalog["cases"], key=lambda case: case["id"])
    stats = coverage(catalog)

    document = Document()
    _docx_setup(document)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = title.add_run("AMFE · 50 западных кейсов")
    run.font.size = Pt(17)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
    title.paragraph_format.space_after = Pt(0)

    subtitle = document.add_paragraph()
    run = subtitle.add_run(
        f"Каталог гипотез · {stats['cases']} кейсов · {stats['companies']} компаний × 2 · "
        f"собрано {CREATED} · DRAFT · RESEARCH_ONLY · admission=false · not_client=true"
    )
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    intro = document.add_paragraph(
        "Краткие карточки для разбора одной боли на созвоне: сегмент, вопрос, наши модули, срок "
        "и ориентир цены. Каждый кейс — гипотеза, а не КП, не аудит и не прогноз; цифры выручки, "
        "штата и контактов не приводятся. Цены — только USD и только из объявленных полос."
    )
    intro.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    document.add_heading("Покрытие по сегментам", level=1)
    rows = [["Сегмент", "Кейсов", "Компаний"]]
    for segment in SEGMENTS:
        entry = stats["segments"][segment]
        rows.append([f"{segment} / {SEGMENT_RU[segment]}", str(entry["cases"]), str(entry["companies"])])
    _docx_table(document, rows, (6.0, 2.0, 2.2))

    document.add_heading("Модули и ценовые полосы, USD", level=1)
    rows = [["Модуль", "Кейсов", "Полоса, USD", "Кейсов"]]
    band_labels = [label for _, _, label in BAND_LABELS]
    for index, module in enumerate(MODULES):
        label = band_labels[index] if index < len(band_labels) else ""
        band_cases = str(stats["price_bands"][label]) if label else ""
        rows.append([module, str(stats["modules"][module]), label, band_cases])
    _docx_table(document, rows, (4.6, 1.6, 5.4, 1.6))

    document.add_heading("Правила и границы", level=1)
    for text in (
        "not_client=true: ни один кейс не является клиентским, не согласован и не отправлен.",
        "profile_hypothesis, pain и question — гипотезы; проверяются на discovery, а не выдаются за факт.",
        "Срок соответствует полосе: диагностика 1 неделя, пилот 2-4, полный сценарий 4-6 или 8-12, API 6-12.",
        "RESEARCH_ONLY, admission=false: это не допуск модели, не прогноз и не инвестиционная рекомендация.",
        "catalog.md детерминирован; PDF несёт служебные метаданные LibreOffice и байт-детерминизмом не является.",
    ):
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    document.add_heading("Что входит в полосу, USD", level=1)
    rows = [["Полоса", "Срок", "Что входит"]]
    rows.extend([
        ["0 — диагностика", "1 нед.", "одна встреча, разбор двух решений, one-pager с рамкой"],
        ["7 500-10 000 — пилот", "2-4 нед.", "2-4 категории TCO, балл и бюджет, отчёт для финансового контура"],
        ["15 000-25 000 — сценарий", "4-6 нед.", "один рынок, 2-3 стресса, PDF/XLSX/JSON, воркшоп"],
        ["20 000-30 000 — полный", "8-12 нед.", "3-5 сценариев, страна и отрасль, два воркшопа"],
        ["25 000-80 000 — API", "6-12 нед.", "ключи и квоты, OpenAPI, Client Pack, заготовка SLA"],
    ])
    _docx_table(document, rows, (4.6, 1.6, 11.6))

    document.add_heading("Как читать кейс", level=1)
    for text in (
        "Сегмент и регион показывают роль, а не структуру компании: это гипотеза о buying center.",
        "Вопрос — формулировка для созвона; если на discovery звучит другой вопрос, меняется он, а не модель.",
        "Модули — то, чем закрывается боль; цена и срок берутся из полосы, а не «на глаз».",
        "NOT_FETCHED значит: для ответа нужны внешние данные, в этой сессии они не забирались.",
    ):
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    document.add_heading("Компании в каталоге", level=1)
    order = sorted({case["company"] for case in cases},
                   key=lambda name: min(c["id"] for c in cases if c["company"] == name))
    segment_of = {case["company"]: case["segment"] for case in cases}
    rows = []
    for start in range(0, len(order), 5):
        rows.append([f"{name} ({segment_of[name]})" for name in order[start:start + 5]])
    _docx_table(document, rows, (3.4, 3.4, 3.4, 3.4, 3.4))

    document.add_page_break()
    heading = document.add_heading("50 кейсов: компания, вопрос, модули, срок, цена", level=1)
    heading.paragraph_format.space_before = Pt(0)

    def table_rows(chunk):
        rows = [["#", "Компания", "Сегмент", "Вопрос (гипотеза)", "Модули", "Нед.", "USD"]]
        for case in chunk:
            rows.append([
                case["id"].replace("wc-", ""),
                case["company"],
                case["segment"],
                _short(case["question"], 110),
                ", ".join(case["modules"]),
                str(case["duration_weeks"]),
                fmt_usd(case["price_usd"]),
            ])
        return rows

    sizes = (0.9, 3.0, 1.4, 6.9, 2.4, 1.0, 1.3)
    _docx_table(document, table_rows(cases[:32]), sizes)

    document.add_page_break()
    continuation = document.add_heading("50 кейсов (продолжение: 033-050)", level=1)
    continuation.paragraph_format.space_before = Pt(0)
    _docx_table(document, table_rows(cases[32:]), sizes)

    document.add_heading("Как использовать каталог", level=1)
    for text in (
        "Показать один кейс, а не каталог: 50 строк читаются как прайс, одна строка — как разбор задачи.",
        "Проверить гипотезу на discovery и заменить pain/question на то, что сказано на созвоне.",
        "После диагностики зафиксировать одну цену и объём работ в price_note и пересобрать каталог.",
        "Отправка, NDA и договор — только человеком; агент не отправляет и не коммитит.",
    ):
        paragraph = document.add_paragraph(text, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(0)

    footer = document.sections[0].footer.paragraphs[0]
    footer.text = (
        "AMFE · RESEARCH_ONLY · admission=false · not_client=true · гипотезы, не данные компаний · "
        "не прогноз · не для распространения без дисклеймера"
    )
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for footer_run in footer.runs:
        footer_run.font.size = Pt(6.5)
        footer_run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    header = document.sections[0].header.paragraphs[0]
    header.text = f"AMFE · каталог западных кейсов · {CREATED} · SHA-256 источника {source_sha[:12]}"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for header_run in header.runs:
        header_run.font.size = Pt(6.5)
        header_run.font.color.rgb = RGBColor(0x77, 0x77, 0x77)

    DOCX_DIR.mkdir(parents=True, exist_ok=True)
    docx_path = DOCX_DIR / "catalog.docx"
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

def build_manifest(out_dir: Path, cases_path: Path) -> dict:
    entries = []
    for name in ("cases.json", "catalog.md", "catalog.pdf"):
        path = out_dir / name
        if not path.is_file():
            continue
        entry = {
            "path": name,
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
        "source": {
            "path": "marketing/western_cases/cases.json",
            "sha256": sha256_file(source) if source.is_file() else sha256_file(cases_path),
            "cases": stats["cases"],
            "companies": stats["companies"],
        },
        "coverage": {
            "segments": stats["segments"],
            "modules": stats["modules"],
            "price_bands": stats["price_bands"],
            "evidence_status": stats["evidence_status"],
            "price_usd": stats["price_usd"],
            "duration_weeks": stats["duration_weeks"],
        },
        "rules": {
            "not_client": True,
            "hypotheses_only": True,
            "no_contacts": True,
            "no_revenue_or_headcount": True,
            "no_admission_claim": True,
            "nothing_sent": True,
            "price_in_usd_only": True,
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
    listed = {entry["path"] for entry in manifest.get("files", [])}
    for name in sorted(listed):
        path = out_dir / name
        if not path.is_file():
            print(f"missing: {name}")
            ok = False
    for required in ("cases.json", "catalog.md"):
        if required not in listed:
            print(f"manifest does not list {required}")
            ok = False
    if (out_dir / "catalog.pdf").is_file() and "catalog.pdf" not in listed:
        print("manifest does not list catalog.pdf although the file exists")
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

    markdown = render_markdown(catalog, source_sha)
    (out_dir / "catalog.md").write_text(markdown, encoding="utf-8", newline="\n")

    pages = None
    if with_pdf:
        pages = render_pdf(catalog, source_sha, out_dir / "catalog.pdf")
    else:
        stale = out_dir / "catalog.pdf"
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
        "pdf": (out_dir / "catalog.pdf").is_file(),
        "pdf_pages": pages,
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "coverage": coverage(catalog),
        "status": "RESEARCH_ONLY",
    }


def summary_json(cases_path: Path) -> int:
    catalog = load_cases(cases_path)
    problems = validate(catalog)
    payload = {
        "ok": not problems,
        "schema": catalog.get("schema"),
        "status": "RESEARCH_ONLY",
        "admission": False,
        "source": str(cases_path.relative_to(ROOT)).replace("\\", "/"),
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
    parser = argparse.ArgumentParser(description="Validate and build the AMFE western case catalog")
    parser.add_argument("--check", action="store_true",
                        help="verify manifest, sha256, sizes and pages; write nothing")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable summary and write nothing")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--no-pdf", action="store_true", help="skip the LibreOffice PDF")
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
    print(f"catalog: {result['out']} ({result['cases']} cases, {result['companies']} companies{pages})")
    print(f"manifest: {result['manifest_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
