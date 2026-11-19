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

``--cache-dir`` enables the deterministic forecast cache (tools/eval_cache.py): predictions are
memoised under a PIT/version-safe content key (prediction identity + frozen versions + forecast-code
digest + digest of the *actual* data root + run channel/data class), so a repeated run reuses the
same precomputed forecasts and produces exactly the same report; entries written for another data
root, channel or data class (e.g. primary/W1_EXT reconstructed vs standard/data/raw) are never
replayed - they are reported on stderr, and ``--cache-strict`` turns that mismatch into a hard
failure; ``--max-origins`` limits the origins per window for benchmarks/debug runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.forecast import forecast  # noqa: E402
from amfe.forecast.data import as_date, load_latest, month_index, usrec_event  # noqa: E402
from amfe.candidates.models import (CANDIDATE_DESCRIPTION, CANDIDATE_IDS,  # noqa: E402
                                    candidate_forecast as rw_ar_candidate)
from amfe.ensemble import ENSEMBLE_IDS  # noqa: E402
from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate  # noqa: E402
try:
    from spf_augmented_candidate import (  # noqa: E402
        CANDIDATE_IDS as SPF_CANDIDATE_IDS, candidate_forecast as spf_candidate_forecast)
except Exception:  # pragma: no cover - tool not importable
    SPF_CANDIDATE_IDS = ()
    spf_candidate_forecast = None
try:
    from amfe.ensemble import regime as regime_candidate  # noqa: E402
    REGIME_MODULES = {regime_candidate.CANDIDATE_ID: regime_candidate}
    try:
        from amfe.ensemble import regime_v2 as regime_candidate_v2  # noqa: E402
        REGIME_MODULES[regime_candidate_v2.CANDIDATE_ID] = regime_candidate_v2
    except Exception:  # pragma: no cover - module not importable
        pass
    try:
        from amfe.ensemble import regime_v3 as regime_candidate_v3  # noqa: E402
        REGIME_MODULES[regime_candidate_v3.CANDIDATE_ID] = regime_candidate_v3
    except Exception:  # pragma: no cover - module not importable
        pass
    try:
        from amfe.ensemble import regime_v4 as regime_candidate_v4  # noqa: E402
        REGIME_MODULES[regime_candidate_v4.CANDIDATE_ID] = regime_candidate_v4
    except Exception:  # pragma: no cover - module not importable
        pass
    REGIME_IDS = tuple(REGIME_MODULES)
except Exception:  # pragma: no cover - module not importable
    REGIME_MODULES = {}
    REGIME_IDS = ()



def candidate_forecast(candidate_id, target, origin, horizon, series_id=None, mode="auto"):
    """Dispatcher: ensemble candidates come from amfe.ensemble, the rest from amfe.candidates."""
    if candidate_id in ENSEMBLE_IDS:
        return ensemble_candidate(candidate_id, target, origin, horizon, series_id=series_id, mode=mode)
    if REGIME_IDS and candidate_id in REGIME_IDS:
        module = REGIME_MODULES[candidate_id]
        return module.candidate_forecast(candidate_id, target, origin, horizon,
                                         series_id=series_id, mode=mode)
    if SPF_CANDIDATE_IDS and candidate_id in SPF_CANDIDATE_IDS:
        return spf_candidate_forecast(candidate_id, target, origin, horizon,
                                      series_id=series_id, mode=mode)
    return rw_ar_candidate(candidate_id, target, origin, horizon, series_id=series_id, mode=mode)
from amfe.evaluation.actuals import actual_pair  # noqa: E402
from amfe.evaluation.distributions import mixture_quantiles  # noqa: E402
import evaluate  # noqa: E402
import eval_cache  # noqa: E402
import windows  # noqa: E402

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
R3 = ROOT / "protocols" / "r3_specification.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
CANDIDATES = ROOT / "protocols" / "candidates_2026-11-11.json"
RECONSTRUCTION_DOSSIER = ROOT / "protocols" / "pit_reconstruction.json"
# origins before the first real ALFRED vintage are reconstructed (v5, D-151)
FIRST_REAL_VINTAGE = "1996-12-31"


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
    """Recession mask for C4: the ORIGIN MONTH (protocol v4/v5 C4 text, B-1 docs/111).

    The previous implementation used the event window (t, t+h] for h>0; the protocol text says
    "the origin month is an NBER recession month", so the mask is the origin month for every
    horizon.  The horizon parameter is kept for call compatibility and reported as mask_rule.
    """
    usrec = load_latest("USREC")
    values = usrec.values_dict()
    mask = []
    for origin in origins:
        ym = month_index(origin)
        mask.append(1 if values.get(ym, 0.0) >= 0.5 else 0)
    return mask


MASK_RULE = "origin month (NBER recession month of the origin; not the (t,t+h] window)"


def _is_reconstructed_root() -> bool:
    """True when AMFE_DATA_ROOT points at the reconstructed data root (fail-closed checks)."""
    root = os.environ.get("AMFE_DATA_ROOT")
    if not root:
        return False
    path = Path(root)
    if "reconstructed" in path.name.lower():
        return True
    manifest = path / "manifest.json"
    if manifest.exists():
        try:
            return bool(json.loads(manifest.read_text(encoding="utf-8")).get("reconstructed"))
        except (ValueError, OSError):
            return False
    return False


def build_window(target: str, horizon: int, window: str, candidate_id: str, series_id,
                 mode: str, allow_pseudo: bool = False, cache=None, max_origins=None,
                 protocol_path: Path | None = None, origin_floor: str | None = None) -> dict:
    protocol = json.loads((protocol_path or PROTOCOL).read_text(encoding="utf-8"))
    composition = json.loads(COMPOSITION.read_text(encoding="utf-8"))
    rows = windows.origins_for(protocol, target, horizon, window)
    origins = [r["origin"] for r in rows]
    if origin_floor is not None:
        origins = [o for o in origins if o >= origin_floor]
    if max_origins is not None:
        origins = origins[:int(max_origins)]
    member_ids = members_for(composition, target, "CRPS" if target != "T3_RECESSION" else "Brier", horizon)
    cand_points, cand_quants = [], []
    comp_points, comp_quants = [], []
    actual_primary, actual_secondary = [], []
    data_classes = set()
    vintage_dates = set()
    used_origins = []
    member_failures = 0
    pseudo_skipped = 0

    def predict(kind: str, name: str, compute):
        """Cache-aware prediction call (identity + version key, see tools/eval_cache.py)."""
        if cache is None:
            return compute()
        payload, _hit = cache.forecast(
            compute, kind=kind, name=name, target=target, horizon=horizon, origin=origin,
            series_id=series_id, mode=mode)
        return payload

    for origin in origins:
        try:
            if candidate_id in REGIME_IDS:
                compose = REGIME_MODULES[candidate_id].compose(predict)
                cand = predict("candidate", candidate_id,
                               lambda: compose(origin, horizon, series_id, mode))
            else:
                cand = predict("candidate", candidate_id, lambda: candidate_forecast(
                    candidate_id, target, origin, horizon, series_id=series_id, mode=mode))
        except Exception:
            continue
        actual = actual_pair(target, origin, horizon, series_id=series_id)
        if actual["primary"] is None:
            continue
        member_forecasts = []
        for member in member_ids:
            try:
                member_forecasts.append(predict("model", member, lambda m=member: forecast(
                    m, target, origin, horizon, series_id=series_id, mode=mode).to_dict()))
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
        "origin_floor": origin_floor,
        "used_origins": used_origins,
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
    """Report wrapper: derived metadata and digests only, never raw arrays or third-party values."""
    out = {k: v for k, v in window.items()
           if k not in ("actual_primary", "actual_secondary", "recession", "candidate", "comparator")}
    for key in ("actual_primary", "actual_secondary"):
        if key in window:
            out[f"{key}_sha256"] = _array_digest(window[key])
            out[f"{key}_n"] = int(np.asarray(window[key]).size)
    if "recession" in window:
        out["recession_mask_sha256"] = _array_digest(window["recession"])
    for role in ("candidate", "comparator"):
        block = window.get(role) or {}
        clean = {}
        for name, values in block.items():
            clean[f"{name}_sha256"] = _array_digest(values)
            clean[f"{name}_n"] = int(np.asarray(values).size)
        out[role] = clean
    out["licence_note"] = ("forecasts and actual values are not stored: only digests and derived metrics "
                           "(docs/84, D-147)")
    return out


def _subset_window(window: dict, keep_idx) -> dict:
    """Subset one built window to the given origin indices (v5 REAL_TIME-only slice)."""
    keep = np.asarray(list(keep_idx), dtype=int)
    n = len(window.get("used_origins", []))
    out = dict(window)

    def cut(values):
        arr = np.asarray(values)
        return arr[keep] if arr.ndim >= 1 and arr.shape[0] == n else values

    for key in ("actual_primary", "actual_secondary", "recession"):
        if key in window:
            out[key] = cut(window[key])
    for role in ("candidate", "comparator"):
        block = window.get(role) or {}
        out[role] = {name: cut(values) for name, values in block.items()}
    out["origins"] = int(keep.size)
    out["used_origins"] = [o for i, o in enumerate(window.get("used_origins", [])) if i in set(keep.tolist())]
    out["first_origin"] = out["used_origins"][0] if out["used_origins"] else None
    out["last_origin"] = out["used_origins"][-1] if out["used_origins"] else None
    return out


def _episode_losses(window: dict, target: str, horizon: int) -> dict:
    """Per-episode and leave-one-episode-out relative losses (aggregates only, v5 C4 sensitivity)."""
    import evaluate as _ev

    episodes = {
        "1990_91": ("1990-08-01", "1991-03-31"),
        "2001": ("2001-04-01", "2001-11-30"),
        "2007_09": ("2007-12-01", "2009-06-30"),
    }
    origins = [as_date(o) for o in window.get("used_origins", [])]
    if not origins:
        return {}
    y = np.asarray(window["actual_primary"], dtype=float)
    cand_block = window["candidate"]
    comp_block = window["comparator"]
    out = {}
    for name, (start, end) in episodes.items():
        idx = [i for i, d in enumerate(origins) if start <= d.isoformat() <= end]
        if not idx:
            continue
        per_loss = {}
        for loss in _ev.loss_names(target):
            cand = _ev.observation_loss(loss, y, point=cand_block.get("point"),
                                        quantiles=cand_block.get("quantiles"),
                                        probability=cand_block.get("probability"))
            comp = _ev.observation_loss(loss, y, point=comp_block.get("point"),
                                        quantiles=comp_block.get("quantiles"),
                                        probability=comp_block.get("probability"))
            csum = float(np.sum(cand[idx]))
            compsum = float(np.sum(comp[idx]))
            ratio = (csum / compsum) if compsum > 0 else None
            mask = np.ones(len(origins), dtype=bool)
            mask[idx] = False
            csum_loo = float(np.sum(cand[mask]))
            compsum_loo = float(np.sum(comp[mask]))
            loo = (csum_loo / compsum_loo) if compsum_loo > 0 else None
            per_loss[loss] = {"origins": len(idx), "relative_loss_episode": ratio,
                              "relative_loss_leave_one_out": loo}
        out[name] = per_loss
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
    ap.add_argument("--cache-dir", default=None,
                    help="forecast cache directory (tools/eval_cache.py); off by default")
    ap.add_argument("--cache-strict", action="store_true",
                    help="fail when the cache holds entries of another data root/channel/class "
                         "instead of ignoring them with an explicit warning")
    ap.add_argument("--max-origins", type=int, default=None,
                    help="limit origins per window (benchmark/debug; default: all origins)")
    ap.add_argument("--protocol", default=str(PROTOCOL),
                    help="protocol file (default v4; v5 adds the reconstructed C4 window)")
    ap.add_argument("--data-root", default=None,
                    help="data root (sets AMFE_DATA_ROOT); reconstructed root for v5")
    ap.add_argument("--data-class", default=None,
                    help="report-level data class override (v5: REAL_TIME_RECONSTRUCTED)")
    ap.add_argument("--extended-c4", action="store_true",
                    help="v5: C4 on windows.W1_EXT (reconstructed origins), C1/C2/C5 on REAL_TIME only")
    ap.add_argument("--c4-min-recession", type=int, default=None,
                    help="override the protocol's minimum recession origins for C4")
    ap.add_argument("--skip-candidate-registration", action="store_true",
                    help="do not rewrite protocols/candidates_2026-11-11.json")
    ap.add_argument("--candidates-doc", default=None,
                    help="candidate registration document used for the report hash")
    args = ap.parse_args(argv)
    if args.max_origins is not None and args.max_origins < 1:
        raise SystemExit("--max-origins must be a positive integer")
    if args.cache_strict and not args.cache_dir:
        raise SystemExit("--cache-strict requires --cache-dir")
    if args.data_root:
        os.environ["AMFE_DATA_ROOT"] = str(Path(args.data_root).resolve())
    protocol_path = Path(args.protocol)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol_label = f"protocols/{protocol_path.name}"
    cache = None
    if args.cache_dir:
        # the key must cover the root/channel/class the forecast code actually reads (task-68)
        channel = "primary" if args.extended_c4 else "standard"
        requested_class = args.data_class or ("REAL_TIME_RECONSTRUCTED"
                                              if channel == "primary" else "REAL_TIME")
        cache = eval_cache.ForecastCache(Path(args.cache_dir), channel=channel,
                                         data_class=requested_class,
                                         strict=args.cache_strict)
        try:
            provenance = cache.preflight()
        except eval_cache.CacheProvenanceError as exc:
            raise SystemExit(f"cache provenance: {exc}")
        if not provenance["ok"]:
            print(json.dumps({"cache_provenance": provenance}, ensure_ascii=False),
                  file=sys.stderr)
    if (args.candidate not in CANDIDATE_IDS and args.candidate not in ENSEMBLE_IDS
            and args.candidate not in (SPF_CANDIDATE_IDS or ())
            and args.candidate not in REGIME_IDS):
        raise SystemExit(f"unknown candidate {args.candidate}")
    if not args.skip_candidate_registration:
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
        if CANDIDATES.exists():
            existing = json.loads(CANDIDATES.read_text(encoding="utf-8"))
            known = {c["id"] for c in existing.get("candidates", [])}
            merged = list(existing.get("candidates", []))
            for cand in candidate_doc["candidates"]:
                if cand["id"] not in known:
                    merged.append(cand)
            existing["candidates"] = merged
            existing["registered_on"] = existing.get("registered_on", candidate_doc["registered_on"])
            existing["updated_on"] = "2026-11-12"
            candidate_doc = existing
        CANDIDATES.write_text(json.dumps(candidate_doc, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8", newline="\n")
    if args.extended_c4 and "W1_EXT" not in protocol.get("windows", {}):
        raise SystemExit("--extended-c4 requires windows.W1_EXT (protocol v5)")
    reconstructed_root = _is_reconstructed_root()
    if reconstructed_root and args.target == "T2_GDP_GROWTH":
        raise SystemExit("fail-closed: T2 must not be evaluated on the reconstructed data root "
                         "(GDPC1 revisions are material; v5 not_used, B-2 docs/111)")
    if args.extended_c4 and not reconstructed_root:
        raise SystemExit("--extended-c4 requires the reconstructed data root (AMFE_DATA_ROOT); "
                         "data/raw has no 1990-1996 vintages (fail-closed, B-2 docs/111)")
    w1_name = "W1_EXT" if args.extended_c4 else "W1"
    w1_floor = None
    if reconstructed_root and not args.extended_c4:
        # fail-closed: a reconstructed root may never leak reconstructed origins into C1/C2/C5
        w1_floor = FIRST_REAL_VINTAGE
    w1_full = build_window(args.target, args.horizon, w1_name, args.candidate, args.series_id,
                           args.mode, allow_pseudo=args.allow_pseudo, cache=cache,
                           max_origins=None, protocol_path=protocol_path,
                           origin_floor=w1_floor)
    if args.extended_c4:
        w1_full["data_class"] = "REAL_TIME_RECONSTRUCTED"
        keep = [i for i, o in enumerate(w1_full["used_origins"]) if o >= FIRST_REAL_VINTAGE]
        w1 = _subset_window(w1_full, keep)
        w1["window"] = "W1"  # the REAL_TIME slice is reported under its own name
        # the slice keeps only REAL_TIME origins, so its own data class is REAL_TIME; the run-level
        # class (REAL_TIME_RECONSTRUCTED) describes the W1_EXT block, not this window (V2 docs/172 §3)
        w1["data_class"] = "REAL_TIME"
        w1["origin_floor"] = FIRST_REAL_VINTAGE
        reconstructed_origins = int(w1_full["origins"] - w1["origins"])
    else:
        w1 = w1_full
        reconstructed_origins = 0
    w2 = build_window(args.target, args.horizon, "W2", args.candidate, args.series_id, args.mode,
                      allow_pseudo=args.allow_pseudo, cache=cache, max_origins=None,
                      protocol_path=protocol_path)
    if args.max_origins:
        n = int(args.max_origins)
        w1 = _subset_window(w1, range(min(n, w1["origins"])))
        w2 = _subset_window(w2, range(min(n, w2["origins"])))
        if args.extended_c4:
            w1_full = _subset_window(w1_full, range(min(n, w1_full["origins"])))
        reconstructed_origins = int(w1_full["origins"] - w1["origins"]) if args.extended_c4 else 0
    if w1["origins"] == 0 or w2["origins"] == 0:
        raise SystemExit(
            f"empty evaluation window (W1={w1['origins']}, W2={w2['origins']}): check the data root "
            f"(AMFE_DATA_ROOT={os.environ.get('AMFE_DATA_ROOT')}) and the origin floor; "
            "for the extended C4 window the reconstructed data root is required")
    if args.data_class:
        data_class = args.data_class
    elif args.extended_c4 and reconstructed_origins:
        data_class = "REAL_TIME_RECONSTRUCTED"
    else:
        data_class = ("REAL_TIME" if w1["data_class"] == w2["data_class"] == "REAL_TIME"
                      else "PSEUDO_REAL_TIME")
    c4_min = args.c4_min_recession
    if c4_min is None:
        c4_min = int(protocol.get("admission", {}).get("c4_window_and_minimum", {})
                     .get("min_recession_months")
                     or protocol.get("admission", {}).get("recession_conditioning", {})
                     .get("min_recession_months", 12))
    admission_classes = (protocol.get("admission", {}).get("admission_classes")
                         or protocol.get("data_depth", {}).get("reconstruction", {})
                         .get("admission_classes"))
    admission_classes = tuple(admission_classes) if admission_classes else None
    reconstruction = {"extended_c4": bool(args.extended_c4),
                      "first_real_vintage": FIRST_REAL_VINTAGE,
                      "mask_rule": MASK_RULE,
                      "w1_origins_total": int(w1_full["origins"]),
                      "w1_origins_real_time": int(w1["origins"]),
                      "w1_origins_reconstructed": int(reconstructed_origins)}
    if RECONSTRUCTION_DOSSIER.exists():
        reconstruction["dossier"] = {"file": f"protocols/{RECONSTRUCTION_DOSSIER.name}",
                                     "sha256": sha(RECONSTRUCTION_DOSSIER)}
    if RECONSTRUCTION_DOSSIER.with_suffix(".sha256").exists():
        reconstruction["dossier_pin"] = {
            "file": f"protocols/{RECONSTRUCTION_DOSSIER.stem}.sha256",
            "sha256": sha(RECONSTRUCTION_DOSSIER.with_suffix(".sha256"))}
    candidates_doc = Path(args.candidates_doc) if args.candidates_doc else CANDIDATES
    report = {
        "report_id": "AMFE-FIRST-REAL-EVALUATION",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate": args.candidate,
        "target": args.target,
        "horizon": args.horizon,
        "mode": args.mode,
        "data_class": data_class,
        "conditional_data_class": bool(args.extended_c4),
        "re_evaluation_under_changed_protocol": bool(args.extended_c4),
        "protocol": {"file": protocol_label, "version": protocol["version"],
                     "sha256": sha(protocol_path)},
        "r3_specification": {"file": "protocols/r3_specification.json",
                             "version": json.loads(R3.read_text(encoding="utf-8"))["version"],
                             "sha256": sha(R3)},
        "composition": {"file": COMPOSITION.name, "sha256": sha(COMPOSITION)},
        "candidates": {"file": f"protocols/{candidates_doc.name}",
                       "sha256": sha(candidates_doc)},
        "reconstruction": reconstruction,
        "windows": {w["window"]: _sanitized_window(w) for w in ([w1_full] if args.extended_c4 else [])
                    + [w1, w2]},
        "c6": "not available on the first day: the forward registry has no resolved forecasts yet",
        "admission": evaluate.evaluate_admission(
            args.target, args.horizon,
            {"W1": {k: w1[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator")},
             "W2": {k: w2[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator")}},
            data_class=data_class, min_recession_origins=c4_min,
            admission_classes=admission_classes, protocol_label=protocol_label,
            conditional_data_class=bool(args.extended_c4)),
    }
    # C4 needs the recession mask per window; recompute the admission report with it.
    # T3 gates on no C4 (protocol: targets.T3_RECESSION.c4_not_applicable) and build_window()
    # attaches no recession mask for it, so the mask block exists only for C4-gating targets;
    # building it unconditionally raised KeyError('recession') on every T3 run.
    c4_windows = {"W1": w1_full} if args.extended_c4 else None
    if args.target != "T3_RECESSION":
        windows_with_mask = {
            "W1": {k: w1[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator",
                                      "recession")},
            "W2": {k: w2[k] for k in ("actual_primary", "actual_secondary", "candidate", "comparator",
                                      "recession")},
        }
        family = {}  # the evaluated candidate is added under candidate_id; other candidates go here
        report["admission"] = evaluate.evaluate_admission(
            args.target, args.horizon, windows_with_mask, data_class=data_class,
            family=family, candidate_id=args.candidate, min_recession_origins=c4_min,
            admission_classes=admission_classes, protocol_label=protocol_label,
            c4_windows=c4_windows, conditional_data_class=bool(args.extended_c4))
    report["c4_episodes"] = {
        "W2": _episode_losses(w2, args.target, args.horizon),
    }
    if args.extended_c4:
        report["c4_episodes"]["W1_EXT"] = _episode_losses(w1_full, args.target, args.horizon)
        # C4 on the REAL_TIME-only part of the extended window, recomputed inside this run: the
        # preregistration sensitivity slice must be its own calculation, not an alias of the
        # standard (non-extended) run and not a copy of the W1_EXT block (docs/172 §4).
        if args.target != "T3_RECESSION":
            slice_block = evaluate.evaluate_window(
                "W1", args.target, args.horizon, w1["actual_primary"], w1["actual_secondary"],
                candidate=w1["candidate"], comparator=w1["comparator"],
                recession_mask=w1["recession"], min_recession_origins=c4_min)
            report["c4_slices"] = {"real_time_window": {
                "window": "W1",
                "data_class": "REAL_TIME",
                "origins": int(w1["origins"]),
                "first_origin": w1["first_origin"],
                "origin_floor": FIRST_REAL_VINTAGE,
                "recession_origins": int(np.count_nonzero(w1["recession"])),
                "losses": slice_block.get("recession") or {},
                "note": "C4 recomputed inside the extended run on the REAL_TIME origins only "
                        "(origins >= the first real vintage); it does not reuse the standard v5 run",
            }}
    def _json_default(obj):
        import numpy as _np

        if isinstance(obj, _np.ndarray):
            return obj.tolist()
        if isinstance(obj, (_np.floating, _np.integer)):
            return obj.item()
        raise TypeError(f"not JSON serializable: {type(obj)!r}")

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1,
                                         default=_json_default) + "\n",
                              encoding="utf-8", newline="\n")
    adm = report["admission"]
    summary = {"candidate": args.candidate, "target": args.target, "horizon": args.horizon,
               "data_class": data_class, "status": adm["status"],
               "status_label": adm.get("status_label"),
               "conditional_data_class": adm.get("conditional_data_class"),
               "w1_origins": w1["origins"], "w2_origins": w2["origins"],
               "w1_origins_extended": w1_full["origins"],
               "w1_origins_reconstructed": reconstructed_origins,
               "c4_min_recession_origins": c4_min,
               "c1": {loss: adm["criteria"]["C1"]["windows"]["W1"][loss]["pass"]
                      for loss in adm["criteria"]["C1"]["windows"]["W1"]},
               "c2": adm["criteria"]["C2"]["pass"], "c4": adm["criteria"]["C4"]["pass"],
               "c5": adm["criteria"]["C5"]["pass"], "out": args.out}
    if cache is not None:
        summary["cache"] = cache.stats()
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
