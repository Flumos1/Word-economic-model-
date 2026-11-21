#!/usr/bin/env python3
"""Client Pack v2 — one deterministic client package aggregating the real artefacts.

The pack is an *aggregator*, not a model: every number is read from committed/generated artefacts and
copied (or counted) — nothing is re-estimated, invented or scraped:

* ``build/product/forecast_latest.json`` — the product feed of the scored candidate
  (status CANDIDATE / RESEARCH_ONLY; no admission is ever claimed);
* ``build/registry/modules.jsonl`` — the append-only module registry with RESEARCH_ONLY passports;
* ``build/api/index.json`` and **every forecast contract it lists** (``forecasts/oil_brent.json``,
  ``forecasts/gas_ttf.json``, …) plus one market scenario (``hormuz_closed_6m`` by default);
* optionally, ``build/forecast_world_bridge/report.json`` (or ``--bridge``) — the reduced-form
  forecast→world bridge, reduced to a scenario × country/industry top-effects table with its
  source and SHA-256.  The block is optional: a missing artefact yields ``available: false`` with
  an explicit reason and the pack still builds (fail-open); a present artefact that is corrupt,
  foreign (wrong schema/status), carries raw-data markers or contradicts its ``.sha256`` sidecar
  fails the build closed.

Counts (passports, market modules, scenario, forecasts) are **read from the registry and the API
index**, never hard-coded: the 5-passport / 1-market-module v1 layout still builds, while a v2 index
with 7 passports and 2 market modules (oil + gas) is aggregated as-is.

Outputs: DOCX executive summary / methodology / licence, XLSX numbers, a static HTML dashboard, a
machine-readable JSON, a reproducibility manifest and a README. The build is fail-closed: if any
required artefact is missing, unparsable, of an unexpected status (e.g. an admitted model) or if the
scenario id is unknown, the tool raises before touching the output directory.

Determinism follows the same approach as ``tools/market_client_pack.py``: the pack's
``--generated-at`` (or, by default, the newest input timestamp) is used for every document, and the
DOCX/XLSX zip entries and ``docProps/core.xml`` dates are pinned to it, so two builds with the same
inputs and the same timestamp are byte-identical.

Usage::

    python tools/client_pack_v2.py --generated-at 2026-11-17T00:00:00Z \
        --out build/client_pack/v2 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from html import escape as _escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "amfe.client_pack.v2"
REPRO_SCHEMA = "amfe.client_pack.repro.v2"
STATUS = "RESEARCH_ONLY"
DEFAULT_CLIENT = "Research client"
DEFAULT_SCENARIO = "hormuz_closed_6m"
DEFAULT_PACK_NAME = "amfe_client_pack_v2"
ALLOWED_CANDIDATE_STATUSES = ("CANDIDATE", "RESEARCH_ONLY")
ALLOWED_MODULE_STATUSES = ("RESEARCH_ONLY",)
FALLBACK_GENERATED_AT = "2026-11-17T00:00:00Z"

DISCLAIMER = (
    "Research only. This pack is an aggregation of the AMFE research artefacts, not a forecast, not "
    "an admission decision and not investment advice. No model is admitted: the published candidate "
    "keeps the CANDIDATE / RESEARCH_ONLY status, C4 and C6 remain open, and the scenario numbers are "
    "scenario arithmetic over declared assumptions, not a confidence interval."
)
LICENCE = {
    "research_only": True,
    "no_admission": True,
    "no_investment_advice": True,
    "no_third_party_observations": True,
    "third_party_data_committed": False,
    "note": ("Only the project's own forecasts, aggregated metrics and derived statistics are "
             "published; raw third-party observations stay in the private CI artefacts "
             "(protocols/licence_register.json, docs/84, docs/90)."),
    "documents": ["docs/84_Лицензионный_аудит_2_2026-11-12.md",
                  "docs/89_Релизный_чеклист_этап9.md",
                  "docs/161_Реестр_API_паспорта.md",
                  "docs/171_Client_Pack_v2.md"],
}

INPUT_PATHS = {
    "product_feed": "product/forecast_latest.json",
    "registry": "registry/modules.jsonl",
    "api_index": "api/index.json",
}
SCENARIO_PATH = "api/scenarios/oil.brent__{scenario}.json"
DEFAULT_FORECAST_TARGET = "oil_brent"
LEGACY_FORECAST_PATH = "forecasts/oil_brent.json"
MODULE_FIELDS = ("module_id", "display_name", "version", "kind", "status", "owner", "summary",
                 "limitations", "code_sha256", "data_sha256", "data_status", "components",
                 "entrypoints", "docs", "tests", "created_at")

# Optional reduced-form bridge (tools/forecast_world_bridge.py).  Absence is fail-open, a present
# but invalid artefact is fail-closed.
BRIDGE_SCHEMA = "amfe.forecast_world_bridge.v1"
BRIDGE_DIR = "forecast_world_bridge"
BRIDGE_FILE_NAMES = ("report.json", "forecast_world.json")
BRIDGE_STATUSES = ("RESEARCH_ONLY", "PARTIAL_INPUTS")
BRIDGE_SCENARIO_STATUSES = ("OK", "BLOCKED_INPUT")
BRIDGE_RAW_MARKERS = ("data/mrio", "data\\mrio", "dataset.json", "\\data\\", "F:\\", "F:/")
BRIDGE_RAW_KEYS = frozenset({"Z", "Z_sparse", "x", "y", "va", "sector_rows", "sector_cols"})
BRIDGE_DOC_TOP = 5
BRIDGE_NOTE = (
    "reduced-form: мост переводит точечный прогноз и интервал T1/T2/T3 в сценарные агрегаты "
    "мира через уже опубликованные эластичности (ценовой канал quantity_bridge и агрегатный "
    "множитель Леонтьева). Это сравнительная статика, НЕ прогноз мира и НЕ допуск; аллокация "
    "страна×отрасль сепарабельная (внешнее произведение маргиналов), кросс-взаимодействие "
    "не моделируется, ошибка растёт с шагом шока."
)

# Optional blocks 15/17 (docs/203, docs/204).  The pack aggregates the artefacts of
# tools/institutions_scenarios.py and tools/technology_scenarios.py; absence is fail-open (the
# block is marked available=false), a present but invalid artefact fails the build closed.
BLOCKS_SCHEMA = "amfe.client_pack.blocks.v1"
BLOCKS_STATUSES = ("OK", "PARTIAL", "NOT_AVAILABLE", "NOT_REQUESTED")
BLOCK_ARTEFACT_STATUS = "RESEARCH_ONLY"
BLOCK_SPECS = (
    {
        "id": "block_15_institutions",
        "block": 15,
        "module_id": "world.institutions",
        "title": "Блок 15 · Институты (модификатор сценариев)",
        "artifact": "institutions/report.json",
        "fallbacks": (),
        "schema": "amfe.world.institutions.report.v1",
        "command": "python tools/institutions_scenarios.py --out build/institutions",
        "docs": "docs/203_Блок_15_институты.md",
    },
    {
        "id": "block_17_technology",
        "block": 17,
        "module_id": "world.technology_diffusion",
        "title": "Блок 17 · Наука, технологии, ИИ",
        "artifact": "technology_scenarios/report.json",
        "fallbacks": ("technology_scenarios/demo/demo.json",),
        "schema": "amfe.world.technology_report.v1",
        "command": "python tools/technology_scenarios.py --out build/technology_scenarios",
        "docs": "docs/204_Блок_17_технологии_ИИ.md",
    },
)
BLOCKS_NOTE = (
    "Блоки 15/17 — сценарные модификаторы на объявленных допущениях, не прогноз и не "
    "forecast-контракты: числа показывают механику («что если правила/внедрение сдвинутся»), а не "
    "ожидаемый эффект; калибровки нет (NOT_CALIBRATED), admission=false, страновых значений в "
    "блоке 15 нет. В forecast-контракты и в точечный прогноз пакета они не входят."
)


class ClientPackError(RuntimeError):
    """Fail-closed aggregation error (missing/corrupt/unsafe input artefact)."""


# ---------------------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_sha256(payload) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ClientPackError(f"{label}: expected YYYY-MM-DDTHH:MM:SSZ, got {value!r}") from exc


def _fmt(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _short(value: str | None, size: int = 12) -> str:
    return (value or "")[:size]


def _repo_path(root: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# ------------------------------------------------------------- optional bridge (forecast → world)
def _finite(value):
    """A finite float from a JSON number, or ``None`` for anything else (bool/None/NaN/∞)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bridge_default_paths(inputs_root: Path) -> list[Path]:
    return [Path(inputs_root) / BRIDGE_DIR / name for name in BRIDGE_FILE_NAMES]


def _resolve_bridge_path(inputs_root: Path, explicit, root: Path) -> tuple[Path | None, list[Path]]:
    """Resolve the optional bridge artefact; refuse traversal and paths outside the artefact roots.

    ``explicit`` may name a JSON file or a directory containing ``report.json`` /
    ``forecast_world.json`` (the natural target is a ``tools/forecast_world_bridge.py`` output
    directory).  Without ``explicit`` the default ``<inputs_root>/forecast_world_bridge/`` is
    searched and ``(None, searched)`` is returned when nothing is there.
    """
    inputs_root, root = Path(inputs_root), Path(root)
    searched = _bridge_default_paths(inputs_root)
    if explicit is None or not str(explicit).strip():
        for candidate in searched:
            if candidate.is_file():
                return candidate.resolve(), searched
        return None, searched
    raw = Path(str(explicit).strip())
    if ".." in raw.parts:
        raise ClientPackError(
            f"bridge: unsafe path {str(explicit)!r} (parent traversal is refused)")
    if raw.is_absolute():
        candidate = raw.resolve()
        inside = False
        for base in (inputs_root.resolve(), root.resolve()):
            try:
                candidate.relative_to(base)
                inside = True
                break
            except ValueError:
                continue
        if not inside:
            raise ClientPackError(
                f"bridge: unsafe path {str(explicit)!r}: outside the artefact roots")
    else:
        candidate = root / raw
        if not candidate.exists():
            candidate = inputs_root / raw
        candidate = candidate.resolve()
    if candidate.is_dir():
        for name in BRIDGE_FILE_NAMES:
            inner = candidate / name
            if inner.is_file():
                return inner.resolve(), [inner]
        raise ClientPackError(
            f"bridge: directory {str(explicit)!r} has no {' / '.join(BRIDGE_FILE_NAMES)}")
    if not candidate.is_file():
        raise ClientPackError(f"bridge: explicit artefact is missing: {str(explicit)!r}")
    return candidate, [candidate]


def _bridge_sidecar_sha(path: Path) -> str | None:
    """Read ``<file>.sha256`` next to the artefact when present (optional integrity pin)."""
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.is_file():
        return None
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClientPackError(
            f"bridge: cannot read the digest sidecar {sidecar.name}: {exc!r}") from exc
    match = re.search(r"[0-9a-fA-F]{64}", text)
    if not match:
        raise ClientPackError(f"bridge: the digest sidecar {sidecar.name} has no SHA-256")
    return match.group(0).lower()


def _bridge_raw_markers(payload) -> list[str]:
    """Raw-data markers in a bridge payload (must be empty: only aggregates are published)."""
    problems: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key in BRIDGE_RAW_KEYS:
                    problems.append(f"{path}.{key}: raw key")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for position, value in enumerate(node):
                walk(value, f"{path}[{position}]")
        elif isinstance(node, str):
            for marker in BRIDGE_RAW_MARKERS:
                if marker in node:
                    problems.append(f"{path}: raw path marker {marker!r}")

    walk(payload, "$")
    return problems


def _validate_bridge(document: dict, path: Path) -> None:
    """Fail-closed validation of a present bridge artefact (schema, status, numbers, no raw)."""
    name = Path(path).name
    if document.get("schema") != BRIDGE_SCHEMA:
        raise ClientPackError(
            f"bridge: {name} is a foreign artefact (schema {document.get('schema')!r}, "
            f"expected {BRIDGE_SCHEMA!r})")
    if document.get("status") not in BRIDGE_STATUSES:
        raise ClientPackError(
            f"bridge: {name} has status {document.get('status')!r}, expected one of "
            f"{list(BRIDGE_STATUSES)}")
    if document.get("admission") is not False:
        raise ClientPackError(f"bridge: {name} does not declare admission=false")
    model = document.get("model")
    if not isinstance(model, dict) or not model.get("dataset"):
        raise ClientPackError(f"bridge: {name} has no model.dataset")
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ClientPackError(f"bridge: {name} has an empty scenarios[]")
    seen: set[str] = set()
    for item in scenarios:
        if not isinstance(item, dict) or not item.get("id"):
            raise ClientPackError(f"bridge: {name}: a scenario has no id")
        if item["id"] in seen:
            raise ClientPackError(f"bridge: {name}: duplicate scenario {item['id']!r}")
        seen.add(item["id"])
        if item.get("status") not in BRIDGE_SCENARIO_STATUSES:
            raise ClientPackError(
                f"bridge: {name}: scenario {item['id']!r} has status {item.get('status')!r}")
        combined = item.get("combined")
        if not isinstance(combined, dict):
            raise ClientPackError(f"bridge: {name}: scenario {item['id']!r} has no combined block")
        if item["status"] == "OK" and _finite(combined.get("delta_output_usd_mn")) is None:
            raise ClientPackError(
                f"bridge: {name}: scenario {item['id']!r} has a non-numeric delta_output_usd_mn")
        for cell in item.get("top_country_industry") or []:
            if not isinstance(cell, dict) or not cell.get("country") or not cell.get("industry"):
                raise ClientPackError(
                    f"bridge: {name}: scenario {item['id']!r}: a top cell has no country/industry")
            if _finite(cell.get("delta_output")) is None:
                raise ClientPackError(
                    f"bridge: {name}: scenario {item['id']!r}: a top cell has a non-numeric delta")
    problems = _bridge_raw_markers(document)
    if problems:
        raise ClientPackError(
            f"bridge: {name} carries raw-data markers: " + "; ".join(problems[:5]))
    checks = document.get("checks")
    if not isinstance(checks, dict) or checks.get("no_raw_data") is not True:
        raise ClientPackError(f"bridge: {name} does not declare checks.no_raw_data=true")


def load_bridge(inputs_root: Path, explicit, root: Path) -> dict:
    """Load the optional bridge block: fail-open on absence, fail-closed on a bad artefact."""
    path, searched = _resolve_bridge_path(inputs_root, explicit, root)
    if path is None:
        relative = [_repo_path(root, item) for item in searched]
        return {
            "available": False, "path": None, "sha256": None, "sidecar_sha256": None,
            "document": None,
            "section": {
                "available": False,
                "status": "NOT_AVAILABLE",
                "optional": True,
                "reason": ("bridge-артефакт не найден: " + " / ".join(relative) +
                           " (запустите tools/forecast_world_bridge.py или укажите --bridge)"),
                "searched": relative,
                "source": None,
                "sha256": None,
                "dataset": None,
                "scenarios": [],
                "effects": [],
                "note": ("опциональный блок: пакет собирается без него; это не ошибка данных, "
                         "но раздел явно помечен как недоступный"),
            },
        }
    document = _read_json(path, "bridge")
    digest = _sha256(path)
    sidecar = _bridge_sidecar_sha(path)
    if sidecar is not None and sidecar != digest:
        raise ClientPackError(
            f"bridge: SHA-256 sidecar {Path(str(path) + '.sha256').name} does not match the "
            f"artefact ({sidecar[:12]} != {digest[:12]})")
    _validate_bridge(document, path)
    return {"available": True, "path": path, "sha256": digest, "sidecar_sha256": sidecar,
            "document": document, "section": None}


def _bridge_section(bridge: dict, root: Path) -> dict:
    """Reduce the bridge payload to the client-facing block (scenario → country/industry)."""
    if not bridge.get("available"):
        return dict(bridge["section"])
    document = bridge["document"]
    path = bridge["path"]
    model = document.get("model") or {}
    dataset_info = model.get("dataset_info") or {}
    price_calibration = model.get("price_channel") or {}
    growth_calibration = model.get("growth_channel") or {}
    source_checks = document.get("checks") or {}
    risk = document.get("risk_overlay") or {}
    scenarios: list[dict] = []
    effects: list[dict] = []
    for scenario in document["scenarios"]:
        combined = scenario.get("combined") or {}
        price_channel = scenario.get("price_channel") or {}
        shifts = scenario.get("shifts") or {}
        interval = combined.get("interval") or {}
        step = _finite(price_channel.get("relative_price_shock"))
        relative = _finite(combined.get("relative_output"))
        top: list[dict] = []
        if scenario.get("status") == "OK":
            for cell in scenario.get("top_country_industry") or []:
                row = {
                    "scenario": scenario["id"],
                    "country": str(cell["country"]),
                    "industry": str(cell["industry"]),
                    "delta_output_usd_mn": _finite(cell.get("delta_output")),
                    "share_of_abs": _finite(cell.get("share_of_abs")),
                }
                top.append(row)
                effects.append(row)
        scenarios.append({
            "id": scenario["id"],
            "label": scenario.get("label"),
            "status": scenario.get("status"),
            "shifts_pp": {"cpi": _finite(shifts.get("cpi_pp")),
                          "growth": _finite(shifts.get("growth_pp"))},
            "price_step_pct": round(step * 100.0, 9) if step is not None else None,
            "delta_output_usd_mn": _finite(combined.get("delta_output_usd_mn")),
            "relative_output_pct": round(relative * 100.0, 9) if relative is not None else None,
            "interval_usd_mn": [_finite(interval.get("delta_low")),
                                _finite(interval.get("delta_high"))],
            "base_output_usd_mn": _finite(combined.get("base_output_usd_mn")),
            "top_effects": top,
            "effects_count": len(top),
            "reason": scenario.get("reason"),
        })
    scenario_checks = [item["checks"] for item in document["scenarios"]
                       if item.get("status") == "OK"
                       and isinstance(item.get("checks"), dict)
                       and "conservation" in item["checks"]]
    return {
        "available": True,
        "status": "OK",
        "optional": True,
        "schema": document.get("schema"),
        "artefact_status": document.get("status"),
        "admission": False,
        "generated_at": document.get("generated_at"),
        "source": _repo_path(root, path),
        "sha256": bridge["sha256"],
        "sha256_verified": "sidecar" if bridge.get("sidecar_sha256") else "file",
        "bytes": Path(path).stat().st_size,
        "dataset": {
            "id": model.get("dataset"),
            "label": model.get("dataset_label"),
            "source": dataset_info.get("source"),
            "units": dataset_info.get("units"),
            "countries": dataset_info.get("countries"),
            "industries": dataset_info.get("industries"),
            "sectors": dataset_info.get("sectors"),
        },
        "calibration": {
            "price_sensitivity": _finite(price_calibration.get("sensitivity")),
            "aggregate_relative_price": _finite(price_calibration.get("relative_output")),
            "growth_multiplier": _finite(growth_calibration.get("value")),
            "pass_through": _finite(model.get("pass_through")),
            "channel": model.get("channel_used"),
            "horizon": model.get("horizon"),
        },
        "risk_overlay": {
            "target": risk.get("target"),
            "status": risk.get("status"),
            "probability": _finite(risk.get("probability")),
            "threshold": _finite(risk.get("threshold")),
            "flag": risk.get("flag"),
            "effect_on_numbers": risk.get("effect_on_numbers"),
        },
        "scenarios": scenarios,
        "effects": effects,
        "checks": {
            "no_raw_data": True,
            "admission": False,
            "mode": source_checks.get("mode"),
            "scenarios_ok": list(source_checks.get("scenarios_ok") or []),
            "scenarios_blocked": list(source_checks.get("scenarios_blocked") or []),
            "allocated_scenarios": len(scenario_checks),
            "conservation": bool(scenario_checks) and all(
                item.get("conservation") is True for item in scenario_checks),
            "country_margins": bool(scenario_checks) and all(
                item.get("country_margins") is True for item in scenario_checks),
            "industry_margins": bool(scenario_checks) and all(
                item.get("industry_margins") is True for item in scenario_checks),
            "relative_matches_channel": bool(scenario_checks) and all(
                item.get("relative_matches_channel") is True for item in scenario_checks),
            "fail_closed": source_checks.get("fail_closed"),
        },
        "limitations": [str(item) for item in document.get("limitations") or []],
        "note": BRIDGE_NOTE,
    }


# ------------------------------------------------------------- optional blocks (15/17, docs/203-204)
def _block_candidates(inputs_root: Path, spec: dict) -> list[Path]:
    names = [spec["artifact"], *spec.get("fallbacks", ())]
    return [Path(inputs_root) / name for name in names]


def _block_unavailable(spec: dict, searched: list[str], reason: str) -> dict:
    return {
        "id": spec["id"], "block": spec["block"], "module_id": spec["module_id"],
        "title": spec["title"], "available": False, "status": "NOT_AVAILABLE",
        "reason": reason, "searched": searched, "source": None, "sha256": None,
        "schema": spec["schema"], "scenarios": [], "assumptions": [],
        "assumptions_count": 0, "limitations": [], "docs": spec["docs"],
        "note": BLOCKS_NOTE,
    }


def _block_common(spec: dict, document: dict, path: Path, digest: str, root: Path) -> dict:
    return {
        "id": spec["id"], "block": spec["block"], "module_id": spec["module_id"],
        "title": spec["title"], "available": True, "status": BLOCK_ARTEFACT_STATUS,
        "admission": False, "source": _repo_path(root, path), "sha256": digest,
        "schema": document.get("schema"), "data_status": document.get("data_status"),
        "not_calibrated": document.get("not_calibrated"),
        "scenarios": [], "assumptions": [], "assumptions_count": 0,
        "limitations": [str(item) for item in (document.get("limitations") or [])[:5]],
        "docs": spec["docs"], "note": BLOCKS_NOTE,
    }


def _validate_institutions_block(document: dict, path: Path) -> None:
    name = Path(path).name
    spec_schema = BLOCK_SPECS[0]["schema"]
    if document.get("schema") != spec_schema:
        raise ClientPackError(
            f"block 15: {name} is a foreign artefact (schema {document.get('schema')!r}, "
            f"expected {spec_schema!r})")
    if document.get("status") != BLOCK_ARTEFACT_STATUS:
        raise ClientPackError(
            f"block 15: {name} has status {document.get('status')!r}, "
            f"expected {BLOCK_ARTEFACT_STATUS!r}")
    if document.get("admission") is not False or document.get("gating") is not False:
        raise ClientPackError(f"block 15: {name} does not declare admission=false/gating=false")
    index = document.get("index")
    if not isinstance(index, dict):
        raise ClientPackError(f"block 15: {name} has no index block")
    profile = index.get("profile")
    if not isinstance(profile, dict) or not profile.get("name"):
        raise ClientPackError(f"block 15: {name} has no declared profile")
    scenarios = index.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ClientPackError(f"block 15: {name} has an empty index.scenarios[]")
    for entry in scenarios:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ClientPackError(f"block 15: {name}: an index scenario has no id")
        headline = entry.get("headline") or {}
        for key in ("delta_score_points", "delta_risk_premium_bps", "delta_investment_pp",
                    "delta_growth_pp"):
            if _finite(headline.get(key)) is None:
                raise ClientPackError(
                    f"block 15: {name}: scenario {entry['id']!r} has a non-numeric {key}")


def _validate_technology_block(document: dict, path: Path) -> None:
    name = Path(path).name
    spec_schema = BLOCK_SPECS[1]["schema"]
    if document.get("schema") != spec_schema:
        raise ClientPackError(
            f"block 17: {name} is a foreign artefact (schema {document.get('schema')!r}, "
            f"expected {spec_schema!r})")
    if document.get("status") != BLOCK_ARTEFACT_STATUS:
        raise ClientPackError(
            f"block 17: {name} has status {document.get('status')!r}, "
            f"expected {BLOCK_ARTEFACT_STATUS!r}")
    if document.get("admission") is not False:
        raise ClientPackError(f"block 17: {name} does not declare admission=false")
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ClientPackError(f"block 17: {name} has an empty scenarios[]")
    for item in scenarios:
        if not isinstance(item, dict) or not item.get("scenario_id"):
            raise ClientPackError(f"block 17: {name}: a scenario has no scenario_id")
        aggregate = item.get("aggregate") or {}
        horizon = aggregate.get("horizon") or {}
        if _finite(horizon.get("output_gain_pct")) is None:
            raise ClientPackError(
                f"block 17: {name}: scenario {item['scenario_id']!r} has a non-numeric "
                "aggregate.horizon.output_gain_pct")


def _institutions_block_entry(spec: dict, document: dict, path: Path, digest: str,
                              root: Path) -> dict:
    index = document["index"]
    profile = index["profile"]
    parameters = index.get("parameters") or {}
    scenarios = []
    for item in index["scenarios"]:
        headline = item.get("headline") or {}
        scenarios.append({
            "id": item.get("id"),
            "title_ru": item.get("title_ru"),
            "kind": item.get("kind"),
            "ramp_years": item.get("ramp_years"),
            "delta_score_points": _finite(headline.get("delta_score_points")),
            "delta_risk_premium_bps": _finite(headline.get("delta_risk_premium_bps")),
            "delta_investment_pp": _finite(headline.get("delta_investment_pp")),
            "delta_growth_pp": _finite(headline.get("delta_growth_pp")),
            "digest": item.get("digest"),
        })
    assumptions = index.get("assumptions") or []
    entry = _block_common(spec, document, path, digest, root)
    entry.update({
        "profile": profile.get("name"),
        "profile_source": profile.get("source"),
        "score": _finite(profile.get("score")),
        "dispersion": _finite(profile.get("dispersion")),
        "horizon_years": parameters.get("horizon_years"),
        "scenarios": scenarios,
        "assumptions": [{"id": item.get("id"), "kind": item.get("kind"),
                         "statement": str(item.get("statement") or "")[:400]}
                        for item in assumptions[:10]],
        "assumptions_count": len(assumptions),
        "checks": {"scenario_count": len(scenarios),
                   "all_no_raw_data": bool((index.get("checks") or {}).get("all_no_raw_data"))},
    })
    return entry


def _technology_block_entry(spec: dict, document: dict, path: Path, digest: str,
                            root: Path) -> dict:
    scenarios = []
    for item in document["scenarios"]:
        aggregate = item.get("aggregate") or {}
        horizon = aggregate.get("horizon") or {}
        scenarios.append({
            "id": item.get("scenario_id"),
            "title_ru": item.get("title_ru"),
            "adoption": _finite(horizon.get("adoption")),
            "tfp_gain_pct": _finite(horizon.get("tfp_gain_pct")),
            "output_gain_pct": _finite(horizon.get("output_gain_pct")),
            "energy_change_pct": _finite(horizon.get("energy_change_pct")),
            "energy_intensity_change_pct": _finite(horizon.get("energy_intensity_change_pct")),
            "contributions_log": {key: _finite(value) for key, value in
                                  (horizon.get("contributions_log") or {}).items()},
        })
    assumptions = document.get("assumptions") or []
    entry = _block_common(spec, document, path, digest, root)
    entry.update({
        "horizon_years": document.get("horizon_years"),
        "sectors": list(document.get("sectors") or []),
        "growth": document.get("growth") or {},
        "scenarios": scenarios,
        "assumptions": [{"name": item.get("name"), "value": item.get("value"),
                         "unit": item.get("unit"), "source_class": item.get("source_class")}
                        for item in assumptions[:10]],
        "assumptions_count": len(assumptions),
        "checks": {"scenario_count": len(scenarios),
                   "all_no_raw_series": bool((document.get("checks") or {}).get("no_raw_series"))},
    })
    return entry


def _load_one_block(inputs_root: Path, spec: dict, root: Path) -> tuple[dict, Path | None]:
    candidates = _block_candidates(inputs_root, spec)
    searched = [_repo_path(root, item) for item in candidates]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        reason = ("артефакт не найден: " + " / ".join(searched) +
                  f" (запустите {spec['command']})")
        return _block_unavailable(spec, searched, reason), None
    document = _read_json(path, f"block {spec['block']}")
    digest = _sha256(path)
    if spec["block"] == 15:
        _validate_institutions_block(document, path)
        entry = _institutions_block_entry(spec, document, path, digest, root)
    else:
        _validate_technology_block(document, path)
        entry = _technology_block_entry(spec, document, path, digest, root)
    return entry, path


def load_blocks(inputs_root: Path, include: bool, root: Path) -> dict:
    """Load the optional blocks 15/17 section: fail-open on absence, fail-closed on a bad artefact."""
    root = Path(root)
    section = {
        "schema": BLOCKS_SCHEMA, "kind": "blocks_15_17", "included": bool(include),
        "admission": False, "blocks": [], "note": BLOCKS_NOTE,
        "documents": [spec["docs"] for spec in BLOCK_SPECS],
    }
    if not include:
        section.update({
            "available": False,
            "status": "NOT_REQUESTED",
            "reason": ("блоки 15/17 не запрошены: передайте --include-blocks, чтобы добавить "
                       "компактные сценарные разделы (нужны артефакты tools/"
                       "institutions_scenarios.py и tools/technology_scenarios.py)"),
        })
        return {"included": False, "available": False, "status": "NOT_REQUESTED",
                "section": section, "artifacts": []}
    blocks: list[dict] = []
    artifacts: list[dict] = []
    for spec in BLOCK_SPECS:
        entry, path = _load_one_block(Path(inputs_root), spec, root)
        blocks.append(entry)
        if path is not None:
            artifacts.append({"id": spec["id"], "path": path, "sha256": entry["sha256"]})
    available = [entry for entry in blocks if entry["available"]]
    status = ("OK" if len(available) == len(blocks)
              else ("PARTIAL" if available else "NOT_AVAILABLE"))
    section.update({
        "available": bool(available),
        "status": status,
        "blocks": blocks,
        "scenarios_total": sum(len(entry["scenarios"]) for entry in blocks),
    })
    return {"included": True, "available": bool(available), "status": status,
            "section": section, "artifacts": artifacts}


def _read_json(path: Path, label: str) -> dict:
    if not Path(path).is_file():
        raise ClientPackError(f"{label}: required artefact is missing: {path}")
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClientPackError(f"{label}: cannot parse {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise ClientPackError(f"{label}: expected a JSON object in {path}")
    return document


def _read_jsonl(path: Path, label: str) -> list[dict]:
    if not Path(path).is_file():
        raise ClientPackError(f"{label}: required artefact is missing: {path}")
    entries = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ClientPackError(f"{label}: cannot read {path}: {exc!r}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ClientPackError(f"{label}: line {number} is not JSON: {exc!r}") from exc
        if not isinstance(entry, dict) or not entry.get("module_id"):
            raise ClientPackError(f"{label}: line {number} is not a module passport record")
        entries.append(entry)
    if not entries:
        raise ClientPackError(f"{label}: registry is empty")
    return entries


def _write_json(path: Path, document: dict) -> None:
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode()
    Path(path).write_bytes(payload)


def _normalise_office_zip(path: Path, moment: datetime) -> None:
    """Pin docx/xlsx zip entry timestamps and docProps/core.xml dates to ``moment``.

    Same normalisation as tools/market_client_pack.py: python-docx/openpyxl stamp the wall clock, so
    without it two identical rebuilds differ by a second and are not byte-identical.
    """
    import zipfile

    date_time = moment.timetuple()[:6]
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ").encode("utf-8")
    with zipfile.ZipFile(path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in entries:
            if info.filename == "docProps/core.xml":
                data = re.sub(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
                              rb"\g<1>" + stamp + rb"\g<2>", data)
            new_info = zipfile.ZipInfo(info.filename, date_time=date_time)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            target.writestr(new_info, data)
    temporary.replace(path)


# ----------------------------------------------------------------------------------------- inputs
def _forecast_path(inputs_root: Path, relative) -> Path:
    """Resolve ``api/<file>`` from the index, refusing absolute and parent-traversal paths."""
    rel = Path(str(relative))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ClientPackError(f"forecast: unsafe file path {relative!r}")
    return Path(inputs_root) / "api" / rel


def _forecast_specs(index: dict, inputs_root: Path) -> list[dict]:
    """Every forecast contract listed by the API index; legacy oil fallback keeps v1 working."""
    specs: list[dict] = []
    seen: set[str] = set()
    for item in index.get("forecasts") or []:
        if not isinstance(item, dict):
            raise ClientPackError("api_index: forecasts[] contains a non-object entry")
        target, file = item.get("target"), item.get("file")
        if not target or not file:
            raise ClientPackError("api_index: every forecast needs target and file")
        if str(target) in seen:
            raise ClientPackError(f"api_index: duplicate forecast target {target!r}")
        seen.add(str(target))
        specs.append(dict(item))
    if DEFAULT_FORECAST_TARGET not in seen:
        legacy = Path(inputs_root) / "api" / LEGACY_FORECAST_PATH
        if legacy.is_file():
            specs.append({"target": DEFAULT_FORECAST_TARGET, "module_id": "oil.brent",
                          "file": LEGACY_FORECAST_PATH, "units": "USD/bbl",
                          "status": STATUS})
    if not specs:
        raise ClientPackError("api_index: no forecast contracts (forecasts[] is empty)")
    return specs


def _validate_index_counts(index: dict) -> None:
    """Declared ``counts`` must agree with the listed sections (fail-closed, no silent drift)."""
    counts = index.get("counts") or {}
    for section in ("modules", "scenarios", "forecasts", "market_modules"):
        items = index.get(section)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ClientPackError(f"api_index: {section!r} is not a list")
        declared = counts.get(section)
        if declared is not None:
            try:
                declared_count = int(declared)
            except (TypeError, ValueError) as exc:
                raise ClientPackError(
                    f"api_index: counts.{section}={declared!r} is not an integer") from exc
            if declared_count != len(items):
                raise ClientPackError(
                    f"api_index: counts.{section}={declared_count} but {section}[] has "
                    f"{len(items)} entries")


def _validate_forecast(document: dict, spec: dict) -> None:
    if document.get("status") != STATUS:
        raise ClientPackError(f"forecast {spec.get('target')!r}: status is "
                              f"{document.get('status')!r}, expected {STATUS!r}")
    if not isinstance(document.get("envelope"), dict):
        raise ClientPackError(f"forecast {spec.get('target')!r}: envelope is missing")
    if not document.get("module_id"):
        raise ClientPackError(f"forecast {spec.get('target')!r}: module_id is missing")


def load_inputs(inputs_root: Path, scenario_id: str, root: Path,
                bridge_path=None, include_blocks: bool = False) -> dict:
    """Load and validate every artefact; raises ClientPackError (fail-closed) on any problem."""
    inputs_root = Path(inputs_root)
    paths = {key: inputs_root / relative for key, relative in INPUT_PATHS.items()}
    paths["scenario"] = inputs_root / SCENARIO_PATH.format(scenario=scenario_id)
    documents = {}
    for key in ("product_feed", "api_index", "scenario"):
        documents[key] = _read_json(paths[key], key)
    documents["registry"] = _read_jsonl(paths["registry"], "registry")
    documents["bridge"] = load_bridge(inputs_root, bridge_path, root)
    documents["blocks"] = load_blocks(inputs_root, include_blocks, root)

    feed = documents["product_feed"]
    _validate_product_feed(feed)
    registry = documents["registry"]
    _validate_registry(registry)
    index = documents["api_index"]
    if not str(index.get("schema", "")).startswith("amfe.forecast_api."):
        raise ClientPackError(f"api_index: unexpected schema {index.get('schema')!r}")
    if index.get("status") != STATUS:
        raise ClientPackError(f"api_index: status is {index.get('status')!r}, expected {STATUS!r}")
    for key in ("modules", "endpoints", "scenarios", "forecasts"):
        if not index.get(key):
            raise ClientPackError(f"api_index: empty {key!r} section")
    _validate_index_counts(index)

    forecasts: dict[str, dict] = {}
    forecast_paths: dict[str, Path] = {}
    forecast_specs: dict[str, dict] = {}
    for spec in _forecast_specs(index, inputs_root):
        target = str(spec["target"])
        path = _forecast_path(inputs_root, spec["file"])
        document = _read_json(path, f"forecast:{target}")
        _validate_forecast(document, spec)
        forecasts[target] = document
        forecast_paths[target] = path
        forecast_specs[target] = spec
    documents["forecasts"] = forecasts
    documents["forecast_paths"] = forecast_paths
    documents["forecast_specs"] = forecast_specs
    # Legacy key: the oil contract when present, otherwise the first published contract.
    documents["api_forecast"] = forecasts.get(DEFAULT_FORECAST_TARGET) or next(iter(forecasts.values()))
    documents["paths"] = paths
    documents["inputs_root"] = inputs_root
    documents["root"] = root
    documents["scenario_id"] = scenario_id

    scenario = documents["scenario"]
    if scenario.get("status") != STATUS:
        raise ClientPackError(f"scenario: status is {scenario.get('status')!r}, expected {STATUS!r}")
    result = scenario.get("result") or {}
    if not result.get("scenario_id") or not result.get("module_id"):
        raise ClientPackError("scenario: result has no scenario_id/module_id")
    if result.get("scenario_id") != scenario_id:
        raise ClientPackError(f"scenario: result scenario_id {result.get('scenario_id')!r} != "
                              f"requested {scenario_id!r}")
    if not result.get("summary") or not isinstance(result.get("price_path"), list):
        raise ClientPackError("scenario: result has no summary/price_path")
    forecast = documents["api_forecast"]
    if forecast.get("status") != STATUS:
        raise ClientPackError(f"api_forecast: status is {forecast.get('status')!r}, expected {STATUS!r}")
    if not isinstance(forecast.get("envelope"), dict):
        raise ClientPackError("api_forecast: envelope is missing")
    return documents


def _validate_product_feed(feed: dict) -> None:
    status = feed.get("status")
    if status not in ALLOWED_CANDIDATE_STATUSES:
        raise ClientPackError(
            f"product_feed: status is {status!r}; the client pack publishes only "
            f"{'/'.join(ALLOWED_CANDIDATE_STATUSES)} and never an admitted model")
    for key in ("candidate", "target", "horizon", "origin", "point"):
        if key not in feed:
            raise ClientPackError(f"product_feed: required field {key!r} is missing")
    quantiles = feed.get("quantiles_5_to_95_step_5")
    if not isinstance(quantiles, list) or len(quantiles) != 19:
        raise ClientPackError("product_feed: 19 quantiles are required")
    try:
        values = [float(value) for value in quantiles]
    except (TypeError, ValueError) as exc:
        raise ClientPackError(f"product_feed: non-numeric quantile: {exc!r}") from exc
    if any(right <= left for left, right in zip(values, values[1:])):
        raise ClientPackError("product_feed: quantiles are not strictly increasing")
    if not isinstance(feed.get("protocol"), dict) or not feed["protocol"].get("sha256"):
        raise ClientPackError("product_feed: protocol reference/digest is missing")
    if not isinstance(feed.get("c4_status"), dict) or not isinstance(feed.get("c6_status"), dict):
        raise ClientPackError("product_feed: C4/C6 status blocks are missing")


def _validate_registry(entries: list) -> None:
    for entry in entries:
        if entry.get("status") not in ALLOWED_MODULE_STATUSES:
            raise ClientPackError(
                f"registry: module {entry.get('module_id')!r} has status {entry.get('status')!r}; "
                f"only {ALLOWED_MODULE_STATUSES} passports are allowed")
        code = entry.get("code_sha256") or ""
        if not re.fullmatch(r"[0-9a-f]{64}", code):
            raise ClientPackError(f"registry: module {entry.get('module_id')!r} has no code digest")


def latest_passports(entries: list) -> list:
    """The append-only registry keeps history: take the latest record per module_id."""
    latest = {}
    for entry in entries:
        latest[entry["module_id"]] = entry
    return [latest[module_id] for module_id in sorted(latest)]


def _default_generated_at(documents: dict) -> str:
    candidates = []
    for document in (documents["product_feed"], documents["api_index"],
                     documents["scenario"], *documents["forecasts"].values()):
        value = document.get("generated_at")
        if value:
            _parse_timestamp(value, "input generated_at")
            candidates.append(value)
    for entry in documents["registry"]:
        created = entry.get("created_at")
        if created:
            candidates.append(f"{created}T00:00:00Z")
    bridge = documents.get("bridge") or {}
    if bridge.get("available"):
        value = (bridge.get("document") or {}).get("generated_at")
        if value:
            _parse_timestamp(value, "bridge generated_at")
            candidates.append(value)
    for value in candidates:
        _parse_timestamp(value, "input generated_at")
    return max(candidates) if candidates else FALLBACK_GENERATED_AT


# ------------------------------------------------------------------------------------- aggregate
def _forecast_item(target: str, spec: dict, document: dict) -> dict:
    """One published forecast contract, reduced to its aggregate envelope (no raw series)."""
    envelope = document.get("envelope") or {}
    return {
        "target": target,
        "file": spec.get("file"),
        "module_id": spec.get("module_id") or document.get("module_id"),
        "module_version": spec.get("module_version"),
        "commodity": spec.get("commodity"),
        "units": spec.get("units"),
        "status": document.get("status"),
        "no_point_forecast": document.get("no_point_forecast"),
        "result_digest": document.get("result_digest"),
        "assumptions_count": document.get("assumptions_count"),
        "price_peak": envelope.get("price_peak"),
        "price_mean": envelope.get("price_mean"),
        "price_trough": envelope.get("price_trough"),
        "price_peak_month": envelope.get("price_peak_month"),
        "range_peak": envelope.get("range_peak"),
        "interval": envelope.get("interval"),
        "checks": envelope.get("checks") or {},
    }


def _market_module_item(item: dict) -> dict:
    return {
        "module_id": item.get("module_id"),
        "module_version": item.get("module_version"),
        "commodity": item.get("commodity"),
        "default_scenario": item.get("default_scenario"),
        "forecast_target": item.get("forecast_target"),
        "status": item.get("status"),
        "units": item.get("units"),
        "scenarios": len(item.get("scenarios") or []),
    }


def _legacy_forecast_path(inputs: dict, forecast: dict) -> Path:
    target = str(forecast.get("target") or DEFAULT_FORECAST_TARGET)
    path = inputs["forecast_paths"].get(target)
    return path if path is not None else next(iter(inputs["forecast_paths"].values()))


def build_document(inputs: dict, client: str, generated_at: str) -> dict:
    feed = inputs["product_feed"]
    registry = latest_passports(inputs["registry"])
    scenario = inputs["scenario"]
    result = scenario["result"]
    index = inputs["api_index"]
    forecast = inputs["api_forecast"]
    envelope = forecast["envelope"]
    summary = result.get("summary") or {}
    quantiles = [float(value) for value in feed["quantiles_5_to_95_step_5"]]
    bridge = _bridge_section(inputs["bridge"], inputs["root"])
    blocks = inputs["blocks"]["section"]
    blocks_available = [entry for entry in blocks.get("blocks") or [] if entry.get("available")]
    by_status, by_kind = {}, {}
    for entry in registry:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    market_modules = list(index.get("market_modules") or [])
    forecast_items = [_forecast_item(target, inputs["forecast_specs"][target],
                                     inputs["forecasts"][target])
                      for target in sorted(inputs["forecasts"])]
    market_module_items = [_market_module_item(item) for item in market_modules]
    scenario_peak = summary.get("price_peak", envelope.get("price_peak"))
    scenario_mean = summary.get("price_mean", envelope.get("price_mean"))
    document = {
        "schema": SCHEMA,
        "kind": "client_pack",
        "status": STATUS,
        "client": client,
        "generated_at": generated_at,
        "disclaimer": DISCLAIMER,
        "admission": {
            "admitted": False,
            "candidate_status": feed.get("status"),
            "note": ("no model is admitted: the product feed publishes a "
                     f"{feed.get('status')} candidate; C4 pass={feed.get('c4_status', {}).get('pass')}, "
                     f"C6 status={feed.get('c6_status', {}).get('status')}"),
        },
        "summary": {
            "candidate": feed.get("candidate"),
            "candidate_status": feed.get("status"),
            "target": feed.get("target"),
            "horizon": feed.get("horizon"),
            "origin": feed.get("origin"),
            "point": feed.get("point"),
            "interval_5_95": [quantiles[0], quantiles[-1]],
            "c4_pass": bool(feed.get("c4_status", {}).get("pass")),
            "c6_status": feed.get("c6_status", {}).get("status"),
            "modules_total": len(registry),
            "modules_statuses": by_status,
            "scenario_id": result.get("scenario_id"),
            "scenario_peak": scenario_peak,
            "scenario_mean": scenario_mean,
            "api_version": index.get("api_version"),
            "api_modules": len(index.get("modules") or []),
            "api_endpoints": len(index.get("endpoints") or []),
            "market_modules": len(market_modules),
            "market_module_ids": [item.get("module_id") for item in market_modules],
            "forecasts": len(forecast_items),
            "forecast_targets": [item["target"] for item in forecast_items],
            "bridge_available": bridge["available"],
            "bridge_source": bridge.get("source"),
            "bridge_sha256": bridge.get("sha256"),
            "bridge_scenarios": len(bridge.get("scenarios") or []),
            "bridge_effects": len(bridge.get("effects") or []),
            "blocks_included": bool(blocks.get("included")),
            "blocks_available": bool(blocks.get("available")),
            "blocks_status": blocks.get("status"),
            "blocks_count": len(blocks_available),
            "blocks_scenarios": sum(len(entry.get("scenarios") or [])
                                    for entry in blocks_available),
            "blocks_sha256": {entry["id"]: entry.get("sha256")
                              for entry in blocks_available},
            "sources": (len(inputs["paths"]) + len(inputs["forecasts"])
                        + (1 if bridge["available"] else 0)
                        + len(inputs["blocks"]["artifacts"])),
        },
        "product_feed": {
            "source": _repo_path(inputs["root"], inputs["paths"]["product_feed"]),
            "schema_version": feed.get("schema_version"),
            "generated_at": feed.get("generated_at"),
            "status": feed.get("status"),
            "status_label": feed.get("status_label"),
            "candidate": feed.get("candidate"),
            "target": feed.get("target"),
            "horizon": feed.get("horizon"),
            "origin": feed.get("origin"),
            "point": feed.get("point"),
            "quantiles_5_to_95_step_5": quantiles,
            "controls": feed.get("controls") or {},
            "c4_status": feed.get("c4_status") or {},
            "c6_status": feed.get("c6_status") or {},
            "data_class": feed.get("data_class"),
            "conditional_data_class": feed.get("conditional_data_class"),
            "protocol": feed.get("protocol") or {},
            "report_id": feed.get("report_id"),
            "source_hashes": feed.get("source_hashes") or {},
            "limitations": feed.get("limitations") or [],
            "no_admission": True,
        },
        "candidates": {
            "source": "product feed (forecast_latest.json)",
            "no_admission": True,
            "items": [{
                "candidate": feed.get("candidate"),
                "status": feed.get("status"),
                "status_label": feed.get("status_label"),
                "target": feed.get("target"),
                "horizon": feed.get("horizon"),
                "origin": feed.get("origin"),
                "point": feed.get("point"),
                "data_class": feed.get("data_class"),
            }],
            "registered_candidates_note": ("the pack publishes only the candidate of the product "
                                           "feed; it does not invent or admit candidates"),
        },
        "modules": {
            "source": _repo_path(inputs["root"], inputs["paths"]["registry"]),
            "registry_sha256": _sha256(inputs["paths"]["registry"]),
            "count": len(registry),
            "by_status": by_status,
            "by_kind": by_kind,
            "items": [{field: entry.get(field) for field in MODULE_FIELDS} for entry in registry],
        },
        "market_modules": {
            "source": _repo_path(inputs["root"], inputs["paths"]["api_index"]),
            "count": len(market_module_items),
            "items": market_module_items,
            "note": ("market modules are read from api/index.json (market_modules[]); the count is "
                     "never hard-coded, so a 1-module v1 index and a 2-module v2 index both build"),
        },
        "forecasts": {
            "source": _repo_path(inputs["root"], inputs["paths"]["api_index"]),
            "count": len(forecast_items),
            "items": forecast_items,
            "note": ("every forecast contract listed by api/index.json is included; the gas "
                     "contract (gas_ttf) is a v2 addition and does not replace the oil contract"),
        },
        "market_scenario": {
            "source": _repo_path(inputs["root"], inputs["paths"]["scenario"]),
            "scenario_id": result.get("scenario_id"),
            "module_id": result.get("module_id"),
            "module_version": (scenario.get("module_ref") or {}).get("version"),
            "status": scenario.get("status"),
            "commodity": result.get("commodity"),
            "units": result.get("units"),
            "horizon_months": result.get("horizon_months"),
            "scenario": scenario.get("scenario") or {},
            "summary": summary,
            "price_path": [float(value) for value in result.get("price_path") or []],
            "sensitivity": result.get("sensitivity") or [],
            "assumptions_count": len(result.get("assumptions") or []),
            "checks": result.get("checks") or {},
            "result_digest": result.get("digest"),
            "generated_at": scenario.get("generated_at"),
        },
        "forecast_api": {
            "source": _repo_path(inputs["root"], inputs["paths"]["api_index"]),
            "schema": index.get("schema"),
            "api_version": index.get("api_version"),
            "base_path": index.get("base_path"),
            "status": index.get("status"),
            "counts": index.get("counts") or {},
            "endpoints": [{"method": item.get("method"), "path": item.get("path"),
                           "description": item.get("description")}
                          for item in index.get("endpoints") or []],
            "modules": [{"module_id": item.get("module_id"),
                         "version": item.get("version"),
                         "status": item.get("status"),
                         "kind": item.get("kind")}
                        for item in index.get("modules") or []],
            "scenarios": [{"id": item.get("id"), "module": item.get("module"),
                           "horizon_months": item.get("horizon_months")}
                          for item in index.get("scenarios") or []],
            "market_modules": market_module_items,
            "forecasts": forecast_items,
            "forecast": {
                "source": _repo_path(inputs["root"], _legacy_forecast_path(inputs, forecast)),
                "target": forecast.get("target"),
                "module_id": forecast.get("module_id"),
                "status": forecast.get("status"),
                "no_point_forecast": forecast.get("no_point_forecast"),
                "result_digest": forecast.get("result_digest"),
                "assumptions_count": forecast.get("assumptions_count"),
                "price_peak": envelope.get("price_peak"),
                "price_mean": envelope.get("price_mean"),
                "price_trough": envelope.get("price_trough"),
                "price_peak_month": envelope.get("price_peak_month"),
            },
            "generated_at": index.get("generated_at"),
        },
        "bridge": bridge,
        "blocks": blocks,
        "licence": dict(LICENCE),
        "sources": [],
    }
    source_paths = [(key, inputs["paths"][key])
                    for key in ("product_feed", "registry", "api_index", "scenario")]
    source_paths += [(f"forecast:{target}", inputs["forecast_paths"][target])
                     for target in sorted(inputs["forecast_paths"])]
    if bridge["available"]:
        source_paths.append(("bridge", inputs["bridge"]["path"]))
    for artifact in inputs["blocks"]["artifacts"]:
        source_paths.append((f"block:{artifact['id']}", artifact["path"]))
    for kind, path in source_paths:
        document["sources"].append({"kind": kind, "path": _repo_path(inputs["root"], path),
                                    "bytes": path.stat().st_size, "sha256": _sha256(path)})
    source_meta = {
        "product_feed": (feed.get("schema_version"), feed.get("generated_at")),
        "registry": ("module_registry.jsonl", None),
        "api_index": (index.get("schema"), index.get("generated_at")),
        "scenario": (scenario.get("schema"), scenario.get("generated_at")),
    }
    for target, item in inputs["forecasts"].items():
        source_meta[f"forecast:{target}"] = (item.get("schema"), item.get("generated_at"))
    if bridge["available"]:
        source_meta["bridge"] = (bridge.get("schema"), bridge.get("generated_at"))
    for entry in inputs["blocks"]["section"].get("blocks") or []:
        if entry.get("available"):
            source_meta[f"block:{entry['id']}"] = (entry.get("schema"), None)
    for item in document["sources"]:
        schema, generated = source_meta[item["kind"]]
        item["schema"] = schema
        item["generated_at"] = generated
    document["document_digest"] = _canonical_sha256(document)
    return document


# ------------------------------------------------------------------------------------------ DOCX
def _docx():
    try:
        import docx  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency-style guard
        raise ClientPackError("python-docx is required to build the client pack") from exc
    return docx


def _new_document(title: str, client: str, generated_at: str):
    docx = _docx()
    document = docx.Document()
    document.add_heading(title, level=0)
    document.add_paragraph(f"Клиент: {client} · сформирован: {generated_at} · "
                           f"статус: {STATUS} (research only)")
    notice = document.add_paragraph(DISCLAIMER)
    notice.style = document.styles["Intense Quote"]
    return document


def _table(document, headers: list, rows: list) -> None:
    if not rows:
        document.add_paragraph("—")
        return
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = str(header)
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = "" if value is None else str(value)


def _bridge_docx(document, bridge: dict) -> None:
    """Optional section: scenario → country/industry top effects, or an explicit absence note."""
    document.add_heading("Мост «прогноз → мир» (reduced-form, опционально)", level=1)
    if not bridge.get("available"):
        document.add_paragraph("Раздел недоступен: " + str(bridge.get("reason")))
        document.add_paragraph(
            "Это не ошибка сборки: блок опциональный, пакет собран без него. Чтобы включить "
            "раздел, запустите tools/forecast_world_bridge.py или укажите --bridge.")
        return
    dataset = bridge.get("dataset") or {}
    calibration = bridge.get("calibration") or {}
    document.add_paragraph(
        f"Источник: {bridge['source']} · SHA-256 {bridge['sha256']} · набор "
        f"{dataset.get('id')} ({dataset.get('label')}, {dataset.get('units')}) · "
        f"S_price {_fmt(calibration.get('price_sensitivity'), 4)} · M "
        f"{_fmt(calibration.get('growth_multiplier'), 4)} · pass-through "
        f"{_fmt(calibration.get('pass_through'), 2)} · статус артефакта "
        f"{bridge.get('artefact_status')}.")
    _table(document, ["Сценарий", "Статус", "Шаг CPI, пп", "Δ выпуска, USD млн",
                      "% выпуска", "Интервал 5–95, USD млн"],
           [[item["id"], item["status"], _fmt(item["shifts_pp"]["cpi"], 2),
             _fmt(item["delta_output_usd_mn"], 1), _fmt(item["relative_output_pct"], 4),
             f"{_fmt(item['interval_usd_mn'][0], 1)} … {_fmt(item['interval_usd_mn'][1], 1)}"]
            for item in bridge["scenarios"]])
    rows = []
    for item in bridge["scenarios"]:
        for cell in item["top_effects"][:BRIDGE_DOC_TOP]:
            rows.append([item["id"], cell["country"], cell["industry"],
                         _fmt(cell["delta_output_usd_mn"], 1), _fmt(cell["share_of_abs"], 4)])
    document.add_paragraph("Топ-эффекты «сценарий → страна/отрасль» "
                           f"(до {BRIDGE_DOC_TOP} ячеек на сценарий, полный список — в XLSX/JSON):")
    _table(document, ["Сценарий", "Страна", "Отрасль", "Δ выпуска, USD млн", "Доля"], rows)
    document.add_paragraph(bridge["note"])


def _blocks_docx(document, blocks: dict) -> None:
    """Optional section: blocks 15/17 compact scenario tables, or an explicit absence note."""
    document.add_heading("Блоки 15/17: институты и технологии (сценарные модификаторы)", level=1)
    if not blocks.get("included"):
        document.add_paragraph("Раздел не включён: " + str(blocks.get("reason")))
        return
    document.add_paragraph(blocks.get("note"))
    for entry in blocks.get("blocks") or []:
        document.add_heading(str(entry.get("title")), level=2)
        if not entry.get("available"):
            document.add_paragraph("Раздел недоступен: " + str(entry.get("reason")))
            document.add_paragraph(
                "Это не ошибка сборки: блок опциональный. Чтобы включить раздел, соберите "
                f"артефакт ({entry.get('schema')}) и передайте --include-blocks.")
            continue
        document.add_paragraph(
            f"Источник: {entry.get('source')} · SHA-256 {entry.get('sha256')} · статус "
            f"{entry.get('status')} · допущений {entry.get('assumptions_count')} · горизонт "
            f"{entry.get('horizon_years')} лет.")
        if entry.get("block") == 15:
            document.add_paragraph(
                f"Профиль {entry.get('profile')} ({entry.get('profile_source')}): композит "
                f"{_fmt(entry.get('score'), 2)}, разброс {_fmt(entry.get('dispersion'), 2)}. "
                "Профили EXAMPLE-* иллюстративны, страновых значений в блоке нет.")
            _table(document, ["Сценарий", "Тип", "Δ композита, п.", "Δ премии, б.п.",
                              "Δ инвестиций, п.п.", "Δ роста, п.п."],
                   [[item["id"], item.get("kind"), _fmt(item.get("delta_score_points"), 2),
                     _fmt(item.get("delta_risk_premium_bps"), 1),
                     _fmt(item.get("delta_investment_pp"), 3),
                     _fmt(item.get("delta_growth_pp"), 3)]
                    for item in entry.get("scenarios") or []])
        elif entry.get("block") == 17:
            _table(document, ["Сценарий", "Внедрение", "TFP, %", "Выпуск, %", "Энергия, %",
                              "Энергоёмкость, %"],
                   [[item["id"], _fmt(item.get("adoption"), 3),
                     _fmt(item.get("tfp_gain_pct"), 2), _fmt(item.get("output_gain_pct"), 2),
                     _fmt(item.get("energy_change_pct"), 2),
                     _fmt(item.get("energy_intensity_change_pct"), 2)]
                    for item in entry.get("scenarios") or []])
            document.add_paragraph(
                "Пять агрегированных отраслей: " + ", ".join(entry.get("sectors") or []) +
                ". Это сценарная арифметика, а не оценка эффекта ИИ.")
        limitations = entry.get("limitations") or []
        if limitations:
            document.add_paragraph("Ограничения: " + "; ".join(str(item) for item in limitations))


def build_executive(path: Path, data: dict, generated: datetime) -> None:
    document = _new_document("AMFE — клиентский пакет v2 (executive summary)", data["client"],
                             data["generated_at"])
    summary = data["summary"]
    document.add_heading("Ключевые числа (только из артефактов)", level=1)
    _table(document, ["Показатель", "Значение"], [
        ["Кандидат", f"{summary['candidate']} · {summary['candidate_status']}"],
        ["Цель / горизонт / начало", f"{summary['target']} / h={summary['horizon']} / "
                                     f"{summary['origin']}"],
        ["Точечный прогноз (из feed)", _fmt(summary["point"], 4)],
        ["Интервал 5–95% (из 19 квантилей)", f"{_fmt(summary['interval_5_95'][0], 4)} – "
                                             f"{_fmt(summary['interval_5_95'][1], 4)}"],
        ["C4 (рецессионные начала)", "PASS" if summary["c4_pass"] else "FAIL / открыт"],
        ["C6 (forward-журнал)", summary["c6_status"]],
        ["Модульные паспорта", f"{summary['modules_total']} · {STATUS}"],
        ["Рыночные модули / forecast-контракты",
         f"{summary['market_modules']} / {summary['forecasts']} "
         f"({', '.join(summary['forecast_targets'])})"],
        ["Рыночный сценарий", f"{summary['scenario_id']} · пик {_fmt(summary['scenario_peak'])}"],
        ["Forecast API", f"версия {summary['api_version']} · модулей {summary['api_modules']} · "
                         f"эндпоинтов {summary['api_endpoints']}"],
        ["Источников в пакете", summary["sources"]],
    ])
    document.add_heading("Что означает статус", level=1)
    document.add_paragraph(
        "Ни одна модель не допущена: продукт публикует кандидата в статусе CANDIDATE, C4 и C6 "
        "остаются открытыми (см. docs/076, docs/088). Числа пакета — это значения из артефактов "
        "product feed и forecast API; пакет ничего не пересчитывает. Это не прогноз и не "
        "инвестиционная рекомендация.")
    _bridge_docx(document, data["bridge"])
    _blocks_docx(document, data["blocks"])
    document.add_heading("Что внутри", level=1)
    document.add_paragraph(
        "02_Numbers.xlsx — числа по кандидату, паспортам модулей, рыночному сценарию и API "
        "(листы Bridge/Bridge_Scenarios/Bridge_Effects при наличии моста); "
        "03_Dashboard.html — статическая панель; 04_Machine_Readable.json — машиночитаемая версия; "
        "05_Methodology_and_Sources.docx — источники и метод; "
        "06_Licence_and_Disclaimer.docx — лицензии и дисклеймеры; "
        "07_Reproducibility.json — манифест воспроизводимости; README.md — краткое описание.")
    document.save(str(path))


def build_methodology(path: Path, data: dict, generated: datetime) -> None:
    document = _new_document("AMFE — методология и источники", data["client"], data["generated_at"])
    document.add_heading("Источники чисел", level=1)
    _table(document, ["Артефакт", "Схема / generated_at", "SHA-256 (12)"],
           [[item["path"], f"{item.get('schema') or '—'} / {item.get('generated_at') or '—'}",
             _short(item["sha256"])] for item in data["sources"]])
    document.add_heading("Как строится пакет", level=1)
    for line in (
            "1. Инструмент читает product feed, реестр модульных паспортов, индекс forecast API, "
            "рыночный сценарий и каждый forecast-контракт, который перечислен в индексе "
            "(включая газовый gas_ttf).",
            "2. Числа копируются из артефактов; собственные вычисления — только счётчики, длины и "
            "границы квантилей. Количество паспортов, рыночных модулей и контрактов выводится из "
            "реестра и api/index.json, а не задано константой.",
            "3. Никаких сырых значений третьих сторон пакет не содержит: в репозиторий идут "
            "производные статистики и собственные прогнозы.",
            "4. Сборка fail-closed: отсутствующий/битый артефакт или недопустимый статус "
            "(например, admitted model) останавливают сборку до записи выходного каталога.",
    ):
        document.add_paragraph(line)
    bridge = data["bridge"]
    if bridge.get("available"):
        document.add_paragraph(
            "Опциональный блок «Мост прогноз → мир» прочитан из "
            f"{bridge['source']} (SHA-256 {bridge['sha256']}, статус артефакта "
            f"{bridge.get('artefact_status')}). Это reduced-form сравнительная статика по "
            "объявленным эластичностям; сырых рядов в артефакте нет (checks.no_raw_data=true).")
    else:
        document.add_paragraph(
            "Опциональный блок «Мост прогноз → мир» не включён: "
            f"{bridge.get('reason')}. Отсутствие необязательного артефакта не является ошибкой.")
    document.add_heading("Детерминизм", level=1)
    document.add_paragraph(
        "Дата пакета фиксируется --generated-at (по умолчанию — самый свежий timestamp входов). "
        "Zip-входы DOCX/XLSX и даты docProps/core.xml прибиваются к этой дате (тот же подход, что в "
        "market_client_pack.py), поэтому две сборки на тех же входах байт-идентичны.")
    document.save(str(path))


def build_licence(path: Path, data: dict, generated: datetime) -> None:
    document = _new_document("AMFE — лицензии и дисклеймер", data["client"], data["generated_at"])
    document.add_heading("Статус", level=1)
    document.add_paragraph(f"Пакет: {STATUS}. Допуск модели не присвоен и не подразумевается.")
    document.add_heading("Ограничения", level=1)
    for line in (
            "Ни одна модель не admitted: кандидат остаётся CANDIDATE/RESEARCH_ONLY; C4 открыт, "
            "C6 недоступен до накопления forward-журнала.",
            "Сценарий — сценарная арифметика по объявленным допущениям, а не точечный прогноз и "
            "не доверительный интервал.",
            "Это не инвестиционная рекомендация и не юридическая консультация.",
            "Сырые наблюдения третьих сторон не публикуются: только производные статистики и "
            "собственные прогнозы проекта (см. protocols/licence_register.json, docs/84, docs/90).",
            "Использование — исследовательское; условия источников — предмет лицензионного "
            "реестра проекта.",
    ):
        document.add_paragraph("• " + line)
    document.add_heading("Документы", level=1)
    for item in data["licence"].get("documents", []):
        document.add_paragraph(item)
    document.save(str(path))


# ----------------------------------------------------------------------------------------- XLSX
def build_xlsx(path: Path, data: dict, generated: datetime) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover
        raise ClientPackError("openpyxl is required to build the client pack") from exc
    workbook = Workbook()
    summary = data["summary"]
    sheet = workbook.active
    sheet.title = "Summary"
    rows = [
        ("Клиент", data["client"]), ("Сформирован", data["generated_at"]),
        ("Статус", data["status"]), ("Кандидат", summary["candidate"]),
        ("Статус кандидата", summary["candidate_status"]), ("Цель", summary["target"]),
        ("Горизонт", summary["horizon"]), ("Начало", summary["origin"]),
        ("Точечный прогноз", summary["point"]),
        ("Квантиль 5%", summary["interval_5_95"][0]),
        ("Квантиль 95%", summary["interval_5_95"][1]),
        ("C4 pass", summary["c4_pass"]), ("C6 статус", summary["c6_status"]),
        ("Модулей", summary["modules_total"]),
        ("Рыночных модулей", summary["market_modules"]),
        ("Forecast-контрактов", summary["forecasts"]),
        ("Forecast targets", ", ".join(summary["forecast_targets"])),
        ("Сценарий", summary["scenario_id"]),
        ("Пик сценария", summary["scenario_peak"]), ("Среднее сценария", summary["scenario_mean"]),
        ("API версия", summary["api_version"]), ("API модулей", summary["api_modules"]),
        ("API эндпоинтов", summary["api_endpoints"]),
        ("Digest документа", data["document_digest"]),
    ]
    for row_index, (label, value) in enumerate(rows, start=1):
        sheet.cell(row=row_index, column=1, value=label)
        sheet.cell(row=row_index, column=2, value=value)

    feed = data["product_feed"]
    candidates = workbook.create_sheet("Candidate")
    for row_index, header in enumerate(("Поле", "Значение"), start=1):
        candidates.cell(row=1, column=row_index, value=header)
    feed_rows = [("candidate", feed.get("candidate")), ("status", feed.get("status")),
                 ("target", feed.get("target")), ("horizon", feed.get("horizon")),
                 ("origin", feed.get("origin")), ("point", feed.get("point")),
                 ("data_class", feed.get("data_class")),
                 ("C4 pass", (feed.get("c4_status") or {}).get("pass")),
                 ("C6 status", (feed.get("c6_status") or {}).get("status")),
                 ("protocol version", (feed.get("protocol") or {}).get("version")),
                 ("feed digest", (feed.get("source_hashes") or {}).get("report"))]
    for row_index, (label, value) in enumerate(feed_rows, start=2):
        candidates.cell(row=row_index, column=1, value=label)
        candidates.cell(row=row_index, column=2, value=value)

    modules = workbook.create_sheet("Modules")
    headers = ("module_id", "version", "kind", "status", "owner", "code_sha256",
               "data_status", "created_at")
    for column, header in enumerate(headers, start=1):
        modules.cell(row=1, column=column, value=header)
    for row_index, entry in enumerate(data["modules"]["items"], start=2):
        for column, header in enumerate(headers, start=1):
            modules.cell(row=row_index, column=column, value=entry.get(header))

    market_modules = workbook.create_sheet("Market_Modules")
    market_headers = ("module_id", "module_version", "commodity", "default_scenario",
                      "forecast_target", "status", "units", "scenarios")
    for column, header in enumerate(market_headers, start=1):
        market_modules.cell(row=1, column=column, value=header)
    for row_index, entry in enumerate(data["market_modules"]["items"], start=2):
        for column, header in enumerate(market_headers, start=1):
            market_modules.cell(row=row_index, column=column, value=entry.get(header))

    forecasts = workbook.create_sheet("Forecasts")
    forecast_headers = ("target", "module_id", "module_version", "status", "units",
                        "no_point_forecast", "price_peak", "price_mean", "price_trough",
                        "result_digest")
    for column, header in enumerate(forecast_headers, start=1):
        forecasts.cell(row=1, column=column, value=header)
    for row_index, entry in enumerate(data["forecasts"]["items"], start=2):
        for column, header in enumerate(forecast_headers, start=1):
            forecasts.cell(row=row_index, column=column, value=entry.get(header))

    scenario = data["market_scenario"]
    scenario_sheet = workbook.create_sheet("Scenario_Summary")
    scenario_rows = [("scenario_id", scenario["scenario_id"]),
                     ("module_id", scenario["module_id"]),
                     ("module_version", scenario["module_version"]),
                     ("commodity", scenario["commodity"]), ("units", scenario["units"]),
                     ("horizon_months", scenario["horizon_months"]),
                     ("assumptions", scenario["assumptions_count"]),
                     ("result_digest", scenario["result_digest"])]
    for row_index, (label, value) in enumerate(scenario_rows, start=1):
        scenario_sheet.cell(row=row_index, column=1, value=label)
        scenario_sheet.cell(row=row_index, column=2, value=value)
    path_sheet = workbook.create_sheet("Scenario_Path")
    path_sheet.cell(row=1, column=1, value="month")
    path_sheet.cell(row=1, column=2, value=f"price, {scenario['units']}")
    for row_index, price in enumerate(scenario["price_path"], start=2):
        path_sheet.cell(row=row_index, column=1, value=row_index - 2)
        path_sheet.cell(row=row_index, column=2, value=float(price))
    sensitivity_sheet = workbook.create_sheet("Scenario_Sensitivity")
    variants = scenario["sensitivity"]
    keys = list(variants[0]) if variants else []
    for column, key in enumerate(["variant", *keys], start=1):
        sensitivity_sheet.cell(row=1, column=column, value=key)
    for row_index, item in enumerate(variants, start=2):
        sensitivity_sheet.cell(row=row_index, column=1, value=item.get("variant"))
        for column, key in enumerate(keys, start=2):
            value = item.get(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            sensitivity_sheet.cell(row=row_index, column=column, value=value)

    api = workbook.create_sheet("API")
    api_rows = [("schema", data["forecast_api"]["schema"]),
                ("api_version", data["forecast_api"]["api_version"]),
                ("base_path", data["forecast_api"]["base_path"]),
                ("modules", data["forecast_api"]["counts"].get("modules")),
                ("market modules", len(data["forecast_api"]["market_modules"])),
                ("forecast contracts", len(data["forecast_api"]["forecasts"])),
                ("forecast targets", ", ".join(item["target"]
                                               for item in data["forecast_api"]["forecasts"])),
                ("endpoints", len(data["forecast_api"]["endpoints"])),
                ("forecast target", data["forecast_api"]["forecast"]["target"]),
                ("forecast peak", data["forecast_api"]["forecast"]["price_peak"]),
                ("forecast mean", data["forecast_api"]["forecast"]["price_mean"]),
                ("forecast result_digest", data["forecast_api"]["forecast"]["result_digest"])]
    for row_index, (label, value) in enumerate(api_rows, start=1):
        api.cell(row=row_index, column=1, value=label)
        api.cell(row=row_index, column=2, value=value)

    bridge = data["bridge"]
    bridge_sheet = workbook.create_sheet("Bridge")
    bridge_rows = [
        ("available", bridge["available"]),
        ("status", bridge["status"]),
        ("reason", bridge.get("reason")),
        ("source", bridge.get("source")),
        ("sha256", bridge.get("sha256")),
        ("sha256_verified", bridge.get("sha256_verified")),
        ("artefact_status", bridge.get("artefact_status")),
        ("dataset", (bridge.get("dataset") or {}).get("id")),
        ("dataset_label", (bridge.get("dataset") or {}).get("label")),
        ("units", (bridge.get("dataset") or {}).get("units")),
        ("price_sensitivity", (bridge.get("calibration") or {}).get("price_sensitivity")),
        ("growth_multiplier", (bridge.get("calibration") or {}).get("growth_multiplier")),
        ("pass_through", (bridge.get("calibration") or {}).get("pass_through")),
        ("scenarios", len(bridge.get("scenarios") or [])),
        ("effects", len(bridge.get("effects") or [])),
        ("note", bridge.get("note")),
    ]
    for row_index, (label, value) in enumerate(bridge_rows, start=1):
        bridge_sheet.cell(row=row_index, column=1, value=label)
        bridge_sheet.cell(row=row_index, column=2, value=value)

    bridge_scenarios = workbook.create_sheet("Bridge_Scenarios")
    bridge_scenario_headers = ("scenario_id", "status", "cpi_shift_pp", "growth_shift_pp",
                               "price_step_pct", "delta_output_usd_mn", "relative_output_pct",
                               "interval_low_usd_mn", "interval_high_usd_mn", "reason")
    for column, header in enumerate(bridge_scenario_headers, start=1):
        bridge_scenarios.cell(row=1, column=column, value=header)
    for row_index, item in enumerate(bridge.get("scenarios") or [], start=2):
        interval = item.get("interval_usd_mn") or [None, None]
        values = (item.get("id"), item.get("status"),
                  (item.get("shifts_pp") or {}).get("cpi"),
                  (item.get("shifts_pp") or {}).get("growth"),
                  item.get("price_step_pct"), item.get("delta_output_usd_mn"),
                  item.get("relative_output_pct"), interval[0], interval[1],
                  item.get("reason"))
        for column, value in enumerate(values, start=1):
            bridge_scenarios.cell(row=row_index, column=column, value=value)

    bridge_effects = workbook.create_sheet("Bridge_Effects")
    bridge_effect_headers = ("scenario", "country", "industry", "delta_output_usd_mn",
                             "share_of_abs")
    for column, header in enumerate(bridge_effect_headers, start=1):
        bridge_effects.cell(row=1, column=column, value=header)
    for row_index, item in enumerate(bridge.get("effects") or [], start=2):
        for column, key in enumerate(bridge_effect_headers, start=1):
            bridge_effects.cell(row=row_index, column=column, value=item.get(key))

    blocks = data["blocks"]
    blocks_sheet = workbook.create_sheet("Blocks")
    block_headers = ("block", "block_id", "module_id", "available", "status", "source", "sha256",
                     "horizon_years", "profile", "scenarios", "assumptions", "note")
    for column, header in enumerate(block_headers, start=1):
        blocks_sheet.cell(row=1, column=column, value=header)
    for row_index, entry in enumerate(blocks.get("blocks") or [], start=2):
        values = (entry.get("block"), entry.get("id"), entry.get("module_id"),
                  entry.get("available"), entry.get("status"), entry.get("source"),
                  entry.get("sha256"), entry.get("horizon_years"), entry.get("profile"),
                  len(entry.get("scenarios") or []), entry.get("assumptions_count"),
                  entry.get("reason") if not entry.get("available") else blocks.get("note"))
        for column, value in enumerate(values, start=1):
            blocks_sheet.cell(row=row_index, column=column, value=value)
    blocks_scenarios = workbook.create_sheet("Blocks_Scenarios")
    block_scenario_headers = ("block", "module_id", "scenario_id", "title_ru", "horizon_years",
                              "delta_score_points", "delta_risk_premium_bps",
                              "delta_investment_pp", "delta_growth_pp", "adoption",
                              "tfp_gain_pct", "output_gain_pct", "energy_change_pct",
                              "energy_intensity_change_pct")
    for column, header in enumerate(block_scenario_headers, start=1):
        blocks_scenarios.cell(row=1, column=column, value=header)
    row_index = 2
    for entry in blocks.get("blocks") or []:
        if not entry.get("available"):
            continue
        for item in entry.get("scenarios") or []:
            values = (entry.get("block"), entry.get("module_id"), item.get("id"),
                      item.get("title_ru"), entry.get("horizon_years"),
                      item.get("delta_score_points"), item.get("delta_risk_premium_bps"),
                      item.get("delta_investment_pp"), item.get("delta_growth_pp"),
                      item.get("adoption"), item.get("tfp_gain_pct"),
                      item.get("output_gain_pct"), item.get("energy_change_pct"),
                      item.get("energy_intensity_change_pct"))
            for column, value in enumerate(values, start=1):
                blocks_scenarios.cell(row=row_index, column=column, value=value)
            row_index += 1

    sources = workbook.create_sheet("Sources")
    for column, header in enumerate(("kind", "path", "schema", "generated_at", "bytes", "sha256"),
                                    start=1):
        sources.cell(row=1, column=column, value=header)
    for row_index, item in enumerate(data["sources"], start=2):
        for column, key in enumerate(("kind", "path", "schema", "generated_at", "bytes", "sha256"),
                                     start=1):
            sources.cell(row=row_index, column=column, value=item.get(key))

    properties = workbook.properties
    properties.creator = "AMFE"
    properties.lastModifiedBy = "AMFE client pack v2"
    properties.created = generated.replace(tzinfo=None)
    properties.modified = generated.replace(tzinfo=None)
    workbook.save(str(path))


# ------------------------------------------------------------------------------------------ HTML
_HTML_STYLE = """
  body { font-family: system-ui, "Segoe UI", sans-serif; max-width: 64rem; margin: 0 auto;
         padding: 1.5rem; line-height: 1.45; }
  h1 { font-size: 1.5rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
  .meta { color: #666; font-size: 0.9rem; }
  .banner { border-left: 4px solid #b45309; background: #b4530915; padding: .7rem 1rem;
            margin: 1rem 0; }
  .cards { display: flex; gap: 1rem; flex-wrap: wrap; }
  .card { border: 1px solid #8884; border-radius: 8px; padding: .8rem 1rem; min-width: 12rem; }
  .card .v { font-size: 1.4rem; font-weight: 600; }
  table { border-collapse: collapse; width: 100%; margin: .6rem 0; }
  th, td { border: 1px solid #8884; padding: .35rem .5rem; text-align: left; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  footer { margin-top: 2rem; border-top: 1px solid #8884; padding-top: .7rem;
           color: #666; font-size: .85rem; }
"""


def _bridge_html(bridge: dict) -> str:
    """Optional bridge section for the static dashboard (escaped, no external resources)."""
    if not bridge.get("available"):
        return ("  <h2>Мост «прогноз → мир» (reduced-form)</h2>\n"
                f"  <div class=\"banner\">Раздел недоступен: "
                f"{_escape(str(bridge.get('reason')))}</div>")
    dataset = bridge.get("dataset") or {}
    calibration = bridge.get("calibration") or {}
    meta = (f"Источник: {bridge['source']} · SHA-256 {bridge['sha256']} · набор "
            f"{dataset.get('id')} ({dataset.get('label')}, {dataset.get('units')}) · S_price "
            f"{_fmt(calibration.get('price_sensitivity'), 4)} · M "
            f"{_fmt(calibration.get('growth_multiplier'), 4)}")
    scenario_rows = "".join(
        f"<tr><td>{_escape(str(item.get('id')))}</td><td>{_escape(str(item.get('status')))}</td>"
        f"<td class=\"num\">{_fmt(item.get('price_step_pct'), 2)}</td>"
        f"<td class=\"num\">{_fmt(item.get('delta_output_usd_mn'), 1)}</td>"
        f"<td class=\"num\">{_fmt(item.get('relative_output_pct'), 4)}</td>"
        f"<td class=\"num\">{_fmt(interval[0], 1)} … {_fmt(interval[1], 1)}</td></tr>"
        for item, interval in ((item, (item.get("interval_usd_mn") or [None, None])[:2])
                               for item in bridge.get("scenarios") or []))
    effect_rows = "".join(
        f"<tr><td>{_escape(str(item.get('id')))}</td>"
        f"<td>{_escape(cell['country'])}</td><td>{_escape(cell['industry'])}</td>"
        f"<td class=\"num\">{_fmt(cell.get('delta_output_usd_mn'), 1)}</td>"
        f"<td class=\"num\">{_fmt(cell.get('share_of_abs'), 4)}</td></tr>"
        for item in bridge.get("scenarios") or []
        for cell in (item.get("top_effects") or [])[:BRIDGE_DOC_TOP])
    return f"""  <h2>Мост «прогноз → мир» (reduced-form, опционально)</h2>
  <p class="meta">{_escape(meta)}</p>
  <table><tr><th>сценарий</th><th>статус</th><th>шаг CPI, пп</th>
  <th>Δ выпуска, USD млн</th><th>% выпуска</th><th>интервал 5–95, USD млн</th></tr>
  {scenario_rows}</table>
  <p class="meta">Топ-эффекты «сценарий → страна/отрасль» (до {BRIDGE_DOC_TOP} ячеек на
  сценарий; полный список — в 02_Numbers.xlsx и 04_Machine_Readable.json):</p>
  <table><tr><th>сценарий</th><th>страна</th><th>отрасль</th><th>Δ выпуска, USD млн</th>
  <th>доля</th></tr>{effect_rows}</table>
  <p class="meta">{_escape(bridge.get('note'))}</p>"""


def _blocks_html(blocks: dict) -> str:
    """Optional blocks 15/17 for the static dashboard (escaped, aggregate-only)."""
    if not blocks.get("included"):
        return ("  <h2>Блоки 15/17: институты и технологии</h2>\n"
                f"  <div class=\"banner\">Раздел не включён: "
                f"{_escape(str(blocks.get('reason')))}</div>")
    parts = ["  <h2>Блоки 15/17: институты и технологии (сценарные модификаторы)</h2>",
             f"  <p class=\"meta\">{_escape(str(blocks.get('note')))}</p>"]
    for entry in blocks.get("blocks") or []:
        parts.append(f"  <h3>{_escape(str(entry.get('title')))}</h3>")
        if not entry.get("available"):
            parts.append(f"  <div class=\"banner\">Раздел недоступен: "
                         f"{_escape(str(entry.get('reason')))}</div>")
            continue
        meta = (f"Источник: {entry.get('source')} · SHA-256 {entry.get('sha256')} · "
                f"статус {entry.get('status')} · допущений {entry.get('assumptions_count')} · "
                f"горизонт {entry.get('horizon_years')} лет")
        parts.append(f"  <p class=\"meta\">{_escape(meta)}</p>")
        if entry.get("block") == 15:
            rows = "".join(
                f"<tr><td>{_escape(str(item.get('id')))}</td>"
                f"<td>{_escape(str(item.get('kind')))}</td>"
                f"<td class=\"num\">{_fmt(item.get('delta_score_points'), 2)}</td>"
                f"<td class=\"num\">{_fmt(item.get('delta_risk_premium_bps'), 1)}</td>"
                f"<td class=\"num\">{_fmt(item.get('delta_investment_pp'), 3)}</td>"
                f"<td class=\"num\">{_fmt(item.get('delta_growth_pp'), 3)}</td></tr>"
                for item in entry.get("scenarios") or [])
            parts.append("  <table><tr><th>сценарий</th><th>тип</th><th>Δ композита, п.</th>"
                         "<th>Δ премии, б.п.</th><th>Δ инвестиций, п.п.</th>"
                         "<th>Δ роста, п.п.</th></tr>" + rows + "</table>")
            parts.append(f"  <p class=\"meta\">Профиль: {_escape(str(entry.get('profile')))}, "
                         f"композит {_fmt(entry.get('score'), 2)}, разброс "
                         f"{_fmt(entry.get('dispersion'), 2)}. Страновых значений в блоке нет: "
                         "профили EXAMPLE-* иллюстративны.</p>")
        elif entry.get("block") == 17:
            rows = "".join(
                f"<tr><td>{_escape(str(item.get('id')))}</td>"
                f"<td class=\"num\">{_fmt(item.get('adoption'), 3)}</td>"
                f"<td class=\"num\">{_fmt(item.get('tfp_gain_pct'), 2)}</td>"
                f"<td class=\"num\">{_fmt(item.get('output_gain_pct'), 2)}</td>"
                f"<td class=\"num\">{_fmt(item.get('energy_change_pct'), 2)}</td>"
                f"<td class=\"num\">{_fmt(item.get('energy_intensity_change_pct'), 2)}</td></tr>"
                for item in entry.get("scenarios") or [])
            parts.append("  <table><tr><th>сценарий</th><th>внедрение</th><th>TFP, %</th>"
                         "<th>выпуск, %</th><th>энергия, %</th><th>энергоёмкость, %</th></tr>"
                         + rows + "</table>")
            parts.append("  <p class=\"meta\">Пять агрегированных отраслей: "
                         f"{_escape(', '.join(entry.get('sectors') or []))}. Отраслевого моста в "
                         "MRIO нет.</p>")
        limitations = entry.get("limitations") or []
        if limitations:
            parts.append("  <ul>" + "".join(f"<li>{_escape(str(item))}</li>"
                                            for item in limitations) + "</ul>")
    parts.append("  <p class=\"meta\">Блоки 15/17 не входят в forecast-контракты, точечный прогноз "
                 "и не являются допуском (admission=false).</p>")
    return "\n".join(parts)


def build_dashboard(path: Path, data: dict) -> None:
    summary = data["summary"]
    module_rows = "".join(
        f"<tr><td>{item.get('module_id')}</td><td>{item.get('version')}</td>"
        f"<td>{item.get('kind')}</td><td>{item.get('status')}</td>"
        f"<td>{item.get('data_status') or '—'}</td></tr>"
        for item in data["modules"]["items"])
    endpoint_rows = "".join(
        f"<tr><td>{item.get('method')}</td><td>{item.get('path')}</td>"
        f"<td>{item.get('description')}</td></tr>"
        for item in data["forecast_api"]["endpoints"])
    market_rows = "".join(
        f"<tr><td>{item.get('module_id')}</td><td>{item.get('commodity')}</td>"
        f"<td>{item.get('default_scenario')}</td><td>{item.get('forecast_target')}</td>"
        f"<td>{item.get('status')}</td><td>{item.get('units')}</td></tr>"
        for item in data["market_modules"]["items"])
    forecast_rows = "".join(
        f"<tr><td>{item.get('target')}</td><td>{item.get('module_id')}</td>"
        f"<td>{item.get('units')}</td><td class=\"num\">{_fmt(item.get('price_peak'))}</td>"
        f"<td>{item.get('status')}</td><td>{item.get('result_digest') or '—'}</td></tr>"
        for item in data["forecasts"]["items"])
    path_rows = "".join(f"<tr><td>{index}</td><td class=\"num\">{_fmt(price)}</td></tr>"
                        for index, price in enumerate(data["market_scenario"]["price_path"]))
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMFE — client pack v2 (research only)</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
  <h1>AMFE — клиентский пакет v2</h1>
  <p class="meta">Клиент: {data['client']} · сформирован: {data['generated_at']} ·
     статус: {STATUS} · сценарий: {summary['scenario_id']}</p>
  <div class="banner">{DISCLAIMER}</div>
  <div class="cards">
    <div class="card"><div>Кандидат</div><div class="v">{summary['candidate']}</div>
      <div class="meta">{summary['candidate_status']} · C4: {'PASS' if summary['c4_pass'] else 'OPEN'}
      · C6: {summary['c6_status']}</div></div>
    <div class="card"><div>Точечный прогноз</div><div class="v">{_fmt(summary['point'], 4)}</div>
      <div class="meta">{summary['target']}, h={summary['horizon']}, {summary['origin']}</div></div>
    <div class="card"><div>Сценарий: пик</div>
      <div class="v">{_fmt(summary['scenario_peak'])}</div>
      <div class="meta">{summary['scenario_id']}</div></div>
    <div class="card"><div>Модулей</div><div class="v">{summary['modules_total']}</div>
      <div class="meta">{STATUS}</div></div>
    <div class="card"><div>Рыночных модулей</div><div class="v">{summary['market_modules']}</div>
      <div class="meta">forecast-контрактов {summary['forecasts']}</div></div>
    <div class="card"><div>Forecast API</div><div class="v">{summary['api_version']}</div>
      <div class="meta">модулей {summary['api_modules']} · эндпоинтов {summary['api_endpoints']}</div></div>
  </div>
  <h2>Модульные паспорта</h2>
  <table><tr><th>module_id</th><th>версия</th><th>kind</th><th>статус</th><th>data_status</th></tr>
  {module_rows}</table>
  <h2>Рыночные модули и forecast-контракты</h2>
  <table><tr><th>module_id</th><th>товар</th><th>сценарий по умолчанию</th>
  <th>forecast target</th><th>статус</th><th>единицы</th></tr>{market_rows}</table>
  <table><tr><th>target</th><th>module_id</th><th>единицы</th><th>пик</th><th>статус</th>
  <th>result_digest</th></tr>{forecast_rows}</table>
  <h2>Рыночный сценарий: путь цены</h2>
  <table><tr><th>месяц</th><th>цена</th></tr>{path_rows}</table>
  <h2>Forecast API</h2>
  <table><tr><th>метод</th><th>путь</th><th>описание</th></tr>{endpoint_rows}</table>
{_bridge_html(data['bridge'])}
{_blocks_html(data['blocks'])}
  <h2>Источники</h2>
  <ul>{''.join(f"<li>{item['path']} — sha256 {_short(item['sha256'])}</li>" for item in data['sources'])}</ul>
  <footer>Research only; not a forecast, not an admission decision, not investment advice.
  Собрано детерминированно из артефактов; сырые наблюдения третьих сторон не публикуются.</footer>
</body>
</html>
"""
    Path(path).write_text(html, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------------------- README
def build_readme(path: Path, data: dict, files: list) -> None:
    summary = data["summary"]
    lines = [
        "# AMFE — Client Pack v2 (research only)",
        "",
        f"Клиент: **{data['client']}** · сформирован: `{data['generated_at']}` · "
        f"статус: **{STATUS}**.",
        "",
        "> " + DISCLAIMER,
        "",
        "## Что внутри",
        "",
        "| Файл | Назначение |",
        "|---|---|",
        "| 01_Executive_Summary.docx | Executive summary с ключевыми числами из артефактов |",
        "| 02_Numbers.xlsx | Числа: кандидат, модульные паспорта, сценарий, forecast API, источники |",
        "| 03_Dashboard.html | Статическая панель (без скриптов и внешних ссылок) |",
        "| 04_Machine_Readable.json | Машиночитаемый пакет (числа + источники + лицензия) |",
        "| 05_Methodology_and_Sources.docx | Методология, источники и их SHA-256 |",
        "| 06_Licence_and_Disclaimer.docx | Лицензии и дисклеймеры |",
        "| 07_Reproducibility.json | Манифест воспроизводимости и команда сборки |",
        "| README.md | Этот файл |",
        "",
        "## Ключевые числа (из артефактов)",
        "",
        f"- Кандидат: `{summary['candidate']}` · статус **{summary['candidate_status']}** "
        f"(допуск не присвоен; C4 pass={summary['c4_pass']}, C6={summary['c6_status']}).",
        f"- Цель: `{summary['target']}`, h={summary['horizon']}, начало `{summary['origin']}`; "
        f"точечный прогноз {_fmt(summary['point'], 4)}, интервал 5–95% "
        f"{_fmt(summary['interval_5_95'][0], 4)}–{_fmt(summary['interval_5_95'][1], 4)}.",
        f"- Модульные паспорта: {summary['modules_total']} ({STATUS}); рыночный сценарий: "
        f"`{summary['scenario_id']}`, пик {_fmt(summary['scenario_peak'])}.",
        f"- Рыночные модули: {summary['market_modules']} "
        f"({', '.join(summary['market_module_ids'])}); forecast-контракты: "
        f"{summary['forecasts']} ({', '.join(summary['forecast_targets'])}); "
        f"счётчики выведены из реестра и api/index.json.",
        f"- Forecast API: {summary['api_version']}, модулей {summary['api_modules']}, "
        f"эндпоинтов {summary['api_endpoints']}.",
        "",
        "## Forecast-контракты",
        "",
        "| target | module_id | единицы | пик | статус |",
        "|---|---|---|---|---|",
    ]
    for item in data["forecasts"]["items"]:
        lines.append(f"| `{item['target']}` | `{item['module_id']}` | {item.get('units') or '—'} | "
                     f"{_fmt(item.get('price_peak'))} | {item.get('status')} |")
    bridge = data["bridge"]
    lines += ["", "## Мост «прогноз → мир» (опционально, reduced-form)", ""]
    if bridge.get("available"):
        dataset = bridge.get("dataset") or {}
        calibration = bridge.get("calibration") or {}
        lines += [
            f"- Источник: `{bridge['source']}` · SHA-256 `{bridge['sha256']}` · статус артефакта "
            f"`{bridge.get('artefact_status')}`.",
            f"- Набор: `{dataset.get('id')}` ({dataset.get('label')}, {dataset.get('units')}); "
            f"S_price {_fmt(calibration.get('price_sensitivity'), 4)}, M "
            f"{_fmt(calibration.get('growth_multiplier'), 4)}, pass-through "
            f"{_fmt(calibration.get('pass_through'), 2)}.",
            f"- Сценариев: {len(bridge['scenarios'])}; топ-эффектов «сценарий → страна/отрасль»: "
            f"{len(bridge['effects'])} (полный список — в `02_Numbers.xlsx` и "
            f"`04_Machine_Readable.json`).",
            "",
            "| сценарий | статус | шаг CPI, пп | Δ выпуска, USD млн | % выпуска |",
            "|---|---|---|---|---|",
        ]
        for item in bridge["scenarios"]:
            lines.append(f"| `{item['id']}` | {item['status']} | "
                         f"{_fmt(item['shifts_pp']['cpi'], 2)} | "
                         f"{_fmt(item['delta_output_usd_mn'], 1)} | "
                         f"{_fmt(item['relative_output_pct'], 4)} |")
        lines += ["", "| сценарий | страна | отрасль | Δ выпуска, USD млн | доля |",
                  "|---|---|---|---|---|"]
        for item in bridge["scenarios"]:
            for cell in item["top_effects"][:BRIDGE_DOC_TOP]:
                lines.append(f"| `{item['id']}` | {cell['country']} | {cell['industry']} | "
                             f"{_fmt(cell['delta_output_usd_mn'], 1)} | "
                             f"{_fmt(cell['share_of_abs'], 4)} |")
        lines += ["", f"> {bridge['note']}"]
    else:
        lines += [f"- Недоступен: {bridge.get('reason')}.",
                  "- Отсутствие необязательного артефакта не ошибка: пакет собран без раздела. "
                  "Чтобы включить, запустите `tools/forecast_world_bridge.py` или укажите "
                  "`--bridge <path>`."]
    blocks = data["blocks"]
    lines += ["", "## Блоки 15/17: институты и технологии (опционально)", ""]
    if not blocks.get("included"):
        lines += [f"- Раздел не включён: {blocks.get('reason')}."]
    else:
        lines += [
            f"- Статус: `{blocks.get('status')}`; доступно блоков: "
            f"{len([item for item in blocks.get('blocks') or [] if item.get('available')])} "
            f"из {len(blocks.get('blocks') or [])}.",
            "- Это сценарные модификаторы на объявленных допущениях, **не прогноз** и не "
            "forecast-контракты; `admission=false`, калибровки нет (`NOT_CALIBRATED`).",
        ]
        for entry in blocks.get("blocks") or []:
            if not entry.get("available"):
                lines += [f"- `{entry.get('title')}` — недоступен: {entry.get('reason')}."]
                continue
            lines += [f"- `{entry.get('title')}`: источник `{entry.get('source')}`, SHA-256 "
                      f"`{entry.get('sha256')}`, горизонт {entry.get('horizon_years')} лет, "
                      f"допущений {entry.get('assumptions_count')}."]
        lines += ["", "| блок | сценарий | числа |", "|---|---|---|"]
        for entry in blocks.get("blocks") or []:
            if not entry.get("available"):
                continue
            for item in entry.get("scenarios") or []:
                if entry.get("block") == 15:
                    numbers = (f"Δ композита {_fmt(item.get('delta_score_points'), 2)} п., "
                               f"Δ премии {_fmt(item.get('delta_risk_premium_bps'), 1)} б.п., "
                               f"Δ инвестиций {_fmt(item.get('delta_investment_pp'), 3)} п.п.")
                else:
                    numbers = (f"внедрение {_fmt(item.get('adoption'), 3)}, "
                               f"выпуск {_fmt(item.get('output_gain_pct'), 2)} %, "
                               f"энергоёмкость "
                               f"{_fmt(item.get('energy_intensity_change_pct'), 2)} %")
                lines.append(f"| {entry.get('block')} | `{item.get('id')}` | {numbers} |")
    lines += ["", "## Источники", ""]
    for item in data["sources"]:
        lines.append(f"- `{item['path']}` — `{_short(item['sha256'], 16)}` "
                     f"({item.get('schema') or '—'}, generated_at "
                     f"{item.get('generated_at') or '—'})")
    lines += [
        "",
        "## Сборка",
        "",
        "```",
        f"python tools/client_pack_v2.py --generated-at {data['generated_at']} "
        f"--out <OUT_DIR>"
        + (" --include-blocks" if data["blocks"].get("included") else ""),
        "```",
        "",
        "<OUT_DIR> — каталог этого пакета (например, `build/client_pack/v2_7`). "
        "Пакет детерминирован при том же `--generated-at`: DOCX/XLSX нормализуются (zip-времена и "
        "docProps/core.xml), JSON пишется с сортировкой ключей. Сборка fail-closed: при "
        "отсутствующем/битом артефакте или недопустимом статусе выход не создаётся. "
        "`--include-blocks` добавляет опциональные разделы блоков 15/17 (институты, технологии).",
        "",
        "## Ограничения",
        "",
        "- Ни одна модель не admitted; C4 открыт, C6 недоступен до накопления forward-журнала.",
        "- Сценарные числа — сценарная арифметика по допущениям, не точечный прогноз и не "
        "доверительный интервал.",
        "- Сырые наблюдения третьих сторон не публикуются; только производные статистики и "
        "собственные прогнозы.",
        "- Не инвестиционная рекомендация и не юридическая консультация.",
        "",
    ]
    Path(path).write_text("\n".join(lines), encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------------- reproducibility
def build_reproducibility(path: Path, data: dict, files: list, command: str) -> None:
    document = {
        "schema": REPRO_SCHEMA,
        "kind": "client_pack_reproducibility",
        "status": STATUS,
        "generated_at": data["generated_at"],
        "tool": "tools/client_pack_v2.py",
        "command": command,
        "inputs": data["sources"],
        "deterministic_artifacts": {item["path"]: item["sha256"]
                                    for item in sorted(files, key=lambda entry: entry["path"])},
        "normalization": {
            "docx": "zip entry timestamps + docProps/core.xml created/modified pinned to generated_at",
            "xlsx": "zip entry timestamps + docProps/core.xml created/modified pinned to generated_at",
            "json": "sorted keys, indent=1, UTF-8, LF",
            "html": "static, no scripts, no external links",
        },
        "file_count": len(files),
        "document_digest": data["document_digest"],
    }
    _write_json(path, document)


# --------------------------------------------------------------------------------------- build pack
def build_pack(out_dir: Path, *, inputs_root: Path | None = None, scenario_id: str = DEFAULT_SCENARIO,
               client: str = DEFAULT_CLIENT, generated_at: str | None = None,
               root: Path | None = None, bridge_path=None, include_blocks: bool = False) -> dict:
    """Build the whole client pack; raises ClientPackError (fail-closed) before any output."""
    root = Path(root) if root is not None else ROOT
    inputs_root = Path(inputs_root) if inputs_root is not None else root / "build"
    out_dir = Path(out_dir)
    if not client or not client.strip():
        raise ClientPackError("client name must not be empty")
    if not re.fullmatch(r"[a-z0-9_]+", scenario_id or ""):
        raise ClientPackError(f"invalid scenario id {scenario_id!r}")
    inputs = load_inputs(inputs_root, scenario_id, root, bridge_path=bridge_path,
                         include_blocks=include_blocks)
    generated_at = generated_at or _default_generated_at(inputs)
    _parse_timestamp(generated_at, "generated_at")
    generated = _parse_timestamp(generated_at, "generated_at")
    data = build_document(inputs, client, generated_at)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".client_pack_v2-", dir=str(out_dir.parent)))
    try:
        files: list[dict] = []

        def record(path: Path) -> None:
            files.append({"path": path.name, "sha256": _sha256(path),
                          "bytes": path.stat().st_size})

        docx_builders = [
            ("01_Executive_Summary.docx", build_executive),
            ("05_Methodology_and_Sources.docx", build_methodology),
            ("06_Licence_and_Disclaimer.docx", build_licence),
        ]
        for name, builder in docx_builders:
            path = staging / name
            builder(path, data, generated)
            _normalise_office_zip(path, generated)
            record(path)

        xlsx_path = staging / "02_Numbers.xlsx"
        build_xlsx(xlsx_path, data, generated)
        _normalise_office_zip(xlsx_path, generated)
        record(xlsx_path)

        build_dashboard(staging / "03_Dashboard.html", data)
        _write_json(staging / "04_Machine_Readable.json", data)
        for name in ("03_Dashboard.html", "04_Machine_Readable.json"):
            record(staging / name)

        build_readme(staging / "README.md", data, files)
        record(staging / "README.md")

        # The reproducibility command must not depend on the chosen output directory: for the
        # default destination it is the repository-relative path, for any other destination a
        # placeholder is recorded (the manifest stays byte-identical across rebuild directories).
        default_out = root / "build" / "client_pack" / "v2"
        try:
            same_destination = out_dir.resolve() == default_out.resolve()
        except OSError:
            same_destination = False
        out_display = _repo_path(root, out_dir) if same_destination else "<OUT_DIR>"
        command = (f"python tools/client_pack_v2.py --client {client!r} "
                   f"--scenario {scenario_id} --generated-at {generated_at} --out "
                   f"{out_display}")
        if data["bridge"].get("available"):
            command += f" --bridge {data['bridge']['source']}"
        if data["blocks"].get("included"):
            command += " --include-blocks"
        build_reproducibility(staging / "07_Reproducibility.json", data, files, command)
        record(staging / "07_Reproducibility.json")

        if out_dir.exists():
            shutil.rmtree(out_dir)
        staging.replace(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "ok": True,
        "schema": SCHEMA,
        "status": STATUS,
        "client": client,
        "scenario_id": scenario_id,
        "generated_at": generated_at,
        "out": str(out_dir).replace("\\", "/"),
        "files": sorted(item["path"] for item in files),
        "document_digest": data["document_digest"],
        "numbers": {
            "candidate": data["summary"]["candidate"],
            "candidate_status": data["summary"]["candidate_status"],
            "point": data["summary"]["point"],
            "c4_pass": data["summary"]["c4_pass"],
            "c6_status": data["summary"]["c6_status"],
            "modules": data["summary"]["modules_total"],
            "market_modules": data["summary"]["market_modules"],
            "forecasts": data["summary"]["forecasts"],
            "forecast_targets": data["summary"]["forecast_targets"],
            "scenario_id": data["summary"]["scenario_id"],
            "scenario_peak": data["summary"]["scenario_peak"],
            "api_version": data["summary"]["api_version"],
            "sources": data["summary"]["sources"],
            "bridge": {
                "available": data["bridge"]["available"],
                "scenarios": len(data["bridge"].get("scenarios") or []),
                "effects": len(data["bridge"].get("effects") or []),
                "sha256": data["bridge"].get("sha256"),
                "source": data["bridge"].get("source"),
            },
            "blocks": {
                "included": data["blocks"].get("included"),
                "available": data["blocks"].get("available"),
                "status": data["blocks"].get("status"),
                "count": len([item for item in data["blocks"].get("blocks") or []
                              if item.get("available")]),
                "sha256": {item["id"]: item.get("sha256")
                           for item in data["blocks"].get("blocks") or []
                           if item.get("available")},
            },
        },
    }


# -------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the AMFE Client Pack v2 from the real artefacts (fail-closed)")
    parser.add_argument("--root", default=str(ROOT), help="repository root (source paths)")
    parser.add_argument("--inputs-root", default=None,
                        help="artefact root (default <root>/build)")
    parser.add_argument("--out", default=None,
                        help="output directory (default <root>/build/client_pack/v2)")
    parser.add_argument("--client", default=DEFAULT_CLIENT, help="client name on the documents")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, help="market scenario id")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 timestamp YYYY-MM-DDTHH:MM:SSZ (determinism)")
    parser.add_argument("--bridge", default=None,
                        help=("optional forecast→world bridge artefact: JSON file or an output "
                              "directory of tools/forecast_world_bridge.py (default "
                              "<inputs-root>/forecast_world_bridge/report.json)"))
    parser.add_argument("--include-blocks", action="store_true",
                        help=("include the optional blocks 15/17 section (institutions, technology) "
                              "from <inputs-root>/institutions/report.json and "
                              "<inputs-root>/technology_scenarios/report.json"))
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    inputs_root = Path(args.inputs_root) if args.inputs_root else root / "build"
    out_dir = Path(args.out) if args.out else root / "build" / "client_pack" / "v2"
    try:
        summary = build_pack(out_dir, inputs_root=inputs_root, scenario_id=args.scenario,
                             client=args.client, generated_at=args.generated_at, root=root,
                             bridge_path=args.bridge, include_blocks=args.include_blocks)
    except ClientPackError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps({"status": "OK", "out": summary["out"],
                          "scenario": summary["scenario_id"],
                          "files": len(summary["files"]),
                          "numbers": summary["numbers"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
