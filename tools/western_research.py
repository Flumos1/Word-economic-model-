#!/usr/bin/env python3
"""Validate the AMFE western-account research dossiers (``marketing/western_research/*.md``).

The dossiers are hand-written Markdown: one file per account, nine fixed sections, a small
key/value header and explicit evidence tags. This tool is deterministic (no clock, no network),
fail-closed (any problem is an error, exit 2) and it never writes files and never sends anything.

It enforces the policy of the *western* research package, which differs from the Russian package
(``tools/account_research.py``) in two ways:

* the allowed evidence statuses are ``PARTIAL`` / ``HYPOTHESIS`` / ``NOT_FETCHED`` — ``VERIFIED``
  is rejected until a first-party source (site, annual report, filing) has actually been fetched;
* a ``PARTIAL`` dossier must cite at least one ``https://`` source in section 9, while a
  ``NOT_FETCHED`` dossier must not cite any (otherwise the status over- or under-claims).

Layout and policy checks:

* exactly the ten accounts of ``accounts.json``, one ``<id>.md`` file each;
* ``evidence_status`` equals the strongest tag used in section 1;
* three pain hypotheses (all ``[HYPOTHESIS]``), five discovery questions, three use cases that
  reference known AMFE module ids, four trigger classes, a buying center of roles only;
* no e-mail addresses, phone numbers, messenger handles, personal names, revenue / profit /
  headcount / market-cap / production figures and no ``admitted`` claim in a dossier.

Usage::

    python tools/western_research.py --check      # exit 0 / 2, prints the issues
    python tools/western_research.py --summary    # human-readable summary
    python tools/western_research.py --json       # machine summary (amfe.western_research.v1)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "marketing" / "western_research"
DEFAULT_ACCOUNTS = DEFAULT_DIR / "accounts.json"

SCHEMA = "amfe.western_research.v1"
ACCOUNTS_SCHEMA = "amfe.western_research.accounts.v1"
REQUIRED_ACCOUNTS = 10

EVIDENCE_STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_FETCHED")
EVIDENCE_RANK = {"NOT_FETCHED": 0, "HYPOTHESIS": 1, "PARTIAL": 2, "VERIFIED": 3}
ALLOWED_STATUSES = ("PARTIAL", "HYPOTHESIS", "NOT_FETCHED")

REQUIRED_FIELDS = (
    "id",
    "company",
    "segment",
    "segment_ru",
    "hq",
    "region",
    "evidence_status",
    "dossier_date",
    "owner_role",
    "model_status",
)
SECTION_TITLES = {
    1: "профиль",
    2: "боли",
    3: "buying center",
    4: "триггеры",
    5: "use cases",
    6: "вопросы",
    7: "конкуренты",
    8: "что проверить",
    9: "источники",
}
TRIGGER_STEMS = ("отчётност", "capex", "санкц", "энергопереход")
MODULE_IDS = frozenset({
    "oil_price_scenarios",
    "capex_budget",
    "procurement_tco",
    "gas_lng_scenarios",
    "geopolitics",
    "geopolitics_logistics",
    "refining_margin",
    "trading_risk",
    "freight_insurance",
    "api_integration",
})
MIN_PAINS = 3
MIN_QUESTIONS = 5
MIN_USE_CASES = 3
MIN_MODULE_DIVERSITY = 2
MIN_BUYING_CENTER_ROLES = 4
MIN_COMPETITORS = 4
MIN_VERIFY_ITEMS = 5

FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.+?)\s*$", re.M)
SECTION_RE = re.compile(r"^##\s+(\d)\.\s*(.*)$", re.M)
TAG_RE = re.compile(r"\[(VERIFIED|PARTIAL|HYPOTHESIS|NOT_FETCHED)\]")
NUMBERED_RE = re.compile(r"^(\d+)[.)]\s+(.+)$", re.M)
VERIFY_ITEM_RE = re.compile(r"^-\s*\[ \]\s+(.+)$", re.M)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
URL_RE = re.compile(r"https?://\S+")
MODULE_TOKEN_RE = re.compile(r"[a-z][a-z_]{3,}")

# Contacts, personal data and financials must not appear in a dossier. Each entry is
# (pattern, code, message); the tests assert that every code is reachable.
FORBIDDEN_PATTERNS = (
    (re.compile(r"@"), "email_like", "no e-mail addresses in a dossier"),
    (re.compile(r"\bmailto:", re.IGNORECASE), "email_like", "no mailto: links in a dossier"),
    (re.compile(r"\btel:", re.IGNORECASE), "phone_like", "no tel: links in a dossier"),
    (re.compile(r"\bskype:", re.IGNORECASE), "phone_like", "no messenger handles in a dossier"),
    (re.compile(r"\btelegram:", re.IGNORECASE), "phone_like", "no messenger handles in a dossier"),
    (re.compile(r"\bwhatsapp:", re.IGNORECASE), "phone_like", "no messenger handles in a dossier"),
    (re.compile(r"linkedin\.com/in/", re.IGNORECASE), "personal_profile",
     "no personal profile links in a dossier"),
    (re.compile(r"\+\d{1,3}[\s\-(]*\d"), "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"\(\d{3}\)\s*\d{3}[\s-]\d{4}"), "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"), "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"\bФИО\b"), "personal_name", "no personal names or name fields in a dossier"),
    (re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]+(?:ович|евич|овна|евна|ична)\b"),
     "personal_name", "no personal names (patronymic) in a dossier"),
    (re.compile(r"\badmitted\b", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"допущенн\w*", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"допущен(?:а|о|ы)\b", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"(?:revenue|net income|ebitda|ebit|headcount|employees|market cap|"
                r"market capitalization|выручк|прибыл|персонал|численност|сотрудник|капитализац)"
                r"\w*\s*(?:of|is|was|:|=|—|–|-)?\s*(?:US\$|\$|€|£|₽)?\s*\d", re.IGNORECASE),
     "financial_figure", "no revenue/profit/headcount/market-cap figures in a dossier"),
    (re.compile(r"(?:US\$|\$|€|£|₽)\s?\d"), "financial_figure",
     "no currency amounts in a dossier"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:million|billion|bn|trillion)\b", re.IGNORECASE),
     "financial_figure", "no financial magnitudes (million/billion) in a dossier"),
    (re.compile(r"\b\d+\s*(?:млрд|млн|трлн)\b"), "financial_figure",
     "no financial magnitudes (млн/млрд) in a dossier"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:mb/d|kb/d|bpd|boe/d|т/сут|барр)\b", re.IGNORECASE),
     "financial_figure", "no production-volume figures in a dossier"),
)


class WesternResearchError(ValueError):
    """Structural problem with the package (missing directory or accounts registry)."""


def _issue(account: str | None, field: str, code: str, message: str) -> dict:
    return {"account": account, "field": field, "code": code, "message": message}


def parse_dossier(text: str) -> dict:
    """Split a dossier into its header fields, section titles and section bodies."""
    matches = list(SECTION_RE.finditer(text))
    first_section = matches[0].start() if matches else len(text)
    header_text = text[:first_section]
    fields = {match.group(1): match.group(2).strip() for match in FIELD_RE.finditer(header_text)}
    sections: dict[int, str] = {}
    titles: dict[int, str] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[number] = text[match.end():end].strip("\n")
        titles[number] = match.group(2).strip()
    return {"fields": fields, "sections": sections, "titles": titles}


def numbered_items(section: str) -> list[str]:
    """Return the bodies of ``1. ...`` / ``1) ...`` lines, trimmed."""
    return [match.group(2).strip() for match in NUMBERED_RE.finditer(section)]


def table_rows(section: str) -> list[str]:
    """Return data rows of a Markdown table (header and separator excluded)."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        inner = line.strip("|").strip()
        if not inner or set(inner) <= set("-: "):
            continue
        rows.append(inner)
    return rows[1:] if rows else []


def use_case_rows(section: str) -> list[str]:
    """Return numbered use-case table rows (first cell is a digit), full lines."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0].isdigit():
            rows.append(line)
    return rows


def verify_items(section: str) -> list[str]:
    """Return the unchecked checklist items of the 'what to check' section."""
    return [match.group(1).strip() for match in VERIFY_ITEM_RE.finditer(section)]


def modules_in(rows: list[str]) -> set[str]:
    """Return the known module ids referenced by the given use-case rows."""
    found: set[str] = set()
    for row in rows:
        found |= set(MODULE_TOKEN_RE.findall(row)) & MODULE_IDS
    return found


def load_accounts(path: Path = DEFAULT_ACCOUNTS) -> list[dict]:
    """Load and validate the accounts registry (a JSON object with a non-empty list)."""
    path = Path(path)
    if not path.exists():
        raise WesternResearchError(f"accounts registry not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WesternResearchError(f"accounts registry is not valid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != ACCOUNTS_SCHEMA:
        raise WesternResearchError(f"accounts registry must use schema {ACCOUNTS_SCHEMA}")
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise WesternResearchError("accounts registry has no accounts list")
    for row in accounts:
        if not isinstance(row, dict) or not all(row.get(key) for key in
                                                ("id", "company", "segment", "segment_ru", "hq")):
            raise WesternResearchError(f"accounts registry entry is incomplete: {row!r}")
    return accounts


def validate_text(text: str, account: str | None = None) -> list[dict]:
    """Validate one dossier body and return a deterministic list of issues."""
    issues: list[dict] = []
    parsed = parse_dossier(text)
    fields = parsed["fields"]
    sections = parsed["sections"]
    titles = parsed["titles"]

    for field in REQUIRED_FIELDS:
        if not fields.get(field):
            issues.append(_issue(account, field, "required", f"{field} is missing or empty"))

    status = fields.get("evidence_status", "")
    if status and status not in EVIDENCE_STATUSES:
        issues.append(_issue(account, "evidence_status", "evidence_status",
                             f"evidence_status must be one of {list(EVIDENCE_STATUSES)}"))
    elif status == "VERIFIED":
        issues.append(_issue(account, "evidence_status", "evidence_status",
                             "VERIFIED is not allowed until a first-party source is fetched"))

    dossier_date = fields.get("dossier_date", "")
    if dossier_date:
        if not DATE_RE.match(dossier_date):
            issues.append(_issue(account, "dossier_date", "date_format",
                                 "dossier_date must be YYYY-MM-DD"))
        else:
            try:
                date.fromisoformat(dossier_date)
            except ValueError:
                issues.append(_issue(account, "dossier_date", "date_format",
                                     "dossier_date must be a real date"))

    expected_sections = set(SECTION_TITLES)
    present = set(sections)
    for number in sorted(expected_sections - present):
        issues.append(_issue(account, f"section_{number}", "section_missing",
                             f"section {number} ({SECTION_TITLES[number]}) is missing"))
    for number in sorted(present - expected_sections):
        issues.append(_issue(account, f"section_{number}", "section_unexpected",
                             f"unexpected section {number}"))

    for number, keyword in SECTION_TITLES.items():
        title = titles.get(number)
        if title is not None and keyword.casefold() not in title.casefold():
            issues.append(_issue(account, f"section_{number}", "section_title",
                                 f"section {number} title must mention '{keyword}'"))

    # Evidence: the file status must equal the strongest tag used in section 1.
    section_one = sections.get(1, "")
    tags = set(TAG_RE.findall(section_one))
    if not tags:
        issues.append(_issue(account, "evidence", "evidence_tags",
                             "section 1 needs at least one evidence tag"))
    elif status in EVIDENCE_RANK:
        strongest = max(tags, key=lambda tag: EVIDENCE_RANK[tag])
        if EVIDENCE_RANK[strongest] != EVIDENCE_RANK[status]:
            issues.append(_issue(account, "evidence_status", "evidence_status_mismatch",
                                 f"evidence_status {status} != strongest tag [{strongest}]"))

    # A PARTIAL dossier needs a fetched source; a NOT_FETCHED dossier must not pretend to.
    urls = URL_RE.findall(text)
    if status == "PARTIAL" and not urls:
        issues.append(_issue(account, "sources", "source_url",
                             "PARTIAL dossier must cite at least one https:// source in section 9"))
    if status == "NOT_FETCHED" and urls:
        issues.append(_issue(account, "sources", "source_url_mismatch",
                             "NOT_FETCHED dossier must not cite fetched sources"))

    # Pain hypotheses: exactly three, each explicitly a hypothesis.
    pains = numbered_items(sections.get(2, ""))
    if len(pains) != MIN_PAINS:
        issues.append(_issue(account, "pains", "pain_count",
                             f"expected {MIN_PAINS} pain hypotheses, got {len(pains)}"))
    for index, pain in enumerate(pains, start=1):
        if "[HYPOTHESIS]" not in pain:
            issues.append(_issue(account, f"pain_{index}", "pain_evidence",
                                 "every pain hypothesis must be tagged [HYPOTHESIS]"))
        if len(pain) < 40:
            issues.append(_issue(account, f"pain_{index}", "pain_short",
                                 "pain hypothesis is too short to be actionable"))

    # Discovery questions: exactly five, each a question.
    questions = numbered_items(sections.get(6, ""))
    if len(questions) != MIN_QUESTIONS:
        issues.append(_issue(account, "questions", "question_count",
                             f"expected {MIN_QUESTIONS} discovery questions, got {len(questions)}"))
    for index, question in enumerate(questions, start=1):
        if not question.rstrip().endswith("?"):
            issues.append(_issue(account, f"question_{index}", "question_format",
                                 "discovery question must end with '?'"))
        if len(question) < 20:
            issues.append(_issue(account, f"question_{index}", "question_short",
                                 "discovery question is too short"))

    # Use cases: exactly three rows, each referencing a known module id.
    rows = use_case_rows(sections.get(5, ""))
    if len(rows) != MIN_USE_CASES:
        issues.append(_issue(account, "use_cases", "use_case_count",
                             f"expected {MIN_USE_CASES} use cases, got {len(rows)}"))
    for index, row in enumerate(rows, start=1):
        if not modules_in([row]):
            issues.append(_issue(account, f"use_case_{index}", "use_case_module",
                                 "use case must reference a known AMFE module id "
                                 "(e.g. oil_price_scenarios)"))
    found_modules = modules_in(rows)
    if rows and len(found_modules) < MIN_MODULE_DIVERSITY:
        issues.append(_issue(account, "use_cases", "use_case_diversity",
                             f"use cases must reference at least {MIN_MODULE_DIVERSITY} "
                             "distinct modules"))

    # Triggers: all four trigger classes must be present.
    section_four = sections.get(4, "").casefold()
    for stem in TRIGGER_STEMS:
        if stem not in section_four:
            issues.append(_issue(account, "triggers", "trigger_missing",
                                 f"trigger class '{stem}' is missing from section 4"))

    # Buying center: roles only, enough of them, explicit policy line.
    buying_rows = table_rows(sections.get(3, ""))
    if len(buying_rows) < MIN_BUYING_CENTER_ROLES:
        issues.append(_issue(account, "buying_center", "buying_center_rows",
                             f"expected >= {MIN_BUYING_CENTER_ROLES} buying-center roles, "
                             f"got {len(buying_rows)}"))
    if "роли, не имена" not in sections.get(3, "").casefold():
        issues.append(_issue(account, "buying_center", "buying_center_policy",
                             "section 3 must state 'Роли, не имена'"))

    # Competitors and the pre-contact checklist.
    competitors = table_rows(sections.get(7, ""))
    if len(competitors) < MIN_COMPETITORS:
        issues.append(_issue(account, "competitors", "competitor_rows",
                             f"expected >= {MIN_COMPETITORS} alternatives, "
                             f"got {len(competitors)}"))
    checks = verify_items(sections.get(8, ""))
    if len(checks) < MIN_VERIFY_ITEMS:
        issues.append(_issue(account, "verify", "verify_items",
                             f"expected >= {MIN_VERIFY_ITEMS} checklist items, got {len(checks)}"))

    # Sources and model status are policy, not decoration.
    if "western_research/README.md" not in text:
        issues.append(_issue(account, "sources", "sources",
                             "section 9 must reference marketing/western_research/README.md"))
    if "RESEARCH_ONLY" not in text:
        issues.append(_issue(account, "model_status", "model_status",
                             "dossier must state RESEARCH_ONLY"))
    if "admission=false" not in text:
        issues.append(_issue(account, "model_status", "model_status",
                             "dossier must state admission=false"))

    for pattern, code, message in FORBIDDEN_PATTERNS:
        if pattern.search(text):
            issues.append(_issue(account, pattern.pattern[:24], code, message))

    return sorted(issues, key=lambda item: (item["account"] or "", item["field"], item["code"]))


def _detail(row: dict, path: Path, parsed: dict) -> dict:
    fields = parsed["fields"]
    sections = parsed["sections"]
    rows = use_case_rows(sections.get(5, ""))
    return {
        "id": row["id"],
        "company": fields.get("company", ""),
        "segment": fields.get("segment", ""),
        "region": fields.get("region", ""),
        "hq": fields.get("hq", ""),
        "evidence_status": fields.get("evidence_status", ""),
        "dossier_date": fields.get("dossier_date", ""),
        "pains": len(numbered_items(sections.get(2, ""))),
        "questions": len(numbered_items(sections.get(6, ""))),
        "use_case_rows": len(rows),
        "modules": sorted(modules_in(rows)),
        "triggers": {stem: stem in sections.get(4, "").casefold() for stem in TRIGGER_STEMS},
        "buying_center_roles": len(table_rows(sections.get(3, ""))),
        "competitors": len(table_rows(sections.get(7, ""))),
        "verify_items": len(verify_items(sections.get(8, ""))),
        "sources": len(URL_RE.findall(parsed.get("text", ""))),
        "path": path.name,
    }


def check(directory: Path = DEFAULT_DIR, accounts_path: Path = DEFAULT_ACCOUNTS) -> dict:
    """Validate the package and return a summary dict (raises :class:`WesternResearchError`)."""
    directory = Path(directory)
    if not directory.exists():
        raise WesternResearchError(f"directory not found: {directory}")
    accounts = load_accounts(accounts_path)

    issues: list[dict] = []
    if len(accounts) != REQUIRED_ACCOUNTS:
        issues.append(_issue(None, "accounts", "accounts_count",
                             f"expected {REQUIRED_ACCOUNTS} accounts, got {len(accounts)}"))
    ids = [row["id"] for row in accounts]
    for duplicate in sorted({account for account in ids if ids.count(account) > 1}):
        issues.append(_issue(duplicate, "id", "id_duplicate",
                             f"account id is duplicated in the registry: {duplicate}"))
    for account in ids:
        if not ID_RE.match(account):
            issues.append(_issue(account, "id", "id_format", "id has an unexpected format"))

    files = sorted(path for path in directory.glob("*.md") if path.name.casefold() != "readme.md")
    found = {path.name for path in files}
    expected = {f"{account.lower()}.md" for account in ids}
    for missing in sorted(expected - found):
        issues.append(_issue(None, "file", "file_missing", f"dossier file is missing: {missing}"))
    for extra in sorted(found - expected):
        issues.append(_issue(None, "file", "file_unexpected",
                             f"unexpected dossier file: {extra}"))

    details: list[dict] = []
    for row in accounts:
        account = row["id"]
        path = directory / f"{account.lower()}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        parsed = parse_dossier(text)
        parsed["text"] = text
        fields = parsed["fields"]
        issues.extend(validate_text(text, account))

        if fields.get("id") != account:
            issues.append(_issue(account, "id", "id_mismatch",
                                 f"header id {fields.get('id')!r} != {account!r}"))
        for field, expected_value in (("company", row["company"]), ("segment", row["segment"]),
                                      ("segment_ru", row["segment_ru"]), ("hq", row["hq"]),
                                      ("region", row.get("region", ""))):
            if expected_value and fields.get(field) != expected_value:
                issues.append(_issue(account, field, f"{field}_mismatch",
                                     f"{field} {fields.get(field)!r} != registry "
                                     f"{expected_value!r}"))
        details.append(_detail(row, path, parsed))

    return build_summary(details, issues)


def build_summary(details: list[dict], issues: list[dict]) -> dict:
    """Deterministic machine summary of the package (never includes contacts)."""
    evidence = {status: 0 for status in EVIDENCE_STATUSES}
    segments: dict[str, int] = {}
    regions: dict[str, int] = {}
    coverage: dict[str, int] = {}
    triggers = {stem: 0 for stem in TRIGGER_STEMS}
    for detail in details:
        if detail["evidence_status"] in evidence:
            evidence[detail["evidence_status"]] += 1
        if detail["segment"]:
            segments[detail["segment"]] = segments.get(detail["segment"], 0) + 1
        if detail["region"]:
            regions[detail["region"]] = regions.get(detail["region"], 0) + 1
        for module in detail["modules"]:
            coverage[module] = coverage.get(module, 0) + 1
        for stem, present in detail["triggers"].items():
            if present:
                triggers[stem] += 1
    return {
        "schema": SCHEMA,
        "accounts": len(details),
        "valid": not issues,
        "errors": len(issues),
        "evidence": evidence,
        "segments": dict(sorted(segments.items())),
        "regions": dict(sorted(regions.items())),
        "use_case_coverage": dict(sorted(coverage.items())),
        "trigger_coverage": triggers,
        "dossiers": details,
        "issues": issues,
        "policy": {
            "model_status": "RESEARCH_ONLY",
            "admission": False,
            "sending": "human_only",
            "contacts": "none",
            "financials": "none",
            "external_verification": "secondary_sources_only",
            "note": "Dossiers are drafts: no contacts, no first-party verification, nothing is "
                    "sent by tooling.",
        },
    }


def render_summary(summary: dict) -> str:
    """Human-readable summary; deterministic line order."""
    lines = [
        f"AMFE western research: {summary['accounts']} досье, ошибок {summary['errors']}, "
        f"valid={str(summary['valid']).lower()}",
        "Evidence: " + ", ".join(f"{status}={count}"
                                 for status, count in summary["evidence"].items()),
        "Сегменты: " + (", ".join(f"{segment}={count}"
                                  for segment, count in summary["segments"].items()) or "нет"),
        "Регионы: " + (", ".join(f"{region}={count}"
                                 for region, count in summary["regions"].items()) or "нет"),
        "Триггеры: " + ", ".join(f"{stem}={count}"
                                 for stem, count in summary["trigger_coverage"].items()),
        "Модули: " + (", ".join(f"{name}={count}"
                                for name, count in summary["use_case_coverage"].items())
                      or "нет"),
        "",
        "Досье:",
    ]
    for detail in summary["dossiers"]:
        missing = [stem for stem, present in detail["triggers"].items() if not present]
        trigger_note = "" if not missing else f", нет триггеров: {','.join(missing)}"
        lines.append(f"  {detail['id']:<14} {detail['evidence_status']:<11} "
                     f"pains={detail['pains']} questions={detail['questions']} "
                     f"use_cases={detail['use_case_rows']} modules={len(detail['modules'])} "
                     f"roles={detail['buying_center_roles']} verify={detail['verify_items']}"
                     f"{trigger_note}")
    if summary["issues"]:
        lines.append("")
        lines.append("Ошибки:")
        for issue in summary["issues"]:
            where = issue["account"] or "файл"
            lines.append(f"  [{where}] {issue['field']}: {issue['message']} ({issue['code']})")
    else:
        lines.append("")
        lines.append("Ошибок нет: схема, 10 досье, evidence, use cases, триггеры, роли и "
                     "отсутствие контактов/финпоказателей в порядке.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the AMFE western-account dossiers")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
    parser.add_argument("--check", action="store_true", help="validate only; exit 2 on any issue")
    parser.add_argument("--summary", action="store_true", help="human-readable summary")
    parser.add_argument("--json", action="store_true", help="machine summary as JSON")
    args = parser.parse_args(argv)

    try:
        summary = check(Path(args.dir), Path(args.accounts))
    except WesternResearchError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2

    code = 0 if summary["valid"] else 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.check:
        if summary["valid"]:
            evidence = "/".join(str(summary["evidence"][status]) for status in EVIDENCE_STATUSES)
            print(f"OK: {summary['accounts']} dossiers, evidence {evidence}")
        else:
            print(f"FAIL: {len(summary['issues'])} issue(s)", file=sys.stderr)
            for issue in summary["issues"]:
                where = issue["account"] or "file"
                print(f"  [{where}] {issue['field']}: {issue['message']} ({issue['code']})",
                      file=sys.stderr)
    else:
        print(render_summary(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
