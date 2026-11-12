#!/usr/bin/env python3
"""Resolve forward-registry rows against first-publication actuals and report the C6 status.

Protocol v4 (``admission.criteria[6]``, C6): at least 12 resolved forward-registry forecasts for
the pair, cumulative relative loss <= 1.00 and, for T1/T2, empirical interval coverage within 10
percentage points of nominal; for T3 calibration replaces coverage.

Resolution is journal-only: the comparator rows of the control arms are recorded in the same
journal (``EQUAL_WEIGHT_POINT`` for point losses, ``EQUAL_WEIGHT_DENSITY`` for distribution and
event losses), so a resolution never recomputes a forecast from data that post-dates the origin.
Actuals are the first-publication values (``amfe.evaluation.actuals.actual_pair``); no raw value
is printed or written: the report carries counts, relative losses, coverage and calibration only.

Usage:
    python tools/registry_resolve.py --status
    python tools/registry_resolve.py --resolve
    python tools/registry_resolve.py --status --journal path/to/journal.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

DEFAULT_JOURNAL = ROOT / "registry" / "forecasts.jsonl"
MIN_FORWARD = 12

#: comparison of a candidate row with its control-arm comparator, by loss family
COMPARATOR_ARM = {"RMSE": "EQUAL_WEIGHT_POINT", "CRPS": "EQUAL_WEIGHT_DENSITY",
                  "Brier": "EQUAL_WEIGHT_DENSITY", "log_score": "EQUAL_WEIGHT_DENSITY"}


def _identity(row: dict) -> tuple:
    return (row.get("candidate_id"), row.get("target"), row.get("horizon"), row.get("origin"))


def _pair_key(row: dict) -> tuple:
    return (row.get("target"), row.get("horizon"))


def default_actual(target: str, origin: str, horizon: int) -> float | None:
    """First-publication actual of the target period, or None while it is not published."""
    from amfe.evaluation.actuals import actual_pair

    pair = actual_pair(target, origin, horizon)
    return None if pair.get("primary") is None else float(pair["primary"])


def collect(journal: Path) -> dict:
    """Group journal rows: candidates, control arms and every pair present."""
    from registry import read_rows

    rows = read_rows(journal)
    candidates: dict[tuple, dict] = {}
    arms: dict[tuple, dict] = {}
    pairs = set()
    for row in rows:
        key = _identity(row)
        pairs.add(_pair_key(row))
        if row.get("control_arm") or row.get("candidate_id") in {
                "RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY", "OWN_NOWCAST_BRIDGE"}:
            arms[key] = row
        else:
            candidates[key] = row
    return {"rows": rows, "candidates": candidates, "arms": arms, "pairs": sorted(pairs)}


def resolve_rows(journal: Path, actual_for=default_actual) -> dict:
    """Resolve every journal row whose fact is published; returns metadata + loss inputs.

    The returned structure contains only forecasts from the journal and actuals as scalars; callers
    must report aggregates (relative loss, coverage, counts), never the values themselves.
    """
    from registry import read_rows

    data = collect(journal)
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for row in read_rows(journal):
        target, horizon = _pair_key(row)
        actual = actual_for(target, row.get("origin"), int(horizon))
        item = {"candidate_id": row.get("candidate_id"), "target": target,
                "horizon": int(horizon), "origin": row.get("origin"),
                "control_arm": bool(row.get("control_arm")),
                "event_probability": row.get("event_probability"),
                "point": row.get("point"),
                "quantiles": row.get("quantiles_5_to_95_step_5")}
        if actual is None:
            unresolved.append({"candidate_id": item["candidate_id"], "target": target,
                               "horizon": int(horizon), "origin": row.get("origin"),
                               "reason": "fact not published"})
        else:
            item["actual"] = float(actual)
            resolved.append(item)
    return {"resolved": resolved, "unresolved": unresolved,
            "pairs": [{"target": t, "horizon": int(h)} for t, h in data["pairs"]],
            "rows": len(data["rows"])}


def _comparator(resolved: list[dict], target: str, horizon: int, origin, arm: str) -> dict | None:
    for item in resolved:
        if (item["candidate_id"] == arm and item["target"] == target
                and item["horizon"] == int(horizon) and item["origin"] == origin):
            return item
    return None


def c6_for_candidate(resolved: list[dict], candidate_id: str, target: str, horizon: int) -> dict:
    """C6 status of one candidate on one (target, horizon) pair, against the recorded arms."""
    import numpy as np

    import evaluate

    rows = sorted(
        (item for item in resolved
         if item["candidate_id"] == candidate_id and item["target"] == target
         and item["horizon"] == int(horizon)),
        key=lambda item: item["origin"])
    actuals = [item["actual"] for item in rows]
    if target == "T3_RECESSION":
        probability = [float(item["event_probability"]) for item in rows]
        comparator_p = []
        for item in rows:
            arm = _comparator(resolved, target, horizon, item["origin"],
                              COMPARATOR_ARM["Brier"])
            comparator_p.append(0.5 if arm is None or arm["event_probability"] is None
                                else float(arm["event_probability"]))
        check = evaluate.c6_forward_check(
            target, actuals, candidate_probability=probability,
            comparator_probability=comparator_p, min_forecasts=MIN_FORWARD)
    else:
        points = np.asarray([[float(item["point"])] for item in rows], dtype=float).ravel()
        quantiles = np.asarray([item["quantiles"] for item in rows], dtype=float)
        comparator_points, comparator_quantiles = [], []
        for item in rows:
            point_arm = _comparator(resolved, target, horizon, item["origin"],
                                    COMPARATOR_ARM["RMSE"])
            density_arm = _comparator(resolved, target, horizon, item["origin"],
                                      COMPARATOR_ARM["CRPS"])
            comparator_points.append(0.0 if point_arm is None or point_arm["point"] is None
                                     else float(point_arm["point"]))
            if density_arm is not None and density_arm["quantiles"] is not None:
                comparator_quantiles.append([float(v) for v in density_arm["quantiles"]])
            else:
                comparator_quantiles.append(None)
        usable = [i for i, q in enumerate(comparator_quantiles) if q is not None]
        if len(usable) < len(rows):
            # a pair without a complete control arm is reported but cannot pass C6
            usable_all = False
        else:
            usable_all = True
        comparator_q = ([comparator_quantiles[i] for i in usable] if usable
                        else np.zeros((len(rows), 19)))
        keep = usable if usable else list(range(len(rows)))
        check = evaluate.c6_forward_check(
            target, [actuals[i] for i in keep],
            candidate_point=points[keep],
            candidate_quantiles=quantiles[keep],
            comparator_point=np.asarray([comparator_points[i] for i in keep], dtype=float),
            comparator_quantiles=np.asarray(comparator_q, dtype=float),
            min_forecasts=MIN_FORWARD)
        check["comparator_complete"] = usable_all
        if not usable_all:
            check["pass"] = False
    check["candidate_id"] = candidate_id
    check["origins"] = [item["origin"] for item in rows]
    return check


MIN_DISTINCT_TARGET_PERIODS = 4  # v5 D-151: weekly rows of one target month do not add periods


def _target_period_label(target: str, origin, horizon: int) -> str:
    """Calendar target period of one registry row (monthly y/y, quarterly growth, event window)."""
    from datetime import date
    from amfe.forecast.data import month_end_of, target_period

    try:
        parts = str(origin).split("-")
        start = month_end_of(date(int(parts[0]), int(parts[1]), 1))
        freq, period = target_period(target, start, int(horizon))
        return f"{freq}:{int(period)}"
    except Exception:
        return f"{origin}:{int(horizon)}"


def status_report(journal: Path, actual_for=default_actual) -> dict:
    from registry import journal_status

    resolution = resolve_rows(journal, actual_for=actual_for)
    candidates = sorted({item["candidate_id"] for item in resolution["resolved"]
                         if not item["control_arm"]})
    pairs = sorted({(item["target"], item["horizon"]) for item in resolution["resolved"]
                    if not item["control_arm"]})
    checks = []
    for candidate_id in candidates:
        for target, horizon in pairs:
            check = c6_for_candidate(resolution["resolved"], candidate_id, target, horizon)
            periods = sorted({_target_period_label(target, item["origin"], horizon)
                              for item in resolution["resolved"]
                              if item["candidate_id"] == candidate_id
                              and item["target"] == target and item["horizon"] == horizon})
            check["distinct_target_periods"] = len(periods)
            check["min_distinct_target_periods"] = MIN_DISTINCT_TARGET_PERIODS
            check["distinct_periods_ok"] = len(periods) >= MIN_DISTINCT_TARGET_PERIODS
            check["pass"] = bool(check["pass"] and check["distinct_periods_ok"])
            checks.append(check)
    any_complete = any(check["count_met"] and check.get("comparator_complete", True)
                       for check in checks)
    report = {
        "journal": journal_status(journal),
        "rows": resolution["rows"],
        "resolved_rows": len(resolution["resolved"]),
        "unresolved_rows": len(resolution["unresolved"]),
        "min_forecasts": MIN_FORWARD,
        "min_distinct_target_periods": MIN_DISTINCT_TARGET_PERIODS,
        "c6_checks": checks,
        "n_resolved": max([check["n_resolved"] for check in checks], default=0),
        "pass": bool(checks and all(check["pass"] for check in checks)),
        "status": ("PASS" if checks and all(check["pass"] for check in checks)
                   else "FAIL_INSUFFICIENT_DATA" if not any_complete
                   else "FAIL"),
        "note": ("C6 (v5) needs at least 12 resolved forecasts per pair, at least "
                 f"{MIN_DISTINCT_TARGET_PERIODS} distinct target periods, and every loss and "
                 "coverage check to pass; weekly rows of one target month are correlated and do "
                 "not add periods; unresolved rows do not enter the count"),
    }
    return report


def resolve_report(journal: Path, actual_for=default_actual) -> dict:
    """Cumulative relative loss and coverage for every resolved row; no raw values."""
    import numpy as np

    import evaluate

    resolution = resolve_rows(journal, actual_for=actual_for)
    blocks = []
    for item in resolution["resolved"]:
        if item["control_arm"]:
            continue
        target, horizon = item["target"], item["horizon"]
        arm = _comparator(resolution["resolved"], target, horizon, item["origin"],
                          COMPARATOR_ARM["Brier" if target == "T3_RECESSION" else "CRPS"])
        entry = {"candidate_id": item["candidate_id"], "target": target, "horizon": horizon,
                 "origin": item["origin"], "actual_available": True}
        if target == "T3_RECESSION":
            probability = np.asarray([item["event_probability"]], dtype=float)
            comparator_p = np.asarray([0.5 if arm is None else arm["event_probability"]],
                                      dtype=float)
            actuals = np.asarray([item["actual"]], dtype=float)
            entry["brier"] = evaluate.aggregate_loss(
                "Brier", evaluate.observation_loss("Brier", actuals, probability=probability))
            entry["comparator_brier"] = evaluate.aggregate_loss(
                "Brier", evaluate.observation_loss("Brier", actuals, probability=comparator_p))
            entry["cumulative_relative_loss"] = evaluate.relative_loss(
                entry["brier"], entry["comparator_brier"]) if entry["comparator_brier"] > 0 else None
            entry["calibration"] = evaluate.calibration_bins(probability, actuals)
        else:
            actuals = np.asarray([item["actual"]], dtype=float)
            point = np.asarray([item["point"]], dtype=float)
            quantiles = np.asarray([item["quantiles"]], dtype=float)
            entry["rmse"] = evaluate.aggregate_loss(
                "RMSE", evaluate.observation_loss("RMSE", actuals, point=point))
            entry["crps"] = evaluate.aggregate_loss(
                "CRPS", evaluate.observation_loss("CRPS", actuals, quantiles=quantiles))
            if arm is not None and arm["point"] is not None:
                entry["comparator_rmse"] = evaluate.aggregate_loss(
                    "RMSE", evaluate.observation_loss(
                        "RMSE", actuals, point=np.asarray([arm["point"]], dtype=float)))
                entry["cumulative_relative_loss"] = evaluate.relative_loss(
                    entry["rmse"], entry["comparator_rmse"]) if entry["comparator_rmse"] > 0 else None
            if arm is not None and arm["quantiles"] is not None:
                entry["comparator_crps"] = evaluate.aggregate_loss(
                    "CRPS", evaluate.observation_loss(
                        "CRPS", actuals, quantiles=np.asarray([arm["quantiles"]], dtype=float)))
            entry["coverage"] = evaluate.interval_coverage(quantiles, actuals)
        blocks.append(entry)
    return {"journal": str(journal), "resolved_rows": len(resolution["resolved"]),
            "unresolved_rows": len(resolution["unresolved"]), "resolutions": blocks,
            "note": "cumulative relative loss = candidate loss / control-arm loss over the "
                    "resolved rows of this row; raw actual values are never reported"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the forward registry and report C6")
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    parser.add_argument("--status", action="store_true", help="C6 status summary")
    parser.add_argument("--resolve", action="store_true", help="resolve rows with a published fact")
    args = parser.parse_args(argv)
    journal = Path(args.journal)
    try:
        if args.resolve:
            print(json.dumps(resolve_report(journal), ensure_ascii=False, indent=1))
        else:
            print(json.dumps(status_report(journal), ensure_ascii=False, indent=1))
        return 0
    except Exception as error:  # pragma: no cover - CLI surface
        print(json.dumps({"error": str(error), "type": type(error).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
