#!/usr/bin/env python3
"""SPF sensitivity analysis in the T2 (real GDP growth) scoring (docs/95, stage 7).

Question
--------
The registered T2 ensemble candidates were evaluated **without** the third-party SPF member
(docs/91 §7: the external member never entered the T2 ensemble run). The SPF mapping is now
confirmed (docs/86: T2 h=0/1/2/4 -> drgdp2/3/4/6, availability by the official news-release date
through ``amfe.ensemble.spf_calendar``). This tool measures how much the T2 result changes when the
SPF median enters the ensemble as an external member, and separates the SPF contribution from the
plain weighting/sample effects.

Three comparisons, all on the protocol v4 windows and REAL_TIME origins
-----------------------------------------------------------------------
``no_spf``            the control ensemble over the internal R3 members only (``ENSEMBLE_EQUAL`` by
                      default; the scheme is a declared control, docs/91).
``with_spf``          the same scheme with the external ``SPF_GDP_MEDIAN`` member added where its
                      median is public at the origin (release date rule, docs/86 §5). The weights are
                      recomputed for the enlarged member set, so this is a weight-refit comparison,
                      not a fixed-weight plug-in.
``paired sensitivity`` ``no_spf`` vs ``with_spf`` on the subsample of origins where the SPF median is
                      available, i.e. the identical-origin comparison that isolates the SPF effect.
                      The full-sample aggregates are reported too, but they mix SPF and non-SPF
                      origins and are therefore diagnostic only.

Losses and inference
--------------------
* RMSE (protocol point loss) is the primary loss; the report is valid without any mixture.
* CRPS (protocol 19-quantile pinball loss) is computed **only if the ensemble mixture is sane on the
  scored origins**: docs/91 found extreme T2 mixture quantiles (q95 up to ~1.9e4 pp) and declared T2
  CRPS unusable. The tool re-checks the mixtures (monotonicity, finite, deviation from the point
  forecast and absolute size). If the check fails, CRPS is ``NOT_AVAILABLE`` with the evidence and
  RMSE is the only reported result; once the mixture fix lands the same code reports CRPS.
* Diebold-Mariano (HAC h-1, HLN) on the paired loss differential ``loss(with_spf) - loss(no_spf)``,
  one-sided H1: the SPF member improves the loss.
* Romano-Wolf stepdown over the family of losses (RMSE and, when usable, CRPS) on the same subset,
  stationary bootstrap with a fixed seed and the protocol FWER (0.10).

Licence and publication rule
----------------------------
The SPF median values are third-party data. They live only in the git-ignored
``data/raw/spf/spf_median.json`` and are **never written to this report**: the report stores counts,
availability verdicts, losses, p-values, digests and the required Philadelphia Fed attribution, in
line with ``protocols/licence_register.json`` (PHILADELPHIA_FED, research use) and docs/84.

If the local values are absent (fresh CI checkout), the tool honestly reports ``NOT_AVAILABLE`` with
the reason and the exact way to obtain them (the ``r2-metadata`` workflow artifact); it never
fabricates an SPF series.

Usage
-----
    python tools/spf_t2_sensitivity.py --check                     # fast availability/plan summary
    python tools/spf_t2_sensitivity.py --horizons 0 1              # full run (T2 h=0 and h=1)
    python tools/spf_t2_sensitivity.py --horizons 0 --max-origins 24 --out <tmp>.json

The runner is deterministic for the same data/code/seed (``generated_at`` and the bootstrap are the
only time/seed-dependent parts; the bootstrap seed is fixed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.ensemble.ensemble import (  # noqa: E402
    ENSEMBLE_EQUAL,
    ENSEMBLE_IDS,
    combine_member_forecasts,
    ensemble_forecast,
)
from amfe.ensemble.history import current_member_parts, default_member_ids  # noqa: E402
from amfe.ensemble.spf_calendar import (  # noqa: E402
    is_real_time_quarter,
    release_date as spf_release_date,
    SPF_MIDDLE_MONTHS,
)
from amfe.ensemble.sources import (  # noqa: E402
    SPF_ATTRIBUTION,
    load_spf_medians,
    spf_available,
    spf_forecast,
    spf_values_path,
    survey_quarter_for_origin,
    survey_quarter_label,
)
from amfe.evaluation.actuals import actual_pair  # noqa: E402
import evaluate  # noqa: E402
import windows  # noqa: E402

TOOL = "tools/spf_t2_sensitivity.py"
REPORT_ID = "AMFE-SPF-T2-SENSITIVITY"
VERSION = "0.1.0"
TARGET = "T2_GDP_GROWTH"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
R3 = ROOT / "protocols" / "r3_specification.json"
DEFAULT_OUT = ROOT / "protocols" / "spf_t2_sensitivity_report.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"
DEFAULT_SEED = 20261112
DEFAULT_REPLICATIONS = 2000
#: Mixture sanity limits for the GDP-growth target (annualised percentage points).  The protocol
#: target is a quarterly growth rate: a 50 pp deviation of a 5%/95% quantile from the point forecast
#: is already absurd, and the docs/91 defect produced q95 ~1.9e4.  Any violation switches CRPS off.
MIXTURE_DEVIATION_LIMIT = 50.0
MIXTURE_ABSOLUTE_LIMIT = 1000.0
MIN_PAIRED_ORIGINS = 10
HAC_HORIZON = {0: 3, 1: 1, 2: 1, 4: 1}  # h=0 origins are monthly and overlap 3 months


class SensitivityError(RuntimeError):
    """The sensitivity analysis cannot run (bad arguments, missing inputs)."""


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _digest(values) -> str:
    array = np.asarray(values, dtype=float)
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _round(value, digits: int = 10):
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_length(*named) -> None:
    lengths = {name: np.asarray(values).size for name, values in named}
    if len(set(lengths.values())) > 1:
        raise SensitivityError(f"length mismatch: {lengths}")


# ----------------------------------------------------------------------------------------------
# SPF availability (release-date rule; docs/86 §5)
# ----------------------------------------------------------------------------------------------
def spf_availability(origin, horizon: int, *, data_root=None, medians=None) -> dict:
    """Is the SPF median for the protocol (target, horizon) public at ``origin``?

    h=0 follows the ensemble rule: the origin must be a month-end of the middle month of the survey
    quarter and on/after the official news release date.  h=1 uses the protocol's quarter-end origin:
    the survey of the origin quarter is released in its middle month, which is on or before the
    quarter-end origin, so the ``drgdp3`` forecast for the target quarter is public; the same release
    date rule is applied explicitly.  Before 1990Q3 the SPF was not conducted in real time (docs/86).
    """
    from amfe.forecast.data import as_date, month_end_of

    origin_d = month_end_of(as_date(origin))
    horizon_i = int(horizon)
    code = {"T2_GDP_GROWTH": {0: "drgdp2", 1: "drgdp3", 2: "drgdp4", 4: "drgdp6"}}.get(TARGET, {}).get(horizon_i)
    out = {"origin": origin_d.isoformat(), "horizon": horizon_i, "available": False, "reason": None,
           "code": code, "survey_quarter": None, "release_date": None, "value": None,
           "mapping": "T2 h=0/1/2/4 -> drgdp2/3/4/6 (docs/86)"}
    if code is None:
        out["reason"] = f"NO_SPF_HORIZON_CODE_FOR_H{horizon_i}"
        return out
    if horizon_i == 0:
        survey_quarter = survey_quarter_for_origin(origin_d)
        if survey_quarter is None:
            out["reason"] = "ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH"
            return out
    else:
        survey_quarter = origin_d.year * 4 + (origin_d.month - 1) // 3
    label = survey_quarter_label(survey_quarter)
    out["survey_quarter"] = label
    if not is_real_time_quarter(label):
        out["reason"] = "SPF_NOT_REAL_TIME_BEFORE_1990Q3"
        return out
    release = spf_release_date(label)
    out["release_date"] = release.isoformat()
    if origin_d < release:
        out["reason"] = "SPF_NEWS_RELEASE_NOT_PUBLISHED_AT_ORIGIN"
        return out
    try:
        table = medians if medians is not None else load_spf_medians(data_root)
    except Exception:
        out["reason"] = "SPF_VALUES_NOT_DOWNLOADED"
        return out
    entry = table.get(code, {}).get(label)
    if entry is None:
        out["reason"] = "SURVEY_QUARTER_NOT_IN_SPF_FILE"
        return out
    out["available"] = True
    out["value"] = float(entry["value"])
    return out


def spf_availability_summary(origins, horizon: int, *, data_root=None, medians=None) -> dict:
    """Counts of SPF availability and its reasons over a list of origin dates."""
    reasons: dict[str, int] = {}
    available = []
    for origin in origins:
        verdict = spf_availability(origin, horizon, data_root=data_root, medians=medians)
        if verdict["available"]:
            available.append(verdict["origin"])
        else:
            reasons[str(verdict["reason"])] = reasons.get(str(verdict["reason"]), 0) + 1
    return {"origins": len(available) + sum(reasons.values()), "available": len(available),
            "first_available": available[0] if available else None,
            "last_available": available[-1] if available else None,
            "not_available_reasons": dict(sorted(reasons.items()))}


# ----------------------------------------------------------------------------------------------
# Mixture sanity / CRPS gate
# ----------------------------------------------------------------------------------------------
def mixture_sanity(point: float, quantiles) -> dict:
    """Check one predictive mixture before it may enter a CRPS aggregate.

    The docs/91 defect produced extreme upper quantiles (q95 up to ~1.9e4 while the target is a
    few percent of annualised growth) from the CDF inversion.  A mixture is rejected when it is
    non-finite, non-monotone, deviates from the point forecast by more than
    :data:`MIXTURE_DEVIATION_LIMIT`, or exceeds :data:`MIXTURE_ABSOLUTE_LIMIT` in absolute value.
    """
    stack = np.asarray(quantiles, dtype=float).ravel()
    checks = {"finite": bool(np.all(np.isfinite(stack))), "monotone": True,
              "max_deviation": None, "max_abs": None}
    if not checks["finite"]:
        return {"sane": False, "reason": "NON_FINITE_QUANTILES", **checks}
    checks["monotone"] = bool(np.all(np.diff(stack) >= -1e-9))
    checks["max_deviation"] = float(np.max(np.abs(stack - float(point))))
    checks["max_abs"] = float(np.max(np.abs(stack)))
    if not checks["monotone"]:
        return {"sane": False, "reason": "NON_MONOTONE_QUANTILES", **checks}
    if checks["max_deviation"] > MIXTURE_DEVIATION_LIMIT or checks["max_abs"] > MIXTURE_ABSOLUTE_LIMIT:
        return {"sane": False, "reason": "EXTREME_QUANTILES", **checks}
    return {"sane": True, "reason": None, **checks}


# ----------------------------------------------------------------------------------------------
# Window construction and scoring
# ----------------------------------------------------------------------------------------------
def _ensemble_members(target: str, horizon: int, include_external: bool) -> list:
    return default_member_ids(target, horizon, include_external=include_external)


def _control_variants(origin, horizon: int, internal: list, enlarged: list, data_root=None) -> tuple:
    """Both control-scheme variants from one member-forecast computation.

    ``ENSEMBLE_EQUAL`` over the enlarged member set is identical to ``ensemble_forecast`` for the
    same scheme, but the internal member forecasts are computed once per origin instead of twice.
    The no-SPF variant is the same computation restricted to the internal members, i.e. the weights
    are refit (equal weights) over the reduced set - the comparison is not a fixed-weight plug-in.
    """
    from amfe.forecast.data import month_end_of

    origin_d = month_end_of(origin)
    parts, excluded = current_member_parts(TARGET, origin_d, int(horizon), list(enlarged),
                                           mode="auto", data_root=data_root)
    if not parts:
        raise SensitivityError(f"no member is available at {origin_d.isoformat()}")

    def combine(selected):
        subset = {member: parts[member] for member in selected if member in parts}
        if not subset:
            raise SensitivityError(f"no selected member is available at {origin_d.isoformat()}")
        weight = 1.0 / len(subset)
        return combine_member_forecasts(
            TARGET, origin_d, int(horizon), subset, {member: weight for member in subset},
            ensemble_id=ENSEMBLE_EQUAL,
            excluded_members={member: excluded.get(member, "UNAVAILABLE")
                              for member in selected if member not in subset},
            history_meta={"weight_source": "EQUAL_DECLARED_CONTROL"},
        )

    no_spf = combine(internal)
    if "SPF_GDP_MEDIAN" not in parts:
        return no_spf, no_spf
    return no_spf, combine(enlarged)


def build_window_records(horizon: int, window: str, *, ensemble_id: str = ENSEMBLE_EQUAL,
                         data_root=None, max_origins: int | None = None,
                         need_crps: bool = False) -> dict:
    """Score both variants over one protocol window and return per-origin records (in memory)."""
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    rows = windows.origins_for(protocol, TARGET, horizon, window)
    if max_origins is not None:
        rows = rows[:int(max_origins)]
    internal = _ensemble_members(TARGET, horizon, include_external=False)
    enlarged = _ensemble_members(TARGET, horizon, include_external=True)
    medians = None
    try:
        medians = load_spf_medians(data_root)
    except Exception:
        medians = None
    records = []
    equal_scheme = ensemble_id == ENSEMBLE_EQUAL
    meta = {"window": window, "horizon": horizon, "origins_in_protocol": len(rows),
            "origins_scored": 0, "skipped_no_actual": 0, "skipped_forecast": 0,
            "pseudo_origins_skipped": 0, "spf_available": 0, "spf_unavailable_reasons": {},
            "crps_checked": 0, "crps_rejected": 0, "crps_reject_reasons": {},
            "crps_max_deviation": 0.0, "crps_max_abs": 0.0,
            "data_classes": {}, "first_origin": None, "last_origin": None,
            "internal_members": list(internal), "enlarged_members": list(enlarged)}
    for row in rows:
        origin = row["origin"]
        try:
            actual = actual_pair(TARGET, origin, horizon)
        except Exception:
            meta["skipped_forecast"] += 1
            continue
        if actual["primary"] is None:
            meta["skipped_no_actual"] += 1
            continue
        verdict = spf_availability(origin, horizon, data_root=data_root, medians=medians)
        try:
            if equal_scheme:
                no_spf, with_spf = _control_variants(origin, horizon, internal, enlarged, data_root)
            else:
                no_spf = ensemble_forecast(ensemble_id, TARGET, origin, horizon, member_ids=internal)
                with_spf = None
                if verdict["available"]:
                    try:
                        with_spf = ensemble_forecast(ensemble_id, TARGET, origin, horizon,
                                                     member_ids=enlarged)
                    except Exception:
                        with_spf = None
        except Exception:
            meta["skipped_forecast"] += 1
            continue
        if with_spf is None:
            with_spf = no_spf
        if verdict["available"] and "SPF_GDP_MEDIAN" not in (with_spf.get("member_weights") or {}):
            verdict["available"] = False
            verdict["reason"] = "SPF_MEMBER_EXCLUDED_BY_ENSEMBLE"
            with_spf = no_spf
        data_class = with_spf.get("data_class", no_spf.get("data_class"))
        meta["data_classes"][str(data_class)] = meta["data_classes"].get(str(data_class), 0) + 1
        if data_class != "REAL_TIME":
            meta["pseudo_origins_skipped"] += 1
            continue
        crps_ok = True
        if need_crps:
            meta["crps_checked"] += 1
            sanity = mixture_sanity(with_spf["point"], with_spf["quantiles"])
            meta["crps_max_deviation"] = max(meta["crps_max_deviation"],
                                             sanity["max_deviation"] or 0.0)
            meta["crps_max_abs"] = max(meta["crps_max_abs"], sanity["max_abs"] or 0.0)
            if not sanity["sane"]:
                crps_ok = False
                meta["crps_rejected"] += 1
                reason = str(sanity["reason"])
                meta["crps_reject_reasons"][reason] = meta["crps_reject_reasons"].get(reason, 0) + 1
        spf_point = verdict.get("value") if verdict["available"] else None
        if not verdict["available"]:
            reason = str(verdict["reason"])
            meta["spf_unavailable_reasons"][reason] = meta["spf_unavailable_reasons"].get(reason, 0) + 1
        else:
            meta["spf_available"] += 1
        records.append({
            "origin": origin,
            "target": row["target"],
            "actual": float(actual["primary"]),
            "point_no_spf": float(no_spf["point"]),
            "point_with_spf": float(with_spf["point"]),
            "quantiles_no_spf": None if no_spf.get("quantiles") is None else [float(v) for v in no_spf["quantiles"]],
            "quantiles_with_spf": None if with_spf.get("quantiles") is None else [float(v) for v in with_spf["quantiles"]],
            "spf_available": bool(verdict["available"]),
            "spf_point": spf_point,
            "spf_weight": float((with_spf.get("member_weights") or {}).get("SPF_GDP_MEDIAN", 0.0)),
            "n_members_no_spf": len(no_spf.get("member_weights") or {}),
            "n_members_with_spf": len(with_spf.get("member_weights") or {}),
            "crps_ok": crps_ok,
        })
    meta["origins_scored"] = len(records)
    meta["first_origin"] = records[0]["origin"] if records else None
    meta["last_origin"] = records[-1]["origin"] if records else None
    return {"meta": meta, "records": records}


def _loss_arrays(records: list, loss: str) -> dict:
    actual = np.asarray([r["actual"] for r in records], dtype=float)
    no_spf = np.asarray([r["point_no_spf"] for r in records], dtype=float)
    with_spf = np.asarray([r["point_with_spf"] for r in records], dtype=float)
    if loss == "RMSE":
        return {"no_spf": evaluate.rmse_squared_error(no_spf, actual),
                "with_spf": evaluate.rmse_squared_error(with_spf, actual)}
    quantiles_no = [r["quantiles_no_spf"] for r in records if r["quantiles_no_spf"] is not None]
    quantiles_with = [r["quantiles_with_spf"] for r in records if r["quantiles_with_spf"] is not None]
    if len(quantiles_no) != len(records) or len(quantiles_with) != len(records):
        raise SensitivityError("CRPS requested but some records have no quantiles")
    return {"no_spf": evaluate.crps(np.asarray(quantiles_no, dtype=float), actual),
            "with_spf": evaluate.crps(np.asarray(quantiles_with, dtype=float), actual)}


def summarize_subset(records: list, *, horizon: int, loss_set, seed: int, replications: int,
                     subset: str) -> dict:
    """Aggregate one subset of per-origin records into losses, DM and Romano-Wolf results."""
    result = {"subset": subset, "origins": len(records),
              "first_origin": records[0]["origin"] if records else None,
              "last_origin": records[-1]["origin"] if records else None,
              "losses": {}, "dm": {}, "romano_wolf": None}
    if not records:
        result["status"] = "NOT_AVAILABLE"
        result["reason"] = "NO_ORIGINS"
        return result
    result["status"] = "OK"
    differentials = {}
    for loss in loss_set:
        arrays = _loss_arrays(records, loss)
        aggregate = {"no_spf": evaluate.aggregate_loss(loss, arrays["no_spf"]),
                     "with_spf": evaluate.aggregate_loss(loss, arrays["with_spf"])}
        if loss == "RMSE":
            aggregate["no_spf"] = math.sqrt(aggregate["no_spf"])
            aggregate["with_spf"] = math.sqrt(aggregate["with_spf"])
        aggregate["relative_with_spf"] = (
            aggregate["with_spf"] / aggregate["no_spf"] if aggregate["no_spf"] > 0 else None)
        differential = arrays["with_spf"] - arrays["no_spf"]  # >0: SPF makes the loss larger
        differentials[loss] = differential
        hedge = {"n": int(differential.size), "mean_differential": _round(np.mean(differential)),
                 "digest": _digest(differential)}
        if loss == "RMSE":
            hedge["rmse_improvement_percent"] = _round(
                100.0 * (1.0 - aggregate["with_spf"] / aggregate["no_spf"])) \
                if aggregate["no_spf"] > 0 else None
        result["losses"][loss] = {**aggregate, **hedge,
                                  "no_spf_digest": _digest(arrays["no_spf"]),
                                  "with_spf_digest": _digest(arrays["with_spf"])}
        result["dm"][loss] = evaluate.dm_test(differential, horizon=HAC_HORIZON.get(int(horizon), 1),
                                              alternative="less")
    if records and all(r.get("spf_point") is not None for r in records):
        spf = np.asarray([r["spf_point"] for r in records], dtype=float)
        actual = np.asarray([r["actual"] for r in records], dtype=float)
        result["spf_only"] = {"rmse": _round(math.sqrt(float(np.mean((actual - spf) ** 2)))),
                              "n": int(spf.size), "digest": _digest(spf - actual)}
    if differentials:
        try:
            result["romano_wolf"] = evaluate.romano_wolf_stepdown(
                differentials, horizon=HAC_HORIZON.get(int(horizon), 1),
                replications=int(replications), seed=int(seed))
        except Exception as exc:  # too few observations for the bootstrap, etc.
            result["romano_wolf"] = {"status": "NOT_AVAILABLE", "reason": f"{type(exc).__name__}: {exc}"}
    return result


# ----------------------------------------------------------------------------------------------
# Report assembly
# ----------------------------------------------------------------------------------------------
def content_digest(report: dict) -> str:
    core = {key: value for key, value in report.items()
            if key not in ("generated_at", "content_sha256")}
    blob = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def build_report(horizons, *, ensemble_id: str = ENSEMBLE_EQUAL, data_root=None,
                 max_origins: int | None = None, seed: int = DEFAULT_SEED,
                 replications: int = DEFAULT_REPLICATIONS) -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    r3 = json.loads(R3.read_text(encoding="utf-8"))
    values_present = spf_available(data_root)
    report = {
        "report_id": REPORT_ID,
        "version": VERSION,
        "tool": TOOL,
        "generated_at": _utc_now(),
        "purpose": ("does the third-party SPF median improve the T2 (real GDP growth) ensemble "
                    "scoring, and how much of the result is the SPF rather than the weights or the "
                    "origin sample? The report is diagnostic: no admission is assigned"),
        "protocol": {"file": "protocols/evaluation_protocol_v4.json", "version": protocol["version"],
                     "sha256": sha256_file(PROTOCOL)},
        "r3_specification": {"file": "protocols/r3_specification.json", "version": r3.get("version"),
                             "sha256": sha256_file(R3)},
        "target": TARGET,
        "horizons": [int(h) for h in horizons],
        "ensemble": ensemble_id,
        "ensemble_scheme_note": ("declared control scheme; adding the external member refits the "
                                 "weights over the enlarged member set, it is not a fixed-weight plug-in"),
        "calendar_rule": ("SPF median is public at the origin iff the survey is real time (>= 1990Q3), "
                          "the origin is a middle-month month-end (h=0) or a quarter-end origin whose "
                          "quarter survey was already released (h>0), and origin >= the official news "
                          "release date (src/amfe/ensemble/spf_calendar.py, docs/86 §5)"),
        "spf_mapping": {"T2_GDP_GROWTH": {"0": "drgdp2", "1": "drgdp3", "2": "drgdp4", "4": "drgdp6"},
                        "source": "protocols/../docs/86 §2-3; official SPF documentation PDF §3",
                        "not_used": {"h": 3, "reason": "drgdp5 exists but h=3 is not on the T2 grid"}},
        "spf_attribution": SPF_ATTRIBUTION,
        "spf_licence_note": ("SPF medians are third-party data (PHILADELPHIA_FED, research use): "
                            "values stay in the git-ignored data directory, only derived statistics "
                            "are published; no SPF value is written to this report"),
        "spf_values_present": bool(values_present),
        "spf_values_location": str(spf_values_path(data_root).relative_to(ROOT)).replace("\\", "/")
        if str(spf_values_path(data_root)).startswith(str(ROOT)) else "data/raw/spf/spf_median.json",
        "weights_diagnostics": {},
        "results": {},
        "data_quality": {},
        "limitations": [
            "diagnostic, not an admission decision: C1-C6 are not applied and no stage is advanced; "
            "the protocol spf_alignment rule (re-run every model at the survey deadline) is not used "
            "here - the sensitivity runs at the protocol month-end origins with the release-date "
            "availability filter",
            "SPF values are the latest file revision, not a point-in-time vintage: the release date "
            "fixes availability but errata/revisions after the survey are not audited (docs/86 §8)",
            "the SPF member's predictive distribution is a project-derived statistic (empirical "
            "historical SPF errors centred on the median), not a Philadelphia Fed density",
            "RMSE is the primary loss; CRPS is reported only when the ensemble mixture passes the "
            "sanity gate (docs/91 found extreme T2 mixture quantiles)",
            "the SPF-available subsample is selected by the calendar (middle months / release rule), "
            "not by performance, but it differs from the full sample and the paired comparison is the "
            "only apples-to-apples one",
            "REAL_TIME only: PSEUDO_REAL_TIME origins at the start of W1 are counted and excluded, "
            "as in docs/91",
            "the external member enters with equal weights under the control scheme; alternative "
            "weighting schemes are not re-fitted for this sensitivity",
        ],
        "next_steps": [
            "rerun this tool after the T2 mixture fix (docs/94) lands: CRPS switches from NOT_AVAILABLE "
            "to a reported loss automatically if the mixtures pass the sanity gate",
            "repeat the analysis at the protocol's SPF-alignment origins (survey deadlines) as a "
            "sensitivity check, and with the adaptive weighting scheme",
            "extend to T2 h=2/h=4 once the mixture and the SPF subsample sizes are sufficient",
            "decide (Lead) whether a SPF-augmented T2 candidate is registered for admission testing; "
            "this report deliberately assigns no status",
        ],
        "ci_method": ("no SPF values are in git: restore the private r2-metadata artifact "
                      "(data/raw/spf/spf_median.json) or run tools/fetch_spf.py, then run this tool. "
                      "The tool reports NOT_AVAILABLE honestly when the values are absent"),
    }
    loss_set = ["RMSE"]
    all_records = {}
    for horizon in report["horizons"]:
        horizon_records = {}
        for window in ("W1", "W2"):
            built = build_window_records(horizon, window, ensemble_id=ensemble_id,
                                         data_root=data_root, max_origins=max_origins,
                                         need_crps=True)
            horizon_records[window] = built
        combined = [row for block in horizon_records.values() for row in block["records"]]
        # CRPS availability: only if SPF values exist and every scored mixture passed the gate
        checked = [row for block in horizon_records.values() for row in block["records"]]
        rejected = sum(1 for row in checked if not row.get("crps_ok", True))
        crps_usable = bool(values_present and checked and rejected == 0)
        horizon_loss_set = ["RMSE"] + (["CRPS"] if crps_usable else [])
        results = {}
        spf_records = [row for row in combined if row["spf_available"]]
        completeness = {}
        for window in ("W1", "W2"):
            records = horizon_records[window]["records"]
            block = {"meta": horizon_records[window]["meta"]}
            block["full_sample"] = summarize_subset(records, horizon=horizon, loss_set=horizon_loss_set,
                                                    seed=seed, replications=replications,
                                                    subset=f"{window}_ALL_ORIGINS")
            block["paired_spf_subsample"] = summarize_subset(
                [row for row in records if row["spf_available"]], horizon=horizon,
                loss_set=horizon_loss_set, seed=seed, replications=replications,
                subset=f"{window}_SPF_AVAILABLE")
            results[window] = block
            completeness[window] = {"origins_scored": len(records),
                                    "spf_available": sum(1 for row in records if row["spf_available"])}
        results["ALL"] = {"meta": {"origins_scored": len(combined),
                                   "spf_available": len(spf_records),
                                   "crps_checked": sum(b["meta"]["crps_checked"] for b in horizon_records.values()),
                                   "crps_rejected": rejected,
                                   "crps_max_deviation": max((b["meta"]["crps_max_deviation"] for b in horizon_records.values()), default=0.0),
                                   "crps_max_abs": max((b["meta"]["crps_max_abs"] for b in horizon_records.values()), default=0.0)},
                         "full_sample": summarize_subset(combined, horizon=horizon, loss_set=horizon_loss_set,
                                                         seed=seed, replications=replications,
                                                         subset="ALL_ORIGINS"),
                         "paired_spf_subsample": summarize_subset(spf_records, horizon=horizon,
                                                                  loss_set=horizon_loss_set, seed=seed,
                                                                  replications=replications,
                                                                  subset="SPF_AVAILABLE")}
        report["results"][str(horizon)] = {"hac_horizon": HAC_HORIZON.get(int(horizon), 1),
                                           "crps": {"available": crps_usable,
                                                    "status": "AVAILABLE" if crps_usable else "NOT_AVAILABLE",
                                                    "reason": None if crps_usable else _crps_reason(horizon_records)},
                                           "completeness": completeness,
                                           "windows": results}
        report["data_quality"][f"{TARGET}/h{horizon}"] = {
            "crps_usable": crps_usable,
            "mixture_sanity": {
                "deviation_limit": MIXTURE_DEVIATION_LIMIT,
                "absolute_limit": MIXTURE_ABSOLUTE_LIMIT,
                "checked": sum(block["meta"]["crps_checked"] for block in horizon_records.values()),
                "rejected": rejected,
                "max_deviation": max((block["meta"]["crps_max_deviation"] for block in horizon_records.values()), default=0.0),
                "max_abs": max((block["meta"]["crps_max_abs"] for block in horizon_records.values()), default=0.0),
                "reasons": {reason: sum(block["meta"]["crps_reject_reasons"].get(reason, 0)
                                        for block in horizon_records.values())
                            for reason in {r for block in horizon_records.values()
                                           for r in block["meta"]["crps_reject_reasons"]}},
            },
            "note": ("docs/91 flagged the T2 ensemble mixture; the gate is recomputed on every run so "
                     "CRPS is reported automatically once the mixtures are sane"),
        }
        all_records[str(horizon)] = combined
    # availability section (counts only) and weight diagnostics
    availability = {}
    medians = None
    try:
        medians = load_spf_medians(data_root)
    except Exception:
        medians = None
    for horizon in report["horizons"]:
        rows = windows.origins_for(protocol, TARGET, int(horizon), "W1") + \
            windows.origins_for(protocol, TARGET, int(horizon), "W2")
        if max_origins is not None:
            rows = rows[:int(max_origins)]
        availability[str(horizon)] = spf_availability_summary([row["origin"] for row in rows],
                                                              int(horizon), data_root=data_root,
                                                              medians=medians)
    report["availability"] = availability
    weights = {}
    horizon_rows = all_records.get(str(report["horizons"][0]), [])
    spf_rows = [row for row in horizon_rows if row["spf_available"]]
    if spf_rows:
        weights["spf_weight_share_mean"] = _round(float(np.mean([row["spf_weight"] for row in spf_rows])))
        weights["spf_weight_share_min"] = _round(float(np.min([row["spf_weight"] for row in spf_rows])))
        weights["spf_weight_share_max"] = _round(float(np.max([row["spf_weight"] for row in spf_rows])))
    if horizon_rows:
        weights["mean_members_no_spf"] = _round(float(np.mean([row["n_members_no_spf"] for row in horizon_rows])))
        weights["mean_members_with_spf"] = _round(float(np.mean([row["n_members_with_spf"] for row in horizon_rows])))
    weights["internal_members"] = list(_ensemble_members(TARGET, report["horizons"][0], include_external=False))
    weights["external_members"] = ["SPF_GDP_MEDIAN"]
    report["weights_diagnostics"] = weights
    # honest status
    scored_real_time = sum(report["results"][str(h)]["windows"][w]["meta"]["origins_scored"]
                           for h in report["horizons"] for w in ("W1", "W2"))
    paired = sum(report["results"][str(h)]["windows"]["ALL"]["paired_spf_subsample"].get("origins", 0)
                 for h in report["horizons"])
    if not values_present:
        report["status"] = "NOT_AVAILABLE"
        report["status_reason"] = "SPF_VALUES_NOT_DOWNLOADED"
    elif scored_real_time == 0:
        report["status"] = "NOT_AVAILABLE"
        report["status_reason"] = "NO_REAL_TIME_ORIGINS_SCORED"
    elif paired < MIN_PAIRED_ORIGINS:
        report["status"] = "PARTIAL"
        report["status_reason"] = f"TOO_FEW_PAIRED_ORIGINS({paired})"
    else:
        report["status"] = "OK"
        report["status_reason"] = None
    report["summary"] = {"real_time_origins_scored": scored_real_time,
                         "paired_spf_origins": paired,
                         "horizons": report["horizons"],
                         "crps_reported": all(report["results"][str(h)]["crps"]["available"]
                                              for h in report["horizons"]) if report["horizons"] else False}
    report["content_sha256"] = content_digest(report)
    return report


def _crps_reason(horizon_records: dict) -> str:
    rejected = sum(block["meta"]["crps_rejected"] for block in horizon_records.values())
    checked = sum(block["meta"]["crps_checked"] for block in horizon_records.values())
    if rejected:
        return (f"mixture sanity gate rejected {rejected} of {checked} scored mixtures "
                f"(extreme quantiles, docs/91); CRPS is not reported")
    if checked == 0:
        return "no mixture was scored (no REAL_TIME origins with both variants)"
    return "CRPS usable"


def check_plan(*, data_root=None, horizons=(0, 1)) -> dict:
    """Fast availability/plan summary (no scoring); used by tests and CI smoke."""
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    present = spf_available(data_root)
    medians = None
    try:
        medians = load_spf_medians(data_root)
    except Exception:
        medians = None
    out = {"report_id": REPORT_ID, "status": "OK" if present else "NOT_AVAILABLE",
           "status_reason": None if present else "SPF_VALUES_NOT_DOWNLOADED",
           "spf_values_present": present,
           "spf_values_location": str(spf_values_path(data_root)),
           "protocol_version": protocol["version"], "horizons": {}}
    for horizon in horizons:
        rows = windows.origins_for(protocol, TARGET, int(horizon), "W1") + \
            windows.origins_for(protocol, TARGET, int(horizon), "W2")
        out["horizons"][str(horizon)] = spf_availability_summary(
            [row["origin"] for row in rows], int(horizon), data_root=data_root, medians=medians)
    return out


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SPF sensitivity in the T2 scoring (docs/95)")
    parser.add_argument("--target", default=TARGET, help=f"only {TARGET} is supported")
    parser.add_argument("--horizons", nargs="+", type=int, default=[0, 1])
    parser.add_argument("--ensemble", default=ENSEMBLE_EQUAL, choices=list(ENSEMBLE_IDS))
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-origins", type=int, default=None,
                        help="score at most N origins per window (tests/smoke runs only)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--replications", type=int, default=DEFAULT_REPLICATIONS)
    parser.add_argument("--check", action="store_true",
                        help="print the availability/plan summary and exit (no scoring)")
    args = parser.parse_args(argv)
    if args.target != TARGET:
        raise SystemExit(f"unsupported target {args.target!r}: this tool is T2-only")
    horizons = sorted({int(h) for h in args.horizons})
    unsupported = [h for h in horizons if h not in HAC_HORIZON]
    if unsupported:
        raise SystemExit(f"unsupported horizon(s) {unsupported}; T2 grid is 0,1,2,4")
    if args.check:
        summary = check_plan(data_root=Path(args.data_root), horizons=horizons)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary["status"] == "OK" else 3
    report = build_report(horizons, ensemble_id=args.ensemble, data_root=Path(args.data_root),
                          max_origins=args.max_origins, seed=args.seed,
                          replications=args.replications)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=_json_default) + "\n",
                   encoding="utf-8", newline="\n")
    summary = {
        "out": str(out), "status": report["status"], "reason": report["status_reason"],
        "summary": report["summary"],
        "crps": {h: report["results"][h]["crps"]["status"] for h in report["results"]},
        "availability": {h: {"available": report["availability"][h]["available"],
                             "origins": report["availability"][h]["origins"]}
                         for h in report["availability"]},
        "content_sha256": report["content_sha256"][:16],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
