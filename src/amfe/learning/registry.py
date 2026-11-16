"""Append-only model/version registry for the learning loop (wave 21, track F, docs/151).

The registry is a JSONL audit log: one canonical JSON object per line, appended and never rewritten
or edited.  Replaying the log yields the current status of every model version; **ACTIVE** is gated
by the two non-negotiable conditions from docs/145:

* **preregistered criteria** — the acceptance criteria were recorded for this version *before* the
  evaluation date (``criteria.registered_at <= evaluated_at``);
* **independent review** — a review record with ``independent: true``, ``pass: true``, a reviewer
  different from the author and a report hash.

A version without both can reach at most LIMITED; the promotion attempt raises
:class:`RegistryError` and nothing is appended.  The log also supports explicit ``REJECT`` and
``REVERT`` (rollback to the previously ACTIVE version), which is how a degraded version is removed
without rewriting history.

Public API
----------
``register`` / ``promote`` / ``reject`` / ``revert``  append validated events.
``load_events`` / ``state`` / ``validate_registry``   read-only replay and integrity check.
``sha256_text`` / ``sha256_file``                     hash helpers for code/data/report digests.
``STATUSES``, ``ALLOWED_TRANSITIONS``, ``ACTIVE_MIN_Q``, ``SCHEMA``  documented contract.

Determinism: no wall clock is read; every event carries the caller's ``at``/``evaluated_at`` date and
``event_id`` is the SHA-256 of the canonical event, so identical inputs produce identical files.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SCHEMA = "AMFE-MODEL-REGISTRY/1.0.0"
STATUSES = ("CANDIDATE", "SHADOW", "LIMITED", "ACTIVE", "REJECTED")
ALLOWED_TRANSITIONS = {
    "REGISTER": {"CANDIDATE"},
    "PROMOTE": {
        "CANDIDATE": ("SHADOW",),
        "SHADOW": ("LIMITED",),
        "LIMITED": ("ACTIVE",),
    },
    "REJECT": {"CANDIDATE": ("REJECTED",), "SHADOW": ("REJECTED",),
               "LIMITED": ("REJECTED",)},
}
ACTIVE_MIN_Q = 0.60
HEX64 = set("0123456789abcdef")


class RegistryError(ValueError):
    """Invalid registry event, illegal transition or a missing ACTIVE prerequisite."""


# ---------------------------------------------------------------------------------------- helpers
def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _hex64(value, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(char not in HEX64 for char in text):
        raise RegistryError(f"{name} must be a 64-character lowercase hex SHA-256")
    return text


def _date(value, name: str) -> str:
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise RegistryError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    year, month, day = value.split("-")
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        raise RegistryError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        raise RegistryError(f"{name} is not a valid date: {value!r}")
    return value


def _label(value, name: str) -> str:
    text = str(value).strip()
    if not text or "/" in text and name == "version":
        raise RegistryError(f"{name} must be a non-empty label without '/', got {value!r}")
    return text


def _jsonable(value, name: str = "value"):
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"{name} must be JSON-serialisable: {exc!r}") from exc
    return value


def canonical_event(event: Mapping) -> str:
    """Canonical JSON line (sorted keys, compact separators, no trailing newline)."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(event: Mapping) -> str:
    payload = {key: value for key, value in event.items() if key != "event_id"}
    return sha256_text(canonical_event(payload))


# ---------------------------------------------------------------------------------- event building
def _validate_criteria(criteria, evaluated_at: str) -> dict | None:
    if criteria is None:
        return None
    if not isinstance(criteria, Mapping):
        raise RegistryError("criteria must be a mapping with registered_at and criteria")
    registered_at = _date(criteria.get("registered_at"), "criteria.registered_at")
    body = criteria.get("criteria")
    if not isinstance(body, Mapping) or not body:
        raise RegistryError("criteria.criteria must be a non-empty mapping")
    if registered_at > evaluated_at:
        raise RegistryError(
            "criteria were registered after the evaluation date "
            f"({registered_at} > {evaluated_at}): pre-registration violated")
    _jsonable(dict(body), "criteria.criteria")
    return {"registered_at": registered_at,
            "criteria": json.loads(json.dumps(dict(body), ensure_ascii=False, sort_keys=True)),
            "sha256": sha256_text(canonical_event(dict(body)))}


def _validate_quality(quality, name: str = "quality") -> dict:
    if not isinstance(quality, Mapping) or "q" not in quality:
        raise RegistryError(f"{name} must be a quality result document with 'q'")
    q = quality["q"]
    if isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(float(q)):
        raise RegistryError(f"{name}.q must be a finite number")
    constraints = quality.get("hard_constraints")
    if not isinstance(constraints, Mapping) or not constraints.get("pass"):
        raise RegistryError(f"{name} does not pass the hard constraints")
    return json.loads(json.dumps(dict(quality), ensure_ascii=False, sort_keys=True))


def build_register_event(*, model_id: str, version: str, code_sha256: str, data_sha256: str,
                         metrics: Mapping, quality: Mapping, evaluated_at: str,
                         author: str = "automation", criteria: Mapping | None = None,
                         note: str = "") -> dict:
    """Build (and validate) a REGISTER event without writing it."""
    model = _label(model_id, "model_id")
    vers = _label(version, "version")
    at = _date(evaluated_at, "evaluated_at")
    if not isinstance(metrics, Mapping) or not metrics:
        raise RegistryError("metrics must be a non-empty mapping")
    _jsonable(dict(metrics), "metrics")
    event = {
        "schema": SCHEMA, "action": "REGISTER", "model_id": model, "version": vers,
        "status": "CANDIDATE", "at": at, "evaluated_at": at,
        "author": _label(author, "author"),
        "code_sha256": _hex64(code_sha256, "code_sha256"),
        "data_sha256": _hex64(data_sha256, "data_sha256"),
        "metrics": json.loads(json.dumps(dict(metrics), ensure_ascii=False, sort_keys=True)),
        "quality": _validate_quality(quality),
        "criteria": _validate_criteria(criteria, at),
        "note": str(note),
    }
    event["event_id"] = _event_id(event)
    return event


def build_promote_event(*, model_id: str, version: str, to_status: str, at: str,
                        state: Mapping, independent_review: Mapping | None = None,
                        note: str = "") -> dict:
    """Build a PROMOTE event, enforcing the status machine and the ACTIVE prerequisites."""
    if to_status not in STATUSES:
        raise RegistryError(f"unknown target status {to_status!r}")
    record = _version_record(state, model_id, version)
    current = record["status"]
    allowed = ALLOWED_TRANSITIONS["PROMOTE"].get(current, ())
    if to_status not in allowed:
        raise RegistryError(f"illegal transition {current} -> {to_status} for {model_id}/{version}")
    event = {"schema": SCHEMA, "action": "PROMOTE", "model_id": record["model_id"],
             "version": record["version"], "from_status": current, "to_status": to_status,
             "at": _date(at, "at"), "note": str(note)}
    if to_status == "ACTIVE":
        _check_active_prerequisites(record, state, independent_review, at)
        event["independent_review"] = _validate_review(independent_review, record["author"])
    elif independent_review is not None:
        raise RegistryError("independent_review is only accepted for the ACTIVE promotion")
    event["event_id"] = _event_id(event)
    return event


def _validate_review(review, author: str) -> dict:
    if not isinstance(review, Mapping):
        raise RegistryError("ACTIVE requires an independent_review mapping")
    if review.get("independent") is not True:
        raise RegistryError("ACTIVE requires independent_review.independent = true")
    if review.get("pass") is not True:
        raise RegistryError("ACTIVE requires independent_review.pass = true")
    reviewer = _label(review.get("reviewer", ""), "reviewer")
    if reviewer.strip().lower() == str(author).strip().lower():
        raise RegistryError("the independent reviewer must differ from the author")
    report = _hex64(review.get("report_sha256", ""), "report_sha256")
    return {"reviewer": reviewer, "independent": True, "pass": True,
            "reviewed_at": _date(review.get("reviewed_at"), "reviewed_at"),
            "report_sha256": report}


def _check_active_prerequisites(record: Mapping, state: Mapping, review, at: str) -> None:
    active_elsewhere = [model for model, data in state.items()
                        if model == record["model_id"] and data.get("active_version")
                        and data["active_version"] != record["version"]]
    if active_elsewhere:
        raise RegistryError(
            f"model {record['model_id']!r} already has an ACTIVE version "
            f"{state[record['model_id']]['active_version']!r}; revert before promoting another")
    criteria = record.get("criteria")
    if not isinstance(criteria, Mapping) or not criteria.get("registered_at"):
        raise RegistryError(
            "ACTIVE requires preregistered criteria for this version "
            "(none were recorded at registration)")
    if criteria["registered_at"] > record["evaluated_at"]:
        raise RegistryError("preregistered criteria are later than the evaluation date")
    quality = record.get("quality") or {}
    q = quality.get("q")
    if isinstance(q, bool) or not isinstance(q, (int, float)):
        raise RegistryError("ACTIVE requires a quality score for the version")
    if float(q) < ACTIVE_MIN_Q:
        raise RegistryError(
            f"ACTIVE requires Q >= {ACTIVE_MIN_Q}, {record['version']} has Q = {float(q):.4f}")
    if not (quality.get("hard_constraints") or {}).get("pass"):
        raise RegistryError("ACTIVE requires the quality hard constraints to pass")
    if not isinstance(review, Mapping):
        raise RegistryError(
            "ACTIVE requires an independent review (independent_review mapping)")
    if _date(at, "at") < _date(review.get("reviewed_at"), "reviewed_at"):
        raise RegistryError("the ACTIVE promotion cannot precede the independent review")


def build_reject_event(*, model_id: str, version: str, at: str, reason: str,
                       state: Mapping) -> dict:
    record = _version_record(state, model_id, version)
    current = record["status"]
    if "REJECTED" not in ALLOWED_TRANSITIONS["REJECT"].get(current, ()):
        raise RegistryError(
            f"cannot reject {model_id}/{version} in status {current}; use revert for ACTIVE")
    if not str(reason).strip():
        raise RegistryError("reject requires a non-empty reason")
    event = {"schema": SCHEMA, "action": "REJECT", "model_id": record["model_id"],
             "version": record["version"], "from_status": current,
             "to_status": "REJECTED", "at": _date(at, "at"), "reason": str(reason)}
    event["event_id"] = _event_id(event)
    return event


def build_revert_event(*, model_id: str, from_version: str, to_version: str, at: str,
                       reason: str, state: Mapping) -> dict:
    model = str(model_id)
    if model not in state:
        raise RegistryError(f"unknown model {model_id!r}")
    data = state[model]
    from_record = _version_record(state, model, from_version)
    if from_record["status"] not in ("ACTIVE", "LIMITED"):
        raise RegistryError(
            f"revert source must be ACTIVE or LIMITED, got {from_record['status']}")
    target = _version_record(state, model, to_version)
    if target.get("ever_active") is not True:
        raise RegistryError(f"{to_version} was never ACTIVE; nothing to revert to")
    if target["status"] == "REJECTED":
        raise RegistryError(f"{to_version} is REJECTED and cannot be restored")
    if not str(reason).strip():
        raise RegistryError("revert requires a non-empty reason")
    event = {"schema": SCHEMA, "action": "REVERT", "model_id": model,
             "from_version": from_record["version"], "to_version": target["version"],
             "at": _date(at, "at"), "reason": str(reason)}
    event["event_id"] = _event_id(event)
    return event


# -------------------------------------------------------------------------------------- log I/O
def load_events(path: Path) -> list[dict]:
    """Read and parse the JSONL log (fail-closed on a malformed line or broken schema)."""
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
            raise RegistryError(f"registry line {number} is not JSON: {exc}") from exc
        if not isinstance(event, dict) or event.get("schema") != SCHEMA:
            raise RegistryError(f"registry line {number} has an unexpected schema")
        events.append(event)
    return events


def append_event(path: Path, event: Mapping) -> dict:
    """Append one canonical line; never rewrites existing bytes (append-only contract)."""
    if not isinstance(event, Mapping) or event.get("schema") != SCHEMA:
        raise RegistryError("event must be a registry event with the current schema")
    line = canonical_event(event)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.is_file() else b""
    if existing and not existing.endswith(b"\n"):
        raise RegistryError("registry file does not end with a newline; refusing to append")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    return dict(event)


# ------------------------------------------------------------------------------------------ replay
def _version_record(state: Mapping, model_id, version) -> Mapping:
    model = str(model_id)
    if model not in state:
        raise RegistryError(f"unknown model {model_id!r}")
    versions = state[model]["versions"]
    if str(version) not in versions:
        raise RegistryError(f"unknown version {version!r} of model {model!r}")
    return versions[str(version)]


def state(events: list[dict] | Path) -> dict:
    """Replay events into ``{model: {versions, active_version, events}}`` (fail-closed)."""
    if isinstance(events, (str, Path)):
        events = load_events(Path(events))
    models: dict = {}
    for index, event in enumerate(events, start=1):
        action = event.get("action")
        model = event.get("model_id")
        if action == "REGISTER":
            version = event.get("version")
            if model in models and version in models[model]["versions"]:
                raise RegistryError(f"duplicate REGISTER for {model}/{version} at event {index}")
            models.setdefault(model, {"versions": {}, "active_version": None,
                                      "events": 0})
            models[model]["versions"][version] = {
                "model_id": model, "version": version, "status": "CANDIDATE",
                "author": event.get("author"), "evaluated_at": event.get("evaluated_at"),
                "code_sha256": event.get("code_sha256"), "data_sha256": event.get("data_sha256"),
                "metrics": event.get("metrics"), "quality": event.get("quality"),
                "criteria": event.get("criteria"), "ever_active": False,
                "registered_event": index,
            }
            models[model]["events"] += 1
        elif action in ("PROMOTE", "REJECT"):
            record = _version_record(models, model, event.get("version"))
            if action == "PROMOTE":
                if event.get("to_status") == "ACTIVE" and record["status"] != "LIMITED":
                    raise RegistryError(f"ACTIVE promotion requires LIMITED, got {record['status']}")
                record["status"] = event["to_status"]
                if record["status"] == "ACTIVE":
                    record["ever_active"] = True
                    models[model]["active_version"] = record["version"]
            else:
                record["status"] = "REJECTED"
                if models[model]["active_version"] == record["version"]:
                    models[model]["active_version"] = None
            models[model]["events"] += 1
        elif action == "REVERT":
            source = _version_record(models, model, event.get("from_version"))
            target = _version_record(models, model, event.get("to_version"))
            source["status"] = "REJECTED"
            target["status"] = "ACTIVE"
            target["ever_active"] = True
            models[model]["active_version"] = target["version"]
            models[model]["events"] += 1
        else:
            raise RegistryError(f"unknown registry action {action!r} at event {index}")
    return models


# ------------------------------------------------------------------------------------ high level
def register(path: Path, *, model_id: str, version: str, code_sha256: str, data_sha256: str,
             metrics: Mapping, quality: Mapping, evaluated_at: str, author: str = "automation",
             criteria: Mapping | None = None, note: str = "") -> dict:
    """Validate and append a REGISTER event; returns the appended event."""
    events = load_events(path)
    current = state(events)
    if model_id in current and version in current[model_id]["versions"]:
        raise RegistryError(f"{model_id}/{version} is already registered")
    event = build_register_event(model_id=model_id, version=version, code_sha256=code_sha256,
                                 data_sha256=data_sha256, metrics=metrics, quality=quality,
                                 evaluated_at=evaluated_at, author=author, criteria=criteria,
                                 note=note)
    return append_event(path, event)


def promote(path: Path, *, model_id: str, version: str, to_status: str, at: str,
            independent_review: Mapping | None = None, note: str = "") -> dict:
    current = state(load_events(path))
    event = build_promote_event(model_id=model_id, version=version, to_status=to_status, at=at,
                                state=current, independent_review=independent_review, note=note)
    return append_event(path, event)


def reject(path: Path, *, model_id: str, version: str, at: str, reason: str) -> dict:
    current = state(load_events(path))
    event = build_reject_event(model_id=model_id, version=version, at=at, reason=reason,
                               state=current)
    return append_event(path, event)


def revert(path: Path, *, model_id: str, from_version: str, to_version: str, at: str,
           reason: str) -> dict:
    current = state(load_events(path))
    event = build_revert_event(model_id=model_id, from_version=from_version, to_version=to_version,
                               at=at, reason=reason, state=current)
    return append_event(path, event)


def validate_registry(path: Path) -> dict:
    """Replay and check every event; returns a report instead of raising (audit view)."""
    errors: list[str] = []
    path = Path(path)
    try:
        events = load_events(path)
    except RegistryError as exc:
        return {"valid": False, "path": str(path), "entries": 0, "errors": [str(exc)]}
    replay: dict = {}
    actives: dict = {}
    for index, event in enumerate(events, start=1):
        action = event.get("action")
        model, version = event.get("model_id"), event.get("version")
        try:
            if action == "REGISTER":
                if model in replay and version in replay[model]["versions"]:
                    raise RegistryError(f"duplicate REGISTER {model}/{version}")
                replay.setdefault(model, {"versions": {}, "active_version": None})
                record = {"status": "CANDIDATE", "version": version, "author":
                          event.get("author"), "evaluated_at": event.get("evaluated_at"),
                          "criteria": event.get("criteria"), "quality": event.get("quality"),
                          "ever_active": False, "model_id": model}
                replay[model]["versions"][version] = record
            elif action == "PROMOTE":
                record = replay[model]["versions"][version]
                if event.get("to_status") == "ACTIVE":
                    if record["status"] != "LIMITED":
                        raise RegistryError(f"ACTIVE from {record['status']}")
                    _check_active_prerequisites(record, replay, event.get("independent_review"),
                                                event.get("at"))
                    if model in actives:
                        raise RegistryError(f"second ACTIVE version {version}")
                    actives[model] = version
                    record["ever_active"] = True
                record["status"] = event.get("to_status")
                replay[model]["active_version"] = (
                    version if event.get("to_status") == "ACTIVE" else
                    replay[model]["active_version"])
            elif action == "REJECT":
                record = replay[model]["versions"][version]
                if record["status"] == "ACTIVE":
                    raise RegistryError("ACTIVE must be reverted, not rejected")
                record["status"] = "REJECTED"
            elif action == "REVERT":
                source = replay[model]["versions"][event.get("from_version")]
                target = replay[model]["versions"][event.get("to_version")]
                if not target.get("ever_active"):
                    raise RegistryError("revert target was never ACTIVE")
                source["status"] = "REJECTED"
                target["status"] = "ACTIVE"
                replay[model]["active_version"] = target["version"]
                actives[model] = target["version"]
            else:
                raise RegistryError(f"unknown action {action!r}")
        except (RegistryError, KeyError) as exc:
            errors.append(f"line {index}: {exc}")
    return {"valid": not errors, "path": str(path), "entries": len(events), "errors": errors,
            "models": {model: {"active_version": data["active_version"],
                               "versions": {version: record["status"] for version, record
                                            in data["versions"].items()}}
                       for model, data in replay.items()}}


def model_status(events: list[dict] | Path, model_id: str) -> dict:
    """Current view of one model: active version and per-version statuses."""
    current = state(events)
    if model_id not in current:
        raise RegistryError(f"unknown model {model_id!r}")
    return {"model_id": model_id, "active_version": current[model_id]["active_version"],
            "versions": {version: record["status"]
                         for version, record in current[model_id]["versions"].items()}}


__all__ = ["ACTIVE_MIN_Q", "ALLOWED_TRANSITIONS", "HEX64", "RegistryError", "SCHEMA", "STATUSES",
           "append_event", "build_promote_event", "build_register_event", "build_reject_event",
           "build_revert_event", "canonical_event", "load_events", "model_status", "promote",
           "register", "reject", "revert", "sha256_file", "sha256_text", "state",
           "validate_registry"]
