"""Module registry (G2, docs/157): "module -> version -> passport" as an append-only JSONL log.

This is the module-level companion of :mod:`amfe.learning.registry` (which records *model versions*
with metrics and quality).  A module passport describes identity, ownership, hashes, entrypoints,
docs/tests and limitations; it lets G4 (walk-forward) and products reference a stable version without
touching the module code.

Statuses: ``RESEARCH_ONLY`` (default) -> ``CANDIDATE`` -> ``LIMITED`` -> ``ACTIVE``.  A passport may
be *registered* only as RESEARCH_ONLY or CANDIDATE; anything **above CANDIDATE** requires
preregistered criteria (recorded before the promotion date) and an independent review, and it can
never be set at registration time.  This mirrors the learning registry rule from docs/145: no version
reaches production without preregistered criteria.

Append-only contract: one canonical JSON object per line; existing bytes are never rewritten; every
event carries ``event_id`` = SHA-256 of the canonical event, so identical inputs produce identical
files and no wall clock is read (caller passes ``created_at`` / ``at``).

Public API
----------
``build_passport`` / ``register`` / ``set_status``     write validated events.
``load_entries`` / ``state`` / ``find`` / ``export_for_api`` / ``validate_registry``   read/export.
``code_hash`` / ``sha256_text`` / ``sha256_file``      deterministic code/data digests.
``STATUSES`` / ``STATUS_RANK`` / ``KINDS`` / ``SCHEMA``  documented contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

SCHEMA = "AMFE-MODULE-REGISTRY/1.0.0"
EXPORT_SCHEMA = "AMFE-MODULE-REGISTRY-EXPORT/1.0.0"
STATUSES = ("RESEARCH_ONLY", "CANDIDATE", "LIMITED", "ACTIVE")
STATUS_RANK = {status: rank for rank, status in enumerate(STATUSES)}
KINDS = ("market", "agri", "finance", "agentic", "world", "core", "other")
DATA_STATUSES = ("COMMITTED", "LOCAL_ONLY", "SYNTHETIC", "ASSUMPTION_BASED", "NONE")
REGISTER_STATUSES = ("RESEARCH_ONLY", "CANDIDATE")
ABOVE_CANDIDATE = tuple(status for status in STATUSES if STATUS_RANK[status] > STATUS_RANK["CANDIDATE"])
HEX64 = set("0123456789abcdef")
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


class ModuleRegistryError(ValueError):
    """Invalid passport/event, duplicate registration or a blocked status promotion."""


# ----------------------------------------------------------------------- digests and basic helpers
def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def code_hash(files: Mapping[str, Path | str] | Sequence[Path | str], *,
              root: Path | None = None) -> dict:
    """Digest a set of source files: ``{relative_path: sha256}`` and one combined ``sha256``.

    The combined hash is the SHA-256 of the sorted ``"relative_path:file_sha256"`` lines, so it does
    not depend on the caller's absolute paths or enumeration order.
    """
    if isinstance(files, Mapping):
        pairs = [(str(relative), Path(path)) for relative, path in files.items()]
    else:
        base = Path(root) if root is not None else Path(".")
        pairs = []
        for path in files:
            resolved = Path(path)
            try:
                relative = str(resolved.relative_to(base)).replace("\\", "/")
            except ValueError:
                relative = resolved.name
            pairs.append((relative, resolved))
    if not pairs:
        raise ModuleRegistryError("code_hash needs at least one file")
    hashes: dict = {}
    for relative, path in sorted(pairs, key=lambda item: item[0]):
        if relative in hashes:
            raise ModuleRegistryError(f"duplicate code path {relative!r}")
        if not Path(path).is_file():
            raise ModuleRegistryError(f"code file not found: {path}")
        hashes[relative] = sha256_file(Path(path))
    combined = sha256_text("\n".join(f"{relative}:{digest}"
                                     for relative, digest in sorted(hashes.items())))
    return {"sha256": combined, "files": hashes}


def _hex64(value, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(char not in HEX64 for char in text):
        raise ModuleRegistryError(f"{name} must be a 64-character lowercase hex SHA-256")
    return text


def _text(value, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ModuleRegistryError(f"{name} must be a non-empty string")
    return text


def _date(value, name: str) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ModuleRegistryError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    year, month, day = (int(part) for part in text.split("-"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ModuleRegistryError(f"{name} is not a valid date: {value!r}")
    return text


def _version(value) -> str:
    text = str(value).strip()
    if not VERSION_RE.fullmatch(text):
        raise ModuleRegistryError(f"version must look like MAJOR.MINOR[.PATCH], got {value!r}")
    return text


def _string_list(value, name: str, *, required: bool = True) -> list[str]:
    if value is None:
        if required:
            raise ModuleRegistryError(f"{name} must be a non-empty list of strings")
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ModuleRegistryError(f"{name} must be a list of strings")
    items = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise ModuleRegistryError(f"{name} contains an empty string")
        items.append(text)
    if required and not items:
        raise ModuleRegistryError(f"{name} must be a non-empty list of strings")
    return items


def _jsonable(value, name: str):
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ModuleRegistryError(f"{name} must be JSON-serialisable: {exc!r}") from exc
    return value


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(event: Mapping) -> str:
    return sha256_text(_canonical({key: value for key, value in event.items()
                                  if key != "event_id"}))


# ---------------------------------------------------------------------------------------- passport
def _criteria(criteria, at: str) -> dict | None:
    if criteria is None:
        return None
    if not isinstance(criteria, Mapping):
        raise ModuleRegistryError("preregistered_criteria must be a mapping")
    registered_at = _date(criteria.get("registered_at"), "criteria.registered_at")
    body = criteria.get("criteria")
    if not isinstance(body, Mapping) or not body:
        raise ModuleRegistryError("criteria.criteria must be a non-empty mapping")
    if registered_at > at:
        raise ModuleRegistryError(
            f"criteria registered at {registered_at} after the promotion date {at}")
    return {"registered_at": registered_at,
            "criteria": json.loads(_canonical(dict(body))),
            "sha256": sha256_text(_canonical(dict(body)))}


def _review(review, owner: str, at: str) -> dict | None:
    if review is None:
        return None
    if not isinstance(review, Mapping):
        raise ModuleRegistryError("independent_review must be a mapping")
    if review.get("independent") is not True:
        raise ModuleRegistryError("independent_review.independent must be true")
    if review.get("pass") is not True:
        raise ModuleRegistryError("independent_review.pass must be true")
    reviewer = _text(review.get("reviewer", ""), "reviewer")
    if reviewer.strip().lower() == str(owner).strip().lower():
        raise ModuleRegistryError("the independent reviewer must differ from the owner")
    reviewed_at = _date(review.get("reviewed_at"), "reviewed_at")
    if reviewed_at > at:
        raise ModuleRegistryError("the promotion cannot precede the independent review")
    return {"reviewer": reviewer, "independent": True, "pass": True,
            "reviewed_at": reviewed_at,
            "report_sha256": _hex64(review.get("report_sha256", ""), "report_sha256")}


def build_passport(*, module_id: str, kind: str, version: str, display_name: str, summary: str,
                   owner: str, code_sha256: str, data_sha256: str | None,
                   data_status: str = "NONE", created_at: str, docs: Sequence[str],
                   tests: Sequence[str], entrypoints: Sequence[str],
                   limitations: Sequence[str], components: Sequence[str] = (),
                   code_files: Mapping[str, str] | None = None, status: str = "RESEARCH_ONLY",
                   criteria: Mapping | None = None, review: Mapping | None = None,
                   note: str = "") -> dict:
    """Build and validate a passport event (does not write it)."""
    module = _text(module_id, "module_id")
    if any(ord(char) < 32 for char in module):
        raise ModuleRegistryError("module_id must not contain control characters")
    kind_value = _text(kind, "kind")
    if kind_value not in KINDS:
        raise ModuleRegistryError(f"kind must be one of {KINDS}, got {kind_value!r}")
    status_value = _text(status, "status")
    if status_value not in STATUSES:
        raise ModuleRegistryError(f"status must be one of {STATUSES}, got {status_value!r}")
    if status_value not in REGISTER_STATUSES:
        raise ModuleRegistryError(
            f"a passport can be registered only as {REGISTER_STATUSES}; "
            f"{status_value} requires a promotion event with preregistered criteria and review")
    if criteria is not None or review is not None:
        raise ModuleRegistryError(
            "criteria/review belong to a status promotion, not to registration")
    at = _date(created_at, "created_at")
    data_sha = None if data_sha256 is None else _hex64(data_sha256, "data_sha256")
    data_state = _text(data_status, "data_status")
    if data_state not in DATA_STATUSES:
        raise ModuleRegistryError(f"data_status must be one of {DATA_STATUSES}")
    if data_sha is not None and data_state == "NONE":
        raise ModuleRegistryError("data_status NONE cannot carry a data_sha256")
    files = None
    if code_files is not None:
        files = {str(key): _hex64(value, f"code_files[{key}]")
                 for key, value in code_files.items()}
    event = {
        "schema": SCHEMA, "action": "REGISTER", "module_id": module, "kind": kind_value,
        "version": _version(version), "status": status_value, "owner": _text(owner, "owner"),
        "display_name": _text(display_name, "display_name"),
        "summary": _text(summary, "summary"),
        "code_sha256": _hex64(code_sha256, "code_sha256"), "code_files": files,
        "data_sha256": data_sha, "data_status": data_state, "created_at": at,
        "docs": _string_list(docs, "docs"), "tests": _string_list(tests, "tests"),
        "entrypoints": _string_list(entrypoints, "entrypoints"),
        "limitations": _string_list(limitations, "limitations"),
        "components": _string_list(components, "components", required=False),
        "note": str(note),
    }
    _jsonable(event, "passport")
    event["event_id"] = _event_id(event)
    return event


def build_status_event(*, module_id: str, version: str, to_status: str, at: str, entries_state: Mapping,
                       criteria: Mapping | None = None, review: Mapping | None = None,
                       note: str = "") -> dict:
    """Build a STATUS event, enforcing that nothing above CANDIDATE skips the gate."""
    record = _record(entries_state, module_id, version)
    if to_status not in STATUSES:
        raise ModuleRegistryError(f"unknown target status {to_status!r}")
    current = record["status"]
    if to_status == current:
        raise ModuleRegistryError(
            f"{module_id}/{version} is already in status {current}")
    moment = _date(at, "at")
    event = {"schema": SCHEMA, "action": "STATUS", "module_id": record["module_id"],
             "version": record["version"], "from_status": current, "to_status": to_status,
             "at": moment, "note": str(note)}
    if STATUS_RANK[to_status] > STATUS_RANK["CANDIDATE"]:
        if not isinstance(criteria, Mapping):
            raise ModuleRegistryError(
                f"status {to_status} requires preregistered criteria (registered before promotion)")
        if not isinstance(review, Mapping):
            raise ModuleRegistryError(f"status {to_status} requires an independent review")
        checked_criteria = _criteria(criteria, moment)
        if checked_criteria is None or checked_criteria["registered_at"] > moment:
            raise ModuleRegistryError("preregistered criteria are later than the promotion date")
        if record.get("criteria") is not None and checked_criteria["sha256"] != \
                record["criteria"].get("sha256"):
            raise ModuleRegistryError(
                "criteria different from the criteria preregistered with the passport")
        event["criteria"] = checked_criteria
        event["independent_review"] = _review(review, record["owner"], moment)
    else:
        if criteria is not None or review is not None:
            raise ModuleRegistryError(
                f"criteria/review are only valid above CANDIDATE, not for {to_status}")
    event["event_id"] = _event_id(event)
    return event


# ---------------------------------------------------------------------------------------- log I/O
def load_entries(path: Path) -> list[dict]:
    """Read and parse the append-only log; fail-closed on a malformed line or wrong schema."""
    path = Path(path)
    if not path.is_file():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ModuleRegistryError(f"registry line {number} is not JSON: {exc}") from exc
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
            raise ModuleRegistryError(f"registry line {number} has an unexpected schema")
        entries.append(entry)
    return entries


def append_event(path: Path, event: Mapping) -> dict:
    if not isinstance(event, Mapping) or event.get("schema") != SCHEMA:
        raise ModuleRegistryError("event must be a module-registry event")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.is_file() else b""
    if existing and not existing.endswith(b"\n"):
        raise ModuleRegistryError("registry file does not end with a newline; refusing to append")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(event) + "\n")
    return dict(event)


def _record(state_map: Mapping, module_id, version) -> Mapping:
    module = str(module_id)
    if module not in state_map:
        raise ModuleRegistryError(f"unknown module {module_id!r}")
    versions = state_map[module]["versions"]
    if str(version) not in versions:
        raise ModuleRegistryError(f"unknown version {version!r} of module {module!r}")
    return versions[str(version)]


def state(entries: list[dict] | Path) -> dict:
    """Replay the log: ``{module_id: {versions, latest_version, status, kind, owner}}``."""
    if isinstance(entries, (str, Path)):
        entries = load_entries(Path(entries))
    modules: dict = {}
    for index, entry in enumerate(entries, start=1):
        action = entry.get("action")
        module = entry.get("module_id")
        version = entry.get("version")
        if action == "REGISTER":
            if module in modules and version in modules[module]["versions"]:
                raise ModuleRegistryError(
                    f"duplicate REGISTER for {module}/{version} at line {index}")
            modules.setdefault(module, {"versions": {}})
            modules[module]["versions"][version] = {
                key: entry.get(key) for key in
                ("module_id", "kind", "version", "status", "owner", "display_name", "summary",
                 "code_sha256", "code_files", "data_sha256", "data_status", "created_at", "docs",
                 "tests", "entrypoints", "limitations", "components", "note", "criteria",
                 "independent_review")
            }
            modules[module]["versions"][version]["status"] = entry["status"]
            modules[module]["versions"][version]["status_history"] = [entry["status"]]
            modules[module]["versions"][version]["status_events"] = 0
        elif action == "STATUS":
            record = _record(modules, module, version)
            record["status"] = entry["to_status"]
            record["status_history"].append(entry["to_status"])
            record["status_events"] += 1
            if entry.get("criteria") is not None:
                record["criteria"] = entry["criteria"]
            if entry.get("independent_review") is not None:
                record["independent_review"] = entry["independent_review"]
        else:
            raise ModuleRegistryError(f"unknown module-registry action {action!r} at line {index}")
    for module, data in modules.items():
        versions = data["versions"]
        latest = max(versions, key=lambda item: _version_tuple(item))
        data["latest_version"] = latest
        data["status"] = versions[latest]["status"]
        data["kind"] = versions[latest]["kind"]
        data["owner"] = versions[latest]["owner"]
    return modules


def _version_tuple(value: str) -> tuple:
    parts = [int(part) for part in value.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


# ------------------------------------------------------------------------------------- high level
def register(path: Path, passport: Mapping) -> dict:
    if not isinstance(passport, Mapping) or passport.get("schema") != SCHEMA:
        raise ModuleRegistryError("passport must be a module-registry event")
    if passport.get("action") != "REGISTER":
        raise ModuleRegistryError("passport event must have action REGISTER")
    event = dict(passport)
    if "event_id" not in event:
        event["event_id"] = _event_id(event)
    current = state(load_entries(path))
    module, version = event["module_id"], event["version"]
    if module in current and version in current[module]["versions"]:
        raise ModuleRegistryError(f"{module}/{version} is already registered")
    return append_event(path, event)


def set_status(path: Path, *, module_id: str, version: str, to_status: str, at: str,
               criteria: Mapping | None = None, review: Mapping | None = None,
               note: str = "") -> dict:
    current = state(load_entries(path))
    event = build_status_event(module_id=module_id, version=version, to_status=to_status, at=at,
                               entries_state=current, criteria=criteria, review=review,
                               note=note)
    return append_event(path, event)


def find(path: Path | list[dict], *, module_id: str | None = None, kind: str | None = None,
         status: str | None = None, latest_only: bool = True) -> list[dict]:
    """Filter passports; by default only the latest version of each module is returned."""
    current = state(path) if isinstance(path, (str, Path)) else state(path)
    results: list[dict] = []
    for module in sorted(current):
        data = current[module]
        if module_id is not None and module != module_id:
            continue
        versions = [data["latest_version"]] if latest_only else sorted(
            data["versions"], key=_version_tuple)
        for version in versions:
            record = dict(data["versions"][version])
            if kind is not None and record.get("kind") != kind:
                continue
            if status is not None and record.get("status") != status:
                continue
            results.append(record)
    return results


def export_for_api(path: Path) -> dict:
    """Deterministic export for the API: latest passport per module, sorted by module id."""
    current = state(path)
    modules = []
    by_kind: dict = {}
    by_status: dict = {}
    for module in sorted(current):
        data = current[module]
        record = dict(data["versions"][data["latest_version"]])
        record.pop("status_history", None)
        record.pop("status_events", None)
        record["module_id"] = module
        modules.append(record)
        by_kind[record.get("kind")] = by_kind.get(record.get("kind"), 0) + 1
        by_status[record.get("status")] = by_status.get(record.get("status"), 0) + 1
    return {"schema": EXPORT_SCHEMA, "source": str(path), "modules": modules,
            "counts": {"modules": len(modules),
                       "by_kind": dict(sorted(by_kind.items())),
                       "by_status": dict(sorted(by_status.items()))}}


def validate_registry(path: Path) -> dict:
    """Replay and validate the log; returns a report instead of raising (audit view)."""
    errors: list[str] = []
    path = Path(path)
    try:
        entries = load_entries(path)
    except ModuleRegistryError as exc:
        return {"valid": False, "path": str(path), "entries": 0, "errors": [str(exc)]}
    modules: dict = {}
    for index, entry in enumerate(entries, start=1):
        try:
            action = entry.get("action")
            module, version = entry.get("module_id"), entry.get("version")
            if action == "REGISTER":
                if entry.get("status") not in REGISTER_STATUSES:
                    raise ModuleRegistryError(
                        f"registration status {entry.get('status')} is above CANDIDATE")
                if module in modules and version in modules[module]["versions"]:
                    raise ModuleRegistryError(f"duplicate REGISTER {module}/{version}")
                modules.setdefault(module, {"versions": {}})
                modules[module]["versions"][version] = {
                    "status": entry["status"], "owner": entry["owner"],
                    "criteria": entry.get("criteria"),
                    "independent_review": entry.get("independent_review"),
                    "status_history": [entry["status"]]}
            elif action == "STATUS":
                record = _record(modules, module, version)
                target = entry.get("to_status")
                if target not in STATUSES:
                    raise ModuleRegistryError(f"unknown status {target!r}")
                current_rank = STATUS_RANK[record["status"]]
                target_rank = STATUS_RANK[target]
                if target_rank > STATUS_RANK["CANDIDATE"]:
                    if not isinstance(entry.get("criteria"), Mapping):
                        raise ModuleRegistryError(f"{target} without preregistered criteria")
                    if not isinstance(entry.get("independent_review"), Mapping):
                        raise ModuleRegistryError(f"{target} without independent review")
                record["status"] = target
                record["status_history"].append(target)
            else:
                raise ModuleRegistryError(f"unknown action {action!r}")
        except (ModuleRegistryError, KeyError) as exc:
            errors.append(f"line {index}: {exc}")
    return {"valid": not errors, "path": str(path), "entries": len(entries), "errors": errors,
            "modules": {module: {"latest_version":
                                 max(data["versions"], key=_version_tuple),
                                 "versions": {version: record["status"]
                                              for version, record in data["versions"].items()}}
                        for module, data in modules.items()}}


__all__ = ["ABOVE_CANDIDATE", "DATA_STATUSES", "EXPORT_SCHEMA", "HEX64", "KINDS",
           "ModuleRegistryError", "REGISTER_STATUSES", "SCHEMA", "STATUSES", "STATUS_RANK",
           "append_event", "build_passport", "build_status_event", "code_hash", "export_for_api",
           "find", "load_entries", "register", "set_status", "sha256_file", "sha256_text", "state",
           "validate_registry"]
