"""MRIO data layer for the AMFE P2 world model (10+ countries x 30+ industries).

The package defines the canonical multi-regional input-output interface consumed by the world
model and the data track (docs/121):

* :mod:`amfe.mrio.schema` -- immutable :class:`~amfe.mrio.schema.MRIODataset`, the accounting
  identities ``x = Z 1 + y`` / ``x = 1' Z + va`` / ``sum(va) = sum(y)``, the validator with the
  P2 scale gate (≥ 10 countries, ≥ 30 industries, ``USD_mn``), and the dated source/licence
  matrix of the candidate providers (OECD ICIO, WIOD, Eurostat FIGARO, UN Comtrade, BEA, Eora).
* :mod:`amfe.mrio.loaders` -- fail-closed local loading from ``AMFE_MRIO_ROOT``, deterministic
  dataset writing, aggregate-only manifests with SHA-256 and the ``AVAILABLE`` / ``BLOCKED_DATA``
  status, plus live (optional) source probes.

No third-party observations live in the repository: the local root is git-ignored, and only
hashes, counts and validation aggregates are written to manifests.  Nothing here is a forecast or
a model result; stage P2 of docs/105 §2.2 is the consumer.
"""
from __future__ import annotations

from .loaders import (
    DATASET_FILENAME,
    DATA_ROOT_ENV,
    DEFAULT_DATASET_ID,
    LoadedDataset,
    available_datasets,
    build_manifest,
    dataset_path,
    load_dataset,
    mrio_root,
    probe_sources,
    require_available,
    sha256_file,
    source_status_summary,
    write_dataset,
    write_manifest,
)
from .schema import (
    ALLOWED_UNITS,
    CANONICAL_UNITS,
    DATASET_STATUSES,
    MIN_COUNTRIES,
    MIN_INDUSTRIES,
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE_BY_ID,
    SOURCE_MATRIX,
    SOURCE_STATUSES,
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    DataUnavailable,
    MRIODataset,
    MRIOError,
    SchemaError,
    SourceEntry,
    ValidationError,
    ValidationReport,
    canonical_bytes,
    check_p2_scale,
    dataset_from_document,
    dataset_to_bytes,
    require_valid,
    source_matrix_document,
    validate_dataset,
)

__all__ = [
    "ALLOWED_UNITS",
    "CANONICAL_UNITS",
    "DATASET_FILENAME",
    "DATASET_STATUSES",
    "DATA_ROOT_ENV",
    "DEFAULT_DATASET_ID",
    "MIN_COUNTRIES",
    "MIN_INDUSTRIES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SOURCE_BY_ID",
    "SOURCE_MATRIX",
    "SOURCE_STATUSES",
    "STATUS_AVAILABLE",
    "STATUS_BLOCKED",
    "DataUnavailable",
    "LoadedDataset",
    "MRIODataset",
    "MRIOError",
    "SchemaError",
    "SourceEntry",
    "ValidationError",
    "ValidationReport",
    "available_datasets",
    "build_manifest",
    "canonical_bytes",
    "check_p2_scale",
    "dataset_from_document",
    "dataset_path",
    "dataset_to_bytes",
    "load_dataset",
    "mrio_root",
    "probe_sources",
    "require_available",
    "require_valid",
    "sha256_file",
    "source_matrix_document",
    "source_status_summary",
    "validate_dataset",
    "write_dataset",
    "write_manifest",
]
