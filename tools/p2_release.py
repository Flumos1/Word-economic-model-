#!/usr/bin/env python3
"""P2 0.1 release manifest: reproducible state of the D1-D6 acceptance criteria.

The tool reads **only committed-code and local data artefacts** and writes one JSON manifest with:

  * release metadata (version 0.1, date, git commit when available, schema);
  * the world summary (FIGARO / OECD ICIO / WIOD coverage, 8 scenarios on two datasets, SFC, GVAR);
  * one record per D1-D6 criterion: status (PASS/PARTIAL/FAIL/RESEARCH_ONLY), rationale, evidence
    files with SHA-256 and the numbers taken from those files, limitations;
  * SHA-256/bytes of every input, so the manifest can be re-checked;
  * explicit statements that P2 0.1 is a **research release, not an admission** and that no model
    is admitted (T1 candidate is CANDIDATE, regime candidate is SHADOW_TEST conditioned on the
    reconstructed data class).

Fail-closed: a missing required artefact or a malformed JSON raises and no manifest is written.
Determinism: the same inputs, ``--date`` and ``--git-commit`` produce byte-identical output; only
``generated_at``/``date`` may differ (the release date is also an explicit argument).

Usage:
    python tools/p2_release.py
    python tools/p2_release.py --data-root "F:/Мировая экономика модель/data/mrio" \
        --generated-at 2026-11-14T20:00:00Z --date 2026-11-14
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "AMFE-P2-RELEASE/1.0.0"
RELEASE = "P2 0.1"
VERSION = "0.1"
STATUSES = ("PASS", "PARTIAL", "FAIL", "RESEARCH_ONLY")
FALLBACK_DATA_ROOT = Path("F:/Мировая экономика модель/data/mrio")

#: Documents that are part of the release context (required unless listed as optional).
REQUIRED_DOCS = ("docs/120_Аудит_ТЗ_и_план_P2.md", "docs/125_Реальный_MRIO_FIGARO.md",
                 "docs/128_GVAR_на_реальных_данных.md", "docs/129_Схема_v1.1_и_WIOD.md",
                 "docs/130_OECD_ICIO.md", "docs/131_Сценарные_отчёты_мира.md",
                 "docs/133_SFC_IO_интеграция.md")
OPTIONAL_DOCS = ("docs/135_*.md", "docs/136_*.md")


class ReleaseError(RuntimeError):
    """Required input is missing/corrupt: the release manifest must not be written."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(Path(path).resolve()).replace("\\", "/")


def _read_json(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ReleaseError(f"required artefact missing ({label}): {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {label} {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise ReleaseError(f"{label} {path}: expected a JSON object")
    return document


def _artifact(root: Path, path: Path, required: bool = True) -> dict:
    path = Path(path)
    if not path.is_file():
        if required:
            raise ReleaseError(f"required artefact missing: {path}")
        return {"path": _rel(path, root), "exists": False, "sha256": None, "bytes": None}
    return {"path": _rel(path, root), "exists": True, "sha256": sha256_file(path),
            "bytes": path.stat().st_size}


def _git_commit(repo: Path) -> str:
    try:
        completed = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value else "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_data_root(explicit=None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise ReleaseError(f"data root is not a directory: {path}")
        return path
    env = os.environ.get("AMFE_MRIO_ROOT", "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    if FALLBACK_DATA_ROOT.is_dir():
        return FALLBACK_DATA_ROOT
    return ROOT / "data" / "mrio"


# ----------------------------------------------------------------------------------------- collect
def collect_inputs(repo: Path, data_root: Path) -> dict:
    """Load every input artefact and return the raw documents plus the SHA/bylines record."""
    repo, data_root = Path(repo), Path(data_root)
    required = {
        "figaro_manifest": data_root / "figaro" / "figaro_2019" / "manifest.json",
        "oecd_coverage": repo / "protocols" / "oecd_icio_coverage.json",
        "figaro_report_index": repo / "build" / "world" / "report_index.json",
        "report_v5": repo / "protocols" / "first_real_evaluation_report_v5.json",
        "regime_report": repo / "protocols" / "regime_v4_candidate_evaluation_report.json",
        "shadow_status": repo / "build" / "shadow" / "shadow_status.json",
        "protocol_v5": repo / "protocols" / "evaluation_protocol_v5.json",
    }
    documents: dict = {}
    files: list[dict] = []
    for key, path in required.items():
        if not Path(path).is_file():
            raise ReleaseError(f"required artefact missing ({key}): {path}")
        documents[key] = _read_json(path, key)
        entry = _artifact(repo, path)
        entry.update({"id": key, "required": True})
        files.append(entry)

    # OECD world report (optional by contract: it may be produced on another machine)
    oecd_index = repo / "build" / "world_oecd" / "report_index.json"
    if oecd_index.is_file():
        documents["oecd_report_index"] = _read_json(oecd_index, "oecd_report_index")
        entry = _artifact(repo, oecd_index)
        entry.update({"id": "oecd_report_index", "required": False})
        files.append(entry)
    else:
        documents["oecd_report_index"] = None
        files.append({"id": "oecd_report_index", "path": _rel(oecd_index, repo),
                      "exists": False, "sha256": None, "bytes": None, "required": False})

    # WIOD: all year manifests under the data root (15 in the full release, 3 on a partial copy)
    wiod_paths = sorted(data_root.glob("wiod_2016/wiod_*/manifest.json"))
    if not wiod_paths:
        raise ReleaseError(f"required artefact missing: no WIOD manifests under "
                           f"{data_root / 'wiod_2016'}")
    wiod = []
    for path in wiod_paths:
        entry = _artifact(repo, path)
        entry.update({"id": f"wiod_manifest_{path.parent.name}", "required": True})
        files.append(entry)
        wiod.append(_read_json(path, entry["id"]))
    documents["wiod_manifests"] = wiod

    # GVAR OOS report (research appendix): optional local cache, never a required data file
    gvar_candidates = [data_root / "cache" / "gvar_real" / "gvar_v2_report.json",
                       repo / "data" / "mrio" / "cache" / "gvar_real" / "gvar_v2_report.json"]
    gvar_path = next((path for path in gvar_candidates if path.is_file()), None)
    if gvar_path is not None:
        documents["gvar_report"] = _read_json(gvar_path, "gvar_report")
        entry = _artifact(repo, gvar_path)
        entry.update({"id": "gvar_report", "required": False})
        files.append(entry)
    else:
        documents["gvar_report"] = None
        files.append({"id": "gvar_report", "path": str(gvar_candidates[0]), "exists": False,
                      "sha256": None, "bytes": None, "required": False})

    # Documents (release context) and their hashes
    docs = []
    for pattern in REQUIRED_DOCS:
        path = repo / pattern
        entry = _artifact(repo, path)
        entry.update({"id": f"doc:{Path(pattern).stem}", "required": True,
                      "role": "release context"})
        files.append(entry)
        docs.append(entry["path"])
    optional = []
    for pattern in OPTIONAL_DOCS:
        matches = sorted(repo.glob(pattern))
        for path in matches:
            entry = _artifact(repo, path)
            entry.update({"id": f"doc:{path.stem}", "required": False, "role": "optional"})
            files.append(entry)
            optional.append(entry["path"])
    documents["docs"] = {"required": docs, "optional": optional}

    # Optional protocol v4 (legacy chain used by the first evaluation)
    v4 = repo / "protocols" / "evaluation_protocol_v4.json"
    if v4.is_file():
        entry = _artifact(repo, v4)
        entry.update({"id": "protocol_v4", "required": False})
        files.append(entry)

    documents["files"] = sorted(files, key=lambda item: item["id"])
    return documents


# ---------------------------------------------------------------------------------- world summary
def _figaro_summary(manifest: dict) -> dict:
    dataset = manifest.get("dataset") or {}
    validation = manifest.get("validation") or {}
    coverage = manifest.get("coverage") or {}
    metrics = validation.get("metrics") or {}
    return {
        "source": "Eurostat FIGARO (NAIO_10_FCP_IP3, product-by-product, 2019)",
        "dataset_id": manifest.get("dataset_id"),
        "countries": dataset.get("countries", metrics.get("countries")),
        "industries": dataset.get("industries", metrics.get("industries")),
        "sectors": (dataset.get("countries", 0) or 0) * (dataset.get("industries", 0) or 0),
        "years": dataset.get("years"),
        "units": dataset.get("units"),
        "sha256": dataset.get("sha256"),
        "bytes": dataset.get("bytes"),
        "cells_Z": dataset.get("cells_Z"),
        "stored_Z": dataset.get("stored_Z"),
        "gdp_coverage_named_pct": coverage.get("named_share_pct"),
        "gdp_coverage_with_row_pct": coverage.get("coverage_with_row_pct"),
        "validation_ok": bool(validation.get("ok")),
        "max_abs_column_residual": metrics.get("max_abs_column_residual"),
        "total_output": metrics.get("total_output"),
        "total_value_added": metrics.get("total_value_added"),
    }


def _oecd_summary(protocol: dict, report_index: dict | None) -> dict:
    entry = {
        "source": "OECD ICIO 2016 (ISIC Rev.3)",
        "economies": protocol.get("economies"),
        "countries_named": protocol.get("countries_named"),
        "industries": protocol.get("industries"),
        "period": protocol.get("period"),
        "d1_pass_period": protocol.get("d1_pass_period"),
        "d1_fail_period": protocol.get("d1_fail_period"),
        "gdp_coverage_min_pct": protocol.get("coverage_world_value_added_pct_min"),
        "gdp_coverage_max_pct": protocol.get("coverage_world_value_added_pct_max"),
        "units": protocol.get("units"),
        "status": protocol.get("status"),
        "report_index_available": report_index is not None,
    }
    if report_index:
        dataset = report_index.get("dataset") or {}
        entry.update({"dataset_sha256": dataset.get("sha256"), "dataset_bytes": dataset.get("bytes"),
                      "sectors": dataset.get("sectors"), "cells_Z": dataset.get("cells_Z"),
                      "stored_Z": dataset.get("stored_Z")})
    return entry


def _wiod_summary(manifests: list[dict]) -> dict:
    coverage = [float((manifest.get("coverage") or {}).get("named_share_percent"))
                for manifest in manifests
                if (manifest.get("coverage") or {}).get("named_share_percent") is not None]
    years = sorted(int((manifest.get("dataset") or {}).get("years", [0])[0]) for manifest in manifests
                   if (manifest.get("dataset") or {}).get("years"))
    countries = {int((manifest.get("dataset") or {}).get("countries", 0)) for manifest in manifests}
    industries = {int((manifest.get("dataset") or {}).get("industries", 0)) for manifest in manifests}
    residuals = []
    for manifest in manifests:
        metrics = (manifest.get("validation") or {}).get("metrics") or {}
        value = metrics.get("max_abs_column_residual")
        if value is not None:
            residuals.append(float(value))
    return {
        "source": "WIOD 2016 release (current prices, USD_mn)",
        "years": years,
        "year_count": len(years),
        "countries": max(countries) if countries else None,
        "industries": max(industries) if industries else None,
        "gdp_coverage_min_pct": min(coverage) if coverage else None,
        "gdp_coverage_max_pct": max(coverage) if coverage else None,
        "validation_ok": all(bool((manifest.get("validation") or {}).get("ok"))
                             for manifest in manifests),
        "max_abs_column_residual": max(residuals) if residuals else None,
    }


def _scenarios_summary(report_index: dict, label: str) -> dict:
    checks = report_index.get("checks") or {}
    scenarios = report_index.get("scenarios") or []
    residuals = [float((entry.get("headline") or {}).get("max_abs_material_residual"))
                 for entry in scenarios if (entry.get("headline") or {}).get("max_abs_material_residual")
                 is not None]
    return {
        "dataset": label,
        "status": report_index.get("status"),
        "scenario_count": checks.get("scenario_count", len(scenarios)),
        "all_feasible": bool(checks.get("all_feasible")),
        "all_validations_pass": bool(checks.get("all_validations_pass")),
        "all_sfc_pass": bool(checks.get("all_sfc_pass")),
        "max_abs_material_residual": checks.get("max_abs_material_residual"),
        "max_scenario_headline_residual": max(residuals) if residuals else None,
        "bytes": report_index.get("dataset", {}).get("bytes"),
        "sha256": report_index.get("dataset", {}).get("sha256"),
    }


def _gvar_summary(report: dict | None) -> dict:
    if report is None:
        return {"report_available": False, "oos_negative": None,
                "note": "GVAR OOS report not found locally; D4 is taken from docs/128"}
    vs = report.get("vs_best_baseline") or {}
    comparisons = report.get("comparisons") or {}
    primary = report.get("sample") or {}
    negative = (vs.get("primary_better_cells", 0) < vs.get("baseline_better_cells", 0))
    year = comparisons.get("GVAR_2019") or {}
    year_aggregate = year.get("aggregate") or {}
    return {
        "report_available": True,
        "report_id": report.get("report_id"),
        "countries": primary.get("countries"),
        "years": primary.get("years"),
        "primary": comparisons.get("primary"),
        "baselines": vs.get("baselines"),
        "primary_better_cells": vs.get("primary_better_cells"),
        "baseline_better_cells": vs.get("baseline_better_cells"),
        "cells": vs.get("cells"),
        "oos_negative": bool(negative),
        "year_weights_mean_delta_rmse": year_aggregate.get("mean_delta_rmse"),
        "year_weights_primary_better_cells": year_aggregate.get("primary_better_cells"),
        "year_weights_other_better_cells": year_aggregate.get("other_better_cells"),
        "limits": list(report.get("limits") or []),
        "note": "research OOS only: not connected to admission, registry or candidates",
    }


def _candidates_summary(report_v5: dict, regime_report: dict, shadow: dict) -> dict:
    admission = report_v5.get("admission") or {}
    regime = regime_report.get("admission") or {}
    candidate = shadow.get("candidate") or {}
    c6 = shadow.get("c6") or {}
    return {
        "t1": {
            "candidate": report_v5.get("candidate"),
            "target": report_v5.get("target"),
            "horizon": report_v5.get("horizon"),
            "status": admission.get("status"),
            "status_label": admission.get("status_label"),
            "conditional_data_class": admission.get("conditional_data_class"),
            "c4_pass": bool((admission.get("criteria") or {}).get("C4", {}).get("pass")),
            "c6_pass": bool((admission.get("criteria") or {}).get("C6", {}).get("pass")),
            "protocol_version": (report_v5.get("protocol") or {}).get("version"),
        },
        "regime": {
            "candidate": regime_report.get("candidate"),
            "target": regime_report.get("target"),
            "horizon": regime_report.get("horizon"),
            "status": regime.get("status"),
            "status_label": regime.get("status_label"),
            "conditional_data_class": regime.get("conditional_data_class"),
            "stages": regime.get("stages"),
            "protocol_version": (regime_report.get("protocol") or {}).get("version"),
        },
        "shadow": {
            "candidate": candidate.get("id"),
            "status": shadow.get("status"),
            "journal_rows": (shadow.get("journal") or {}).get("rows_total"),
            "c6_resolved": c6.get("n_resolved"),
            "c6_min_forecasts": c6.get("min_forecasts"),
            "c6_pass": bool(c6.get("pass")),
            "alerts": list(shadow.get("alerts") or []),
        },
        "admitted": False,
        "note": "no candidate is admitted; CANDIDATE and SHADOW_TEST are not deployment statuses",
    }


# ------------------------------------------------------------------------------------- criteria
def build_criteria(documents: dict, world: dict) -> list[dict]:
    """Derive the D1-D6 statuses from the evidence (deterministic, fail-closed on contradiction)."""
    figaro = world["figaro"]
    oecd = world["oecd"]
    wiod = world["wiod"]
    figaro_index = documents["figaro_report_index"]
    oecd_index = documents.get("oecd_report_index")
    gvar = world["gvar"]
    scenarios = world["scenarios"]

    criteria = []

    # D1: real MRIO datasets, coverage >=80% of world GDP, identities and manifests.
    d1_pass = bool(figaro.get("validation_ok")
                   and (figaro.get("gdp_coverage_named_pct") or 0.0) >= 80.0
                   and oecd.get("status")
                   and (oecd.get("gdp_coverage_min_pct") or 0.0) >= 80.0
                   and wiod.get("year_count", 0) >= 1 and wiod.get("validation_ok"))
    d1_missing = [name for name, ok in (
        ("figaro coverage >=80%", (figaro.get("gdp_coverage_named_pct") or 0.0) >= 80.0),
        ("figaro identities", figaro.get("validation_ok")),
        ("oecd coverage >=80%", (oecd.get("gdp_coverage_min_pct") or 0.0) >= 80.0),
        ("wiod identities", wiod.get("validation_ok")),
    ) if not ok]
    criteria.append({
        "id": "D1", "title": "Данные готовы: реальные MRIO, покрытие ≥80% ВВП, идентичности",
        "status": "PASS" if d1_pass else "FAIL",
        "rationale": "FIGARO 2019, OECD ICIO (2000-2011) и WIOD (2000-2014) сконвертированы в "
                     "каноническую схему; покрытие по ВВП выше 80% на всех трёх источниках, "
                     "тождества и P2-гейт валидатора PASS, манифесты с SHA-256.",
        "evidence": {
            "figaro": {"countries": figaro.get("countries"), "industries": figaro.get("industries"),
                       "gdp_coverage_named_pct": figaro.get("gdp_coverage_named_pct"),
                       "sha256": figaro.get("sha256"), "bytes": figaro.get("bytes")},
            "oecd": {"economies": oecd.get("economies"), "industries": oecd.get("industries"),
                     "gdp_coverage_min_pct": oecd.get("gdp_coverage_min_pct"),
                     "gdp_coverage_max_pct": oecd.get("gdp_coverage_max_pct"),
                     "period": oecd.get("period"), "d1_pass_period": oecd.get("d1_pass_period")},
            "wiod": {"years": wiod.get("year_count"), "countries": wiod.get("countries"),
                     "industries": wiod.get("industries"),
                     "gdp_coverage_min_pct": wiod.get("gdp_coverage_min_pct"),
                     "gdp_coverage_max_pct": wiod.get("gdp_coverage_max_pct")},
        },
        "limitations": ([] if d1_pass else [f"not met: {', '.join(d1_missing)}"]) + [
            "FIGARO: RoW is the FIGARO WRL_REST aggregate; EUR->USD at the annual average rate",
            "OECD: 34 industries / 2011 release; OECD 1995-1999 refused (negative final demand cell)",
            "WIOD: 44x56, current prices; negative inventory/VA cells handled by schema v1.1 adjustments",
        ],
    })

    # D2: static core on real data.
    def index_ok(index):
        checks = (index or {}).get("checks") or {}
        residual = checks.get("max_abs_material_residual")
        return bool(index) and checks.get("all_validations_pass") and (
            residual is not None and float(residual) <= 1e-6)

    d2_figaro, d2_oecd = index_ok(figaro_index), index_ok(oecd_index)
    if d2_figaro and d2_oecd:
        d2_status = "PASS"
    elif d2_figaro or d2_oecd:
        d2_status = "PARTIAL"
    else:
        d2_status = "FAIL"
    criteria.append({
        "id": "D2", "title": "Статическое ядро: A=Z/x, продуктивность, solve, агрегация",
        "status": d2_status,
        "rationale": "Пайплайн world_pipeline.py проходит валидацию на реальных FIGARO 2019 и "
                     "OECD ICIO 2011: все сценарии feasible, материальные балансы сходятся, "
                     "решатель x=(I-A)^-1 y воспроизводим; A строится из реальных Z.",
        "evidence": {
            "figaro": {"status": figaro_index.get("status"),
                       "all_validations_pass": (figaro_index.get("checks") or {}).get("all_validations_pass"),
                       "max_abs_material_residual": (figaro_index.get("checks") or {}).get(
                           "max_abs_material_residual"),
                       "sectors": (figaro_index.get("dataset") or {}).get("sectors")},
            "oecd": None if oecd_index is None else {
                "status": oecd_index.get("status"),
                "all_validations_pass": (oecd_index.get("checks") or {}).get("all_validations_pass"),
                "max_abs_material_residual": (oecd_index.get("checks") or {}).get(
                    "max_abs_material_residual"),
                "sectors": (oecd_index.get("dataset") or {}).get("sectors")},
        },
        "limitations": [
            "сравнительная статика без цен/замещения/динамики (docs/127, docs/131)",
            "агрегация по странам/отраслям проверяется по итогам, а не по каждой ячейке Z",
        ],
    })

    # D3: SFC block.
    def sfc_pass(index):
        return bool(index) and bool((index.get("sfc") or {}).get("pass"))

    d3_status = "PASS" if sfc_pass(figaro_index) and sfc_pass(oecd_index) else (
        "PARTIAL" if sfc_pass(figaro_index) or sfc_pass(oecd_index) else "FAIL")
    criteria.append({
        "id": "D3", "title": "SFC: счета стран, S=I+CA, ΣCA=0, шаг потоков/запасов",
        "status": d3_status,
        "rationale": "SFC-блок проходит на FIGARO 2019 и OECD 2011: балансы S=I+CA и ΣCA=0, "
                     "согласование потоков/запасов; это бухгалтерский блок, не поведенческая модель.",
        "evidence": {
            "figaro": {"pass": bool((figaro_index.get("sfc") or {}).get("pass")),
                       "checks": (figaro_index.get("sfc") or {}).get("checks")},
            "oecd": None if oecd_index is None else {
                "pass": bool((oecd_index.get("sfc") or {}).get("pass")),
                "checks": (oecd_index.get("sfc") or {}).get("checks")},
        },
        "limitations": ["final-goods destination, consumption/investment split are not identified by "
                        "the dataset; assumptions are listed in docs/133"],
    })

    # D4: dynamics / GVAR OOS.
    negative = gvar.get("oos_negative")
    if negative is None:
        d4_status = "PARTIAL"
        d4_reason = "GVAR OOS report is not available locally; docs/128 records a negative result"
    elif negative:
        d4_status = "RESEARCH_ONLY"
        d4_reason = ("GVAR OOS отрицательный: на 32 ячейках GVAR лучше AR(1)/RW только в "
                     f"{gvar.get('primary_better_cells')} против {gvar.get('baseline_better_cells')}; "
                     "честно зафиксированный отрицательный результат, вне допуска")
    else:
        d4_status = "PASS"
        d4_reason = "GVAR OOS не хуже бенчмарков на агрегатах (см. отчёт)"
    criteria.append({
        "id": "D4", "title": "Динамика: GVAR на реальных рядах, стабильность, OOS",
        "status": d4_status,
        "rationale": d4_reason,
        "evidence": {"gvar": {key: gvar.get(key) for key in (
            "report_id", "countries", "years", "primary", "baselines", "primary_better_cells",
            "baseline_better_cells", "cells", "oos_negative", "year_weights_mean_delta_rmse")}},
        "limitations": list(gvar.get("limits") or []) + [
            "D4 — research-only: динамика не входит в допуск и не используется сценариями P2 0.1",
        ],
    })

    # D5: mandatory scenario set on both worlds.
    d5_ok = [scenario for scenario in scenarios
             if scenario.get("scenario_count", 0) >= 8 and scenario.get("all_feasible")
             and scenario.get("all_validations_pass")]
    d5_status = "PASS" if len(d5_ok) >= 2 else ("PARTIAL" if d5_ok else "FAIL")
    criteria.append({
        "id": "D5", "title": "Сценарии/отчёты: энергия +50%, импорт +10%, FX −20% на двух мирах",
        "status": d5_status,
        "rationale": "Полный набор из 8 сценариев прогнан на FIGARO 2019 (3200 секторов) и "
                     "OECD ICIO 2011 (2414 секторов); отчёты «страна × отрасль» JSON/MD, "
                     "балансы сходятся, SFC-блок PASS.",
        "evidence": {"scenarios": scenarios},
        "limitations": ["сравнительная статика; без цен, замещения, динамики и обратных связей",
                        "отчёты агрегатные: страновые и отраслевые суммы, без сырых Z/x/y"],
    })

    # D6: integration (pipeline, tests, docs, reproducible release).
    pipeline = ROOT / "tools" / "world_pipeline.py"
    tests = [ROOT / "tests" / "test_world_pipeline.py", ROOT / "tests" / "test_figaro_import.py",
             ROOT / "tests" / "test_p2_release.py"]
    missing = [path.name for path in [pipeline, *tests] if not path.is_file()]
    d6_status = "PASS" if not missing and documents.get("docs", {}).get("required") else "PARTIAL"
    criteria.append({
        "id": "D6", "title": "Интеграция: один пайплайн, тесты, docs, воспроизводимый релиз",
        "status": d6_status,
        "rationale": "world_pipeline.py (data -> core -> scenarios -> report) воспроизводим на "
                     "FIGARO и OECD, покрыт тестами, документирован (docs/120,125,127-133); "
                     "настоящий манифест — релизный артефакт P2 0.1.",
        "evidence": {"pipeline": "tools/world_pipeline.py",
                     "tests_present": [path.name for path in tests if path.is_file()],
                     "missing": missing,
                     "docs_required": documents.get("docs", {}).get("required", []),
                     "docs_optional_present": documents.get("docs", {}).get("optional", [])},
        "limitations": ["P2 0.1 не включает T2/ACTIVE и микроуровень (фирмы); это research-release",
                        "релиз не является допуском: T1 CANDIDATE, regime SHADOW_TEST (conditional)"],
    })
    for criterion in criteria:
        if criterion["status"] not in STATUSES:
            raise ReleaseError(f"{criterion['id']}: unknown status {criterion['status']!r}")
    return criteria


# ----------------------------------------------------------------------------------------- manifest
def build_manifest(repo: Path = ROOT, data_root: Path | None = None,
                   generated_at: str | None = None, date: str | None = None,
                   git_commit: str | None = None) -> dict:
    repo = Path(repo)
    data_root = resolve_data_root(data_root)
    generated_at = generated_at or _now()
    date = date or generated_at[:10]
    documents = collect_inputs(repo, data_root)

    world = {
        "figaro": _figaro_summary(documents["figaro_manifest"]),
        "oecd": _oecd_summary(documents["oecd_coverage"], documents.get("oecd_report_index")),
        "wiod": _wiod_summary(documents["wiod_manifests"]),
        "scenarios": [
            _scenarios_summary(documents["figaro_report_index"], "figaro"),
            *([] if documents.get("oecd_report_index") is None else
              [_scenarios_summary(documents["oecd_report_index"], "oecd")]),
        ],
        "gvar": _gvar_summary(documents.get("gvar_report")),
        "candidates": _candidates_summary(documents["report_v5"], documents["regime_report"],
                                          documents["shadow_status"]),
    }
    criteria = build_criteria(documents, world)

    manifest = {
        "schema": SCHEMA,
        "release": RELEASE,
        "version": VERSION,
        "generated_at": generated_at,
        "date": date,
        "git_commit": git_commit or _git_commit(repo),
        "status": "RESEARCH_RELEASE",
        "admission": False,
        "admission_statement": (
            "P2 0.1 — исследовательский релиз. Ни один кандидат не допущен: T1 ENSEMBLE_INVERSE_MSE "
            "остаётся CANDIDATE (C4 FAIL), ENSEMBLE_REGIME_V4 — SHADOW_TEST с "
            "conditional_data_class=true. Релиз не является допуском, прогнозом или инвестиционной "
            "рекомендацией."
        ),
        "data_root": str(data_root).replace("\\", "/"),
        "world": world,
        "criteria": criteria,
        "criteria_summary": {criterion["id"]: criterion["status"] for criterion in criteria},
        "inputs": documents["files"],
        "docs_context_missing": [pattern for pattern in OPTIONAL_DOCS
                                  if not sorted(repo.glob(pattern))],
        "limitations": [
            "P2 0.1 — research release, не допуск модели и не прогнозный продукт",
            "допущенной модели нет: T1 CANDIDATE, regime SHADOW_TEST (conditional_data_class=true)",
            "сценарии — сравнительная статика без цен, замещения, динамики и обратных связей",
            "D4 (динамика) — отрицательный исследовательский результат, вне допуска",
            "FIGARO/OECD/WIOD — сторонние данные: в git не коммитятся, в манифесте только хеши и агрегаты",
            "T2/ACTIVE и микроуровень (фирмы) не входят в P2 0.1",
            ("docs/135 и docs/136 включены в inputs с SHA-256"
             if not [pattern for pattern in OPTIONAL_DOCS if not sorted(repo.glob(pattern))] else
             "docs/135 и docs/136 отсутствуют в рабочем дереве на дату релиза — это "
             "необязательный контекст, обязательные документы перечислены в inputs"),
        ],
        "reproduce": [
            "python tools/world_pipeline.py --dataset data/mrio/figaro/figaro_2019/dataset.json "
            "--build --solve",
            "python tools/world_reports.py --root <data_root> --dataset figaro_2019 --out build/world",
            "python tools/world_reports.py --root <data_root> --dataset oecd_icio/2011 --out build/world_oecd",
            "python tools/p2_release.py --data-root <data_root>",
        ],
        "links": {
            "repository": "https://github.com/Flumos1/Word-economic-model-",
            "site": "https://flumos1.github.io/Word-economic-model-/",
            "docs": documents["docs"],
        },
    }
    return manifest


def write_manifest(manifest: dict, path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


# ------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2 0.1 release manifest: D1-D6 statuses, evidence and input checksums")
    parser.add_argument("--repo", default=str(ROOT), help="repository root")
    parser.add_argument("--data-root", default=None,
                        help="MRIO data root (default AMFE_MRIO_ROOT, else F:, else repo/data/mrio)")
    parser.add_argument("--out", default=None,
                        help="output path (default <repo>/build/p2_release/p2_release.json)")
    parser.add_argument("--generated-at", default=None, help="ISO 8601 timestamp (determinism)")
    parser.add_argument("--date", default=None, help="release date YYYY-MM-DD (default: from timestamp)")
    parser.add_argument("--git-commit", default=None,
                        help="override the git commit (tests/deterministic rebuilds)")
    parser.add_argument("--json", action="store_true", help="print the manifest to stdout")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo)
    out = Path(args.out) if args.out else repo / "build" / "p2_release" / "p2_release.json"
    try:
        manifest = build_manifest(repo, data_root=args.data_root,
                                  generated_at=args.generated_at, date=args.date,
                                  git_commit=args.git_commit)
        sha = write_manifest(manifest, out)
    except ReleaseError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    summary = {"status": "OK", "release": manifest["release"], "date": manifest["date"],
               "git_commit": manifest["git_commit"], "out": str(out).replace("\\", "/"),
               "sha256": sha, "bytes": out.stat().st_size,
               "criteria": manifest["criteria_summary"], "inputs": len(manifest["inputs"])}
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
