"""Source registry loader/validator (G3, docs/162): what is confirmed vs what is an assumption.

The registry is a JSON document (``protocols/source_registry.json``,
``schema=AMFE-SOURCE-REGISTRY/1.0.0``) that lists data sources with licence, access, coverage,
fields and an honest **status**:

* ``CATALOGUED``     — the record was checked (URL, publisher, licence, coverage, fields); no data
                       has been retrieved into the project yet;
* ``NOT_DOWNLOADED`` — the source is identified but access is paid/licensed/manual and no data was
                       retrieved; nothing may be cited as observed from it;
* ``VERIFIED``      — data was actually retrieved into the project and checked (``retrieved_at`` is
                       set, a note points to the evidence). Only VERIFIED may be cited as observed.

The module never makes network calls: it validates the local document, searches it, resolves
``assumption -> source_id`` links and exports a deterministic API view.  All validation is
fail-closed: a missing field, an ``http://`` URL, an unknown status, a VERIFIED record without a
retrieval date, duplicate ids etc. raise :class:`SourceRegistryError`.

Public API
----------
``load`` / ``validate_document`` / ``validate_file``     read and validate.
``sources`` / ``get`` / ``find`` / ``counts`` / ``missing``   read-only views.
``export_for_api``                                     deterministic API export.
``license_class``                                      coarse licence classifier.
``resolve_assumption_source``                          assumption -> source_id resolution (suggestions
                                                       only, never auto-verification).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

SCHEMA = "AMFE-SOURCE-REGISTRY/1.0.0"
EXPORT_SCHEMA = "AMFE-SOURCE-REGISTRY-EXPORT/1.0.0"
STATUSES = ("CATALOGUED", "NOT_DOWNLOADED", "VERIFIED")
ACCESS = ("api", "file", "paid")
VINTAGE_SUPPORT = ("full", "partial", "none", "unknown")
LICENSE_CLASSES = ("public_domain", "open_attribution", "non_commercial", "commercial", "unknown")
SOURCE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUIRED_FIELDS = ("source_id", "name", "publisher", "url", "license", "access", "coverage",
                   "fields", "retrieved_at", "vintage_support", "status")
COVERAGE_KEYS = ("geography", "period", "frequency")


class SourceRegistryError(ValueError):
    """Invalid registry document: a required field is missing or an invariant is violated."""


# ------------------------------------------------------------------------------------------ helpers
def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceRegistryError(f"{name} must be a non-empty string")
    return value.strip()


def _date(value, name: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise SourceRegistryError(f"{name} must be an ISO date, got null")
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise SourceRegistryError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    month, day = int(value[5:7]), int(value[8:10])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise SourceRegistryError(f"{name} is not a valid date: {value!r}")
    return value


def _string_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SourceRegistryError(f"{name} must be a non-empty list of non-empty strings")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SourceRegistryError(f"{name} contains a non-string or empty value")
        items.append(item.strip())
    return items


def _coverage(value, name: str) -> dict:
    if not isinstance(value, Mapping):
        raise SourceRegistryError(f"{name} must be an object with {COVERAGE_KEYS}")
    for key in COVERAGE_KEYS:
        _text(value.get(key), f"{name}.{key}")
    return {key: str(value[key]).strip() for key in COVERAGE_KEYS}


def _url(value, name: str) -> str:
    text = _text(value, name)
    if not text.startswith("https://"):
        raise SourceRegistryError(f"{name} must use the https:// scheme, got {text!r}")
    if any(char.isspace() for char in text):
        raise SourceRegistryError(f"{name} must not contain whitespace: {text!r}")
    if len(text) < len("https://") + 3:
        raise SourceRegistryError(f"{name} is too short to be a URL: {text!r}")
    return text


# ------------------------------------------------------------------------------------------ schema
def validate_source(source: Mapping, *, index: int = 0) -> dict:
    """Validate one source record and return a normalized copy (fail-closed)."""
    if not isinstance(source, Mapping):
        raise SourceRegistryError(f"sources[{index}] must be an object")
    missing = [field for field in REQUIRED_FIELDS if field not in source]
    if missing:
        raise SourceRegistryError(f"sources[{index}] is missing fields: {missing}")
    source_id = _text(source["source_id"], f"sources[{index}].source_id")
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise SourceRegistryError(
            f"sources[{index}].source_id must match {SOURCE_ID_RE.pattern}, got {source_id!r}")
    status = _text(source["status"], f"sources[{index}].status")
    if status not in STATUSES:
        raise SourceRegistryError(f"sources[{index}].status must be one of {STATUSES}")
    access = _text(source["access"], f"sources[{index}].access")
    if access not in ACCESS:
        raise SourceRegistryError(f"sources[{index}].access must be one of {ACCESS}")
    vintage = _text(source["vintage_support"], f"sources[{index}].vintage_support")
    if vintage not in VINTAGE_SUPPORT:
        raise SourceRegistryError(
            f"sources[{index}].vintage_support must be one of {VINTAGE_SUPPORT}")
    retrieved_at = _date(source["retrieved_at"], f"sources[{index}].retrieved_at", optional=True)
    notes = str(source.get("notes") or "").strip()
    license_text = _text(source["license"], f"sources[{index}].license")
    record = {
        "source_id": source_id,
        "name": _text(source["name"], f"sources[{index}].name"),
        "publisher": _text(source["publisher"], f"sources[{index}].publisher"),
        "url": _url(source["url"], f"sources[{index}].url"),
        "license": license_text,
        "access": access,
        "coverage": _coverage(source["coverage"], f"sources[{index}].coverage"),
        "fields": _string_list(source["fields"], f"sources[{index}].fields"),
        "retrieved_at": retrieved_at,
        "vintage_support": vintage,
        "status": status,
        "notes": notes,
    }
    if status == "VERIFIED":
        if retrieved_at is None:
            raise SourceRegistryError(
                f"sources[{index}] ({source_id}) is VERIFIED but has no retrieved_at: a source "
                "cannot be verified without evidence of retrieval")
        if not notes:
            raise SourceRegistryError(
                f"sources[{index}] ({source_id}) is VERIFIED but has no notes: the evidence must be "
                "named (dataset/manifest/docs)")
        if license_text.lower().startswith("unknown"):
            raise SourceRegistryError(
                f"sources[{index}] ({source_id}) cannot be VERIFIED with an unknown licence")
    return record


def validate_document(document: Mapping) -> dict:
    """Validate the whole registry document and return it normalized (fail-closed)."""
    if not isinstance(document, Mapping):
        raise SourceRegistryError("registry document must be an object")
    if document.get("schema") != SCHEMA:
        raise SourceRegistryError(
            f"registry schema must be {SCHEMA!r}, got {document.get('schema')!r}")
    _date(document.get("date"), "date")
    _text(document.get("intended_use"), "intended_use")
    statuses = document.get("statuses")
    if not isinstance(statuses, Mapping):
        raise SourceRegistryError("statuses must be an object with a definition per status")
    if set(statuses) != set(STATUSES):
        raise SourceRegistryError(f"statuses must define exactly {STATUSES}")
    for status, definition in statuses.items():
        _text(definition, f"statuses[{status}]")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceRegistryError("sources must be a non-empty list")
    sources = []
    seen_ids: set = set()
    seen_urls: dict = {}
    for index, source in enumerate(raw_sources):
        record = validate_source(source, index=index)
        if record["source_id"] in seen_ids:
            raise SourceRegistryError(f"duplicate source_id {record['source_id']!r}")
        seen_ids.add(record["source_id"])
        if record["url"] in seen_urls:
            raise SourceRegistryError(
                f"duplicate url {record['url']!r} used by {seen_urls[record['url']]!r} and "
                f"{record['source_id']!r}")
        seen_urls[record["url"]] = record["source_id"]
        sources.append(record)
    normalized = {
        "schema": SCHEMA,
        "date": document["date"],
        "intended_use": str(document["intended_use"]).strip(),
        "statuses": {status: str(statuses[status]).strip() for status in STATUSES},
        "sources": sorted(sources, key=lambda item: item["source_id"]),
    }
    return normalized


def load(path: Path) -> dict:
    """Load and validate the registry from disk (no network)."""
    path = Path(path)
    if not path.is_file():
        raise SourceRegistryError(f"source registry not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceRegistryError(f"cannot read source registry {path}: {exc!r}") from exc
    return validate_document(document)


def validate_file(path: Path) -> dict:
    """Validate and return a summary report instead of raising (audit view)."""
    path = Path(path)
    try:
        document = load(path)
    except SourceRegistryError as exc:
        return {"valid": False, "path": str(path), "sources": 0, "errors": [str(exc)]}
    status_counts = {status: 0 for status in STATUSES}
    access_counts = {access: 0 for access in ACCESS}
    for source in document["sources"]:
        status_counts[source["status"]] += 1
        access_counts[source["access"]] += 1
    return {"valid": True, "path": str(path), "sources": len(document["sources"]),
            "errors": [], "status_counts": status_counts, "access_counts": access_counts,
            "verified": [source["source_id"] for source in document["sources"]
                         if source["status"] == "VERIFIED"]}


# ------------------------------------------------------------------------------------------ views
def sources(document: Mapping | Path) -> list[dict]:
    document = load(document) if isinstance(document, (str, Path)) else validate_document(document)
    return list(document["sources"])


def get(document: Mapping | Path, source_id: str) -> dict:
    for source in sources(document):
        if source["source_id"] == source_id:
            return source
    raise SourceRegistryError(f"unknown source_id {source_id!r}")


def find(document: Mapping | Path, *, status: str | None = None, access: str | None = None,
         query: str | None = None) -> list[dict]:
    """Filter sources; deterministic order by source_id."""
    if status is not None and status not in STATUSES:
        raise SourceRegistryError(f"unknown status filter {status!r}")
    if access is not None and access not in ACCESS:
        raise SourceRegistryError(f"unknown access filter {access!r}")
    needle = str(query).lower().strip() if query else None
    results = []
    for source in sources(document):
        if status is not None and source["status"] != status:
            continue
        if access is not None and source["access"] != access:
            continue
        if needle is not None:
            haystack = " ".join([source["source_id"], source["name"], source["publisher"],
                                 source["license"], source["notes"],
                                 " ".join(source["fields"])]).lower()
            if needle not in haystack:
                continue
        results.append(source)
    return results


def counts(document: Mapping | Path) -> dict:
    source_list = sources(document)
    status_counts = {status: 0 for status in STATUSES}
    access_counts = {access: 0 for access in ACCESS}
    license_counts = {name: 0 for name in LICENSE_CLASSES}
    for source in source_list:
        status_counts[source["status"]] += 1
        access_counts[source["access"]] += 1
        license_counts[license_class(source["license"])] += 1
    return {"sources": len(source_list), "by_status": status_counts, "by_access": access_counts,
            "by_license_class": license_counts}


def missing(document: Mapping | Path) -> dict:
    """What is not usable yet: non-VERIFIED sources and their blocking reason."""
    source_list = sources(document)
    pending = [{"source_id": source["source_id"], "status": source["status"],
                "access": source["access"],
                "reason": ("catalogued only, no data retrieved" if source["status"] == "CATALOGUED"
                           else "paid/licensed or manual source, no data retrieved")}
               for source in source_list if source["status"] != "VERIFIED"]
    return {"pending": pending, "n_pending": len(pending),
            "verified": [source["source_id"] for source in source_list
                         if source["status"] == "VERIFIED"]}


def export_for_api(document: Mapping | Path, *, source_path: str | None = None) -> dict:
    """Deterministic API export: sorted sources, no paths, counts by status/access."""
    document = load(document) if isinstance(document, (str, Path)) else validate_document(document)
    modules = []
    for source in document["sources"]:
        modules.append({
            "source_id": source["source_id"], "name": source["name"],
            "publisher": source["publisher"], "url": source["url"],
            "license": source["license"], "license_class": license_class(source["license"]),
            "access": source["access"], "coverage": dict(source["coverage"]),
            "fields": list(source["fields"]), "retrieved_at": source["retrieved_at"],
            "vintage_support": source["vintage_support"], "status": source["status"],
            "notes": source["notes"],
        })
    report = {"schema": EXPORT_SCHEMA, "intended_use": document["intended_use"],
              "sources": modules, "counts": counts(document)}
    if source_path is not None:
        report["source"] = str(source_path)
    return report


# -------------------------------------------------------------------------------------- licences
def license_class(license_text: str) -> str:
    """Coarse, documented classifier of licence strings (used for filtering/export only)."""
    text = str(license_text).lower()
    if not text.strip() or "unknown" in text:
        return "unknown"
    if "public domain" in text:
        return "public_domain"
    if "cc by-nc" in text or "non-commercial" in text or "noncommercial" in text or "nC" in text:
        return "non_commercial"
    if "commercial" in text or "subscription" in text or "licence" in text and "require" in text:
        return "commercial"
    if "cc by" in text or "attribution" in text or "terms of use" in text or "copyright" in text:
        return "open_attribution"
    return "unknown"


# ---------------------------------------------------------------------------------- assumptions
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
ASSUMPTION_MARKER = "ASSUMPTION"


def _tokens(text: str) -> set:
    return {token.lower() for token in TOKEN_RE.findall(str(text)) if len(token) > 1}


def resolve_assumption_source(reference, document: Mapping | Path) -> dict:
    """Resolve one assumption reference to a source_id or to the explicit ASSUMPTION marker.

    The function is deliberately non-committal: it never turns an assumption into observed data.
    ``kind`` is ``ASSUMPTION`` when the reference says so explicitly (``source_class="assumption"``
    or the text contains ``ASSUMPTION``), ``SOURCE`` when a source_id appears verbatim, otherwise
    ``UNKNOWN`` with token-based ``suggestions`` (source ids only, no automatic choice).
    """
    source_list = sources(document)
    ref = str(reference if reference is not None else "").strip()
    known = {source["source_id"]: source for source in source_list}
    explicit = ASSUMPTION_MARKER.lower() in ref.lower() or not ref
    tokens = _tokens(ref)
    direct = None
    for source_id in known:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(source_id)}(?![A-Za-z0-9_])", ref):
            direct = source_id
            break
    if direct is not None:
        return {"ref": ref, "kind": "SOURCE", "source_id": direct, "suggestions": [],
                "resolved": True, "explicit_assumption": explicit}
    suggestions = []
    for source_id, source in sorted(known.items()):
        haystack = _tokens(source_id) | _tokens(source["name"]) | _tokens(source["publisher"])
        if tokens & haystack:
            suggestions.append(source_id)
    if explicit:
        kind = "ASSUMPTION"
    elif suggestions:
        kind = "UNKNOWN"
    else:
        kind = "UNKNOWN"
    return {"ref": ref, "kind": kind, "source_id": None, "suggestions": suggestions[:5],
            "resolved": kind == "ASSUMPTION", "explicit_assumption": explicit}


def build_template(source_id: str = "NEW_SOURCE") -> dict:
    """An empty, valid template for a new source record (status CATALOGUED, no retrieval)."""
    template = {
        "source_id": _text(source_id, "source_id"),
        "name": "Full source name",
        "publisher": "Publisher / institution",
        "url": "https://example.org/data",
        "license": "licence terms (public domain / CC BY 4.0 / subscription ...)",
        "access": "api",
        "coverage": {"geography": "what geography", "period": "what period",
                     "frequency": "monthly / annual"},
        "fields": ["field one", "field two"],
        "retrieved_at": None,
        "vintage_support": "none",
        "status": "CATALOGUED",
        "notes": "",
    }
    return validate_source(template)


__all__ = ["ACCESS", "ASSUMPTION_MARKER", "COVERAGE_KEYS", "EXPORT_SCHEMA", "LICENSE_CLASSES",
           "REQUIRED_FIELDS", "SCHEMA", "SOURCE_ID_RE", "STATUSES", "SourceRegistryError",
           "VINTAGE_SUPPORT", "build_template", "counts", "export_for_api", "find", "get",
           "license_class", "load", "missing", "resolve_assumption_source", "sources",
           "validate_document", "validate_file", "validate_source"]
