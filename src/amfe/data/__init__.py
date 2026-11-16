"""Data-layer contracts and registries (wave 23, G3, docs/162).

Currently: :mod:`amfe.data.source_registry` — the source registry loader/validator that separates
confirmed (VERIFIED) data sources from catalogued and not-downloaded ones, without network access.
"""

from .source_registry import (  # noqa: F401
    ACCESS,
    EXPORT_SCHEMA,
    STATUSES,
    VINTAGE_SUPPORT,
    SourceRegistryError,
    build_template,
    counts,
    export_for_api,
    find,
    get,
    license_class,
    load,
    missing,
    resolve_assumption_source,
    sources,
    validate_document,
    validate_file,
    validate_source,
)

__all__ = [
    "ACCESS", "EXPORT_SCHEMA", "STATUSES", "VINTAGE_SUPPORT", "SourceRegistryError",
    "build_template", "counts", "export_for_api", "find", "get", "license_class", "load",
    "missing", "resolve_assumption_source", "sources", "validate_document", "validate_file",
    "validate_source",
]
