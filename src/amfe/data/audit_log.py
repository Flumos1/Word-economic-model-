"""Append-only audit journal with a SHA-256 event chain (AMFE-EVENT/1.0.0, docs/194).

The journal is a local JSONL file: one canonical-JSON event per line, each event carrying a
``prev_hash`` pointing at the previous event's ``event_id`` (genesis = 64 zeros).  Recomputing the
chain detects payload tampering, re-ordering, gaps and truncation (when the expected head is known).

Guarantees and non-guarantees
-----------------------------
* **Tamper-evident locally, not tamper-proof**: an attacker with write access can rewrite the whole
  file and recompute the chain; protection comes from comparing the head/count with an external
  checkpoint (``verify(expected_head=..., expected_count=...)``) and from the exported manifest.
* **Single-writer**: no distributed locking; one process appends at a time (documented limitation).
* **No raw data, no secrets**: payloads are aggregates/hashes; keys matching secret/personal
  patterns and raw-data keys are rejected (see :data:`FORBIDDEN_KEYS`, :data:`RAW_DATA_KEYS`).
* **Deterministic content**: the event id is a canonical-JSON SHA-256; timestamps are supplied by the
  caller (the bus clock) so tests and replays reproduce identical ids.

This module owns the primitives (constants, canonical JSON, validation, chain verification, export)
and the integration wrappers ``emit_forecast_event`` / ``emit_scenario_event`` /
``emit_export_event`` (duck-typed on a bus object, so no imports of :mod:`amfe.data.event_bus`).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

EVENT_SCHEMA = "AMFE-EVENT/1.0.0"
JOURNAL_SCHEMA = "amfe.audit.journal.v1"
MANIFEST_SCHEMA = "amfe.audit.export.v1"
GENESIS_HASH = "0" * 64
KINDS = frozenset({
    "DATA_RELEASE", "MODEL_REGISTERED", "FORECAST_RECORDED", "SCENARIO_RUN", "SIGNOFF",
    "PRODUCT_EXPORT",
})
MAX_PAYLOAD_BYTES = 8192
MAX_STRING_LENGTH = 1024
MAX_DEPTH = 4
MAX_LIST_ITEMS = 64
MAX_REFS = 16
TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
#: Keys that must never reach the journal (secrets, credentials, personal contacts).
FORBIDDEN_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey", "private_key",
    "credential", "credentials", "authorization", "cookie", "session", "email", "phone",
    "ssn", "iban", "card_number",
})
#: Keys that would smuggle raw data/matrices into the journal; payloads must stay aggregate-only.
RAW_DATA_KEYS = frozenset({
    "z", "matrix", "matrices", "values", "rows", "data", "raw", "array", "series",
    "timeseries", "quantiles_5_to_95_step_5", "y", "x", "va",
})


class AuditError(ValueError):
    """Base error of the audit journal / event bus."""


class JournalCorruption(AuditError):
    """Fail-closed: a journal line is not a valid event or the chain does not verify."""


def canonical_json(document: object) -> str:
    """Canonical JSON used for hashing: sorted keys, compact separators, UTF-8."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(HEX64_PATTERN.match(value))


def _check_key(key: object, path: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise AuditError(f"{path}: payload keys must be non-empty strings, got {key!r}")
    lowered = key.lower()
    if lowered in FORBIDDEN_KEYS:
        raise AuditError(f"{path}.{key}: forbidden key (secrets/personal data are not journaled)")
    if lowered in RAW_DATA_KEYS:
        raise AuditError(f"{path}.{key}: raw data keys are not allowed; journal aggregates/hashes")
    return key


def _check_value(value: object, path: str, depth: int) -> object:
    if depth > MAX_DEPTH:
        raise AuditError(f"{path}: payload nesting deeper than {MAX_DEPTH}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditError(f"{path}: non-finite float is not JSON-safe")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise AuditError(f"{path}: string longer than {MAX_STRING_LENGTH} characters")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST_ITEMS:
            raise AuditError(f"{path}: list longer than {MAX_LIST_ITEMS} items")
        return [_check_value(item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            key = _check_key(key, path)
            cleaned[key] = _check_value(item, f"{path}.{key}", depth + 1)
        return cleaned
    raise AuditError(f"{path}: unsupported type {type(value).__name__}; payload must be JSON "
                     "primitives (no arrays/objects from third-party models)")


def validate_payload(payload: object) -> dict:
    """Validate and copy an aggregate-only payload (fail-closed)."""
    if not isinstance(payload, Mapping):
        raise AuditError("payload must be a mapping of aggregates/hashes")
    cleaned = _check_value(dict(payload), "payload", 0)
    size = len(canonical_json(cleaned).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise AuditError(f"payload is {size} bytes, limit is {MAX_PAYLOAD_BYTES}")
    return cleaned


def _check_ref_path(path: object) -> str:
    text = str(path)
    if not text or len(text) > 512:
        raise AuditError("ref.path must be a non-empty relative path")
    if os.path.isabs(text) or ".." in text.split("/") or "\\" in text:
        raise AuditError(f"ref.path must be a relative POSIX path without '..': {text!r}")
    lowered = text.lower()
    if any(token in lowered for token in ("secret", ".env", "id_rsa", "private", "token")):
        raise AuditError(f"ref.path looks sensitive: {text!r}")
    return text


def validate_refs(refs: object) -> list:
    """Validate ``refs``: relative paths + optional SHA-256 of the referenced artefact."""
    if refs is None:
        return []
    if isinstance(refs, (str, bytes)) or not isinstance(refs, (list, tuple)):
        raise AuditError("refs must be a list of {path, sha256} mappings")
    if len(refs) > MAX_REFS:
        raise AuditError(f"refs longer than {MAX_REFS} entries")
    cleaned = []
    for index, item in enumerate(refs):
        if not isinstance(item, Mapping):
            raise AuditError(f"refs[{index}] must be a mapping")
        path = _check_ref_path(item.get("path", ""))
        digest = item.get("sha256")
        if digest is not None and not is_hex64(digest):
            raise AuditError(f"refs[{index}].sha256 must be 64 lowercase hex characters")
        cleaned.append({"path": path, "sha256": digest})
    return cleaned


def event_id_for(record_without_id: Mapping) -> str:
    """SHA-256 of the canonical record (all fields except ``event_id``)."""
    payload = {key: value for key, value in dict(record_without_id).items() if key != "event_id"}
    return sha256_hex(canonical_json(payload))


def validate_event(record: object) -> list[str]:
    """Return structural/validation errors of one event record (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return ["record is not a mapping"]
    if record.get("schema") != EVENT_SCHEMA:
        errors.append(f"schema must be {EVENT_SCHEMA}")
    seq = record.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        errors.append("seq must be a positive integer")
    if not is_hex64(record.get("event_id")):
        errors.append("event_id must be 64 lowercase hex characters")
    ts = record.get("ts")
    if not isinstance(ts, str) or not TS_PATTERN.match(ts):
        errors.append("ts must be an ISO-8601 UTC string like 2026-11-20T09:00:00Z")
    if record.get("kind") not in KINDS:
        errors.append(f"unknown kind {record.get('kind')!r}")
    for name in ("actor", "subject"):
        value = record.get(name, "")
        if not isinstance(value, str) or len(value) > MAX_STRING_LENGTH:
            errors.append(f"{name} must be a string up to {MAX_STRING_LENGTH} characters")
    correlation = record.get("correlation_id")
    if correlation is not None and (not isinstance(correlation, str) or not correlation):
        errors.append("correlation_id must be a non-empty string or null")
    if not is_hex64(record.get("prev_hash")):
        errors.append("prev_hash must be 64 lowercase hex characters")
    try:
        validate_payload(record.get("payload", {}))
    except AuditError as error:
        errors.append(f"payload rejected: {error}")
    try:
        validate_refs(record.get("refs", []))
    except AuditError as error:
        errors.append(f"refs rejected: {error}")
    unknown = set(record) - {"schema", "seq", "event_id", "ts", "kind", "actor", "subject",
                             "payload", "refs", "prev_hash", "correlation_id"}
    if unknown:
        errors.append(f"unexpected fields: {sorted(unknown)}")
    return errors


def make_record(*, seq: int, ts: str, kind: str, actor: str, subject: str, payload: Mapping,
                refs: object, prev_hash: str, correlation_id: str | None) -> dict:
    """Build the canonical event record and compute its ``event_id``."""
    record = {
        "schema": EVENT_SCHEMA, "seq": int(seq), "ts": str(ts), "kind": str(kind),
        "actor": str(actor), "subject": str(subject), "payload": validate_payload(payload),
        "refs": validate_refs(refs), "prev_hash": str(prev_hash),
        "correlation_id": correlation_id,
    }
    record["event_id"] = event_id_for(record)
    errors = validate_event(record)
    if errors:
        raise AuditError(f"invalid event: {errors}")
    return record


@dataclass(frozen=True)
class ExportInfo:
    journal_path: str
    manifest_path: str
    count: int
    head: str | None
    sha256: str

    def as_dict(self) -> dict:
        return {"journal_path": self.journal_path, "manifest_path": self.manifest_path,
                "count": self.count, "head": self.head, "sha256": self.sha256}


class AuditLog:
    """Append-only JSONL journal at ``path`` (created lazily on the first append)."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    # -- read -----------------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> list[dict]:
        if not self.exists():
            return []
        events = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(),
                                            start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                raise JournalCorruption(f"{self.path}: line {line_number} is not JSON: {error}")
            events.append(record)
        return events

    def count(self) -> int:
        return len(self.read())

    def head(self) -> dict | None:
        events = self.read()
        return events[-1] if events else None

    def digest(self) -> str:
        if not self.exists():
            return sha256_hex("")
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    # -- write ----------------------------------------------------------------------
    def append_record(self, record: Mapping) -> None:
        """Append one validated event; enforces seq continuity and chain linkage."""
        errors = validate_event(record)
        if errors:
            raise AuditError(f"refusing to append an invalid event: {errors}")
        current = self.head()
        expected_seq = 1 if current is None else int(current["seq"]) + 1
        if int(record["seq"]) != expected_seq:
            raise JournalCorruption(f"append refused: seq {record['seq']} != expected {expected_seq}")
        expected_prev = GENESIS_HASH if current is None else current["event_id"]
        if record["prev_hash"] != expected_prev:
            raise JournalCorruption("append refused: prev_hash does not match the journal head")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(dict(record)) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    # -- verify / export ------------------------------------------------------------
    def verify(self, *, expected_head: str | None = None,
               expected_count: int | None = None) -> dict:
        """Recompute the whole chain; report every defect with its line number."""
        errors: list[dict] = []
        events = []
        previous: dict | None = None
        lines = []
        if self.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                errors.append({"line": line_number, "code": "invalid_json", "message": str(error)})
                continue
            events.append(record)
            defects = validate_event(record)
            for message in defects:
                errors.append({"line": line_number, "code": "invalid_event", "message": message})
            expected_seq = 1 if previous is None else int(previous["seq"]) + 1
            if record.get("seq") != expected_seq:
                errors.append({"line": line_number, "code": "seq_gap",
                               "message": f"seq {record.get('seq')} != expected {expected_seq}"})
            expected_prev = GENESIS_HASH if previous is None else previous.get("event_id")
            if record.get("prev_hash") != expected_prev:
                errors.append({"line": line_number, "code": "chain_broken",
                               "message": f"prev_hash {record.get('prev_hash')} != "
                                          f"expected {expected_prev}"})
            recomputed = event_id_for(record)
            if record.get("event_id") != recomputed:
                errors.append({"line": line_number, "code": "id_mismatch",
                               "message": "event_id does not match the canonical content"})
            previous = record
        head = events[-1]["event_id"] if events else None
        if expected_head is not None and head != expected_head:
            errors.append({"line": len(lines), "code": "head_mismatch",
                           "message": f"head {head} != expected {expected_head}"})
        if expected_count is not None and len(events) != int(expected_count):
            errors.append({"line": len(lines), "code": "count_mismatch",
                           "message": f"count {len(events)} != expected {expected_count}"})
        checks = {
            "exists": self.exists(),
            "events": len(events),
            "head": head,
            "schema_ok": all(record.get("schema") == EVENT_SCHEMA for record in events),
            "ids_ok": all(record.get("event_id") == event_id_for(record) for record in events),
            "chain_ok": all(
                events[index]["prev_hash"] == (events[index - 1]["event_id"] if index else GENESIS_HASH)
                for index in range(len(events))),
            "seq_ok": [record.get("seq") for record in events] == list(range(1, len(events) + 1)),
            "kinds_ok": all(record.get("kind") in KINDS for record in events),
            "payloads_ok": all(
                not validate_event(record) for record in events),
        }
        return {"schema": JOURNAL_SCHEMA, "ok": not errors, "errors": errors, "checks": checks,
                "count": len(events), "head": head, "journal_sha256": self.digest(),
                "expected_head": expected_head, "expected_count": expected_count}

    def export(self, destination: str | os.PathLike, *,
               exported_at: str | None = None) -> ExportInfo:
        """Copy the journal to ``destination/journal.jsonl`` plus a manifest with its hash."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "journal.jsonl"
        target.write_bytes(self.path.read_bytes() if self.exists() else b"")
        verification = self.verify()
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "source": str(self.path),
            "journal": target.name,
            "count": verification["count"],
            "head": verification["head"],
            "journal_sha256": verification["journal_sha256"],
            "verify_ok": verification["ok"],
            "note": "append-only journal copy for external checkpointing; compare head/count "
                    "outside the process to detect a wholesale rewrite",
        }
        if exported_at is not None:
            manifest["exported_at"] = exported_at
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
        return ExportInfo(journal_path=str(target), manifest_path=str(manifest_path),
                          count=verification["count"], head=verification["head"],
                          sha256=verification["journal_sha256"])


# ---------------------------------------------------------------------------------------
# Integration wrappers (no imports of the bus; duck-typed on ``bus.emit``)
# ---------------------------------------------------------------------------------------
def _emit(bus, kind: str, payload: Mapping, **kwargs):
    try:
        return bus.emit(kind, payload, **kwargs)
    except TypeError:
        return bus.emit(kind, payload=payload, **kwargs)


def emit_forecast_event(bus, *, candidate_id: str, target: str, origin: str, point: float,
                        quantiles: Sequence[float] | None = None, refs=(), actor: str = "",
                        subject: str = "", correlation_id: str | None = None, ts: str | None = None):
    """FORECAST_RECORDED: forecast aggregates + quantile summary (never the raw distribution)."""
    payload = {"candidate_id": candidate_id, "target": target, "origin": origin,
               "point": round(float(point), 9)}
    if quantiles is not None:
        payload["quantiles_count"] = len(quantiles)
        payload["quantile_min"] = round(float(min(quantiles)), 9) if quantiles else None
        payload["quantile_max"] = round(float(max(quantiles)), 9) if quantiles else None
    return _emit(bus, "FORECAST_RECORDED", payload, actor=actor or "registry.forecasts",
                 subject=subject or f"{candidate_id}:{target}:{origin}", refs=refs,
                 correlation_id=correlation_id, ts=ts)


def emit_scenario_event(bus, *, scenario_id: str, commodity: str, price_low: float,
                        price_central: float, price_high: float, horizon_months: int,
                        refs=(), actor: str = "", subject: str = "",
                        correlation_id: str | None = None, ts: str | None = None):
    """SCENARIO_RUN: market-scenario price range (aggregates only)."""
    payload = {"scenario_id": scenario_id, "commodity": commodity,
               "price_low": round(float(price_low), 6),
               "price_central": round(float(price_central), 6),
               "price_high": round(float(price_high), 6),
               "horizon_months": int(horizon_months)}
    return _emit(bus, "SCENARIO_RUN", payload, actor=actor or "markets.scenarios",
                 subject=subject or f"{commodity}:{scenario_id}", refs=refs,
                 correlation_id=correlation_id, ts=ts)


def emit_export_event(bus, *, product: str, format: str, bytes_count: int, sha256: str,
                      refs=(), actor: str = "", subject: str = "",
                      correlation_id: str | None = None, ts: str | None = None):
    """PRODUCT_EXPORT: exported product aggregate + content hash (not the content itself)."""
    if not is_hex64(sha256):
        raise AuditError("export sha256 must be 64 lowercase hex characters")
    payload = {"product": product, "format": format, "bytes": int(bytes_count),
               "sha256": sha256}
    return _emit(bus, "PRODUCT_EXPORT", payload, actor=actor or "products.export",
                 subject=subject or f"{product}:{format}", refs=refs,
                 correlation_id=correlation_id, ts=ts)
