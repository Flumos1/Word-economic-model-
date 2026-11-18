#!/usr/bin/env python3
"""Deterministic forecasting cache for REAL_TIME evaluation runs (task-42, task-68).

The cache stores only *forecasts* (candidate and comparator-member predictions) and never the PIT
input data or the evaluation result. Its key is a content-addressed identity of one prediction:

* the prediction identity: ``kind`` (candidate/model), name, target, horizon, origin, series_id, mode;
* a version fingerprint: the LF-normalised SHA-256 of the frozen artefacts (protocol v4, R3,
  NF-SIM, comparator composition, NBER announcement dates, SPF metadata), the SHA-256 digest of the
  forecast code (``src/amfe/forecast``, ``src/amfe/candidates``, ``src/amfe/ensemble``), and the
  SHA-256 digest of the private data archive.

Since task-68 the fingerprint also covers the *actual* input root and the run channel
(docs/172, section 4.2):

* ``data_root`` - the resolved root the forecast code reads (``AMFE_DATA_ROOT`` / ``--data-root``,
  otherwise ``data/raw``), labelled relative to the repository root where possible;
* ``data_sha256`` - the digest of the files under *that* root, not of the hard-wired ``data/raw``;
* ``channel`` - ``primary`` (extended/W1_EXT run on the reconstructed root) or ``standard``
  (REAL_TIME-only run); primary and standard predictions never share entries even on one data root;
* ``data_class`` - the requested run class (``REAL_TIME`` / ``REAL_TIME_RECONSTRUCTED``).

Consequences (PIT-safe by construction):

* the same prediction identity *and* environment always maps to the same key, so a cache hit
  returns exactly the value that a fresh run would compute - including its ``data_class``: a cache
  replay cannot relabel a reconstructed forecast as REAL_TIME or vice versa;
* a standard run over ``data/raw`` can no longer be served reconstructed forecasts written by a
  primary run over ``data/reconstructed`` (the defect behind the false 204-origin REAL_TIME slice);
* changing the source data, the data root, the run channel, the model code, the frozen
  protocol/R3/NF-SIM/composition or the NBER/SPF metadata changes the key, i.e. the old entries are
  automatically invalidated (they stay on disk as orphans until ``prune`` removes them);
* an entry written under another schema or fingerprint is never used silently: :meth:`get` counts
  it as ``stale`` and returns a miss, ``strict=True`` raises :class:`CacheProvenanceError`, and
  :meth:`preflight` reports the counts before a run (the runner prints them to stderr and
  ``--cache-strict`` turns them into a hard failure);
* the origin is part of the key, so no future data can be served for an earlier origin: the cache
  only memoises the deterministic output of the same PIT computation.

The module is standalone (no network, no third-party imports beyond the standard library); the
evaluation runner uses it through :class:`ForecastCache`. CLI::

    python tools/eval_cache.py stats  --dir build/eval_cache
    python tools/eval_cache.py audit  --dir build/eval_cache [--strict]
    python tools/eval_cache.py prune  --dir build/eval_cache
    python tools/eval_cache.py clear  --dir build/eval_cache

``audit`` exits 0 when every entry matches the current data root/channel/fingerprint and 3 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_SCHEMA = "eval-cache/1.1.0"
LEGACY_SCHEMAS = ("eval-cache/1.0.0",)
FROZEN_FILES = {
    "protocol_v4": "protocols/evaluation_protocol_v4.json",
    "r3_specification": "protocols/r3_specification.json",
    "nf_sim_thresholds": "protocols/nf_sim_thresholds.json",
    "comparator_composition": "protocols/comparator_composition_2026-11-11.json",
    "nber_dates": "protocols/nber_dates.json",
    "spf_metadata": "protocols/spf_metadata.json",
}
SOURCE_DIRS = ("src/amfe/forecast", "src/amfe/candidates", "src/amfe/ensemble")
DATA_DIR = "data/raw"
DATA_ROOT_ENV = "AMFE_DATA_ROOT"
DATA_SUFFIXES = {".csv", ".zip", ".json"}
CHANNEL_PRIMARY = "primary"
CHANNEL_STANDARD = "standard"


class CacheProvenanceError(RuntimeError):
    """A cache entry belongs to another data root, channel, data class or schema."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lf_sha256(path: Path | None) -> str | None:
    """SHA-256 of the LF-normalised bytes (the project's pin convention)."""
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _jsonable(value):
    """Recursively convert numpy-like payloads into plain JSON values (lists/floats/ints)."""
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    # numpy scalars/arrays and anything else with tolist()/item()
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    if hasattr(value, "item"):
        return _jsonable(value.item())
    return float(value)


def resolve_data_root(root=None, data_root=None) -> Path:
    """Resolve the actual input root: explicit argument, then ``AMFE_DATA_ROOT``, then data/raw."""
    base = Path(root).resolve() if root is not None else ROOT.resolve()
    candidate = data_root if data_root is not None else os.environ.get(DATA_ROOT_ENV)
    if candidate:
        path = Path(candidate)
        return path.resolve() if path.is_absolute() else (base / path).resolve()
    return (base / DATA_DIR).resolve()


def data_root_label(root, data_root) -> str:
    """Stable label of a data root: repository-relative POSIX path when possible."""
    base = Path(root).resolve()
    path = Path(data_root).resolve()
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def channel_for_data_root(data_root) -> str:
    """Derive the run channel from the data root: reconstructed roots are primary runs."""
    path = Path(data_root)
    if "reconstructed" in path.name.lower():
        return CHANNEL_PRIMARY
    manifest = path / "manifest.json"
    if manifest.is_file():
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = {}
        if isinstance(document, dict) and document.get("reconstructed"):
            return CHANNEL_PRIMARY
    return CHANNEL_STANDARD


def data_class_for_channel(channel: str) -> str:
    """Requested run class for a channel (a cache key must never mix the two classes)."""
    return "REAL_TIME_RECONSTRUCTED" if channel == CHANNEL_PRIMARY else "REAL_TIME"


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ForecastCache:
    """Content-addressed cache of forecasting results (one JSON file per prediction).

    ``data_root`` defaults to ``AMFE_DATA_ROOT`` or ``data/raw``; ``channel`` defaults to the one
    derived from that root (``primary`` for reconstructed roots, ``standard`` otherwise). When an
    explicit ``data_root`` contradicts ``AMFE_DATA_ROOT`` the constructor fails closed: the key must
    cover the root the forecast code actually reads.
    """

    def __init__(self, cache_dir, root=None, data_root=None, channel=None, data_class=None,
                 strict=False):
        self.cache_dir = Path(cache_dir)
        self.root = Path(root) if root is not None else ROOT
        env_root = os.environ.get(DATA_ROOT_ENV)
        if data_root is None:
            self.data_root = resolve_data_root(self.root, env_root)
        else:
            self.data_root = resolve_data_root(self.root, data_root)
            if env_root:
                effective = resolve_data_root(self.root, env_root)
                if effective != self.data_root:
                    raise ValueError(
                        f"data_root={self.data_root} contradicts {DATA_ROOT_ENV}={effective}; "
                        "the cache key must cover the root the forecast code reads")
        self.data_root_label = data_root_label(self.root, self.data_root)
        self.channel = channel or channel_for_data_root(self.data_root)
        self.data_class = data_class or data_class_for_channel(self.channel)
        self.strict = bool(strict)
        self._fingerprint: dict | None = None
        self._provenance: dict | None = None
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.corrupt = 0
        self.stale = 0
        self.stale_keys: list = []
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ identity and fingerprint
    def fingerprint(self) -> dict:
        if self._fingerprint is None:
            self._fingerprint = build_fingerprint(
                self.root, data_root=self.data_root, channel=self.channel,
                data_class=self.data_class)
        return self._fingerprint

    def environment(self) -> dict:
        return {"data_root": self.data_root_label, "channel": self.channel,
                "data_class": self.data_class}

    def key(self, *, kind: str, name: str, target: str, horizon: int, origin,
            series_id=None, mode: str = "auto") -> str:
        return cache_key(self.fingerprint(), kind=kind, name=name, target=target,
                         horizon=horizon, origin=origin, series_id=series_id, mode=mode)

    def identity(self, *, kind: str, name: str, target: str, horizon: int, origin,
                 series_id=None, mode: str = "auto") -> dict:
        return {"kind": kind, "name": name, "target": target, "horizon": int(horizon),
                "origin": str(origin), "series_id": series_id, "mode": mode}

    def _entry_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    # ------------------------------------------------------------------------------- cache access
    def _mismatch_reason(self, entry: dict, key: str) -> str | None:
        """Why an entry may not be replayed, or ``None`` when it matches this cache exactly."""
        schema = entry.get("schema")
        if schema != CACHE_SCHEMA:
            if schema in LEGACY_SCHEMAS:
                return f"legacy schema {schema}"
            return f"foreign schema {schema}"
        if entry.get("key") != key or not isinstance(entry.get("payload"), dict):
            return "corrupt"
        stored = entry.get("fingerprint")
        if not isinstance(stored, dict):
            return "missing fingerprint"
        if _canonical(stored) != _canonical(self.fingerprint()):
            return "fingerprint mismatch (data root/channel/data class/versions)"
        return None

    def get(self, key: str) -> dict | None:
        path = self._entry_path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            self.corrupt += 1
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            self.corrupt += 1
            return None
        if not isinstance(entry, dict):
            self.corrupt += 1
            return None
        reason = self._mismatch_reason(entry, key)
        if reason is None:
            return entry["payload"]
        if reason == "corrupt":
            self.corrupt += 1
            return None
        self.stale += 1
        if key not in self.stale_keys:
            self.stale_keys.append(key)
        if self.strict:
            raise CacheProvenanceError(
                f"cache entry {path.name} rejected: {reason}; "
                f"data_root={self.data_root_label}, channel={self.channel}, "
                f"data_class={self.data_class}; run 'python tools/eval_cache.py prune'")
        return None

    def put(self, key: str, payload: dict, *, kind: str, name: str, target: str, horizon: int,
            origin, series_id=None, mode: str = "auto") -> None:
        entry = {
            "schema": CACHE_SCHEMA,
            "key": key,
            "created_at": utc_now(),
            "environment": self.environment(),
            "fingerprint": self.fingerprint(),
            "identity": self.identity(kind=kind, name=name, target=target, horizon=horizon,
                                      origin=origin, series_id=series_id, mode=mode),
            "payload": _jsonable(payload),
        }
        path = self._entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                             encoding="utf-8", newline="\n")
        temporary.replace(path)
        self.writes += 1
        self._provenance = None

    def forecast(self, compute, *, kind: str, name: str, target: str, horizon: int, origin,
                 series_id=None, mode: str = "auto") -> tuple:
        """Return ``(payload, hit)``: a cache hit or the freshly computed and stored payload.

        The data root, channel and data class are bound to this cache instance, so a replay cannot
        substitute a payload produced for another root/channel/class.
        """
        key = self.key(kind=kind, name=name, target=target, horizon=horizon, origin=origin,
                       series_id=series_id, mode=mode)
        cached = self.get(key)
        if cached is not None:
            self.hits += 1
            return cached, True
        self.misses += 1
        payload = _jsonable(compute())
        self.put(key, payload, kind=kind, name=name, target=target, horizon=horizon,
                 origin=origin, series_id=series_id, mode=mode)
        return payload, False

    # ------------------------------------------------------------------------------ maintenance
    def _entries(self) -> list:
        return sorted(self.cache_dir.glob("*/*.json"))

    def provenance(self, refresh: bool = False) -> dict:
        """Classify every entry: ``current``, ``legacy``, ``foreign`` or ``corrupt``."""
        if self._provenance is not None and not refresh:
            return self._provenance
        current_fingerprint = _canonical(self.fingerprint())
        counts = {"current": 0, "legacy": 0, "foreign": 0, "corrupt": 0}
        for path in self._entries():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                counts["corrupt"] += 1
                continue
            if not isinstance(entry, dict):
                counts["corrupt"] += 1
                continue
            schema = entry.get("schema")
            if schema != CACHE_SCHEMA:
                counts["legacy" if schema in LEGACY_SCHEMAS else "foreign"] += 1
            elif _canonical(entry.get("fingerprint")) != current_fingerprint:
                counts["foreign"] += 1
            elif entry.get("key") != path.stem:
                counts["corrupt"] += 1
            else:
                counts["current"] += 1
        report = {
            "dir": str(self.cache_dir),
            "entries": sum(counts.values()),
            "data_root": self.data_root_label,
            "channel": self.channel,
            "data_class": self.data_class,
            "schema": CACHE_SCHEMA,
            **counts,
        }
        report["ok"] = counts["legacy"] + counts["foreign"] + counts["corrupt"] == 0
        self._provenance = report
        return report

    def preflight(self, strict: bool | None = None) -> dict:
        """Report provenance and, under ``strict``, fail on any entry of another provenance."""
        report = self.provenance(refresh=True)
        fail = self.strict if strict is None else bool(strict)
        if fail and not report["ok"]:
            raise CacheProvenanceError(
                f"{report['entries'] - report['current']} cache entries do not match the current "
                f"provenance (data_root={report['data_root']}, channel={report['channel']}, "
                f"data_class={report['data_class']}): legacy={report['legacy']}, "
                f"foreign={report['foreign']}, corrupt={report['corrupt']}; they are ignored; run "
                "'python tools/eval_cache.py prune --dir <dir>' to remove them")
        return report

    def stats(self) -> dict:
        entries = self._entries()
        size = 0
        for path in entries:
            try:
                size += path.stat().st_size
            except OSError:
                continue
        return {"schema": CACHE_SCHEMA, "dir": str(self.cache_dir), "entries": len(entries),
                "bytes": size, "hits": self.hits, "misses": self.misses,
                "writes": self.writes, "corrupt": self.corrupt, "stale": self.stale,
                "data_root": self.data_root_label, "channel": self.channel,
                "data_class": self.data_class, "fingerprint": self.fingerprint(),
                "provenance": self.provenance()}

    def clear(self) -> int:
        removed = 0
        for path in self._entries():
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        for directory in sorted(self.cache_dir.glob("*")):
            try:
                directory.rmdir()
            except OSError:
                continue
        self._provenance = None
        return removed

    def prune(self) -> int:
        """Remove entries produced under a different version/data/channel fingerprint."""
        current = _canonical(self.fingerprint())
        removed = 0
        for path in self._entries():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or _canonical(entry.get("fingerprint")) != current:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        self._provenance = None
        return removed


# ---------------------------------------------------------------------------------- fingerprinting
def _digest_files(base: Path, files) -> str:
    digest = hashlib.sha256()
    for path in files:
        try:
            content = path.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue
        digest.update(str(path.relative_to(base)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def build_fingerprint(root=None, data_root=None, channel=None, data_class=None) -> dict:
    """Content fingerprint of the frozen versions, forecast code and the actual PIT data root.

    ``data_root`` is the root the forecast code reads (``AMFE_DATA_ROOT`` when omitted); the digest
    covers that root, and ``data_root``/``channel``/``data_class`` are part of the fingerprint, so
    primary (reconstructed) and standard (data/raw) caches never share keys.
    """
    root = Path(root) if root is not None else ROOT
    resolved = resolve_data_root(root, data_root)
    channel = channel or channel_for_data_root(resolved)
    data_class = data_class or data_class_for_channel(channel)
    versions = {name: lf_sha256(root / relative) for name, relative in FROZEN_FILES.items()}
    source_files = []
    for directory in SOURCE_DIRS:
        source_files.extend(sorted((root / directory).glob("*.py")))
    data_files = sorted(path for path in resolved.rglob("*")
                        if path.is_file() and path.suffix.lower() in DATA_SUFFIXES) \
        if resolved.is_dir() else []
    return {
        "cache_schema": CACHE_SCHEMA,
        "versions": versions,
        "source_sha256": _digest_files(root, source_files),
        "source_files": len(source_files),
        "data_root": data_root_label(root, resolved),
        "channel": channel,
        "data_class": data_class,
        "data_sha256": _digest_files(resolved, data_files),
        "data_files": len(data_files),
    }


def cache_key(fingerprint: dict, *, kind: str, name: str, target: str, horizon: int, origin,
              series_id=None, mode: str = "auto") -> str:
    payload = {
        "schema": CACHE_SCHEMA,
        "fingerprint": fingerprint,
        "identity": {"kind": kind, "name": name, "target": target, "horizon": int(horizon),
                     "origin": str(origin), "series_id": series_id, "mode": mode},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Forecast cache maintenance (offline)")
    parser.add_argument("command", choices=("stats", "audit", "clear", "prune"))
    parser.add_argument("--dir", required=True, help="cache directory")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    parser.add_argument("--data-root", default=None,
                        help="actual input root (default: AMFE_DATA_ROOT or data/raw)")
    parser.add_argument("--channel", default=None, help="run channel (primary/standard)")
    parser.add_argument("--data-class", dest="data_class", default=None,
                        help="requested run class (REAL_TIME/REAL_TIME_RECONSTRUCTED)")
    parser.add_argument("--strict", action="store_true",
                        help="fail (exit 3 on audit) when entries have another provenance")
    args = parser.parse_args(argv)
    try:
        cache = ForecastCache(Path(args.dir), args.root, data_root=args.data_root,
                              channel=args.channel, data_class=args.data_class,
                              strict=args.strict)
    except ValueError as exc:  # explicit --data-root contradicts AMFE_DATA_ROOT
        parser.error(str(exc))
    code = 0
    if args.command == "stats":
        result = cache.stats()
    elif args.command == "audit":
        result = cache.provenance(refresh=True)
        code = 0 if result["ok"] else 3
    elif args.command == "clear":
        result = {"removed": cache.clear(), "dir": str(cache.cache_dir)}
    else:
        result = {"removed": cache.prune(), "dir": str(cache.cache_dir),
                  "fingerprint": cache.fingerprint()["data_sha256"][:16]}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
