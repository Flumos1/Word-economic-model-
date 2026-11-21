#!/usr/bin/env python3
"""Validate the AMFE target-account research dossiers (``marketing/research/*.md``).

The dossiers are hand-written Markdown: one file per account, nine fixed sections, a small
key/value header and explicit evidence tags. This tool is deterministic (no clock, no network),
fail-closed (any problem is an error, exit 2) and it never writes files. It also cross-checks every
dossier against ``marketing/target_accounts.json`` (same id, company, segment and HQ) so the
research cannot silently drift from the curated account list.

The validator enforces the policy of the research package, not just the layout:

* exactly the ten required accounts, one ``<id>.md`` file each;
* ``evidence_status`` is one of VERIFIED / PARTIAL / HYPOTHESIS and matches the strongest tag used
  in section 1;
* three pain hypotheses, three use cases, five discovery questions, four trigger classes;
* no e-mail addresses, phone numbers, messenger handles, personal names or invented revenue /
  headcount figures in a dossier;
* no ``admitted`` / ``допущенный`` claim: the model stays RESEARCH_ONLY, ``admission=false``.

Usage::

    python tools/account_research.py --check      # exit 0 / 2, prints the issues
    python tools/account_research.py --summary    # human-readable summary
    python tools/account_research.py --json       # machine summary (amfe.account_research.v1)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "marketing" / "research"
DEFAULT_TARGETS = ROOT / "marketing" / "target_accounts.json"

SCHEMA = "amfe.account_research.v1"
REQUIRED_IDS = (
    "TATNEFT",
    "TANECO",
    "GAZPROMNEFT",
    "NOVATEK",
    "IRKUTSK_OIL",
    "LUKOIL",
    "ROSNEFT",
    "SURGUTNEFTEGAS",
    "SIBUR",
    "ZARUBEZHNEFT",
)
EVIDENCE_RANK = {"HYPOTHESIS": 0, "PARTIAL": 1, "VERIFIED": 2}
EVIDENCE_STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS")
REQUIRED_FIELDS = (
    "id",
    "company",
    "segment",
    "segment_ru",
    "hq",
    "evidence_status",
    "dossier_date",
    "owner_role",
    "model_status",
)
SECTION_TITLES = {
    1: "чем занимается",
    2: "боли",
    3: "buying center",
    4: "триггеры",
    5: "use cases",
    6: "вопросы",
    7: "конкуренты",
    8: "что проверить",
    9: "источники",
}
TRIGGER_STEMS = ("отчётност", "capex", "импортозамещ", "санкц")
MIN_VERIFY_ITEMS = 5
MIN_BUYING_CENTER_ROLES = 3
MIN_COMPETITORS = 3

FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.+?)\s*$", re.M)
SECTION_RE = re.compile(r"^##\s+(\d)\.\s*(.*)$", re.M)
TAG_RE = re.compile(r"\[(VERIFIED|PARTIAL|HYPOTHESIS)\]")
NUMBERED_RE = re.compile(r"^(\d+)[.)]\s+(.+)$", re.M)
VERIFY_ITEM_RE = re.compile(r"^-\s*\[ \]\s+(.+)$", re.M)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
USE_CASE_TOKEN_RE = re.compile(r"[a-z][a-z_]{3,}")

# Contacts must not be invented: no addresses, phones, messengers or personal names.
FORBIDDEN_PATTERNS = (
    (re.compile(r"@"), "email_like", "no e-mail addresses in a dossier"),
    (re.compile(r"\bmailto:", re.IGNORECASE), "email_like", "no mailto: links in a dossier"),
    (re.compile(r"\btel:", re.IGNORECASE), "phone_like", "no tel: links in a dossier"),
    (re.compile(r"\bskype:", re.IGNORECASE), "phone_like", "no messenger handles in a dossier"),
    (re.compile(r"\btelegram:", re.IGNORECASE), "phone_like", "no messenger handles in a dossier"),
    (re.compile(r"\+7[\s\-(]*\d"), "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"(?<!\d)8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"),
     "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"(?<!\d)\d{3}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}(?!\d)"),
     "phone_like", "no phone numbers in a dossier"),
    (re.compile(r"\bФИО\b"), "personal_name", "no personal names or name fields in a dossier"),
    (re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]+(?:ович|евич|овна|евна|ична)\b"),
     "personal_name", "no personal names (patronymic) in a dossier"),
    (re.compile(r"\badmitted\b", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"допущенн\w*", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"допущен(?:а|о|ы)\b", re.IGNORECASE), "admission_claim",
     "no admitted-model claim: use admission=false"),
    (re.compile(r"(?:выручк|персонал|численност)\w*\s*[:—–-]\s*\d", re.IGNORECASE),
     "revenue_headcount", "no revenue/headcount figures in a dossier"),
)


class ResearchError(ValueError):
    """Structural problem with the research package (missing directory or targets file)."""


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
    """Return the numbered use-case table rows (first cell is a digit), full lines."""
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


def known_use_cases(targets: dict[str, dict]) -> set[str]:
    """Vocabulary of use-case ids from the curated target file."""
    vocabulary: set[str] = set()
    for row in targets.values():
        for item in row.get("use_cases", []) or []:
            if isinstance(item, str):
                vocabulary.add(item)
    return vocabulary


def load_targets(path: Path = DEFAULT_TARGETS) -> dict[str, dict]:
    """Load the curated accounts keyed by id; an absent file means no cross-check."""
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ResearchError(f"target file is not a JSON list: {path}")
    return {row["id"]: row for row in data if isinstance(row, dict) and "id" in row}


def validate_text(text: str, account: str | None = None,
                  vocabulary: set[str] | None = None) -> list[dict]:
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
                             "section 1 needs at least one [VERIFIED]/[PARTIAL]/[HYPOTHESIS] tag"))
    elif status in EVIDENCE_RANK:
        strongest = max(tags, key=lambda tag: EVIDENCE_RANK[tag])
        if EVIDENCE_RANK[strongest] != EVIDENCE_RANK[status]:
            issues.append(_issue(account, "evidence_status", "evidence_status_mismatch",
                                 f"evidence_status {status} != strongest tag [{strongest}]"))

    # Pain hypotheses: exactly three, each explicitly a hypothesis.
    pains = numbered_items(sections.get(2, ""))
    if len(pains) != 3:
        issues.append(_issue(account, "pains", "pain_count",
                             f"expected 3 pain hypotheses, got {len(pains)}"))
    for index, pain in enumerate(pains, start=1):
        if "[HYPOTHESIS]" not in pain:
            issues.append(_issue(account, f"pain_{index}", "pain_evidence",
                                 "every pain hypothesis must be tagged [HYPOTHESIS]"))
        if len(pain) < 40:
            issues.append(_issue(account, f"pain_{index}", "pain_short",
                                 "pain hypothesis is too short to be actionable"))

    # Discovery questions: exactly five, each a question.
    questions = numbered_items(sections.get(6, ""))
    if len(questions) != 5:
        issues.append(_issue(account, "questions", "question_count",
                             f"expected 5 discovery questions, got {len(questions)}"))
    for index, question in enumerate(questions, start=1):
        if not question.rstrip().endswith("?"):
            issues.append(_issue(account, f"question_{index}", "question_format",
                                 "discovery question must end with '?'"))
        if len(question) < 20:
            issues.append(_issue(account, f"question_{index}", "question_short",
                                 "discovery question is too short"))

    # Use cases: exactly three rows, each referencing a known module/use-case id.
    rows = use_case_rows(sections.get(5, ""))
    if len(rows) != 3:
        issues.append(_issue(account, "use_cases", "use_case_count",
                             f"expected 3 use cases, got {len(rows)}"))
    vocabulary = vocabulary or set()
    for index, row in enumerate(rows, start=1):
        found = set(USE_CASE_TOKEN_RE.findall(row)) & vocabulary
        if not found:
            issues.append(_issue(account, f"use_case_{index}", "use_case_module",
                                 "use case must reference an id from target_accounts.json "
                                 "(e.g. oil_price_scenarios)"))

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
                             f"expected >= {MIN_COMPETITORS} alternatives, got {len(competitors)}"))
    checks = verify_items(sections.get(8, ""))
    if len(checks) < MIN_VERIFY_ITEMS:
        issues.append(_issue(account, "verify", "verify_items",
                             f"expected >= {MIN_VERIFY_ITEMS} checklist items, got {len(checks)}"))

    # Sources and model status are part of the policy, not decoration.
    if "target_accounts.json" not in text:
        issues.append(_issue(account, "sources", "sources",
                             "section 9 must reference marketing/target_accounts.json"))
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


def _detail(account: str, path: Path, parsed: dict, vocabulary: set[str]) -> dict:
    fields = parsed["fields"]
    sections = parsed["sections"]
    rows = use_case_rows(sections.get(5, ""))
    use_cases = sorted({token for row in rows for token in
                        (set(USE_CASE_TOKEN_RE.findall(row)) & vocabulary)})
    triggers = {stem: stem in sections.get(4, "").casefold() for stem in TRIGGER_STEMS}
    return {
        "id": account,
        "company": fields.get("company", ""),
        "segment": fields.get("segment", ""),
        "segment_ru": fields.get("segment_ru", ""),
        "hq": fields.get("hq", ""),
        "evidence_status": fields.get("evidence_status", ""),
        "dossier_date": fields.get("dossier_date", ""),
        "pains": len(numbered_items(sections.get(2, ""))),
        "questions": len(numbered_items(sections.get(6, ""))),
        "use_cases": use_cases,
        "use_case_rows": len(rows),
        "triggers": triggers,
        "verify_items": len(verify_items(sections.get(8, ""))),
        "path": path.name,
    }


def check(directory: Path = DEFAULT_DIR, targets_path: Path = DEFAULT_TARGETS) -> dict:
    """Validate the research package and return a summary dict (raises :class:`ResearchError`)."""
    directory = Path(directory)
    if not directory.exists():
        raise ResearchError(f"directory not found: {directory}")
    targets = load_targets(targets_path)
    vocabulary = known_use_cases(targets)

    files = sorted(path for path in directory.glob("*.md") if path.name.casefold() != "readme.md")
    found = {path.name for path in files}
    expected = {f"{account.lower()}.md" for account in REQUIRED_IDS}
    issues: list[dict] = []
    for missing in sorted(expected - found):
        issues.append(_issue(None, "file", "file_missing", f"dossier file is missing: {missing}"))
    for extra in sorted(found - expected):
        issues.append(_issue(None, "file", "file_unexpected",
                             f"unexpected dossier file: {extra}"))

    details: list[dict] = []
    for account in REQUIRED_IDS:
        path = directory / f"{account.lower()}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        parsed = parse_dossier(text)
        fields = parsed["fields"]
        issues.extend(validate_text(text, account, vocabulary))

        if fields.get("id") != account:
            issues.append(_issue(account, "id", "id_mismatch",
                                 f"header id {fields.get('id')!r} != {account!r}"))
        elif not ID_RE.match(account):
            issues.append(_issue(account, "id", "id_format", "id has an unexpected format"))
        target = targets.get(account)
        if target:
            for field in ("company", "segment", "hq"):
                target_field = "name" if field == "company" else field
                expected_value = target.get(target_field)
                if expected_value and fields.get(field) != expected_value:
                    issues.append(_issue(account, field, f"{field}_mismatch",
                                         f"{field} {fields.get(field)!r} != target file "
                                         f"{expected_value!r}"))
        details.append(_detail(account, path, parsed, vocabulary))

    return build_summary(details, issues)


def build_summary(details: list[dict], issues: list[dict]) -> dict:
    """Deterministic machine summary of the research package (never includes contacts)."""
    evidence = {status: 0 for status in EVIDENCE_STATUSES}
    segments: dict[str, int] = {}
    coverage: dict[str, int] = {}
    triggers = {stem: 0 for stem in TRIGGER_STEMS}
    for detail in details:
        if detail["evidence_status"] in evidence:
            evidence[detail["evidence_status"]] += 1
        segment = detail["segment"]
        if segment:
            segments[segment] = segments.get(segment, 0) + 1
        for use_case in detail["use_cases"]:
            coverage[use_case] = coverage.get(use_case, 0) + 1
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
        "use_case_coverage": dict(sorted(coverage.items())),
        "trigger_coverage": triggers,
        "dossiers": details,
        "issues": issues,
        "policy": {
            "model_status": "RESEARCH_ONLY",
            "admission": False,
            "sending": "human_only",
            "external_verification": False,
            "contacts": "none",
            "note": "Dossiers are drafts: no channel, no verified contacts, nothing is sent by tooling.",
        },
    }


def render_summary(summary: dict) -> str:
    """Human-readable summary; deterministic line order."""
    lines = [
        f"AMFE account research: {summary['accounts']} досье, ошибок {summary['errors']}, "
        f"valid={str(summary['valid']).lower()}",
        "Evidence: " + ", ".join(f"{status}={count}"
                                 for status, count in summary["evidence"].items()),
        "Сегменты: " + (", ".join(f"{segment}={count}"
                                  for segment, count in summary["segments"].items()) or "нет"),
        "Триггеры: " + ", ".join(f"{stem}={count}"
                                 for stem, count in summary["trigger_coverage"].items()),
        "Use cases: " + (", ".join(f"{name}={count}"
                                   for name, count in summary["use_case_coverage"].items())
                         or "нет"),
        "",
        "Досье:",
    ]
    for detail in summary["dossiers"]:
        missing = [stem for stem, present in detail["triggers"].items() if not present]
        trigger_note = "" if not missing else f", нет триггеров: {','.join(missing)}"
        lines.append(f"  {detail['id']:<16} {detail['evidence_status']:<10} "
                     f"pains={detail['pains']} questions={detail['questions']} "
                     f"use_cases={detail['use_case_rows']} verify={detail['verify_items']}"
                     f"{trigger_note}")
    if summary["issues"]:
        lines.append("")
        lines.append("Ошибки:")
        for issue in summary["issues"]:
            where = issue["account"] or "файл"
            lines.append(f"  [{where}] {issue['field']}: {issue['message']} ({issue['code']})")
    else:
        lines.append("")
        lines.append("Ошибок нет: схема, 10 досье, evidence, use cases, триггеры и отсутствие "
                     "контактов в порядке.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the AMFE target-account research dossiers")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--check", action="store_true", help="validate only; exit 2 on any issue")
    parser.add_argument("--summary", action="store_true", help="human-readable summary")
    parser.add_argument("--json", action="store_true", help="machine summary as JSON")
    args = parser.parse_args(argv)

    try:
        summary = check(Path(args.dir), Path(args.targets))
    except ResearchError as error:
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
