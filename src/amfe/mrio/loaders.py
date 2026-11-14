"""Fail-closed local loaders, manifest builder and live source probes for MRIO data (docs/121).

Design rules
------------
* **No third-party observations in git.**  The canonical dataset lives in a local directory
  (env ``AMFE_MRIO_ROOT`` or an explicit argument); the repository stores only this code, the
  schema, synthetic fixtures and manifests with hashes/aggregates.
* **Fail-closed.**  A missing directory, a missing ``dataset.json``, an unreadable file, a
  SHA-256 mismatch, an unknown schema or a failed accounting identity all raise instead of
  returning a partial table.  The CLI maps this to the ``BLOCKED_DATA`` status.
* **Deterministic hashing.**  :func:`write_dataset` and :func:`sha256_file` are byte-stable, so a
  manifest can be compared across runs; only ``generated_at``/``checked_at`` timestamps change.
* **Manifest is aggregate-only.**  It contains hashes, counts, years, units, checks and
  country-level totals from the validator; never the ``Z`` matrix or raw observations.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .schema import (
    CANONICAL_UNITS,
    MIN_COUNTRIES,
    MIN_INDUSTRIES,
    MRIODataset,
    MRIOError,
    SCHEMA_VERSION,
    SOURCE_MATRIX,
    SourceEntry,
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    DataUnavailable,
    ValidationError,
    ValidationReport,
    dataset_from_document,
    dataset_to_bytes,
    validate_dataset,
)

#: Environment variable that points at the local MRIO data root.
DATA_ROOT_ENV = "AMFE_MRIO_ROOT"
#: Canonical file name of a dataset under ``<root>/<dataset_id>/``.
DATASET_FILENAME = "dataset.json"
MANIFEST_SCHEMA = "amfe.mrio.manifest.v1"
DEFAULT_DATASET_ID = "default"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _generated_at() -> str:
    """Reproducible stamp: ``AMFE_GENERATED_AT`` wins (tests/CI), else current UTC time."""
    override = os.environ.get("AMFE_GENERATED_AT", "").strip()
    return override or _utc_now()


# --------------------------------------------------------------------------------------
# Local root and paths
# --------------------------------------------------------------------------------------
def mrio_root(root=None) -> Path:
    """Resolve the MRIO data root: explicit argument, else ``AMFE_MRIO_ROOT``.

    Fail-closed: an unset environment variable or a missing directory raises
    :class:`DataUnavailable` with the ``BLOCKED_DATA`` reason, so a caller never silently
    computes on an empty table.
    """
    if root is not None:
        path = Path(root)
        if not path.exists():
            raise DataUnavailable(f"{STATUS_BLOCKED}: data root does not exist: {path}")
        if not path.is_dir():
            raise DataUnavailable(f"{STATUS_BLOCKED}: data root is not a directory: {path}")
        return path
    env = os.environ.get(DATA_ROOT_ENV, "").strip()
    if not env:
        raise DataUnavailable(
            f"{STATUS_BLOCKED}: environment variable {DATA_ROOT_ENV} is not set; "
            "download an MRIO table (docs/121 §3) or pass --root"
        )
    path = Path(env)
    if not path.exists() or not path.is_dir():
        raise DataUnavailable(
            f"{STATUS_BLOCKED}: {DATA_ROOT_ENV}={env!r} is not an existing directory"
        )
    return path


def dataset_path(dataset_id: str = DEFAULT_DATASET_ID, root=None) -> Path:
    """Path of ``<root>/<dataset_id>/dataset.json`` with a strict dataset id."""
    candidate = Path(dataset_id)
    if candidate.suffix.lower() == ".json" or candidate.is_file():
        return candidate
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise DataUnavailable(f"{STATUS_BLOCKED}: empty dataset id")
    if any(separator in dataset_id for separator in ("/", "\\", "..")):
        raise DataUnavailable(f"{STATUS_BLOCKED}: invalid dataset id {dataset_id!r}")
    return mrio_root(root) / dataset_id / DATASET_FILENAME


def available_datasets(root=None) -> list[str]:
    """Sorted ids of the datasets present under the root (``<id>/dataset.json``)."""
    path = mrio_root(root)
    found = []
    for child in sorted(path.iterdir()):
        if child.is_dir() and (child / DATASET_FILENAME).is_file():
            found.append(child.name)
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_dataset(dataset: MRIODataset, path: Path) -> str:
    """Write the dataset deterministically and return the SHA-256 of the written bytes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dataset_to_bytes(dataset)
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LoadedDataset:
    """A dataset that passed the loader gate, with its provenance and validation report."""

    dataset: MRIODataset
    report: ValidationReport
    path: Path
    sha256: str
    size_bytes: int

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.size_bytes,
            "dataset": self.dataset.describe(),
            "validation": self.report.as_dict(),
        }


def load_dataset(dataset_id: str = DEFAULT_DATASET_ID, root=None,
                 expected_sha256: str | None = None,
                 enforce_p2_scale: bool = True,
                 strict: bool = True) -> LoadedDataset:
    """Load and validate one local dataset; every failure is fail-closed.

    Raises :class:`DataUnavailable` when the file is absent/unreadable or its hash differs from
    ``expected_sha256``, :class:`SchemaError` when the document violates the canonical schema, and
    :class:`ValidationError` (``strict=True``) when an accounting identity or the P2 gate fails.
    """
    path = dataset_path(dataset_id, root)
    if not path.is_file():
        raise DataUnavailable(
            f"{STATUS_BLOCKED}: dataset file not found: {path}; expected an MRIO table converted "
            "to the canonical schema (docs/121 §3)"
        )
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest.lower() != str(expected_sha256).lower():
        raise DataUnavailable(
            f"{STATUS_BLOCKED}: SHA-256 mismatch for {path}: expected {expected_sha256}, "
            f"got {digest}"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"{STATUS_BLOCKED}: {path} is not valid UTF-8 JSON: {exc}") from exc
    dataset = dataset_from_document(document)
    report = validate_dataset(dataset, enforce_p2_scale=enforce_p2_scale)
    if strict and not report.ok:
        raise ValidationError(
            f"{path}: dataset rejected by the validator: " + "; ".join(report.errors[:5])
        )
    return LoadedDataset(dataset=dataset, report=report, path=path, sha256=digest,
                         size_bytes=len(raw))


def require_available(root=None, enforce_p2_scale: bool = True) -> LoadedDataset:
    """The first valid dataset under the root, or :class:`DataUnavailable` (fail-closed)."""
    ids = available_datasets(root)
    if not ids:
        raise DataUnavailable(
            f"{STATUS_BLOCKED}: no '<dataset_id>/{DATASET_FILENAME}' under {mrio_root(root)}"
        )
    last: Exception | None = None
    for dataset_id in ids:
        try:
            return load_dataset(dataset_id, root, enforce_p2_scale=enforce_p2_scale)
        except MRIOError as exc:  # try the next candidate, remember the failure
            last = exc
    raise DataUnavailable(f"{STATUS_BLOCKED}: no usable dataset under {mrio_root(root)}: {last}")


# --------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------
def _dataset_entry(dataset_id: str, root, enforce_p2_scale: bool) -> dict:
    entry: dict = {"dataset_id": dataset_id, "status": STATUS_BLOCKED, "reason": None}
    try:
        loaded = load_dataset(dataset_id, root, enforce_p2_scale=enforce_p2_scale)
    except MRIOError as exc:
        entry["reason"] = str(exc)
        return entry
    report = loaded.report
    metrics = report.metrics
    entry.update({
        "status": STATUS_AVAILABLE,
        "reason": None,
        "path": str(loaded.path),
        "sha256": loaded.sha256,
        "bytes": loaded.size_bytes,
        "countries": metrics["countries"],
        "industries": metrics["industries"],
        "years": metrics["years"],
        "units": metrics["units"],
        "dataset_digest": metrics["digest"],
        "validation": {
            "ok": report.ok,
            "checks": report.checks,
            "warnings": list(report.warnings),
            "max_abs_row_residual": metrics["max_abs_row_residual"],
            "max_abs_column_residual": metrics["max_abs_column_residual"],
            "global_difference_va_minus_y": metrics["global_difference_va_minus_y"],
            "total_output": metrics["total_output"],
            "total_value_added": metrics["total_value_added"],
            "net_exports_sum": metrics["net_exports_sum"],
        },
    })
    return entry


def build_manifest(root=None, dataset_ids: Sequence[str] | None = None,
                   enforce_p2_scale: bool = True,
                   include_sources: bool = True) -> dict:
    """Aggregate-only manifest of the local MRIO data root (hashes, counts, checks, status)."""
    root_path: Path | None
    root_error: str | None = None
    try:
        root_path = mrio_root(root)
        discovered = available_datasets(root_path)
    except DataUnavailable as exc:
        root_path = None
        root_error = str(exc)
        discovered = []
    ids = [str(item) for item in (dataset_ids if dataset_ids is not None else discovered)]
    entries = []
    for dataset_id in ids:
        if root_path is None:
            entries.append({"dataset_id": dataset_id, "status": STATUS_BLOCKED,
                            "reason": root_error})
            continue
        entries.append(_dataset_entry(dataset_id, root_path, enforce_p2_scale))

    available = [entry for entry in entries if entry["status"] == STATUS_AVAILABLE]
    document = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _generated_at(),
        "data_root": str(root_path) if root_path is not None else None,
        "data_root_env": DATA_ROOT_ENV,
        "status": STATUS_AVAILABLE if available else STATUS_BLOCKED,
        "reason": None if available else (root_error or (
            "no dataset under the data root: the P2 MRIO table is not available locally "
            "(docs/121 §3-4)"
        )),
        "p2_requirements": {
            "min_countries": MIN_COUNTRIES,
            "min_industries": MIN_INDUSTRIES,
            "units": CANONICAL_UNITS,
            "schema": "amfe.mrio.dataset.v1",
        },
        "datasets": entries,
    }
    if include_sources:
        document["sources"] = [entry.as_dict() for entry in SOURCE_MATRIX]
    return document


def write_manifest(document: Mapping, path: Path) -> str:
    """Write a manifest deterministically (sorted keys, 2-space indent) and return its SHA-256."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
               + "\n").encode("utf-8")
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------------------
# Live source probes (network; facts are recorded, never invented)
# --------------------------------------------------------------------------------------
def _probe_one(url: str, timeout: float) -> dict:
    started = time.monotonic()
    result: dict = {"url": url, "http_status": None, "reachable": False,
                    "error": None, "bytes_read": 0}
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "AMFE-MRIO-data-check/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            chunk = response.read(2048)
            result.update({"http_status": status, "reachable": 200 <= int(status) < 400,
                           "bytes_read": len(chunk)})
    except urllib.error.HTTPError as exc:
        result.update({"http_status": exc.code, "reachable": False, "error": f"HTTPError {exc.code}"})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result.update({"error": f"{type(exc).__name__}: {exc}"})
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return result


def probe_sources(entries: Iterable[SourceEntry] | None = None, network: bool = True,
                  timeout: float = 8.0) -> list[dict]:
    """Probe every candidate source URL; offline mode reports ``network=false`` explicitly."""
    if timeout <= 0:
        raise DataUnavailable("probe timeout must be positive")
    out = []
    for entry in (SOURCE_MATRIX if entries is None else entries):
        record = {"source_id": entry.source_id, "status_recorded": entry.status,
                  "checked_at_recorded": entry.checked_at, "network": bool(network)}
        if network:
            record["probe"] = _probe_one(entry.url, timeout)
        else:
            record["probe"] = {"skipped": "network disabled (--no-network)", "reachable": None,
                               "http_status": None}
        out.append(record)
    return out


def source_status_summary(probes: Sequence[Mapping] | None = None) -> dict:
    """Counts by recorded status, plus the number of probed URLs that are reachable now."""
    counts: dict[str, int] = {}
    for entry in SOURCE_MATRIX:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    summary = {
        "sources": len(SOURCE_MATRIX),
        "by_status": dict(sorted(counts.items())),
        "with_data_api_no_key": [entry.source_id for entry in SOURCE_MATRIX
                                 if entry.status == "AVAILABLE_API"],
        "blocked": [entry.source_id for entry in SOURCE_MATRIX
                    if entry.status.startswith("BLOCKED")],
    }
    if probes is not None:
        summary["probed"] = len(probes)
        summary["reachable_now"] = sum(
            1 for probe in probes
            if isinstance(probe.get("probe"), Mapping) and probe["probe"].get("reachable")
        )
    return summary
