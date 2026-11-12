#!/usr/bin/env python3
"""Deterministic forecasting cache for REAL_TIME evaluation runs (task-42).

The cache stores only *forecasts* (candidate and comparator-member predictions) and never the PIT
input data or the evaluation result. Its key is a content-addressed identity of one prediction:

* the prediction identity: ``kind`` (candidate/model), name, target, horizon, origin, series_id, mode;
* a version fingerprint: the LF-normalised SHA-256 of the frozen artefacts (protocol v4, R3,
  NF-SIM, comparator composition, NBER announcement dates, SPF metadata), the SHA-256 digest of the
  forecast code (``src/amfe/forecast``, ``src/amfe/candidates``, ``src/amfe/ensemble``), and the
  SHA-256 digest of the private data archive (``data/raw/**.csv|zip|json``).

Consequences (PIT-safe by construction):

* the same prediction identity always maps to the same key, so a cache hit returns exactly the value
  that a fresh run would compute;
* changing the source data, the model code, the frozen protocol/R3/NF-SIM/composition or the
  NBER/SPF metadata changes the key, i.e. the old entries are automatically invalidated (they stay on
  disk as orphans until ``prune`` removes them);
* the origin is part of the key, so no future data can be served for an earlier origin: the cache
  only memoises the deterministic output of the same PIT computation.

The module is standalone (no network, no third-party imports beyond the standard library); the
evaluation runner uses it through :class:`ForecastCache`. CLI::

    python tools/eval_cache.py stats  --dir build/eval_cache
    python tools/eval_cache.py prune  --dir build/eval_cache
    python tools/eval_cache.py clear  --dir build/eval_cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_SCHEMA = "eval-cache/1.0.0"
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
DATA_SUFFIXES = {".csv", ".zip", ".json"}


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


class ForecastCache:
    """Content-addressed cache of forecasting results (one JSON file per prediction)."""

    def __init__(self, cache_dir, root=None):
        self.cache_dir = Path(cache_dir)
        self.root = Path(root) if root is not None else ROOT
        self._fingerprint: dict | None = None
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.corrupt = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ identity and fingerprint
    def fingerprint(self) -> dict:
        if self._fingerprint is None:
            self._fingerprint = build_fingerprint(self.root)
        return self._fingerprint

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
        if (not isinstance(entry, dict) or entry.get("schema") != CACHE_SCHEMA
                or entry.get("key") != key or not isinstance(entry.get("payload"), dict)):
            self.corrupt += 1
            return None
        return entry["payload"]

    def put(self, key: str, payload: dict, *, kind: str, name: str, target: str, horizon: int,
            origin, series_id=None, mode: str = "auto") -> None:
        entry = {
            "schema": CACHE_SCHEMA,
            "key": key,
            "created_at": utc_now(),
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

    def forecast(self, compute, *, kind: str, name: str, target: str, horizon: int, origin,
                 series_id=None, mode: str = "auto") -> tuple:
        """Return ``(payload, hit)``: a cache hit or the freshly computed and stored payload."""
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
                "writes": self.writes, "corrupt": self.corrupt,
                "fingerprint": self.fingerprint()}

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
        return removed

    def prune(self) -> int:
        """Remove entries produced under a different version/data fingerprint."""
        current = json.dumps(self.fingerprint(), sort_keys=True)
        removed = 0
        for path in self._entries():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if json.dumps(entry.get("fingerprint"), sort_keys=True) != current:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
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


def build_fingerprint(root=None) -> dict:
    """Content fingerprint of the frozen versions, the forecast code and the PIT data archive."""
    root = Path(root) if root is not None else ROOT
    versions = {name: lf_sha256(root / relative) for name, relative in FROZEN_FILES.items()}
    source_files = []
    for directory in SOURCE_DIRS:
        source_files.extend(sorted((root / directory).glob("*.py")))
    data_dir = root / DATA_DIR
    data_files = sorted(path for path in data_dir.rglob("*")
                        if path.is_file() and path.suffix.lower() in DATA_SUFFIXES) \
        if data_dir.is_dir() else []
    return {
        "cache_schema": CACHE_SCHEMA,
        "versions": versions,
        "source_sha256": _digest_files(root, source_files),
        "source_files": len(source_files),
        "data_sha256": _digest_files(root, data_files),
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
    parser.add_argument("command", choices=("stats", "clear", "prune"))
    parser.add_argument("--dir", required=True, help="cache directory")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    args = parser.parse_args(argv)
    cache = ForecastCache(Path(args.dir), args.root)
    if args.command == "stats":
        result = cache.stats()
    elif args.command == "clear":
        result = {"removed": cache.clear(), "dir": str(cache.cache_dir)}
    else:
        result = {"removed": cache.prune(), "dir": str(cache.cache_dir),
                  "fingerprint": cache.fingerprint()["data_sha256"][:16]}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
