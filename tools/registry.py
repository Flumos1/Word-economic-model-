#!/usr/bin/env python3
"""Append-only journal of future forecasts of the AMFE evaluation protocol v4.

Protocol v4, forward_registry:
  * record: candidate_id, code_commit, data_vintage_hash, target, horizon, origin,
    quantiles_5_to_95_step_5 (T1 and T2) or event_probability (T3), point, scoring_rule_commit;
  * committed to the repository before the actual value is published; rows are append-only;
    a correction is a new row that references the old one.

The journal stores metadata and forecasts only (identifiers, dates, hashes, numbers), never raw
third-party data, so it is committed: registry/forecasts.jsonl (one canonical JSON object per
line, UTF-8, LF).  The data itself stays in the git-ignored data directory.

Write discipline (enforced by tools/registry.py, the only intended writer):
  * a record is validated against the schema before it is appended;
  * a duplicate (candidate_id, target, horizon, origin) is rejected unless the new row is a
    correction that carries correction_of = SHA-256 of the row it replaces;
  * no function edits, deletes or reorders rows; the file is only ever opened in append mode;
  * the record digest is the SHA-256 of the canonical JSON of the row (sorted keys, compact),
    the value a later correction must reference in correction_of.

forward_registry.start in the protocol is null: the start is the date of the first registry commit
(no backdating), and the journal records it as the date of its first row (registered_at).

Usage:
    python tools/registry.py --validate
    python tools/registry.py --append path/to/record.json
    python tools/registry.py --list
    python tools/registry.py --status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOURNAL = ROOT / "registry" / "forecasts.jsonl"

SCHEMA_ID = "AMFE-FORWARD-REGISTRY/1"
QUANTILES = [round(0.05 * i, 2) for i in range(1, 20)]
HEX = re.compile(r"^[0-9a-f]{7,64}$")
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
WEEK = re.compile(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")
TARGETS = {
    "T1_CPI_YOY": {"horizons": [1, 3, 6, 12], "kind": "quantiles"},
    "T2_GDP_GROWTH": {"horizons": [0, 1, 2, 4], "kind": "quantiles"},
    "T3_RECESSION": {"horizons": [3, 6, 12], "kind": "event"},
}
COMMON_FIELDS = ["candidate_id", "code_commit", "data_vintage_hash", "target", "horizon",
                 "origin", "scoring_rule_commit"]
QUANTILE_FIELDS = ["quantiles_5_to_95_step_5", "point"]
EVENT_FIELDS = ["event_probability"]
OPTIONAL_FIELDS = ["point", "registered_at", "correction_of", "notes", "control_arm",
                   "vintage_week"]


class RegistryError(RuntimeError):
    pass


def canonical_json(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_digest(row: dict) -> str:
    """SHA-256 of the canonical JSON of a row; a correction references the old digest."""
    return hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()


def required_fields(target: str) -> list[str]:
    kind = TARGETS[target]["kind"]
    return COMMON_FIELDS + (QUANTILE_FIELDS if kind == "quantiles" else EVENT_FIELDS)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_record(record: dict) -> list[str]:
    """Schema validation.  Returns a list of human-readable problems (empty when valid)."""
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    target = record.get("target")
    if target not in TARGETS:
        return [f"target must be one of {sorted(TARGETS)}"]
    for field in required_fields(target):
        if field not in record or record[field] is None:
            problems.append(f"missing required field {field}")
    for field in record:
        if field not in required_fields(target) and field not in OPTIONAL_FIELDS:
            problems.append(f"unknown field {field}")

    candidate_id = record.get("candidate_id")
    if candidate_id is not None:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            problems.append("candidate_id must be a non-empty string")
        elif len(candidate_id) > 120:
            problems.append("candidate_id is longer than 120 characters")

    for field in ("code_commit", "scoring_rule_commit"):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not HEX.match(value)):
            problems.append(f"{field} must be a git commit or hash: 7-64 lowercase hex characters")
    vintage = record.get("data_vintage_hash")
    if vintage is not None:
        if not isinstance(vintage, str) or not re.fullmatch(r"[0-9a-f]{64}", vintage):
            problems.append("data_vintage_hash must be a lowercase hex SHA-256 (64 characters)")

    horizon = record.get("horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        problems.append("horizon must be an integer")
    elif horizon not in TARGETS[target]["horizons"]:
        problems.append(f"horizon {horizon} is not in {TARGETS[target]['horizons']} for {target}")

    origin = record.get("origin")
    if origin is not None and (not isinstance(origin, str) or not MONTH.match(origin)):
        problems.append("origin must be a month string YYYY-MM (month end)")
    elif isinstance(origin, str) and origin > date.today().strftime("%Y-%m"):
        problems.append("origin is in the future; the journal is written after the month end")

    if TARGETS[target]["kind"] == "quantiles":
        quantiles = record.get("quantiles_5_to_95_step_5")
        if quantiles is not None:
            if not isinstance(quantiles, list) or len(quantiles) != len(QUANTILES):
                problems.append(f"quantiles_5_to_95_step_5 must be a list of {len(QUANTILES)} numbers")
            elif not all(_is_number(q) for q in quantiles):
                problems.append("quantiles_5_to_95_step_5 must contain numbers only")
            else:
                if any(b <= a for a, b in zip(quantiles, quantiles[1:])):
                    problems.append("quantiles_5_to_95_step_5 must be strictly increasing")
                if any(q == 0 for q in quantiles):
                    problems.append("quantiles_5_to_95_step_5 must not be exactly zero (degenerate grid)")
        point = record.get("point")
        if point is not None and not _is_number(point):
            problems.append("point must be a number for T1 and T2")
    else:
        probability = record.get("event_probability")
        if probability is not None:
            if not _is_number(probability) or not (0.0 <= probability <= 1.0):
                problems.append("event_probability must be a number in [0, 1]")
        if record.get("point") is not None:
            problems.append("point is not defined for T3; leave it null or absent")
        if "quantiles_5_to_95_step_5" in record:
            problems.append("quantiles_5_to_95_step_5 does not apply to T3")

    registered_at = record.get("registered_at")
    if registered_at is not None:
        try:
            datetime.fromisoformat(str(registered_at))
        except ValueError:
            problems.append("registered_at must be an ISO 8601 date or date-time")
    vintage_week = record.get("vintage_week")
    if vintage_week is not None and (not isinstance(vintage_week, str) or not WEEK.fullmatch(vintage_week)):
        problems.append("vintage_week must be an ISO week string YYYY-Www (v5 D-156)")
    correction = record.get("correction_of")
    if correction is not None and (not isinstance(correction, str) or not re.fullmatch(r"[0-9a-f]{64}", correction)):
        problems.append("correction_of must be the SHA-256 digest of the corrected row")
    notes = record.get("notes")
    if notes is not None and not isinstance(notes, str):
        problems.append("notes must be a string")
    control_arm = record.get("control_arm")
    if control_arm is not None and not isinstance(control_arm, bool):
        problems.append("control_arm must be a boolean")
    return problems


def identity(row: dict) -> tuple:
    """A forecast is identified by the pair, the horizon, the origin and the vintage week.

    v5 5.0.1 (D-156): the weekly vintage row of the same origin month is a distinct row and carries
    ``vintage_week``; the monthly anchor row has no vintage_week (None).
    """
    return (row.get("candidate_id"), row.get("target"), row.get("horizon"), row.get("origin"),
            row.get("vintage_week"))


def read_rows(journal: Path = DEFAULT_JOURNAL) -> list[dict]:
    if not journal.exists():
        return []
    rows: list[dict] = []
    for number, line in enumerate(journal.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise RegistryError(f"{journal}: line {number} is not JSON: {error}") from error
    return rows


def validate_journal(journal: Path = DEFAULT_JOURNAL) -> tuple[list[dict], list[str]]:
    """Validate every row and the append-only rules (duplicates, correction references)."""
    rows = read_rows(journal)
    problems: list[str] = []
    seen: dict[tuple, dict] = {}
    digests = {record_digest(row) for row in rows}
    for number, row in enumerate(rows, start=1):
        for problem in validate_record(row):
            problems.append(f"line {number}: {problem}")
        key = identity(row)
        if key in seen:
            correction = row.get("correction_of")
            if correction is None:
                problems.append(f"line {number}: duplicate {key} without correction_of")
            elif correction != record_digest(seen[key]):
                problems.append(f"line {number}: correction_of does not reference the replaced row")
            else:
                seen[key] = row
        else:
            seen[key] = row
        correction = row.get("correction_of")
        if correction is not None and correction not in digests:
            problems.append(f"line {number}: correction_of references a row that is not in the journal")
    return rows, problems


def append_record(record: dict, journal: Path = DEFAULT_JOURNAL) -> dict:
    """Validate, check the append-only rules and append one canonical JSON line."""
    problems = validate_record(record)
    if problems:
        raise RegistryError("; ".join(problems))
    journal.parent.mkdir(parents=True, exist_ok=True)
    rows, journal_problems = validate_journal(journal) if journal.exists() else ([], [])
    if journal_problems:
        raise RegistryError("journal is not valid, refusing to append: " + "; ".join(journal_problems))
    key = identity(record)
    existing = {identity(row): row for row in rows}
    if key in existing:
        correction = record.get("correction_of")
        if correction is None:
            raise RegistryError(f"duplicate {key}: append-only, a correction must set correction_of")
        if correction != record_digest(existing[key]):
            raise RegistryError("correction_of does not reference the row it replaces")
    elif record.get("correction_of") is not None:
        raise RegistryError("correction_of set but no row with that identity exists")
    line = canonical_json(record) + "\n"
    with journal.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    return {"appended": True, "identity": list(key), "digest": record_digest(record),
            "bytes": len(line.encode("utf-8")), "journal": repo_path(journal)}


def repo_path(path: Path) -> str:
    """Path relative to the repository when possible, absolute-friendly string otherwise."""
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def journal_status(journal: Path = DEFAULT_JOURNAL) -> dict:
    rows, problems = validate_journal(journal)
    dates = sorted(row["registered_at"] for row in rows if row.get("registered_at"))
    pairs = sorted({(row["target"], row["horizon"]) for row in rows})
    return {
        "journal": repo_path(journal),
        "rows": len(rows),
        "valid": not problems,
        "problems": problems,
        "first_registered_at": dates[0] if dates else None,
        "last_registered_at": dates[-1] if dates else None,
        "pairs": [{"target": t, "horizon": h} for t, h in pairs],
        "forward_registry_start": (dates[0][:10] if dates else None),
    }


def load_record_file(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise RegistryError("a record file must contain one JSON object")
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Append-only forward-forecast journal")
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    parser.add_argument("--validate", action="store_true", help="validate the journal")
    parser.add_argument("--append", metavar="FILE", help="validate and append one JSON record")
    parser.add_argument("--list", action="store_true", help="print every row")
    parser.add_argument("--status", action="store_true", help="journal status summary")
    args = parser.parse_args(argv)
    journal = Path(args.journal)
    try:
        if args.append:
            print(json.dumps(append_record(load_record_file(Path(args.append)), journal),
                             ensure_ascii=False))
        elif args.list:
            for row in read_rows(journal):
                print(canonical_json(row))
        elif args.validate:
            _, problems = validate_journal(journal)
            print(json.dumps({"valid": not problems, "problems": problems}, ensure_ascii=False))
            return 0 if not problems else 1
        else:
            print(json.dumps(journal_status(journal), ensure_ascii=False, indent=2))
        return 0
    except RegistryError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
