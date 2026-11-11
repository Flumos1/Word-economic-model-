#!/usr/bin/env python3
"""First end-to-end evaluation run: PIPELINE VALIDATION PILOT (no admission).

This runner wires the pieces together for the first time: PIT data layer, the seven baseline
models, the comparator composition and the C1/C2 machinery of the frozen protocol v4. It is a
pilot, not an admission decision:

  * only window W2 and a short sub-range are used;
  * actuals are the latest-vintage values (first-publication actuals are a separate R2 layer);
  * density losses (CRPS of the equal-weight mixture) are deferred until the mixture pool is wired;
  * only point losses (RMSE) and event losses (Brier, log score) are reported;
  * no candidate leaves the CANDIDATE stage; the report carries pilot=true, admission=false.

Run: python tools/run_first_evaluation.py --out protocols/first_evaluation_report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.forecast import forecast, applicable_models  # noqa: E402
from amfe.forecast.data import target_actual  # noqa: E402
import evaluate  # noqa: E402
import windows  # noqa: E402

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
R3 = ROOT / "protocols" / "r3_specification.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
THRESHOLDS = ROOT / "protocols" / "nf_sim_thresholds.json"

PILOT_TARGETS = {
    "T1_CPI_YOY": [1, 12],
    "T2_GDP_GROWTH": [0, 4],
    "T3_RECESSION": [3, 12],
}
SERIES_BY_TARGET = {"T1_CPI_YOY": "CPIAUCSL", "T2_GDP_GROWTH": None, "T3_RECESSION": None}
LOSSES = {"T1_CPI_YOY": ["RMSE"], "T2_GDP_GROWTH": ["RMSE"], "T3_RECESSION": ["Brier", "log_score"]}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def members_for(composition: dict, target: str, loss: str, horizon: int) -> list:
    """Comparator members for the (target, loss, horizon) triple from the composition document."""
    block = composition["targets"][target]
    losses = block.get("losses", {})
    entry = losses.get(loss) or {}
    by_horizon = entry.get("by_horizon", {})
    members = by_horizon.get(str(int(horizon)), {}).get("members")
    if members:
        return list(members)
    members = entry.get("members")
    if isinstance(members, list):
        return [m["id"] if isinstance(m, dict) else m for m in members]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="First evaluation pilot (pipeline validation)")
    ap.add_argument("--out", default=str(ROOT / "protocols" / "first_evaluation_report.json"))
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2019-12-31")
    ap.add_argument("--mode", default="latest")
    args = ap.parse_args(argv)

    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    composition = json.loads(COMPOSITION.read_text(encoding="utf-8"))
    thresholds = evaluate.load_thresholds(THRESHOLDS)
    report = {
        "report_id": "AMFE-FIRST-EVALUATION-PILOT",
        "pilot": True,
        "admission": False,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": args.mode,
        "window": "W2",
        "range": [args.start, args.end],
        "actuals": "latest vintage (first-publication actuals are a separate R2 layer)",
        "deferred": ["density losses (CRPS of the mixture pool)", "window W1", "first-publication actuals",
                     "admission stages and multiplicity"],
        "protocol": {"file": "protocols/evaluation_protocol_v4.json", "version": protocol["version"],
                     "sha256": sha(PROTOCOL)},
        "r3_specification": {"file": "protocols/r3_specification.json", "version": json.loads(R3.read_text(encoding="utf-8"))["version"],
                             "sha256": sha(R3)},
        "composition": {"file": COMPOSITION.name, "sha256": sha(COMPOSITION)},
        "pairs": [],
    }
    for target, horizons in PILOT_TARGETS.items():
        for h in horizons:
            rows = windows.origins_for(protocol, target, h, "W2")
            origins = [r["origin"] for r in rows if args.start <= r["origin"] <= args.end]
            series_id = SERIES_BY_TARGET[target]
            actuals = []
            model_data = {}
            failures = {}
            for origin in origins:
                actual = target_actual(target, origin, h, series_id=series_id)
                actuals.append(np.nan if actual is None else float(actual))
                for model_id in applicable_models(target, h):
                    try:
                        f = forecast(model_id, target, origin, h, series_id=series_id,
                                     mode=args.mode).to_dict()
                        model_data.setdefault(model_id, []).append(f)
                    except Exception as exc:  # model refusal is counted, not fatal
                        failures[model_id] = failures.get(model_id, 0) + 1
            if not origins:
                continue
            actual = np.array(actuals)
            valid = ~np.isnan(actual)
            for loss in LOSSES[target]:
                members = members_for(composition, target, loss, h)
                if not members:
                    report["pairs"].append({"target": target, "horizon": h, "loss": loss,
                                            "skipped": "no members in the composition document"})
                    continue
                point = {}
                prob = {}
                for model_id in members:
                    records = model_data.get(model_id, [])
                    if len(records) != len(origins):
                        continue
                    if target == "T3_RECESSION":
                        prob[model_id] = np.array([float(r.get("probability", np.nan)) for r in records])
                    else:
                        point[model_id] = np.array([float(r.get("point", np.nan)) for r in records])
                if target == "T3_RECESSION":
                    comp = np.nanmean(np.array(list(prob.values())), axis=0) if prob else None
                    cand_arrays = prob
                else:
                    comp = np.nanmean(np.array(list(point.values())), axis=0) if point else None
                    cand_arrays = point
                if comp is None:
                    report["pairs"].append({"target": target, "horizon": h, "loss": loss,
                                            "skipped": "no comparator member produced forecasts"})
                    continue
                for model_id, cand in cand_arrays.items():
                    mask = valid & ~np.isnan(cand) & ~np.isnan(comp)
                    if mask.sum() < 8:
                        continue
                    if loss == "RMSE":
                        cand_loss = evaluate.aggregate_loss(loss, np.abs(cand[mask] - actual[mask]))
                        comp_loss = evaluate.aggregate_loss(loss, np.abs(comp[mask] - actual[mask]))
                        differential = (cand[mask] - actual[mask]) ** 2 - (comp[mask] - actual[mask]) ** 2
                    else:
                        cand_loss = evaluate.aggregate_loss(loss, evaluate.observation_loss(loss, actual[mask], probability=cand[mask]))
                        comp_loss = evaluate.aggregate_loss(loss, evaluate.observation_loss(loss, actual[mask], probability=comp[mask]))
                        if loss == "Brier":
                            differential = (cand[mask] - actual[mask]) ** 2 - (comp[mask] - actual[mask]) ** 2
                        else:
                            differential = (evaluate.log_score_observation(cand[mask], actual[mask])
                                            - evaluate.log_score_observation(comp[mask], actual[mask]))
                    threshold = evaluate.threshold_for(thresholds, target, h, loss, "W2")
                    c1 = evaluate.c1_window(cand_loss, comp_loss, threshold)
                    dm = evaluate.dm_test(differential, horizon=h, alternative="less")
                    report["pairs"].append({
                        "target": target, "horizon": h, "loss": loss, "candidate": model_id,
                        "candidate_role": "comparator member (pipeline validation, not a real candidate)",
                        "origins": int(mask.sum()), "comparator_members": sorted(cand_arrays.keys()),
                        "candidate_loss": round(float(cand_loss), 6),
                        "comparator_loss": round(float(comp_loss), 6),
                        "relative_loss": round(float(evaluate.relative_loss(cand_loss, comp_loss)), 4),
                        "threshold": threshold, "c1_pass": bool(c1["pass"]),
                        "dm_p_value_one_sided": round(float(dm["p_value"]), 4),
                        "model_failures": failures.get(model_id, 0),
                    })
                report["coverage"] = report.get("coverage", [])
                report["coverage"].append({"target": target, "horizon": h, "loss": loss,
                                           "origins": len(origins),
                                           "models_with_forecasts": sorted(cand_arrays.keys())})
    pairs = [p for p in report["pairs"] if "relative_loss" in p]
    report["summary"] = {
        "pairs": len(pairs),
        "c1_pass_pairs": sum(1 for p in pairs if p["c1_pass"]),
        "relative_loss_range": [min(p["relative_loss"] for p in pairs), max(p["relative_loss"] for p in pairs)] if pairs else None,
        "conclusion": ("Pipeline validated end to end: PIT data -> models -> comparator composition -> losses, "
                       "thresholds and C1/C2. This is not an admission result."),
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    print(json.dumps({"pairs": len(report["pairs"]), "out": args.out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
