#!/usr/bin/env python3
"""Static Forecast API contract for the AMFE market modules (schema ``amfe.forecast_api.v2``).

The tool does **not** run a server: it materialises the deterministic JSON responses a read-only
API would return, so the contract can be reviewed, versioned and served by any static host.

Endpoints (all read-only, ``GET``):

* ``/v1/modules``                 -- module passports from the module registry (versions, status,
                                     kind, limitations, code hashes) — **new in v2**;
* ``/v1/scenarios``               -- index of modules, scenarios, passports and forecast targets;
* ``/v1/scenarios/{scenario_id}`` -- one scenario document with the real ``MarketResult`` fields;
* ``/v1/forecast/{target}``       -- the scenario envelope for a target: oil (Brent, ``oil_brent``,
                                     range only) or gas/LNG (TTF, ``gas_ttf``, central point plus
                                     the sensitivity interval).

Every number is copied from ``amfe.markets`` (``OilModule`` / ``GasModule`` /
``MarketResult.as_dict``); the tool never invents a number, never stores raw third-party
observations and never returns a probability forecast.  Module passports come from
``amfe.learning.module_registry`` (docs/157) and are linked from every scenario/forecast as
``module_id@version``.

Backward compatibility with v1: ``--api-version v1`` reproduces the v1 contract exactly
(``amfe.forecast_api.v1``, oil only, no registry required); the v2 documents keep every v1 field and
only add new ones.  All responses carry ``status = "RESEARCH_ONLY"`` and an explicit disclaimer.

Fail-closed: if the market module package or the module registry is unavailable, or a scenario/
target does not exist, the tool raises :class:`ForecastApiError` and writes nothing.

Usage:
    python tools/forecast_api.py --out build/api --generated-at 2026-11-16T10:00:00Z
    python tools/forecast_api.py --api-version v1 --out build/api_v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCHEMA = "amfe.forecast_api.v2"
LEGACY_SCHEMA = "amfe.forecast_api.v1"
API_VERSION = "v2"
LEGACY_API_VERSION = "v1"
SUPPORTED_API_VERSIONS = (LEGACY_API_VERSION, API_VERSION)
DEFAULT_OUT = ROOT / "build" / "api"
DEFAULT_REGISTRY = ROOT / "build" / "registry" / "modules.jsonl"
OPENAPI_VERSION = "3.0.3"
DISCLAIMER = (
    "Research-only scenario arithmetic, not a forecast, not an admission decision and not "
    "investment advice. Numbers are produced by the deterministic amfe.markets engine from declared "
    "assumptions; the reported range is a range over sensitivity variants, not a confidence "
    "interval. Module passports are registry metadata (RESEARCH_ONLY), not an admission. No "
    "third-party observations are redistributed."
)
#: Accepted aliases per module (the canonical target id is still :func:`target_id`).
TARGET_ALIASES = {
    "oil.brent": ("oil.brent", "oil_brent", "brent"),
    "gas.lng_eu": ("gas.lng_eu", "gas_lng_eu", "gas_ttf", "ttf"),
}
#: Stable API target ids that are not a plain ``module_id.replace('.', '_')``.
TARGET_IDS = {"gas.lng_eu": "gas_ttf"}
#: Per-target forecast semantics (which metric the envelope reports, whether it has a point).
TARGET_META = {
    "oil.brent": {
        "metric": "brent_peak_usd_per_bbl",
        "point_estimate": False,
        "interval": True,
        "definition": "Brent price path and its peak/mean range under the declared Hormuz "
                      "scenario; a scenario envelope, not a point forecast.",
    },
    "gas.lng_eu": {
        "metric": "ttf_peak_usd_per_mmbtu",
        "point_estimate": False,
        "interval": True,
        "definition": "TTF gas price path and its peak/mean range under the declared European "
                      "pipeline-shock scenario, with JKM and LNG-diversion aggregates. Only a "
                      "sensitivity interval is published; no point forecast.",
    },
}
DEFAULT_TARGET_META = {
    "metric": "price_peak",
    "point_estimate": False,
    "interval": True,
    "definition": "Scenario envelope built from the module's published summary; scenario "
                  "arithmetic on declared assumptions, not a point forecast.",
}


class ForecastApiError(RuntimeError):
    """The API contract cannot be built (missing module/registry) or a resource does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _params(key: str, path: str, description: str, schema: dict) -> dict:
    return {"name": key, "in": path, "required": True, "description": description,
            "schema": schema}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_for(api_version: str) -> str:
    return LEGACY_SCHEMA if api_version == LEGACY_API_VERSION else SCHEMA


# ---------------------------------------------------------------------------------------- inputs
def get_modules(modules=None, *, include_gas: bool = True) -> tuple:
    """Return the market modules with a forecast contract (oil + gas by default).

    The gas module self-registers when imported (``amfe.markets.gas``), so it is imported here
    explicitly: without that, an API process that never touched gas would silently publish an
    oil-only contract while the registry already lists ``gas.lng_eu``.  ``include_gas=False`` keeps
    the legacy v1 surface oil-only even when gas was imported earlier in the same process.
    """
    if modules is not None:
        modules = tuple(modules)
        if not modules:
            raise ForecastApiError("no market modules registered: cannot build the API")
        return modules
    if include_gas:
        try:
            from amfe.markets import gas as _gas  # noqa: F401  (import registers the module)
        except ImportError:  # pragma: no cover - gas is optional in a trimmed checkout
            _gas = None
    try:
        from amfe.markets import registered_modules
    except ImportError as exc:  # pragma: no cover - environment without src/amfe/markets
        raise ForecastApiError(f"market module package unavailable: {exc!r}") from exc
    modules = tuple(registered_modules())
    if not modules:
        raise ForecastApiError("no market modules registered: cannot build the API")
    if not include_gas:
        modules = tuple(module for module in modules if str(module.module_id) == "oil.brent")
    return modules


def load_registry(path: Path | None = None) -> dict:
    """Load the module registry and its deterministic API export (fail-closed)."""
    path = Path(path) if path is not None else DEFAULT_REGISTRY
    if not path.is_file():
        raise ForecastApiError(
            f"module registry not found: {path}; run 'python tools/register_modules.py' first")
    try:
        from amfe.learning import module_registry
    except ImportError as exc:  # pragma: no cover - environment without src/amfe/learning
        raise ForecastApiError(f"module registry package unavailable: {exc!r}") from exc
    try:
        export = module_registry.export_for_api(path)
    except Exception as exc:
        raise ForecastApiError(f"cannot read module registry {path}: {exc!r}") from exc
    if not isinstance(export, dict) or not export.get("modules"):
        raise ForecastApiError(f"module registry {path} has no module passports")
    return {"path": str(path).replace("\\", "/"), "sha256": _sha256(path),
            "schema": export.get("schema"), "export": export,
            "by_id": {str(item["module_id"]): item for item in export["modules"]}}


def _passport(item: dict) -> dict:
    """The stable subset of a registry passport exposed by the API (no invented fields)."""
    return {
        "module_id": item.get("module_id"),
        "version": item.get("version"),
        "status": item.get("status"),
        "kind": item.get("kind"),
        "limitations": list(item.get("limitations") or []),
        "code_sha256": item.get("code_sha256"),
        "code_files": item.get("code_files"),
        "data_status": item.get("data_status"),
        "data_sha256": item.get("data_sha256"),
        "owner": item.get("owner"),
        "display_name": item.get("display_name"),
        "summary": item.get("summary"),
        "created_at": item.get("created_at"),
        "docs": list(item.get("docs") or []),
        "tests": list(item.get("tests") or []),
        "entrypoints": list(item.get("entrypoints") or []),
        "components": list(item.get("components") or []),
    }


def _module_ref(module, registry: dict | None) -> dict | None:
    """``{module_id, version, code_sha256, status, kind}`` link for a market module."""
    if registry is None:
        return None
    item = registry["by_id"].get(str(module.module_id))
    if item is None:
        raise ForecastApiError(
            f"market module {module.module_id!r} is not in the module registry {registry['path']}")
    return {"module_id": item["module_id"], "version": item["version"],
            "code_sha256": item.get("code_sha256"), "status": item.get("status"),
            "kind": item.get("kind")}


def target_id(module) -> str:
    """Stable API target id for a module (``oil.brent`` -> ``oil_brent``, gas -> ``gas_ttf``)."""
    module_id = str(module.module_id)
    return TARGET_IDS.get(module_id, module_id.replace(".", "_"))


def target_meta(module) -> dict:
    """Forecast semantics for a module (metric, point/interval, human definition)."""
    return dict(TARGET_META.get(str(module.module_id), DEFAULT_TARGET_META))


def target_aliases(module) -> tuple:
    """Every accepted request target for a module (canonical id, module id, short aliases)."""
    module_id = str(module.module_id)
    aliases = set(TARGET_ALIASES.get(module_id, ()))
    aliases.update((target_id(module), module_id))
    return tuple(sorted(aliases))


def module_kind(module, registry: dict | None = None) -> str:
    """Registry ``kind`` of a module (``market``/``storage``/...) without inventing one."""
    if registry is None:
        return "market"
    item = registry["by_id"].get(str(module.module_id))
    if item is None:
        raise ForecastApiError(
            f"market module {module.module_id!r} is not in the module registry {registry['path']}")
    return str(item.get("kind") or "market")


def module_document(module, registry: dict | None = None) -> dict:
    """Module passport plus the API target; no result numbers are invented here."""
    described = module.describe()
    document = {
        "module_id": described["module_id"],
        "commodity": described["commodity"],
        "units": described["units"],
        "status": described["status"],
        "result_schema": described["schema"],
        "scenarios": described["scenarios"],
        "default_scenario": described["default_scenario"],
        "assumption_classes": described["assumption_classes"],
        "forecast_target": target_id(module),
        "research_only": True,
    }
    reference = _module_ref(module, registry)
    if reference is not None:
        document["registry"] = reference
        document["module_version"] = reference["version"]
    return document


# -------------------------------------------------------------------------------------- documents
def build_scenario(module, scenario_id: str, generated_at: str,
                   registry: dict | None = None, api_version: str = API_VERSION) -> dict:
    """One ``GET /v1/scenarios/{scenario_id}`` response built from the real MarketResult."""
    try:
        result = module.run(scenario_id)
    except Exception as exc:
        raise ForecastApiError(f"scenario {scenario_id!r} for {module.module_id}: {exc}") from exc
    payload = result.as_dict()
    document = {
        "schema": _schema_for(api_version),
        "kind": "scenario",
        "api_version": api_version,
        "generated_at": generated_at,
        "status": payload["status"],
        "disclaimer": DISCLAIMER,
        "id": f"{module.module_id}/{scenario_id}",
        "module": module_document(module, registry),
        "scenario": (payload.get("meta") or {}).get("scenario"),
        "result": {
            "schema": payload["schema"],
            "module_id": payload["module_id"],
            "commodity": payload["commodity"],
            "scenario_id": payload["scenario_id"],
            "units": payload["units"],
            "horizon_months": payload["horizon_months"],
            "price_path": payload["price_path"],
            "summary": payload["summary"],
            "assumptions": payload["assumptions"],
            "sensitivity": payload["sensitivity"],
            "checks": payload["checks"],
            "meta": payload["meta"],
            "digest": payload["digest"],
        },
    }
    if api_version == API_VERSION:
        document["module_ref"] = _module_ref(module, registry)
        document["links"] = {"self": f"scenarios/{module.module_id}__{scenario_id}.json",
                             "index": "index.json", "modules": "modules.json"}
    return document


def _oil_envelope(payload: dict) -> dict:
    """The v1/v2 oil envelope: exact legacy field names, no point, no raw data."""
    summary = payload["summary"]
    return {
        "price_path": payload["price_path"],
        "price_peak": summary["price_peak"],
        "price_mean": summary["price_mean"],
        "price_trough": summary["price_trough"],
        "price_peak_month": summary["price_peak_month"],
        "range_peak": summary["brent_range_usd_per_bbl"],
        "range_mean": summary["brent_range_mean_usd_per_bbl"],
        "sensitivity": payload["sensitivity"],
        "checks": payload["checks"],
        "units": payload["units"],
    }


def _gas_envelope(payload: dict) -> dict:
    """The gas/LNG envelope: TTF point + sensitivity interval, JKM and LNG aggregates.

    Nothing is recomputed: every number is read from the gas ``MarketResult`` summary, and only
    aggregate fields are copied (no observations, no raw series, no assumptions blob).
    """
    summary = payload["summary"]
    price_path = list(payload["price_path"])
    ranges = summary.get("ranges") or {}
    ttf_range = dict(ranges.get("ttf_peak") or {})
    jkm_range = dict(ranges.get("jkm_peak") or {})
    units = payload["units"]
    peak = summary["ttf_peak_usd_per_mmbtu"]
    peak_month = price_path.index(max(price_path)) if price_path else 0
    arbitrage = summary.get("arbitrage") or {}
    diversion = arbitrage.get("diversion") or {}
    flows = summary.get("lng_flows") or {}
    storage = summary.get("storage") or {}
    return {
        "price_path": price_path,
        "price_peak": peak,
        "price_mean": summary["ttf_mean_usd_per_mmbtu"],
        "price_trough": summary["ttf_trough_usd_per_mmbtu"],
        "price_peak_month": peak_month,
        "range_peak": ttf_range,
        "range_mean": None,
        "interval": {
            "low": ttf_range.get("low"),
            "central": ttf_range.get("central"),
            "high": ttf_range.get("high"),
            "metric": "peak",
            "units": units,
            "basis": "sensitivity_variants",
            "confidence_interval": False,
            "note": ttf_range.get("note"),
        },
        "secondary": {
            "jkm": {
                "price_path": list(summary.get("jkm_usd_per_mmbtu_path") or []),
                "price_peak": summary.get("jkm_peak_usd_per_mmbtu"),
                "price_mean": summary.get("jkm_mean_usd_per_mmbtu"),
                "range_peak": jkm_range,
                "units": units,
            },
            "henry_hub_reference_usd_per_mmbtu":
                summary.get("henry_hub_reference_usd_per_mmbtu"),
            "arbitrage_band_usd_per_mmbtu": arbitrage.get("band_usd_per_mmbtu"),
            "diversion_bcm_y": diversion.get("diversion_bcm_y"),
            "diversion_binding": diversion.get("binding"),
            "storage_end_bcm": storage.get("end_bcm"),
            "storage_end_share_of_capacity": storage.get("end_share_of_capacity"),
            "lng_import_capacity_bcm_y": flows.get("eu_import_capacity_bcm_y"),
        },
        "sensitivity": payload["sensitivity"],
        "checks": payload["checks"],
        "units": units,
    }


def _generic_envelope(payload: dict) -> dict:
    """Kind-agnostic fallback: the published summary, never a hardcoded oil field."""
    summary = payload["summary"]
    envelope = {
        "price_path": payload["price_path"],
        "price_peak": summary.get("price_peak"),
        "price_mean": summary.get("price_mean"),
        "price_trough": summary.get("price_trough"),
        "sensitivity": payload["sensitivity"],
        "checks": payload["checks"],
        "units": payload["units"],
        "summary": dict(summary),
    }
    if summary.get("price_peak") is None:
        envelope.pop("price_peak")
    return envelope


def _forecast_envelope(module, payload: dict, registry: dict | None) -> dict:
    """Dispatch by ``module_id``, then by registry ``kind``; generic fallback otherwise."""
    builders = {"oil.brent": _oil_envelope, "gas.lng_eu": _gas_envelope}
    builder = builders.get(str(module.module_id))
    if builder is None:
        builder = {"oil": _oil_envelope, "gas": _gas_envelope}.get(
            module_kind(module, registry), _generic_envelope)
    return builder(payload)


def build_forecast(module, target: str, generated_at: str,
                   registry: dict | None = None, api_version: str = API_VERSION) -> dict:
    """One ``GET /v1/forecast/{target}`` response: the real scenario envelope for the target."""
    allowed = target_aliases(module)
    if target not in allowed:
        raise ForecastApiError(
            f"unknown forecast target {target!r}; available {list(allowed)}")
    meta = target_meta(module)
    scenario_id = module.default_scenario().scenario_id
    result = module.run(scenario_id)
    payload = result.as_dict()
    target_definition = {
        "commodity": payload["commodity"],
        "units": payload["units"],
        "horizon_months": payload["horizon_months"],
        "default_scenario": scenario_id,
        "definition": meta["definition"],
    }
    if api_version == API_VERSION:
        target_definition.update({
            "scenario_ids": [scenario.scenario_id for scenario in module.scenarios()],
            "aliases": list(target_aliases(module)),
            "forecast_metric": meta["metric"],
            "point_estimate": meta["point_estimate"],
            "interval": meta["interval"],
        })
    document = {
        "schema": _schema_for(api_version),
        "kind": "forecast_envelope",
        "api_version": api_version,
        "generated_at": generated_at,
        "status": payload["status"],
        "disclaimer": DISCLAIMER,
        "target": target,
        "target_definition": target_definition,
        "module_id": payload["module_id"],
        "envelope": _forecast_envelope(module, payload, registry),
        "assumptions_count": len(payload["assumptions"]),
        "assumptions_classes": sorted({item["source_class"] for item in payload["assumptions"]}),
        "result_digest": payload["digest"],
        "research_only": True,
        "no_point_forecast": not meta["point_estimate"],
    }
    if api_version == API_VERSION:
        document["point_estimate"] = meta["point_estimate"]
        document["module_ref"] = _module_ref(module, registry)
        document["links"] = {"self": f"forecasts/{target_id(module)}.json",
                             "index": "index.json", "modules": "modules.json"}
    return document


def build_modules(registry: dict, generated_at: str) -> dict:
    """``GET /v1/modules``: passport list from the module registry (no market numbers)."""
    export = registry["export"]
    modules = [_passport(item) for item in export["modules"]]
    return {
        "schema": SCHEMA,
        "kind": "modules",
        "api_version": API_VERSION,
        "generated_at": generated_at,
        "status": "RESEARCH_ONLY",
        "disclaimer": DISCLAIMER,
        "registry": {"path": registry["path"], "sha256": registry["sha256"],
                     "schema": registry["schema"], "counts": export.get("counts")},
        "modules": modules,
        "counts": {"modules": len(modules),
                   "by_kind": dict(sorted((export.get("counts") or {}).get("by_kind", {}).items())),
                   "by_status": dict(sorted((export.get("counts") or {}).get("by_status", {}).items()))},
        "policy": {"read_only": True, "research_only": True, "no_admission": True,
                   "module_passports_are_metadata": True},
        "links": {"self": "modules.json", "index": "index.json"},
    }


def build_index(modules, generated_at: str, files: list[dict] | None = None,
                registry: dict | None = None, api_version: str = API_VERSION) -> dict:
    """``GET /v1/scenarios`` index: modules/passports, scenarios, targets and file map."""
    scenarios, market_modules = [], []
    for module in modules:
        market_modules.append(module_document(module, registry))
        for scenario in module.scenarios():
            result = module.run(scenario.scenario_id).as_dict()
            entry = {
                "id": f"{module.module_id}/{scenario.scenario_id}",
                "module_id": module.module_id,
                "scenario_id": scenario.scenario_id,
                "description": scenario.description,
                "horizon_months": int(scenario.horizon_months),
                "parameters": dict(scenario.parameters),
                "status": result["status"],
                "digest": result["digest"],
                "summary": _summary_excerpt(result),
                "file": f"scenarios/{module.module_id}__{scenario.scenario_id}.json",
            }
            reference = _module_ref(module, registry)
            if reference is not None:
                entry["module_version"] = reference["version"]
                entry["module_ref"] = reference
            scenarios.append(entry)
    forecasts = []
    for module in modules:
        meta = target_meta(module)
        entry = {"target": target_id(module), "module_id": module.module_id,
                 "commodity": module.commodity, "units": module.units,
                 "file": f"forecasts/{target_id(module)}.json"}
        if api_version == API_VERSION:
            entry.update({"kind": module_kind(module, registry),
                          "default_scenario": module.default_scenario().scenario_id,
                          "point_estimate": meta["point_estimate"],
                          "interval": meta["interval"]})
        reference = _module_ref(module, registry)
        if reference is not None:
            entry["module_version"] = reference["version"]
            entry["module_ref"] = reference
        forecasts.append(entry)
    endpoints = [
        {"method": "GET", "path": "/v1/scenarios", "file": "index.json",
         "description": "index of modules, scenarios and forecast targets"},
        {"method": "GET", "path": "/v1/scenarios/{scenario_id}",
         "file": "scenarios/<module>__<scenario>.json",
         "description": "one scenario with the real MarketResult fields"},
        {"method": "GET", "path": "/v1/forecast/{target}",
         "file": "forecasts/<target>.json",
         "description": "scenario envelope for a target (not a point forecast)"},
        {"method": "GET", "path": "/openapi.json", "file": "openapi.json",
         "description": "OpenAPI description of the contract"},
    ]
    index = {
        "schema": _schema_for(api_version),
        "kind": "index",
        "api_version": api_version,
        "base_path": "/v1",
        "generated_at": generated_at,
        "status": "RESEARCH_ONLY",
        "disclaimer": DISCLAIMER,
        "policy": {
            "read_only": True,
            "research_only": True,
            "no_admission": True,
            "no_point_forecast": not any(target_meta(module)["point_estimate"]
                                         for module in modules),
            "no_raw_third_party_data": True,
            "deterministic_except_generated_at": True,
        },
        "market_modules": market_modules,
        "scenarios": scenarios,
        "forecasts": forecasts,
        "endpoints": endpoints,
        "openapi": "openapi.json",
    }
    if api_version == API_VERSION:
        index["policy"]["module_passports"] = True
        index["policy"]["no_probability_forecast"] = True
        index["api_versions"] = list(SUPPORTED_API_VERSIONS)
        index["backward_compatible_with"] = LEGACY_SCHEMA
        index["modules"] = [_passport(item) for item in registry["export"]["modules"]]
        index["registry"] = {"path": registry["path"], "sha256": registry["sha256"],
                             "schema": registry["schema"],
                             "counts": registry["export"].get("counts")}
        index["endpoints"].insert(0, {
            "method": "GET", "path": "/v1/modules", "file": "modules.json",
            "description": "module passports (versions, statuses, limitations, code hashes)"})
        index["counts"] = {"modules": len(index["modules"]),
                           "market_modules": len(market_modules),
                           "scenarios": len(scenarios), "forecasts": len(forecasts)}
        index["links"] = {"modules": "modules.json", "self": "index.json"}
    else:
        # v1: ``modules`` was the market-module list; counts.modules counted them.
        index["modules"] = market_modules
        index["counts"] = {"modules": len(market_modules), "scenarios": len(scenarios),
                           "forecasts": len(forecasts)}
    if files is not None:
        index["files"] = sorted(files, key=lambda item: item["path"])
    return index


def _oil_excerpt(result: dict) -> dict:
    summary = result["summary"]
    return {
        "price_peak": summary["price_peak"],
        "price_mean": summary["price_mean"],
        "price_peak_month": summary["price_peak_month"],
        "range_peak": summary["brent_range_usd_per_bbl"],
        "range_mean": summary["brent_range_mean_usd_per_bbl"],
        "units": result["units"],
        "horizon_months": result["horizon_months"],
    }


def _gas_excerpt(result: dict) -> dict:
    summary = result["summary"]
    path = list(result["price_path"])
    ranges = summary.get("ranges") or {}
    return {
        "price_peak": summary["ttf_peak_usd_per_mmbtu"],
        "price_mean": summary["ttf_mean_usd_per_mmbtu"],
        "price_peak_month": path.index(max(path)) if path else 0,
        "range_peak": ranges.get("ttf_peak"),
        "range_mean": None,
        "forecast_metric": "ttf_peak",
        "units": result["units"],
        "horizon_months": result["horizon_months"],
    }


def _summary_excerpt(result: dict) -> dict:
    """The small numbers an index is allowed to repeat (all taken from the result)."""
    builders = {"oil.brent": _oil_excerpt, "gas.lng_eu": _gas_excerpt}
    builder = builders.get(str(result.get("module_id")))
    if builder is not None:
        return builder(result)
    summary = result["summary"]
    excerpt = {"units": result["units"], "horizon_months": result["horizon_months"],
               "summary": dict(summary)}
    for key in ("price_peak", "price_mean", "price_trough", "price_peak_month"):
        if key in summary:
            excerpt[key] = summary[key]
    return excerpt


def build_openapi(modules, registry: dict | None = None,
                  api_version: str = API_VERSION) -> dict:
    """OpenAPI 3.0 description of the read-only endpoints (no numbers invented)."""
    module_enum = (sorted({alias for module in modules for alias in target_aliases(module)})
                   if api_version == API_VERSION else [target_id(module) for module in modules])
    module_ids = [module.module_id for module in modules]
    scenario_ids = sorted(f"{module.module_id}/{scenario.scenario_id}"
                          for module in modules for scenario in module.scenarios())
    paths = {
        "/v1/scenarios": {
            "get": {
                "tags": ["scenarios"], "operationId": "listScenarios",
                "summary": "Index of modules, scenarios and forecast targets",
                "responses": {"200": {"description": "index",
                                      "content": {"application/json": {"schema": {
                                          "$ref": "#/components/schemas/Index"}}}}},
            }
        },
        "/v1/scenarios/{scenario_id}": {
            "get": {
                "tags": ["scenarios"], "operationId": "getScenario",
                "summary": "One scenario document (real MarketResult fields)",
                "parameters": [_params("scenario_id", "path",
                                       "module_id/scenario_id, e.g. oil.brent/hormuz_closed_6m",
                                       {"type": "string", "enum": scenario_ids})],
                "responses": {
                    "200": {"description": "scenario",
                            "content": {"application/json": {"schema": {
                                "$ref": "#/components/schemas/Scenario"}}}},
                    "404": {"description": "unknown scenario",
                            "content": {"application/json": {"schema": {
                                "$ref": "#/components/schemas/Error"}}}},
                },
            }
        },
        "/v1/forecast/{target}": {
            "get": {
                "tags": ["forecast"], "operationId": "getForecast",
                "summary": ("Scenario envelope for a target (oil range; gas central point + "
                            "sensitivity interval)" if api_version == API_VERSION else
                            "Scenario envelope for a target (not a point forecast)"),
                "parameters": [_params("target", "path",
                                       "target id, e.g. oil_brent or gas_ttf"
                                       if api_version == API_VERSION else
                                       "target id, e.g. oil_brent",
                                       {"type": "string", "enum": module_enum})],
                "responses": {
                    "200": {"description": "envelope",
                            "content": {"application/json": {"schema": {
                                "$ref": "#/components/schemas/Forecast"}}}},
                    "404": {"description": "unknown target",
                            "content": {"application/json": {"schema": {
                                "$ref": "#/components/schemas/Error"}}}},
                },
            }
        },
    }
    components = {
        "Index": {"type": "object",
                  "required": ["schema", "api_version", "status", "scenarios", "forecasts"],
                  "properties": {"schema": {"type": "string"},
                                 "kind": {"type": "string", "const": "index"},
                                 "api_version": {"type": "string"},
                                 "generated_at": {"type": "string", "format": "date-time"},
                                 "status": {"type": "string", "enum": ["RESEARCH_ONLY"]},
                                 "modules": {"type": "array", "items": {"type": "object"}},
                                 "market_modules": {"type": "array", "items": {"type": "object"}},
                                 "scenarios": {"type": "array", "items": {"type": "object"}},
                                 "forecasts": {"type": "array", "items": {"type": "object"}},
                                 "endpoints": {"type": "array", "items": {"type": "object"}}}},
        "Scenario": {"type": "object",
                     "required": ["schema", "kind", "status", "id", "result"],
                     "properties": {"schema": {"type": "string"},
                                    "kind": {"type": "string", "const": "scenario"},
                                    "id": {"type": "string"},
                                    "status": {"type": "string"},
                                    "module": {"type": "object"},
                                    "module_ref": {"$ref": "#/components/schemas/ModuleRef"},
                                    "scenario": {"type": "object"},
                                    "result": {"type": "object",
                                               "required": ["price_path", "summary",
                                                            "assumptions", "sensitivity",
                                                            "checks", "digest"]}}},
        "Forecast": {"type": "object",
                     "required": ["schema", "kind", "status", "target", "module_id", "envelope"],
                     "properties": {"schema": {"type": "string"},
                                    "kind": {"type": "string", "const": "forecast_envelope"},
                                    "target": {"type": "string"},
                                    "status": {"type": "string"},
                                    "module_id": {"type": "string"},
                                    "module_ref": {"$ref": "#/components/schemas/ModuleRef"},
                                    "target_definition": {"type": "object"},
                                    "envelope": {"type": "object",
                                                 "required": ["price_path", "sensitivity",
                                                              "checks", "units"],
                                                 "properties": {
                                                     "price_path": {"type": "array",
                                                                    "items": {"type": "number"}},
                                                     "price_peak": {"type": "number"},
                                                     "price_mean": {"type": "number"},
                                                     "price_trough": {"type": "number"},
                                                     "price_peak_month": {"type": "integer"},
                                                     "range_peak": {"type": "object",
                                                                    "nullable": True},
                                                     "range_mean": {"type": "object",
                                                                    "nullable": True},
                                                     "point": {"type": "object", "nullable": True},
                                                     "interval": {"type": "object",
                                                                  "nullable": True},
                                                     "secondary": {"type": "object",
                                                                   "nullable": True},
                                                     "sensitivity": {"type": "array",
                                                                     "items": {"type": "object"}},
                                                     "checks": {"type": "object"},
                                                     "units": {"type": "string"}}},
                                    "no_point_forecast": {"type": "boolean"},
                                    "point_estimate": {"type": "boolean"},
                                    "research_only": {"type": "boolean"}}},
        "ModuleRef": {"type": "object", "required": ["module_id", "version"],
                      "properties": {"module_id": {"type": "string"},
                                     "version": {"type": "string"},
                                     "code_sha256": {"type": "string"},
                                     "status": {"type": "string"},
                                     "kind": {"type": "string"}}},
        "Error": {"type": "object", "required": ["status", "error"],
                  "properties": {"status": {"type": "string", "enum": ["NOT_FOUND"]},
                                 "error": {"type": "string"}}},
    }
    if api_version == LEGACY_API_VERSION:
        # Keep the frozen v1 forecast schema: v1 publishes oil ranges only.
        components["Forecast"] = {
            "type": "object",
            "required": ["schema", "kind", "status", "target", "envelope"],
            "properties": {"schema": {"type": "string"},
                           "kind": {"type": "string", "const": "forecast_envelope"},
                           "target": {"type": "string"},
                           "status": {"type": "string"},
                           "module_ref": {"$ref": "#/components/schemas/ModuleRef"},
                           "target_definition": {"type": "object"},
                           "envelope": {"type": "object",
                                        "required": ["price_path", "range_peak", "range_mean",
                                                     "sensitivity"]},
                           "research_only": {"type": "boolean"}}}
    if api_version == API_VERSION:
        paths["/v1/modules"] = {
            "get": {
                "tags": ["modules"], "operationId": "listModules",
                "summary": "Module passports from the module registry",
                "responses": {"200": {"description": "passports",
                                      "content": {"application/json": {"schema": {
                                          "$ref": "#/components/schemas/Modules"}}}}},
            }
        }
        components["Modules"] = {
            "type": "object", "required": ["schema", "status", "modules"],
            "properties": {"schema": {"type": "string"},
                           "kind": {"type": "string", "const": "modules"},
                           "api_version": {"type": "string"},
                           "status": {"type": "string"},
                           "registry": {"type": "object"},
                           "modules": {"type": "array",
                                       "items": {"$ref": "#/components/schemas/ModulePassport"}},
                           "counts": {"type": "object"}},
        }
        components["ModulePassport"] = {
            "type": "object",
            "required": ["module_id", "version", "status", "kind", "limitations", "code_sha256"],
            "properties": {"module_id": {"type": "string"}, "version": {"type": "string"},
                           "status": {"type": "string"}, "kind": {"type": "string"},
                           "limitations": {"type": "array", "items": {"type": "string"}},
                           "code_sha256": {"type": "string"},
                           "code_files": {"type": "object", "nullable": True},
                           "data_status": {"type": "string"},
                           "data_sha256": {"type": "string", "nullable": True},
                           "owner": {"type": "string"},
                           "docs": {"type": "array", "items": {"type": "string"}},
                           "tests": {"type": "array", "items": {"type": "string"}}},
        }
    document = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "AMFE Forecast API (research contract)",
            "version": "2.0.0" if api_version == API_VERSION else "1.0.0",
            "description": "Read-only static contract over the deterministic AMFE market modules. "
                           "Research-only: no admissions, no raw third-party observations, no "
                           "probability forecast. Oil targets publish a range; the gas TTF target "
                           "publishes the central scenario value as a point plus the sensitivity "
                           "interval. Module passports are registry metadata, not an admission. "
                           "The shipped JSON files are the exact responses.",
            "license": {"name": "Research use only; see 08_Licence_and_Disclaimer"},
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": "modules", "description": "module passports"},
                 {"name": "forecast", "description": "scenario envelopes"},
                 {"name": "scenarios", "description": "market scenarios"}],
        "paths": paths,
        "components": {"schemas": components},
        "x-module-ids": module_ids,
        "x-api-versions": list(SUPPORTED_API_VERSIONS),
    }
    if api_version == LEGACY_API_VERSION:
        document["info"]["description"] = (
            "Read-only static contract over the deterministic AMFE market modules. Research-only: "
            "no admissions, no point forecasts, no raw third-party observations. Module passports "
            "are registry metadata, not an admission. The shipped JSON files are the exact "
            "responses.")
    if api_version == API_VERSION:
        document["x-forecast-targets"] = {
            target_id(module): {
                "module_id": module.module_id,
                "kind": module_kind(module, registry),
                "aliases": list(target_aliases(module)),
                **target_meta(module),
            }
            for module in modules
        }
    if registry is not None and api_version == API_VERSION:
        document["x-registry-schema"] = registry["schema"]
    return document


# ------------------------------------------------------------------------------------------ build
def _write_json(path: Path, document: dict) -> None:
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1)
               + "\n").encode("utf-8")
    Path(path).write_bytes(payload)


def _readme(index: dict, api_version: str) -> str:
    lines = [
        "# AMFE Forecast API (static research contract)",
        "",
        f"Schema: `{index['schema']}` · API: `{index['api_version']}` · status: **RESEARCH_ONLY** · "
        f"generated: {index['generated_at']}.",
        "",
        "> " + DISCLAIMER,
        "",
        "## Files",
        "",
        "| File | Endpoint |",
        "|---|---|",
    ]
    if api_version == API_VERSION:
        lines.append("| `modules.json` | `GET /v1/modules` |")
    lines += [
        "| `index.json` | `GET /v1/scenarios` |",
        "| `scenarios/<module>__<scenario>.json` | `GET /v1/scenarios/{scenario_id}` |",
        "| `forecasts/<target>.json` | `GET /v1/forecast/{target}` |",
        "| `openapi.json` | OpenAPI description |",
        "",
        "## Contents",
        "",
    ]
    if api_version == API_VERSION and index.get("registry"):
        lines.append(f"Module registry: `{index['registry']['path']}` "
                     f"(SHA-256 `{index['registry']['sha256'][:16]}…`), "
                     f"{len(index['modules'])} passports.")
        lines.append("")
    if any(entry.get("point_estimate") for entry in index["forecasts"]):
        lines += ["Targets with `point_estimate=true` (gas) publish the central scenario value as "
                  "`point` plus the sensitivity `interval`; oil targets stay range-only. No "
                  "probability forecast is published.", ""]
    for scenario in index["scenarios"]:
        version = scenario.get("module_version")
        suffix = f", module version {version}" if version else ""
        lines.append(f"- `{scenario['id']}` — {scenario['file']} "
                     f"(digest `{scenario['digest'][:16]}…`{suffix})")
    for forecast in index["forecasts"]:
        lines.append(f"- forecast target `{forecast['target']}` — {forecast['file']}")
    lines += [
        "",
        "Serve the directory with any static HTTP server; responses are pre-computed and "
        "byte-stable except `generated_at`.",
        "",
    ]
    return "\n".join(lines)


def build_api(out_dir: Path = DEFAULT_OUT, generated_at: str | None = None,
              modules=None, registry_path: Path | None = None,
              api_version: str = API_VERSION) -> dict:
    """Write the static API tree for ``api_version`` (v1 legacy or v2 default)."""
    if api_version not in SUPPORTED_API_VERSIONS:
        raise ForecastApiError(f"unknown api version {api_version!r}; "
                               f"available {list(SUPPORTED_API_VERSIONS)}")
    generated_at = generated_at or _now()
    modules = get_modules(modules, include_gas=(api_version == API_VERSION))
    if api_version == LEGACY_API_VERSION:
        # v1 is a frozen oil-only contract: gas is available in v2, never injected into the legacy
        # documents (even when gas was imported earlier in the same process and self-registered).
        modules = tuple(module for module in modules if str(module.module_id) == "oil.brent")
    registry = load_registry(registry_path) if api_version == API_VERSION else None
    out_dir = Path(out_dir)
    scenario_dir = out_dir / "scenarios"
    forecast_dir = out_dir / "forecasts"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    forecast_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []

    def record(path: Path) -> None:
        files.append({"path": str(path.relative_to(out_dir)).replace("\\", "/"),
                      "sha256": _sha256(path), "bytes": path.stat().st_size})

    for module in modules:
        for scenario in module.scenarios():
            document = build_scenario(module, scenario.scenario_id, generated_at, registry,
                                      api_version)
            path = scenario_dir / f"{module.module_id}__{scenario.scenario_id}.json"
            _write_json(path, document)
            record(path)
        forecast = build_forecast(module, target_id(module), generated_at, registry, api_version)
        path = forecast_dir / f"{target_id(module)}.json"
        _write_json(path, forecast)
        record(path)
    if registry is not None:
        modules_document = build_modules(registry, generated_at)
        modules_path = out_dir / "modules.json"
        _write_json(modules_path, modules_document)
        record(modules_path)
        module_dir = out_dir / "modules"
        module_dir.mkdir(parents=True, exist_ok=True)
        for item in modules_document["modules"]:
            single = {
                "schema": SCHEMA, "kind": "module", "api_version": API_VERSION,
                "generated_at": generated_at, "status": "RESEARCH_ONLY",
                "disclaimer": DISCLAIMER, "module": item,
                "links": {"self": f"modules/{item['module_id']}.json",
                          "all": "modules.json", "index": "index.json"},
            }
            path = module_dir / f"{item['module_id']}.json"
            _write_json(path, single)
            record(path)
    index = build_index(modules, generated_at, files, registry, api_version)
    index_path = out_dir / "index.json"
    _write_json(index_path, index)
    openapi_path = out_dir / "openapi.json"
    _write_json(openapi_path, build_openapi(modules, registry, api_version))
    (out_dir / "README.md").write_text(_readme(index, api_version), encoding="utf-8",
                                       newline="\n")
    return {
        "schema": index["schema"],
        "api_version": api_version,
        "status": "OK",
        "generated_at": generated_at,
        "out": str(out_dir).replace("\\", "/"),
        "counts": index["counts"],
        "registry": None if registry is None else {"path": registry["path"],
                                                   "sha256": registry["sha256"]},
        "files": [{"path": entry["path"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                  for entry in index["files"]]
        + [{"path": "index.json", "sha256": _sha256(index_path),
            "bytes": index_path.stat().st_size},
           {"path": "openapi.json", "sha256": _sha256(openapi_path),
            "bytes": openapi_path.stat().st_size}],
        "index_sha256": _sha256(index_path),
        "openapi_sha256": _sha256(openapi_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the static AMFE Forecast API contract (amfe.forecast_api.v2)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (build/api/)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 timestamp (determinism); default: current UTC")
    parser.add_argument("--api-version", choices=SUPPORTED_API_VERSIONS, default=API_VERSION,
                        help="API contract version to emit (v2 default; v1 = legacy output)")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                        help="module registry JSONL (v2; default build/registry/modules.jsonl)")
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = build_api(Path(args.out), generated_at=args.generated_at,
                            registry_path=Path(args.registry), api_version=args.api_version)
    except ForecastApiError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps({"status": summary["status"], "api_version": summary["api_version"],
                          "out": summary["out"], "counts": summary["counts"],
                          "index_sha256": summary["index_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
