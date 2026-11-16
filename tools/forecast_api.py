#!/usr/bin/env python3
"""Static Forecast API contract for the AMFE market modules (schema ``amfe.forecast_api.v1``).

The tool does **not** run a server: it materialises the deterministic JSON responses a read-only
API would return, so the contract can be reviewed, versioned and served by any static host.

Endpoints (all read-only, ``GET``):

* ``/v1/scenarios``               -- index of modules, scenarios and forecast targets;
* ``/v1/scenarios/{scenario_id}`` -- one scenario document with the real ``MarketResult`` fields;
* ``/v1/forecast/{target}``       -- the scenario envelope for a target (oil: Brent, ``oil_brent``).

Every number is copied from ``amfe.markets`` (``OilModule`` / ``MarketResult.as_dict``); the tool
never invents a number, never stores raw third-party observations and never returns a point
forecast.  All responses carry ``status = "RESEARCH_ONLY"`` and an explicit disclaimer.

Fail-closed: if the market module package is unavailable/unregistered, or a scenario/target does
not exist, the tool raises :class:`ForecastApiError` and writes nothing.

Usage:
    python tools/forecast_api.py --out build/api --generated-at 2026-11-16T10:00:00Z
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

SCHEMA = "amfe.forecast_api.v1"
API_VERSION = "v1"
STATUS = "RESEARCH_ONLY"
DEFAULT_OUT = ROOT / "build" / "api"
OPENAPI_VERSION = "3.0.3"
DISCLAIMER = (
    "Research-only scenario arithmetic, not a forecast, not an admission decision and not "
    "investment advice. Numbers are produced by the deterministic amfe.markets engine from declared "
    "assumptions; the reported range is a range over sensitivity variants, not a confidence "
    "interval. No third-party observations are redistributed."
)
TARGET_ALIASES = ("oil.brent", "oil_brent", "brent")


class ForecastApiError(RuntimeError):
    """The API contract cannot be built (missing module/package) or a resource does not exist."""


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


def get_modules(modules=None) -> tuple:
    """Return the registered market modules (fail-closed when the package is unavailable)."""
    if modules is not None:
        modules = tuple(modules)
        if not modules:
            raise ForecastApiError("no market modules registered: cannot build the API")
        return modules
    try:
        from amfe.markets import registered_modules
    except ImportError as exc:  # pragma: no cover - environment without src/amfe/markets
        raise ForecastApiError(f"oil module package unavailable: {exc!r}") from exc
    modules = tuple(registered_modules())
    if not modules:
        raise ForecastApiError("no market modules registered: cannot build the API")
    return modules


def target_id(module) -> str:
    """Stable API target id for a module (``oil.brent`` -> ``oil_brent``)."""
    return str(module.module_id).replace(".", "_")


def module_document(module) -> dict:
    """Module passport plus the API target; no result numbers are invented here."""
    described = module.describe()
    return {
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


def _scenario_key(module, scenario_id: str) -> str:
    return f"{module.module_id}/{scenario_id}"


def _summary_excerpt(result: dict) -> dict:
    """The small numbers an index is allowed to repeat (all taken from the result)."""
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


def build_scenario(module, scenario_id: str, generated_at: str) -> dict:
    """One ``GET /v1/scenarios/{scenario_id}`` response built from the real MarketResult."""
    try:
        result = module.run(scenario_id)
    except Exception as exc:
        raise ForecastApiError(f"scenario {scenario_id!r} for {module.module_id}: {exc}") from exc
    payload = result.as_dict()
    return {
        "schema": SCHEMA,
        "kind": "scenario",
        "api_version": API_VERSION,
        "generated_at": generated_at,
        "status": payload["status"],
        "disclaimer": DISCLAIMER,
        "id": _scenario_key(module, scenario_id),
        "module": module_document(module),
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


def build_forecast(module, target: str, generated_at: str) -> dict:
    """One ``GET /v1/forecast/{target}`` response: the real scenario envelope for the target."""
    expected = target_id(module)
    if target not in (expected, module.module_id) and not (
            module.module_id == "oil.brent" and target in TARGET_ALIASES):
        raise ForecastApiError(
            f"unknown forecast target {target!r}; available [{expected}]")
    scenario_id = module.default_scenario().scenario_id
    result = module.run(scenario_id)
    payload = result.as_dict()
    summary = payload["summary"]
    range_peak = summary["brent_range_usd_per_bbl"]
    range_mean = summary["brent_range_mean_usd_per_bbl"]
    return {
        "schema": SCHEMA,
        "kind": "forecast_envelope",
        "api_version": API_VERSION,
        "generated_at": generated_at,
        "status": payload["status"],
        "disclaimer": DISCLAIMER,
        "target": target,
        "target_definition": {
            "commodity": payload["commodity"],
            "units": payload["units"],
            "horizon_months": payload["horizon_months"],
            "default_scenario": scenario_id,
            "definition": "Brent price path and its peak/mean range under the declared Hormuz "
                          "scenario; a scenario envelope, not a point forecast.",
        },
        "module_id": payload["module_id"],
        "envelope": {
            "price_path": payload["price_path"],
            "price_peak": summary["price_peak"],
            "price_mean": summary["price_mean"],
            "price_trough": summary["price_trough"],
            "price_peak_month": summary["price_peak_month"],
            "range_peak": range_peak,
            "range_mean": range_mean,
            "sensitivity": payload["sensitivity"],
            "checks": payload["checks"],
            "units": payload["units"],
        },
        "assumptions_count": len(payload["assumptions"]),
        "assumptions_classes": sorted({item["source_class"] for item in payload["assumptions"]}),
        "result_digest": payload["digest"],
        "research_only": True,
        "no_point_forecast": True,
    }


def build_index(modules, generated_at: str, files: list[dict] | None = None) -> dict:
    """``GET /v1/scenarios`` index: modules, scenarios, targets and file map."""
    scenarios, modules_doc = [], []
    for module in modules:
        modules_doc.append(module_document(module))
        for scenario in module.scenarios():
            result = module.run(scenario.scenario_id).as_dict()
            scenarios.append({
                "id": _scenario_key(module, scenario.scenario_id),
                "module_id": module.module_id,
                "scenario_id": scenario.scenario_id,
                "description": scenario.description,
                "horizon_months": int(scenario.horizon_months),
                "parameters": dict(scenario.parameters),
                "status": result["status"],
                "digest": result["digest"],
                "summary": _summary_excerpt(result),
                "file": f"scenarios/{module.module_id}__{scenario.scenario_id}.json",
            })
    forecasts = [{"target": target_id(module), "module_id": module.module_id,
                  "commodity": module.commodity, "units": module.units,
                  "file": f"forecasts/{target_id(module)}.json"} for module in modules]
    index = {
        "schema": SCHEMA,
        "kind": "index",
        "api_version": API_VERSION,
        "base_path": "/v1",
        "generated_at": generated_at,
        "status": STATUS,
        "disclaimer": DISCLAIMER,
        "policy": {
            "read_only": True,
            "research_only": True,
            "no_admission": True,
            "no_point_forecast": True,
            "no_raw_third_party_data": True,
            "deterministic_except_generated_at": True,
        },
        "modules": modules_doc,
        "scenarios": scenarios,
        "forecasts": forecasts,
        "endpoints": [
            {"method": "GET", "path": "/v1/scenarios", "file": "index.json",
             "description": "index of modules, scenarios and forecast targets"},
            {"method": "GET", "path": "/v1/scenarios/{scenario_id}",
             "file": "scenarios/<module>__<scenario>.json",
             "description": "one scenario with the real MarketResult fields"},
            {"method": "GET", "path": "/v1/forecast/{target}",
             "file": "forecasts/<target>.json",
             "description": "scenario envelope for a target (not a point forecast)"},
            {"method": "GET", "path": "/openapi.json", "file": "openapi.json",
             "description": "OpenAPI 3.0.2 description of the contract"},
        ],
        "counts": {"modules": len(modules), "scenarios": len(scenarios),
                   "forecasts": len(forecasts)},
        "openapi": "openapi.json",
    }
    if files is not None:
        index["files"] = sorted(files, key=lambda item: item["path"])
    return index


def build_openapi(modules) -> dict:
    """OpenAPI 3.0 description of the three read-only endpoints (no numbers invented)."""
    module_enum = [target_id(module) for module in modules]
    module_ids = [module.module_id for module in modules]
    scenario_ids = sorted(f"{module.module_id}/{scenario.scenario_id}"
                          for module in modules for scenario in module.scenarios())
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "AMFE Forecast API (research contract)",
            "version": "1.0.0",
            "description": "Read-only static contract over the deterministic AMFE market modules. "
                           "Research-only: no admissions, no point forecasts, no raw third-party "
                           "observations. The shipped JSON files are the exact responses.",
            "license": {"name": "Research use only; see 08_Licence_and_Disclaimer"},
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": "forecast", "description": "scenario envelopes"},
                 {"name": "scenarios", "description": "market scenarios"}],
        "paths": {
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
                    "summary": "Scenario envelope for a target (not a point forecast)",
                    "parameters": [_params("target", "path", "target id, e.g. oil_brent",
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
        },
        "components": {
            "schemas": {
                "Index": {"type": "object", "required": ["schema", "api_version", "status",
                                                          "modules", "scenarios", "forecasts"],
                          "properties": {"schema": {"type": "string"},
                                         "kind": {"type": "string", "const": "index"},
                                         "api_version": {"type": "string"},
                                         "generated_at": {"type": "string", "format": "date-time"},
                                         "status": {"type": "string", "enum": ["RESEARCH_ONLY"]},
                                         "modules": {"type": "array", "items": {"type": "object"}},
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
                                            "scenario": {"type": "object"},
                                            "result": {"type": "object",
                                                       "required": ["price_path", "summary",
                                                                    "assumptions", "sensitivity",
                                                                    "checks", "digest"]}}},
                "Forecast": {"type": "object",
                             "required": ["schema", "kind", "status", "target", "envelope"],
                             "properties": {"schema": {"type": "string"},
                                            "kind": {"type": "string",
                                                     "const": "forecast_envelope"},
                                            "target": {"type": "string"},
                                            "status": {"type": "string"},
                                            "target_definition": {"type": "object"},
                                            "envelope": {"type": "object",
                                                         "required": ["price_path", "range_peak",
                                                                      "range_mean", "sensitivity"]},
                                            "research_only": {"type": "boolean"}}},
                "Error": {"type": "object", "required": ["status", "error"],
                          "properties": {"status": {"type": "string", "enum": ["NOT_FOUND"]},
                                         "error": {"type": "string"}}},
            }
        },
        "x-module-ids": module_ids,
    }


def build_api(out_dir: Path = DEFAULT_OUT, generated_at: str | None = None,
              modules=None) -> dict:
    """Write ``index.json``, ``scenarios/*.json``, ``forecasts/*.json``, ``openapi.json``."""
    generated_at = generated_at or _now()
    modules = get_modules(modules)
    out_dir = Path(out_dir)
    scenario_dir = out_dir / "scenarios"
    forecast_dir = out_dir / "forecasts"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    forecast_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    for module in modules:
        for scenario in module.scenarios():
            document = build_scenario(module, scenario.scenario_id, generated_at)
            path = scenario_dir / f"{module.module_id}__{scenario.scenario_id}.json"
            _write_json(path, document)
            files.append({"path": str(path.relative_to(out_dir)).replace("\\", "/"),
                          "sha256": _sha256(path), "bytes": path.stat().st_size})
        forecast = build_forecast(module, target_id(module), generated_at)
        path = forecast_dir / f"{target_id(module)}.json"
        _write_json(path, forecast)
        files.append({"path": str(path.relative_to(out_dir)).replace("\\", "/"),
                      "sha256": _sha256(path), "bytes": path.stat().st_size})
    index = build_index(modules, generated_at, files)
    index_path = out_dir / "index.json"
    _write_json(index_path, index)
    openapi_path = out_dir / "openapi.json"
    _write_json(openapi_path, build_openapi(modules))
    readme = _readme(index)
    (out_dir / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    return {
        "schema": SCHEMA,
        "status": "OK",
        "generated_at": generated_at,
        "out": str(out_dir).replace("\\", "/"),
        "counts": index["counts"],
        "files": [{"path": entry["path"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
                  for entry in index["files"]]
        + [{"path": "index.json", "sha256": _sha256(index_path),
            "bytes": index_path.stat().st_size},
           {"path": "openapi.json", "sha256": _sha256(openapi_path),
            "bytes": openapi_path.stat().st_size}],
        "index_sha256": _sha256(index_path),
        "openapi_sha256": _sha256(openapi_path),
    }


def _write_json(path: Path, document: dict) -> None:
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1)
               + "\n").encode("utf-8")
    Path(path).write_bytes(payload)


def _readme(index: dict) -> str:
    lines = [
        "# AMFE Forecast API (static research contract)",
        "",
        f"Schema: `{SCHEMA}` · API: `{API_VERSION}` · status: **{STATUS}** · "
        f"generated: {index['generated_at']}.",
        "",
        "> " + DISCLAIMER,
        "",
        "## Files",
        "",
        "| File | Endpoint |",
        "|---|---|",
        "| `index.json` | `GET /v1/scenarios` |",
        "| `scenarios/<module>__<scenario>.json` | `GET /v1/scenarios/{scenario_id}` |",
        "| `forecasts/<target>.json` | `GET /v1/forecast/{target}` |",
        "| `openapi.json` | OpenAPI description |",
        "",
        "## Contents",
        "",
    ]
    for scenario in index["scenarios"]:
        lines.append(f"- `{scenario['id']}` — {scenario['file']} "
                     f"(digest `{scenario['digest'][:16]}…`)")
    for forecast in index["forecasts"]:
        lines.append(f"- forecast target `{forecast['target']}` — {forecast['file']}")
    lines += [
        "",
        "Serve the directory with any static HTTP server; responses are pre-computed and "
        "byte-stable except `generated_at`.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the static AMFE Forecast API contract (amfe.forecast_api.v1)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (build/api/)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 timestamp (determinism); default: current UTC")
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = build_api(Path(args.out), generated_at=args.generated_at)
    except ForecastApiError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps({"status": summary["status"], "out": summary["out"],
                          "counts": summary["counts"], "index_sha256": summary["index_sha256"]},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
