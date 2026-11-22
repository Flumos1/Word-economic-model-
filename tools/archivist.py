#!/usr/bin/env python3
"""Cross-project knowledge archive for AMFE (role ``archivist``).

The archive is deliberately separate from ``knowledge_base/`` (market-intelligence facts).
It stores what every project may need: methods, source cards, legal *frameworks* (pointers,
never legal opinions), fact packs and templates. Every record carries provenance, a project
namespace, a licence, an access level, evidence status and an explicit review date.

The tool is deterministic (no network, no clock: pass ``--today`` / ``--as-of`` /
``--collected-at`` explicitly), fail-closed (any defect is exit 2 and nothing is written) and
writes only inside the archive directory. It never invents knowledge: a missing source stays
``NOT_FETCHED`` with the blocker in ``notes``.

Commands::

    python tools/archivist.py --check                      # validate, exit 0 / 2
    python tools/archivist.py validate --json --today 2026-11-22
    python tools/archivist.py search "провенанс" --namespace global
    python tools/archivist.py add --kind method --namespace global --title ... --summary ...
    python tools/archivist.py merge                        # inbox -> entries (validated, no duplicates)
    python tools/archivist.py index --today 2026-11-22     # index.json + index.md
    python tools/archivist.py pack --project osint --topic "границы сбора" --role osint-scout
    python tools/archivist.py export --format json|md|jsonl [--out path]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "knowledge_archive"

ARCHIVE_SCHEMA = "amfe.archive.entry.v1"
SCHEMA_DOCUMENT = "amfe.archive.schema.v1"
INDEX_SCHEMA = "amfe.archive.index.v1"
VALIDATE_SCHEMA = "amfe.archive.validate.v1"
PACK_SCHEMA = "amfe.archive.pack.v1"
EXPORT_SCHEMA = "amfe.archive.export.v1"
MERGE_SCHEMA = "amfe.archive.merge.v1"
SEARCH_SCHEMA = "amfe.archive.search.v1"

KINDS = ("method", "source", "legal", "fact_pack", "template")
STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_FETCHED")
STATUS_RANK = {"NOT_FETCHED": 0, "HYPOTHESIS": 1, "PARTIAL": 2, "VERIFIED": 3}
FRESHNESS_RANK = {"fresh": 0, "due_soon": 1, "UNKNOWN": 2, "stale": 3}
GRADES = ("A", "B", "C")
LICENCES = ("OPEN", "ATTRIBUTION", "PUBLIC_DOMAIN", "SUBSCRIPTION", "LICENCE_REQUIRED",
            "INTERNAL", "NONE")
OPEN_LICENCES = ("OPEN", "PUBLIC_DOMAIN", "ATTRIBUTION")
RESTRICTED_LICENCES = ("SUBSCRIPTION", "LICENCE_REQUIRED", "NONE")
ACCESS_LEVELS = ("PUBLIC", "PROJECT", "RESTRICTED")
ORIGIN_TYPES = ("OFFICIAL_LAW", "OFFICIAL_STATISTICS", "COMPANY_FILING", "OPEN_WEB",
                "INTERNAL_DOC", "BOOK", "DATASET", "EXPERT_NOTE")
VERIFIED_ORIGINS = ("OFFICIAL_LAW", "OFFICIAL_STATISTICS", "COMPANY_FILING",
                    "INTERNAL_DOC", "DATASET")
METHODS = ("MANUAL_READ", "DOCUMENTED_API", "OFFICIAL_PORTAL", "DERIVED", "EXPERT_NOTE")
RETENTIONS = ("TTL", "PROJECT", "PERMANENT", "LEGAL_HOLD")
GLOBAL_NS = "global"

DEFAULT_TTL = {"method": 365, "source": 365, "legal": 365, "fact_pack": 180, "template": 730}
DEFAULT_ACCESS = {"method": "PROJECT", "source": "PROJECT", "legal": "PROJECT",
                  "fact_pack": "PROJECT", "template": "PROJECT"}
DEFAULT_RETENTION = {"method": "PERMANENT", "source": "PERMANENT", "legal": "LEGAL_HOLD",
                     "fact_pack": "TTL", "template": "PERMANENT"}

ENTRY_FIELDS = (
    "id", "schema", "namespace", "kind", "title", "summary", "tags", "audience",
    "evidence_status", "evidence_grade", "licence", "access", "provenance", "source_ids",
    "jurisdiction", "as_of", "ttl_days", "review_due", "owner_role", "retention", "version",
    "created", "updated", "notes", "links", "supersedes", "archived",
)
REQUIRED_ENTRY_FIELDS = (
    "id", "schema", "namespace", "kind", "title", "summary", "tags", "audience",
    "evidence_status", "evidence_grade", "licence", "access", "provenance", "source_ids",
    "as_of", "ttl_days", "review_due", "owner_role", "retention", "version",
    "created", "updated", "notes", "links",
)
PROVENANCE_FIELDS = ("origin", "origin_type", "collected_by", "collected_at", "method",
                     "retrieved_at", "sha256")
REQUIRED_PROVENANCE_FIELDS = ("origin", "origin_type", "collected_by", "collected_at", "method")

ID_RE = re.compile(r"^AR-\d{4}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{2,30}$")
NS_RE = re.compile(r"^[a-z][a-z0-9_-]{2,23}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https://[^\s]+$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
JURISDICTION_RE = re.compile(r"^[A-Z]{2,3}(?:-[A-Z]{2})?$")
FIGURE_RE = re.compile(
    r"(?:\d[\d\s.,]*\s*(?:%|₽|\$|€|млн|млрд|тыс))|(?:(?:₽|\$|€)\s*\d[\d\s.,]*)",
    re.IGNORECASE,
)
ADMISSION_RE = re.compile(r"\b(admitted|допущенн\w*|допуск\s+модели)\b", re.IGNORECASE)
FORBIDDEN_TEXT_RE = re.compile(
    r"(?:\+\d[\d\s()-]{7,}\d)"
    r"|(?:[\w.+-]+@[\w-]+\.[\w.]+)"
    r"|(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r"|(?:\bsk-[A-Za-z0-9]{16,})"
    r"|(?:\bAKIA[0-9A-Z]{16}\b)"
    r"|(?:\bghp_[A-Za-z0-9]{20,})"
    r"|(?i:\b(?:api[_-]?key|secret|passwd|password|token|bearer)\s*[:=]\s*\S+)"
    r"|(?:\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b)",
)

LICENCE_NOTES = {
    "OPEN": "открытая лицензия: можно использовать, сохраняя ссылку на запись",
    "ATTRIBUTION": "требуется атрибуция: указывать источник и запись архива",
    "PUBLIC_DOMAIN": "общественное достояние: ограничений нет, ссылка желательна",
    "SUBSCRIPTION": "только при действующей подписке; перепродажа и выгрузка запрещены",
    "LICENCE_REQUIRED": "нужен договор/лицензия; до договора использовать нельзя",
    "INTERNAL": "только внутреннее использование AMFE; наружу не передаётся",
    "NONE": "прав нет: запись-указатель, использовать как источник нельзя",
}


class ArchiveError(ValueError):
    """The archive cannot be used at all (fail-closed input)."""


def _issue(code: str, field: str, message: str, where: str | None = None) -> dict:
    return {"code": code, "field": field, "message": message, "where": where}


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _where(base: str, line: int | None) -> str:
    return f"{base}:{line}" if line is not None else base


def read_jsonl(path: Path, base: str | None = None) -> tuple[list[dict], list[dict]]:
    """Read JSONL into ``(records, parse_issues)``; blank lines are ignored."""
    records: list[dict] = []
    issues: list[dict] = []
    label = base or path.name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return records, [_issue("read", "file", f"cannot read: {error}", label)]
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as error:
            issues.append(_issue("json", "line", f"invalid JSON: {error}", _where(label, number)))
            continue
        if not isinstance(data, dict):
            issues.append(_issue("json", "line", "record must be a JSON object",
                                 _where(label, number)))
            continue
        data["__where"] = _where(label, number)
        records.append(data)
    return records, issues


def load_schema(archive: Path = DEFAULT_ARCHIVE) -> dict:
    """Load ``schema.json``; raises :class:`ArchiveError` when it cannot be used."""
    path = Path(archive) / "schema.json"
    if not path.exists():
        raise ArchiveError(f"file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ArchiveError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ArchiveError(f"{path}: top-level value must be an object")
    return document


def load_namespaces(schema: dict) -> dict[str, dict]:
    namespaces = schema.get("x-namespaces")
    if not isinstance(namespaces, dict):
        return {}
    return {name: value for name, value in namespaces.items() if isinstance(value, dict)}


def load_roles(schema: dict) -> tuple[str, ...]:
    roles = schema.get("x-roles")
    if not isinstance(roles, list):
        return ()
    return tuple(role for role in roles if isinstance(role, str))


def load_entries(archive: Path = DEFAULT_ARCHIVE) -> tuple[list[dict], list[dict]]:
    entries_dir = Path(archive) / "entries"
    records: list[dict] = []
    issues: list[dict] = []
    if not entries_dir.is_dir():
        return records, [_issue("entries_dir", "entries", "entries/ directory is missing")]
    for path in sorted(entries_dir.glob("*.jsonl")):
        part, parse_issues = read_jsonl(path, f"entries/{path.name}")
        records.extend(part)
        issues.extend(parse_issues)
    if not records and not issues:
        issues.append(_issue("entries_empty", "entries", "no entries found in entries/*.jsonl"))
    return records, issues


def load_inbox(archive: Path = DEFAULT_ARCHIVE) -> tuple[list[dict], list[dict]]:
    inbox_dir = Path(archive) / "inbox"
    records: list[dict] = []
    issues: list[dict] = []
    if not inbox_dir.is_dir():
        return records, issues
    for path in sorted(inbox_dir.glob("*.jsonl")):
        part, parse_issues = read_jsonl(path, f"inbox/{path.name}")
        records.extend(part)
        issues.extend(parse_issues)
    return records, issues


def validate_schema(schema: dict) -> tuple[dict[str, dict], tuple[str, ...], list[dict]]:
    """Validate the schema document itself; returns ``(namespaces, roles, issues)``."""
    issues: list[dict] = []
    where = "schema.json"
    if schema.get("$id") != SCHEMA_DOCUMENT:
        issues.append(_issue("schema_id", "$id", f"$id must be {SCHEMA_DOCUMENT!r}", where))
    version = schema.get("version")
    if not isinstance(version, str) or not re.match(r"^\d+\.\d+\.\d+$", version):
        issues.append(_issue("schema_version", "version", "version must look like 1.0.0", where))
    entry = schema.get("entry")
    if not isinstance(entry, dict):
        issues.append(_issue("schema_entry", "entry", "entry schema is missing", where))
    else:
        const = (entry.get("properties") or {}).get("schema", {}).get("const")
        if const != ARCHIVE_SCHEMA:
            issues.append(_issue("schema_const", "entry.properties.schema.const",
                                 f"must be {ARCHIVE_SCHEMA!r}", where))
        required = entry.get("required")
        if not isinstance(required, list) or sorted(required) != sorted(REQUIRED_ENTRY_FIELDS):
            issues.append(_issue("schema_required", "entry.required",
                                 "required fields must match the tool's REQUIRED_ENTRY_FIELDS",
                                 where))
    rules = schema.get("x-rules")
    if not isinstance(rules, list) or len(rules) < 10:
        issues.append(_issue("schema_rules", "x-rules", "x-rules must list at least 10 rules",
                             where))
    namespaces = load_namespaces(schema)
    if not namespaces:
        issues.append(_issue("schema_namespaces", "x-namespaces",
                             "x-namespaces must be a non-empty object", where))
    if GLOBAL_NS not in namespaces:
        issues.append(_issue("schema_global", "x-namespaces",
                             f"namespace {GLOBAL_NS!r} is required", where))
    roles = load_roles(schema)
    if not roles:
        issues.append(_issue("schema_roles", "x-roles", "x-roles must be a non-empty list", where))
    for name, item in namespaces.items():
        if not NS_RE.match(name):
            issues.append(_issue("namespace_name", "x-namespaces", f"bad namespace name {name!r}",
                                 where))
        owner = item.get("owner_role")
        if not isinstance(owner, str) or owner not in roles:
            issues.append(_issue("namespace_owner", "x-namespaces",
                                 f"{name}: owner_role must be one of x-roles", where))
        if not isinstance(item.get("description"), str) or len(item.get("description", "")) < 10:
            issues.append(_issue("namespace_description", "x-namespaces",
                                 f"{name}: description is required", where))
    enums = schema.get("enums")
    if isinstance(enums, dict):
        expected = {"kind": list(KINDS), "evidence_status": list(STATUSES),
                    "licence": list(LICENCES), "access": list(ACCESS_LEVELS),
                    "origin_type": list(ORIGIN_TYPES), "method": list(METHODS),
                    "retention": list(RETENTIONS)}
        for key, value in expected.items():
            if enums.get(key) != value:
                issues.append(_issue("schema_enum", f"enums.{key}",
                                     f"must match the tool enum {value}", where))
    return namespaces, roles, issues


def _check_origin(origin: str, origin_type: str) -> list[dict]:
    issues: list[dict] = []
    if "://" in origin:
        if not URL_RE.match(origin):
            issues.append(_issue("origin_url", "provenance.origin",
                                 "URL origin must use https:// and contain no spaces"))
        if re.match(r"^https?://[^/]*@", origin):
            issues.append(_issue("origin_credentials", "provenance.origin",
                                 "credentials in origin URL are forbidden"))
        if origin_type == "INTERNAL_DOC":
            issues.append(_issue("origin_type", "provenance.origin_type",
                                 "INTERNAL_DOC origin must be a repository path, not a URL"))
    else:
        if origin_type in ("OFFICIAL_LAW", "OPEN_WEB"):
            issues.append(_issue("origin_required_url", "provenance.origin",
                                 f"{origin_type} requires an https:// origin"))
        if origin_type == "INTERNAL_DOC":
            if origin.startswith("/") or ".." in Path(origin).parts:
                issues.append(_issue("origin_escape", "provenance.origin",
                                     "origin must be a repository-relative path"))
            elif not (ROOT / origin).exists():
                issues.append(_issue("origin_missing", "provenance.origin",
                                     f"INTERNAL_DOC origin does not exist: {origin}"))
    return issues


def validate_provenance(entry: dict) -> list[dict]:
    issues: list[dict] = []
    provenance = entry.get("provenance")
    where = entry.get("__where")
    if not isinstance(provenance, dict):
        issues.append(_issue("provenance_type", "provenance", "provenance must be an object", where))
        return issues
    extra = sorted(set(provenance) - set(PROVENANCE_FIELDS))
    if extra:
        issues.append(_issue("unknown_field", "provenance", f"unknown fields: {extra}", where))
    for field in REQUIRED_PROVENANCE_FIELDS:
        if field not in provenance:
            issues.append(_issue("required", f"provenance.{field}", f"missing {field}", where))
    origin = provenance.get("origin")
    origin_type = provenance.get("origin_type")
    if not isinstance(origin, str) or not 5 <= len(origin) <= 300:
        issues.append(_issue("origin", "provenance.origin", "origin must be 5..300 chars", where))
    if origin_type not in ORIGIN_TYPES:
        issues.append(_issue("origin_type", "provenance.origin_type",
                             f"origin_type must be one of {list(ORIGIN_TYPES)}", where))
    if isinstance(origin, str) and origin_type in ORIGIN_TYPES:
        issues.extend(_check_origin(origin.strip(), origin_type))
    collected_by = provenance.get("collected_by")
    if not isinstance(collected_by, str) or not collected_by.strip():
        issues.append(_issue("collected_by", "provenance.collected_by",
                             "collected_by is required", where))
    if not _is_date(provenance.get("collected_at")):
        issues.append(_issue("collected_at", "provenance.collected_at",
                             "collected_at must be a real ISO date", where))
    elif _is_date(entry.get("as_of")) and provenance["collected_at"] > entry["as_of"]:
        issues.append(_issue("collected_after_as_of", "provenance.collected_at",
                             "collected_at cannot be later than as_of", where))
    if provenance.get("method") not in METHODS:
        issues.append(_issue("provenance_method", "provenance.method",
                             f"method must be one of {list(METHODS)}", where))
    retrieved_at = provenance.get("retrieved_at")
    if retrieved_at is not None and not _is_date(retrieved_at):
        issues.append(_issue("retrieved_at", "provenance.retrieved_at",
                             "retrieved_at must be a date or null", where))
    elif isinstance(retrieved_at, str) and _is_date(entry.get("as_of")):
        if retrieved_at > entry["as_of"]:
            issues.append(_issue("retrieved_after_as_of", "provenance.retrieved_at",
                                 "retrieved_at cannot be later than as_of", where))
    sha256 = provenance.get("sha256")
    if sha256 is not None and (not isinstance(sha256, str) or not HASH_RE.match(sha256)):
        issues.append(_issue("sha256", "provenance.sha256",
                             "sha256 must be 64 lowercase hex chars or null", where))
    return issues


def validate_entry(entry: dict, *, namespaces: dict[str, dict], roles: tuple[str, ...],
                   by_id: dict[str, dict], position: int | None = None) -> list[dict]:
    """Validate one archive record against the schema and the semantic rules."""
    issues: list[dict] = []
    where = entry.get("__where") or (f"entry#{position}" if position is not None else "entry")

    for field in REQUIRED_ENTRY_FIELDS:
        if field not in entry:
            issues.append(_issue("required", field, f"missing {field}", where))
    extra = sorted(set(entry) - set(ENTRY_FIELDS) - {"__where"})
    if extra:
        issues.append(_issue("unknown_field", "entry", f"unknown fields: {extra}", where))

    if entry.get("schema") != ARCHIVE_SCHEMA:
        issues.append(_issue("entry_schema", "schema", f"schema must be {ARCHIVE_SCHEMA!r}",
                             where))
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not ID_RE.match(entry_id):
        issues.append(_issue("entry_id", "id", "id must match ^AR-[0-9]{4}$", where))

    namespace = entry.get("namespace")
    if not isinstance(namespace, str) or not NS_RE.match(namespace):
        issues.append(_issue("namespace", "namespace", "namespace must match ^[a-z][a-z0-9_-]+$",
                             where))
    elif namespace not in namespaces:
        issues.append(_issue("namespace_unknown", "namespace",
                             f"namespace {namespace!r} is not registered in schema.json", where))

    kind = entry.get("kind")
    if kind not in KINDS:
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

    audience = entry.get("audience")
    if not isinstance(audience, list) or not audience:
        issues.append(_issue("audience", "audience", "audience must be a non-empty list", where))
    else:
        for role in audience:
            if role != "all" and (not isinstance(role, str) or role not in roles):
                issues.append(_issue("audience_role", "audience",
                                     f"{role!r} is not a registered role (or 'all')", where))
        if len(set(audience)) != len(audience):
            issues.append(_issue("audience_duplicate", "audience", "audience must be unique",
                                 where))

    status = entry.get("evidence_status")
    if status not in STATUSES:
        issues.append(_issue("entry_status", "evidence_status",
                             f"status must be one of {list(STATUSES)}", where))
    grade = entry.get("evidence_grade")
    if grade is not None and grade not in GRADES:
        issues.append(_issue("entry_grade", "evidence_grade", "grade must be A, B, C or null",
                             where))

    licence = entry.get("licence")
    if licence not in LICENCES:
        issues.append(_issue("entry_licence", "licence",
                             f"licence must be one of {list(LICENCES)}", where))
    access = entry.get("access")
    if access not in ACCESS_LEVELS:
        issues.append(_issue("entry_access", "access",
                             f"access must be one of {list(ACCESS_LEVELS)}", where))

    for field in ("as_of", "created", "updated", "review_due"):
        if not _is_date(entry.get(field)):
            issues.append(_issue("date_format", field, "must be a real ISO date YYYY-MM-DD", where))
    if _is_date(entry.get("created")) and _is_date(entry.get("updated")):
        if entry["updated"] < entry["created"]:
            issues.append(_issue("updated_before_created", "updated",
                                 "updated cannot be earlier than created", where))

    ttl = entry.get("ttl_days")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 3650:
        issues.append(_issue("ttl", "ttl_days", "ttl_days must be an integer 1..3650", where))
        ttl = None
    if ttl is not None and _is_date(entry.get("as_of")) and _is_date(entry.get("review_due")):
        expected = (_parse_date(entry["as_of"]) + timedelta(days=ttl)).isoformat()
        if entry["review_due"] != expected:
            issues.append(_issue("review_due_mismatch", "review_due",
                                 f"review_due must be {expected} (as_of + ttl_days)", where))

    owner_role = entry.get("owner_role")
    if not isinstance(owner_role, str) or not ROLE_RE.match(owner_role):
        issues.append(_issue("owner_role", "owner_role", "owner_role is required", where))
    elif owner_role not in roles:
        issues.append(_issue("owner_role_unknown", "owner_role",
                             f"{owner_role!r} is not a registered role", where))

    retention = entry.get("retention")
    if retention not in RETENTIONS:
        issues.append(_issue("retention", "retention", f"retention must be one of {list(RETENTIONS)}",
                             where))
    archived = entry.get("archived", False)
    if not isinstance(archived, bool):
        issues.append(_issue("archived_flag", "archived", "archived must be a boolean", where))
    if retention == "LEGAL_HOLD" and archived is True:
        issues.append(_issue("legal_hold_archived", "archived",
                             "LEGAL_HOLD records cannot be archived", where))

    version = entry.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        issues.append(_issue("version", "version", "version must be an integer >= 1", where))
        version = None

    notes = entry.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        issues.append(_issue("entry_notes", "notes", "notes are required", where))
        notes = ""
    if status == "NOT_FETCHED" and len(notes.strip()) < 20:
        issues.append(_issue("not_fetched_reason", "notes",
                             "NOT_FETCHED must state the reason and the blocker", where))

    jurisdiction = entry.get("jurisdiction")
    if kind == "legal" and not isinstance(jurisdiction, str):
        issues.append(_issue("jurisdiction_required", "jurisdiction",
                             "kind=legal requires jurisdiction", where))
    if jurisdiction is not None and (not isinstance(jurisdiction, str)
                                     or not JURISDICTION_RE.match(jurisdiction)):
        issues.append(_issue("jurisdiction_format", "jurisdiction",
                             "jurisdiction must look like EU, UA, US, INTL", where))
    if kind != "legal" and jurisdiction is not None:
        issues.append(_issue("jurisdiction_not_allowed", "jurisdiction",
                             "jurisdiction is only allowed for kind=legal", where))

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
                issues.append(_issue("link_missing", "links", f"link target does not exist: {link}",
                                     where))

    issues.extend(validate_provenance(entry))

    # --- evidence semantics -------------------------------------------------------------
    provenance = entry.get("provenance") if isinstance(entry.get("provenance"), dict) else {}
    retrieved_at = provenance.get("retrieved_at")
    origin_type = provenance.get("origin_type")
    if grade == "A" and status != "VERIFIED":
        issues.append(_issue("grade_status", "evidence_grade",
                             "grade A is only allowed with evidence_status VERIFIED", where))
    if status in ("HYPOTHESIS", "NOT_FETCHED") and grade not in (None, "C"):
        issues.append(_issue("grade_cap", "evidence_grade",
                             f"{status} allows only grade C or null", where))
    if status == "VERIFIED":
        if grade != "A":
            issues.append(_issue("verified_grade", "evidence_grade", "VERIFIED requires grade A",
                                 where))
        if not _is_date(retrieved_at):
            issues.append(_issue("verified_retrieved", "provenance.retrieved_at",
                                 "VERIFIED requires retrieved_at", where))
        if origin_type not in VERIFIED_ORIGINS:
            issues.append(_issue("verified_origin", "provenance.origin_type",
                                 f"VERIFIED requires a primary origin {list(VERIFIED_ORIGINS)}",
                                 where))
        if licence == "NONE":
            issues.append(_issue("verified_licence", "licence",
                                 "VERIFIED cannot carry licence NONE", where))
    if status == "NOT_FETCHED":
        if licence != "NONE":
            issues.append(_issue("not_fetched_licence", "licence",
                                 "NOT_FETCHED requires licence NONE", where))
        if retrieved_at is not None:
            issues.append(_issue("not_fetched_retrieved", "provenance.retrieved_at",
                                 "NOT_FETCHED cannot have retrieved_at", where))
    if licence == "NONE" and status != "NOT_FETCHED":
        issues.append(_issue("licence_none", "licence",
                             "licence NONE is only allowed with NOT_FETCHED", where))

    # --- access / licence compatibility -------------------------------------------------
    if access == "RESTRICTED" and licence in OPEN_LICENCES:
        issues.append(_issue("restricted_open_licence", "access",
                             "access RESTRICTED is incompatible with an open licence", where))
    if access == "PUBLIC" and licence in RESTRICTED_LICENCES:
        issues.append(_issue("public_restricted_licence", "access",
                             f"access PUBLIC is incompatible with licence {licence}", where))

    # --- sources ------------------------------------------------------------------------
    cited = entry.get("source_ids")
    if not isinstance(cited, list):
        issues.append(_issue("entry_sources", "source_ids", "source_ids must be a list", where))
        cited = []
    for source_id in cited:
        if not isinstance(source_id, str) or not ID_RE.match(source_id):
            issues.append(_issue("source_id_format", "source_ids",
                                 f"source id must match ^AR-[0-9]{{4}}$: {source_id!r}", where))
            continue
        if entry_id and source_id == entry_id:
            issues.append(_issue("source_self", "source_ids",
                                 "a record cannot cite itself", where))
            continue
        target = by_id.get(source_id)
        if target is None:
            issues.append(_issue("source_unknown", "source_ids",
                                 f"source {source_id} is not in the archive", where))
        elif target.get("kind") != "source":
            issues.append(_issue("source_kind", "source_ids",
                                 f"{source_id} has kind={target.get('kind')}, expected source",
                                 where))
        elif target.get("archived") is True:
            issues.append(_issue("source_archived", "source_ids",
                                 f"{source_id} is archived and cannot be cited live", where))
    if len(set(cited)) != len(cited):
        issues.append(_issue("source_duplicate", "source_ids", "source_ids must be unique", where))
    if kind in ("fact_pack", "legal") and status != "NOT_FETCHED" and not cited:
        issues.append(_issue("sources_required", "source_ids",
                             f"kind={kind} must cite at least one source", where))

    # --- supersession -------------------------------------------------------------------
    supersedes = entry.get("supersedes")
    if supersedes is not None:
        if not isinstance(supersedes, str) or not ID_RE.match(supersedes):
            issues.append(_issue("supersedes_format", "supersedes",
                                 "supersedes must be AR-NNNN or null", where))
        elif supersedes == entry_id:
            issues.append(_issue("supersedes_self", "supersedes",
                                 "a record cannot supersede itself", where))
        else:
            target = by_id.get(supersedes)
            if target is None:
                issues.append(_issue("supersedes_unknown", "supersedes",
                                     f"{supersedes} is not in the archive", where))
            else:
                if target.get("kind") != kind or target.get("namespace") != namespace:
                    issues.append(_issue("supersedes_scope", "supersedes",
                                         "supersedes target must share kind and namespace", where))
                if target.get("archived") is True:
                    issues.append(_issue("supersedes_archived", "supersedes",
                                         "cannot supersede an archived record", where))
                successors = [item.get("id") for item in by_id.values()
                              if item.get("supersedes") == supersedes]
                if len(successors) > 1:
                    issues.append(_issue("supersedes_fork", "supersedes",
                                         f"{supersedes} already has a successor: {successors}",
                                         where))
                if isinstance(version, int) and isinstance(target.get("version"), int):
                    if version <= target["version"]:
                        issues.append(_issue("version_order", "version",
                                             "version must be greater than the superseded one",
                                             where))

    # --- text safety --------------------------------------------------------------------
    text = " ".join(str(entry.get(key) or "") for key in ("title", "summary", "notes"))
    if status in ("HYPOTHESIS", "NOT_FETCHED") and FIGURE_RE.search(text):
        issues.append(_issue("figure_without_source", "summary",
                             "figures with currency/percent/scale are not allowed without a source",
                             where))
    if ADMISSION_RE.search(text):
        issues.append(_issue("admission_claim", "summary",
                             "admission wording is forbidden: the archive is RESEARCH_ONLY", where))
    probe = text + " " + str(provenance.get("origin") or "")
    if FORBIDDEN_TEXT_RE.search(probe):
        issues.append(_issue("secret_or_contact", "summary",
                             "e-mail/phone/key/payment-like value is forbidden in the archive", where))
    return issues


def _entry_counts(entries: list[dict]) -> dict:
    by_kind = {kind: 0 for kind in KINDS}
    by_status = {status: 0 for status in STATUSES}
    by_grade = {grade: 0 for grade in ("A", "B", "C", "NONE")}
    by_licence = {licence: 0 for licence in LICENCES}
    by_access = {access: 0 for access in ACCESS_LEVELS}
    by_retention = {retention: 0 for retention in RETENTIONS}
    for entry in entries:
        if entry.get("kind") in by_kind:
            by_kind[entry["kind"]] += 1
        if entry.get("evidence_status") in by_status:
            by_status[entry["evidence_status"]] += 1
        grade = entry.get("evidence_grade")
        by_grade[grade if grade in GRADES else "NONE"] += 1
        if entry.get("licence") in by_licence:
            by_licence[entry["licence"]] += 1
        if entry.get("access") in by_access:
            by_access[entry["access"]] += 1
        if entry.get("retention") in by_retention:
            by_retention[entry["retention"]] += 1
    return {"by_kind": by_kind, "by_status": by_status, "by_grade": by_grade,
            "by_licence": by_licence, "by_access": by_access, "by_retention": by_retention}


def aggregate(entries: list[dict]) -> dict:
    return {"total": len(entries), **_entry_counts(entries)}


def freshness(review_due: str, today: date | None) -> str:
    """Freshness class of a record relative to ``today`` (UNKNOWN without a date)."""
    if today is None or not _is_date(review_due):
        return "UNKNOWN"
    due = _parse_date(review_due)
    if due < today:
        return "stale"
    if due <= today + timedelta(days=30):
        return "due_soon"
    return "fresh"


def _freshness_counts(entries: list[dict], today: date | None) -> dict:
    counts = {"fresh": 0, "due_soon": 0, "stale": 0, "UNKNOWN": 0}
    for entry in entries:
        state = freshness(str(entry.get("review_due")), today)
        counts[state] = counts.get(state, 0) + 1
    return counts


def _norm_title(value: object) -> str:
    text = str(value or "").lower().replace("ё", "е")
    return " ".join(text.split())


def content_key(entry: dict) -> tuple:
    """Dedupe key: the same knowledge in the same project may exist once."""
    return (str(entry.get("namespace")), str(entry.get("kind")), _norm_title(entry.get("title")))


def validate_all(archive: Path = DEFAULT_ARCHIVE, today: date | None = None) -> dict:
    """Validate the whole archive and return the machine summary."""
    archive = Path(archive)
    schema = load_schema(archive)
    namespaces, roles, schema_issues = validate_schema(schema)
    entries, entry_issues = load_entries(archive)
    inbox, inbox_issues = load_inbox(archive)
    issues: list[dict] = list(schema_issues) + list(entry_issues) + list(inbox_issues)

    records = entries + inbox
    by_id = {str(record.get("id")): record for record in records
             if isinstance(record.get("id"), str) and ID_RE.match(str(record.get("id")))}
    for position, entry in enumerate(records, start=1):
        issues.extend(validate_entry(entry, namespaces=namespaces, roles=roles,
                                     by_id=by_id, position=position))

    seen: set[str] = set()
    previous: dict[str, str] = {}
    for entry in records:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        if entry_id in seen:
            issues.append(_issue("duplicate_id", "id", f"duplicate id {entry_id}",
                                 entry.get("__where")))
        seen.add(entry_id)
        where_file = str(entry.get("__where") or "").split(":")[0]
        last = previous.get(where_file)
        if last and entry_id < last:
            issues.append(_issue("id_order", "id",
                                 f"ids must ascend within a file: {entry_id} after {last}",
                                 entry.get("__where")))
        previous[where_file] = entry_id

    keys: dict[tuple, str] = {}
    for entry in records:
        key = content_key(entry)
        if key in keys:
            issues.append(_issue("duplicate_content", "title",
                                 f"duplicate (namespace,kind,title) with {keys[key]}",
                                 entry.get("__where")))
        else:
            keys[key] = str(entry.get("id"))

    by_namespace = {name: 0 for name in namespaces}
    for entry in entries:
        if entry.get("namespace") in by_namespace:
            by_namespace[entry["namespace"]] += 1

    issues.sort(key=lambda item: (str(item.get("where") or ""), item["code"], item["field"]))
    counts = aggregate(entries)
    counts["by_namespace"] = by_namespace
    return {
        "schema": VALIDATE_SCHEMA,
        "archive": str(archive),
        "schema_version": schema.get("version"),
        "valid": not issues,
        "errors": len(issues),
        "entries": len(entries),
        "inbox_entries": len(inbox),
        "namespaces": len(namespaces),
        "roles": len(roles),
        "counts": counts,
        "inbox_counts": aggregate(inbox),
        "freshness": _freshness_counts(entries, today),
        "generated_for": today.isoformat() if today else None,
        "issues": issues,
        "policy": {
            "network": "disabled",
            "clock": "not read (pass --today explicitly)",
            "writes": "inside knowledge_archive/ only",
            "model_status": "RESEARCH_ONLY",
            "admission": False,
            "pii": "forbidden",
        },
    }


def check(archive: Path = DEFAULT_ARCHIVE, today: date | None = None) -> dict:
    """Validate and return the summary (alias for :func:`validate_all`)."""
    return validate_all(archive, today)


def search_entries(entries: list[dict], query: str = "", *, kind: str | None = None,
                   namespace: str | None = None, project: str | None = None,
                   tag: str | None = None, status: str | None = None,
                   licence: str | None = None, access: str | None = None,
                   today: date | None = None, only_stale: bool = False,
                   limit: int | None = None) -> list[dict]:
    """Token search with filters; deterministic order by id."""
    tokens = [token for token in re.split(r"\s+", (query or "").strip().lower()) if token]
    results = []
    for entry in entries:
        haystack = " ".join([
            str(entry.get("id") or ""), str(entry.get("namespace") or ""),
            str(entry.get("kind") or ""), str(entry.get("title") or ""),
            str(entry.get("summary") or ""), " ".join(entry.get("tags") or []),
            " ".join(entry.get("audience") or []), str(entry.get("owner_role") or ""),
            str(entry.get("notes") or ""),
        ]).lower()
        if any(not _token_matches(token, haystack) for token in tokens):
            continue
        if kind and entry.get("kind") != kind:
            continue
        if namespace and entry.get("namespace") != namespace:
            continue
        if project:
            if entry.get("namespace") not in (project, GLOBAL_NS) and entry.get("access") != "PUBLIC":
                continue
        if tag and tag not in (entry.get("tags") or []):
            continue
        if status and entry.get("evidence_status") != status:
            continue
        if licence and entry.get("licence") != licence:
            continue
        if access and entry.get("access") != access:
            continue
        if only_stale and freshness(str(entry.get("review_due")), today) != "stale":
            continue
        if entry.get("archived") is True:
            continue
        results.append(entry)
    results.sort(key=lambda item: str(item.get("id") or ""))
    if limit is not None:
        results = results[:limit]
    return results


def _next_id(records: list[dict]) -> str:
    numbers = [int(str(record.get("id"))[3:]) for record in records
               if isinstance(record.get("id"), str) and ID_RE.match(str(record.get("id")))]
    return f"AR-{(max(numbers) + 1) if numbers else 1:04d}"


def build_entry(*, kind: str, namespace: str, title: str, summary: str, owner_role: str,
                audience: list[str], as_of: str, evidence: str = "HYPOTHESIS",
                grade: str | None = "C", licence: str = "INTERNAL", access: str | None = None,
                provenance: dict | None = None, source_ids: list[str] | None = None,
                tags: list[str] | None = None, links: list[str] | None = None,
                notes: str = "", ttl_days: int | None = None, entry_id: str | None = None,
                retention: str | None = None, version: int = 1,
                jurisdiction: str | None = None, supersedes: str | None = None,
                archived: bool = False) -> dict:
    """Build an archive record; ``review_due`` is derived from ``as_of + ttl_days``."""
    ttl = ttl_days if ttl_days is not None else DEFAULT_TTL.get(kind, 180)
    review_due = (_parse_date(as_of) + timedelta(days=ttl)).isoformat() if _is_date(as_of) else None
    if provenance is None:
        provenance = {"origin": "", "origin_type": "EXPERT_NOTE", "collected_by": owner_role,
                      "collected_at": as_of, "method": "EXPERT_NOTE", "retrieved_at": None,
                      "sha256": None}
    entry = {
        "id": entry_id or "",
        "schema": ARCHIVE_SCHEMA,
        "namespace": namespace,
        "kind": kind,
        "title": title,
        "summary": summary,
        "tags": tags or [],
        "audience": audience,
        "evidence_status": evidence,
        "evidence_grade": grade,
        "licence": licence,
        "access": access or DEFAULT_ACCESS.get(kind, "PROJECT"),
        "provenance": provenance,
        "source_ids": source_ids or [],
        "as_of": as_of,
        "ttl_days": ttl,
        "review_due": review_due,
        "owner_role": owner_role,
        "retention": retention or DEFAULT_RETENTION.get(kind, "PROJECT"),
        "version": version,
        "created": as_of,
        "updated": as_of,
        "notes": notes or "Запись создана через tools/archivist.py add.",
        "links": links or [],
        "supersedes": supersedes,
        "archived": archived,
    }
    if jurisdiction is not None:
        entry["jurisdiction"] = jurisdiction
    return entry


def render_entry_line(entry: dict) -> str:
    """One canonical JSONL line (no ``__where`` helper key)."""
    clean = {key: value for key, value in entry.items() if key != "__where"}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


def _dependency_block(issues: list[dict], title: str) -> str:
    return title + "\n" + "\n".join(
        f"  {issue['code']}: {issue.get('field')}: {issue['message']}" for issue in issues)


def add_entry(archive: Path, entry: dict, out: Path | None = None) -> tuple[Path, dict]:
    """Validate and append one record to ``inbox/`` (default) or ``out``."""
    archive = Path(archive)
    schema = load_schema(archive)
    namespaces, roles, schema_issues = validate_schema(schema)
    if schema_issues:
        raise ArchiveError(_dependency_block(schema_issues, "schema.json is invalid"))
    entries, _ = load_entries(archive)
    inbox, _ = load_inbox(archive)
    existing = entries + inbox
    if not entry.get("id"):
        entry["id"] = _next_id(existing)
    if any(item.get("id") == entry["id"] for item in existing):
        raise ArchiveError(f"record id already exists: {entry['id']}")
    if content_key(entry) in {content_key(item) for item in existing}:
        raise ArchiveError("duplicate content: (namespace, kind, title) already exists; "
                           "use --supersedes with a new version")
    by_id = {str(item.get("id")): item for item in existing
             if isinstance(item.get("id"), str) and ID_RE.match(str(item.get("id")))}
    if entry.get("supersedes") and entry.get("version", 1) <= 1:
        target = by_id.get(str(entry["supersedes"]))
        if target and isinstance(target.get("version"), int):
            entry["version"] = target["version"] + 1
    by_id[entry["id"]] = entry
    issues = validate_entry(entry, namespaces=namespaces, roles=roles, by_id=by_id)
    if issues:
        raise ArchiveError(_dependency_block(issues, "record is invalid"))
    target = Path(out) if out else archive / "inbox" / f"{entry['id']}.jsonl"
    if target.exists():
        raise ArchiveError(f"file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_entry_line(entry) + "\n", encoding="utf-8", newline="\n")
    return target, entry


def merge_inbox(archive: Path = DEFAULT_ARCHIVE, to: Path | None = None) -> dict:
    """Merge validated inbox records into ``entries/``; duplicates are skipped, not merged."""
    archive = Path(archive)
    schema = load_schema(archive)
    namespaces, roles, schema_issues = validate_schema(schema)
    if schema_issues:
        raise ArchiveError(_dependency_block(schema_issues, "schema.json is invalid"))
    inbox_dir = archive / "inbox"
    inbox_files = sorted(inbox_dir.glob("*.jsonl")) if inbox_dir.is_dir() else []
    entries, entry_parse_issues = load_entries(archive)
    inbox, inbox_parse_issues = load_inbox(archive)
    issues: list[dict] = list(entry_parse_issues) + list(inbox_parse_issues)

    existing_ids = {str(entry.get("id")) for entry in entries}
    duplicate_ids: list[str] = []
    pending: list[dict] = []
    for record in inbox:
        record_id = str(record.get("id"))
        if record_id in existing_ids:
            duplicate_ids.append(record_id)
        else:
            pending.append(record)

    known_keys = {content_key(entry): str(entry.get("id")) for entry in entries}
    duplicate_content: list[str] = []
    accepted: list[dict] = []
    for record in pending:
        key = content_key(record)
        if key in known_keys:
            duplicate_content.append(f"{record.get('id')}:content={known_keys[key]}")
        else:
            known_keys[key] = str(record.get("id"))
            accepted.append(record)

    by_id = {str(record.get("id")): record for record in entries + accepted
             if isinstance(record.get("id"), str) and ID_RE.match(str(record.get("id")))}
    for record in entries + accepted:
        issues.extend(validate_entry(record, namespaces=namespaces, roles=roles, by_id=by_id))

    numbers = [int(str(item.get("id"))[3:]) for item in entries
               if isinstance(item.get("id"), str) and ID_RE.match(str(item.get("id")))]
    highest = max(numbers) if numbers else 0
    kept: list[dict] = []
    for record in accepted:
        record_id = str(record.get("id"))
        if ID_RE.match(record_id) and int(record_id[3:]) <= highest:
            issues.append(_issue("id_order", "id",
                                 f"new id {record_id} must be greater than the last id in entries",
                                 record.get("__where")))
            continue
        kept.append(record)
    if issues:
        raise ArchiveError(_dependency_block(issues, "inbox is not mergeable"))

    target = Path(to) if to else archive / "entries" / "seed.jsonl"
    if kept:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for record in kept:
                handle.write(render_entry_line(record) + "\n")
    archive_dir = inbox_dir / "merged"
    archived: list[str] = []
    if inbox_files:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in inbox_files:
            destination = archive_dir / path.name
            counter = 1
            while destination.exists():
                destination = archive_dir / f"{path.stem}.{counter}{path.suffix}"
                counter += 1
            path.replace(destination)
            archived.append(str(destination.relative_to(archive)).replace("\\", "/"))
    return {"schema": MERGE_SCHEMA, "archive": str(archive), "target": str(target),
            "inbox": len(inbox), "merged": len(kept),
            "duplicates": sorted(duplicate_ids + duplicate_content),
            "archived": archived, "ids": [record.get("id") for record in kept]}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9а-яё]+", "-", str(text or "").strip().lower(), flags=re.IGNORECASE)
    slug = slug.strip("-")
    return slug[:40].strip("-") or "all"


def _topic_tokens(topic: str) -> list[str]:
    return [token for token in re.split(r"\s+", (topic or "").strip().lower()) if token]


def _haystack(entry: dict) -> str:
    """Text of a record for topic relevance (roles are not topics)."""
    return " ".join([
        str(entry.get("id") or ""), str(entry.get("namespace") or ""),
        str(entry.get("kind") or ""), str(entry.get("title") or ""),
        str(entry.get("summary") or ""), " ".join(entry.get("tags") or []),
        str(entry.get("notes") or ""),
    ]).lower()


def _token_matches(token: str, haystack: str) -> bool:
    """Exact token or a conservative stem prefix (Russian inflections).

    ``pack`` is a relevance search, not a strict query language: "источники" must find
    "источника"/"источников", otherwise the pack silently loses the right records.
    """
    if token in haystack:
        return True
    if len(token) >= 5:
        return token[:max(5, len(token) - 2)] in haystack
    return False


def select_for_pack(entries: list[dict], *, project: str, topic: str = "",
                    role: str | None = None, today: date | None = None,
                    include_stale: bool = False, allow_restricted: bool = False,
                    limit: int | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Select records for a context pack; returns ``(selected, excluded, gaps)``."""
    tokens = _topic_tokens(topic)
    candidates: list[tuple[dict, list[str], str]] = []
    excluded: list[dict] = []
    visible_count = 0
    for entry in sorted(entries, key=lambda item: str(item.get("id") or "")):
        entry_id = str(entry.get("id"))
        if entry.get("archived") is True:
            excluded.append({"id": entry_id, "reason": "archived"})
            continue
        namespace = entry.get("namespace")
        access = entry.get("access")
        if namespace != project and namespace != GLOBAL_NS and access != "PUBLIC":
            excluded.append({"id": entry_id, "reason": f"namespace:{namespace}"})
            continue
        if access == "RESTRICTED":
            if not (namespace == project and allow_restricted):
                excluded.append({"id": entry_id, "reason": "access:restricted"})
                continue
        visible_count += 1
        if role and not (role in (entry.get("audience") or []) or "all" in (entry.get("audience") or [])):
            excluded.append({"id": entry_id, "reason": f"audience:{role}"})
            continue
        state = freshness(str(entry.get("review_due")), today)
        if state == "stale" and not include_stale:
            excluded.append({"id": entry_id, "reason": "stale"})
            continue
        haystack = _haystack(entry)
        matched = [token for token in tokens if _token_matches(token, haystack)]
        if tokens and not matched:
            excluded.append({"id": entry_id, "reason": "topic"})
            continue
        candidates.append((entry, matched, state))

    candidates.sort(key=lambda item: (-len(item[1]), FRESHNESS_RANK[item[2]],
                                      str(item[0].get("id"))))
    if limit is not None and len(candidates) > limit:
        for entry, _, _ in candidates[limit:]:
            excluded.append({"id": str(entry.get("id")), "reason": "limit"})
        candidates = candidates[:limit]

    selected: list[dict] = []
    matched_tokens: set[str] = set()
    for entry, matched, state in candidates:
        matched_tokens.update(matched)
        view = {key: value for key, value in entry.items() if key != "__where"}
        view["freshness"] = state
        view["match"] = matched
        view["visibility"] = ("project" if entry.get("namespace") == project
                              else GLOBAL_NS if entry.get("namespace") == GLOBAL_NS
                              else f"public:{entry.get('namespace')}")
        selected.append(view)

    gaps: list[str] = []
    for token in tokens:
        if token not in matched_tokens:
            gaps.append(f"нет видимых записей по токену «{token}»")
    if not selected:
        where = f" в проекте {project}" if project else ""
        gaps.append(f"нет записей по запросу{where}: тема «{topic or 'любая'}»")
    if excluded and any(item["reason"] == "stale" for item in excluded):
        stale_count = sum(1 for item in excluded if item["reason"] == "stale")
        gaps.append(f"исключено просроченных: {stale_count} (--include-stale, чтобы включить)")
    if selected and not any(item.get("evidence_status") in ("VERIFIED", "PARTIAL")
                            for item in selected):
        gaps.append("в пакете нет записей с подтверждённым evidence (VERIFIED/PARTIAL)")
    return selected, sorted(excluded, key=lambda item: (item["id"], item["reason"])), gaps


def _pack_counts(selected: list[dict], excluded: list[dict]) -> dict:
    counts = {"selected": len(selected), "excluded": len(excluded),
              "fresh": 0, "due_soon": 0, "stale": 0, "UNKNOWN": 0}
    for entry in selected:
        state = str(entry.get("freshness") or "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _licence_notices(selected: list[dict]) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    for entry in selected:
        grouped.setdefault(str(entry.get("licence")), []).append(str(entry.get("id")))
    return [{"licence": licence, "ids": sorted(ids), "note": LICENCE_NOTES.get(licence, "")}
            for licence, ids in sorted(grouped.items())]


def build_pack(archive: Path = DEFAULT_ARCHIVE, *, project: str, topic: str = "",
               role: str | None = None, today: date | None = None, limit: int | None = None,
               include_stale: bool = False, allow_restricted: bool = False,
               write: bool = True) -> dict:
    """Build a context pack for one project/topic/role and optionally write it."""
    archive = Path(archive)
    schema = load_schema(archive)
    namespaces, roles, schema_issues = validate_schema(schema)
    if schema_issues:
        raise ArchiveError(_dependency_block(schema_issues, "schema.json is invalid"))
    if project not in namespaces:
        raise ArchiveError(f"unknown project namespace: {project!r}; "
                           f"known: {sorted(namespaces)}")
    if role is not None and role not in roles:
        raise ArchiveError(f"unknown role: {role!r}")
    if limit is not None and limit < 1:
        raise ArchiveError("--limit must be a positive integer")
    entries, _ = load_entries(archive)
    selected, excluded, gaps = select_for_pack(
        entries, project=project, topic=topic, role=role, today=today,
        include_stale=include_stale, allow_restricted=allow_restricted, limit=limit)
    pack = {
        "schema": PACK_SCHEMA,
        "archive": str(archive),
        "project": project,
        "topic": topic,
        "role": role,
        "generated_for": today.isoformat() if today else None,
        "limits": {"limit": limit, "include_stale": include_stale,
                   "allow_restricted": allow_restricted},
        "counts": _pack_counts(selected, excluded),
        "entries": selected,
        "excluded": excluded,
        "gap_count": len(gaps),
        "gaps": gaps,
        "licence_notices": _licence_notices(selected),
        "policy": {
            "network": "disabled",
            "clock": "not read (pass --today explicitly)",
            "evidence": "VERIFIED/PARTIAL/HYPOTHESIS/NOT_FETCHED; numbers require a source",
            "model_status": "RESEARCH_ONLY",
            "admission": False,
        },
    }
    if write:
        packs_dir = archive / "context_packs"
        packs_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{project}--{_slug(topic)}--{role or 'all'}"
        (packs_dir / f"{stem}.json").write_text(_dump_json(pack), encoding="utf-8", newline="\n")
        (packs_dir / f"{stem}.md").write_text(render_pack_md(pack), encoding="utf-8",
                                              newline="\n")
        pack["files"] = [f"context_packs/{stem}.json", f"context_packs/{stem}.md"]
    return pack


def render_pack_md(pack: dict) -> str:
    """Deterministic Markdown context pack."""
    counts = pack["counts"]
    lines = [
        f"# Контекст-пак: {pack['project']} / {pack['topic'] or 'любая тема'}"
        f"{' / ' + pack['role'] if pack['role'] else ''}",
        "",
        f"Собрано на дату: **{pack['generated_for'] or 'не задана (--today)'}**. "
        f"Записей: **{counts['selected']}**, исключено: {counts['excluded']}.",
        f"Свежесть: fresh={counts['fresh']}, due_soon={counts['due_soon']}, "
        f"stale={counts['stale']}, UNKNOWN={counts['UNKNOWN']}.",
        "",
        "Пакет собирается детерминированно: сортировка — совпадение темы, затем свежесть, затем id.",
        "",
        "## Записи",
        "",
        "| ID | Namespace | Вид | Статус | Grade | Лицензия | Доступ | Review due | Название |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in pack["entries"]:
        lines.append(
            f"| {entry.get('id')} | {entry.get('namespace')} | {entry.get('kind')} | "
            f"{entry.get('evidence_status')} | {entry.get('evidence_grade') or '-'} | "
            f"{entry.get('licence')} | {entry.get('access')} | {entry.get('review_due')} | "
            f"{entry.get('title')} |")
    lines += ["", "## Как читать записи", ""]
    for entry in pack["entries"]:
        provenance = entry.get("provenance") or {}
        lines += [
            f"### {entry.get('id')} — {entry.get('title')}", "",
            str(entry.get("summary")), "",
            f"- вид/статус: {entry.get('kind')} / {entry.get('evidence_status')}"
            f" ({entry.get('evidence_grade') or '-'})",
            f"- провенанс: {provenance.get('origin_type')} — {provenance.get('origin')}",
            f"- собрал: {provenance.get('collected_by')} {provenance.get('collected_at')}; "
            f"метод: {provenance.get('method')}; retrieved_at: "
            f"{provenance.get('retrieved_at') or 'нет'}",
            f"- лицензия/доступ: {entry.get('licence')} / {entry.get('access')}; "
            f"review_due: {entry.get('review_due')} ({entry.get('freshness')})",
            f"- адресовано: {', '.join(entry.get('audience') or [])}",
            f"- совпадение с темой: {', '.join(entry.get('match') or []) or 'тема не задана'}",
            f"- заметки: {entry.get('notes')}", "",
        ]
    lines += ["## Лицензии", "", "| Лицензия | Записи | Что можно |", "|---|---|---|"]
    for notice in pack["licence_notices"]:
        lines.append(f"| {notice['licence']} | {', '.join(notice['ids'])} | {notice['note']} |")
    if pack["excluded"]:
        lines += ["", "## Исключено (и почему)", "", "| ID | Причина |", "|---|---|"]
        for item in pack["excluded"]:
            lines.append(f"| {item['id']} | {item['reason']} |")
    if pack["gaps"]:
        lines += ["", "## Пробелы", ""]
        for gap in pack["gaps"]:
            lines.append(f"- {gap}")
    lines += ["", "## Границы", "",
              "Пакет не является юридическим заключением, инвестиционной рекомендацией или "
              "моделью, прошедшей приёмку. Числа без источника в пакет не попадают; NOT_FETCHED "
              "означает «не получено», а не «нет в мире».", ""]
    return "\n".join(lines)


def _dump_json(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_index(archive: Path = DEFAULT_ARCHIVE, today: date | None = None) -> dict:
    """Build and write ``index.json`` / ``index.md``; returns the index document."""
    archive = Path(archive)
    summary = validate_all(archive, today)
    entries, _ = load_entries(archive)
    schema = load_schema(archive)
    namespaces = load_namespaces(schema)

    chains: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry.get("supersedes"), str):
            chains[str(entry["supersedes"])] = str(entry.get("id"))
    namespace_rows = []
    for name in sorted(namespaces):
        subset = [entry for entry in entries if entry.get("namespace") == name]
        active = [entry for entry in subset if entry.get("archived") is not True]
        namespace_rows.append({
            "namespace": name,
            "owner_role": namespaces[name].get("owner_role"),
            "title": namespaces[name].get("title"),
            "total": len(subset),
            "active": len(active),
            "stale": sum(1 for entry in subset
                         if freshness(str(entry.get("review_due")), today) == "stale"),
            "audience": sorted({role for entry in subset
                                for role in (entry.get("audience") or [])}),
        })
    rows = []
    for entry in sorted(entries, key=lambda item: str(item.get("id"))):
        rows.append({
            "id": entry.get("id"),
            "namespace": entry.get("namespace"),
            "kind": entry.get("kind"),
            "title": entry.get("title"),
            "evidence_status": entry.get("evidence_status"),
            "evidence_grade": entry.get("evidence_grade"),
            "licence": entry.get("licence"),
            "access": entry.get("access"),
            "retention": entry.get("retention"),
            "review_due": entry.get("review_due"),
            "freshness": freshness(str(entry.get("review_due")), today),
            "owner_role": entry.get("owner_role"),
            "version": entry.get("version"),
            "supersedes": entry.get("supersedes"),
            "superseded_by": chains.get(str(entry.get("id"))),
            "archived": bool(entry.get("archived")),
        })
    index = {
        "schema": INDEX_SCHEMA,
        "archive": str(archive),
        "generated_for": today.isoformat() if today else None,
        "valid": summary["valid"],
        "errors": summary["errors"],
        "totals": summary["counts"]["total"],
        "active": len([entry for entry in entries if entry.get("archived") is not True]),
        "counts": summary["counts"],
        "freshness": summary["freshness"],
        "namespaces": namespace_rows,
        "entries": rows,
        "policy": summary["policy"],
        "issues": summary["issues"],
    }
    (archive / "index.json").write_text(_dump_json(index), encoding="utf-8", newline="\n")
    (archive / "index.md").write_text(render_index_md(index), encoding="utf-8", newline="\n")
    return index


def render_index_md(index: dict) -> str:
    counts = index["counts"]
    statuses = ", ".join(f"{key}={value}" for key, value in counts["by_status"].items())
    kinds = ", ".join(f"{key}={value}" for key, value in counts["by_kind"].items())
    lines = [
        "# Индекс межпроектного архива AMFE",
        "",
        f"Записей: **{index['totals']}** (активных {index['active']}). "
        f"Свежесть рассчитана на: {index['generated_for'] or 'не рассчитана (--today не задан)'}.",
        f"Статусы: {statuses}. Виды: {kinds}.",
        "",
        "## Пространства имён",
        "",
        "| Namespace | Владелец | Всего | Активных | Просрочено | Аудитория |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in index["namespaces"]:
        lines.append(f"| {row['namespace']} | {row['owner_role']} | {row['total']} | "
                     f"{row['active']} | {row['stale']} | {', '.join(row['audience']) or '-'} |")
    lines += ["", "## Записи", "",
              "| ID | Namespace | Вид | Статус | Grade | Лицензия | Доступ | Review due | "
              "Свежесть | Название |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for row in index["entries"]:
        mark = " (архив)" if row["archived"] else ""
        lines.append(f"| {row['id']} | {row['namespace']} | {row['kind']} | "
                     f"{row['evidence_status']} | {row['evidence_grade'] or '-'} | "
                     f"{row['licence']} | {row['access']} | {row['review_due']} | "
                     f"{row['freshness']} | {row['title']}{mark} |")
    lines += ["", "## Свежесть", "", "| Класс | Записей |", "|---|---:|"]
    for key, value in index["freshness"].items():
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Счётчики", "",
              f"- по доступу: " + ", ".join(f"{key}={value}"
                                            for key, value in counts["by_access"].items()),
              f"- по хранению: " + ", ".join(f"{key}={value}"
                                             for key, value in counts["by_retention"].items()),
              f"- по лицензиям: " + ", ".join(f"{key}={value}"
                                              for key, value in counts["by_licence"].items())]
    if index["issues"]:
        lines += ["", "## Дефекты", "", f"Всего: {index['errors']}", ""]
        for issue in index["issues"]:
            lines.append(f"- `{issue['code']}` ({issue['field']}): {issue['message']}")
    else:
        lines += ["", "Дефектов нет: схема, провенанс, лицензии, доступ, ссылки и свежесть "
                      "в порядке."]
    return "\n".join(lines) + "\n"


def export(archive: Path = DEFAULT_ARCHIVE, fmt: str = "json",
           today: date | None = None) -> str:
    """Export the archive as deterministic JSON, Markdown or JSONL."""
    archive = Path(archive)
    entries, _ = load_entries(archive)
    schema = load_schema(archive)
    namespaces = load_namespaces(schema)
    summary = validate_all(archive, today)
    ordered = sorted(entries, key=lambda item: str(item.get("id")))
    if fmt == "jsonl":
        return "".join(render_entry_line(entry) + "\n" for entry in ordered)
    if fmt == "json":
        payload = {
            "schema": EXPORT_SCHEMA,
            "archive": str(archive),
            "valid": summary["valid"],
            "generated_for": today.isoformat() if today else None,
            "totals": summary["counts"]["total"],
            "namespaces": {name: namespaces[name] for name in sorted(namespaces)},
            "entries": [{key: value for key, value in entry.items() if key != "__where"}
                        for entry in ordered],
        }
        return _dump_json(payload)
    lines = ["# Экспорт межпроектного архива AMFE", "",
             f"Записей: {summary['counts']['total']}; статус: RESEARCH_ONLY, admission=false; "
             f"дата расчёта свежести: {today.isoformat() if today else 'не задана'}.", ""]
    for namespace in sorted(namespaces):
        subset = [entry for entry in ordered if entry.get("namespace") == namespace]
        if not subset:
            continue
        lines += [f"## {namespace} ({len(subset)})", "",
                  "| ID | Вид | Статус | Grade | Лицензия | Доступ | Review due | Название |",
                  "|---|---|---|---|---|---|---|---|"]
        for entry in subset:
            lines.append(f"| {entry.get('id')} | {entry.get('kind')} | "
                         f"{entry.get('evidence_status')} | {entry.get('evidence_grade') or '-'} | "
                         f"{entry.get('licence')} | {entry.get('access')} | "
                         f"{entry.get('review_due')} | {entry.get('title')} |")
        lines.append("")
        for entry in subset:
            provenance = entry.get("provenance") or {}
            lines += [f"### {entry.get('id')} — {entry.get('title')}", "",
                      str(entry.get("summary")), "",
                      f"- провенанс: {provenance.get('origin_type')} — "
                      f"{provenance.get('origin')}",
                      f"- лицензия/доступ: {entry.get('licence')} / {entry.get('access')}; "
                      f"retention: {entry.get('retention')}",
                      f"- sources: {', '.join(entry.get('source_ids') or []) or 'нет'}",
                      f"- review_due: {entry.get('review_due')}; owner: {entry.get('owner_role')}; "
                      f"version: {entry.get('version')}",
                      f"- notes: {entry.get('notes')}", ""]
    return "\n".join(lines) + "\n"


def render_validate(summary: dict) -> str:
    lines = [
        f"Архив: {summary['entries']} записей, inbox {summary['inbox_entries']}, "
        f"namespace {summary['namespaces']}, ошибок {summary['errors']}, "
        f"valid={str(summary['valid']).lower()}",
        "Статусы: " + ", ".join(f"{key}={value}" for key, value in summary["counts"]["by_status"].items()),
        "Виды: " + ", ".join(f"{key}={value}" for key, value in summary["counts"]["by_kind"].items()),
        "Свежесть: " + ", ".join(f"{key}={value}" for key, value in summary["freshness"].items()),
    ]
    if summary["issues"]:
        lines += ["", "Ошибки:"]
        for issue in summary["issues"]:
            where = f"[{issue['where']}] " if issue.get("where") else ""
            lines.append(f"  {where}{issue['code']}: {issue['field']}: {issue['message']}")
    else:
        lines.append("Ошибок нет: схема, провенанс, лицензии, доступ, ссылки и свежесть в порядке.")
    return "\n".join(lines)


def _split(value: str | None, separator: str = ",") -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(separator) if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AMFE cross-project knowledge archive tool")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE),
                        help="archive directory (default: knowledge_archive/)")
    parser.add_argument("--check", action="store_true", help="validate; exit 2 on any issue")
    parser.add_argument("--json", action="store_true", help="machine output for --check")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="validate the archive")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--today")

    search = sub.add_parser("search", help="search records")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--kind", choices=KINDS)
    search.add_argument("--namespace")
    search.add_argument("--project")
    search.add_argument("--tag")
    search.add_argument("--evidence", choices=STATUSES)
    search.add_argument("--licence", choices=LICENCES)
    search.add_argument("--access", choices=ACCESS_LEVELS)
    search.add_argument("--today")
    search.add_argument("--stale", action="store_true")
    search.add_argument("--limit", type=int)
    search.add_argument("--json", action="store_true")

    add = sub.add_parser("add", help="build, validate and append one record to inbox")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--namespace", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--owner-role", required=True)
    add.add_argument("--audience", required=True, help="comma-separated roles or 'all'")
    add.add_argument("--as-of", required=True, help="ISO date, e.g. 2026-11-22")
    add.add_argument("--evidence", default="HYPOTHESIS", choices=STATUSES)
    add.add_argument("--grade", default="C", choices=("A", "B", "C", "none"))
    add.add_argument("--licence", default="INTERNAL", choices=LICENCES)
    add.add_argument("--access", choices=ACCESS_LEVELS)
    add.add_argument("--origin", required=True)
    add.add_argument("--origin-type", required=True, choices=ORIGIN_TYPES)
    add.add_argument("--collected-by", required=True)
    add.add_argument("--collected-at", help="defaults to --as-of")
    add.add_argument("--method", default="MANUAL_READ", choices=METHODS)
    add.add_argument("--retrieved-at")
    add.add_argument("--sha256")
    add.add_argument("--source", action="append", default=[], help="AR-NNNN (repeatable)")
    add.add_argument("--tag", action="append", default=[], help="tag (repeatable)")
    add.add_argument("--link", action="append", default=[], help="repo-relative link (repeatable)")
    add.add_argument("--notes", default="")
    add.add_argument("--ttl-days", type=int)
    add.add_argument("--id", dest="entry_id")
    add.add_argument("--retention", choices=RETENTIONS)
    add.add_argument("--version", type=int)
    add.add_argument("--jurisdiction")
    add.add_argument("--supersedes")
    add.add_argument("--out")

    merge = sub.add_parser("merge", help="merge validated inbox records into entries")
    merge.add_argument("--to")

    index = sub.add_parser("index", help="rebuild index.json and index.md")
    index.add_argument("--today")

    pack = sub.add_parser("pack", help="build a context pack for a project/topic/role")
    pack.add_argument("--project", required=True)
    pack.add_argument("--topic", default="")
    pack.add_argument("--role")
    pack.add_argument("--today")
    pack.add_argument("--limit", type=int)
    pack.add_argument("--include-stale", action="store_true")
    pack.add_argument("--allow-restricted", action="store_true")
    pack.add_argument("--json", action="store_true")

    export_cmd = sub.add_parser("export", help="export as JSON, Markdown or JSONL")
    export_cmd.add_argument("--format", choices=("json", "md", "jsonl"), default="json")
    export_cmd.add_argument("--today")
    export_cmd.add_argument("--out")

    args = parser.parse_args(argv)
    archive = Path(args.archive)

    def _today(value: str | None) -> date | None:
        if not value:
            return None
        if not _is_date(value):
            raise ArchiveError(f"--today must be an ISO date YYYY-MM-DD: {value!r}")
        return _parse_date(value)

    try:
        if args.command == "add":
            grade = None if args.grade == "none" else args.grade
            provenance = {
                "origin": args.origin,
                "origin_type": args.origin_type,
                "collected_by": args.collected_by,
                "collected_at": args.collected_at or args.as_of,
                "method": args.method,
                "retrieved_at": args.retrieved_at,
                "sha256": args.sha256,
            }
            entry = build_entry(
                kind=args.kind, namespace=args.namespace, title=args.title,
                summary=args.summary, owner_role=args.owner_role,
                audience=_split(args.audience), as_of=args.as_of, evidence=args.evidence,
                grade=grade, licence=args.licence, access=args.access, provenance=provenance,
                source_ids=_split(",".join(args.source)), tags=[tag for tag in args.tag if tag],
                links=list(args.link), notes=args.notes, ttl_days=args.ttl_days,
                entry_id=args.entry_id, retention=args.retention,
                version=args.version if args.version is not None else 1,
                jurisdiction=args.jurisdiction, supersedes=args.supersedes)
            target, written = add_entry(archive, entry, Path(args.out) if args.out else None)
            print(f"added: {written['id']} -> {target}")
            return 0
        if args.command == "merge":
            result = merge_inbox(archive, Path(args.to) if args.to else None)
            print(f"merged {result['merged']} of {result['inbox']} inbox records into "
                  f"{result['target']}; duplicates: {len(result['duplicates'])}; "
                  f"archived: {len(result['archived'])}")
            if result["ids"]:
                print("new ids: " + ", ".join(str(item) for item in result["ids"]))
            return 0
        if args.command == "index":
            today = _today(args.today)
            result = build_index(archive, today)
            print(f"index: {result['totals']} records, active={result['active']}, "
                  f"fresh={result['freshness']['fresh']}, due_soon={result['freshness']['due_soon']}, "
                  f"stale={result['freshness']['stale']}, "
                  f"unknown={result['freshness']['UNKNOWN']} -> {archive / 'index.json'}")
            return 0 if result["valid"] else 2
        if args.command == "pack":
            today = _today(args.today)
            result = build_pack(archive, project=args.project, topic=args.topic, role=args.role,
                                today=today, limit=args.limit,
                                include_stale=args.include_stale,
                                allow_restricted=args.allow_restricted)
            if args.json:
                print(_dump_json(result), end="")
            else:
                print(f"pack: {result['project']} / {result['topic'] or 'любая тема'}"
                      f"{' / ' + result['role'] if result['role'] else ''}: "
                      f"{result['counts']['selected']} записей, "
                      f"исключено {result['counts']['excluded']}, "
                      f"пробелов {result['gap_count']}")
                print("files: " + ", ".join(result.get("files") or []))
                for entry in result["entries"]:
                    print(f"  {entry['id']}  [{entry['kind']}/{entry['evidence_status']}]  "
                          f"{entry['namespace']:<8} {entry['title']}")
                for gap in result["gaps"]:
                    print(f"  gap: {gap}")
            return 0
        if args.command == "export":
            text = export(archive, args.format, _today(args.today))
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise ArchiveError(f"output directory does not exist: {out.parent}")
                out.write_text(text, encoding="utf-8", newline="\n")
                print(f"export written: {out}")
            else:
                print(text, end="")
            return 0
        if args.command == "search":
            entries, parse_issues = load_entries(archive)
            if parse_issues:
                raise ArchiveError("cannot search: entries contain invalid JSON lines")
            today = _today(args.today)
            if args.stale and today is None:
                raise ArchiveError("--stale requires --today (no clock is read)")
            results = search_entries(entries, args.query, kind=args.kind,
                                     namespace=args.namespace, project=args.project,
                                     tag=args.tag, status=args.evidence,
                                     licence=args.licence, access=args.access,
                                     today=today, only_stale=args.stale, limit=args.limit)
            if args.json:
                print(_dump_json({
                    "schema": SEARCH_SCHEMA, "query": args.query, "count": len(results),
                    "results": [{key: value for key, value in entry.items() if key != "__where"}
                                for entry in results]}), end="")
            else:
                print(f"найдено: {len(results)}")
                for entry in results:
                    print(f"  {entry.get('id')}  [{entry.get('evidence_status')}/"
                          f"{entry.get('evidence_grade') or '-'}]  "
                          f"{entry.get('namespace'):<8} {entry.get('kind'):<10} "
                          f"{entry.get('title')}")
            return 0
        # default / validate / --check
        summary = validate_all(archive, _today(getattr(args, "today", None)))
        if args.json or (args.command == "validate" and getattr(args, "json", False)):
            print(_dump_json(summary), end="")
        elif args.check or args.command == "validate":
            if summary["valid"]:
                print(f"OK: {summary['entries']} records, {summary['namespaces']} namespaces, "
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
    except ArchiveError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
