#!/usr/bin/env python3
"""External forecasts registry v2: validation, coverage and scorecard comparison.

Question
--------
docs/173 (P-003/P-004) asks for **20+ external forecast sources**, not for 20 downloaded
series.  The honest first step is a registry that says, for every source, what it forecasts,
how often, on which horizon, under which licence, with which publication/vintage policy, and
whether the project can actually score it.  This tool validates that registry
(``protocols/external_forecasts_v2.json``), reports coverage by status/target/licence and
compares the catalogue with the current scorecard (``protocols/external_forecast_scorecard.json``,
one scored source).  It changes no number in the scorecard: the scorecard stays the only place
where errors are computed, and this report only says what is ready, what is blocked and why.

What the tool refuses to do
---------------------------
* It never downloads anything: there is no ``urllib``/``requests``/``socket`` import and no
  network call, so a registry entry can never be "verified" here by fetching a page.
* It never reads or invents a forecast value.  The registry carries metadata only; a
  value-like key (``values``, ``series``, ``observations``, ``forecast`` ...) or a numeric
  series is a hard validation error, not a warning.
* It never upgrades a source: a source without an adapter, without a licence-register entry
  or without a local data path cannot be ``ADAPTER_READY``; it is ``NOT_FETCHED``,
  ``UNAVAILABLE`` or ``RESTRICTED``.
* It fails closed: an invalid registry exits 2 and writes no artefact.  A valid registry that
  is merely partial exits 0 (``--strict`` turns that into exit 3) because "1 of 26 sources is
  scorable" is the finding, not a tool failure.

Status vocabulary (see the registry's own ``statuses`` block)
-------------------------------------------------------------
``ADAPTER_READY``  adapter implemented + licence allows use + local data path registered
``UNAVAILABLE``    non-licence blocker: no archive in the repository, or no protocol
                   target/horizon mapping
``RESTRICTED``     subscription / members-only / news terms / unverified licence: never read
``NOT_FETCHED``    licence allows use, mapping exists or is pending, but no local data yet

Determinism
-----------
No timestamp is written and no clock is read: two runs over the same registry produce
byte-identical JSON and Markdown.  ``content_sha256`` covers the whole report except itself.

Usage
-----
    python tools/external_scorecard_v2.py --check            # validate, print summary (0 / 2)
    python tools/external_scorecard_v2.py --check --strict   # also require status OK (0 / 3)
    python tools/external_scorecard_v2.py --json             # full report on stdout
    python tools/external_scorecard_v2.py --md --out reports/external_forecasts_v2.md
    python tools/external_scorecard_v2.py --out reports/external_forecasts_v2.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

TOOL = "tools/external_scorecard_v2.py"
ENGINE_VERSION = "0.1.0"
REPORT_ID = "AMFE-EXTERNAL-FORECASTS-V2-COVERAGE"
REGISTRY_SCHEMA = "AMFE-EXTERNAL-FORECASTS-V2/1.0.0"
REGISTRY_ID = "AMFE-EXTERNAL-FORECASTS-V2"

REGISTRY_PATH = ROOT / "protocols" / "external_forecasts_v2.json"
SCORECARD_PATH = ROOT / "protocols" / "external_forecast_scorecard.json"
LICENCE_PATH = ROOT / "protocols" / "licence_register.json"

#: docs/173 P-003: at least 20 catalogued sources; docs/173 P0-1: at least 3 scored sources.
MIN_SOURCES = 20
MIN_READY_SOURCES = 3

TARGETS = ("CPI", "GDP", "UNRATE", "RECESSION")
STATUSES = ("ADAPTER_READY", "UNAVAILABLE", "RESTRICTED", "NOT_FETCHED")
LICENCE_STATUSES = ("OPEN", "OPEN_ATTRIBUTION", "RESTRICTED", "UNVERIFIED")
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "SEMIANNUAL", "ANNUAL", "IRREGULAR")
MAPPINGS = ("EXACT", "PENDING", "NONE")
ADAPTER_STATES = ("IMPLEMENTED", "PLANNED", "NOT_APPLICABLE")
PROVIDER_TYPES = (
    "CENTRAL_BANK",
    "FEDERAL_AGENCY",
    "INTERNATIONAL_ORGANISATION",
    "GOVERNMENT",
    "PRIVATE_VENDOR",
    "MEDIA",
    "ACADEMIC",
    "RESEARCH_INSTITUTE",
)

#: Adapters that exist in tools/external_forecast_scorecard.py (task-54).  A registry entry may
#: claim IMPLEMENTED only with one of these names; adding a new name here requires the adapter too.
IMPLEMENTED_ADAPTERS = ("spf_local", "ny_fed_local", "metadata_only", "restricted")

STATUS_REASONS = {
    "ADAPTER_READY": ("SCORED_IN_CURRENT_SCORECARD", "ADAPTER_IMPLEMENTED_DATA_LOCAL"),
    "UNAVAILABLE": (
        "ARCHIVE_NOT_LOCAL",
        "NO_PROTOCOL_HORIZON_MAPPING",
        "NO_PROTOCOL_TARGET_MAPPING",
        "NO_EVENT_PROBABILITY_MAPPING",
    ),
    "RESTRICTED": (
        "LICENCE_RESTRICTED",
        "SUBSCRIPTION_ONLY",
        "MEMBERS_ONLY",
        "NEWS_TERMS_NO_ARCHIVE",
        "LICENCE_UNVERIFIED",
    ),
    "NOT_FETCHED": ("PUBLIC_ARCHIVE_NOT_FETCHED", "MAPPING_PENDING"),
}

#: Reasons that mean "there is no protocol target/horizon at all", so the mapping must be NONE.
NO_MAPPING_REASONS = frozenset(
    ("NO_PROTOCOL_HORIZON_MAPPING", "NO_PROTOCOL_TARGET_MAPPING", "NO_EVENT_PROBABILITY_MAPPING")
)

#: Key names that would put raw third-party values into the registry (metadata only).
RAW_VALUE_KEYS = frozenset(
    (
        "values",
        "series",
        "observations",
        "rows",
        "history",
        "data",
        "forecast",
        "forecasts",
        "actual",
        "actuals",
        "points",
        "samples",
        "quantiles",
        "probabilities",
        "vintage_values",
        "forecast_values",
        "releases",
    )
)

REQUIRED_SOURCE_FIELDS = (
    "id",
    "provider",
    "provider_type",
    "target",
    "frequency",
    "horizon",
    "publication",
    "licence",
    "status",
    "status_reason",
    "adapter",
    "adapter_state",
    "data_path",
    "url",
    "notes",
)
REQUIRED_TEXT_FIELDS = ("id", "provider", "url", "notes")


class RegistryError(RuntimeError):
    """The registry could not be read at all (missing file, invalid JSON, wrong shape)."""


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relpath(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def _load_json(path: Path, what: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"cannot read {what} {path}: {exc}") from exc
    except ValueError as exc:
        raise RegistryError(f"{what} {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError(f"{what} {path} must be a JSON object, got {type(payload).__name__}")
    return payload


def load_registry(path: Path | str | None = None) -> dict:
    """Read the registry; a missing/invalid file raises :class:`RegistryError`."""
    return _load_json(Path(path) if path is not None else REGISTRY_PATH, "registry")


def load_scorecard(path: Path | str | None = None) -> dict | None:
    """Read the current scorecard report, or ``None`` when it has not been generated."""
    target = Path(path) if path is not None else SCORECARD_PATH
    if not target.exists():
        return None
    return _load_json(target, "scorecard report")


def load_licence_register(path: Path | str | None = None) -> dict | None:
    """Read the licence register, or ``None`` when it is absent (a warning, not an error)."""
    target = Path(path) if path is not None else LICENCE_PATH
    if not target.exists():
        return None
    payload = _load_json(target, "licence register")
    if not isinstance(payload.get("entries"), list):
        raise RegistryError(f"licence register {target} has no entries list")
    return payload


def register_entry(register: Mapping | None, entry_id: str) -> dict | None:
    if not register:
        return None
    for entry in register.get("entries") or []:
        if entry.get("id") == entry_id:
            return dict(entry)
    return None


def _licence_rank(status: str) -> int:
    """How permissive a licence status is; a stricter registry entry is always allowed."""
    return {"UNVERIFIED": 0, "RESTRICTED": 1, "OPEN_ATTRIBUTION": 3, "OPEN": 3}.get(status, -1)


# ----------------------------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------------------------
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _walk_raw_values(node, path: str, errors: list[str]) -> None:
    """Reject value-like keys and numeric series anywhere in the registry."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            key_text = str(key)
            here = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in RAW_VALUE_KEYS:
                errors.append(
                    f"RAW_VALUES_NOT_ALLOWED: {here}: the registry stores metadata only; "
                    f"'{key_text}' would put third-party values into a published artefact"
                )
                continue
            _walk_raw_values(value, here, errors)
    elif isinstance(node, (list, tuple)):
        numbers = [item for item in node if _is_number(item)]
        if len(node) > 4 and len(numbers) == len(node):
            errors.append(
                f"RAW_VALUES_NOT_ALLOWED: {path}: a numeric series of {len(node)} elements "
                f"is not registry metadata"
            )
            return
        for index, item in enumerate(node):
            _walk_raw_values(item, f"{path}[{index}]", errors)


def _check_source(source: Mapping, index: int, errors: list[str]) -> None:
    where = f"sources[{index}]"
    if not isinstance(source, Mapping):
        errors.append(f"BAD_TYPE: {where}: a source must be an object")
        return
    for field in REQUIRED_SOURCE_FIELDS:
        if field not in source:
            errors.append(f"MISSING_FIELD: {where}: '{field}' is required")
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        errors.append(f"BAD_TYPE: {where}.id: a non-empty string is required")
        return
    where = f"{source_id}"
    if not source_id.replace("_", "").isalnum() or not source_id.isupper():
        errors.append(f"BAD_ID: {where}: ids are UPPER_SNAKE_CASE")
    for field in REQUIRED_TEXT_FIELDS:
        value = source.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"BAD_TYPE: {where}.{field}: a non-empty string is required")

    provider_type = source.get("provider_type")
    if provider_type not in PROVIDER_TYPES:
        errors.append(f"UNKNOWN_PROVIDER_TYPE: {where}: {provider_type!r} not in {list(PROVIDER_TYPES)}")

    target = source.get("target")
    if target not in TARGETS:
        errors.append(f"UNKNOWN_TARGET: {where}: {target!r} not in {list(TARGETS)}")
    secondary = source.get("target_secondary", [])
    if not isinstance(secondary, list) or any(t not in TARGETS for t in secondary):
        errors.append(f"BAD_TARGET_SECONDARY: {where}: target_secondary must be a list of {list(TARGETS)}")
    elif len(set(secondary)) != len(secondary) or target in secondary:
        errors.append(f"BAD_TARGET_SECONDARY: {where}: duplicate or primary target repeated")

    frequency = source.get("frequency")
    if frequency not in FREQUENCIES:
        errors.append(f"UNKNOWN_FREQUENCY: {where}: {frequency!r} not in {list(FREQUENCIES)}")

    status = source.get("status")
    if status not in STATUSES:
        errors.append(f"UNKNOWN_STATUS: {where}: {status!r} not in {list(STATUSES)}")
    adapter_state = source.get("adapter_state")
    if adapter_state not in ADAPTER_STATES:
        errors.append(f"UNKNOWN_ADAPTER_STATE: {where}: {adapter_state!r} not in {list(ADAPTER_STATES)}")
    adapter = source.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        errors.append(f"BAD_TYPE: {where}.adapter: a string or null is required")
    if adapter_state == "IMPLEMENTED" and adapter not in IMPLEMENTED_ADAPTERS:
        errors.append(
            f"ADAPTER_NOT_IMPLEMENTED: {where}: '{adapter}' is not in the implemented adapter catalogue "
            f"{list(IMPLEMENTED_ADAPTERS)}"
        )
    if adapter_state == "PLANNED" and not adapter:
        errors.append(f"MISSING_ADAPTER_NAME: {where}: a planned adapter must be named")
    if adapter_state == "NOT_APPLICABLE" and adapter is not None:
        errors.append(f"ADAPTER_STATE_MISMATCH: {where}: NOT_APPLICABLE requires adapter = null")

    horizon = source.get("horizon")
    mapping = None
    if not isinstance(horizon, Mapping):
        errors.append(f"BAD_TYPE: {where}.horizon: an object is required")
    else:
        mapping = horizon.get("mapping")
        if mapping not in MAPPINGS:
            errors.append(f"UNKNOWN_MAPPING: {where}: {mapping!r} not in {list(MAPPINGS)}")
        native = horizon.get("native")
        if not isinstance(native, str) or not native.strip():
            errors.append(f"BAD_TYPE: {where}.horizon.native: a description is required")
        protocol_h = horizon.get("protocol_h")
        if not isinstance(protocol_h, list):
            errors.append(f"BAD_TYPE: {where}.horizon.protocol_h: a list of integers is required")
        else:
            if any(not isinstance(h, int) or isinstance(h, bool) or h < 0 for h in protocol_h):
                errors.append(f"BAD_HORIZON: {where}: protocol_h must be non-negative integers")
            elif len(set(protocol_h)) != len(protocol_h):
                errors.append(f"BAD_HORIZON: {where}: protocol_h has duplicates")
            if mapping == "EXACT" and not protocol_h:
                errors.append(
                    f"MAPPING_PROTOCOL_H_MISMATCH: {where}: EXACT requires the matched protocol horizons"
                )
            if mapping == "NONE" and protocol_h:
                errors.append(
                    f"MAPPING_PROTOCOL_H_MISMATCH: {where}: NONE must not claim protocol horizons"
                )

    publication = source.get("publication")
    if not isinstance(publication, Mapping):
        errors.append(f"BAD_TYPE: {where}.publication: an object is required")
    else:
        for field in ("cadence", "vintage_policy", "publication_date_source"):
            value = publication.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"BAD_TYPE: {where}.publication.{field}: a non-empty string is required")

    licence = source.get("licence")
    licence_status = None
    if not isinstance(licence, Mapping):
        errors.append(f"BAD_TYPE: {where}.licence: an object is required")
    else:
        licence_status = licence.get("status")
        if licence_status not in LICENCE_STATUSES:
            errors.append(
                f"UNKNOWN_LICENCE: {where}: {licence_status!r} not in {list(LICENCE_STATUSES)}"
            )
        if not isinstance(licence.get("values_publishable"), bool):
            errors.append(f"BAD_TYPE: {where}.licence.values_publishable: a boolean is required")
        entry = licence.get("entry")
        if entry is not None and not isinstance(entry, str):
            errors.append(f"BAD_TYPE: {where}.licence.entry: a string or null is required")
        terms_url = licence.get("terms_url")
        if terms_url is not None and not isinstance(terms_url, str):
            errors.append(f"BAD_TYPE: {where}.licence.terms_url: a string or null is required")
        note = licence.get("note")
        if not isinstance(note, str) or not note.strip():
            errors.append(f"BAD_TYPE: {where}.licence.note: a non-empty string is required")

    if isinstance(source.get("url"), str) and not source["url"].startswith(("https://", "http://")):
        errors.append(f"BAD_URL: {where}: url must be http(s)")

    data_path = source.get("data_path")
    if data_path is not None and (not isinstance(data_path, str) or not data_path.strip()):
        errors.append(f"BAD_TYPE: {where}.data_path: a non-empty string or null is required")

    # -- status / reason / licence / mapping / adapter consistency ---------------------------
    reason = source.get("status_reason")
    if status in STATUS_REASONS and reason not in STATUS_REASONS[status]:
        errors.append(
            f"STATUS_REASON_MISMATCH: {where}: {reason!r} is not a {status} reason "
            f"{list(STATUS_REASONS[status])}"
        )
    if licence_status in ("RESTRICTED", "UNVERIFIED") and status != "RESTRICTED":
        errors.append(
            f"STATUS_VS_LICENCE: {where}: a {licence_status} licence cannot be {status}"
        )
    if status == "RESTRICTED" and licence_status not in ("RESTRICTED", "UNVERIFIED"):
        errors.append(
            f"LICENCE_VS_STATUS: {where}: RESTRICTED status requires a RESTRICTED/UNVERIFIED licence"
        )
    if status == "ADAPTER_READY":
        if mapping != "EXACT":
            errors.append(f"READY_WITHOUT_EXACT_MAPPING: {where}: ADAPTER_READY requires mapping EXACT")
        if adapter_state != "IMPLEMENTED":
            errors.append(f"READY_WITHOUT_ADAPTER: {where}: ADAPTER_READY requires an implemented adapter")
        if not data_path:
            errors.append(f"READY_WITHOUT_DATA_PATH: {where}: ADAPTER_READY requires a registered local data path")
        if not isinstance(licence, Mapping) or not licence.get("entry"):
            errors.append(
                f"READY_WITHOUT_LICENCE_ENTRY: {where}: ADAPTER_READY requires a licence-register entry"
            )
    if status == "UNAVAILABLE":
        if reason in NO_MAPPING_REASONS and mapping != "NONE":
            errors.append(
                f"UNAVAILABLE_REASON_VS_MAPPING: {where}: {reason} requires mapping NONE"
            )
        if reason == "ARCHIVE_NOT_LOCAL":
            if mapping == "NONE":
                errors.append(
                    f"UNAVAILABLE_REASON_VS_MAPPING: {where}: ARCHIVE_NOT_LOCAL requires a mappable horizon"
                )
            if adapter_state != "IMPLEMENTED":
                errors.append(
                    f"UNAVAILABLE_REASON_VS_ADAPTER: {where}: ARCHIVE_NOT_LOCAL requires an implemented adapter"
                )
    if status == "NOT_FETCHED":
        if licence_status not in ("OPEN", "OPEN_ATTRIBUTION"):
            errors.append(f"NOT_FETCHED_WITHOUT_OPEN_LICENCE: {where}: {licence_status} cannot be NOT_FETCHED")
        if mapping not in ("EXACT", "PENDING"):
            errors.append(f"NOT_FETCHED_WITHOUT_MAPPING: {where}: mapping {mapping} cannot be NOT_FETCHED")
        if adapter_state != "PLANNED":
            errors.append(f"NOT_FETCHED_WITHOUT_PLANNED_ADAPTER: {where}: NOT_FETCHED requires adapter_state PLANNED")


def validate_registry(
    registry: Mapping,
    *,
    register: Mapping | None = None,
    scorecard: Mapping | None = None,
) -> dict:
    """Validate the registry against its own schema, the licence register and the scorecard.

    Returns ``{"ok", "errors", "warnings", "counts"}``.  Errors are fail-closed: the CLI exits 2
    and writes nothing.  Warnings are reported but do not block: a missing scorecard, a source
    that is ready but not yet in the scorecard, a missing licence register.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(registry, Mapping):
        return {"ok": False, "errors": ["REGISTRY_NOT_OBJECT"], "warnings": [],
                "counts": {"sources": 0, "errors": 1, "warnings": 0}}
    if registry.get("schema") != REGISTRY_SCHEMA:
        errors.append(f"SCHEMA_MISMATCH: expected {REGISTRY_SCHEMA!r}, got {registry.get('schema')!r}")
    if registry.get("registry_id") != REGISTRY_ID:
        errors.append(f"REGISTRY_ID_MISMATCH: expected {REGISTRY_ID!r}, got {registry.get('registry_id')!r}")
    for field in ("version", "date", "intended_use", "rules", "statuses", "licence_statuses", "targets"):
        if not registry.get(field):
            errors.append(f"MISSING_FIELD: top-level '{field}' is required")
    documented_statuses = registry.get("statuses") or {}
    if isinstance(documented_statuses, Mapping):
        for status in STATUSES:
            if not documented_statuses.get(status):
                errors.append(f"UNDOCUMENTED_STATUS: top-level statuses.{status} is required")
    else:
        errors.append("BAD_TYPE: top-level statuses must be an object")

    sources = registry.get("sources")
    if not isinstance(sources, list):
        errors.append("BAD_TYPE: top-level sources must be a list")
        return {"ok": False, "errors": errors, "warnings": warnings,
                "counts": {"sources": 0, "errors": len(errors), "warnings": len(warnings)}}

    seen: dict[str, int] = {}
    for index, source in enumerate(sources):
        _check_source(source, index, errors)
        if isinstance(source, Mapping) and isinstance(source.get("id"), str):
            source_id = source["id"]
            if source_id in seen:
                errors.append(f"DUPLICATE_ID: {source_id}: also at sources[{seen[source_id]}]")
            else:
                seen[source_id] = index

    _walk_raw_values(sources, "sources", errors)

    # -- licence register cross-check -------------------------------------------------------
    if register is None:
        warnings.append("LICENCE_REGISTER_MISSING: no licence register was supplied; entry cross-checks skipped")
    else:
        for source in sources:
            licence = source.get("licence") if isinstance(source, Mapping) else None
            if not isinstance(licence, Mapping):
                continue
            entry_id = licence.get("entry")
            if not entry_id:
                continue
            entry = register_entry(register, str(entry_id))
            if entry is None:
                errors.append(f"LICENCE_ENTRY_UNKNOWN: {source.get('id')}: '{entry_id}' is not in the licence register")
                continue
            register_status = str(entry.get("status") or "UNVERIFIED").upper()
            registry_status = str(licence.get("status") or "UNVERIFIED").upper()
            if _licence_rank(registry_status) > _licence_rank(register_status):
                errors.append(
                    f"LICENCE_MORE_PERMISSIVE_THAN_REGISTER: {source.get('id')}: registry says "
                    f"{registry_status}, the register entry {entry_id} says {register_status}"
                )

    # -- scorecard cross-check --------------------------------------------------------------
    if scorecard is None:
        warnings.append("SCORECARD_MISSING: nothing to compare with; the coverage report stands alone")
    else:
        mismatches, missing = _compare_scorecard(sources, scorecard)
        errors.extend(mismatches)
        warnings.extend(missing)

    counts = {"sources": len(sources), "errors": len(errors), "warnings": len(warnings)}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts}


SCORECARD_STATUS_TO_REGISTRY = {
    "AVAILABLE": "ADAPTER_READY",
    "UNAVAILABLE": "UNAVAILABLE",
    "RESTRICTED_NOT_USED": "RESTRICTED",
}


def _scorecard_sources(scorecard: Mapping) -> dict[str, str]:
    sources = scorecard.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    out: dict[str, str] = {}
    for source_id, block in sources.items():
        status = ((block or {}).get("status") or {}).get("status")
        if isinstance(status, str):
            out[str(source_id)] = status
    return out


def _compare_scorecard(sources: Sequence[Mapping], scorecard: Mapping) -> tuple[list[str], list[str]]:
    mismatches: list[str] = []
    warnings: list[str] = []
    scorecard_status = _scorecard_sources(scorecard)
    registry_status = {str(s.get("id")): s.get("status") for s in sources if isinstance(s, Mapping)}
    for source_id, status in registry_status.items():
        if source_id not in scorecard_status:
            if status == "ADAPTER_READY":
                warnings.append(
                    f"READY_NOT_IN_SCORECARD: {source_id}: the registry declares it scorable, "
                    f"but the current scorecard has no block for it (the scorecard is stale)"
                )
            continue
        expected = SCORECARD_STATUS_TO_REGISTRY.get(scorecard_status[source_id])
        if expected is None:
            warnings.append(
                f"UNKNOWN_SCORECARD_STATUS: {source_id}: {scorecard_status[source_id]!r} is not mapped"
            )
        elif expected != status:
            mismatches.append(
                f"SCORECARD_MISMATCH: {source_id}: scorecard says {scorecard_status[source_id]} "
                f"(-> {expected}), registry says {status}"
            )
    return mismatches, warnings


# ----------------------------------------------------------------------------------------------
# Coverage
# ----------------------------------------------------------------------------------------------
def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _count_by(sources: Iterable[Mapping], key) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        value = key(source)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _table(counts: Mapping[str, int], order: Sequence[str], total: int) -> dict:
    keys = list(order) + sorted(k for k in counts if k not in order)
    return {
        key: {"count": int(counts.get(key, 0)), "share": _share(int(counts.get(key, 0)), total)}
        for key in keys
    }


def coverage(
    registry: Mapping,
    *,
    scorecard: Mapping | None = None,
    scorecard_path: Path | str | None = None,
) -> dict:
    """Coverage by status/target/licence/mapping plus the scorecard comparison."""
    sources = [s for s in (registry.get("sources") or []) if isinstance(s, Mapping)]
    total = len(sources)
    by_status = _table(_count_by(sources, lambda s: s.get("status")), STATUSES, total)
    by_target = _table(_count_by(sources, lambda s: s.get("target")), TARGETS, total)
    by_licence = _table(
        _count_by(sources, lambda s: (s.get("licence") or {}).get("status")), LICENCE_STATUSES, total
    )
    by_mapping = _table(
        _count_by(sources, lambda s: (s.get("horizon") or {}).get("mapping")), MAPPINGS, total
    )
    by_frequency = _table(_count_by(sources, lambda s: s.get("frequency")), FREQUENCIES, total)

    # secondary targets are reported separately: they do not make a source a target source
    secondary: dict[str, int] = {target: 0 for target in TARGETS}
    for source in sources:
        for target in source.get("target_secondary") or []:
            secondary[str(target)] = secondary.get(str(target), 0) + 1
    by_target_secondary = {
        target: {"count": count, "share": _share(count, total)} for target, count in secondary.items()
    }

    ready = sorted(str(s.get("id")) for s in sources if s.get("status") == "ADAPTER_READY")
    not_fetched = sorted(str(s.get("id")) for s in sources if s.get("status") == "NOT_FETCHED")
    restricted = sorted(str(s.get("id")) for s in sources if s.get("status") == "RESTRICTED")
    unavailable: dict[str, list[str]] = {}
    for source in sources:
        if source.get("status") != "UNAVAILABLE":
            continue
        reason = str(source.get("status_reason"))
        unavailable.setdefault(reason, []).append(str(source.get("id")))
    unavailable = {reason: sorted(ids) for reason, ids in sorted(unavailable.items())}

    licensed_open = sorted(
        str(s.get("id"))
        for s in sources
        if (s.get("licence") or {}).get("status") in ("OPEN", "OPEN_ATTRIBUTION")
    )
    registered = sorted(
        str(s.get("id")) for s in sources if (s.get("licence") or {}).get("entry")
    )
    ready_targets = sorted({str(s.get("target")) for s in sources if s.get("status") == "ADAPTER_READY"})
    targets_with_ready_source = {
        target: target in ready_targets for target in TARGETS
    }

    return {
        "sources_total": total,
        "by_status": by_status,
        "by_target": by_target,
        "by_target_secondary": by_target_secondary,
        "by_licence": by_licence,
        "by_mapping": by_mapping,
        "by_frequency": by_frequency,
        "adapter_ready": {
            "count": len(ready),
            "sources": ready,
            "targets": ready_targets,
            "targets_with_ready_source": targets_with_ready_source,
            "share_of_catalogue": _share(len(ready), total),
        },
        "not_fetched": {"count": len(not_fetched), "sources": not_fetched},
        "restricted": {"count": len(restricted), "sources": restricted},
        "unavailable": {
            "count": sum(len(ids) for ids in unavailable.values()),
            "by_reason": unavailable,
        },
        "licensed_open": {"count": len(licensed_open), "sources": licensed_open},
        "registered_in_licence_register": {"count": len(registered), "sources": registered},
        "scorecard_comparison": _scorecard_comparison(
            registry, scorecard, ready, Path(scorecard_path) if scorecard_path is not None else SCORECARD_PATH
        ),
    }


def _scorecard_comparison(
    registry: Mapping,
    scorecard: Mapping | None,
    ready: Sequence[str],
    scorecard_file: Path,
) -> dict:
    sources = [s for s in (registry.get("sources") or []) if isinstance(s, Mapping)]
    registry_status = {str(s.get("id")): s.get("status") for s in sources}
    if scorecard is None:
        return {
            "file": _relpath(scorecard_file),
            "state": "MISSING",
            "scored_sources": [],
            "scored_points_total": 0,
            "status_map": [],
            "mismatches": [],
            "registry_only": sorted(registry_status),
            "scorecard_only": [],
            "ready_but_not_scored": sorted(ready),
            "comparison": "the current scorecard report is absent: the registry cannot be compared with it",
        }
    scorecard_status = _scorecard_sources(scorecard)
    cross = scorecard.get("cross_source") or {}
    scored = sorted(str(s) for s in (cross.get("scored_sources") or []))
    status_map = []
    mismatches: list[str] = []
    for source_id in sorted(set(registry_status) & set(scorecard_status)):
        expected = SCORECARD_STATUS_TO_REGISTRY.get(scorecard_status[source_id], "UNKNOWN")
        consistent = expected == registry_status[source_id]
        status_map.append(
            {
                "id": source_id,
                "scorecard_status": scorecard_status[source_id],
                "registry_status": registry_status[source_id],
                "expected_registry_status": expected,
                "consistent": consistent,
            }
        )
        if not consistent:
            mismatches.append(source_id)
    return {
        "file": _relpath(scorecard_file),
        "sha256": sha256_file(scorecard_file) if scorecard_file.exists() else None,
        "state": "FOUND",
        "scorecard_status": scorecard.get("status"),
        "scorecard_version": scorecard.get("version"),
        "scored_sources": scored,
        "scored_points_total": int(cross.get("scored_points_total") or 0),
        "status_map": status_map,
        "mismatches": sorted(mismatches),
        "registry_only": sorted(set(registry_status) - set(scorecard_status)),
        "scorecard_only": sorted(set(scorecard_status) - set(registry_status)),
        "ready_but_not_scored": sorted(set(ready) - set(scored)),
        "comparison": (
            f"{len(scored)} scored source(s) in the current scorecard; the registry declares "
            f"{len(ready)} ADAPTER_READY source(s) out of {len(registry_status)}"
        ),
    }


# ----------------------------------------------------------------------------------------------
# Status, gaps, report
# ----------------------------------------------------------------------------------------------
def _criteria(registry: Mapping, cov: Mapping, validation: Mapping) -> dict:
    ready = int(cov["adapter_ready"]["count"])
    total = int(cov["sources_total"])
    comparison = cov["scorecard_comparison"]
    return {
        "min_sources": {
            "required": MIN_SOURCES,
            "count": total,
            "pass": total >= MIN_SOURCES,
        },
        "min_ready_sources": {
            "required": MIN_READY_SOURCES,
            "count": ready,
            "pass": ready >= MIN_READY_SOURCES,
            "source": "docs/173 P0-1: at least three external sources with a scorecard",
        },
        "all_statuses_documented": {
            "pass": all(status in (registry.get("statuses") or {}) for status in STATUSES),
        },
        "all_targets_catalogued": {
            "pass": all(cov["by_target"].get(target, {}).get("count", 0) > 0 for target in TARGETS),
        },
        "scorecard_consistent": {"pass": not comparison.get("mismatches")},
        "registry_valid": {"pass": bool(validation.get("ok"))},
    }


def _status_of(criteria: Mapping) -> tuple[str, list[str]]:
    if not criteria["registry_valid"]["pass"]:
        return "INVALID", ["REGISTRY_VALIDATION_FAILED"]
    reasons: list[str] = []
    if not criteria["min_sources"]["pass"]:
        reasons.append(
            f"SOURCES_BELOW_CRITERION({criteria['min_sources']['count']}<{MIN_SOURCES})"
        )
    if not criteria["min_ready_sources"]["pass"]:
        reasons.append(
            f"ADAPTER_READY_BELOW_CRITERION({criteria['min_ready_sources']['count']}<{MIN_READY_SOURCES})"
        )
    if not criteria["scorecard_consistent"]["pass"]:
        reasons.append("SCORECARD_MISMATCH")
    if not criteria["all_targets_catalogued"]["pass"]:
        reasons.append("TARGET_NOT_CATALOGUED")
    return ("OK" if not reasons else "PARTIAL"), reasons


def _gaps(cov: Mapping, criteria: Mapping) -> list[str]:
    gaps: list[str] = []
    ready = cov["adapter_ready"]
    if not criteria["min_ready_sources"]["pass"]:
        gaps.append(
            f"only {ready['count']} of {cov['sources_total']} catalogued sources is ADAPTER_READY; "
            f"docs/173 asks for >= {MIN_READY_SOURCES} scored sources, so the scorecard still cannot "
            f"compare 'who is more accurate'"
        )
    gaps.append(
        f"{cov['not_fetched']['count']} source(s) are NOT_FETCHED: the licence allows use, but no data "
        f"has been retrieved and no result may be cited from them"
    )
    gaps.append(
        f"{cov['restricted']['count']} source(s) are RESTRICTED (subscription, members-only or news "
        f"terms): they are documented, never read, and cannot be unlocked by a code change"
    )
    unavailable = cov["unavailable"]
    if unavailable["count"]:
        reasons = ", ".join(f"{reason} ({len(ids)})" for reason, ids in unavailable["by_reason"].items())
        gaps.append(f"{unavailable['count']} source(s) are UNAVAILABLE: {reasons}")
    if not criteria["all_targets_catalogued"]["pass"]:
        missing = [t for t, block in cov["by_target"].items() if not block["count"]]
        gaps.append(f"no catalogued source for target(s) {missing}")
    if not cov["adapter_ready"]["targets_with_ready_source"].get("UNRATE", False):
        gaps.append(
            "no ADAPTER_READY source targets UNRATE: protocol v5 has no unemployment target, so a "
            "UNRATE mapping is a protocol decision, not a registry edit"
        )
    if cov["adapter_ready"]["count"]:
        gaps.append(
            f"the scorecard still scores {len(cov['scorecard_comparison']['scored_sources'])} source(s); "
            f"this registry changes no error, it only records what could be scored next"
        )
    return gaps


def content_digest(report: Mapping) -> str:
    core = {key: value for key, value in report.items() if key != "content_sha256"}
    blob = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return sha256_text(blob)


def build_report(
    *,
    registry_path: Path | str | None = None,
    scorecard_path: Path | str | None = None,
    licence_path: Path | str | None = None,
) -> dict:
    """Validate the registry and assemble the deterministic coverage report."""
    registry_file = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    scorecard_file = Path(scorecard_path) if scorecard_path is not None else SCORECARD_PATH
    registry = load_registry(registry_file)
    scorecard = load_scorecard(scorecard_file)
    register = load_licence_register(licence_path if licence_path is not None else LICENCE_PATH)
    validation = validate_registry(registry, register=register, scorecard=scorecard)
    cov = coverage(registry, scorecard=scorecard, scorecard_path=scorecard_file)
    criteria = _criteria(registry, cov, validation)
    status, reasons = _status_of(criteria)
    report: dict = {
        "report_id": REPORT_ID,
        "engine": {
            "tool": TOOL,
            "version": ENGINE_VERSION,
            "registry_schema": REGISTRY_SCHEMA,
            "deterministic": "no timestamp and no clock is read: two runs are byte-identical",
            "network": "none: the tool downloads nothing and reads no local raw data",
        },
        "registry": {
            "file": _relpath(registry_file),
            "sha256": sha256_file(registry_file) if registry_file.exists() else None,
            "registry_id": registry.get("registry_id"),
            "version": registry.get("version"),
            "date": registry.get("date"),
            "sources": len(registry.get("sources") or []),
        },
        "validation": validation,
        "status": status,
        "status_reasons": reasons,
        "criteria": criteria,
        "coverage": cov,
        "gaps": _gaps(cov, criteria),
        "notes": [
            "the registry is metadata only: no forecast value, vintage value or series appears in it",
            "a source is ADAPTER_READY only with an implemented adapter, an OPEN licence register entry "
            "and a registered local data path; everything else stays NOT_FETCHED/UNAVAILABLE/RESTRICTED",
            "the coverage report changes no number in the scorecard; it says what is scorable next",
        ],
    }
    report["content_sha256"] = content_digest(report)
    return report


# ----------------------------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------------------------
def _share_text(share: float) -> str:
    return f"{share:.4f}"


def summary_lines(report: Mapping) -> list[str]:
    cov = report["coverage"]
    by_status = ", ".join(
        f"{status} {block['count']}" for status, block in cov["by_status"].items() if block["count"]
    )
    by_target = ", ".join(
        f"{target} {block['count']}" for target, block in cov["by_target"].items()
    )
    by_licence = ", ".join(
        f"{status} {block['count']}" for status, block in cov["by_licence"].items() if block["count"]
    )
    comparison = cov["scorecard_comparison"]
    lines = [
        f"external forecasts registry v2: {cov['sources_total']} sources",
        f"status: {report['status']}"
        + (f" ({'; '.join(report['status_reasons'])})" if report["status_reasons"] else " (all criteria pass)"),
        f"by status: {by_status}",
        f"by target: {by_target}",
        f"by licence: {by_licence}",
        f"adapter-ready: {cov['adapter_ready']['count']} -> {', '.join(cov['adapter_ready']['sources']) or 'none'}",
        f"current scorecard: {comparison['comparison']}",
        f"validation: {report['validation']['counts']['errors']} error(s), "
        f"{report['validation']['counts']['warnings']} warning(s)",
    ]
    for gap in report["gaps"]:
        lines.append(f"gap: {gap}")
    for error in report["validation"]["errors"]:
        lines.append(f"error: {error}")
    return lines


def render_markdown(report: Mapping) -> str:
    cov = report["coverage"]
    comparison = cov["scorecard_comparison"]
    lines = [
        "# External forecasts registry v2 - coverage",
        "",
        f"- registry: `{report['registry']['file']}` (sha256 `{str(report['registry']['sha256'])[:16]}...`)",
        f"- sources: **{cov['sources_total']}**",
        f"- status: **{report['status']}**"
        + (f" ({'; '.join(report['status_reasons'])})" if report["status_reasons"] else ""),
        f"- engine: `{report['engine']['tool']}` {report['engine']['version']}",
        "",
        "## 1. By status",
        "",
        "| status | count | share |",
        "|---|---:|---:|",
    ]
    for status, block in cov["by_status"].items():
        lines.append(f"| {status} | {block['count']} | {_share_text(block['share'])} |")
    lines += ["", "## 2. By target (primary)", "", "| target | count | share |", "|---|---:|---:|"]
    for target, block in cov["by_target"].items():
        lines.append(f"| {target} | {block['count']} | {_share_text(block['share'])} |")
    lines += [
        "",
        "Secondary targets are reported separately:",
        "",
        "| target | sources naming it as secondary |",
        "|---|---:|",
    ]
    for target, block in cov["by_target_secondary"].items():
        lines.append(f"| {target} | {block['count']} |")
    lines += ["", "## 3. By licence", "", "| licence | count | share |", "|---|---:|---:|"]
    for status, block in cov["by_licence"].items():
        lines.append(f"| {status} | {block['count']} | {_share_text(block['share'])} |")
    lines += ["", "## 4. By horizon mapping", "", "| mapping | count | share |", "|---|---:|---:|"]
    for mapping, block in cov["by_mapping"].items():
        lines.append(f"| {mapping} | {block['count']} | {_share_text(block['share'])} |")
    lines += [
        "",
        "## 5. Adapter-ready sources",
        "",
        f"- count: {cov['adapter_ready']['count']} ({_share_text(cov['adapter_ready']['share_of_catalogue'])} of the catalogue)",
        f"- sources: {', '.join(cov['adapter_ready']['sources']) or 'none'}",
        "- targets covered by a ready source: "
        + ", ".join(
            f"{target}={'yes' if ok else 'no'}"
            for target, ok in cov["adapter_ready"]["targets_with_ready_source"].items()
        ),
        "",
        "## 6. Current scorecard comparison",
        "",
        f"- report: `{comparison['file']}` ({comparison['state']})",
        f"- scorecard status: {comparison.get('scorecard_status')}",
        f"- scored sources: {', '.join(comparison['scored_sources']) or 'none'}",
        f"- scored points: {comparison['scored_points_total']}",
        f"- registry sources absent from the scorecard: {len(comparison['registry_only'])}",
        f"- ready but not scored: {', '.join(comparison['ready_but_not_scored']) or 'none'}",
        f"- status mismatches: {', '.join(comparison['mismatches']) or 'none'}",
        "",
        "| source | scorecard | registry | consistent |",
        "|---|---|---|---|",
    ]
    for row in comparison["status_map"]:
        lines.append(
            f"| {row['id']} | {row['scorecard_status']} | {row['registry_status']} | "
            f"{'yes' if row['consistent'] else 'no'} |"
        )
    lines += [
        "",
        "## 7. Validation",
        "",
        f"- errors: {report['validation']['counts']['errors']}",
        f"- warnings: {report['validation']['counts']['warnings']}",
    ]
    for error in report["validation"]["errors"]:
        lines.append(f"- error: {error}")
    for warning in report["validation"]["warnings"]:
        lines.append(f"- warning: {warning}")
    lines += ["", "## 8. Gaps", ""]
    for gap in report["gaps"]:
        lines.append(f"- {gap}")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="External forecasts registry v2: validate, report coverage, compare with the scorecard"
    )
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--scorecard", default=str(SCORECARD_PATH))
    parser.add_argument("--licence-register", default=str(LICENCE_PATH))
    parser.add_argument("--json", action="store_true", help="print the full coverage report as JSON")
    parser.add_argument("--md", action="store_true", help="print the coverage report as Markdown")
    parser.add_argument("--out", default=None, help="write the report to this path (.json or .md)")
    parser.add_argument("--check", action="store_true", help="validate and print a short summary")
    parser.add_argument("--strict", action="store_true", help="with --check: also require status OK (exit 3)")
    args = parser.parse_args(argv)

    try:
        report = build_report(
            registry_path=args.registry,
            scorecard_path=args.scorecard,
            licence_path=args.licence_register,
        )
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    invalid = report["status"] == "INVALID"
    if invalid:
        for error in report["validation"]["errors"]:
            print(f"error: {error}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=False, default=_json_default))
    if args.md:
        print(render_markdown(report))
    if args.out and invalid:
        # fail-closed: an invalid registry never produces an artefact
        print(f"error: nothing written to {args.out}: the registry is invalid", file=sys.stderr)
    elif args.out:
        out = Path(args.out)
        suffix = out.suffix.lower()
        if suffix == ".json":
            _write(out, json.dumps(report, ensure_ascii=False, indent=1, default=_json_default) + "\n")
        elif suffix in (".md", ".markdown"):
            _write(out, render_markdown(report))
        else:
            print(f"error: --out must end with .json or .md, got {out.suffix!r}", file=sys.stderr)
            return 2
    if not (args.json or args.md or args.out) or args.check:
        for line in summary_lines(report):
            print(line)

    if invalid:
        return 2
    if args.check and args.strict and report["status"] != "OK":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
