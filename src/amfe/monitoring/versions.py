"""Versioning of forecast models and weights with an append-only, hash-chained journal.

Stage 8 of the AMFE roadmap (docs/42) and the controlled self-learning contour of docs/41:
  * every forecast model version is identified by its code hash and its weight snapshot;
  * the journal is append-only and hash-chained: a VERSION, SWITCH, ROLLBACK, UPDATE or DECISION
    entry cannot be silently edited or removed without breaking the chain;
  * a switch keeps the previous active version, so a rollback always has a target;
  * the journal is the audit trail required by docs/41 §6 ("old/new version, diff, inputs and
    vintages, test results, responsible person, ground, rollback conditions").

Nothing here trains a model.  The module records what the monitoring layer did and makes it
reproducible; model fitting belongs to the forecast/candidate layers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

GENESIS_HASH = "0" * 64
VERSION_ID_SEPARATOR = "@"


class VersionError(RuntimeError):
    """A version or journal operation violates the versioning contract."""


def canonical_json(value) -> str:
    """Canonical JSON: sorted keys, compact separators, UTF-8 (hashable representation)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_source(source: str) -> str:
    """SHA-256 of a source text with CRLF normalised to LF."""
    return sha256_text(source.replace("\r\n", "\n"))


def hash_files(paths) -> str:
    """Deterministic SHA-256 over (relative path, LF-normalised content) pairs of code files."""
    resolved = sorted((Path(p) for p in paths), key=lambda p: str(p).replace("\\", "/"))
    digest = hashlib.sha256()
    for path in resolved:
        try:
            content = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError as error:  # pragma: no cover - filesystem guard
            raise VersionError(f"cannot hash {path}: {error}") from error
        digest.update(str(path).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_text(content).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def weights_hash(weights: dict) -> str:
    """SHA-256 of the canonical weight snapshot."""
    return sha256_text(canonical_json({str(k): float(v) for k, v in weights.items()}))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ModelVersion:
    """One immutable version of a forecast model: code hash + weights + provenance."""

    version_id: str
    model_id: str
    code_hash: str
    weights: dict
    created_at: str = field(default_factory=utc_now)
    code_commit: str | None = None
    data_vintage_hash: str | None = None
    parent_version_id: str | None = None
    metadata: dict = field(default_factory=dict)
    description: str = ""

    def __post_init__(self):
        object.__setattr__(self, "weights", {str(k): float(v) for k, v in self.weights.items()})

    def validate(self) -> list[str]:
        problems = []
        if not self.version_id or not isinstance(self.version_id, str):
            problems.append("version_id must be a non-empty string")
        if not self.model_id:
            problems.append("model_id must be a non-empty string")
        if not self.code_hash or len(self.code_hash) != 64:
            problems.append("code_hash must be a SHA-256 hex digest")
        if not self.weights:
            problems.append("weights must not be empty")
        for name, value in self.weights.items():
            if not isinstance(name, str) or not name:
                problems.append("weight names must be non-empty strings")
            try:
                float(value)
            except (TypeError, ValueError):
                problems.append(f"weight {name!r} is not a number")
        if self.parent_version_id == self.version_id:
            problems.append("a version cannot be its own parent")
        return problems

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "model_id": self.model_id,
            "code_hash": self.code_hash,
            "weights": dict(sorted(self.weights.items())),
            "weights_hash": weights_hash(self.weights),
            "created_at": self.created_at,
            "code_commit": self.code_commit,
            "data_vintage_hash": self.data_vintage_hash,
            "parent_version_id": self.parent_version_id,
            "metadata": dict(self.metadata),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ModelVersion":
        return cls(
            version_id=payload["version_id"], model_id=payload["model_id"],
            code_hash=payload["code_hash"], weights=payload["weights"],
            created_at=payload.get("created_at", utc_now()),
            code_commit=payload.get("code_commit"),
            data_vintage_hash=payload.get("data_vintage_hash"),
            parent_version_id=payload.get("parent_version_id"),
            metadata=payload.get("metadata", {}), description=payload.get("description", ""),
        )


def make_version(model_id: str, weights: dict, *, code_hash: str | None = None,
                 code_source: str | None = None, code_paths=None,
                 parent: ModelVersion | str | None = None, code_commit: str | None = None,
                 data_vintage_hash: str | None = None, metadata: dict | None = None,
                 description: str = "", version_id: str | None = None) -> ModelVersion:
    """Build a deterministic version: the id is bound to the code hash and the weight snapshot."""
    if code_hash is None:
        if code_source is not None:
            code_hash = hash_source(code_source)
        elif code_paths is not None:
            code_hash = hash_files(code_paths)
        else:
            raise VersionError("provide code_hash, code_source or code_paths")
    parent_id = parent.version_id if isinstance(parent, ModelVersion) else parent
    if version_id is None:
        version_id = (f"{model_id}{VERSION_ID_SEPARATOR}{code_hash[:12]}"
                      f"+{weights_hash(weights)[:8]}")
    return ModelVersion(version_id=version_id, model_id=model_id, code_hash=code_hash,
                        weights=weights, code_commit=code_commit,
                        data_vintage_hash=data_vintage_hash, parent_version_id=parent_id,
                        metadata=metadata or {}, description=description)


class VersionJournal:
    """Append-only hash-chained journal of versions, switches, rollbacks and monitoring decisions.

    The journal lives in memory and, optionally, in a JSONL file: every entry is appended as one
    canonical line with ``prev_hash`` pointing at the previous entry and ``entry_hash`` covering the
    payload.  Loading verifies the chain; a tampered file is rejected.
    """

    KINDS = ("VERSION", "SWITCH", "ROLLBACK", "UPDATE", "DECISION", "NOTE")

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._entries: list[dict] = []
        self._versions: dict[str, ModelVersion] = {}
        self._entries_by_version: dict[str, list[int]] = {}
        if self.path is not None and self.path.exists():
            self._load(self.path)
        self._active: str | None = None
        for entry in self._entries:
            if entry["kind"] == "SWITCH":
                self._active = entry["active"]
            elif entry["kind"] == "ROLLBACK":
                self._active = entry["active"]
        # An empty journal may pin the active version explicitly (used when a version was adopted
        # before stage 8 monitoring existed and the journal is opened later).
        self._previous_active: dict[str, str | None] = {}
        for entry in self._entries:
            if entry["kind"] in ("SWITCH", "ROLLBACK"):
                self._previous_active[entry["active"]] = entry.get("previous_active")

    # ------------------------------------------------------------------ journal mechanics
    @property
    def entries(self) -> list[dict]:
        return list(self._entries)

    @property
    def last_hash(self) -> str:
        return self._entries[-1]["entry_hash"] if self._entries else GENESIS_HASH

    def _append(self, kind: str, payload: dict) -> dict:
        if kind not in self.KINDS:
            raise VersionError(f"unknown journal entry kind {kind!r}")
        body = {
            "seq": len(self._entries) + 1,
            "kind": kind,
            "recorded_at": utc_now(),
            "prev_hash": self.last_hash,
        }
        body.update(payload)
        entry = dict(body, entry_hash=sha256_text(canonical_json(body)))
        self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(entry) + "\n")
        return entry

    def _load(self, path: Path) -> None:
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise VersionError(f"{path}: line {number} is not JSON: {error}") from error
        previous = GENESIS_HASH
        for number, row in enumerate(rows, start=1):
            if row.get("prev_hash") != previous:
                raise VersionError(f"{path}: line {number} breaks the hash chain")
            body = {key: value for key, value in row.items() if key != "entry_hash"}
            if sha256_text(canonical_json(body)) != row.get("entry_hash"):
                raise VersionError(f"{path}: line {number} has an invalid entry hash")
            previous = row["entry_hash"]
            self._entries.append(row)
            if row["kind"] == "VERSION":
                version = ModelVersion.from_dict(row["version"])
                self._versions[version.version_id] = version
                self._entries_by_version.setdefault(version.version_id, []).append(row["seq"])

    def verify(self) -> tuple[bool, list[str]]:
        problems = []
        previous = GENESIS_HASH
        for entry in self._entries:
            body = {key: value for key, value in entry.items() if key != "entry_hash"}
            if entry["prev_hash"] != previous:
                problems.append(f"seq {entry['seq']}: prev_hash does not match")
            if sha256_text(canonical_json(body)) != entry["entry_hash"]:
                problems.append(f"seq {entry['seq']}: entry_hash does not match")
            previous = entry["entry_hash"]
        for version in self._versions.values():
            problems.extend(f"{version.version_id}: {problem}" for problem in version.validate())
        if self._active is not None and self._active not in self._versions:
            problems.append(f"active version {self._active} is not in the journal")
        return not problems, problems

    def to_jsonl(self) -> str:
        return "".join(canonical_json(entry) + "\n" for entry in self._entries)

    # ------------------------------------------------------------------------- versions
    @property
    def active(self) -> str | None:
        return self._active

    @property
    def versions(self) -> dict[str, ModelVersion]:
        return dict(self._versions)

    def version(self, version_id: str) -> ModelVersion:
        if version_id not in self._versions:
            raise VersionError(f"unknown version {version_id!r}")
        return self._versions[version_id]

    def register(self, version: ModelVersion) -> dict:
        problems = version.validate()
        if problems:
            raise VersionError("; ".join(problems))
        if version.version_id in self._versions:
            raise VersionError(f"version {version.version_id!r} is already registered")
        entry = self._append("VERSION", {"version": version.to_dict()})
        self._versions[version.version_id] = version
        self._entries_by_version.setdefault(version.version_id, []).append(entry["seq"])
        return entry

    def ensure_version(self, version: ModelVersion) -> tuple[dict, bool]:
        """Idempotent registration: a second call with the same version returns the existing entry."""
        if version.version_id in self._versions:
            seq = self._entries_by_version[version.version_id][0]
            return self._entries[seq - 1], False
        return self.register(version), True

    def activate(self, version_id: str, reason: str, observations: int = 0,
                 actor: str = "monitoring") -> dict:
        self.version(version_id)
        if version_id == self._active:
            raise VersionError(f"version {version_id!r} is already active")
        entry = self._append("SWITCH", {
            "active": version_id, "previous_active": self._active, "reason": reason,
            "observations": int(observations), "actor": actor,
            "weights": dict(sorted(self.version(version_id).weights.items())),
        })
        self._previous_active[version_id] = self._active
        self._active = version_id
        return entry

    def rollback(self, reason: str, observations: int = 0, target_version_id: str | None = None,
                 actor: str = "monitoring") -> dict:
        if self._active is None:
            raise VersionError("no active version to roll back")
        target = target_version_id
        if target is None:
            target = self._previous_active.get(self._active)
            if target is None:
                for entry in reversed(self._entries):
                    if entry["kind"] in ("SWITCH", "ROLLBACK") and entry["active"] == self._active:
                        target = entry.get("previous_active")
                        break
        if target is None:
            raise VersionError("no previous active version recorded; pass target_version_id")
        if target == self._active:
            raise VersionError("rollback target equals the active version")
        self.version(target)
        entry = self._append("ROLLBACK", {
            "active": target, "previous_active": self._active, "reason": reason,
            "observations": int(observations), "actor": actor,
            "weights": dict(sorted(self.version(target).weights.items())),
        })
        self._previous_active[target] = self._active
        self._active = target
        return entry

    # ---------------------------------------------------------------------- audit records
    def record_decision(self, decision: dict, actor: str = "monitoring") -> dict:
        """Record a monitoring decision without changing the active version (audit only)."""
        payload = {key: decision[key] for key in decision if key not in ("reasons", "per_loss")}
        payload["action"] = decision.get("action")
        payload["passed"] = bool(decision.get("passed"))
        payload["reasons"] = list(decision.get("reasons", []))
        return self._append("DECISION", payload)

    def record_update(self, scope: str, detail: dict, actor: str = "monitoring") -> dict:
        return self._append("UPDATE", {"scope": scope, "detail": detail, "actor": actor})

    def history(self, kind: str | None = None) -> list[dict]:
        if kind is None:
            return self.entries
        return [entry for entry in self._entries if entry["kind"] == kind]

    def switches(self) -> list[dict]:
        return [entry for entry in self._entries if entry["kind"] in ("SWITCH", "ROLLBACK")]

    def active_weights(self) -> dict:
        if self._active is None:
            raise VersionError("no active version")
        return dict(self.version(self._active).weights)

    def summary(self) -> dict:
        return {
            "entries": len(self._entries),
            "versions": sorted(self._versions),
            "active": self._active,
            "switches": len(self.switches()),
            "last_hash": self.last_hash,
            "valid": self.verify()[0],
        }
