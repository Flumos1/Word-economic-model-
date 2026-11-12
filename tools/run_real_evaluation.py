#!/usr/bin/env python3
"""First REAL evaluation: a registered candidate against the frozen comparator on W1 + W2.

Differences from the pilot (tools/run_first_evaluation.py):
  * actuals are first-publication / 12-month-vintage values from the ALFRED archive
    (amfe.evaluation.actuals), not the latest tape;
  * the comparator distribution is the protocol mixture of member CDFs
    (amfe.evaluation.distributions), not an average of quantiles;
  * both windows W1 and W2 are built, so C1/C2/C4/C5 are computed by the admission harness;
  * C6 (12 forward-registry forecasts) cannot be satisfied on the first day, so the best
    possible stage is SHADOW_TEST; the report states this explicitly.

The candidate must be registered before the evaluation (protocol model_class_rules.registry_access);
this runner writes protocols/candidates_<date>.json with the candidate definition and code hash.
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

from amfe.forecast import forecast  # noqa: E402
from amfe.forecast.data import load_latest, month_index, usrec_event  # noqa: E402
from amfe.candidates.models import (CANDIDATE_DESCRIPTION, CANDIDATE_IDS,  # noqa: E402
                                    candidate_forecast as rw_ar_candidate)
from amfe.ensemble import ENSEMBLE_IDS  # noqa: E402
from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate  # noqa: E402


def candidate_forecast(candidate_id, target, origin, horizon, series_id=None, mode="auto"):
    """Dispatcher: ensemble candidates come from amfe.ensemble, the rest from amfe.candidates."""
    if candidate_id in ENSEMBLE_IDS:
        return ensemble_candidate(candidate_id, target, origin, horizon, series_id=series_id, mode=mode)
    return rw_ar_candidate(candidate_id, target, origin, horizon, series_id=series_id, mode=mode)
from amfe.evaluation.actuals import actual_pair  # noqa: E402
from amfe.evaluation.distributions import mixture_quantiles  # noqa: E402
import evaluate  # noqa: E402
import windows  # noqa: E402

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
R3 = ROOT / "protocols" / "r3_specification.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
CANDIDATES = ROOT / "protocols" / "candidates_2026-11-11.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def members_for(composition: dict, target: str, loss: str, horizon: int) -> list:
    block = composition["targets"][target]["losses"].get(loss, {})
    entry = block.get("by_horizon", {}).get(str(int(horizon)), {})
    ids = entry.get("members")
    if ids:
        return list(ids)
    fallback = block.get("members", [])
    return [m["id"] if isinstance(m, dict) else m for m in fallback]


def recession_mask(target: str, origins: list, horizon: int) -> list:
    usrec = load_latest("USREC")
    mask = []
    for origin in origins:
        ym = month_index(origin)
        event = usrec_event(usrec, ym, int(horizon)) if target != "T3_RECESSION" else usrec_event(usrec, ym, int(horizon))
        mask.append(1 if event else 0)
    return mask


def build_window(target: str, horizon: int, window: str, candidate_id: str, series_id,
                 mode: str, allow_pseudo: bool = False) -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    composition = json.loads(COMPOSITION.read_text(encoding="utf-8"))
    rows = windows.origins_for(protocol, target, horizon, window)
    origins = [r["origin"] for r in rows]
    member_ids = members_for(composition, target, "CRPS" if target != "T3_RECESSION" else "Brier", horizon)
    cand_points, cand_quants = [], []
    comp_points, comp_quants = [], []
    actual_primary, actual_secondary = [], []
    data_classes = set()
    vintage_dates = set()
    used_origins = []
    member_failures = 0
    pseudo_skipped = 0
    for origin in origins:
        try:
            cand = candidate_forecast(candidate_id, target, origin, horizon, series_id=series_id, mode=mode)
        except Exception:
            continue
        actual = actual_pair(target, origin, horizon, series_id=series_id)
        if actual["primary"] is None:
            continue
        member_forecasts = []
        for member in member_ids:
            try:
                member_forecasts.append(forecast(member, target, origin, horizon,
                                                 series_id=series_id, mode=mode).to_dict())
            except Exception:
                member_failures += 1
        if len(member_forecasts) < 2:
            continue
        origin_classes = {cand["data_class"]} | {m["data_class"] for m in member_forecasts}
        if not allow_pseudo and origin_classes != {"REAL_TIME"}:
            pseudo_skipped += 1
            continue
        data_classes.update(origin_classes)
        vintage_dates.update([m["vintage_date"] for m in member_forecasts if m.get("vintage_date")])
        if target == "T3_RECESSION":
            prob = np.mean([float(m["probability"]) for m in member_forecasts])
            cand_points.append(float(cand["probability"]))
            comp_points.append(float(prob))
        else:
            cand_points.append(float(cand["point"]))
            cand_quants.append(np.asarray(cand["quantiles"], dtype=float))
            comp_points.append(float(np.mean([float(m["point"]) for m in member_forecasts])))
            comp_quants.append(mixture_quantiles([np.asarray(m["quantiles"], dtype=float)
                                                  for m in member_forecasts]))
        actual_primary.append(actual["primary"])
        actual_secondary.append(actual["secondary"] if actual["secondary"] is not None else actual["primary"])
        used_origins.append(origin)
    data_class = "REAL_TIME" if data_classes == {"REAL_TIME"} else "PSEUDO_REAL_TIME"
    out = {
        "window": window,
        "origins": len(used_origins),
        "first_origin": used_origins[0] if used_origins else None,
        "last_origin": used_origins[-1] if used_origins else None,
        "data_class": data_class,
        "vintage_dates": sorted(vintage_dates),
        "member_failures": member_failures,
        "pseudo_origins_skipped": pseudo_skipped,
        "actual_primary": np.asarray(actual_primary, dtype=float),
        "actual_secondary": np.asarray(actual_secondary, dtype=float),
    }
    if target == "T3_RECESSION":
        out["candidate"] = {"probability": np.asarray(cand_points, dtype=float)}
        out["comparator"] = {"probability": np.asarray(comp_points, dtype=float)}
    else:
        out["candidate"] = {"point": np.asarray(cand_points, dtype=float),
                            "quantiles": np.asarray(cand_quants, dtype=float)}
        out["comparator"] = {"point": np.asarray(comp_points, dtype=float),
                             "quantiles": np.asarray(comp_quants, dtype=float)}
        out["recession"] = np.asarray(recession_mask(target, used_origins, horizon), dtype=bool)
    return out



def _array_digest(values) -> str:
    arr = np.asarray(values, dtype=float)
    return hashlib.sha256(arr.tobytes()).hexdigest()


def _sanitized_window(window: dict) -> dict:
    """Report wrapper: derived metadata and digests only, never the third-party actual values."""
    out = {k: v for k, v in window.items()
           if k not in ("actual_primary", "actual_secondary", "recession")}
    if "actual_primary" in window:
        out["actual_primary_sha256"] = _array_digest(window["actual_primary"])
        out["actual_primary_n"] = int(np.asarray(window["actual_primary"]).size)
    if "actual_secondary" in window:
        out["actual_secondary_sha256"] = _array_digest(window["actual_secondary"])
        out["actual_secondary_n"] = int(np.asarray(window["actual_secondary"]).size)
    if "recession" in window:
        out["recession_mask_sha256"] = _array_digest(window["recession"])
    out["licence_note"] = "actual values are not stored; only digests and derived metrics (docs/84)"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="First real evaluation on W1+W2")
    ap.add_argument("--candidate", default="ENSEMBLE_RW_AR")
    ap.add_argument("--target", default="T1_CPI_YOY")
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--series-id", default=None)
    ap.add_argument("--mode", default="auto", help="auto uses vintages when present (REAL_TIME)")
    ap.add_argument("--allow-pseudo", action="store_true",
                    help="include PSEUDO_REAL_TIME origins (the admission guard will then refuse)")
    ap.add_argument("--out", default=str(ROOT / "protocols" / "first_real_evaluation_report.json"))
    args = ap.parse_args(argv)
    if args.candidate not in CANDIDATE_IDS and args.candidate not in ENSEMBLE_IDS:
        raise SystemExit(f"unknown candidate {args.candidate}")
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    candidate_doc = {
        "document_id": "AMFE-CANDIDATES",
        "registered_on": "2026-11-11",
        "registered_before_evaluation": True,
        "candidates": [{
            "id": cid,
            "description": CANDIDATE_DESCRIPTION[cid],
            "code_file": "src/amfe/candidates/models.py",
            "code_sha256": sha(ROOT / "src" / "amfe" / "candidates" / "models.py"),
            "targets": ["T1_CPI_YOY", "T2_GDP_GROWTH"],
            "members": ["RW", "AR_BIC"],
            "note": "candidate registered before the first real evaluation; forward-registry start follows",
        } for cid in CANDIDATE_IDS],
    }
    CANDIDATES.write_text(json.dumps(candidate_doc, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    w1 = build_window(args.target, args.horizon, "W1", args.candidate, args.series_id, args.mode,
                      allow_pseudo=args.allow_pseudo)
    w2 = build_window(args.target, args.horizon, "W2", args.candidate, args.series_id, args.mode,
                      allow_pseudo=args.allow_pseudo)
    data_class = "REAL_TIME" if w1["data_class"] == w2["data_class"] == "REAL_TIME" else "PSEUDO_REAL_TIME"
    report = {
        "report_id": "AMFE-FIRST-REAL-EVALUATION",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate": args.candidate,
        "target": args.target,
        "horizon": args.horizon,
        "mode": args.mode,
        "data_class": data_class,
        "protocol": {"file": "protocols/evaluation_protocol_v4.json", "version": protocol["version"],
                     "sha256": sha(PROTOCOL)},
        "r3_specification": {"file": "protocols/r3_specification.json",
                             "version": json.loads(R3.read_text(encoding="utf-8"))["version"],
                             "sha256": sha(R3)},
        "composition": {"file": COMPOSITION.name, "sha256": sha(COMPOSITION)},
        "candidates": {"file": CANDIDATES.name, "sha256": sha(CANDIDATES)},
        "windows": {w["window"]: _sanitized_window(w) for w in (w1, w2)},
        "c6": "not available on the first day: the forward registry has no resolved forecasts yet",
        "admission": evaluate.evaluate_admission(
            args.target, args.horizon,
            {"W1": {k: w1[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator")},
             "W2": {k: w2[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator")}},
            data_class=data_class),
    }
    # C4 needs the recession mask per window; recompute the admission report with it
    windows_with_mask = {
        "W1": {k: w1[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator", "recession")},
        "W2": {k: w2[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator", "recession")},
    }
    if args.target != "T3_RECESSION":
        family = {}  # the evaluated candidate is added under candidate_id; other candidates go here
        report["admission"] = evaluate.evaluate_admission(
            args.target, args.horizon, windows_with_mask, data_class=data_class,
            family=family, candidate_id=args.candidate)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8", newline="\n")
    adm = report["admission"]
    print(json.dumps({"candidate": args.candidate, "target": args.target, "horizon": args.horizon,
                      "data_class": data_class, "status": adm["status"],
                      "w1_origins": w1["origins"], "w2_origins": w2["origins"],
                      "c1": {loss: adm["criteria"]["C1"]["windows"]["W1"][loss]["pass"]
                             for loss in adm["criteria"]["C1"]["windows"]["W1"]},
                      "c2": adm["criteria"]["C2"]["pass"], "c4": adm["criteria"]["C4"]["pass"],
                      "c5": adm["criteria"]["C5"]["pass"], "out": args.out}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
