#!/usr/bin/env python3
"""Human Counsel contour: jurisdiction matrix, panel registry, matter brief and signoff checks.

This tool does **not** create or impersonate a lawyer. It verifies that a real licensed human is
required for every jurisdiction, that no invented counsel is present, and that a signoff carries a
human verifier. Network and clock are not used; nothing is written without ``--out``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "agents" / "legal" / "human_counsel"
MATRIX = LEGAL / "jurisdiction_matrix.json"
PANEL = LEGAL / "panel_registry.json"
DOCS = (LEGAL / "README.md", LEGAL / "charter.md", LEGAL / "skills.md",
        LEGAL / "engagement_protocol.md", LEGAL / "conflict_check.md", LEGAL / "signoff.md")
MATRIX_SCHEMA = "amfe.legal.human_counsel.matrix.v1"
PANEL_SCHEMA = "amfe.legal.human_counsel.panel.v1"
SIGNOFF_SCHEMA = "amfe.legal.signoff.v1"
STATUSES = ("UNASSIGNED", "PRIORITY_UNASSIGNED", "ASSIGNED", "VERIFIED", "SUSPENDED")
LEGAL_SYSTEMS = ("COMMON_LAW", "CIVIL_LAW", "MIXED", "RELIGIOUS_LAW", "CUSTOMARY",
                 "TO_BE_CONFIRMED")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)")
AI_MARKERS = ("ai", "model", "agent", "bot", "assistant", "llm")


class HumanCounselError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HumanCounselError(f"{path}: {exc}") from exc


def _issue(code: str, field: str, message: str) -> dict:
    return {"code": code, "field": field, "message": message}


def validate_matrix(matrix: dict) -> list[dict]:
    issues: list[dict] = []
    if matrix.get("schema") != MATRIX_SCHEMA:
        issues.append(_issue("schema", "schema", f"schema must be {MATRIX_SCHEMA!r}"))
    if matrix.get("advice_ceiling") != "SIGNED_LEGAL_OPINION_BY_LICENSED_HUMAN":
        issues.append(_issue("ceiling", "advice_ceiling",
                             "advice ceiling must require a licensed human"))
    jurisdictions = matrix.get("jurisdictions")
    if not isinstance(jurisdictions, list) or len(jurisdictions) < 190:
        issues.append(_issue("count", "jurisdictions", "at least 190 jurisdictions required"))
        return issues
    seen: set[str] = set()
    for index, item in enumerate(jurisdictions, 1):
        where = f"jurisdictions[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("type", where, "must be an object"))
            continue
        code = item.get("code")
        if not isinstance(code, str) or not re.match(r"^[A-Z]{2}$", code):
            issues.append(_issue("code", where, "code must be ISO2"))
            continue
        if code in seen:
            issues.append(_issue("duplicate", where, f"duplicate code {code}"))
        seen.add(code)
        if item.get("status") not in STATUSES:
            issues.append(_issue("status", where, f"bad status {item.get('status')!r}"))
        if item.get("legal_system") not in LEGAL_SYSTEMS:
            issues.append(_issue("legal_system", where, "bad legal_system"))
        if item.get("local_counsel_required") is not True:
            issues.append(_issue("local_required", where, "local counsel must be required"))
        counsel_id = item.get("counsel_id")
        if counsel_id is not None:
            if not isinstance(counsel_id, str) or not re.match(r"^HC-[A-Z]{2}-[0-9]{3}$", counsel_id):
                issues.append(_issue("counsel_id", where, "counsel_id must be HC-XX-NNN or null"))
        for field in ("name", "notes"):
            text = str(item.get(field, ""))
            if EMAIL_RE.search(text) or PHONE_RE.search(text):
                issues.append(_issue("pii", f"{where}.{field}", "contact data must not be here"))
    if not any(item.get("status", "").startswith("PRIORITY") for item in jurisdictions):
        issues.append(_issue("priority", "jurisdictions", "at least one priority jurisdiction"))
    return issues


def validate_panel(panel: dict, matrix: dict) -> list[dict]:
    issues: list[dict] = []
    if panel.get("schema") != PANEL_SCHEMA:
        issues.append(_issue("schema", "schema", f"schema must be {PANEL_SCHEMA!r}"))
    by_code = {item["code"] for item in matrix.get("jurisdictions", [])}
    counsel = panel.get("counsel")
    if not isinstance(counsel, list):
        issues.append(_issue("counsel", "counsel", "counsel must be a list"))
        return issues
    for index, person in enumerate(counsel, 1):
        where = f"counsel[{index}]"
        if not isinstance(person, dict):
            issues.append(_issue("type", where, "must be an object"))
            continue
        for field in ("id", "jurisdictions", "license", "verified_at", "verified_by"):
            if not person.get(field):
                issues.append(_issue("required", f"{where}.{field}", "missing"))
        if person.get("verified_by") and not str(person["verified_by"]).startswith("human:"):
            issues.append(_issue("verified_by", where, "verified_by must start with human:"))
        for code in person.get("jurisdictions", []):
            if code not in by_code:
                issues.append(_issue("jurisdiction", where, f"unknown jurisdiction {code}"))
        for field in ("name", "email", "phone"):
            if field in person:
                issues.append(_issue("pii", where, f"{field} must stay outside git"))
        if EMAIL_RE.search(json.dumps(person, ensure_ascii=False)) or \
                PHONE_RE.search(json.dumps(person, ensure_ascii=False)):
            issues.append(_issue("pii", where, "contact data detected"))
    return issues


def _brief(matrix: dict, jurisdiction: str, question: str) -> dict:
    entries = {item["code"]: item for item in matrix["jurisdictions"]}
    item = entries.get(jurisdiction)
    if item is None:
        raise HumanCounselError(f"unknown jurisdiction: {jurisdiction}")
    if item["status"] not in ("ASSIGNED", "VERIFIED"):
        raise HumanCounselError(
            f"{jurisdiction}: no assigned human counsel (status={item['status']}); "
            "request engagement; do not issue an opinion")
    return {
        "schema": "amfe.legal.human_counsel.matter.v1",
        "jurisdiction": jurisdiction,
        "country": item["name"],
        "legal_system": item["legal_system"],
        "counsel_id": item["counsel_id"],
        "question": question.strip(),
        "required": ["license_check", "conflict_check", "sanctions_check", "engagement_letter",
                     "nda_if_documents", "human_signoff"],
        "ai_role": "research memo and source plan only; no signature, no legal advice",
        "disclaimer": "Не является юридической консультацией; подписывает лицензированный human lawyer.",
    }


def _validate_signoff(signoff: dict, matrix: dict) -> list[dict]:
    issues: list[dict] = []
    if signoff.get("schema") != SIGNOFF_SCHEMA:
        issues.append(_issue("schema", "schema", f"schema must be {SIGNOFF_SCHEMA!r}"))
    code = signoff.get("jurisdiction")
    entries = {item["code"]: item for item in matrix["jurisdictions"]}
    if code not in entries:
        issues.append(_issue("jurisdiction", "jurisdiction", "unknown jurisdiction"))
    else:
        if entries[code]["status"] not in ("ASSIGNED", "VERIFIED"):
            issues.append(_issue("unassigned", "jurisdiction", "no assigned human counsel"))
        if entries[code]["counsel_id"] != signoff.get("counsel_id"):
            issues.append(_issue("counsel_mismatch", "counsel_id", "counsel does not match matrix"))
    for field in ("matter_id", "counsel_id", "license", "scope", "signed_at", "verified_by"):
        if not signoff.get(field):
            issues.append(_issue("required", field, "missing"))
    if not str(signoff.get("verified_by", "")).startswith("human:"):
        issues.append(_issue("human_verifier", "verified_by", "must start with human:"))
    signer = str(signoff.get("signed_by", ""))
    if any(marker in signer.lower().split() or signer.lower().startswith(marker) for marker in AI_MARKERS):
        issues.append(_issue("ai_signer", "signed_by", "AI cannot sign a legal opinion"))
    if signoff.get("ai_research_memo") is not True:
        issues.append(_issue("ai_memo", "ai_research_memo", "must be true"))
    return issues


def check(root: Path = ROOT) -> tuple[int, str]:
    legal = root / "agents" / "legal" / "human_counsel"
    missing = [str(path.relative_to(root)) for path in DOCS if not (root / path.relative_to(ROOT)).is_file()]
    if missing:
        return 2, f"missing docs: {missing}"
    matrix = _load(MATRIX)
    panel = _load(PANEL)
    issues = validate_matrix(matrix) + validate_panel(panel, matrix)
    if issues:
        return 2, json.dumps({"ok": False, "issues": issues}, ensure_ascii=False)
    assigned = sum(1 for item in matrix["jurisdictions"] if item["counsel_id"])
    return 0, (f"OK: jurisdictions={len(matrix['jurisdictions'])}, assigned={assigned}, "
               f"panel={len(panel['counsel'])}, model={matrix['coverage_model']}, "
               f"human_signoff_required=true")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--jurisdictions", action="store_true")
    parser.add_argument("--region")
    parser.add_argument("--matter", action="store_true")
    parser.add_argument("--jurisdiction")
    parser.add_argument("--question", default="")
    parser.add_argument("--signoff")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    try:
        if args.check:
            code, message = check()
            print(message)
            return code
        matrix = _load(MATRIX)
        if args.matrix:
            payload = {k: v for k, v in matrix.items() if k != "jurisdictions"}
            payload["jurisdictions"] = len(matrix["jurisdictions"])
            print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
            return 0
        if args.jurisdictions:
            rows = matrix["jurisdictions"]
            if args.region:
                rows = [row for row in rows if args.region.lower() in row["region"].lower()]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True))
            else:
                for row in rows:
                    print(f"{row['code']} {row['name']} | {row['legal_system']} | {row['status']}")
            return 0
        if args.matter:
            if not args.jurisdiction or not args.question.strip():
                print("--matter requires --jurisdiction and --question", file=sys.stderr)
                return 2
            brief = _brief(matrix, args.jurisdiction.upper(), args.question)
            text = json.dumps(brief, ensure_ascii=False, indent=1, sort_keys=True)
            if args.out:
                Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
            print(text)
            return 0
        if args.signoff:
            signoff = _load(Path(args.signoff))
            issues = _validate_signoff(signoff, matrix)
            if issues:
                print(json.dumps({"ok": False, "issues": issues}, ensure_ascii=False, indent=1))
                return 2
            print("OK: human signoff valid")
            return 0
        parser.print_help()
        return 0
    except HumanCounselError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
