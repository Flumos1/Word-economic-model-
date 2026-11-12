#!/usr/bin/env python3
"""T3 (recession probability) admission evaluation on protocol v4: W1+W2, REAL_TIME.

Self-contained runner (it deliberately does not import ``tools/run_real_evaluation.py``, which is
edited by another track):

  * origins come from ``tools/windows.py``: ``origins_for(protocol_v4, 'T3_RECESSION', h, window)``
    for h in 3/6/12 and the two evaluation windows W1 (T3 starts 1996-12, D-131) and W2;
  * candidate probabilities come from ``amfe.ensemble.candidate_forecast`` for ENSEMBLE_ADAPTIVE
    (candidate) and ENSEMBLE_EQUAL (control, equal weights);
  * the comparator is the frozen composition of ``protocols/comparator_composition_2026-11-11.json``
    for T3 (PROBIT_SPREAD + SAHM_RULE, losses Brier/log_score) and its probability is the arithmetic
    mean of the member probabilities, exactly as ``tools/run_real_evaluation.py`` builds the T3
    comparator.  The member probabilities are read from the same point-in-time member forecasts that
    the ensemble layer runs at that origin (``member_probabilities``); the runner independently
    re-computes the members with ``amfe.forecast.forecast`` on the first origins of every window and
    refuses to continue if any value differs by more than 1e-12;
  * the fact is the NBER chronology target (``amfe.evaluation.actuals.actual_pair``), i.e. the
    protocol T3 resolution rule; primary == secondary for T3;
  * ``tools/evaluate.py`` computes the admission criteria: C1/C2/C5 (C2 on a declared Romano-Wolf
    family of the two candidates), C4 is not applicable to T3 by the protocol, C6 is
    NO_RESOLVED_FORECASTS (the forward registry has no resolved forecast for the pair).

Only aggregates and digests are written to ``protocols/ensemble_eval_<cand>_T3_h<N>_v2.json`` and to
``protocols/ensemble_t3_report.json``: no forecast, probability or actual value is stored (licence
rule docs/84, D-147).  C1/C2/C5 are harness results, not an admission: the reports carry
``reporting_only`` and no stage is assigned.

Usage:
    python tools/run_t3_evaluation.py --jobs 8
    python tools/run_t3_evaluation.py --max-origins 3 --jobs 0   # smoke test, no parallel
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate  # noqa: E402
from amfe.evaluation.actuals import actual_pair  # noqa: E402
from amfe.forecast import forecast  # noqa: E402
from amfe.forecast.data import load_vintages  # noqa: E402
import evaluate  # noqa: E402
import windows  # noqa: E402

TARGET = "T3_RECESSION"
HORIZONS = (3, 6, 12)
CANDIDATES = ("ENSEMBLE_ADAPTIVE", "ENSEMBLE_EQUAL")
CONTROL = "ENSEMBLE_EQUAL"
CANDIDATE_ROLES = {"ENSEMBLE_ADAPTIVE": "candidate", "ENSEMBLE_EQUAL": "control"}
WINDOWS = ("W1", "W2")
LOSSES = ("Brier", "log_score")
MODE = "auto"
MEMBER_TOLERANCE = 1e-12

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
R3 = ROOT / "protocols" / "r3_specification.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
CANDIDATES_FILE = ROOT / "protocols" / "candidates_2026-11-11.json"
THRESHOLDS = ROOT / "protocols" / "nf_sim_thresholds.json"
NBER = ROOT / "protocols" / "nber_dates.json"

PROTOCOL_DOC = json.loads(PROTOCOL.read_text(encoding="utf-8"))
COMPOSITION_DOC = json.loads(COMPOSITION.read_text(encoding="utf-8"))
THRESHOLD_DOC = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
CANDIDATE_DOC = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
R3_DOC = json.loads(R3.read_text(encoding="utf-8"))
NBER_DOC = json.loads(NBER.read_text(encoding="utf-8"))

LICENCE_NOTE = ("forecasts and actual values are not stored: only digests and derived metrics "
                "(docs/84, D-147)")


# ------------------------------------------------------------------------------------ artefacts
def sha(path: Path) -> str:
    """SHA-256 of a text artefact with CRLF normalised to LF (same rule as the other runners)."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _array_digest(values) -> str:
    return hashlib.sha256(np.asarray(values, dtype=float).tobytes()).hexdigest()


def protocol_origins_count(horizon: int, window: str) -> int:
    return len(windows.origins_for(PROTOCOL_DOC, TARGET, int(horizon), window))


# ---------------------------------------------------------------------------- frozen composition
def members_for(composition: dict, target: str, loss: str, horizon: int) -> list:
    """Comparator members for (target, loss, horizon) from the frozen composition document."""
    block = composition["targets"][target]["losses"].get(loss, {})
    entry = block.get("by_horizon", {}).get(str(int(horizon)), {})
    ids = entry.get("members")
    if ids:
        return list(ids)
    fallback = block.get("members", [])
    return [m["id"] if isinstance(m, dict) else m for m in fallback]


def comparator_members(horizon: int) -> list:
    """T3 comparator members; the composition must list the same set for both event losses."""
    blocks = {loss: members_for(COMPOSITION_DOC, TARGET, loss, horizon) for loss in LOSSES}
    unique = {tuple(sorted(ids)) for ids in blocks.values()}
    if len(unique) != 1:
        raise RuntimeError(f"comparator composition differs between losses at h={horizon}: {blocks}")
    return sorted(blocks[LOSSES[0]])


def input_vintage_dates(origin: str) -> list:
    """Vintage dates the T3 inputs would use at this origin: the latest vintage <= origin.

    This mirrors ``amfe.forecast.data.vintage_as_of`` (mode auto) for the inputs of the frozen T3
    members (GS10, TB3MS for PROBIT_SPREAD; UNRATE for SAHM_RULE).  Only dates are recorded.
    """
    cutoff = date.fromisoformat(origin)
    used = []
    for name in ("GS10", "TB3MS", "UNRATE"):
        try:
            vintages = load_vintages(name)
        except Exception:  # pragma: no cover - missing series would surface as a model failure
            continue
        usable = [stamp for stamp in vintages if stamp <= cutoff]
        if usable:
            used.append(max(usable).isoformat())
    return used


# --------------------------------------------------------------------------------- window build
def build_window(target: str, horizon: int, window: str, candidate_id: str, mode: str = MODE,
                 max_origins: int | None = None, allow_pseudo: bool = False,
                 verify_members: int = 2) -> dict:
    """Point-in-time probabilities, comparator and facts for one (candidate, horizon, window).

    The returned dict holds plain lists (picklable for the worker processes): the caller sanitises
    them into digests before anything is written to a report file.
    """
    rows = windows.origins_for(PROTOCOL_DOC, target, int(horizon), window)
    if max_origins is not None:
        rows = rows[:int(max_origins)]
    member_ids = comparator_members(int(horizon))
    cand_probs, comp_probs = [], []
    actual_primary, actual_secondary = [], []
    used_origins, vintage_dates = [], set()
    data_classes = set()
    candidate_failures = 0
    member_failures = 0
    actual_missing = 0
    pseudo_skipped = 0
    verified_origins = 0
    max_member_diff = 0.0
    to_verify = int(verify_members)

    for row in rows:
        origin = row["origin"]
        try:
            cand = ensemble_candidate(candidate_id, target, origin, int(horizon), mode=mode)
        except Exception:
            candidate_failures += 1
            continue
        actual = actual_pair(target, origin, int(horizon))
        if actual["primary"] is None:
            actual_missing += 1
            continue
        probabilities = cand.get("member_probabilities") or {}
        missing = [member for member in member_ids if member not in probabilities]
        if missing:
            member_failures += len(missing)
            continue
        member_classes = cand.get("member_data_classes") or {}
        classes = {cand.get("data_class")} | {member_classes.get(member) for member in member_ids}
        if (not allow_pseudo) and classes != {"REAL_TIME"}:
            pseudo_skipped += 1
            continue
        if to_verify > 0:
            for member in member_ids:
                direct = float(forecast(member, target, origin, int(horizon),
                                        mode=mode).to_dict()["probability"])
                diff = abs(direct - float(probabilities[member]))
                max_member_diff = max(max_member_diff, diff)
                if diff > MEMBER_TOLERANCE:
                    raise RuntimeError(
                        f"comparator member {member} at {origin} h={horizon} differs from the direct "
                        f"amfe.forecast call by {diff:.3e}; refusing to build the comparator")
            to_verify -= 1
            verified_origins += 1
        data_classes.update(name for name in classes if name)
        cand_probs.append(float(cand["probability"]))
        comp_probs.append(float(np.mean([float(probabilities[member]) for member in member_ids])))
        actual_primary.append(float(actual["primary"]))
        actual_secondary.append(float(actual["secondary"] if actual["secondary"] is not None
                                      else actual["primary"]))
        used_origins.append(origin)
        if classes == {"REAL_TIME"}:
            vintage_dates.update(input_vintage_dates(origin))
    data_class = "REAL_TIME" if data_classes == {"REAL_TIME"} else "PSEUDO_REAL_TIME"
    return {
        "window": window,
        "target": target,
        "horizon": int(horizon),
        "candidate": candidate_id,
        "protocol_origins": len(rows),
        "origins": len(used_origins),
        "first_origin": used_origins[0] if used_origins else None,
        "last_origin": used_origins[-1] if used_origins else None,
        "data_class": data_class,
        "vintage_dates": sorted(vintage_dates),
        "member_ids": member_ids,
        "member_failures": int(member_failures),
        "candidate_failures": int(candidate_failures),
        "actual_missing": int(actual_missing),
        "pseudo_origins_skipped": int(pseudo_skipped),
        "member_verification": {"origins_checked": verified_origins,
                                "max_abs_diff": float(max_member_diff),
                                "tolerance": MEMBER_TOLERANCE},
        "actual_primary": actual_primary,
        "actual_secondary": actual_secondary,
        "candidate_probability": cand_probs,
        "comparator_probability": comp_probs,
    }


def _worker_task(payload: dict) -> dict:
    """Multiprocessing entry point: build one (candidate, horizon, window) window."""
    return {"key": payload["key"],
            "result": build_window(payload["target"], payload["horizon"], payload["window"],
                                   payload["candidate"], mode=payload["mode"],
                                   max_origins=payload["max_origins"],
                                   allow_pseudo=payload["allow_pseudo"],
                                   verify_members=payload["verify_members"])}


# ------------------------------------------------------------------------------- report assembly
def _sanitized_window(window: dict) -> dict:
    """Report wrapper: derived metadata, aggregates and digests; never raw arrays or values."""
    out = {key: value for key, value in window.items()
           if key not in ("actual_primary", "actual_secondary", "candidate_probability",
                          "comparator_probability")}
    for key in ("actual_primary", "actual_secondary"):
        out[f"{key}_sha256"] = _array_digest(window[key])
        out[f"{key}_n"] = int(len(window[key]))
    out["actual_event_origins"] = int(np.count_nonzero(np.asarray(window["actual_primary"],
                                                                 dtype=float)))
    out["candidate"] = {"probability_sha256": _array_digest(window["candidate_probability"]),
                        "probability_n": int(len(window["candidate_probability"]))}
    out["comparator"] = {"probability_sha256": _array_digest(window["comparator_probability"]),
                         "probability_n": int(len(window["comparator_probability"]))}
    out["licence_note"] = LICENCE_NOTE
    return out


def _json_default(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def _window_data(window: dict) -> dict:
    """Arrays in the shape the admission harness expects for T3 (event probabilities only)."""
    return {"actual_primary": np.asarray(window["actual_primary"], dtype=float),
            "actual_secondary": np.asarray(window["actual_secondary"], dtype=float),
            "candidate": {"probability": np.asarray(window["candidate_probability"], dtype=float)},
            "comparator": {"probability": np.asarray(window["comparator_probability"], dtype=float)}}


def _threshold_for(horizon: int, loss: str, window: str) -> float:
    for entry in THRESHOLD_DOC["entries"]:
        if (entry["target"] == TARGET and int(entry["horizon"]) == int(horizon)
                and entry["loss"] == loss and entry["window"] == window):
            return float(entry["threshold"])
    raise RuntimeError(f"no NF-SIM threshold for {TARGET}/{horizon}/{loss}/{window}")


def _candidate_registration(candidate_id: str) -> dict:
    entry = next(item for item in CANDIDATE_DOC["candidates"] if item["id"] == candidate_id)
    return {
        "document": f"protocols/{CANDIDATES_FILE.name}",
        "document_sha256": sha(CANDIDATES_FILE),
        "id": entry["id"],
        "description": entry.get("description"),
        "role": entry.get("role", CANDIDATE_ROLES.get(candidate_id, "candidate")),
        "targets": entry.get("targets", []),
        "code_file": entry.get("code_file"),
        "code_sha256": entry.get("code_sha256"),
        "code_files": entry.get("code_files"),
        "registered_on": CANDIDATE_DOC.get("registered_on"),
        "registered_before_evaluation": CANDIDATE_DOC.get("registered_before_evaluation"),
    }


def build_report(candidate_id: str, horizon: int, windows_data: dict, all_windows: dict,
                 candidates=CANDIDATES) -> dict:
    """One v2 evaluation report for a (candidate, horizon) pair, built from the window arrays."""
    admission = evaluate.evaluate_admission(
        TARGET, int(horizon),
        {window: _window_data(windows_data[window]) for window in WINDOWS},
        data_class="REAL_TIME",
        family={other: {window: _window_data(all_windows[other][horizon][window])
                        for window in WINDOWS}
                for other in candidates if other != candidate_id},
        candidate_id=candidate_id,
    )
    protocol_origins = {window: protocol_origins_count(horizon, window) for window in WINDOWS}
    report = {
        "report_id": "AMFE-ENSEMBLE-EVAL-V2",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate": candidate_id,
        "candidate_role": CANDIDATE_ROLES[candidate_id],
        "candidate_registration": _candidate_registration(candidate_id),
        "target": TARGET,
        "horizon": int(horizon),
        "mode": MODE,
        "data_class": "REAL_TIME",
        "protocol": {"file": "protocols/evaluation_protocol_v4.json",
                     "version": PROTOCOL_DOC["version"], "sha256": sha(PROTOCOL)},
        "r3_specification": {"file": "protocols/r3_specification.json",
                             "version": R3_DOC["version"], "sha256": sha(R3)},
        "composition": {"file": COMPOSITION.name, "sha256": sha(COMPOSITION),
                        "members": comparator_members(horizon)},
        "candidates": {"file": CANDIDATES_FILE.name, "sha256": sha(CANDIDATES_FILE)},
        "nf_sim_thresholds": {"file": THRESHOLDS.name, "sha256": sha(THRESHOLDS)},
        "nber_dates": {"file": NBER.name, "sha256": sha(NBER)},
        "comparator_rule": ("arithmetic mean of the point-in-time event probabilities of the frozen "
                            "T3 comparator members (same rule as tools/run_real_evaluation.py)"),
        "real_time_origin_rule": {
            "start": "1996-12-31",
            "rule": ("origins with month >= 1996-12: the ALFRED vintages of GS10/TB3MS start "
                     "1996-12-03 (protocol windows.target_overrides.T3_RECESSION.W1_start, D-131)"),
            "protocol_origins": protocol_origins,
            "used_origins": {window: windows_data[window]["origins"] for window in WINDOWS},
        },
        "windows": {window: _sanitized_window(windows_data[window]) for window in WINDOWS},
        "c6": ("NO_RESOLVED_FORECASTS: the forward registry has no resolved forecast for this "
               "pair; T3 unresolved forecasts do not enter the count and the completeness deadline "
               "delay is stated in every T3 report"),
        "admission": admission,
        "licence_note": LICENCE_NOTE,
        "reporting_only": "harness statuses only; no admission is assigned by this report",
    }
    return report


def build_summary(reports: dict, windows_data: dict, out_paths: dict,
                  candidates=CANDIDATES, horizons=HORIZONS) -> dict:
    """Summary over candidates x horizons: relative losses, p-values, statuses, C4/C6, hashes."""
    pairs = []
    adjusted = {}
    for candidate_id in candidates:
        for horizon in horizons:
            report = reports[(candidate_id, horizon)]
            admission = report["admission"]
            criteria = admission["criteria"]
            per_window = {}
            for window in WINDOWS:
                per_window[window] = {}
                for loss in LOSSES:
                    c1 = criteria["C1"]["windows"][window][loss]
                    c2 = criteria["C2"]["windows"][window][loss]
                    c5 = criteria["C5"]["windows"][window][loss]
                    per_window[window][loss] = {
                        "relative_loss": c1["relative_loss"],
                        "candidate_loss": c1["candidate_loss"],
                        "comparator_loss": c1["comparator_loss"],
                        "threshold": c1["threshold"],
                        "c1_pass": c1["pass"],
                        "c2_p_value": c2["p_value"],
                        "c2_adjusted_p_value": c2.get("adjusted_p_value"),
                        "c5_p_value": c5["p_value"],
                        "c5_ok": bool(c5["p_value"] >= evaluate.ALPHA),
                    }
            pairs.append({
                "candidate": candidate_id,
                "candidate_role": CANDIDATE_ROLES[candidate_id],
                "horizon": int(horizon),
                "origins": {window: windows_data[candidate_id][horizon][window]["origins"]
                            for window in WINDOWS},
                "protocol_origins": {window: windows_data[candidate_id][horizon][window]
                                     ["protocol_origins"] for window in WINDOWS},
                "member_failures": {window: windows_data[candidate_id][horizon][window]
                                    ["member_failures"] for window in WINDOWS},
                "candidate_failures": {window: windows_data[candidate_id][horizon][window]
                                       ["candidate_failures"] for window in WINDOWS},
                "actual_missing": {window: windows_data[candidate_id][horizon][window]
                                   ["actual_missing"] for window in WINDOWS},
                "pseudo_origins_skipped": {window: windows_data[candidate_id][horizon][window]
                                           ["pseudo_origins_skipped"] for window in WINDOWS},
                "data_class": report["data_class"],
                "status": admission["status"],
                "admission_allowed": admission["admission_allowed"],
                "C1_pass": criteria["C1"]["pass"],
                "C2_pass": criteria["C2"]["pass"],
                "C2_adjusted_p_worst": max(
                    value for window in per_window for loss in LOSSES
                    for value in [per_window[window][loss].get("c2_adjusted_p_value") or 0.0]),
                "C4": {"applies": criteria["C4"]["applies"], "pass": criteria["C4"]["pass"],
                       "reason": criteria["C4"].get("reason")},
                "C5_pass": criteria["C5"]["pass"],
                "C6": {"status": criteria["C6"]["status"], "pass": criteria["C6"]["pass"],
                       "reason": criteria["C6"].get("reason")},
                "per_window": per_window,
                "report_file": out_paths[(candidate_id, horizon)],
            })
            adjusted[f"{candidate_id}|h{horizon}"] = admission["multiplicity"][
                "adjusted_p_values"]
    return {
        "report_id": "AMFE-ENSEMBLE-T3-ADMISSION",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": TARGET,
        "horizons": list(horizons),
        "candidates": list(candidates),
        "data_class": "REAL_TIME",
        "protocol": {"file": "protocols/evaluation_protocol_v4.json",
                     "version": PROTOCOL_DOC["version"], "sha256": sha(PROTOCOL)},
        "frozen_artefacts": {
            "protocols/evaluation_protocol_v4.json": sha(PROTOCOL),
            "protocols/r3_specification.json": sha(R3),
            "protocols/comparator_composition_2026-11-11.json": sha(COMPOSITION),
            "protocols/candidates_2026-11-11.json": sha(CANDIDATES_FILE),
            "protocols/nf_sim_thresholds.json": sha(THRESHOLDS),
            "protocols/nber_dates.json": sha(NBER),
        },
        "composition": {"file": COMPOSITION.name, "sha256": sha(COMPOSITION),
                        "members": {str(horizon): comparator_members(horizon)
                                    for horizon in horizons},
                        "losses": list(LOSSES)},
        "losses": list(LOSSES),
        "multiplicity": {
            "method": evaluate.RW_METHOD,
            "family": list(candidates),
            "family_size": len(candidates),
            "fwer": evaluate.ALPHA,
            "replications": evaluate.DEFAULT_RW_REPLICATIONS,
            "seed": evaluate.DEFAULT_RW_SEED,
            "note": ("one Romano-Wolf family of the two evaluated candidates per (horizon, loss) "
                     "and window; both v2 reports publish the same adjusted p-values"),
            "adjusted_p_values": adjusted,
        },
        "c4": {"applies": False, "pass": True,
               "reason": "protocol targets.T3_RECESSION.c4_not_applicable: C4 does not apply to T3"},
        "c6": ("NO_RESOLVED_FORECASTS for every pair: the forward registry has no resolved forecast; "
               "no stage above CANDIDATE/SHADOW_TEST is reachable from this evaluation alone"),
        "pairs": pairs,
        "limits": [
            "T3 has about eight post-war recessions, so the power of every test is low; the "
            "protocol ceiling for T3 is LIMITED_DEPLOYMENT",
            "C6 is NO_RESOLVED_FORECASTS: the forward registry does not exist yet, so the "
            "SHADOW_TEST -> LIMITED_DEPLOYMENT stage cannot be reached",
            "C4 does not apply to T3 by the protocol (degenerate conditional test)",
            "C2 p-values are Romano-Wolf adjusted over a family of two candidates only; the error "
            "accumulates over (target, horizon, loss) pairs and over W1/W2",
            "statuses are harness results, not an admission decision",
        ],
        "licence_note": LICENCE_NOTE,
        "reporting_only": "harness statuses only; no admission is assigned by this report",
    }


# ------------------------------------------------------------------------------------------ main
def _task_order(candidates, horizons) -> list:
    """Longest jobs first (W2 is longer than W1, the adaptive candidate is the slowest)."""
    tasks = []
    for candidate_id in candidates:
        for horizon in horizons:
            for window in ("W2", "W1"):
                tasks.append((candidate_id, int(horizon), window))
    return tasks


def run(tasks: list, jobs: int, max_origins: int | None, allow_pseudo: bool,
        verify_members: int) -> dict:
    payloads = [{"key": f"{candidate}|h{horizon}|{window}", "target": TARGET,
                 "horizon": horizon, "window": window, "candidate": candidate,
                 "mode": MODE, "max_origins": max_origins, "allow_pseudo": allow_pseudo,
                 "verify_members": verify_members}
                for candidate, horizon, window in tasks]
    results = {}
    if jobs and jobs > 1 and len(payloads) > 1:
        context = mp.get_context("spawn")
        with context.Pool(processes=min(int(jobs), len(payloads))) as pool:
            for item in pool.imap_unordered(_worker_task, payloads, chunksize=1):
                results[item["key"]] = item["result"]
                print(f"[t3] done {item['key']}", flush=True)
    else:
        for payload in payloads:
            item = _worker_task(payload)
            results[item["key"]] = item["result"]
            print(f"[t3] done {payload['key']}", flush=True)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="T3 admission evaluation on protocol v4 (W1+W2)")
    parser.add_argument("--candidates", default=",".join(CANDIDATES))
    parser.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    parser.add_argument("--max-origins", type=int, default=None,
                        help="limit origins per window (smoke test only; default: all origins)")
    parser.add_argument("--jobs", type=int, default=min(8, max(1, mp.cpu_count() - 1)),
                        help="parallel worker processes (0 or 1 = sequential)")
    parser.add_argument("--verify-members", type=int, default=2,
                        help="origins per window where the comparator members are re-computed "
                             "directly with amfe.forecast and compared (0 disables)")
    parser.add_argument("--allow-pseudo", action="store_true",
                        help="include PSEUDO_REAL_TIME origins (the admission guard will then refuse)")
    parser.add_argument("--out-dir", default=str(ROOT / "protocols"))
    parser.add_argument("--summary", default=None,
                        help="summary path (default: <out-dir>/ensemble_t3_report.json)")
    parser.add_argument("--no-write", action="store_true",
                        help="compute only, do not write the reports (determinism checks)")
    args = parser.parse_args(argv)

    candidates = tuple(item.strip() for item in args.candidates.split(",") if item.strip())
    horizons = tuple(int(item) for item in args.horizons.split(",") if item.strip())
    unknown = sorted(set(candidates) - set(CANDIDATE_ROLES))
    if unknown:
        raise SystemExit(f"unknown candidates: {unknown}")
    if args.max_origins is not None and args.max_origins < 1:
        raise SystemExit("--max-origins must be a positive integer")

    out_dir = Path(args.out_dir)
    tasks = [(candidate, horizon, window) for candidate in candidates for horizon in horizons
             for window in WINDOWS]
    order = {key: index for index, key in enumerate(_task_order(candidates, horizons))}
    tasks.sort(key=lambda task: order.get((task[0], task[1], task[2]), 99))
    started = datetime.now(timezone.utc)
    results = run(tasks, args.jobs, args.max_origins, args.allow_pseudo, args.verify_members)
    windows_data = {candidate: {horizon: {} for horizon in horizons} for candidate in candidates}
    for candidate, horizon, window in tasks:
        windows_data[candidate][horizon][window] = results[
            f"{candidate}|h{horizon}|{window}"]

    out_paths = {}
    reports = {}
    for candidate in candidates:
        for horizon in horizons:
            report = build_report(candidate, horizon, windows_data[candidate][horizon],
                                  windows_data, candidates=candidates)
            reports[(candidate, horizon)] = report
            out_paths[(candidate, horizon)] = (f"protocols/ensemble_eval_{candidate}_T3_"
                                               f"h{horizon}_v2.json")
    summary = build_summary(reports, windows_data, out_paths, candidates=candidates,
                            horizons=horizons)
    summary_path = Path(args.summary) if args.summary else out_dir / "ensemble_t3_report.json"

    if not args.no_write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for (candidate, horizon), report in reports.items():
            path = out_dir / Path(out_paths[(candidate, horizon)]).name
            path.write_text(json.dumps(report, ensure_ascii=False, indent=1,
                                       default=_json_default) + "\n",
                            encoding="utf-8", newline="\n")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1,
                                           default=_json_default) + "\n",
                                encoding="utf-8", newline="\n")
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(json.dumps({
        "elapsed_seconds": round(elapsed, 1),
        "jobs": args.jobs,
        "max_origins": args.max_origins,
        "pairs": [{"candidate": pair["candidate"], "horizon": pair["horizon"],
                   "status": pair["status"], "C1": pair["C1_pass"], "C2": pair["C2_pass"],
                   "C5": pair["C5_pass"], "C6": pair["C6"]["status"],
                   "relative_loss": {window: {loss: pair["per_window"][window][loss]
                                              ["relative_loss"]
                                              for loss in LOSSES} for window in WINDOWS}}
                  for pair in summary["pairs"]],
        "summary": str(summary_path) if not args.no_write else None,
        "reports": sorted(out_paths.values()) if not args.no_write else None,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
