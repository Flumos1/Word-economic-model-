#!/usr/bin/env python3
"""Validate, extend, search, index and export the AMFE market-intelligence knowledge base.

The tool is deterministic (no network, no clock: pass ``--today``/``--as-of`` explicitly),
fail-closed (any defect is an error, exit 2, nothing is written) and it writes only inside the
knowledge base directory. It never invents data: an entry without a permitted source stays
``HYPOTHESIS``/``NOT_FETCHED``.

Commands::

    python tools/kb.py --check                          # validate, exit 0 / 2
    python tools/kb.py validate --json                  # machine summary
    python tools/kb.py search "TCO" --kind case_template
    python tools/kb.py add --kind method --title ... --summary ... --author-role ... --as-of ...
    python tools/kb.py merge                            # inbox -> entries (validated, no duplicates)
    python tools/kb.py index --today 2026-11-21         # index.json + index.md
    python tools/kb.py export --format json|md [--out path]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB = ROOT / "knowledge_base"
DEFAULT_ACCOUNTS = ROOT / "marketing" / "target_accounts.json"

ENTRY_SCHEMA = "amfe.kb.entry.v1"
SOURCES_SCHEMA = "amfe.kb.sources.v1"
INDEX_SCHEMA = "amfe.kb.index.v1"
VALIDATE_SCHEMA = "amfe.kb.validate.v1"
EXPORT_SCHEMA = "amfe.kb.export.v1"

KINDS = ("segment", "source", "case_template", "target_account", "method", "playbook")
STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_FETCHED")
STATUS_RANK = {"NOT_FETCHED": 0, "HYPOTHESIS": 1, "PARTIAL": 2, "VERIFIED": 3}
GRADES = ("A", "B", "C")
LICENCES = ("OPEN", "ATTRIBUTION", "PUBLIC_DOMAIN", "SUBSCRIPTION", "LICENCE_REQUIRED",
            "INTERNAL", "NONE")
OPEN_LICENCES = ("OPEN", "PUBLIC_DOMAIN", "ATTRIBUTION")
RESTRICTED_LICENCES = ("SUBSCRIPTION", "LICENCE_REQUIRED", "NONE")
SOURCE_CLASSES = ("REGULATOR", "COMPANY_FILING", "OFFICIAL_STATISTICS", "MARKET_DATA_LICENSED",
                  "INDUSTRY_PRESS", "COMPANY_SITE", "REGISTRY", "INTERNAL_CARD")
ACCESS = ("OPEN", "OPEN_REGISTRATION", "SUBSCRIPTION", "LICENCE_REQUIRED",
          "NOT_AVAILABLE_IN_ENV", "INTERNAL")

DEFAULT_TTL = {"segment": 180, "source": 365, "case_template": 180,
               "target_account": 90, "method": 365, "playbook": 365}

ENTRY_FIELDS = (
    "id", "schema", "kind", "title", "summary", "tags", "evidence_status", "evidence_grade",
    "source_ids", "licence", "as_of", "ttl_days", "review_due", "links", "created", "updated",
    "author_role", "notes", "retrieved_at", "account_id", "supersedes", "archived",
)
REQUIRED_ENTRY_FIELDS = (
    "id", "schema", "kind", "title", "summary", "tags", "evidence_status", "evidence_grade",
    "source_ids", "licence", "as_of", "ttl_days", "review_due", "links", "created", "updated",
    "author_role", "notes",
)
SOURCE_FIELDS = (
    "id", "name", "class", "access", "licence", "homepage", "url_checked", "terms_checked",
    "terms_hash", "retrieved_at", "jurisdiction", "allowed_use", "notes",
)

ID_RE = re.compile(r"^KB-\d{4}$")
SOURCE_ID_RE = re.compile(r"^SRC-[A-Z0-9][A-Z0-9_-]{1,40}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https://[^\s]+$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
FIGURE_RE = re.compile(
    r"(?:\d[\d\s.,]*\s*(?:%|₽|\$|€|млн|млрд|тыс))|(?:(?:₽|\$|€)\s*\d[\d\s.,]*)",
    re.IGNORECASE,
)
ADMISSION_RE = re.compile(r"\b(admitted|допущенн\w*|допуск\s+модели)\b", re.IGNORECASE)
FORBIDDEN_TEXT_RE = re.compile(r"(?:\+\d[\d\s()-]{7,}\d)|[\w.+-]+@[\w-]+\.[\w.]+")


class KbError(ValueError):
    """The knowledge base cannot be read at all (fail-closed input)."""


def _issue(code: str, field: str, message: str, where: str | None = None) -> dict:
    return {"code": code, "field": field, "message": message, "where": where}


def _is_date(value: str) -> bool:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def load_accounts(path: Path = DEFAULT_ACCOUNTS) -> set[str]:
    """Account ids from the internal card (empty when unavailable)."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    return {row["id"] for row in data if isinstance(row, dict) and row.get("id")}


def read_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    """Read a JSONL file into ``(records, parse_issues)``; blank lines are ignored."""
    records: list[dict] = []
    issues: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as error:
            issues.append(_issue("json", "line", f"invalid JSON: {error}", f"{path.name}:{number}"))
            continue
        if not isinstance(data, dict):
            issues.append(_issue("json", "line", "record must be a JSON object", f"{path.name}:{number}"))
            continue
        data["__where"] = f"{path.name}:{number}"
        records.append(data)
    return records, issues


def load_sources(kb: Path = DEFAULT_KB) -> dict:
    """Load sources.json; raises :class:`KbError` when it cannot be used."""
    path = Path(kb) / "sources.json"
    if not path.exists():
        raise KbError(f"file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise KbError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(document, dict):
        raise KbError(f"{path}: top-level value must be an object")
    return document


def load_schema(kb: Path = DEFAULT_KB) -> dict:
    """Load schema.json; raises :class:`KbError` when it cannot be used."""
    path = Path(kb) / "schema.json"
    if not path.exists():
        raise KbError(f"file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise KbError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(document, dict):
        raise KbError(f"{path}: top-level value must be an object")
    return document


def load_entries(kb: Path = DEFAULT_KB) -> tuple[list[dict], list[dict]]:
    """All records from ``entries/*.jsonl`` (sorted by file), with parse issues."""
    entries_dir = Path(kb) / "entries"
    records: list[dict] = []
    issues: list[dict] = []
    if not entries_dir.is_dir():
        return records, [_issue("entries_dir", "entries", "entries/ directory is missing")]
    for path in sorted(entries_dir.glob("*.jsonl")):
        part, parse_issues = read_jsonl(path)
        records.extend(part)
        issues.extend(parse_issues)
    if not records and not issues:
        issues.append(_issue("entries_empty", "entries", "no entries found in entries/*.jsonl"))
    return records, issues


def load_inbox(kb: Path = DEFAULT_KB) -> tuple[list[dict], list[dict]]:
    """Records from ``inbox/*.jsonl`` (README.md and other files are ignored)."""
    inbox_dir = Path(kb) / "inbox"
    records: list[dict] = []
    issues: list[dict] = []
    if not inbox_dir.is_dir():
        return records, issues
    for path in sorted(inbox_dir.glob("*.jsonl")):
        part, parse_issues = read_jsonl(path)
        records.extend(part)
        issues.extend(parse_issues)
    return records, issues


def validate_sources(document: dict) -> tuple[dict[str, dict], list[dict]]:
    """Validate the sources document; returns ``(by_id, issues)``."""
    issues: list[dict] = []
    by_id: dict[str, dict] = {}
    if document.get("schema") != SOURCES_SCHEMA:
        issues.append(_issue("sources_schema", "schema", f"schema must be {SOURCES_SCHEMA!r}", "sources.json"))
    version = document.get("version")
    if not isinstance(version, str) or not re.match(r"^\d+\.\d+\.\d+$", version):
        issues.append(_issue("sources_version", "version", "version must look like 1.0.0", "sources.json"))
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources:
        issues.append(_issue("sources_list", "sources", "sources must be a non-empty list", "sources.json"))
        return by_id, issues
    for index, source in enumerate(sources):
        where = f"sources.json:sources[{index}]"
        if not isinstance(source, dict):
            issues.append(_issue("source_type", "sources", "source must be an object", where))
            continue
        for field in SOURCE_FIELDS:
            if field not in source:
                issues.append(_issue("required", field, f"missing {field}", where))
        extra = sorted(set(source) - set(SOURCE_FIELDS))
        if extra:
            issues.append(_issue("unknown_field", "sources", f"unknown fields: {extra}", where))
        source_id = source.get("id")
        if not isinstance(source_id, str) or not SOURCE_ID_RE.match(source_id):
            issues.append(_issue("source_id", "id", "id must match ^SRC-[A-Z0-9][A-Z0-9_-]+$", where))
        elif source_id in by_id:
            issues.append(_issue("duplicate_source_id", "id", f"duplicate source id {source_id}", where))
        else:
            by_id[source_id] = source
        if not isinstance(source.get("name"), str) or len(source.get("name") or "") < 3:
            issues.append(_issue("source_name", "name", "name must be a string of >= 3 chars", where))
        if source.get("class") not in SOURCE_CLASSES:
            issues.append(_issue("source_class", "class", f"class must be one of {list(SOURCE_CLASSES)}", where))
        if source.get("access") not in ACCESS:
            issues.append(_issue("source_access", "access", f"access must be one of {list(ACCESS)}", where))
        if source.get("licence") not in LICENCES:
            issues.append(_issue("source_licence", "licence", f"licence must be one of {list(LICENCES)}", where))
        homepage = source.get("homepage")
        if homepage is not None and (not isinstance(homepage, str) or not URL_RE.match(homepage)):
            issues.append(_issue("source_homepage", "homepage", "homepage must be https:// URL or null", where))
        for flag in ("url_checked", "terms_checked"):
            if not isinstance(source.get(flag), bool):
                issues.append(_issue("source_flag", flag, f"{flag} must be a boolean", where))
        terms_hash = source.get("terms_hash")
        if terms_hash is not None and (not isinstance(terms_hash, str) or not HASH_RE.match(terms_hash)):
            issues.append(_issue("source_hash", "terms_hash", "terms_hash must be sha256 hex or null", where))
        if source.get("terms_checked") is True and terms_hash is None:
            issues.append(_issue("terms_hash_missing", "terms_hash",
                                 "terms_checked=true requires the terms hash", where))
        retrieved_at = source.get("retrieved_at")
        if retrieved_at is not None and not _is_date(retrieved_at):
            issues.append(_issue("source_retrieved", "retrieved_at", "retrieved_at must be a date or null", where))
        if source.get("access") == "NOT_AVAILABLE_IN_ENV" and retrieved_at is not None:
            issues.append(_issue("access_retrieved", "retrieved_at",
                                 "NOT_AVAILABLE_IN_ENV cannot have retrieved_at", where))
        if not isinstance(source.get("jurisdiction"), str) or len(source.get("jurisdiction") or "") < 2:
            issues.append(_issue("source_jurisdiction", "jurisdiction", "jurisdiction is required", where))
        if not isinstance(source.get("allowed_use"), str) or len(source.get("allowed_use") or "") < 5:
            issues.append(_issue("source_allowed_use", "allowed_use", "allowed_use is required", where))
        if not isinstance(source.get("notes"), str) or not source.get("notes"):
            issues.append(_issue("source_notes", "notes", "notes are required", where))
    return by_id, issues


def validate_entry(entry: dict, sources: dict[str, dict], accounts: set[str],
                   position: int | None = None) -> list[dict]:
    """Validate one entry against the schema and the semantic rules."""
    issues: list[dict] = []
    where = entry.get("__where") or (f"entry#{position}" if position is not None else "entry")

    for field in REQUIRED_ENTRY_FIELDS:
        if field not in entry:
            issues.append(_issue("required", field, f"missing {field}", where))
    extra = sorted(set(entry) - set(ENTRY_FIELDS) - {"__where"})
    if extra:
        issues.append(_issue("unknown_field", "entry", f"unknown fields: {extra}", where))

    if entry.get("schema") != ENTRY_SCHEMA:
        issues.append(_issue("entry_schema", "schema", f"schema must be {ENTRY_SCHEMA!r}", where))
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not ID_RE.match(entry_id):
        issues.append(_issue("entry_id", "id", "id must match ^KB-[0-9]{4}$", where))
    if entry.get("kind") not in KINDS:
        issues.append(_issue("entry_kind", "kind", f"kind must be one of {list(KINDS)}", where))
    title = entry.get("title")
    if not isinstance(title, str) or not 3 <= len(title) <= 160:
        issues.append(_issue("entry_title", "title", "title must be 3..160 chars", where))
    summary = entry.get("summary")
    if not isinstance(summary, str) or not 10 <= len(summary) <= 2000:
        issues.append(_issue("entry_summary", "summary", "summary must be 10..2000 chars", where))

    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags:
        issues.append(_issue("entry_tags", "tags", "tags must be a non-empty list", where))
    else:
        for tag in tags:
            if not isinstance(tag, str) or not TAG_RE.match(tag):
                issues.append(_issue("tag_format", "tags",
                                     f"tag must match ^[a-z0-9][a-z0-9_-]+$: {tag!r}", where))
        if len(set(tags)) != len(tags):
            issues.append(_issue("tag_duplicate", "tags", "tags must be unique", where))

    status = entry.get("evidence_status")
    if status not in STATUSES:
        issues.append(_issue("entry_status", "evidence_status",
                             f"status must be one of {list(STATUSES)}", where))
    grade = entry.get("evidence_grade")
    if grade is not None and grade not in GRADES:
        issues.append(_issue("entry_grade", "evidence_grade", "grade must be A, B, C or null", where))

    cited = entry.get("source_ids")
    if not isinstance(cited, list):
        issues.append(_issue("entry_sources", "source_ids", "source_ids must be a list", where))
        cited = []
    for source_id in cited:
        if not isinstance(source_id, str) or not SOURCE_ID_RE.match(source_id):
            issues.append(_issue("source_id_format", "source_ids",
                                 f"source id must match ^SRC-...: {source_id!r}", where))
        elif source_id not in sources:
            issues.append(_issue("source_unknown", "source_ids",
                                 f"source {source_id} is not registered in sources.json", where))
    if len(set(cited)) != len(cited):
        issues.append(_issue("source_duplicate", "source_ids", "source_ids must be unique", where))

    licence = entry.get("licence")
    if licence not in LICENCES:
        issues.append(_issue("entry_licence", "licence", f"licence must be one of {list(LICENCES)}", where))

    for field in ("as_of", "created", "updated", "review_due"):
        if not _is_date(entry.get(field)):
            issues.append(_issue("date_format", field, "must be a real ISO date YYYY-MM-DD", where))
    retrieved_at = entry.get("retrieved_at")
    if retrieved_at is not None and not _is_date(retrieved_at):
        issues.append(_issue("date_format", "retrieved_at", "must be a date or null", where))

    ttl = entry.get("ttl_days")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 3650:
        issues.append(_issue("ttl", "ttl_days", "ttl_days must be an integer 1..3650", where))
        ttl = None
    if ttl is not None and _is_date(entry.get("as_of")) and _is_date(entry.get("review_due")):
        expected = (_parse_date(entry["as_of"]) + timedelta(days=ttl)).isoformat()
        if entry["review_due"] != expected:
            issues.append(_issue("review_due_mismatch", "review_due",
                                 f"review_due must be {expected} (as_of + ttl_days)", where))
    if _is_date(entry.get("created")) and _is_date(entry.get("updated")):
        if entry["updated"] < entry["created"]:
            issues.append(_issue("updated_before_created", "updated",
                                 "updated cannot be earlier than created", where))

    links = entry.get("links")
    if not isinstance(links, list) or not links:
        issues.append(_issue("entry_links", "links", "links must be a non-empty list", where))
    else:
        for link in links:
            if not isinstance(link, str) or not link.strip():
                issues.append(_issue("link_format", "links", "link must be a non-empty string", where))
                continue
            if link.startswith("/") or ".." in Path(link).parts:
                issues.append(_issue("link_escape", "links",
                                     f"link must be a repo-relative path: {link!r}", where))
                continue
            if not (ROOT / link).exists():
                issues.append(_issue("link_missing", "links",
                                     f"link target does not exist: {link}", where))

    if not isinstance(entry.get("author_role"), str) or len(entry.get("author_role") or "") < 2:
        issues.append(_issue("author_role", "author_role", "author_role is required", where))
    notes = entry.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        issues.append(_issue("entry_notes", "notes", "notes are required", where))
        notes = ""
    if status == "NOT_FETCHED" and len(notes.strip()) < 20:
        issues.append(_issue("not_fetched_reason", "notes",
                             "NOT_FETCHED must state the reason and the blocker", where))

    account_id = entry.get("account_id")
    kind = entry.get("kind")
    if kind == "target_account":
        if not isinstance(account_id, str) or not account_id:
            issues.append(_issue("account_required", "account_id",
                                 "target_account entries require account_id", where))
        elif accounts and account_id not in accounts:
            issues.append(_issue("account_unknown", "account_id",
                                 f"account {account_id!r} is not in marketing/target_accounts.json",
                                 where))
    elif account_id not in (None, ""):
        issues.append(_issue("account_not_allowed", "account_id",
                             "account_id is only allowed for kind=target_account", where))
    if not accounts and kind == "target_account":
        issues.append(_issue("accounts_unavailable", "account_id",
                             "accounts file unavailable: cannot verify account_id", where))

    if "archived" in entry and not isinstance(entry["archived"], bool):
        issues.append(_issue("archived_flag", "archived", "archived must be a boolean", where))
    supersedes = entry.get("supersedes")
    if supersedes is not None and (not isinstance(supersedes, str) or not ID_RE.match(supersedes)):
        issues.append(_issue("supersedes_format", "supersedes",
                             "supersedes must be KB-NNNN or null", where))

    # --- evidence semantics -------------------------------------------------------------
    if grade == "A" and status != "VERIFIED":
        issues.append(_issue("grade_status", "evidence_grade",
                             "grade A is only allowed with evidence_status VERIFIED", where))
    if status in ("HYPOTHESIS", "NOT_FETCHED") and grade not in (None, "C"):
        issues.append(_issue("grade_cap", "evidence_grade",
                             f"{status} allows only grade C or null", where))
    if status == "VERIFIED":
        if grade != "A":
            issues.append(_issue("verified_grade", "evidence_grade",
                                 "VERIFIED requires grade A", where))
        if not cited:
            issues.append(_issue("verified_sources", "source_ids",
                                 "VERIFIED requires at least one registered source", where))
        if not _is_date(retrieved_at):
            issues.append(_issue("verified_retrieved", "retrieved_at",
                                 "VERIFIED requires retrieved_at", where))
        if licence == "NONE":
            issues.append(_issue("verified_licence", "licence",
                                 "VERIFIED cannot carry licence NONE", where))
        for source_id in cited:
            source = sources.get(source_id)
            if not source:
                continue
            if source.get("terms_checked") is not True:
                issues.append(_issue("verified_terms", "source_ids",
                                     f"{source_id}: terms must be checked before VERIFIED", where))
            if source.get("access") == "NOT_AVAILABLE_IN_ENV":
                issues.append(_issue("verified_access", "source_ids",
                                     f"{source_id}: source is not available in this environment", where))
    if licence == "NONE" and status != "NOT_FETCHED":
        issues.append(_issue("licence_none", "licence",
                             "licence NONE is only allowed with NOT_FETCHED", where))
    if licence in OPEN_LICENCES:
        restricted = [s for s in cited if (sources.get(s) or {}).get("licence") in RESTRICTED_LICENCES]
        if restricted:
            issues.append(_issue("licence_incompatible", "licence",
                                 f"open licence on entry, restricted sources: {restricted}", where))

    text = " ".join(str(entry.get(key) or "") for key in ("title", "summary", "notes"))
    if status in ("HYPOTHESIS", "NOT_FETCHED") and FIGURE_RE.search(text):
        issues.append(_issue("figure_without_source", "summary",
                             "figures with currency/percent/scale are not allowed without a source",
                             where))
    if ADMISSION_RE.search(text):
        issues.append(_issue("admission_claim", "summary",
                             "admission wording is forbidden: the network is RESEARCH_ONLY", where))
    if FORBIDDEN_TEXT_RE.search(text):
        issues.append(_issue("contact_like", "summary",
                             "e-mail/phone-like value is not allowed in the knowledge base", where))
    return issues


def _entry_counts(entries: list[dict]) -> dict:
    by_kind = {kind: 0 for kind in KINDS}
    by_status = {status: 0 for status in STATUSES}
    by_grade = {grade: 0 for grade in ("A", "B", "C", "NONE")}
    by_licence = {licence: 0 for licence in LICENCES}
    for entry in entries:
        kind = entry.get("kind")
        if kind in by_kind:
            by_kind[kind] += 1
        status = entry.get("evidence_status")
        if status in by_status:
            by_status[status] += 1
        grade = entry.get("evidence_grade")
        by_grade[grade if grade in GRADES else "NONE"] += 1
        licence = entry.get("licence")
        if licence in by_licence:
            by_licence[licence] += 1
    return {"by_kind": by_kind, "by_status": by_status, "by_grade": by_grade,
            "by_licence": by_licence}


def aggregate(entries: list[dict]) -> dict:
    """Deterministic aggregate used by validate/index/export."""
    counts = _entry_counts(entries)
    return {"total": len(entries), **counts}


def validate_all(kb: Path = DEFAULT_KB, accounts_path: Path = DEFAULT_ACCOUNTS) -> dict:
    """Validate the whole knowledge base and return the machine summary."""
    kb = Path(kb)
    schema = load_schema(kb)
    sources_document = load_sources(kb)
    sources, source_issues = validate_sources(sources_document)
    entries, entry_issues = load_entries(kb)
    inbox, inbox_issues = load_inbox(kb)
    accounts = load_accounts(accounts_path)

    issues: list[dict] = list(source_issues) + list(entry_issues) + list(inbox_issues)
    seen: set[str] = set()
    titles: dict[tuple, str] = {}
    previous: dict[str, str] = {}
    for position, entry in enumerate(entries + inbox, start=1):
        issues.extend(validate_entry(entry, sources, accounts, position))
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            if entry_id in seen:
                issues.append(_issue("duplicate_id", "id", f"duplicate id {entry_id}", entry.get("__where")))
            seen.add(entry_id)
            where_file = str(entry.get("__where") or "").split(":")[0]
            last = previous.get(where_file)
            if last and entry_id < last:
                issues.append(_issue("id_order", "id",
                                     f"ids must ascend within a file: {entry_id} after {last}",
                                     entry.get("__where")))
            previous[where_file] = entry_id
        key = (entry.get("kind"), entry.get("title"))
        if key in titles:
            issues.append(_issue("duplicate_title", "title",
                                 f"duplicate (kind,title) with {titles[key]}", entry.get("__where")))
        else:
            titles[key] = str(entry_id)
    issues.sort(key=lambda item: (str(item.get("where") or ""), item["code"], item["field"]))

    aggregate_summary = aggregate(entries)
    return {
        "schema": VALIDATE_SCHEMA,
        "kb": str(kb),
        "schema_version": schema.get("version"),
        "valid": not issues,
        "errors": len(issues),
        "entries": len(entries),
        "inbox_entries": len(inbox),
        "sources": len(sources),
        "counts": aggregate_summary,
        "inbox_counts": aggregate(inbox),
        "accounts_verified": bool(accounts),
        "issues": issues,
        "policy": {
            "network": "disabled",
            "clock": "not read (pass --today explicitly)",
            "model_status": "RESEARCH_ONLY",
            "admission": False,
        },
    }


def check(kb: Path = DEFAULT_KB, accounts_path: Path = DEFAULT_ACCOUNTS) -> dict:
    """Validate and return the summary (alias for :func:`validate_all`)."""
    return validate_all(kb, accounts_path)


def freshness(review_due: str, today: date | None) -> str:
    """Freshness class of an entry relative to ``today`` (UNKNOWN without a date)."""
    if today is None or not _is_date(review_due):
        return "UNKNOWN"
    due = _parse_date(review_due)
    if due < today:
        return "stale"
    if due <= today + timedelta(days=30):
        return "due_soon"
    return "fresh"


def search_entries(entries: list[dict], query: str = "", kind: str | None = None,
                   tag: str | None = None, status: str | None = None,
                   licence: str | None = None, today: date | None = None,
                   only_stale: bool = False, limit: int | None = None) -> list[dict]:
    """Token search with filters; deterministic order by id."""
    tokens = [token for token in re.split(r"\s+", (query or "").strip().lower()) if token]
    results = []
    for entry in entries:
        haystack = " ".join([
            str(entry.get("id") or ""), str(entry.get("kind") or ""),
            str(entry.get("title") or ""), str(entry.get("summary") or ""),
            " ".join(entry.get("tags") or []), str(entry.get("author_role") or ""),
            str(entry.get("notes") or ""),
        ]).lower()
        if any(token not in haystack for token in tokens):
            continue
        if kind and entry.get("kind") != kind:
            continue
        if tag and tag not in (entry.get("tags") or []):
            continue
        if status and entry.get("evidence_status") != status:
            continue
        if licence and entry.get("licence") != licence:
            continue
        if only_stale and freshness(str(entry.get("review_due")), today) != "stale":
            continue
        results.append(entry)
    results.sort(key=lambda item: str(item.get("id") or ""))
    if limit is not None:
        results = results[:limit]
    return results


def _next_id(entries: list[dict]) -> str:
    numbers = [int(str(entry.get("id"))[3:]) for entry in entries
               if isinstance(entry.get("id"), str) and ID_RE.match(str(entry.get("id")))]
    return f"KB-{(max(numbers) + 1) if numbers else 1:04d}"


def build_entry(kind: str, title: str, summary: str, author_role: str, as_of: str,
                evidence: str = "HYPOTHESIS", grade: str | None = "C",
                sources: list[str] | None = None, licence: str = "INTERNAL",
                tags: list[str] | None = None, links: list[str] | None = None,
                notes: str = "", ttl_days: int | None = None,
                entry_id: str | None = None, account_id: str | None = None,
                supersedes: str | None = None, retrieved_at: str | None = None) -> dict:
    """Build an entry dict; ``review_due`` is derived from ``as_of + ttl_days``."""
    ttl = ttl_days if ttl_days is not None else DEFAULT_TTL.get(kind, 180)
    review_due = (_parse_date(as_of) + timedelta(days=ttl)).isoformat() if _is_date(as_of) else None
    return {
        "id": entry_id or "",
        "schema": ENTRY_SCHEMA,
        "kind": kind,
        "title": title,
        "summary": summary,
        "tags": tags or [],
        "evidence_status": evidence,
        "evidence_grade": grade,
        "source_ids": sources or [],
        "licence": licence,
        "as_of": as_of,
        "ttl_days": ttl,
        "review_due": review_due,
        "links": links or [],
        "created": as_of,
        "updated": as_of,
        "author_role": author_role,
        "notes": notes or "Запись создана через tools/kb.py add.",
        "retrieved_at": retrieved_at,
        "account_id": account_id,
        "supersedes": supersedes,
        "archived": False,
    }


def render_entry_line(entry: dict) -> str:
    """One canonical JSONL line (no ``__where`` helper key)."""
    clean = {key: value for key, value in entry.items() if key != "__where"}
    return json.dumps(clean, ensure_ascii=False)


def add_entry(kb: Path, entry: dict, out: Path | None = None,
              accounts_path: Path = DEFAULT_ACCOUNTS) -> tuple[Path, dict]:
    """Validate and append one entry to ``out`` (default ``inbox/<id>.jsonl``)."""
    kb = Path(kb)
    sources, source_issues = validate_sources(load_sources(kb))
    if source_issues:
        raise KbError(f"sources.json is invalid ({len(source_issues)} issue(s)); fix it first")
    entries, _ = load_entries(kb)
    inbox, _ = load_inbox(kb)
    existing = [item for item in entries + inbox]
    if not entry.get("id"):
        entry["id"] = _next_id(existing)
    if any(item.get("id") == entry["id"] for item in existing):
        raise KbError(f"entry id already exists: {entry['id']}")
    issues = validate_entry(entry, sources, load_accounts(accounts_path))
    if issues:
        raise KbError("entry is invalid:\n" + "\n".join(
            f"  {issue['code']}: {issue['field']}: {issue['message']}" for issue in issues))
    target = Path(out) if out else kb / "inbox" / f"{entry['id']}.jsonl"
    if target.exists():
        raise KbError(f"file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_entry_line(entry) + "\n", encoding="utf-8", newline="\n")
    return target, entry


def merge_inbox(kb: Path = DEFAULT_KB, to: Path | None = None,
                accounts_path: Path = DEFAULT_ACCOUNTS) -> dict:
    """Validate every inbox record, append the new ones, then archive the inbox files.

    Processed files are moved to ``inbox/merged/`` (never deleted), so the queue is empty after a
    successful merge and ``--check`` stays green without re-seeing the same ids.
    """
    kb = Path(kb)
    inbox_dir = kb / "inbox"
    inbox_files = sorted(inbox_dir.glob("*.jsonl")) if inbox_dir.is_dir() else []
    sources, source_issues = validate_sources(load_sources(kb))
    entries, _ = load_entries(kb)
    inbox, parse_issues = load_inbox(kb)
    accounts = load_accounts(accounts_path)
    issues: list[dict] = list(source_issues) + list(parse_issues)
    for entry in inbox:
        issues.extend(validate_entry(entry, sources, accounts))
    existing_ids = {entry.get("id") for entry in entries}
    numbers = [int(str(item.get("id"))[3:]) for item in entries
               if isinstance(item.get("id"), str) and ID_RE.match(str(item.get("id")))]
    highest = max(numbers) if numbers else 0
    new: list[dict] = []
    duplicates: list[str] = []
    for entry in inbox:
        entry_id = entry.get("id")
        if entry_id in existing_ids:
            duplicates.append(str(entry_id))
            continue
        if isinstance(entry_id, str) and ID_RE.match(entry_id) and int(entry_id[3:]) <= highest:
            issues.append(_issue("id_order", "id",
                                 f"new id {entry_id} must be greater than the last id in entries",
                                 entry.get("__where")))
            continue
        new.append(entry)
    if issues:
        raise KbError("inbox is not mergeable:\n" + "\n".join(
            f"  {issue['code']}: {issue['field']}: {issue['message']}" for issue in issues))
    target = Path(to) if to else kb / "entries" / "seed.jsonl"
    if new:
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for entry in new:
                handle.write(render_entry_line(entry) + "\n")
    archive = inbox_dir / "merged"
    archived: list[str] = []
    if inbox_files:
        archive.mkdir(parents=True, exist_ok=True)
        for path in inbox_files:
            destination = archive / path.name
            counter = 1
            while destination.exists():
                destination = archive / f"{path.stem}.{counter}{path.suffix}"
                counter += 1
            path.replace(destination)
            archived.append(str(destination.relative_to(kb)).replace("\\", "/"))
    return {"schema": "amfe.kb.merge.v1", "kb": str(kb), "target": str(target),
            "inbox": len(inbox), "merged": len(new), "duplicates": sorted(duplicates),
            "archived": archived, "ids": [entry.get("id") for entry in new]}


def build_index(kb: Path = DEFAULT_KB, today: date | None = None,
                accounts_path: Path = DEFAULT_ACCOUNTS) -> dict:
    """Build and write ``index.json``/``index.md``; returns the index summary."""
    kb = Path(kb)
    summary = validate_all(kb, accounts_path)
    entries, _ = load_entries(kb)
    sources_document = load_sources(kb)
    sources = sources_document.get("sources") or []
    freshness_counts = {"fresh": 0, "due_soon": 0, "stale": 0, "UNKNOWN": 0}
    rows = []
    for entry in sorted(entries, key=lambda item: str(item.get("id"))):
        state = freshness(str(entry.get("review_due")), today)
        freshness_counts[state] = freshness_counts.get(state, 0) + 1
        rows.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "kind": entry.get("kind"),
            "evidence_status": entry.get("evidence_status"),
            "evidence_grade": entry.get("evidence_grade"),
            "licence": entry.get("licence"),
            "review_due": entry.get("review_due"),
            "freshness": state,
            "author_role": entry.get("author_role"),
        })
    by_class: dict[str, int] = {}
    for source in sources:
        by_class[source.get("class")] = by_class.get(source.get("class"), 0) + 1
    index = {
        "schema": INDEX_SCHEMA,
        "kb": str(kb),
        "generated_for": today.isoformat() if today else None,
        "valid": summary["valid"],
        "errors": summary["errors"],
        "totals": summary["counts"]["total"],
        "counts": summary["counts"],
        "freshness": freshness_counts,
        "entries": rows,
        "sources": {
            "total": len(sources),
            "by_class": dict(sorted(by_class.items())),
            "unchecked_urls": sum(1 for s in sources if s.get("url_checked") is not True),
            "unchecked_terms": sum(1 for s in sources if s.get("terms_checked") is not True),
        },
        "issues": summary["issues"],
    }
    (kb / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    (kb / "index.md").write_text(render_index_md(index), encoding="utf-8", newline="\n")
    return index


def render_index_md(index: dict) -> str:
    """Deterministic Markdown index."""
    statuses = ", ".join(f"{key}={value}" for key, value in index["counts"]["by_status"].items())
    kinds = ", ".join(f"{key}={value}" for key, value in index["counts"]["by_kind"].items())
    lines = [
        "# Индекс базы знаний (market intelligence)",
        "",
        f"Записей: **{index['totals']}**. Свежесть рассчитана на: "
        f"{index['generated_for'] or 'не рассчитана (--today не задан)'}.",
        f"Статусы: {statuses}. Виды: {kinds}.",
        f"Источников: **{index['sources']['total']}** "
        f"(без проверки URL: {index['sources']['unchecked_urls']}, "
        f"без проверки условий: {index['sources']['unchecked_terms']}).",
        "",
        "## Записи",
        "",
        "| ID | Вид | Статус | Grade | Лицензия | Review due | Свежесть | Название |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in index["entries"]:
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['evidence_status']} | "
            f"{row['evidence_grade'] or '-'} | {row['licence']} | {row['review_due']} | "
            f"{row['freshness']} | {row['title']} |")
    lines += ["", "## Свежесть", "",
              "| Класс | Записей |", "|---|---:|"]
    for key, value in index["freshness"].items():
        lines.append(f"| {key} | {value} |")
    if index["issues"]:
        lines += ["", "## Дефекты", "", f"Всего: {index['errors']}", ""]
        for issue in index["issues"]:
            lines.append(f"- `{issue['code']}` ({issue['field']}): {issue['message']}")
    else:
        lines += ["", "Дефектов нет: схема, лицензии, ссылки и свежесть в порядке."]
    return "\n".join(lines) + "\n"


def export(kb: Path = DEFAULT_KB, fmt: str = "json", today: date | None = None,
           accounts_path: Path = DEFAULT_ACCOUNTS) -> str:
    """Export the whole base as deterministic JSON or Markdown."""
    kb = Path(kb)
    entries, _ = load_entries(kb)
    sources_document = load_sources(kb)
    summary = validate_all(kb, accounts_path)
    if fmt == "json":
        payload = {
            "schema": EXPORT_SCHEMA,
            "kb": str(kb),
            "valid": summary["valid"],
            "totals": summary["counts"]["total"],
            "sources": sorted(sources_document.get("sources") or [], key=lambda item: str(item.get("id"))),
            "entries": [{key: value for key, value in entry.items() if key != "__where"}
                        for entry in sorted(entries, key=lambda item: str(item.get("id")))],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lines = ["# Экспорт базы знаний AMFE (market intelligence)", "",
             f"Записей: {summary['counts']['total']}; статус: RESEARCH_ONLY, admission=false.", ""]
    for kind in KINDS:
        subset = [entry for entry in sorted(entries, key=lambda item: str(item.get("id")))
                  if entry.get("kind") == kind]
        if not subset:
            continue
        lines += [f"## {kind} ({len(subset)})", "",
                  "| ID | Статус | Grade | Лицензия | Review due | Название |",
                  "|---|---|---|---|---|---|"]
        for entry in subset:
            lines.append(f"| {entry.get('id')} | {entry.get('evidence_status')} | "
                         f"{entry.get('evidence_grade') or '-'} | {entry.get('licence')} | "
                         f"{entry.get('review_due')} | {entry.get('title')} |")
        lines.append("")
        for entry in subset:
            lines += [f"### {entry.get('id')} — {entry.get('title')}", "",
                      str(entry.get("summary")), "",
                      f"- status: {entry.get('evidence_status')} / grade "
                      f"{entry.get('evidence_grade') or '-'}; licence {entry.get('licence')}",
                      f"- sources: {', '.join(entry.get('source_ids') or []) or 'нет'}",
                      f"- review_due: {entry.get('review_due')}; author: {entry.get('author_role')}",
                      f"- notes: {entry.get('notes')}", ""]
    lines += ["## Источники", "", "| ID | Класс | Доступ | Лицензия | Условия проверены |",
              "|---|---|---|---|---|"]
    for source in sorted(sources_document.get("sources") or [], key=lambda item: str(item.get("id"))):
        lines.append(f"| {source.get('id')} | {source.get('class')} | {source.get('access')} | "
                     f"{source.get('licence')} | {'да' if source.get('terms_checked') else 'нет'} |")
    return "\n".join(lines) + "\n"


def render_validate(summary: dict) -> str:
    lines = [
        f"KB: {summary['entries']} записей, {summary['sources']} источников, "
        f"inbox {summary['inbox_entries']}, ошибок {summary['errors']}, "
        f"valid={str(summary['valid']).lower()}",
        "Статусы: " + ", ".join(f"{key}={value}" for key, value in summary["counts"]["by_status"].items()),
        "Виды: " + ", ".join(f"{key}={value}" for key, value in summary["counts"]["by_kind"].items()),
    ]
    if summary["issues"]:
        lines.append("")
        lines.append("Ошибки:")
        for issue in summary["issues"]:
            where = f"[{issue['where']}] " if issue.get("where") else ""
            lines.append(f"  {where}{issue['code']}: {issue['field']}: {issue['message']}")
    else:
        lines.append("Ошибок нет: схема, evidence, лицензии, ссылки и свежесть в порядке.")
    return "\n".join(lines)


def _split(value: str | None, separator: str = ",") -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(separator) if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AMFE market-intelligence knowledge base tool")
    parser.add_argument("--kb", default=str(DEFAULT_KB), help="knowledge base directory")
    parser.add_argument("--accounts-file", default=str(DEFAULT_ACCOUNTS))
    parser.add_argument("--check", action="store_true", help="validate; exit 2 on any issue")
    parser.add_argument("--json", action="store_true", help="machine output for --check")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="validate the knowledge base")
    validate.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="search entries")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--kind", choices=KINDS)
    search.add_argument("--tag")
    search.add_argument("--evidence", choices=STATUSES)
    search.add_argument("--licence", choices=LICENCES)
    search.add_argument("--today")
    search.add_argument("--stale", action="store_true")
    search.add_argument("--limit", type=int)
    search.add_argument("--json", action="store_true")

    add = sub.add_parser("add", help="build, validate and append one entry")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--title", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--author-role", required=True)
    add.add_argument("--as-of", required=True, help="ISO date, e.g. 2026-11-21")
    add.add_argument("--evidence", default="HYPOTHESIS", choices=STATUSES)
    add.add_argument("--grade", default="C", choices=("A", "B", "C", "none"))
    add.add_argument("--source", action="append", default=[], help="source id (repeatable)")
    add.add_argument("--licence", default="INTERNAL", choices=LICENCES)
    add.add_argument("--tags", default="", help="comma-separated tags")
    add.add_argument("--link", action="append", default=[], help="repo-relative link (repeatable)")
    add.add_argument("--notes", default="")
    add.add_argument("--ttl-days", type=int)
    add.add_argument("--id", dest="entry_id")
    add.add_argument("--account")
    add.add_argument("--supersedes")
    add.add_argument("--retrieved-at")
    add.add_argument("--out")

    merge = sub.add_parser("merge", help="merge validated inbox records into entries")
    merge.add_argument("--to")

    index = sub.add_parser("index", help="rebuild index.json and index.md")
    index.add_argument("--today")

    export_cmd = sub.add_parser("export", help="export the base as JSON or Markdown")
    export_cmd.add_argument("--format", choices=("json", "md"), default="json")
    export_cmd.add_argument("--today")
    export_cmd.add_argument("--out")

    args = parser.parse_args(argv)
    kb = Path(args.kb)
    accounts_path = Path(args.accounts_file)

    def _today(value: str | None) -> date | None:
        if not value:
            return None
        if not _is_date(value):
            raise KbError(f"--today must be an ISO date YYYY-MM-DD: {value!r}")
        return _parse_date(value)

    try:
        if args.command == "add":
            grade = None if args.grade == "none" else args.grade
            entry = build_entry(
                kind=args.kind, title=args.title, summary=args.summary,
                author_role=args.author_role, as_of=args.as_of, evidence=args.evidence,
                grade=grade, sources=_split(",".join(args.source)), licence=args.licence,
                tags=_split(args.tags), links=list(args.link), notes=args.notes,
                ttl_days=args.ttl_days, entry_id=args.entry_id, account_id=args.account,
                supersedes=args.supersedes, retrieved_at=args.retrieved_at)
            target, written = add_entry(kb, entry, Path(args.out) if args.out else None, accounts_path)
            print(f"added: {written['id']} -> {target}")
            return 0
        if args.command == "merge":
            result = merge_inbox(kb, Path(args.to) if args.to else None, accounts_path)
            print(f"merged {result['merged']} of {result['inbox']} inbox records into "
                  f"{result['target']}; duplicates: {len(result['duplicates'])}; "
                  f"archived: {len(result['archived'])}")
            if result["ids"]:
                print("new ids: " + ", ".join(str(item) for item in result["ids"]))
            return 0
        if args.command == "index":
            today = _today(args.today)
            result = build_index(kb, today, accounts_path)
            print(f"index: {result['totals']} entries, fresh={result['freshness']['fresh']}, "
                  f"due_soon={result['freshness']['due_soon']}, "
                  f"stale={result['freshness']['stale']}, "
                  f"unknown={result['freshness']['UNKNOWN']} -> {kb / 'index.json'}")
            return 0 if result["valid"] else 2
        if args.command == "export":
            text = export(kb, args.format, _today(args.today), accounts_path)
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise KbError(f"output directory does not exist: {out.parent}")
                out.write_text(text, encoding="utf-8", newline="\n")
                print(f"export written: {out}")
            else:
                print(text, end="")
            return 0
        if args.command == "search":
            entries, parse_issues = load_entries(kb)
            if parse_issues:
                raise KbError("cannot search: entries contain invalid JSON lines")
            today = _today(args.today)
            if args.stale and today is None:
                raise KbError("--stale requires --today (no clock is read)")
            results = search_entries(entries, args.query, args.kind, args.tag, args.evidence,
                                     args.licence, today, args.stale, args.limit)
            if args.json:
                print(json.dumps({"schema": "amfe.kb.search.v1", "query": args.query,
                                  "count": len(results),
                                  "results": [{key: value for key, value in entry.items()
                                               if key != "__where"} for entry in results]},
                                 ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"найдено: {len(results)}")
                for entry in results:
                    print(f"  {entry.get('id')}  [{entry.get('evidence_status')}/"
                          f"{entry.get('evidence_grade') or '-'}]  {entry.get('kind'):<14} "
                          f"{entry.get('title')}")
            return 0
        # default / validate / --check
        summary = validate_all(kb, accounts_path)
        if args.json or (args.command == "validate" and getattr(args, "json", False)):
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.check or args.command == "validate":
            if summary["valid"]:
                print(f"OK: {summary['entries']} entries, {summary['sources']} sources, "
                      f"inbox {summary['inbox_entries']}")
            else:
                print(f"FAIL: {summary['errors']} issue(s)", file=sys.stderr)
                for issue in summary["issues"]:
                    where = f"[{issue['where']}] " if issue.get("where") else ""
                    print(f"  {where}{issue['code']}: {issue['field']}: {issue['message']}",
                          file=sys.stderr)
        else:
            print(render_validate(summary))
        return 0 if summary["valid"] else 2
    except KbError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
