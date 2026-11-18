"""P2 scenario reports: country x industry effects on a real MRIO dataset (docs/131).

The tool turns the performance pipeline (:mod:`world_pipeline`) into **scenario reports**:
it loads one canonical dataset, solves the baseline once, runs the declared static scenarios
(energy demand, import restriction, FX depreciation), aggregates the gross-output change by country
and by industry (top-10), records the balance/consistency checks and writes

    build/world/reports/<scenario>.json   aggregate-only machine-readable report
    build/world/reports/<scenario>.md     the same as a human-readable table
    build/world/report_index.json         index of all scenarios + headline numbers
    build/world/dashboard.html|md         static summary page (no JS, no external URLs)

Design rules
------------
* **Aggregate-only.**  Reports carry per-country (50) and per-industry (64) aggregates, totals,
  ratios and checksums; never ``Z``, ``x``, ``y`` or per-sector values (same rule as docs/121/127).
* **Fail-closed.**  A missing dataset (``BLOCKED_DATA``, exit 2), an unknown scenario type, an
  unknown country/industry, a non-productive shocked technology or a failed balance raises instead
  of writing a partial report (exit 1).  Reports are written only after every scenario succeeded.
* **Deterministic.**  Scenario order, top-list order (``-abs(delta)`` then name) and JSON key order
  are fixed; there are no timestamps in the reports.  Two runs on the same dataset produce
  byte-identical JSON/MD/HTML.
* **Research-only status.**  Every artefact carries ``status = RESEARCH_ONLY`` and the note
  "исследовательский, не допуск": this is a comparative-statics scenario report, not a forecast,
  not an admission decision and not a SHADOW_TEST.

CLI
---
    python tools/world_reports.py --root data/mrio/figaro --dataset figaro_2019 \
        --energy-industries CPA_B,CPA_C19,CPA_D35 --import-shares 0.10,0.20 \
        --fx-countries DE,FR --fx-shares 0.10,0.20 --out build/world
"""
from __future__ import annotations

import argparse
import gc
import os
import hashlib
import html
import json
import sys
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import world_pipeline as wp  # noqa: E402
from amfe.mrio.loaders import load_dataset  # noqa: E402
from amfe.mrio.schema import MRIOError  # noqa: E402
from amfe.world.sfc_io import SFCIOError, sfc_section  # noqa: E402

REPORT_SCHEMA = "amfe.world.scenario_report.v1"
INDEX_SCHEMA = "amfe.world.scenario_index.v1"
STATUS = "RESEARCH_ONLY"
STATUS_NOTE = ("Исследовательский отчёт, не допуск: сравнительная статика на реальной таблице "
               "FIGARO; это не прогноз, не admission и не SHADOW_TEST")
DEFAULT_ROOT = ROOT / "data" / "mrio" / "figaro"
DEFAULT_DATASET = "figaro_2019"
DEFAULT_OUT = ROOT / "build" / "world"
DEFAULT_ENERGY_INDUSTRIES = ("CPA_B", "CPA_C19", "CPA_D35")
DEFAULT_FX_COUNTRIES = ("DE", "FR")
DEFAULT_IMPORT_SHARES = (0.10, 0.20)
DEFAULT_FX_SHARES = (0.10, 0.20)
TOP_N = 10
SFC_SCHEMA = "amfe.world.sfc_block.v1"
DEFAULT_SFC_INVESTMENT_SHARE = 0.2
SFC_CHECK_KEYS = ("current_account_disagreement_max_abs", "global_current_account_sum",
                  "gdp_sum_minus_final_demand_sum", "saving_investment_identity_max_abs",
                  "net_foreign_assets_opening_sum", "net_foreign_assets_closing_sum",
                  "sfc_identity_residual", "sfc_net_worth_residual", "sfc_pass")
#: Aggregate labels that are not single countries (the FIGARO rest-of-world residual block).
REST_OF_WORLD_LABELS = ("ROW", "RoW", "REST_OF_WORLD", "WORLD")
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
LIMITATIONS = (
    "сравнительная статика x'=(I-A')^-1 y', без цен, замещения, динамики и обратных связей;",
    "сценарии не меняют y (кроме energy) и не меняют технологию (кроме import/fx);",
    "FX: изменение курса интерпретируется как pass-through в стоимость импортных затрат "
    "(количественная модель, ценового замещения нет);",
    "отчёт агрегатный: страновые и отраслевые суммы, без сырых Z/x/y и без per-sector значений;",
    "данные FIGARO — третья сторона (Eurostat), не коммитятся; статус исследовательский, не допуск.",
)


class ReportError(RuntimeError):
    """A report stage refused to continue (fail-closed)."""


class BlockedData(ReportError):
    """No usable dataset (maps to ``BLOCKED_DATA`` / exit 2)."""


# ---------------------------------------------------------------------------------- scenario specs
def parse_list(text, cast=str) -> tuple:
    if text is None:
        return ()
    if isinstance(text, (list, tuple)):
        return tuple(cast(item) for item in text)
    return tuple(cast(part.strip()) for part in str(text).split(",") if part.strip())


def _energy_spec(share: float, industries) -> dict:
    """One signed energy-demand spec: positive share = cut, negative = increase (e.g. +50%)."""
    share = float(share)
    if not -1.0 < share < 1.0 or abs(share) < 1e-12:
        raise ReportError(f"energy share must be in (-1, 1) and non-zero, got {share}")
    size = int(round(abs(share) * 100))
    if share > 0:
        scenario_id = "energy_minus5" if abs(share - 0.05) < 1e-12 else f"energy_minus{size}"
        description = (f"сокращение конечного спроса на {size}% в энергетических отраслях "
                       f"{', '.join(industries)}")
    else:
        scenario_id = f"energy_plus{size}"
        description = (f"рост конечного спроса на {size}% в энергетических отраслях "
                       f"{', '.join(industries)} (обратный энергетический шок)")
    return {
        "id": scenario_id,
        "type": "energy",
        "parameters": {"share": share, "industries": list(industries)},
        "description": description,
    }


def scenario_specs(energy_shares=0.05, energy_industries=None,
                   import_shares=DEFAULT_IMPORT_SHARES,
                   fx_countries=DEFAULT_FX_COUNTRIES, fx_shares=DEFAULT_FX_SHARES) -> list[dict]:
    """Declared scenario list, in fixed order: energy (several), import, fx (country-major)."""
    industries = tuple(energy_industries or DEFAULT_ENERGY_INDUSTRIES)
    if not industries:
        raise ReportError("energy industries must not be empty")
    shares = energy_shares if isinstance(energy_shares, (list, tuple)) else (energy_shares,)
    specs = [_energy_spec(share, industries) for share in shares]
    for share in import_shares:
        share = float(share)
        if not 0.0 < share < 1.0:
            raise ReportError(f"import share must be in (0, 1), got {share}")
        specs.append({
            "id": f"import_plus{int(round(share * 100))}",
            "type": "import",
            "parameters": {"share": share},
            "description": (f"снятие {share * 100:.0f}% межстрановых (импортных) коэффициентов "
                            "затрат: импортозамещение в сравнительной статике"),
        })
    for country in fx_countries:
        for share in fx_shares:
            share = float(share)
            if not 0.0 < share < 1.0:
                raise ReportError(f"fx share must be in (0, 1), got {share}")
            specs.append({
                "id": f"fx_minus{int(round(share * 100))}_{country}",
                "type": "fx",
                "parameters": {"country": str(country), "currency_change": -share,
                               "pass_through": share},
                "description": (f"обесценение валюты {country} на {share * 100:.0f}%: "
                                f"импортные затраты +{share * 100:.0f}% (pass-through)"),
            })
    return specs


def _validate_specs_against(table: wp.WorldTable, specs: list[dict]) -> None:
    """Fail-closed pre-flight: every scenario must be runnable on this table."""
    unknown_types = sorted({spec["type"] for spec in specs} - {"energy", "import", "fx"})
    if unknown_types:
        raise ReportError(f"unknown scenario types: {unknown_types}")
    industries = [str(name) for spec in specs if spec["type"] == "energy"
                  for name in spec["parameters"]["industries"]]
    missing = sorted(set(industries) - set(table.industries))
    if missing:
        raise ReportError(f"unknown energy industries: {missing}; dataset has {list(table.industries)}")
    countries = [str(spec["parameters"]["country"]) for spec in specs if spec["type"] == "fx"]
    unknown = sorted(set(countries) - set(table.countries))
    if unknown:
        raise ReportError(f"unknown FX countries: {unknown}; dataset has {list(table.countries)}")


# ------------------------------------------------------------------------------------ aggregation
def top_list(mapping: dict, n: int = TOP_N, exclude=()) -> dict:
    """Top-``n`` entries by absolute change, ties broken by name (deterministic).

    ``exclude`` removes aggregate rows (e.g. the FIGARO ``ROW`` rest-of-world block) from the
    ranking; shares are still computed over the full absolute total.
    """
    if not isinstance(mapping, dict) or not mapping:
        return {"top": [], "total": 0.0, "largest": None, "top_share_of_abs": 0.0,
                "entries": 0, "n": 0, "excluded": list(exclude)}
    items = [(str(name), float(value)) for name, value in mapping.items()]
    total_abs = float(sum(abs(value) for _, value in items))
    skipped = {str(name) for name in exclude}
    ranked = sorted(((name, value) for name, value in items if name not in skipped),
                    key=lambda item: (-abs(item[1]), item[0]))
    selected = ranked[:max(0, int(n))]
    top_abs = float(sum(abs(value) for _, value in selected))
    largest = ranked[0][0] if ranked else None
    return {
        "top": [{"name": name, "delta_output": round(value, 3),
                 "share_of_abs": round(abs(value) / total_abs, 6) if total_abs else 0.0}
                for name, value in selected],
        "total": round(total_abs, 3),
        "largest": largest,
        "top_share_of_abs": round(top_abs / total_abs, 6) if total_abs else 0.0,
        "entries": len(items),
        "n": len(selected),
        "excluded": sorted(skipped & set(name for name, _ in items)),
    }


def _dataset_meta(loaded, dataset_id: str, table: wp.WorldTable) -> dict:
    dataset = loaded.dataset
    return {
        "dataset_id": dataset_id,
        "sha256": loaded.sha256,
        "bytes": loaded.size_bytes,
        "schema": "amfe.mrio.dataset.v1",
        "countries": table.n_countries,
        "industries": table.n_industries,
        "sectors": table.n,
        "cells_Z": table.n * table.n,
        "stored_Z": int(table.Z.nnz),
        "stored_share": round(float(table.Z.nnz) / float(table.n * table.n), 6),
        "years": list(dataset.years),
        "units": table.units,
        "total_output": round(float(table.x.sum()), 3),
        "total_final_demand": round(float(table.y.sum()), 3),
        "total_value_added": round(float(table.va.sum()), 3),
        "source": dataset.source.get("name") if isinstance(dataset.source, dict) else None,
        "provenance_note": "third-party FIGARO data; read-only, never committed",
    }


def _baseline_block(table: wp.WorldTable, solver: wp.WorldSolver) -> dict:
    return {
        "total_output": round(float(solver.baseline_x.sum()), 3),
        "total_final_demand": round(float(table.y.sum()), 3),
        "total_value_added": round(float(table.va.sum()), 3),
        "spectral_radius": round(float(solver.spectral_radius), 12),
        "productive": bool(solver.spectral_radius < 1.0 - table.tolerance),
        "max_abs_material_residual": round(float(solver.material_residual()), 12),
    }


def build_sfc_block(table: wp.WorldTable, *, investment_share: float = DEFAULT_SFC_INVESTMENT_SHARE,
                   out_dir=None) -> dict:
    """SFC <-> IO accounting block for the reports (docs/133 block, docs/131 section).

    The block is computed once from the loaded IO table with :func:`amfe.world.sfc_io.sfc_section`
    and attached to every scenario report: scenarios change gross output, not the accounting
    identities, so ``S = I + CA`` and ``sum CA = 0`` are the same in every scenario.  If a pipeline
    report with an ``sfc`` block exists under ``<out>/pipeline/*/world_pipeline_report.json`` the
    block is recorded as available (``source.pipeline_report``), but the totals are recomputed at
    the requested investment share so the S/I magnitudes are comparable across artefacts.
    """
    block = sfc_section(table, investment_share=float(investment_share))
    block["schema"] = SFC_SCHEMA
    pipeline_source = None
    if out_dir is not None:
        pipeline_source = _pipeline_sfc_path(Path(out_dir))
    block["source"] = {"kind": "computed_from_table", "module": "amfe.world.sfc_io",
                       "investment_share": float(investment_share),
                       "pipeline_report": pipeline_source}
    return block


def _pipeline_sfc_path(out_dir: Path) -> str | None:
    """Path of the first pipeline report carrying an ``sfc`` block (or None)."""
    base = Path(out_dir) / "pipeline"
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*/world_pipeline_report.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and isinstance(document.get("sfc"), dict):
            return str(path)
    return None


def sfc_summary(block: dict | None) -> dict | None:
    """Index-level summary of the SFC block (totals, checks, top surplus/deficit, pass)."""
    if not block:
        return None
    checks = block.get("checks") or {}
    return {
        "schema": SFC_SCHEMA,
        "investment_share": block.get("investment_share"),
        "units": block.get("units"),
        "countries": block.get("countries"),
        "totals": block.get("totals"),
        "current_account": block.get("current_account"),
        "pass": bool(block.get("pass")),
        "checks": {key: checks[key] for key in SFC_CHECK_KEYS if key in checks},
        "assumptions": list(block.get("assumptions") or []),
        "source": block.get("source"),
        "note": ("baseline IO accounting block: the same for every scenario; scenarios change gross "
                 "output, not the identities (docs/133 conventions: A_c = FD_c, CA is the "
                 "intermediate-trade balance)"),
    }


def _format_sfc_rows(block: dict) -> list[str]:
    totals = block.get("totals") or {}
    checks = block.get("checks") or {}
    units = block.get("units") or ""
    surplus = (block.get("current_account") or {}).get("top_surplus") or []
    deficit = (block.get("current_account") or {}).get("top_deficit") or []

    def fmt(items):
        return ", ".join(f"{name} {_format_value(value, 1)}" for name, value in items[:3]) or "—"

    return [
        f"- ВВП {_format_value(totals.get('gdp'))}, конечный спрос "
        f"{_format_value(totals.get('final_demand'))}, сбережения "
        f"{_format_value(totals.get('savings'))}, инвестиции {_format_value(totals.get('investment'))}, "
        f"CA {_format_value(totals.get('current_account'))} {units} "
        f"(investment_share = {block.get('investment_share')})",
        f"- сумма CA по миру: {checks.get('global_current_account_sum', 0.0):+.3e}; "
        f"невязка двух сверок CA: {checks.get('current_account_disagreement_max_abs', 0.0):.3e}; "
        f"невязка S − I − CA: {checks.get('saving_investment_identity_max_abs', 0.0):.3e}",
        f"- SFC-шаг (opening + transactions + revaluation = closing): "
        f"{'PASS' if checks.get('sfc_pass') else 'FAIL'}; невязки тождества "
        f"{checks.get('sfc_identity_residual', 0.0):.3e} и чистой стоимости "
        f"{checks.get('sfc_net_worth_residual', 0.0):.3e}",
        f"- топ-профицит CA: {fmt(surplus)}; топ-дефицит CA: {fmt(deficit)}",
        "- блок относится к базовой таблице: сценарий меняет выпуск, но не счётные тождества; "
        "CA — баланс промежуточной торговли (поглощение A_c = FD_c), не официальный BoP "
        "(docs/131, docs/133)",
    ]


def build_scenario_report(table: wp.WorldTable, solver: wp.WorldSolver, spec: dict,
                          raw: dict, dataset_meta: dict, baseline: dict,
                          validation: dict, core: dict, sfc_block: dict | None = None) -> dict:
    """Assemble one aggregate-only scenario report from a raw ``world_pipeline`` result."""
    by_country = raw["impacts"]["by_country"]
    by_industry = raw["impacts"]["by_industry"]
    delta_total = float(raw["result"]["total_delta_output"])
    baseline_total = float(baseline["total_output"])
    checks = {
        "validation_pass": bool(validation["pass"]),
        "max_abs_row_residual": validation["max_abs_row_residual"],
        "max_abs_value_added_residual": validation["max_abs_value_added_residual"],
        "baseline_productive": baseline["productive"],
        "baseline_spectral_radius": baseline["spectral_radius"],
        "country_block_spectral_radius": core["spectral_radius"],
        "hawkins_simon_pass": bool(core["hawkins_simon_pass"]),
        "scenario_material_residual": raw["balances"]["max_abs_material_residual"],
        "feasible": bool(raw.get("feasible", True)),
    }
    if "technology" in raw:
        checks["technology"] = raw["technology"]
    if "sfc_check" in raw:
        checks["sfc_check"] = raw["sfc_check"]
    report = {
        "schema": REPORT_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "dataset": dataset_meta,
        "scenario": spec,
        "baseline": baseline,
        "result": {
            "total_output": raw["result"]["total_output"],
            "total_delta_output": round(delta_total, 3),
            "relative_delta_output": (round(delta_total / baseline_total, 9)
                                      if baseline_total else None),
            "max_abs_delta_output": raw["result"]["max_abs_delta_output"],
            "output_multiplier_vs_demand": raw["result"].get("output_multiplier_vs_demand"),
            "units": table.units,
        },
        "impacts": {
            "by_country": {name: round(float(value), 3) for name, value in by_country.items()},
            "by_industry": {name: round(float(value), 3) for name, value in by_industry.items()},
            "countries": top_list(by_country),
            "countries_named": top_list(by_country, exclude=REST_OF_WORLD_LABELS),
            "industries": top_list(by_industry),
        },
        "checks": checks,
        "method": {
            "model": "статическая линейная модель Леонтьева (сравнительная статика)",
            "equation": "x = (I - A)^-1 y; сценарий: x' = (I - A')^-1 y'",
            "aggregation": "Δx суммируется по странам и отраслям; топ-10 по |Δвыпуск|",
            "top_n": TOP_N,
        },
        "limitations": list(LIMITATIONS),
    }
    if spec["type"] == "energy":
        report["demand_shock"] = {
            "total_delta_final_demand": raw.get("total_delta_final_demand"),
            "by_country": raw["delta_final_demand"]["by_country"],
            "by_industry": raw["delta_final_demand"]["by_industry"],
        }
    if sfc_block is not None:
        report["sfc"] = sfc_block
    return report


def run_scenarios(table: wp.WorldTable, solver: wp.WorldSolver, specs: list[dict]) -> dict:
    """Run every declared scenario once, in order; fail-closed on the first refusal."""
    outputs = {}
    for spec in specs:
        parameters = spec["parameters"]
        try:
            if spec["type"] == "energy":
                outputs[spec["id"]] = wp.scenario_energy(
                    table, solver, share=parameters["share"],
                    industries=parameters["industries"])
            elif spec["type"] == "import":
                outputs[spec["id"]] = wp.scenario_import(
                    table, solver, share=parameters["share"])
            else:
                outputs[spec["id"]] = wp.scenario_fx(
                    table, solver, country=parameters["country"],
                    pass_through=parameters["pass_through"])
        except wp.PipelineError as exc:
            raise ReportError(f"scenario {spec['id']} refused: {exc}") from exc
    return outputs


# ---------------------------------------------------------------------------------------- index
def build_index(reports: list[dict], dataset_meta: dict, elapsed_ms: float,
                peak_memory_mb: float, output_dir: Path, sfc_block: dict | None = None) -> dict:
    entries = []
    for report in reports:
        scenario = report["scenario"]
        entries.append({
            "id": scenario["id"],
            "type": scenario["type"],
            "description": scenario["description"],
            "parameters": scenario["parameters"],
            "file_json": f"reports/{scenario['id']}.json",
            "file_md": f"reports/{scenario['id']}.md",
            "headline": {
                "total_delta_output": report["result"]["total_delta_output"],
                "relative_delta_output": report["result"]["relative_delta_output"],
                "largest_country": report["impacts"]["countries_named"]["largest"],
                "largest_country_including_row": report["impacts"]["countries"]["largest"],
                "rest_of_world_delta_output": next(
                    (report["impacts"]["by_country"][label] for label in REST_OF_WORLD_LABELS
                     if label in report["impacts"]["by_country"]), None),
                "largest_industry": report["impacts"]["industries"]["largest"],
                "max_abs_material_residual": report["checks"]["scenario_material_residual"],
            },
            "checks": {
                "validation_pass": report["checks"]["validation_pass"],
                "feasible": report["checks"]["feasible"],
                "sfc_pass": (report["checks"].get("sfc_check") or {}).get("pass"),
                "sfc_block_pass": bool((report.get("sfc") or {}).get("pass")),
            },
        })
    return {
        "schema": INDEX_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "dataset": dataset_meta,
        "scenarios": entries,
        "checks": {
            "scenario_count": len(entries),
            "all_validations_pass": all(entry["checks"]["validation_pass"] for entry in entries),
            "all_feasible": all(entry["checks"]["feasible"] for entry in entries),
            "max_abs_material_residual": max(
                (entry["headline"]["max_abs_material_residual"] or 0.0 for entry in entries),
                default=0.0),
            "all_sfc_pass": (bool(all(entry["checks"]["sfc_block_pass"] for entry in entries))
                             if sfc_block is not None else None),
        },
        "sfc": sfc_summary(sfc_block),
        "dashboard": {"html": "dashboard.html", "md": "dashboard.md"},
        "timings_ms": {"total": round(float(elapsed_ms), 3),
                       "peak_memory_mb": round(float(peak_memory_mb), 1)},
        "limitations": list(LIMITATIONS),
    }


# --------------------------------------------------------------------------------------- render
def _format_value(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_scenario_markdown(report: dict) -> str:
    scenario = report["scenario"]
    dataset = report["dataset"]
    result = report["result"]
    lines = [
        f"# Сценарий `{scenario['id']}`: {scenario['description']}",
        "",
        f"**Статус: {STATUS}** — {STATUS_NOTE}.",
        "",
        "## Данные",
        "",
        f"- датасет: `{dataset['dataset_id']}` (SHA-256 `{dataset['sha256'][:16]}…`), "
        f"{dataset['countries']} стран × {dataset['industries']} отраслей = {dataset['sectors']} "
        f"секторов, {dataset['stored_Z']} ненулевых ячеек Z ({dataset['stored_share'] * 100:.1f}%), "
        f"год {dataset['years']}, единицы {dataset['units']}",
        f"- выручка (baseline, суммарный выпуск): {_format_value(report['baseline']['total_output'])} "
        f"{dataset['units']}; конечный спрос {_format_value(report['baseline']['total_final_demand'])}; "
        f"добавленная стоимость {_format_value(report['baseline']['total_value_added'])}",
        "",
        "## Параметры сценария",
        "",
        "```json",
        json.dumps(scenario["parameters"], ensure_ascii=False, sort_keys=True, indent=2),
        "```",
        "",
        "## Результат",
        "",
        f"- Δвыпуск: **{_format_value(result['total_delta_output'])}** {dataset['units']} "
        f"({result['relative_delta_output'] * 100:.4f}% от baseline)",
        f"- выпуск после шока: {_format_value(result['total_output'])} {dataset['units']}",
        f"- максимальное |Δ| по сектору: {_format_value(result['max_abs_delta_output'], 3)}",
    ]
    if result.get("output_multiplier_vs_demand") is not None:
        lines.append(f"- мультипликатор выпуска к шоку спроса: "
                     f"{result['output_multiplier_vs_demand']:.6f}")
    lines += [
        "",
        f"## Топ-10 стран по |Δвыпуска| (всего {report['impacts']['countries_named']['entries']} "
        f"стран; доля топ-10 в сумме |Δ| — "
        f"{report['impacts']['countries_named']['top_share_of_abs'] * 100:.1f}%)",
        "",
        "| # | страна | Δвыпуск | доля в сумме |Δ| |",
        "|---|---|---:|---:|",
    ]
    for position, entry in enumerate(report["impacts"]["countries_named"]["top"], 1):
        lines.append(f"| {position} | {entry['name']} | {_format_value(entry['delta_output'], 3)} | "
                     f"{entry['share_of_abs'] * 100:.2f}% |")
    rest_of_world = next((name for name in REST_OF_WORLD_LABELS
                          if name in report["impacts"]["by_country"]), None)
    if rest_of_world is not None:
        lines += ["",
                  f"Агрегированный остаток мира `{rest_of_world}` (не одна страна) в таблицу не "
                  f"входит: Δвыпуск {_format_value(report['impacts']['by_country'][rest_of_world], 3)}; "
                  f"в полном ранжировании он {'первый' if report['impacts']['countries']['largest'] == rest_of_world else 'не первый'}."]
    lines += [
        "",
        f"## Топ-10 отраслей по |Δвыпуска| (всего "
        f"{report['impacts']['industries']['entries']} отраслей; доля топ-10 — "
        f"{report['impacts']['industries']['top_share_of_abs'] * 100:.1f}%)",
        "",
        "| # | отрасль | Δвыпуск | доля в сумме |Δ| |",
        "|---|---|---:|---:|",
    ]
    for position, entry in enumerate(report["impacts"]["industries"]["top"], 1):
        lines.append(f"| {position} | {entry['name']} | {_format_value(entry['delta_output'], 3)} | "
                     f"{entry['share_of_abs'] * 100:.2f}% |")
    checks = report["checks"]
    lines += [
        "",
        "## Проверки",
        "",
        f"- валидация таблицы: {'PASS' if checks['validation_pass'] else 'FAIL'}; "
        f"невязки продукта {checks['max_abs_row_residual']:.3e} и ДС "
        f"{checks['max_abs_value_added_residual']:.3e}",
        f"- базовая технология продуктивна: {'да' if checks['baseline_productive'] else 'нет'}; "
        f"ρ(A) = {checks['baseline_spectral_radius']:.9f}; Hawkins–Simon "
        f"{'PASS' if checks['hawkins_simon_pass'] else 'FAIL'}",
        f"- материальный остаток сценария: {checks['scenario_material_residual']:.3e}",
        f"- сценарий допустим: {'да' if checks['feasible'] else 'нет'}",
    ]
    if "technology" in checks:
        technology = checks["technology"]
        lines.append(f"- технология после шока: ρ(A') = "
                     f"{technology.get('spectral_radius_after')}, продуктивна "
                     f"{'да' if technology.get('productive_after') else 'нет'}")
    if "sfc_check" in checks:
        sfc = checks["sfc_check"]
        lines.append(f"- SFC-проверка проводки FX: {'PASS' if sfc.get('pass') else 'FAIL'} "
                     f"(мировая чистая стоимость {sfc.get('world_net_worth_residual')})")
    if report.get("sfc"):
        lines += ["", "## SFC-проверки (S = I + CA)", ""]
        lines += _format_sfc_rows(report["sfc"])
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", "## Воспроизведение", "",
              "```powershell",
              "python tools/world_reports.py --root data/mrio/figaro --dataset figaro_2019 "
              f"--out build/world",
              "```", ""]
    return "\n".join(lines)


def render_dashboard_markdown(index: dict) -> str:
    dataset = index["dataset"]
    lines = [
        "# Дашборд мировых сценарных отчётов",
        "",
        f"**Статус: {STATUS}** — {STATUS_NOTE}.",
        "",
        f"Данные: `{dataset['dataset_id']}` (SHA-256 `{dataset['sha256'][:16]}…`), "
        f"{dataset['countries']} стран × {dataset['industries']} отраслей, "
        f"{dataset['stored_Z']} ненулевых Z, год {dataset['years']}, {dataset['units']}. "
        f"Baseline выпуск: {_format_value(dataset['total_output'])}.",
        "",
        "## Сценарии",
        "",
        "| сценарий | Δвыпуск | % baseline | крупнейшая страна | крупнейшая отрасль | отчёт |",
        "|---|---:|---:|---|---|---|",
    ]
    baseline = float(dataset["total_output"] or 0.0)
    for entry in index["scenarios"]:
        headline = entry["headline"]
        delta = float(headline["total_delta_output"] or 0.0)
        relative = (delta / baseline * 100.0) if baseline else 0.0
        lines.append(
            f"| `{entry['id']}` — {entry['description']} | "
            f"{_format_value(delta)} | {relative:.4f}% | {headline['largest_country']} | "
            f"{headline['largest_industry']} | [MD]({entry['file_md']}) / "
            f"[JSON]({entry['file_json']}) |")
    lines += [
        "",
        "## Проверки",
        "",
        f"- сценариев: {index['checks']['scenario_count']}; валидация таблицы: "
        f"{'PASS' if index['checks']['all_validations_pass'] else 'FAIL'}; "
        f"все сценарии допустимы: {'да' if index['checks']['all_feasible'] else 'нет'};",
        f"- максимальный материальный остаток сценариев: "
        f"{index['checks']['max_abs_material_residual']:.3e}.",
        "",
    ]
    if index.get("sfc"):
        lines += ["", "## SFC-проверки (S = I + CA)", ""]
        lines += _format_sfc_rows(index["sfc"])
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in index["limitations"]]
    lines += ["", "---", "", f"Статус: **{STATUS}** ({STATUS_NOTE}).", ""]
    return "\n".join(lines)


_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem auto; max-width: 1100px;
       color: #1a1a1a; background: #f7f7f8; padding: 0 1rem; }
h1 { font-size: 1.5rem; margin-bottom: .25rem; }
h2 { font-size: 1.1rem; margin-top: 1.75rem; border-bottom: 1px solid #d0d0d4; padding-bottom: .25rem; }
.status { display: inline-block; background: #fde8c8; border: 1px solid #d99b2b; color: #6b4a00;
          padding: .25rem .6rem; border-radius: .4rem; font-weight: 600; font-size: .85rem; }
.meta { color: #555; font-size: .9rem; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: .88rem; }
th, td { border: 1px solid #d8d8dc; padding: .35rem .5rem; text-align: left; }
th { background: #ececf0; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
a { color: #1a5fb4; }
footer { margin-top: 2.5rem; color: #555; font-size: .85rem; }
ul { padding-left: 1.25rem; }
"""


def render_dashboard_html(index: dict) -> str:
    dataset = index["dataset"]
    baseline = float(dataset["total_output"] or 0.0)
    rows = []
    for entry in index["scenarios"]:
        headline = entry["headline"]
        delta = float(headline["total_delta_output"] or 0.0)
        relative = (delta / baseline * 100.0) if baseline else 0.0
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(entry['id'])}</code><br>"
            f"<span class='meta'>{html.escape(entry['description'])}</span></td>"
            f"<td class='num'>{html.escape(_format_value(delta))}</td>"
            f"<td class='num'>{relative:.4f}%</td>"
            f"<td>{html.escape(str(headline['largest_country']))}</td>"
            f"<td>{html.escape(str(headline['largest_industry']))}</td>"
            f"<td><a href='{html.escape(entry['file_md'])}'>MD</a> · "
            f"<a href='{html.escape(entry['file_json'])}'>JSON</a></td>"
            "</tr>")
    limitations = "\n".join(f"<li>{html.escape(item)}</li>" for item in index["limitations"])
    sfc_section_html = ""
    if index.get("sfc"):
        sfc_rows = "\n".join(f"<li>{html.escape(line[2:] if line.startswith('- ') else line)}</li>"
                             for line in _format_sfc_rows(index["sfc"]))
        sfc_section_html = f"<h2>SFC-проверки (S = I + CA)</h2>\n<ul>\n{sfc_rows}\n</ul>\n"
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Дашборд мировых сценарных отчётов (исследовательский)</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Дашборд мировых сценарных отчётов</h1>
<p><span class="status">Статус: {html.escape(STATUS)} — исследовательский, не допуск</span></p>
<p class="meta">{html.escape(STATUS_NOTE)}</p>

<h2>Данные</h2>
<p class="meta">Датасет <code>{html.escape(dataset['dataset_id'])}</code>
(SHA-256 <code>{html.escape(dataset['sha256'][:16])}…</code>),
{dataset['countries']} стран × {dataset['industries']} отраслей = {dataset['sectors']} секторов,
{dataset['stored_Z']} ненулевых ячеек Z, год {html.escape(str(dataset['years']))},
{html.escape(dataset['units'])}. Baseline выпуск:
{html.escape(_format_value(dataset['total_output']))} {html.escape(dataset['units'])}.</p>

<h2>Сценарии</h2>
<table>
<thead><tr><th>сценарий</th><th>Δвыпуск</th><th>% baseline</th><th>крупнейшая страна</th>
<th>крупнейшая отрасль</th><th>отчёт</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>

<h2>Проверки</h2>
<ul>
<li>сценариев: {index['checks']['scenario_count']}; валидация таблицы:
{'PASS' if index['checks']['all_validations_pass'] else 'FAIL'}; все сценарии допустимы:
{'да' if index['checks']['all_feasible'] else 'нет'};</li>
<li>максимальный материальный остаток сценариев:
{index['checks']['max_abs_material_residual']:.3e}.</li>
</ul>

{sfc_section_html}
<h2>Методика</h2>
<p class="meta">Статическая линейная модель Леонтьева: x = (I−A)<sup>−1</sup>y; сценарий —
x′ = (I−A′)<sup>−1</sup>y′. Сравнительная статика без цен, замещения и динамики; агрегаты по
странам и отраслям, без сырых матриц. Подробности — в docs/131.</p>

<h2>Ограничения</h2>
<ul>
{limitations}
</ul>

<footer>Статус: <strong>{html.escape(STATUS)}</strong>. {html.escape(STATUS_NOTE)}.
Страница статическая: без JavaScript, фреймворков и внешних URL.</footer>
</body>
</html>
"""


def render_scenario_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_outputs(reports: list[dict], index: dict, out_dir, dashboard: bool = True) -> dict:
    """Write every artefact deterministically (UTF-8, LF) and return the written paths."""
    target = Path(out_dir)
    reports_dir = target / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths: dict = {"reports": {}, "dashboard": {}}
    for report in reports:
        scenario_id = report["scenario"]["id"]
        json_path = reports_dir / f"{scenario_id}.json"
        md_path = reports_dir / f"{scenario_id}.md"
        json_path.write_text(render_scenario_json(report), encoding="utf-8", newline="\n")
        md_path.write_text(render_scenario_markdown(report), encoding="utf-8", newline="\n")
        paths["reports"][scenario_id] = {"json": str(json_path), "md": str(md_path)}
    index_path = target / "report_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    paths["index"] = str(index_path)
    if dashboard:
        dashboard_html = target / "dashboard.html"
        dashboard_md = target / "dashboard.md"
        dashboard_html.write_text(render_dashboard_html(index), encoding="utf-8", newline="\n")
        dashboard_md.write_text(render_dashboard_markdown(index), encoding="utf-8", newline="\n")
        paths["dashboard"] = {"html": str(dashboard_html), "md": str(dashboard_md)}
    return paths


# ------------------------------------------------------------------------------------- pipeline
def load_table(root, dataset_id: str):
    """Load the canonical dataset; returns ``(table, dataset_meta)`` (fail-closed).

    Primary path: :func:`amfe.mrio.loaders.load_dataset` (schema v1.0/v1.1 dense document).
    Fallback (documented in docs/131): the working tree may hold a schema-1.1 **sparse** document
    (``Z_sparse`` = rows/cols/values) that the loader of that revision does not expand yet; the
    fallback reads that document read-only, keeps its SHA-256, checks every structural invariant and
    runs the same balance validation.  If both paths refuse, the report is blocked.
    """
    path = Path(root) / str(dataset_id) / "dataset.json"
    primary_error = None
    try:
        loaded = load_dataset(str(dataset_id), Path(root))
        table = wp.table_from_canonical(loaded)
        return table, _dataset_meta(loaded, str(dataset_id), table)
    except MRIOError as exc:
        primary_error = exc
    except (ValueError, TypeError) as exc:  # defensive: a partially written document
        primary_error = exc
    try:
        table, meta = _load_sparse_document(path, str(dataset_id))
    except Exception as fallback_error:  # pragma: no cover - defensive
        raise BlockedData(
            f"loader refused: {primary_error}; sparse-document fallback refused: "
            f"{fallback_error}") from fallback_error
    meta["fallback"] = {
        "reason": f"loader refused the sparse schema-1.1 document: {primary_error}",
        "path": "Z_sparse rows/cols/values (read-only)",
        "loader": "world_reports._load_sparse_document",
    }
    return table, meta


def _sorted_order(countries: list[str], industries: list[str], document_countries,
                  document_industries) -> np.ndarray:
    country_positions = [document_countries.index(name) for name in countries]
    industry_positions = [document_industries.index(name) for name in industries]
    return np.asarray(
        [country_index * len(document_industries) + industry_index
         for country_index in country_positions for industry_index in industry_positions],
        dtype=np.int64)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sparse_document(path: Path, dataset_id: str):
    """Read a schema-1.1 ``Z_sparse`` document without the canonical loader (fail-closed).

    Memory-tuned: the three Z arrays are converted to NumPy one at a time and the Python lists are
    dropped immediately, so the peak stays well inside the 2 GB budget (docs/131 §6).
    """
    if not Path(path).is_file():
        raise ReportError(f"dataset file not found: {path}")
    digest = _file_sha256(path)
    with Path(path).open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ReportError("dataset document must be a JSON object")
    sparse = document.get("Z_sparse")
    if not isinstance(sparse, dict):
        raise ReportError("dataset document has no Z_sparse block")
    try:
        rows = np.asarray(sparse.pop("rows"), dtype=np.int64)
        cols = np.asarray(sparse.pop("cols"), dtype=np.int64)
        values = np.asarray(sparse.pop("values"), dtype=float)
        document_countries = [str(name) for name in document["countries"]]
        document_industries = [str(name) for name in document["industries"]]
        y = np.asarray(document["y"], dtype=float)
        x = np.asarray(document["x"], dtype=float)
        va = np.asarray(document["va"], dtype=float)
        units = str(document.get("units", "USD_mn"))
        tolerance = float(document.get("tolerance", 1e-6))
        years = [int(year) for year in document.get("years", [])]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError(f"malformed sparse dataset document: {exc}") from exc
    n = len(document_countries) * len(document_industries)
    if not document_countries or not document_industries:
        raise ReportError("sparse document has empty countries/industries")
    if len(set(document_countries)) != len(document_countries) \
            or len(set(document_industries)) != len(document_industries):
        raise ReportError("sparse document has duplicate country/industry labels")
    if y.size != n or x.size != n or va.size != n:
        raise ReportError(f"vector length mismatch: sectors {n}, y {y.size}, x {x.size}, va {va.size}")
    if rows.size != cols.size or rows.size != values.size:
        raise ReportError("Z_sparse rows/cols/values must have equal length")
    if rows.size and (int(rows.min()) < 0 or int(cols.min()) < 0
                      or int(rows.max()) >= n or int(cols.max()) >= n):
        raise ReportError("Z_sparse indices are outside 0..sectors-1")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(y)) \
            or not np.all(np.isfinite(x)) or not np.all(np.isfinite(va)):
        raise ReportError("sparse document contains non-finite values")
    if values.size and float(values.min()) < -tolerance:
        raise ReportError("sparse document has a negative Z cell")
    Z = sp.csr_matrix((values, (rows, cols)), shape=(n, n))
    Z.sum_duplicates()
    Z.sort_indices()
    del rows, cols, values, sparse, document
    gc.collect()
    countries = tuple(sorted(document_countries))
    industries = tuple(sorted(document_industries))
    order = _sorted_order(list(countries), list(industries), document_countries,
                          document_industries)
    Z = Z[order][:, order].tocsr()
    Z.sum_duplicates()
    Z.sort_indices()
    provenance = {
        "name": Path(path).name,
        "dataset_sha256": digest,
        "path": str(path),
        "years": years,
        "source": "Eurostat FIGARO (sparse schema-1.1 document)",
        "synthetic": False,
    }
    table = wp.WorldTable(countries=countries, industries=industries,
                          Z=Z, y=y[order], x=x[order], va=va[order],
                          units=units, tolerance=tolerance, provenance=provenance)
    meta = {
        "dataset_id": dataset_id,
        "sha256": digest,
        "bytes": int(Path(path).stat().st_size),
        "schema": "amfe.mrio.dataset.v1.1",
        "countries": table.n_countries,
        "industries": table.n_industries,
        "sectors": table.n,
        "cells_Z": table.n * table.n,
        "stored_Z": int(Z.nnz),
        "stored_share": round(float(Z.nnz) / float(table.n * table.n), 6),
        "years": years,
        "units": units,
        "total_output": round(float(table.x.sum()), 3),
        "total_final_demand": round(float(table.y.sum()), 3),
        "total_value_added": round(float(table.va.sum()), 3),
        "source": "Eurostat FIGARO",
        "provenance_note": "third-party FIGARO data; read-only, never committed",
    }
    return table, meta


def build_reports(table: wp.WorldTable, specs: list[dict], dataset_meta: dict,
                  workers: int = 1, sfc_block: dict | None = None) -> tuple[list[dict], dict, dict]:
    """Validate, solve the baseline, run every scenario and assemble the reports."""
    _validate_specs_against(table, specs)
    validation = wp.validate_table(table, workers=workers)
    core = wp.country_core(table)
    solver = wp.WorldSolver(table)
    baseline = _baseline_block(table, solver)
    outputs = run_scenarios(table, solver, specs)
    reports = [build_scenario_report(table, solver, spec, outputs[spec["id"]],
                                     dataset_meta, baseline, validation, core,
                                     sfc_block=sfc_block)
               for spec in specs]
    index = build_index(reports, dataset_meta, 0.0, 0.0, Path("."))
    return reports, index, {"validation": validation, "core": core, "baseline": baseline}


def run(args) -> dict:
    """Full CLI action: load -> scenarios -> reports -> index -> dashboard."""
    started = time.perf_counter()
    out_dir = Path(getattr(args, "out", DEFAULT_OUT))
    explicit_energy = parse_list(getattr(args, "energy_industries", None))
    explicit_fx = parse_list(getattr(args, "fx_countries", None))
    if getattr(args, "synthetic", False):
        table = wp.synthetic_table(int(getattr(args, "countries", 60)),
                                   int(getattr(args, "industries", 45)),
                                   int(getattr(args, "seed", 20261114)),
                                   int(getattr(args, "foreign_entries", 8)), workers=1)
        dataset_meta = {
            "dataset_id": "synthetic", "sha256": "-", "bytes": 0,
            "schema": "amfe.mrio.dataset.v1",
            "countries": table.n_countries, "industries": table.n_industries,
            "sectors": table.n, "cells_Z": table.n * table.n, "stored_Z": int(table.Z.nnz),
            "stored_share": round(float(table.Z.nnz) / float(table.n * table.n), 6),
            "years": [2020], "units": table.units,
            "total_output": round(float(table.x.sum()), 3),
            "total_final_demand": round(float(table.y.sum()), 3),
            "total_value_added": round(float(table.va.sum()), 3),
            "source": "synthetic", "provenance_note": "deterministic synthetic table",
        }
        energy_industries = explicit_energy or tuple(table.industries[:3])
        fx_countries = explicit_fx or tuple(table.countries[:2])
    else:
        root = getattr(args, "root", None) or DEFAULT_ROOT
        dataset_id = getattr(args, "dataset", None) or DEFAULT_DATASET
        table, dataset_meta = load_table(root, str(dataset_id))
        gc.collect()  # release the dense canonical document before the factorisations
        energy_industries = explicit_energy or DEFAULT_ENERGY_INDUSTRIES
        fx_countries = explicit_fx or DEFAULT_FX_COUNTRIES
    specs = scenario_specs(
        energy_shares=parse_list(getattr(args, "energy_share", None), float) or (0.05,),
        energy_industries=energy_industries,
        import_shares=parse_list(getattr(args, "import_shares", None), float)
        or DEFAULT_IMPORT_SHARES,
        fx_countries=fx_countries,
        fx_shares=parse_list(getattr(args, "fx_shares", None), float) or DEFAULT_FX_SHARES,
    )
    sfc_block = None
    if getattr(args, "sfc", False):
        sfc_block = build_sfc_block(
            table,
            investment_share=float(getattr(args, "sfc_investment_share",
                                           DEFAULT_SFC_INVESTMENT_SHARE)),
            out_dir=out_dir)
    reports, index, _context = build_reports(table, specs, dataset_meta,
                                             workers=int(getattr(args, "workers", 1) or 1),
                                             sfc_block=sfc_block)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    index = build_index(reports, dataset_meta, elapsed_ms, wp.peak_memory_mb(), out_dir,
                        sfc_block=sfc_block)
    labor_block = None
    if getattr(args, "labor", False):
        from amfe.world import labor as labor_layer
        labor_root = Path(getattr(args, "labor_root", None)
                          or os.environ.get("AMFE_MRIO_ROOT", "")
                          or DEFAULT_ROOT) / "labor"
        labor_data = labor_layer.load_labor(
            path=labor_root / "sea_normalized.json")
        years = list(dataset_meta.get("years") or [labor_data.years[-1]])
        requested = int(years[0]) if years else labor_data.years[-1]
        year = requested if requested in labor_data.years else labor_data.years[-1]
        labor_block = labor_layer.build_labor_block(
            labor_data, year,
            io_dataset={"countries": table.countries, "industries": table.industries,
                        "x": [float(value) for value in table.x], "units": table.units})
        labor_block["dataset_year"] = requested
        labor_block["year_used"] = year
        labor_block["year_note"] = (None if year == requested else
                                    f"labor data covers {labor_data.years[0]}-{labor_data.years[-1]}; "
                                    f"used {year}")
        index["labor"] = labor_block
    paths = write_outputs(reports, index, out_dir,
                          dashboard=not getattr(args, "no_dashboard", False))
    if labor_block is not None:
        labor_path = out_dir / "labor.json"
        labor_path.write_text(json.dumps(labor_block, ensure_ascii=False,
                                         sort_keys=True, indent=1) + "\n", encoding="utf-8",
                              newline="\n")
        paths["labor"] = {"json": str(labor_path)}
    return {"index": index, "reports": reports, "paths": paths}


def _print_summary(result: dict) -> None:
    index = result["index"]
    dataset = index["dataset"]
    print(f"Сценарные отчёты мира — статус {index['status']} (исследовательский, не допуск)")
    print(f"  данные: {dataset['dataset_id']}  {dataset['countries']}x{dataset['industries']} "
          f"= {dataset['sectors']} секторов, Z {dataset['stored_Z']} ненулевых; "
          f"baseline={index['dataset']['total_output']:,.1f} {dataset['units']}")
    for entry in index["scenarios"]:
        headline = entry["headline"]
        print(f"  {entry['id']:<22} дельта выпуска {headline['total_delta_output']:>15,.3f}  "
              f"({(headline['relative_delta_output'] or 0) * 100:+.4f}%)  "
              f"страна {headline['largest_country']}, отрасль {headline['largest_industry']}")
    print(f"  проверки: валидация {'PASS' if index['checks']['all_validations_pass'] else 'FAIL'}, "
          f"max остаток {index['checks']['max_abs_material_residual']:.3e}")
    sfc = index.get("sfc")
    if sfc:
        totals = sfc["totals"]
        print(f"  SFC (--sfc): S={totals['savings']:,.1f}, I={totals['investment']:,.1f}, "
              f"CA={totals['current_account']:,.1f} {sfc.get('units', '')}; "
              f"sumCA={sfc['checks']['global_current_account_sum']:+.3e}; "
              f"S=I+CA max={sfc['checks']['saving_investment_identity_max_abs']:.3e}; "
              f"SFC {'PASS' if sfc['checks']['sfc_pass'] else 'FAIL'}")
    print(f"  время {index['timings_ms']['total']:.0f} мс, пик {index['timings_ms']['peak_memory_mb']} МБ")
    labor = index.get("labor")
    if labor:
        top = labor.get("top_countries_by_labor_share") or []
        print(f"  труд (--labor): labor share ВВП {labor['labor_share_of_gdp']:.4f} "
              f"(с самозанятыми {labor['labor_share_of_gdp_incl_self_employed']:.4f}), "
              f"занятость {labor['world']['persons_engaged_million']:,.1f} млн; "
              f"топ: {top[0]['country']} {top[0]['labor_share']:.3f}"
              if top else f"  труд (--labor): labor share {labor['labor_share_of_gdp']}")
    print(f"  индекс: {result['paths']['index']}")
    print(f"  дашборд: {result['paths']['dashboard']['html']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2 scenario reports on a real MRIO dataset (docs/131)")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="MRIO data root")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="canonical dataset id")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (build/world)")
    parser.add_argument("--energy-share", dest="energy_share", default="0.05",
                        help="energy shock size(s), comma-separated: 0.05 = -5%% demand, "
                             "-0.50 = +50%% demand (default 0.05)")
    parser.add_argument("--energy-industries", dest="energy_industries", default=None,
                        help="comma-separated energy industries (default CPA_B,CPA_C19,CPA_D35)")
    parser.add_argument("--import-shares", dest="import_shares", default=None,
                        help="comma-separated import-restriction sizes (default 0.10,0.20)")
    parser.add_argument("--fx-countries", dest="fx_countries", default=None,
                        help="comma-separated FX countries (default DE,FR)")
    parser.add_argument("--fx-shares", dest="fx_shares", default=None,
                        help="comma-separated depreciation sizes (default 0.10,0.20)")
    parser.add_argument("--workers", type=int, default=1, help="validation worker count")
    parser.add_argument("--synthetic", action="store_true",
                        help="use the synthetic table (tests/CI; no real data)")
    parser.add_argument("--countries", type=int, default=60)
    parser.add_argument("--industries", type=int, default=45)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--foreign-entries", dest="foreign_entries", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="print the report index as JSON")
    parser.add_argument("--labor", action="store_true",
                        help="add the labor/household block from the WIOD SEA layer (docs/136)")
    parser.add_argument("--labor-root", dest="labor_root", default=None,
                        help="MRIO data root holding labor/sea_normalized.json "
                             "(default $AMFE_MRIO_ROOT or --root)")
    parser.add_argument("--no-dashboard", dest="no_dashboard", action="store_true",
                        help="write reports and the index but skip the dashboard")
    parser.add_argument("--sfc", action="store_true",
                        help="include the SFC <-> IO accounting block (S = I + CA, sum CA = 0, "
                             "net foreign asset step) in the reports, index and dashboard "
                             "(docs/131, docs/133)")
    parser.add_argument("--sfc-investment-share", dest="sfc_investment_share", type=float,
                        default=DEFAULT_SFC_INVESTMENT_SHARE,
                        help="investment share of absorption for the S/I split (default 0.2; "
                             "S = I + CA holds for any share)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except BlockedData as exc:
        print(f"BLOCKED_DATA: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except (ReportError, wp.PipelineError, MRIOError, SFCIOError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.no_dashboard:
        pass  # write_outputs already skipped the dashboard artefacts
    if args.json:
        print(json.dumps(result["index"], ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
