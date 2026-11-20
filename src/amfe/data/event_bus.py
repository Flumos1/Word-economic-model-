"""Event bus over the append-only audit journal (AMFE-EVENT/1.0.0, docs/194).

The bus is the L1 seam between data/models/products and the audit journal: callers ``publish``
typed events (DATA_RELEASE, MODEL_REGISTERED, FORECAST_RECORDED, SCENARIO_RUN, SIGNOFF,
PRODUCT_EXPORT), and every event gets a canonical SHA-256 ``event_id`` chained to the previous
event through ``prev_hash``.  Payloads are aggregate-only (no raw data, no secrets) and references
carry relative paths plus optional hashes of artefacts.

Determinism: the timestamp comes from an injectable clock; with a fixed ``ts`` the same inputs
produce the same record, the same ``event_id`` and the same line bytes.  The bus is single-process
(no file locks) and local JSONL only; the journal is tamper-evident, not tamper-proof.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .audit_log import (
    EVENT_SCHEMA,
    GENESIS_HASH,
    KINDS,
    AuditError,
    AuditLog,
    ExportInfo,
    JournalCorruption,
    make_record,
    validate_event,
    validate_payload,
    validate_refs,
)

#: Default journal location for the project tools (git-ignored ``build/*``).
DEFAULT_JOURNAL = "build/event_bus/journal.jsonl"


class EventBusError(AuditError):
    """Base error of the event bus."""


class UnknownKind(EventBusError):
    """Fail-closed: the event kind is not in the AMFE-EVENT/1.0.0 vocabulary."""


class PayloadRejected(EventBusError):
    """Fail-closed: payload contains raw data, secrets or is too large."""


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Event:
    """One bus event (a validated record of the journal)."""

    record: Mapping

    @property
    def seq(self) -> int:
        return int(self.record["seq"])

    @property
    def event_id(self) -> str:
        return str(self.record["event_id"])

    @property
    def ts(self) -> str:
        return str(self.record["ts"])

    @property
    def kind(self) -> str:
        return str(self.record["kind"])

    @property
    def actor(self) -> str:
        return str(self.record.get("actor", ""))

    @property
    def subject(self) -> str:
        return str(self.record.get("subject", ""))

    @property
    def payload(self) -> dict:
        return dict(self.record.get("payload") or {})

    @property
    def refs(self) -> list:
        return [dict(item) for item in (self.record.get("refs") or [])]

    @property
    def prev_hash(self) -> str:
        return str(self.record["prev_hash"])

    @property
    def correlation_id(self) -> str | None:
        return self.record.get("correlation_id")

    def as_dict(self) -> dict:
        return dict(self.record)


class EventBus:
    """Append-only, hash-chained event bus backed by :class:`amfe.data.audit_log.AuditLog`."""

    def __init__(self, journal_path: str | os.PathLike | None = None, *,
                 clock: Callable[[], str] | None = None) -> None:
        self.journal = AuditLog(journal_path or DEFAULT_JOURNAL)
        self._clock = clock or utc_now_iso

    # -- identity ---------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.journal.path

    def count(self) -> int:
        return self.journal.count()

    def head(self) -> dict | None:
        return self.journal.head()

    def digest(self) -> str:
        return self.journal.digest()

    def __len__(self) -> int:
        return self.count()

    # -- publish ----------------------------------------------------------------------
    def publish(self, kind: str, payload: Mapping, *, actor: str = "", subject: str = "",
                refs: object = (), correlation_id: str | None = None,
                ts: str | None = None) -> Event:
        """Append one event; returns the immutable :class:`Event` (fail-closed)."""
        if kind not in KINDS:
            raise UnknownKind(f"unknown event kind {kind!r}; allowed {sorted(KINDS)}")
        try:
            cleaned_payload = validate_payload(payload)
        except AuditError as error:
            raise PayloadRejected(str(error)) from error
        try:
            cleaned_refs = validate_refs(refs)
        except AuditError as error:
            raise PayloadRejected(str(error)) from error
        stamp = str(ts) if ts is not None else str(self._clock())
        current = self.journal.head()
        record = make_record(
            seq=1 if current is None else int(current["seq"]) + 1, ts=stamp, kind=kind,
            actor=actor, subject=subject, payload=cleaned_payload, refs=cleaned_refs,
            prev_hash=GENESIS_HASH if current is None else str(current["event_id"]),
            correlation_id=correlation_id)
        self.journal.append_record(record)
        return Event(record=record)

    #: ``emit`` is an alias of :meth:`publish` (CLI and docs use both names).
    def emit(self, kind: str, payload: Mapping, **kwargs) -> Event:
        return self.publish(kind, payload, **kwargs)

    # -- load / inspect ---------------------------------------------------------------
    @classmethod
    def load(cls, journal_path: str | os.PathLike | None = None, *,
             clock: Callable[[], str] | None = None) -> "EventBus":
        """Open an existing journal for reading/appending (file may be absent)."""
        return cls(journal_path, clock=clock)

    def events(self) -> list[Event]:
        return [Event(record) for record in self.journal.read()]

    def filter(self, *, kind: str | None = None, subject: str | None = None,
               actor: str | None = None, correlation_id: str | None = None,
               since_ts: str | None = None, limit: int | None = None) -> list[Event]:
        """Filter events (all filters are conjunctive; output keeps journal order)."""
        if kind is not None and kind not in KINDS:
            raise UnknownKind(f"unknown event kind {kind!r}; allowed {sorted(KINDS)}")
        if limit is not None and int(limit) < 0:
            raise EventBusError("limit must be non-negative")
        selected = []
        for event in self.events():
            if kind is not None and event.kind != kind:
                continue
            if subject is not None and event.subject != subject:
                continue
            if actor is not None and event.actor != actor:
                continue
            if correlation_id is not None and event.correlation_id != correlation_id:
                continue
            if since_ts is not None and event.ts < str(since_ts):
                continue
            selected.append(event)
        if limit is not None:
            selected = selected[:int(limit)]
        return selected

    def validate(self) -> dict:
        """Verify the journal chain and the event vocabulary (same as :meth:`verify`)."""
        return self.verify()

    def verify(self, *, expected_head: str | None = None,
               expected_count: int | None = None) -> dict:
        return self.journal.verify(expected_head=expected_head,
                                   expected_count=expected_count)

    def export(self, destination: str | os.PathLike, *,
               exported_at: str | None = None) -> ExportInfo:
        return self.journal.export(destination, exported_at=exported_at)

    # -- convenience for tests/docs ----------------------------------------------------
    def summary(self) -> dict:
        verification = self.verify()
        counts: dict = {}
        for event in self.events():
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return {
            "schema": EVENT_SCHEMA,
            "journal": str(self.path),
            "count": verification["count"],
            "head": verification["head"],
            "verify_ok": verification["ok"],
            "kinds": dict(sorted(counts.items())),
            "journal_sha256": verification["journal_sha256"],
        }


__all__ = [
    "AuditLog",
    "AuditError",
    "DEFAULT_JOURNAL",
    "EVENT_SCHEMA",
    "Event",
    "EventBus",
    "EventBusError",
    "ExportInfo",
    "GENESIS_HASH",
    "JournalCorruption",
    "KINDS",
    "PayloadRejected",
    "UnknownKind",
    "emit_export_event",
    "emit_forecast_event",
    "emit_scenario_event",
    "utc_now_iso",
]

from .audit_log import emit_export_event, emit_forecast_event, emit_scenario_event  # noqa: E402
