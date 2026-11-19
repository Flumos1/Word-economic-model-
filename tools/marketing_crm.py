#!/usr/bin/env python3
"""Validate and summarise the AMFE marketing CRM pipeline (``marketing/crm_pipeline.csv``).

The CRM is a plain CSV: 20 target accounts, one row per account, 8 required columns. The tool is
deterministic (no clock, no network), fail-closed (any problem is an error, exit 2), and it never
writes files. It also cross-checks the pipeline against ``marketing/target_accounts.json`` so the
tier in the CRM cannot drift from the scored ranking (``tools/marketing_targets.py``).

Usage::

    python tools/marketing_crm.py --check      # exit 0 / 2, prints the issues
    python tools/marketing_crm.py --summary    # human-readable summary
    python tools/marketing_crm.py --json       # machine summary (schema amfe.marketing.crm.v1)
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "marketing" / "crm_pipeline.csv"
DEFAULT_TARGETS = ROOT / "marketing" / "target_accounts.json"
TARGETS_TOOL = ROOT / "tools" / "marketing_targets.py"

SCHEMA = "amfe.marketing.crm.v1"
FIELDS = ("id", "company", "tier", "owner", "stage", "next_action", "next_date", "notes")
STAGES = ("NEW", "CONTACTED", "DISCOVERY", "PILOT", "PROPOSAL", "WON", "LOST")
CLOSED_STAGES = ("WON", "LOST")
TIERS = ("A", "B", "C")
MIN_ACCOUNTS = 20

ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Contacts must not be invented: no addresses, no phone numbers, no raw observations.
FORBIDDEN_PATTERNS = (
    (re.compile(r"@"), "email-like value", "no invented e-mail addresses"),
    (re.compile(r"\+7[\s\-]?\(?\d{3}"), "phone-like value", "no invented phone numbers"),
    (re.compile(r"\btel:", re.IGNORECASE), "tel: link", "no invented phone numbers"),
    (re.compile(r"\bmailto:", re.IGNORECASE), "mailto: link", "no invented e-mail addresses"),
)


class CrmError(ValueError):
    """Structural problem with the CRM file (missing file, bad header)."""


def _clean(row: dict) -> dict:
    return {field: (row.get(field) or "").strip() for field in FIELDS}


def read_csv(path: Path = DEFAULT_PATH) -> tuple[tuple[str, ...], list[dict]]:
    """Read the CRM CSV and return ``(header, rows)``; raises :class:`CrmError`."""
    path = Path(path)
    if not path.exists():
        raise CrmError(f"file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = tuple(reader.fieldnames or ())
    if not header:
        raise CrmError("empty file: no header row")
    rows = [_clean(row) for row in reader]
    rows = [row for row in rows if any(row.values())]
    return header, rows


def load_targets(path: Path = DEFAULT_TARGETS) -> dict[str, dict]:
    """Load the curated target accounts keyed by id (optional cross-check source)."""
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return {}
    return {row["id"]: row for row in data if isinstance(row, dict) and "id" in row}


def _load_scorer():
    """Load ``tools/marketing_targets.py`` by path, or return ``None`` when unavailable."""
    if not TARGETS_TOOL.exists():
        return None
    spec = importlib.util.spec_from_file_location("marketing_targets", TARGETS_TOOL)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - defensive: scorer is optional
        return None
    return module if hasattr(module, "score") and hasattr(module, "tier") else None


def expected_tiers(targets: dict[str, dict]) -> dict[str, str]:
    """Tier per account id, recomputed from the public scoring tool (empty when unavailable)."""
    scorer = _load_scorer()
    if scorer is None or not targets:
        return {}
    tiers = {}
    for row_id, row in targets.items():
        try:
            tiers[row_id] = scorer.tier(scorer.score(row))
        except (KeyError, TypeError, ValueError):
            continue
    return tiers


def _issue(row: int | None, field: str, code: str, message: str) -> dict:
    return {"row": row, "field": field, "code": code, "message": message}


def _check_date(value: str) -> bool:
    if not DATE_PATTERN.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate(rows: list[dict], header: tuple[str, ...] = FIELDS,
             targets: dict[str, dict] | None = None,
             tiers: dict[str, str] | None = None) -> list[dict]:
    """Return a deterministic, sorted list of issues (an empty list means the CRM is valid)."""
    issues: list[dict] = []
    if header != FIELDS:
        missing = [field for field in FIELDS if field not in header]
        extra = [field for field in header if field not in FIELDS]
        issues.append(_issue(None, "header", "header_mismatch",
                             f"expected {list(FIELDS)}; got {list(header)}; "
                             f"missing={missing}; extra={extra}"))
    if len(rows) != MIN_ACCOUNTS:
        issues.append(_issue(None, "rowcount", "count",
                             f"expected {MIN_ACCOUNTS} accounts, got {len(rows)}"))

    seen_ids: dict[str, int] = {}
    seen_companies: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        for field in FIELDS:
            if not row[field]:
                issues.append(_issue(index, field, "required", f"{field} is empty"))

        row_id = row["id"]
        if row_id and not ID_PATTERN.match(row_id):
            issues.append(_issue(index, "id", "id_format",
                                 "id must match ^[A-Z][A-Z0-9_]{1,31}$"))
        if row_id:
            if row_id in seen_ids:
                issues.append(_issue(index, "id", "duplicate_id",
                                     f"duplicate id {row_id} (first at row {seen_ids[row_id]})"))
            else:
                seen_ids[row_id] = index
        company = row["company"]
        if company:
            if company in seen_companies:
                issues.append(_issue(index, "company", "duplicate_company",
                                     f"duplicate company (first at row {seen_companies[company]})"))
            else:
                seen_companies[company] = index

        tier = row["tier"]
        if tier and tier not in TIERS:
            issues.append(_issue(index, "tier", "tier", f"tier must be one of {list(TIERS)}"))
        if tier and tiers and row_id in tiers and tier != tiers[row_id]:
            issues.append(_issue(index, "tier", "tier_mismatch",
                                 f"tier {tier} != scored tier {tiers[row_id]}"))

        stage = row["stage"]
        if stage and stage not in STAGES:
            issues.append(_issue(index, "stage", "stage", f"stage must be one of {list(STAGES)}"))
        elif stage and stage.islower():
            issues.append(_issue(index, "stage", "stage_case", "stage must be upper case"))

        next_date = row["next_date"]
        if next_date and not _check_date(next_date):
            issues.append(_issue(index, "next_date", "date_format",
                                 "next_date must be a real ISO date YYYY-MM-DD"))
        if stage and stage not in CLOSED_STAGES and not next_date:
            issues.append(_issue(index, "next_date", "date_required",
                                 f"next_date is required for stage {stage or '?'}"))
        if len(row["next_action"]) < 10:
            issues.append(_issue(index, "next_action", "too_short",
                                 "next_action must be a concrete action (>= 10 chars)"))

        for field in FIELDS:
            value = row[field]
            for pattern, code, message in FORBIDDEN_PATTERNS:
                if value and pattern.search(value):
                    issues.append(_issue(index, field, code, message))

    if targets:
        missing = sorted(set(targets) - set(seen_ids))
        extra = sorted(set(seen_ids) - set(targets))
        if missing:
            issues.append(_issue(None, "id", "missing_account",
                                 f"accounts missing from CRM: {missing}"))
        if extra:
            issues.append(_issue(None, "id", "unknown_account",
                                 f"CRM accounts absent from target_accounts.json: {extra}"))

    return sorted(issues, key=lambda item: (item["row"] or 0, item["field"], item["code"]))


def build_summary(rows: list[dict], issues: list[dict]) -> dict:
    """Deterministic machine summary of the pipeline (never includes contacts)."""
    stage_counts = {stage: 0 for stage in STAGES}
    tier_counts = {tier: 0 for tier in TIERS}
    owner_counts: dict[str, int] = {}
    for row in rows:
        if row["stage"] in stage_counts:
            stage_counts[row["stage"]] += 1
        if row["tier"] in tier_counts:
            tier_counts[row["tier"]] += 1
        if row["owner"]:
            owner_counts[row["owner"]] = owner_counts.get(row["owner"], 0) + 1

    dated = [row for row in rows if row["next_date"]]
    next_actions = [
        {"id": row["id"], "company": row["company"], "tier": row["tier"],
         "stage": row["stage"], "next_date": row["next_date"],
         "next_action": row["next_action"]}
        for row in sorted(dated, key=lambda item: (item["next_date"], item["id"]))
    ]
    return {
        "schema": SCHEMA,
        "accounts": len(rows),
        "valid": not issues,
        "errors": len(issues),
        "stages": stage_counts,
        "tiers": tier_counts,
        "owners": dict(sorted(owner_counts.items())),
        "open_accounts": sum(stage_counts[s] for s in STAGES if s not in CLOSED_STAGES),
        "won": stage_counts["WON"],
        "lost": stage_counts["LOST"],
        "next_date_range": {
            "earliest": next_actions[0]["next_date"] if next_actions else None,
            "latest": next_actions[-1]["next_date"] if next_actions else None,
        },
        "next_actions": next_actions,
        "issues": issues,
        "policy": {
            "model_status": "RESEARCH_ONLY",
            "admission": False,
            "sending": "human_only",
            "note": "Pipeline of drafts: no channel, no verified contacts, nothing is sent by tooling.",
        },
    }


def render_summary(summary: dict) -> str:
    """Human-readable summary; deterministic line order."""
    lines = [
        f"AMFE CRM: {summary['accounts']} аккаунтов, ошибок {summary['errors']}, "
        f"valid={str(summary['valid']).lower()}",
        "Стадии: " + ", ".join(f"{stage}={count}" for stage, count in summary["stages"].items()),
        "Tier: " + ", ".join(f"{tier}={count}" for tier, count in summary["tiers"].items()),
        "Владельцы: " + ", ".join(f"{owner}={count}" for owner, count in summary["owners"].items()),
        f"Открытых: {summary['open_accounts']}, WON: {summary['won']}, LOST: {summary['lost']}",
        f"Ближайшее действие: {summary['next_date_range']['earliest']} — "
        f"последнее: {summary['next_date_range']['latest']}",
    ]
    if summary["next_actions"]:
        lines.append("")
        lines.append("Следующие действия:")
        for item in summary["next_actions"]:
            lines.append(f"  {item['next_date']}  {item['id']:<24} {item['stage']:<10} "
                         f"{item['next_action']}")
    if summary["issues"]:
        lines.append("")
        lines.append("Ошибки:")
        for issue in summary["issues"]:
            where = f"строка {issue['row']}" if issue["row"] else "файл"
            lines.append(f"  [{where}] {issue['field']}: {issue['message']} ({issue['code']})")
    else:
        lines.append("")
        lines.append("Ошибок нет: схема, стадии, tier, даты и отсутствие контактов в порядке.")
    return "\n".join(lines)


def check(path: Path = DEFAULT_PATH, targets_path: Path = DEFAULT_TARGETS) -> dict:
    """Validate the CRM file and return the summary dict (raises :class:`CrmError`)."""
    header, rows = read_csv(path)
    targets = load_targets(targets_path)
    issues = validate(rows, header, targets, expected_tiers(targets))
    return build_summary(rows, issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/summarise the AMFE marketing CRM pipeline")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS))
    parser.add_argument("--check", action="store_true", help="validate only; exit 2 on any issue")
    parser.add_argument("--summary", action="store_true", help="human-readable summary")
    parser.add_argument("--json", action="store_true", help="machine summary as JSON")
    args = parser.parse_args(argv)

    try:
        summary = check(Path(args.path), Path(args.targets))
    except CrmError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2

    code = 0 if summary["valid"] else 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.check:
        if summary["valid"]:
            tiers = "/".join(str(summary["tiers"][tier]) for tier in TIERS)
            print(f"OK: {summary['accounts']} accounts, "
                  f"{summary['next_date_range']['earliest']}..{summary['next_date_range']['latest']}, "
                  f"tiers {tiers}")
        else:
            print(f"FAIL: {len(summary['issues'])} issue(s)", file=sys.stderr)
            for issue in summary["issues"]:
                where = f"row {issue['row']}" if issue["row"] else "file"
                print(f"  [{where}] {issue['field']}: {issue['message']} ({issue['code']})",
                      file=sys.stderr)
    else:
        print(render_summary(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
