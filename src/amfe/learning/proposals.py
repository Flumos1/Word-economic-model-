"""Proposal journal for the L-2/L-3 learning loop (G6-prep, docs/195, docs/173 #14).

The engine is deliberately **not** an autonomous learner: it turns observed signals (Q degradation,
a failed C4 gate, non-VERIFIED data sources, a missed C6 release week, missing coverage) into
structured *proposals*, records them in an append-only JSONL journal and stops.  A proposal starts in
``PROPOSED``; only a named **human** can move it to ``APPROVED`` or ``REJECTED``, and only an approved
proposal can be marked ``IMPLEMENTED``.  There is no code path that auto-approves anything: the builder
always emits ``PROPOSED``, the journal refuses approval events that do not carry a human name, and
automation-like names (``automation``, ``system``, ``engine``, ``bot``, ``ai`` …) are rejected by the
human gate.

Proposal kinds (docs/173 P1 #14, L-2 structural / L-3 methodological):
  NEW_LINK   — connect an existing thing (e.g. restore a release-week registration link);
  NEW_REGIME — a new regime/state split is suggested by the evidence;
  NEW_METHOD — a methodological fix/experiment (C4 mask ambiguity, coverage tracking, Q work);
  DATA_GAP   — a data source is not VERIFIED and blocks a claim.

Schema (``AMFE-PROPOSAL/1.0.0``): one canonical JSON line per event
(``REGISTER`` -> ``APPROVE`` / ``REJECT`` -> ``IMPLEMENT``), with a deterministic ``proposal_id``,
``kind``, ``title``, ``rationale``, ``evidence`` (rule + signal values + source paths),
``expected_value`` (benefit/risk/effort heuristic, H-008 proxy), status and the human signature
(``human_approved_by`` / ``human_approved_at``).  No wall clock is read: ``created_at`` / ``at`` are
caller-supplied; identical inputs produce identical bytes.

Public API
----------
``build_proposal`` / ``register`` / ``approve`` / ``reject`` / ``implement``   write validated events.
``load_events`` / ``state`` / ``list_proposals`` / ``get_proposal`` / ``export_for_api``   read views.
``validate_human`` / ``KINDS`` / ``STATUSES`` / ``SCHEMA``                    contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

SCHEMA = "AMFE-PROPOSAL/1.0.0"
EXPORT_SCHEMA = "AMFE-PROPOSAL-EXPORT/1.0.0"
KINDS = ("NEW_LINK", "NEW_REGIME", "NEW_METHOD", "DATA_GAP")
STATUSES = ("PROPOSED", "APPROVED", "REJECTED", "IMPLEMENTED")
ACTIONS = ("REGISTER", "APPROVE", "REJECT", "IMPLEMENT")
EFFORT_WEEKS = {"S": 1, "M": 3, "L": 8}
RISK_LEVELS = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}
HUMAN_FORBIDDEN = ("automation", "automatic", "system", "engine", "tool", "bot", "ai", "auto",
                   "agent", "proposal_engine", "t3-admission", "dsh", "cron", "pipeline")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEDUPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{3,160}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")


class ProposalError(ValueError):
    """Invalid proposal/event, illegal status transition or a missing human signature."""


# ---------------------------------------------------------------------------------------- helpers
def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProposalError(f"{name} must be a non-empty string")
    return value.strip()


def _date(value, name: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ProposalError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    month, day = int(value[5:7]), int(value[8:10])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ProposalError(f"{name} is not a valid date: {value!r}")
    return value


def _int_in(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProposalError(f"{name} must be an integer in [{low}, {high}], got {value!r}")
    return value


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(event: Mapping) -> str:
    return hashlib.sha256(
        _canonical({key: value for key, value in event.items() if key != "event_id"}
                   ).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def validate_human(name) -> str:
    """The human gate: a non-empty personal name that is not an automation label."""
    text = _text(name, "human name")
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text)}
    if tokens & set(HUMAN_FORBIDDEN):
        raise ProposalError(
            f"approval must be signed by a human, not {text!r} (automation-like names are refused)")
    if len(text) < 3:
        raise ProposalError(f"human name is too short: {text!r}")
    return text


# -------------------------------------------------------------------------------------- proposal
def _evidence(value) -> dict:
    if not isinstance(value, Mapping) or not value:
        raise ProposalError("evidence must be a non-empty mapping (rule + signal values)")
    try:
        return json.loads(_canonical(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ProposalError(f"evidence must be JSON-serialisable: {exc!r}") from exc


def _expected_value(benefit, risk, effort) -> dict:
    effort_code = _text(effort, "effort")
    if effort_code not in EFFORT_WEEKS:
        raise ProposalError(f"effort must be one of {tuple(EFFORT_WEEKS)}, got {effort_code!r}")
    benefit_value = _int_in(benefit, "benefit", 1, 5)
    risk_value = _int_in(risk, "risk", 1, 5)
    return {"benefit": benefit_value, "risk": risk_value,
            "value_score": round(benefit_value / (1 + risk_value), 3),
            "effort": effort_code, "effort_weeks": EFFORT_WEEKS[effort_code],
            "risk_level": RISK_LEVELS[risk_value]}


def build_proposal(*, kind: str, title: str, rationale: str, evidence: Mapping, dedupe_key: str,
                   created_at: str, benefit: int, risk: int, effort: str,
                   expected_value_note: str = "", note: str = "") -> dict:
    """Build and validate a PROPOSED event (never APPROVED: automation cannot approve)."""
    kind_value = _text(kind, "kind")
    if kind_value not in KINDS:
        raise ProposalError(f"kind must be one of {KINDS}, got {kind_value!r}")
    dedupe = _text(dedupe_key, "dedupe_key")
    if not DEDUPE_RE.fullmatch(dedupe):
        raise ProposalError(f"dedupe_key must match {DEDUPE_RE.pattern}, got {dedupe!r}")
    payload = {
        "schema": SCHEMA, "action": "REGISTER", "kind": kind_value,
        "title": _text(title, "title"), "rationale": _text(rationale, "rationale"),
        "evidence": _evidence(evidence), "dedupe_key": dedupe,
        "created_at": _date(created_at, "created_at"),
        "expected_value": _expected_value(benefit, risk, effort),
        "note": str(note), "expected_value_note": str(expected_value_note),
        "status": "PROPOSED",
        "human_approved_by": None, "human_approved_at": None,
        "rejected_by": None, "rejected_at": None, "reject_reason": None,
        "implemented_by": None, "implemented_at": None,
    }
    payload["proposal_id"] = hashlib.sha256(
        _canonical({"kind": kind_value, "dedupe_key": dedupe,
                    "title": payload["title"],
                    "evidence_sha256": sha256_text(_canonical(payload["evidence"]))}
                   ).encode("utf-8")).hexdigest()[:16]
    payload["event_id"] = _event_id(payload)
    return payload


def build_approve_event(proposal: Mapping, *, by: str, at: str, note: str = "") -> dict:
    if proposal.get("status") != "PROPOSED":
        raise ProposalError(
            f"only a PROPOSED proposal can be approved, {proposal['proposal_id']} is "
            f"{proposal.get('status')}")
    human = validate_human(by)
    event = {"schema": SCHEMA, "action": "APPROVE", "proposal_id": proposal["proposal_id"],
             "kind": proposal["kind"], "human_approved_by": human,
             "human_approved_at": _date(at, "at"), "note": str(note)}
    event["event_id"] = _event_id(event)
    return event


def build_reject_event(proposal: Mapping, *, by: str, at: str, reason: str) -> dict:
    if proposal.get("status") != "PROPOSED":
        raise ProposalError(
            f"only a PROPOSED proposal can be rejected, {proposal['proposal_id']} is "
            f"{proposal.get('status')}")
    human = validate_human(by)
    event = {"schema": SCHEMA, "action": "REJECT", "proposal_id": proposal["proposal_id"],
             "kind": proposal["kind"], "rejected_by": human,
             "rejected_at": _date(at, "at"), "reject_reason": _text(reason, "reason")}
    event["event_id"] = _event_id(event)
    return event


def build_implement_event(proposal: Mapping, *, by: str, at: str, note: str = "") -> dict:
    if proposal.get("status") != "APPROVED":
        raise ProposalError(
            f"only an APPROVED proposal can be implemented, {proposal['proposal_id']} is "
            f"{proposal.get('status')}")
    event = {"schema": SCHEMA, "action": "IMPLEMENT", "proposal_id": proposal["proposal_id"],
             "kind": proposal["kind"], "implemented_by": validate_human(by),
             "implemented_at": _date(at, "at"), "note": str(note)}
    event["event_id"] = _event_id(event)
    return event


# ---------------------------------------------------------------------------------------- log I/O
def load_events(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ProposalError(f"proposal journal line {number} is not JSON: {exc}") from exc
        if not isinstance(event, dict) or event.get("schema") != SCHEMA:
            raise ProposalError(f"proposal journal line {number} has an unexpected schema")
        events.append(event)
    return events


def append_event(path: Path, event: Mapping) -> dict:
    if not isinstance(event, Mapping) or event.get("schema") != SCHEMA:
        raise ProposalError("event must be a proposal event with the current schema")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.is_file() else b""
    if existing and not existing.endswith(b"\n"):
        raise ProposalError("proposal journal does not end with a newline; refusing to append")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(event) + "\n")
    return dict(event)


def state(events: list[dict] | Path) -> dict:
    """Replay the journal into ``{proposal_id: proposal view}`` (fail-closed)."""
    if isinstance(events, (str, Path)):
        events = load_events(Path(events))
    proposals: dict = {}
    for index, event in enumerate(events, start=1):
        action = event.get("action")
        proposal_id = event.get("proposal_id")
        if action == "REGISTER":
            if proposal_id in proposals:
                raise ProposalError(f"duplicate REGISTER for {proposal_id} at line {index}")
            proposals[proposal_id] = {key: value for key, value in event.items()
                                      if key != "event_id"}
            proposals[proposal_id]["status"] = "PROPOSED"
            proposals[proposal_id]["events"] = 1
        else:
            if proposal_id not in proposals:
                raise ProposalError(f"unknown proposal {proposal_id!r} at line {index}")
            proposal = proposals[proposal_id]
            if action == "APPROVE":
                if proposal["status"] != "PROPOSED":
                    raise ProposalError(f"APPROVE on {proposal_id} in {proposal['status']}")
                proposal.update({"status": "APPROVED",
                                 "human_approved_by": event.get("human_approved_by"),
                                 "human_approved_at": event.get("human_approved_at")})
                validate_human(proposal["human_approved_by"])
            elif action == "REJECT":
                if proposal["status"] != "PROPOSED":
                    raise ProposalError(f"REJECT on {proposal_id} in {proposal['status']}")
                proposal.update({"status": "REJECTED", "rejected_by": event.get("rejected_by"),
                                 "rejected_at": event.get("rejected_at"),
                                 "reject_reason": event.get("reject_reason")})
                validate_human(proposal["rejected_by"])
            elif action == "IMPLEMENT":
                if proposal["status"] != "APPROVED":
                    raise ProposalError(f"IMPLEMENT on {proposal_id} in {proposal['status']}")
                proposal.update({"status": "IMPLEMENTED",
                                 "implemented_by": event.get("implemented_by"),
                                 "implemented_at": event.get("implemented_at")})
                validate_human(proposal["implemented_by"])
            else:
                raise ProposalError(f"unknown proposal action {action!r} at line {index}")
            proposal["events"] += 1
    return proposals


# ------------------------------------------------------------------------------------- high level
def register(path: Path, proposal: Mapping) -> dict:
    if not isinstance(proposal, Mapping) or proposal.get("schema") != SCHEMA:
        raise ProposalError("proposal must be a proposal event")
    if proposal.get("action") != "REGISTER" or proposal.get("status") != "PROPOSED":
        raise ProposalError("only REGISTER/PROPOSED events can be appended as proposals")
    current = state(load_events(path))
    if proposal["proposal_id"] in current:
        raise ProposalError(f"proposal {proposal['proposal_id']} is already registered")
    return append_event(path, proposal)


def approve(path: Path, proposal_id: str, *, by: str, at: str, note: str = "") -> dict:
    current = state(load_events(path))
    if proposal_id not in current:
        raise ProposalError(f"unknown proposal {proposal_id!r}")
    return append_event(path, build_approve_event(current[proposal_id], by=by, at=at, note=note))


def reject(path: Path, proposal_id: str, *, by: str, at: str, reason: str) -> dict:
    current = state(load_events(path))
    if proposal_id not in current:
        raise ProposalError(f"unknown proposal {proposal_id!r}")
    return append_event(path, build_reject_event(current[proposal_id], by=by, at=at, reason=reason))


def implement(path: Path, proposal_id: str, *, by: str, at: str, note: str = "") -> dict:
    current = state(load_events(path))
    if proposal_id not in current:
        raise ProposalError(f"unknown proposal {proposal_id!r}")
    return append_event(path, build_implement_event(current[proposal_id], by=by, at=at, note=note))


def list_proposals(path: Path, *, status: str | None = None, kind: str | None = None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise ProposalError(f"unknown status filter {status!r}")
    if kind is not None and kind not in KINDS:
        raise ProposalError(f"unknown kind filter {kind!r}")
    current = state(path)
    result = []
    for proposal_id in sorted(current):
        proposal = current[proposal_id]
        if status is not None and proposal["status"] != status:
            continue
        if kind is not None and proposal["kind"] != kind:
            continue
        result.append(dict(proposal))
    return result


def get_proposal(path: Path, proposal_id: str) -> dict:
    current = state(path)
    if proposal_id not in current:
        raise ProposalError(f"unknown proposal {proposal_id!r}")
    return dict(current[proposal_id])


def export_for_api(path: Path) -> dict:
    proposals = list_proposals(path)
    counts = {status: 0 for status in STATUSES}
    kinds = {kind: 0 for kind in KINDS}
    for proposal in proposals:
        counts[proposal["status"]] += 1
        kinds[proposal["kind"]] += 1
    return {"schema": EXPORT_SCHEMA, "proposals": proposals,
            "counts": {"proposals": len(proposals), "by_status": counts, "by_kind": kinds}}


def validate_journal(path: Path) -> dict:
    path = Path(path)
    try:
        events = load_events(path)
        state(events)
    except ProposalError as exc:
        return {"valid": False, "path": str(path), "entries": 0, "errors": [str(exc)]}
    statuses = {status: 0 for status in STATUSES}
    for proposal in state(events).values():
        statuses[proposal["status"]] += 1
    return {"valid": True, "path": str(path), "entries": len(events), "errors": [],
            "statuses": statuses}


__all__ = ["ACTIONS", "EXPORT_SCHEMA", "EFFORT_WEEKS", "HUMAN_FORBIDDEN", "KINDS", "ProposalError",
           "RISK_LEVELS", "SCHEMA", "STATUSES", "approve", "build_approve_event",
           "build_implement_event", "build_proposal", "build_reject_event", "export_for_api",
           "get_proposal", "implement", "list_proposals", "load_events", "register", "reject",
           "sha256_text", "state", "validate_human", "validate_journal"]
