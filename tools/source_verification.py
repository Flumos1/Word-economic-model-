"""Source-verification docket (task: licence-auditor, docs/205): offline, fail-closed, no promotion.

The tool reads two local registries and builds a *docket* — a read-only audit view, not a new
source of truth:

* ``protocols/source_registry.json`` — what was catalogued/verified and with what evidence
  (URL, licence text, access, retrieval date, notes; validated by
  ``src/amfe/data/source_registry.py``);
* ``protocols/licence_register.json`` — the append-only journal of terms: status
  (OPEN/RESTRICTED/UNVERIFIED), level, conditions, terms SHA-256.

The docket contains total/VERIFIED/PARTIAL/UNVERIFIED counts, per-source evidence and the exact
items missing before a source may be promoted to VERIFIED (retrieval date, dataset SHA-256,
licence-journal entry, terms hash/conditions), a licence-compatibility matrix over
OPEN/ATTRIBUTION/RESTRICTED/UNKNOWN and the list of gaps. It never downloads anything, never
calls the network and never changes a status.

Hard rules
----------
* **No network.** No networking library is imported; the tool works on local files only.
* **No auto-promotion and no auto-demotion.** Registry statuses are read-only inputs. A docket
  verdict (VERIFIED/PARTIAL/UNVERIFIED) is a derived evidence label, not a registry status. Only
  a human may change a status after the data were ingested and the evidence recorded.
* **Fail-closed.** An unreadable or invalid registry, an unknown status/level, a duplicate id, a
  VERIFIED record without retrieval evidence or an output path equal to an input raises
  :class:`SourceVerificationError` (CLI exit 2). Integrity violations inside the docket are
  reported and make ``--check`` exit 1; no output file is written when the check fails.

CLI
---
``python tools/source_verification.py``                     summary (RU), exit 0/1
``python tools/source_verification.py --check``             docket + invariants, exit 0/1, 2 on bad input
``python tools/source_verification.py --json``              machine-readable docket on stdout
``python tools/source_verification.py --out build/docket.json``  write the docket (atomic, refuses inputs)
``--registry PATH --licences PATH``                         override the input files (tests/CI)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Mapping

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:  # reuse the registry validator instead of duplicating it
    sys.path.insert(0, str(_SRC))

try:  # pragma: no cover - import error is reported through SourceVerificationError
    from amfe.data import source_registry as _sr
except ImportError:  # pragma: no cover
    _sr = None

SCHEMA = "AMFE-SOURCE-DOCKET/1.0.0"
TOOL_VERSION = "1.0.0"
REGISTRY_SCHEMA = "AMFE-SOURCE-REGISTRY/1.0.0"
LICENCE_REGISTER_ID = "AMFE-LICENCE-REGISTER"
DOCKET_VERDICTS = ("VERIFIED", "PARTIAL", "UNVERIFIED")
LICENCE_CLASSES = ("OPEN", "ATTRIBUTION", "RESTRICTED", "UNKNOWN")
MATRIX_VERDICTS = ("OK", "ATTRIBUTION_REQUIRED", "BLOCKED", "BLOCKED_UNKNOWN")
LICENCE_STATUSES = ("OPEN", "RESTRICTED", "UNVERIFIED")
LICENCE_LEVELS = ("full", "extraction", "none")
# Evidence required before a *human* may promote a source to VERIFIED (docs/162 §7, docs/205).
PROMOTION_ITEMS = ("url", "retrieved_at", "dataset_hash", "licence", "licence_entry",
                   "terms_evidence")
ITEM_ORDER = PROMOTION_ITEMS
# Journal entries that cover a source-registry id under another name (explicit, never guessed).
SOURCE_ALIASES = {"BLS": "BLS_PRIMARY", "FRED_ALFRED": "FRED_ALFRED_SERVICES"}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# "sha256 0d17...a79" (full) or "sha256 c3ac04b6...b80673d" (truncated reference).
NOTES_HASH_RE = re.compile(r"sha-?256[^0-9a-f]{0,24}([0-9a-f]{8,64})", re.IGNORECASE)
ATTRIBUTION_MARKERS = ("attribution", "citation", "cite", "credit", "acknowledge")
NON_COMMERCIAL_RE = re.compile(r"\bnon[- ]?commercial\b|\bnc\b|research use only", re.IGNORECASE)
SHARE_ALIKE_RE = re.compile(r"share[- ]?alike|\bsa\b|\bsa-(?:nc|by)\b", re.IGNORECASE)


class SourceVerificationError(ValueError):
    """Invalid input or a refused operation (fail-closed; CLI exit code 2)."""


# --------------------------------------------------------------------------------------------- io
def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SourceVerificationError(f"cannot read {path}: {exc!r}") from exc


def _load_json(path: Path, what: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise SourceVerificationError(f"{what} not found: {path}")
    raw = _read_bytes(path)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceVerificationError(f"{what} is not valid JSON ({path}): {exc!r}") from exc
    if not isinstance(document, dict):
        raise SourceVerificationError(f"{what} must be a JSON object: {path}")
    return document


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceVerificationError(f"{name} must be a non-empty string")
    return value.strip()


def _date(value, name: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise SourceVerificationError(f"{name} must be an ISO date, got null")
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        raise SourceVerificationError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    if not (1 <= int(value[5:7]) <= 12 and 1 <= int(value[8:10]) <= 31):
        raise SourceVerificationError(f"{name} is not a valid date: {value!r}")
    return value


def load_source_registry(path: Path) -> dict:
    """Load and validate the source registry (delegates to the project validator)."""
    if _sr is None:  # pragma: no cover
        raise SourceVerificationError("cannot import amfe.data.source_registry (src/ missing)")
    document = _load_json(path, "source registry")
    try:
        return _sr.validate_document(document)
    except _sr.SourceRegistryError as exc:
        raise SourceVerificationError(f"source registry is invalid: {exc}") from exc


def load_licence_register(path: Path) -> dict:
    """Load and validate the terms journal fail-closed (no silent defaults)."""
    document = _load_json(path, "licence register")
    if document.get("register_id") != LICENCE_REGISTER_ID:
        raise SourceVerificationError(
            f"licence register must have register_id={LICENCE_REGISTER_ID!r}, "
            f"got {document.get('register_id')!r}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise SourceVerificationError(
            "licence register entries must be a list (an empty journal is allowed: it means no "
            "terms were recorded, so every source lacks terms evidence)")
    seen: set = set()
    normalized = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise SourceVerificationError(f"entries[{index}] must be an object")
        entry_id = _text(entry.get("id"), f"entries[{index}].id")
        if entry_id in seen:
            raise SourceVerificationError(f"duplicate licence entry id {entry_id!r}")
        seen.add(entry_id)
        status = _text(entry.get("status"), f"entries[{index}] ({entry_id}).status")
        if status not in LICENCE_STATUSES:
            raise SourceVerificationError(
                f"entries[{index}] ({entry_id}).status must be one of {LICENCE_STATUSES}, "
                f"got {status!r}")
        level = _text(entry.get("level"), f"entries[{index}] ({entry_id}).level")
        if level not in LICENCE_LEVELS:
            raise SourceVerificationError(
                f"entries[{index}] ({entry_id}).level must be one of {LICENCE_LEVELS}, "
                f"got {level!r}")
        date = _date(entry.get("date"), f"entries[{index}] ({entry_id}).date")
        conditions = entry.get("conditions", [])
        if not isinstance(conditions, list) or any(
                not isinstance(item, str) or not item.strip() for item in conditions):
            raise SourceVerificationError(
                f"entries[{index}] ({entry_id}).conditions must be a list of non-empty strings")
        terms_sha = entry.get("terms_sha256")
        if terms_sha is not None:
            if not isinstance(terms_sha, str) or not SHA256_RE.fullmatch(terms_sha):
                raise SourceVerificationError(
                    f"entries[{index}] ({entry_id}).terms_sha256 must be 64 lowercase hex chars")
        normalized.append({
            "id": entry_id,
            "status": status,
            "level": level,
            "date": date,
            "conditions": [item.strip() for item in conditions],
            "terms_sha256": terms_sha,
            "last_reviewed": _date(entry.get("last_reviewed"),
                                   f"entries[{index}] ({entry_id}).last_reviewed",
                                   optional=True),
        })
    return {"register_id": LICENCE_REGISTER_ID,
            "entries": sorted(normalized, key=lambda item: item["id"])}


def write_report(report: Mapping, path: Path, *, inputs=()) -> Path:
    """Write the docket atomically; never overwrite an input registry (fail-closed)."""
    target = Path(path)
    resolved = target.resolve()
    for item in inputs:
        if Path(item).resolve() == resolved:
            raise SourceVerificationError(
                f"refusing to overwrite an input registry: {target} (the docket is read-only)")
    if not target.parent.is_dir():
        raise SourceVerificationError(f"output directory does not exist: {target.parent}")
    payload = dumps(report).encode("utf-8")
    handle = None
    temp_name = None
    try:
        handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                             dir=str(target.parent))
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temp_name, target)
        temp_name = None
    except OSError as exc:
        raise SourceVerificationError(f"cannot write {target}: {exc!r}") from exc
    finally:
        if handle is not None and temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return target


# --------------------------------------------------------------------------------------- licences
def licence_entry_for(source_id: str, entries) -> dict:
    """Resolve a source id to a journal entry: exact, explicit alias, or unique prefix.

    Ambiguity is reported, never resolved silently (fail-closed): two candidates give no entry.
    """
    by_id = {entry["id"]: entry for entry in entries}
    if source_id in by_id:
        return {"entry": by_id[source_id], "matched_by": "exact", "ambiguous": []}
    alias = SOURCE_ALIASES.get(source_id)
    if alias and alias in by_id:
        return {"entry": by_id[alias], "matched_by": f"alias:{alias}", "ambiguous": []}
    prefixed = sorted(entry_id for entry_id in by_id
                      if entry_id.startswith(source_id + "_") or entry_id.startswith(source_id + ":"))
    if len(prefixed) == 1:
        return {"entry": by_id[prefixed[0]], "matched_by": f"prefix:{prefixed[0]}",
                "ambiguous": []}
    if len(prefixed) > 1:
        return {"entry": None, "matched_by": "ambiguous", "ambiguous": prefixed}
    return {"entry": None, "matched_by": "none", "ambiguous": []}


def _base_licence_class(license_text: str) -> str:
    """Map the coarse registry class (src/amfe/data/source_registry.py) to the docket class."""
    if _sr is None:  # pragma: no cover
        coarse = "unknown"
    else:
        coarse = _sr.license_class(license_text)
    return {"public_domain": "OPEN", "open_attribution": "ATTRIBUTION",
            "non_commercial": "ATTRIBUTION", "commercial": "RESTRICTED",
            "unknown": "UNKNOWN"}[coarse]


def classify_licence(source: Mapping, entry: Mapping | None) -> dict:
    """Derive the four-class docket label; the journal can only make the label stricter.

    A RESTRICTED/UNVERIFIED journal entry overrides the registry text (the journal is the
    authority on terms); an OPEN entry with an attribution/citation condition upgrades a
    public-domain label to ATTRIBUTION. No rule ever relaxes a RESTRICTED source.
    """
    text = str(source.get("license") or "")
    lowered = text.lower()
    base = _base_licence_class(text)
    label = base
    basis = "registry_heuristic"
    if entry is not None:
        basis = f"licence_register:{entry['id']}"
        conditions = " ".join(entry["conditions"]).lower()
        if entry["status"] == "RESTRICTED":
            label = "RESTRICTED"
        elif entry["status"] == "UNVERIFIED":
            label = "UNKNOWN"
        elif any(marker in conditions for marker in ATTRIBUTION_MARKERS):
            if label == "OPEN":
                label = "ATTRIBUTION"
        if base == "RESTRICTED":  # never downgrade a restricted licence text
            label = "RESTRICTED"
    share_alike = bool(SHARE_ALIKE_RE.search(lowered))
    non_commercial = bool(NON_COMMERCIAL_RE.search(lowered))
    if entry is not None:
        conditions = " ".join(entry["conditions"]).lower()
        non_commercial = non_commercial or bool(NON_COMMERCIAL_RE.search(conditions))
        share_alike = share_alike or bool(SHARE_ALIKE_RE.search(conditions))
    return {"class": label, "basis": basis, "registry_class": base, "text": text,
            "attribution_required": label == "ATTRIBUTION",
            "non_commercial": non_commercial, "share_alike": share_alike}


def combine_licences(left: str, right: str) -> str:
    """Compatibility of two licence classes for a joint derived artefact (fail-closed)."""
    for name in (left, right):
        if name not in LICENCE_CLASSES:
            raise SourceVerificationError(f"unknown licence class {name!r}")
    if "UNKNOWN" in (left, right):
        return "BLOCKED_UNKNOWN"
    if "RESTRICTED" in (left, right):
        return "BLOCKED"
    if "ATTRIBUTION" in (left, right):
        return "ATTRIBUTION_REQUIRED"
    return "OK"


def compatibility_matrix() -> dict:
    """Deterministic 4x4 matrix: class -> class -> verdict."""
    return {"classes": list(LICENCE_CLASSES),
            "verdicts": list(MATRIX_VERDICTS),
            "matrix": {left: {right: combine_licences(left, right) for right in LICENCE_CLASSES}
                       for left in LICENCE_CLASSES}}


# ---------------------------------------------------------------------------------------- docket
def hash_evidence(notes: str) -> dict:
    """Detect a SHA-256 reference in the notes: 'full' (64 hex), 'truncated' or 'absent'."""
    match = NOTES_HASH_RE.search(notes or "")
    if not match:
        return {"state": "absent", "value": None}
    value = match.group(1)
    return {"state": "full" if len(value) == 64 else "truncated", "value": value}


def assess_source(source: Mapping, entries) -> dict:
    """Build one evidence record: checks, missing promotion items, warnings, docket verdict."""
    source_id = source["source_id"]
    resolved = licence_entry_for(source_id, entries)
    entry = resolved["entry"]
    licence = classify_licence(source, entry)
    retrieved_at = source["retrieved_at"]
    notes = str(source.get("notes") or "")
    hash_info = hash_evidence(notes) if retrieved_at else {"state": "absent", "value": None}
    checks = {
        "url": str(source["url"]).startswith("https://"),
        "retrieved_at": retrieved_at is not None,
        "dataset_hash": retrieved_at is not None and hash_info["state"] in ("full", "truncated"),
        "licence": licence["class"] != "UNKNOWN",
        "licence_entry": entry is not None and entry["status"] == "OPEN",
        "terms_evidence": bool(entry is not None and entry.get("terms_sha256")
                               and entry["conditions"]),
    }
    unmet = [item for item in ITEM_ORDER if not checks[item]]
    status = source["status"]
    if status == "VERIFIED":
        verdict = "VERIFIED" if checks["retrieved_at"] else "UNVERIFIED"
    elif status == "ACCESS_VERIFIED":
        verdict = "PARTIAL"
    elif status == "NOT_DOWNLOADED" or source["access"] == "paid" or not checks["licence"]:
        verdict = "UNVERIFIED"
    elif checks["licence_entry"] and checks["terms_evidence"]:
        # The terms were read and hashed in the journal, but no data were retrieved: the source
        # is closer to VERIFIED than a bare catalogue record, yet it cannot be cited.
        verdict = "PARTIAL"
    else:
        verdict = "UNVERIFIED"
    warnings = []
    if status == "VERIFIED" and unmet:
        warnings.append("VERIFIED, но evidence неполный: " + ", ".join(unmet))
    if hash_info["state"] == "truncated":
        warnings.append("SHA-256 в notes усечён (не 64 hex): нужен полный хеш датасета")
    if entry is not None and entry["status"] != "OPEN":
        warnings.append(f"журнал: запись {entry['id']} имеет статус {entry['status']}")
    if resolved["ambiguous"]:
        warnings.append("несколько записей журнала подходят: " + ", ".join(resolved["ambiguous"]))
    if verdict == "UNVERIFIED" and source["access"] == "paid":
        warnings.append("платный доступ: нужна лицензия/решение владельца, затем загрузка")
    if licence["class"] == "RESTRICTED" and verdict == "VERIFIED":
        warnings.append("RESTRICTED-лицензия при статусе VERIFIED: противоречие, нужна ревизия")
    return {
        "source_id": source_id,
        "name": source["name"],
        "publisher": source["publisher"],
        "url": source["url"],
        "access": source["access"],
        "registry_status": status,
        "docket_verdict": verdict,
        "citation_allowed": verdict == "VERIFIED",
        "licence": {"class": licence["class"], "basis": licence["basis"],
                    "registry_class": licence["registry_class"],
                    "attribution_required": licence["attribution_required"],
                    "non_commercial": licence["non_commercial"],
                    "share_alike": licence["share_alike"], "text": licence["text"]},
        "licence_entry": {"id": entry["id"] if entry else None,
                          "status": entry["status"] if entry else None,
                          "matched_by": resolved["matched_by"],
                          "terms_sha256": bool(entry and entry.get("terms_sha256"))},
        "evidence": dict(checks),
        "dataset_hash": hash_info,
        "missing": [] if verdict == "VERIFIED" else unmet,
        "weak_evidence": unmet if verdict == "VERIFIED" else [],
        "warnings": warnings,
    }


def validate_invariants(report: Mapping) -> list[str]:
    """Internal consistency of the docket; a non-empty list means 'do not publish this docket'."""
    violations = []
    records = report["sources"]
    counts = report["counts"]
    if counts["total"] != len(records):
        violations.append(f"counts.total={counts['total']} != {len(records)} sources")
    by_verdict = counts["by_docket_verdict"]
    for verdict in DOCKET_VERDICTS:
        expected = sum(1 for record in records if record["docket_verdict"] == verdict)
        if by_verdict.get(verdict, 0) != expected:
            violations.append(f"counts[{verdict}]={by_verdict.get(verdict)} != {expected}")
    if sum(by_verdict.values()) != len(records):
        violations.append("docket verdict buckets do not sum to the number of sources")
    for record in records:
        source_id = record["source_id"]
        if record["docket_verdict"] not in DOCKET_VERDICTS:
            violations.append(f"{source_id}: unknown docket verdict {record['docket_verdict']!r}")
        if record["docket_verdict"] == "VERIFIED" and record["registry_status"] != "VERIFIED":
            violations.append(f"{source_id}: docket VERIFIED without registry VERIFIED (promotion)")
        if record["registry_status"] == "VERIFIED" and record["docket_verdict"] != "VERIFIED":
            violations.append(f"{source_id}: registry VERIFIED demoted to {record['docket_verdict']}")
        if record["citation_allowed"] != (record["docket_verdict"] == "VERIFIED"):
            violations.append(f"{source_id}: citation_allowed contradicts the docket verdict")
        if record["docket_verdict"] == "VERIFIED" and record["missing"]:
            violations.append(f"{source_id}: VERIFIED with blocking missing items")
        if record["docket_verdict"] == "PARTIAL" and record["access"] == "paid":
            violations.append(f"{source_id}: paid source cannot be PARTIAL")
        if record["licence"]["class"] not in LICENCE_CLASSES:
            violations.append(f"{source_id}: unknown licence class {record['licence']['class']!r}")
    matrix = report["licence_matrix"]
    for left in LICENCE_CLASSES:
        for right in LICENCE_CLASSES:
            cell = matrix["matrix"].get(left, {}).get(right)
            if cell not in MATRIX_VERDICTS:
                violations.append(f"licence matrix [{left}][{right}]={cell!r}")
    if len(report["gaps"]) != len(records) - by_verdict.get("VERIFIED", 0):
        violations.append("gaps do not match the number of non-VERIFIED sources")
    return violations


def build_docket(registry_path: Path, licences_path: Path) -> dict:
    """Build the complete docket; raises SourceVerificationError on invalid input."""
    registry_path = Path(registry_path)
    licences_path = Path(licences_path)
    inputs = {"source_registry": registry_path, "licence_register": licences_path}
    before = {name: _read_bytes(path) for name, path in inputs.items()}
    source_document = load_source_registry(registry_path)
    licence_document = load_licence_register(licences_path)
    entries = licence_document["entries"]
    records = [assess_source(source, entries) for source in source_document["sources"]]
    records.sort(key=lambda item: item["source_id"])
    by_verdict = {verdict: 0 for verdict in DOCKET_VERDICTS}
    for record in records:
        by_verdict[record["docket_verdict"]] += 1
    by_status = {status: 0 for status in _sr.STATUSES} if _sr else {}
    for record in records:
        by_status[record["registry_status"]] = by_status.get(record["registry_status"], 0) + 1
    by_class = {name: 0 for name in LICENCE_CLASSES}
    for record in records:
        by_class[record["licence"]["class"]] += 1
    journal_by_status = {status: 0 for status in LICENCE_STATUSES}
    journal_by_class = {name: 0 for name in LICENCE_CLASSES}
    for entry in entries:
        journal_by_status[entry["status"]] += 1
        journal_by_class[_journal_class(entry)] += 1
    mapped = [record["source_id"] for record in records if record["licence_entry"]["id"]]
    ambiguous = [record["source_id"] for record in records
                 if record["licence_entry"]["matched_by"] == "ambiguous"]
    after = {name: _read_bytes(path) for name, path in inputs.items()}
    inputs_unchanged = before == after
    report = {
        "schema": SCHEMA,
        "tool_version": TOOL_VERSION,
        "as_of": source_document["date"],
        "inputs": {"source_registry": str(registry_path),
                   "licence_register": str(licences_path)},
        "sources": records,
        "counts": {"total": len(records), "by_docket_verdict": by_verdict,
                   "by_registry_status": by_status, "by_licence_class": by_class},
        "verified": [record["source_id"] for record in records
                     if record["docket_verdict"] == "VERIFIED"],
        "partial": [record["source_id"] for record in records
                    if record["docket_verdict"] == "PARTIAL"],
        "unverified": [record["source_id"] for record in records
                       if record["docket_verdict"] == "UNVERIFIED"],
        "gaps": [{"source_id": record["source_id"],
                  "registry_status": record["registry_status"],
                  "access": record["access"],
                  "licence_class": record["licence"]["class"],
                  "missing": list(record["missing"])}
                 for record in records if record["docket_verdict"] != "VERIFIED"],
        "licence_matrix": {
            **compatibility_matrix(),
            "class_flags": {
                "non_commercial": [record["source_id"] for record in records
                                   if record["licence"]["non_commercial"]],
                "share_alike": [record["source_id"] for record in records
                                if record["licence"]["share_alike"]],
            },
            "publication_rule": {
                "derived_statistics_only": True,
                "allowed_classes": ["OPEN", "ATTRIBUTION"],
                "blocked_classes": ["RESTRICTED", "UNKNOWN"],
                "note": ("Derived statistics of OPEN/ATTRIBUTION sources may be published with "
                         "attribution; RESTRICTED/UNKNOWN sources are never published or cited. "
                         "Non-commercial and share-alike flags are checked separately."),
            },
        },
        "licence_journal": {
            "entries": len(entries),
            "by_status": journal_by_status,
            "by_class": journal_by_class,
            "mapped_to_registry": mapped,
            "unmapped_entries": sorted(entry["id"] for entry in entries
                                       if entry["id"] not in _mapped_entry_ids(records)),
            "ambiguous_sources": ambiguous,
        },
        "invariants": {"no_network": True, "auto_promotion": False, "registry_writes": False,
                       "inputs_unchanged": inputs_unchanged},
    }
    violations = validate_invariants(report)
    if not inputs_unchanged:
        violations.append("input registries changed during the build (writes are forbidden)")
    report["violations"] = violations
    report["ok"] = not violations
    return report


def _mapped_entry_ids(records) -> set:
    """Journal entries actually referenced by a source record (for the unmapped list)."""
    return {record["licence_entry"]["id"] for record in records
            if record["licence_entry"]["id"]}


def _journal_class(entry: Mapping) -> str:
    if entry["status"] == "RESTRICTED":
        return "RESTRICTED"
    if entry["status"] == "UNVERIFIED":
        return "UNKNOWN"
    conditions = " ".join(entry["conditions"]).lower()
    return "ATTRIBUTION" if any(marker in conditions for marker in ATTRIBUTION_MARKERS) else "OPEN"


def dumps(report: Mapping) -> str:
    """Canonical JSON: sorted keys, stable separators, trailing newline (deterministic)."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


# ---------------------------------------------------------------------------------------- render
def _fmt_list(items) -> str:
    return ", ".join(items) if items else "—"


def render_summary(report: Mapping) -> str:
    """Human-readable RU summary of the docket (the JSON stays the machine-readable view)."""
    counts = report["counts"]
    verdicts = counts["by_docket_verdict"]
    statuses = counts["by_registry_status"]
    classes = counts["by_licence_class"]
    lines = [
        "AMFE · docket верификации источников (offline, без сети)",
        f"Дата реестра: {report['as_of']} · всего источников: {counts['total']}",
        f"Вердикт docket: VERIFIED {verdicts['VERIFIED']} · PARTIAL {verdicts['PARTIAL']} · "
        f"UNVERIFIED {verdicts['UNVERIFIED']}",
        "Статусы реестра: " + " · ".join(f"{name} {value}"
                                         for name, value in statuses.items()),
        "Лицензии: " + " · ".join(f"{name} {value}" for name, value in classes.items()),
        f"Цитировать как наблюдения можно: {_fmt_list(report['verified'])}",
        f"PARTIAL (нельзя цитировать как наблюдения): {_fmt_list(report['partial'])}",
        "Пробелы до VERIFIED:",
    ]
    for gap in report["gaps"]:
        lines.append(f"  {gap['source_id']} [{gap['registry_status']}, {gap['licence_class']}]: "
                     f"нет {', '.join(gap['missing']) if gap['missing'] else '—'}")
    weak = [(record["source_id"], record["weak_evidence"]) for record in report["sources"]
            if record["weak_evidence"]]
    if weak:
        lines.append("Слабый evidence у VERIFIED (статус не понижается, пробел фиксируется):")
        for source_id, items in weak:
            lines.append(f"  {source_id}: {', '.join(items)}")
    matrix = report["licence_matrix"]
    lines.append("Матрица совместимости лицензий:")
    for left in LICENCE_CLASSES:
        row = ", ".join(f"{right}={matrix['matrix'][left][right]}" for right in LICENCE_CLASSES)
        lines.append(f"  {left}: {row}")
    journal = report["licence_journal"]
    lines.append(f"Журнал условий: {journal['entries']} записей, "
                 f"{len(journal['mapped_to_registry'])} привязаны к реестру, "
                 f"{len(journal['unmapped_entries'])} без привязки")
    lines.append("Границы: метаданные ≠ юридическая проверка; статусы реестра не меняются.")
    if report["violations"]:
        lines.append("НАРУШЕНИЯ ЦЕЛОСТНОСТИ:")
        lines.extend(f"  - {item}" for item in report["violations"])
    else:
        lines.append("Нарушения целостности: нет")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline source-verification docket: evidence, licence matrix, gaps (no network).")
    parser.add_argument("--check", action="store_true",
                        help="check the docket invariants; exit 1 on violations, 2 on bad input")
    parser.add_argument("--json", action="store_true", help="print the machine-readable docket")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="write the canonical JSON docket to PATH (never an input registry)")
    parser.add_argument("--registry", default=str(_ROOT / "protocols" / "source_registry.json"),
                        help="source registry path")
    parser.add_argument("--licences", default=str(_ROOT / "protocols" / "licence_register.json"),
                        help="licence register path")
    parser.add_argument("--version", action="version", version=f"source_verification {TOOL_VERSION}")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_docket(Path(args.registry), Path(args.licences))
    except SourceVerificationError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ОШИБКА ВХОДА (fail-closed): {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(dumps(report), end="")
    elif args.check:
        if report["violations"]:
            print("FAIL: docket invariants violated")
            for item in report["violations"]:
                print(f"  - {item}")
        else:
            print(f"OK: {report['counts']['total']} источников, "
                  f"VERIFIED {report['counts']['by_docket_verdict']['VERIFIED']}, "
                  f"нарушений нет")
    else:
        print(render_summary(report))
    if not report["ok"]:
        # Fail-closed: a docket with violations is never published as a file.
        if not args.json and not args.check:
            print("Файл не записан: docket с нарушениями не публикуется.", file=sys.stderr)
        return 1
    if args.out:
        try:
            write_report(report, Path(args.out),
                         inputs=(args.registry, args.licences))
        except SourceVerificationError as exc:
            print(f"ОШИБКА ЗАПИСИ (fail-closed): {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
