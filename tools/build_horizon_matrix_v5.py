#!/usr/bin/env python3
"""Build the unified ``target x horizon x candidate`` horizon matrix (protocol v5).

What this tool does
------------------
It reads *existing* evaluation reports -- the v5 runs (``protocols/horizon_v5/``,
``protocols/first_real_evaluation_report_v5.json``, ``protocols/prereg_eval_*_v5.json``) and the
earlier v4-era ``*_v2.json`` reports (including ``protocols/extended_pass/``) -- and writes one
machine-readable matrix ``protocols/horizon_matrix_v5.json`` over the **full** grid of protocol
targets x protocol horizons x registered candidates.  It does not evaluate anything itself, does not
read raw data and stores **only aggregates** (relative losses, thresholds, p-values, criterion
statuses) and SHA-256 digests of the source reports -- never forecasts or actual values (docs/84,
D-147).

Status vocabulary (honest, documented in docs/176):
  * ``CANDIDATE``    -- the pair was evaluated; the harness stage is CANDIDATE (no admission) and no
                        decisive negative evidence was found;
  * ``FAIL``         -- the pair was evaluated and the candidate is worse than the comparator on
                        every window/loss (C1 relative loss > 1.0) while C4 also fails on every
                        applicable window; substantive underperformance, still no admission;
  * ``INCONCLUSIVE`` -- the pair was evaluated but cannot answer its question: the SPF member is
                        structurally absent (candidate is bit-identical to the control) or the
                        window was reduced below the protocol size;
  * ``NOT_RUN``      -- no evaluation exists for the pair; the reason names the blocker.

Every cell of the grid is a row: a missing evaluation is an explicit ``NOT_RUN`` row, never a silent
hole.  A v5 report supersedes a v4-era report for the same cell.

Usage
-----
    python tools/build_horizon_matrix_v5.py
    python tools/build_horizon_matrix_v5.py --check      # validate the existing matrix, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_V5 = ROOT / "protocols" / "evaluation_protocol_v5.json"
PROTOCOL_V4 = ROOT / "protocols" / "evaluation_protocol_v4.json"
CANDIDATES_DOC = ROOT / "protocols" / "candidates_2026-11-11.json"
NEW_RUN_DIR = ROOT / "protocols" / "horizon_v5"
EXTENDED_PASS = ROOT / "protocols" / "extended_pass"
V5_REFERENCE = ROOT / "protocols" / "first_real_evaluation_report_v5.json"
HORIZONS_SUMMARY = ROOT / "protocols" / "ensemble_horizons_report.json"
T3_SUMMARY = ROOT / "protocols" / "ensemble_t3_report.json"
SPF_SUMMARY = ROOT / "protocols" / "spf_t2_sensitivity_report.json"
OUT = ROOT / "protocols" / "horizon_matrix_v5.json"

STATUSES = ("CANDIDATE", "FAIL", "INCONCLUSIVE", "NOT_RUN")
CRITERIA = ("C1", "C2", "C4", "C5")
CRITERION_VOCAB = ("pass", "fail", "not_applicable", "not_run")
REQUIRED_FIELDS = ("target", "horizon", "candidate", "protocol", "data_class", "origins",
                   "criteria", "metrics", "overall_status", "reason", "sources")
# keys that would indicate raw forecast/actual values leaking into the matrix
FORBIDDEN_KEYS = {"actual_primary", "actual_secondary", "candidate", "comparator", "quantiles",
                  "probability", "point", "used_origins", "vintage_dates", "recession",
                  "member_forecasts", "actual_values", "forecast_values"}

# T1 h=12 window build costs depend on the candidate.  Measured in task-58 on 2026-11-18:
# ENSEMBLE_RW_AR -- 1690 cache entries (5 per origin: candidate + four members) for 338 origins,
# 0 hits, 13m51s => 2.5 s/origin; an 8-origin warm benchmark gave 3.4 s/origin.  ENSEMBLE_BAYES --
# 255 entries in 5m19s (~51 origins, ~6 s/origin) before it was stopped; its candidate forecast
# alone measured 8 s/origin, so the full pair projects to ~35-45 min.  The pass evaluated four of
# the five candidates, so the cell-level reason names the one left out.
NOT_RUN_REASONS = {
    "T1_H12_OTHER": ("в пассе 2026-11-18 (task-58, REAL_TIME_RECONSTRUCTED, protocol v5) T1 h=12 "
                     "оценены {evaluated}; этот кандидат в пасс не входил: постройка окон "
                     "W1_EXT+W2 (338 начал) стоит ~2,5–3,4 с на начало для дешёвого кандидата "
                     "(ENSEMBLE_RW_AR: 1690 записей кэша, 0 попаданий, 13 мин 51 с на весь прогон) "
                     "и ~8 с на начало только на прогноз кандидата ENSEMBLE_BAYES (255 записей за "
                     "~5 мин до останова, ~6 с на начало в целом), то есть ~35–45 мин на пару. "
                     "Тёплого кэша не было: build/eval_cache и .tmp-test/cache_v5_clean непригодны "
                     "(schema 1.0.0 или чужой fingerprint — source_files 16 против 17, "
                     "source_sha256 не совпадает), поэтому каждый кандидат строился с нуля. Это "
                     "пропуск при time-box 25–30 мин, а не «эффекта нет»."),
    "T1_H1_RW_AR": ("ENSEMBLE_RW_AR зарегистрирован только для T1, но в T1 h=1 пасс (docs/87, v2) "
                    "и в v5-референс (docs/107) не включён: пара не оценивалась."),
    "T1_H3_H6": ("на T1 h=3/6 переоценивались только ENSEMBLE_ADAPTIVE и ENSEMBLE_EQUAL "
                 "(docs/102); остальные кандидаты на этих горизонтах не строились."),
    "T2_H1_H4_SPF": ("SPF-медиана не допускается на квартальных началах h=1/2/4 "
                     "(origin_policy.spf_alignment, spf_available=false): кандидат вырождается в "
                     "контроль ENSEMBLE_T2_NOT_SPF, отдельный прогон не проводился (docs/95)."),
    "T2_H1_H4_CONTROL": ("контроль SPF-пары ENSEMBLE_T2_NOT_SPF на h>=1 отдельно не оценивался: "
                         "T2 h=1/2/4 пасс покрывает только ENSEMBLE_ADAPTIVE/EQUAL (docs/102)."),
    "T2_H1_H4_OTHER": ("на T2 h=1/2/4 переоценивались только ENSEMBLE_ADAPTIVE и ENSEMBLE_EQUAL "
                       "(docs/102); этот кандидат на этих горизонтах не строился."),
    "T3_OTHER": ("T3 оценён только для ENSEMBLE_ADAPTIVE и ENSEMBLE_EQUAL (docs/109, протокол "
                 "v4); v5-перепрогон в task-58 не выполнялся: замер на data/raw (REAL_TIME, h=3, "
                 "ENSEMBLE_ADAPTIVE, 4 начала) — 7,3 с на начало без кэша; окна W1+W2 у h=3 — "
                 "271 начало, то есть ~33 мин на кандидата и ~66 мин на двух — больше time-box "
                 "25–30 мин. v5 не меняет C1/C2/C5 для T3, а C4 на W1_EXT для T3 не определён, "
                 "поэтому строки остаются v4-эпохи (docs/176, §4.4)."),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:  # pragma: no cover - outside the repo
        return str(path)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(value) -> str:
    return f"{float(value):.4f}"


def _min_adjusted_p(window_block: dict) -> float | None:
    values = []
    for block in (window_block or {}).values():
        if isinstance(block, dict) and block.get("adjusted_p_value") is not None:
            values.append(float(block["adjusted_p_value"]))
    return min(values) if values else None


def protocol_targets() -> dict[str, list[int]]:
    """{target: horizons} straight from protocol v5 (no hand-kept grid)."""
    out = {}
    for target in _load(PROTOCOL_V5).get("targets") or []:
        horizons = target.get("horizons_months") or target.get("horizons_quarters") or []
        out[target["id"]] = [int(h) for h in horizons]
    return out


def registered_candidates() -> list[dict]:
    return [{"candidate": item["id"], "targets": list(item.get("targets") or []),
             "role": item.get("role") or ("control" if "EQUAL" in item["id"] else "candidate")}
            for item in _load(CANDIDATES_DOC).get("candidates") or []]


def extract_metrics(report: dict) -> dict:
    """Aggregates only: C1 relative losses, C2 adjusted p, C4/C5 statuses."""
    admission = report.get("admission") or {}
    criteria = admission.get("criteria") or {}
    out: dict = {"stage": admission.get("status"),
                 "data_class_note": admission.get("data_class_note")}
    c1 = criteria.get("C1") or {}
    c1_windows = {}
    for window, losses in (c1.get("windows") or {}).items():
        c1_windows[window] = {}
        for loss, entry in (losses or {}).items():
            if not isinstance(entry, dict):
                continue
            c1_windows[window][loss] = {
                "relative_loss": entry.get("relative_loss"),
                "threshold": entry.get("threshold"),
                "pass": bool(entry.get("pass")),
            }
    out["C1"] = {"pass": bool(c1.get("pass")), "windows": c1_windows}
    c2 = criteria.get("C2") or {}
    c2_windows = {window: {"adjusted_p_min": _min_adjusted_p(block)}
                  for window, block in (c2.get("windows") or {}).items()}
    out["C2"] = {"pass": bool(c2.get("pass")), "windows": c2_windows}
    c4 = criteria.get("C4") or {}
    c4_windows = {}
    for window, losses in (c4.get("per_window") or {}).items():
        c4_windows[window] = {}
        for loss, entry in (losses or {}).items():
            if isinstance(entry, dict):
                c4_windows[window][loss] = {
                    "status": entry.get("status"),
                    "relative_loss_recession": entry.get("relative_loss_recession"),
                    "recession_origins": entry.get("recession_origins"),
                    "min_recession_origins": entry.get("min_recession_origins"),
                    "pass": bool(entry.get("pass")),
                }
    out["C4"] = {"applies": c4.get("applies"), "pass": c4.get("pass"),
                 "windows": c4_windows, "extended": c4.get("extended")}
    c5 = criteria.get("C5") or {}
    c5_windows = {}
    for window, losses in (c5.get("windows") or {}).items():
        c5_windows[window] = {loss: {"p_value": entry.get("p_value"),
                                     "pass": bool(entry.get("pass"))}
                              for loss, entry in (losses or {}).items()
                              if isinstance(entry, dict)}
    out["C5"] = {"pass": bool(c5.get("pass")), "windows": c5_windows}
    c6 = criteria.get("C6") or {}
    out["C6"] = {"status": c6.get("status", "NOT_EVALUATED"), "pass": bool(c6.get("pass"))}
    return out


def origins_of(report: dict) -> dict:
    windows = report.get("windows") or {}
    out = {}
    for name, block in windows.items():
        if isinstance(block, dict) and block.get("origins") is not None:
            out[name] = int(block["origins"])
    return out


def criteria_of(metrics: dict) -> dict:
    c1 = metrics.get("C1") or {}
    c2 = metrics.get("C2") or {}
    c4 = metrics.get("C4") or {}
    c5 = metrics.get("C5") or {}
    return {
        "C1": "pass" if c1.get("pass") else "fail",
        "C2": "pass" if c2.get("pass") else "fail",
        "C4": ("pass" if c4.get("pass") else "fail") if c4.get("applies")
              else "not_applicable",
        "C5": "pass" if c5.get("pass") else "fail",
    }


def _worse_everywhere(metrics: dict) -> bool:
    rel = []
    for window in (metrics.get("C1", {}).get("windows") or {}).values():
        for loss in window.values():
            if loss.get("relative_loss") is not None:
                rel.append(float(loss["relative_loss"]))
    return bool(rel) and all(value > 1.0 for value in rel)


def _c4_fail_everywhere(metrics: dict) -> bool:
    c4 = metrics.get("C4") or {}
    if not c4.get("applies"):
        return False
    statuses = [entry.get("status") for window in (c4.get("windows") or {}).values()
                for entry in window.values() if entry.get("status")]
    return bool(statuses) and all(status == "FAIL" for status in statuses)


def describe(metrics: dict, spf_fallback: bool = False,
             reduced: bool = False) -> tuple[str, str]:
    """Return (overall_status, reason) from the extracted aggregates (docs/176 §2)."""
    c1 = metrics.get("C1") or {}
    c2 = metrics.get("C2") or {}
    c4 = metrics.get("C4") or {}
    c5 = metrics.get("C5") or {}
    rel = []
    for window, losses in (c1.get("windows") or {}).items():
        for loss, entry in losses.items():
            if entry.get("relative_loss") is not None:
                rel.append(f"{window}/{loss} {_pct(entry['relative_loss'])}")
    p2 = []
    for window, entry in (c2.get("windows") or {}).items():
        if entry.get("adjusted_p_min") is not None:
            p2.append(f"{window} {_pct(entry['adjusted_p_min'])}")
    c4bits = []
    for window, losses in (c4.get("windows") or {}).items():
        statuses = sorted({str(entry.get("status")) for entry in losses.values()})
        if statuses:
            c4bits.append(f"{window} {'/'.join(statuses)}")
    parts = [f"C1 {'pass' if c1.get('pass') else 'fail'} ({', '.join(rel) or 'n/a'})",
             f"C2 {'pass' if c2.get('pass') else 'fail'} (RW adj p {', '.join(p2) or 'n/a'})"]
    if c4.get("applies"):
        parts.append(f"C4 {'pass' if c4.get('pass') else 'fail'} ({', '.join(c4bits) or 'n/a'})")
    else:
        parts.append("C4 не применим по протоколу")
    parts.append(f"C5 {'pass' if c5.get('pass') else 'fail'}")
    if spf_fallback:
        return ("INCONCLUSIVE",
                "SPF-член не допускается на квартальных началах h=1/2/4 (spf_available=false, "
                "fallback на контроль): кандидат побитово равен ENSEMBLE_T2_NOT_SPF, сравнение не "
                "несёт информации о SPF-добавке. " + "; ".join(parts))
    if reduced:
        return ("INCONCLUSIVE",
                "Окно сокращено относительно протокола (ограничение прогона, не результат): "
                + "; ".join(parts))
    if _worse_everywhere(metrics) and _c4_fail_everywhere(metrics):
        return ("FAIL", "Кандидат хуже компаратора на всех окнах/потерях C1 и C4 FAIL: "
                + "; ".join(parts))
    return ("CANDIDATE", "Оценка выполнена, стадия harness CANDIDATE (допуск не присвоен): "
            + "; ".join(parts))


def _protocol_label(report: dict) -> str:
    version = (report.get("protocol") or {}).get("version")
    return f"v{version}" if version else "unknown"


def _report_index() -> dict[tuple[str, int, str], dict]:
    """(target, horizon, candidate) -> {'path', 'label', 'generated_at'} ; v5 supersedes v4-era."""
    fresh = [V5_REFERENCE] + sorted((ROOT / "protocols").glob("prereg_eval_*_v5.json"))
    if NEW_RUN_DIR.exists():
        fresh += sorted(NEW_RUN_DIR.glob("*.json"))
    legacy = sorted((ROOT / "protocols").glob("ensemble_eval_*_v2.json")) \
        + sorted(EXTENDED_PASS.glob("ensemble_eval_*_v2.json"))
    index: dict[tuple[str, int, str], dict] = {}
    for path in fresh + legacy:
        if not path.exists():
            continue
        report = _load(path)
        if not {"target", "horizon", "candidate"} <= set(report):
            continue
        key = (report["target"], int(report["horizon"]), report["candidate"])
        entry = {"path": path, "label": _protocol_label(report),
                 "generated_at": str(report.get("generated_at") or ""),
                 "v5": _protocol_label(report).startswith("v5"),
                 # v5 requires the T1 C4 on W1_EXT; when two v5 reports exist for the same cell
                 # (an extended-C4 run and a REAL_TIME-only run), the extended one is the
                 # protocol-conforming source.  The preference is deterministic and does not
                 # depend on which file happened to be written last.
                 "extended": bool(report.get("conditional_data_class"))}
        current = index.get(key)
        if current is None:
            index[key] = entry
            continue
        # v5 always wins; then the extended-C4 v5 report; otherwise the newest report wins
        if ((entry["v5"], entry["extended"], entry["generated_at"])
                >= (current["v5"], current["extended"], current["generated_at"])):
            index[key] = entry
    return index


def row_from_report(path: Path, *, spf_fallback: bool = False, reduced: bool = False,
                    reason_prefix: str | None = None) -> dict:
    report = _load(path)
    metrics = extract_metrics(report)
    status, reason = describe(metrics, spf_fallback=spf_fallback, reduced=reduced)
    if reason_prefix:
        reason = f"{reason_prefix} {reason}"
    return {
        "target": report["target"],
        "horizon": int(report["horizon"]),
        "candidate": report["candidate"],
        "protocol": _protocol_label(report),
        "data_class": report.get("data_class"),
        "origins": origins_of(report),
        "criteria": criteria_of(metrics),
        "metrics": metrics,
        "overall_status": status,
                "status": status,
        "reason": reason,
        "sources": [{"file": _rel(path), "sha256": sha(path)}],
    }


def _not_run_reason(target: str, horizon: int, candidate: str,
                    siblings: tuple[str, ...] = ()) -> str:
    if target == "T1_CPI_YOY" and horizon == 12:
        evaluated = ", ".join(f"`{name}`" for name in siblings) or "ни один кандидат"
        return NOT_RUN_REASONS["T1_H12_OTHER"].format(evaluated=evaluated)
    if target == "T1_CPI_YOY" and candidate == "ENSEMBLE_RW_AR":
        return NOT_RUN_REASONS["T1_H1_RW_AR"]
    if target == "T1_CPI_YOY":
        return NOT_RUN_REASONS["T1_H3_H6"]
    if target == "T2_GDP_GROWTH" and candidate == "ENSEMBLE_T2_SPF":
        return NOT_RUN_REASONS["T2_H1_H4_SPF"]
    if target == "T2_GDP_GROWTH" and candidate == "ENSEMBLE_T2_NOT_SPF":
        return NOT_RUN_REASONS["T2_H1_H4_CONTROL"]
    if target == "T2_GDP_GROWTH":
        return NOT_RUN_REASONS["T2_H1_H4_OTHER"]
    return NOT_RUN_REASONS["T3_OTHER"]


def _gap_sources(target: str) -> list[dict]:
    paths = [CANDIDATES_DOC]
    if target == "T1_CPI_YOY":
        paths.append(HORIZONS_SUMMARY)
    elif target == "T2_GDP_GROWTH":
        paths.append(SPF_SUMMARY)
    else:
        paths.append(T3_SUMMARY)
    return [{"file": _rel(path), "sha256": sha(path)} for path in paths if path.exists()]


def _not_run_row(target: str, horizon: int, candidate: str, protocol: str,
                 siblings: tuple[str, ...] = ()) -> dict:
    return {
        "target": target,
        "horizon": horizon,
        "candidate": candidate,
        "protocol": protocol,
        "data_class": None,
        "origins": {},
        "criteria": {name: "not_run" for name in CRITERIA},
        "metrics": {},
        "overall_status": "NOT_RUN",
        "status": "NOT_RUN",
        "reason": _not_run_reason(target, horizon, candidate, siblings),
        "sources": _gap_sources(target),
    }


def _gaps(rows: list[dict]) -> list[dict]:
    """v5 reruns deliberately not performed, with the measured cost that blocked them (§4.5)."""
    t1 = [row for row in rows if row["target"] == "T1_CPI_YOY" and row["horizon"] == 12]
    missing = sorted(row["candidate"] for row in t1 if row["overall_status"] == "NOT_RUN")
    done = sorted(row["candidate"] for row in t1 if row["overall_status"] != "NOT_RUN")
    gaps = []
    if missing:
        gaps.append({
            "scope": "T1_CPI_YOY h=12, " + " и ".join(missing),
            "status": "NOT_RUN",
            "reason": NOT_RUN_REASONS["T1_H12_OTHER"].format(
                evaluated=", ".join(f"`{name}`" for name in done) or "ни один кандидат"),
        })
    gaps.append({"scope": "T2_GDP_GROWTH h=1/2/4, ENSEMBLE_T2_SPF", "status": "NOT_RUN",
                 "reason": NOT_RUN_REASONS["T2_H1_H4_SPF"]})
    gaps.append({"scope": "T3_RECESSION h=6/12, v5-перепрогон", "status": "NOT_RUN",
                 "reason": ("T3 h=3 ENSEMBLE_ADAPTIVE уже переоценён под v5 "
                            "(C1/C2 fail, C4 not_applicable, C5 pass; отчёт "
                            "protocols/horizon_v5/T3_h3_ENSEMBLE_ADAPTIVE.json). "
                            "Остаются h=6/12; v5 не меняет C1/C2/C5 для T3, "
                            "C4 на W1_EXT для T3 не определён, поэтому строки "
                            "h=6/12 остаются v4-эпохи (docs/176, §4.4; docs/109).")})
    return gaps


def build() -> dict:
    protocol_v5 = _load(PROTOCOL_V5)
    targets = protocol_targets()
    index = _report_index()
    rows = []
    for target, horizons in targets.items():
        for horizon in horizons:
            for candidate in registered_candidates():
                if target not in candidate["targets"]:
                    continue
                entry = index.get((target, horizon, candidate["candidate"]))
                if entry is None:
                    siblings = tuple(sorted({cand for (tgt, hor, cand) in index
                                             if tgt == target and hor == horizon}))
                    rows.append(_not_run_row(target, horizon, candidate["candidate"], "v4.0.0",
                                             siblings))
                    continue
                spf_fallback = (target == "T2_GDP_GROWTH" and horizon in (1, 2, 4)
                                and candidate["candidate"] == "ENSEMBLE_T2_SPF")
                rows.append(row_from_report(entry["path"], spf_fallback=spf_fallback))
    rows.sort(key=lambda r: (r["target"], r["horizon"], r["candidate"], r["protocol"]))
    counts = {status: sum(1 for row in rows if row["overall_status"] == status)
              for status in STATUSES}
    coverage = {
        "rows": len(rows),
        "evaluated": len(rows) - counts["NOT_RUN"],
        "not_run": counts["NOT_RUN"],
        "status_counts": counts,
        "cells": {target: {str(horizon): sorted(
            row["candidate"] for row in rows
            if row["target"] == target and row["horizon"] == horizon)
            for horizon in horizons}
            for target, horizons in targets.items()},
        "evaluated_cells": {target: {str(horizon): sorted(
            row["candidate"] for row in rows
            if row["target"] == target and row["horizon"] == horizon
            and row["overall_status"] != "NOT_RUN")
            for horizon in horizons}
            for target, horizons in targets.items()},
    }
    return {
        "schema_version": "1.0.0",
        "matrix_id": "AMFE-HORIZON-MATRIX-V5",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "protocol": {"id": protocol_v5.get("protocol_id"),
                     "version": protocol_v5.get("version"),
                     "file": "protocols/evaluation_protocol_v5.json",
                     "sha256": sha(PROTOCOL_V5)},
        "legacy_protocol": {"file": "protocols/evaluation_protocol_v4.json",
                            "version": _load(PROTOCOL_V4).get("version"),
                            "sha256": sha(PROTOCOL_V4)},
        "status_rule": {
            "CANDIDATE": "оценка выполнена, стадия harness CANDIDATE, допуск не присвоен",
            "FAIL": "кандидат хуже компаратора по C1 на всех окнах/потерях и C4 FAIL на всех окнах",
            "INCONCLUSIVE": "пара не отвечает на свой вопрос (SPF-член недоступен / окно сокращено)",
            "NOT_RUN": "оценки нет; причина указана в reason",
        },
        "criteria_rule": {
            "C1": "относительная потеря <= порога NF-SIM",
            "C2": "Romano-Wolf adjusted p < 0.10 на W1 и W2",
            "C4": "условный GW на рецессионных месяцах, минимум 12 рецессионных начал; "
                  "not_applicable — критерий не применяется к цели",
            "C5": "p >= 0.10 (не хуже) на W1 и W2",
        },
        "rows": rows,
        "v5_reruns_not_performed": _gaps(rows),
        "coverage": coverage,
        "licence_note": ("forecasts and actual values are not stored: only relative losses, "
                         "thresholds, p-values and digests of the source reports (docs/84, D-147)"),
        "reporting_only": "harness statuses only; this matrix assigns no admission",
    }


def check(path: Path = OUT) -> list[str]:
    problems = []
    if not path.exists():
        return [f"matrix not found: {path}"]
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0.0":
        problems.append("schema_version != 1.0.0")
    if not data.get("rows"):
        problems.append("no rows")
    for index, row in enumerate(data.get("rows") or []):
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            problems.append(f"row {index}: missing {missing}")
        if row.get("overall_status") not in STATUSES:
            problems.append(f"row {index}: bad overall_status {row.get('overall_status')!r}")
        if not row.get("reason"):
            problems.append(f"row {index}: empty reason")
        if not row.get("sources"):
            problems.append(f"row {index}: no sources")
        criteria = row.get("criteria") or {}
        if sorted(criteria) != sorted(CRITERIA):
            problems.append(f"row {index}: criteria keys {sorted(criteria)}")
        for name, value in criteria.items():
            if value not in CRITERION_VOCAB:
                problems.append(f"row {index}: bad criterion {name}={value!r}")
        for key in FORBIDDEN_KEYS:
            if key in (row.get("metrics") or {}):
                problems.append(f"row {index}: raw key {key!r} in metrics")
        for source in row.get("sources") or []:
            source_path = ROOT / source.get("file", "")
            if not source_path.exists():
                problems.append(f"row {index}: missing source {source.get('file')}")
            elif sha(source_path) != source.get("sha256"):
                problems.append(f"row {index}: digest mismatch {source.get('file')}")
    coverage = data.get("coverage") or {}
    if coverage.get("rows") != len(data.get("rows") or []):
        problems.append("coverage.rows != len(rows)")
    counted = {status: sum(1 for row in data.get("rows") or []
                           if row.get("overall_status") == status) for status in STATUSES}
    if coverage.get("status_counts") != counted:
        problems.append("coverage.status_counts mismatch")
    if coverage.get("not_run") != counted["NOT_RUN"]:
        problems.append("coverage.not_run mismatch")
    if coverage.get("evaluated") != len(data.get("rows") or []) - counted["NOT_RUN"]:
        problems.append("coverage.evaluated mismatch")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the target x horizon x candidate matrix (v5)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="validate the existing matrix only")
    args = ap.parse_args(argv)
    if args.check:
        problems = check(Path(args.out))
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=1))
        return 0 if not problems else 1
    matrix = build()
    Path(args.out).write_text(json.dumps(matrix, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8", newline="\n")
    problems = check(Path(args.out))
    summary = {"rows": len(matrix["rows"]), "status_counts": matrix["coverage"]["status_counts"],
               "not_run": matrix["coverage"]["not_run"], "out": args.out,
               "problems": problems}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
