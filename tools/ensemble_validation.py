#!/usr/bin/env python3
"""V2 validation of the ensemble candidates on historical REAL_TIME data (walk-forward).

The stage-7 ensemble layer (``src/amfe/ensemble``) declares four candidate schemes before any
evaluation: the equal-weight control and three history-based schemes (inverse-MSE, Bayesian,
adaptive).  This tool is the *validation* stage: it asks whether the weighted schemes actually beat
the equal-weight control on a walk-forward history, not on a single hand-picked origin.

Method
------
* pairs: T1_CPI_YOY h=1 (monthly CPI y/y) and T2_GDP_GROWTH h=0 (quarterly real GDP growth, if the
  first-publication actuals exist).  Every protocol window W1 and W2 is evaluated origin by origin,
  exactly as the evaluator defines them (``tools/windows.py``), so the origins are the same as in
  the admission harness.
* for each origin every ensemble candidate is produced by the public
  ``amfe.ensemble.ensemble_forecast`` (PIT member forecasts, PIT calibration history) and is scored
  against the protocol's **first-publication** actual (``amfe.evaluation.actuals.actual_pair``);
* losses: squared error (the RMSE differential) and the protocol CRPS (19-quantile pinball);
* relative losses are computed against ``ENSEMBLE_EQUAL`` inside exactly the same subset of
  origins (all, W1, W2, REAL_TIME only, PSEUDO_REAL_TIME only, NBER recession, NBER expansion);
* significance: Diebold-Mariano (HAC h-1, HLN) on the per-origin loss differential, one-sided
  "candidate better", plus a full Romano-Wolf stepdown (stationary block bootstrap, FWER 0.10) over
  the family of the three weighted schemes for each loss and subset.  The RW adjusted p-values are
  reported; the policy is not an admission gate here, this is a validation report.

Data hygiene
------------
The report stores aggregates, counts, metadata and SHA-256 digests only; per-origin actuals,
forecasts, losses and weights never leave the process.  Raw third-party values stay in the
git-ignored ``data/raw`` directory (docs/84) and only the derived statistics are committed.

Regime split
------------
Recession/expansion uses the NBER chronology (peak/trough months from
``protocols/nber_dates.json``), *not* the latest ``USREC`` tape: an origin is labelled by whether
its target period (CPI month, GDP quarter) contains NBER recession months.  This is a retrospective
split by the final chronology - useful for diagnostics, not a point-in-time gate.

Reproduce:
    python tools/ensemble_validation.py --max-origins 12 --out protocols/ensemble_validation_report.json
    python tools/ensemble_validation.py            # full W1+W2 walk-forward (~minutes, cached forecasts)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zlib
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import evaluate  # noqa: E402
import windows as windows_module  # noqa: E402

from amfe.ensemble import ENSEMBLE_IDS  # noqa: E402
from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate  # noqa: E402
from amfe.forecast.data import month_index  # noqa: E402

PROTOCOL_PATH = ROOT / "protocols" / "evaluation_protocol_v4.json"
NBER_PATH = ROOT / "protocols" / "nber_dates.json"
REPORT_PATH = ROOT / "protocols" / "ensemble_validation_report.json"

ENSEMBLE_EQUAL = "ENSEMBLE_EQUAL"
ENSEMBLE_INVERSE_MSE = "ENSEMBLE_INVERSE_MSE"
ENSEMBLE_BAYES = "ENSEMBLE_BAYES"
ENSEMBLE_ADAPTIVE = "ENSEMBLE_ADAPTIVE"
CONTROL = ENSEMBLE_EQUAL
WEIGHTED = (ENSEMBLE_INVERSE_MSE, ENSEMBLE_BAYES, ENSEMBLE_ADAPTIVE)

DEFAULT_PAIRS = (("T1_CPI_YOY", 1), ("T2_GDP_GROWTH", 0))
PAIR_KEYS = {("T1_CPI_YOY", 1): "T1_CPI_YOY/h1", ("T2_GDP_GROWTH", 0): "T2_GDP_GROWTH/h0"}
ALPHA = evaluate.ALPHA            # 0.10 one-sided; also the Romano-Wolf FWER
DEFAULT_REPLICATIONS = evaluate.DEFAULT_RW_REPLICATIONS
RW_SEED_BASE = evaluate.DEFAULT_RW_SEED

REGIME_RECESSION = "RECESSION"
REGIME_EXPANSION = "EXPANSION"
DATA_REAL_TIME = "REAL_TIME"
DATA_PSEUDO = "PSEUDO_REAL_TIME"

LIMITATIONS = [
    "walk-forward validation, not an admission decision: no C1-C6 gate is applied and no stage is advanced",
    "REAL_TIME origins only are admissible for admission; PSEUDO_REAL_TIME origins that appear at the start of W1 are reported separately and must not be read as REAL_TIME evidence",
    "the first-publication actuals come from the quarterly ALFRED vintage grid, so 'first publication' is the first archived release at or after the publication, a documented approximation",
    "T2 h=0 is limited by the GDPC1 first-publication coverage: the early quarters of W1 may be skipped (the exact counts are in the report)",
    "no SPF member enters the T1 ensemble (SPF has no monthly y/y CPI forecast); for T2 the SPF member enters only when its survey mapping is available at the origin",
    "the NBER regime split is a retrospective diagnostic by the final chronology; it is not PIT and it is not used as a gate",
    "C4/W1 on the historical windows remains limited by data availability (docs/76); this tool reports statistics and does not re-open that question",
    "aggregates only: per-origin actuals, forecasts, losses and weights are not stored, only counts, aggregates and SHA-256 digests",
]


class ValidationError(RuntimeError):
    pass


# -------------------------------------------------------------------------------- utilities
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_array(values) -> str:
    array = np.ascontiguousarray(np.asarray(values, dtype=float))
    return hashlib.sha256(array.tobytes()).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return ""


def load_protocol(path: Path = PROTOCOL_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_nber(path: Path = NBER_PATH) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "reference_dates" not in data:
        raise ValidationError("nber_dates.json has no reference_dates")
    return data


def recession_month_set(nber: dict) -> set:
    """Set of month indices inside NBER recessions from the final peak/trough chronology."""
    months: set = set()
    for cycle in nber["reference_dates"]:
        peak = str(cycle.get("peak") or "")
        trough = str(cycle.get("trough") or "")
        if not peak or not trough:
            continue
        start, end = month_index(date.fromisoformat(peak + "-01")), month_index(
            date.fromisoformat(trough + "-01"))
        if end < start:
            raise ValidationError(f"bad NBER cycle: peak {peak} after trough {trough}")
        months.update(range(start, end + 1))
    if not months:
        raise ValidationError("empty NBER recession calendar")
    return months


def _period_months(target: str, target_iso: str) -> list:
    """Month indices covered by the target period (CPI month or GDP quarter)."""
    day = date.fromisoformat(str(target_iso)[:10])
    if target == "T2_GDP_GROWTH":
        quarter_last_month = day.month
        return [month_index(date(day.year, quarter_last_month - 2, 1)),
                month_index(date(day.year, quarter_last_month - 1, 1)),
                month_index(date(day.year, quarter_last_month, 1))]
    return [month_index(date(day.year, day.month, 1))]


def regime_label(target: str, target_iso: str, recession_months: set) -> str:
    """RECESSION when any month of the target period is an NBER recession month."""
    months = _period_months(target, target_iso)
    return REGIME_RECESSION if any(month in recession_months for month in months) else REGIME_EXPANSION


def _evenly_spaced(items: list, limit: int | None) -> list:
    """Spread a sample over the whole list (keeps the first and last origin)."""
    if limit is None or limit >= len(items):
        return list(items)
    if limit <= 0:
        raise ValidationError("max_origins must be positive")
    if limit == 1:
        return [items[0]]
    indices = np.linspace(0, len(items) - 1, num=int(limit)).round().astype(int)
    return [items[int(index)] for index in indices]


def _default_actual(target, origin, horizon):
    from amfe.evaluation.actuals import actual_pair

    record = actual_pair(target, origin, horizon)
    return None if record.get("primary") is None else float(record["primary"])


def _default_forecast_factory(mode: str):
    def forecast(ensemble_id, target, origin, horizon):
        return ensemble_candidate(ensemble_id, target, origin, horizon, mode=mode)
    return forecast


# -------------------------------------------------------------------------------- collection
def collect_records(target: str, horizon: int, window: str, *, protocol: dict | None = None,
                    recession_months: set | None = None, mode: str = "auto",
                    max_origins: int | None = None, ensemble_ids=ENSEMBLE_IDS,
                    forecast_fn=None, actual_fn=None) -> dict:
    """Score every ensemble candidate on every origin of one protocol window.

    forecast_fn/actual_fn are injectable for tests; the defaults use the real ensemble and the
    first-publication actuals.  Only complete origins (all candidates forecasted) enter the
    comparison, so the relative losses are computed on identical samples.
    """
    protocol = load_protocol() if protocol is None else protocol
    if recession_months is None:
        recession_months = recession_month_set(load_nber())
    forecast_fn = _default_forecast_factory(mode) if forecast_fn is None else forecast_fn
    actual_fn = _default_actual if actual_fn is None else actual_fn
    ensemble_ids = tuple(ensemble_ids)
    if CONTROL not in ensemble_ids:
        raise ValidationError("the equal-weight control must be part of the comparison")
    rows = _evenly_spaced(windows_module.origins_for(protocol, target, int(horizon), window),
                          max_origins)
    records = []
    failures = {ensemble_id: 0 for ensemble_id in ensemble_ids}
    skipped_no_actual = 0
    skipped_incomplete = 0
    weight_sources = {ensemble_id: {} for ensemble_id in ensemble_ids}
    data_classes = {}
    for row in rows:
        origin = date.fromisoformat(str(row["origin"])[:10])
        actual = actual_fn(target, origin, int(horizon))
        if actual is None or not np.isfinite(float(actual)):
            skipped_no_actual += 1
            continue
        losses = {}
        classes = {}
        for ensemble_id in ensemble_ids:
            try:
                forecast = forecast_fn(ensemble_id, target, origin, int(horizon))
            except Exception:  # a candidate that cannot run at an origin is a documented failure
                failures[ensemble_id] += 1
                continue
            if forecast.get("point") is None or forecast.get("quantiles") is None:
                failures[ensemble_id] += 1
                continue
            quantiles = np.asarray(forecast["quantiles"], dtype=float)
            if quantiles.shape != (len(evaluate.QUANTILES),) or not np.all(np.isfinite(quantiles)):
                failures[ensemble_id] += 1
                continue
            actual_value = float(actual)
            losses[ensemble_id] = {
                "squared_error": (float(forecast["point"]) - actual_value) ** 2,
                "crps": float(evaluate.crps(quantiles[None, :], np.asarray([actual_value]))[0]),
            }
            classes[ensemble_id] = str(forecast.get("data_class", "UNSTATED"))
            source = str((forecast.get("history") or {}).get("weight_source", "UNKNOWN"))
            weight_sources[ensemble_id][source] = weight_sources[ensemble_id].get(source, 0) + 1
        if len(losses) != len(ensemble_ids):
            skipped_incomplete += 1
            continue
        data_class = DATA_REAL_TIME if all(value == DATA_REAL_TIME for value in classes.values()) else DATA_PSEUDO
        data_classes[data_class] = data_classes.get(data_class, 0) + 1
        records.append({
            "origin": origin.isoformat(),
            "target_period": str(row["target"])[:10],
            "window": window,
            "regime": regime_label(target, row["target"], recession_months),
            "data_class": data_class,
            "losses": losses,
        })
    return {
        "records": records,
        "meta": {
            "window": window,
            "origins_in_window": len(rows),
            "origins_used": len(records),
            "skipped_no_first_publication_actual": skipped_no_actual,
            "skipped_incomplete_candidates": skipped_incomplete,
            "candidate_failures": failures,
            "data_class_counts": data_classes,
            "weight_source_counts": {
                ensemble_id: dict(sorted(counts.items()))
                for ensemble_id, counts in weight_sources.items()
            },
            "first_origin": records[0]["origin"] if records else None,
            "last_origin": records[-1]["origin"] if records else None,
            "sampled": max_origins is not None and len(rows) > int(max_origins),
        },
    }


# -------------------------------------------------------------------------------- statistics
def summarize(records: list, *, horizon: int, ensemble_ids=ENSEMBLE_IDS,
              control: str = CONTROL, replications: int = DEFAULT_REPLICATIONS,
              seed: int = RW_SEED_BASE) -> dict:
    """Aggregates, relative losses and DM/Romano-Wolf significance for one subset of origins."""
    ensemble_ids = tuple(ensemble_ids)
    complete = [record for record in records
                if all(ensemble_id in record["losses"] for ensemble_id in ensemble_ids)]
    if not complete:
        return {"status": "NO_RECORDS", "n_origins": 0}
    n = len(complete)
    per_ensemble = {}
    squared = {}
    crps = {}
    for ensemble_id in ensemble_ids:
        squared[ensemble_id] = np.asarray([record["losses"][ensemble_id]["squared_error"]
                                           for record in complete], dtype=float)
        crps[ensemble_id] = np.asarray([record["losses"][ensemble_id]["crps"]
                                        for record in complete], dtype=float)
    control_rmse = float(np.sqrt(np.mean(squared[control])))
    control_crps = float(np.mean(crps[control]))
    for ensemble_id in ensemble_ids:
        rmse = float(np.sqrt(np.mean(squared[ensemble_id])))
        mean_crps = float(np.mean(crps[ensemble_id]))
        entry = {
            "n_origins": n,
            "rmse": rmse,
            "mean_crps": mean_crps,
            "relative_rmse_vs_equal": rmse / control_rmse if control_rmse > 0 else None,
            "relative_crps_vs_equal": mean_crps / control_crps if control_crps > 0 else None,
            "is_control": ensemble_id == control,
        }
        if ensemble_id != control:
            entry["dm_rmse"] = evaluate.dm_test(squared[ensemble_id] - squared[control],
                                                int(horizon), "less")
            entry["dm_crps"] = evaluate.dm_test(crps[ensemble_id] - crps[control],
                                                int(horizon), "less")
        per_ensemble[ensemble_id] = entry
    multiplicity = {}
    if n >= 2:
        for loss, arrays in (("RMSE", squared), ("CRPS", crps)):
            family = {ensemble_id: arrays[ensemble_id] - arrays[control] for ensemble_id in ensemble_ids
                      if ensemble_id != control}
            if len(family) < 2:
                multiplicity[loss] = {"status": "SINGLE_CANDIDATE_NOT_ADJUSTED"}
                continue
            result = evaluate.romano_wolf_stepdown(
                family, horizon=int(horizon), replications=int(replications),
                seed=int(seed) + zlib.crc32(loss.encode("utf-8")) % 100000)
            multiplicity[loss] = {
                "method": result["method"],
                "fwer": result["fwer"],
                "replications": result["replications"],
                "family": result["family"],
                "family_size": result["family_size"],
                "block_length": result["block_length"],
                "adjusted_p_values": result["adjusted_p_values"],
                "raw_p_values": result["raw_p_values"],
                "rejected": result["rejected"],
            }
    else:
        multiplicity = {"status": "INSUFFICIENT_N_FOR_SIGNIFICANCE", "n_origins": n}
    return {
        "status": "OK",
        "n_origins": n,
        "control": control,
        "loss_conventions": {
            "RMSE": "sqrt(mean squared error) per origin; DM uses the squared-error differential",
            "CRPS": "protocol CRPS = twice the mean pinball loss over the 19-quantile grid",
        },
        "per_ensemble": per_ensemble,
        "multiplicity": multiplicity,
    }


def _regime_records(records: list, regime: str) -> list:
    return [record for record in records if record["regime"] == regime]


def _data_class_records(records: list, data_class: str) -> list:
    return [record for record in records if record["data_class"] == data_class]


def _subset_summary(records, label, *, horizon, ensemble_ids, replications, seed):
    summary = summarize(records, horizon=horizon, ensemble_ids=ensemble_ids,
                        replications=replications, seed=seed)
    summary["subset"] = label
    return summary


# -------------------------------------------------------------------------------- report
def build_pair_block(target: str, horizon: int, per_window: dict, *, ensemble_ids=ENSEMBLE_IDS,
                     replications: int = DEFAULT_REPLICATIONS) -> dict:
    """Aggregate the two windows, data classes and regimes into one report block."""
    records = []
    for window in ("W1", "W2"):
        records.extend(per_window.get(window, {}).get("records", []))
    if not records:
        return {
            "status": "SKIPPED_NO_DATA",
            "target": target,
            "horizon": int(horizon),
            "reason": "no origin produced a first-publication actual and a complete set of candidates",
            "windows": {window: per_window.get(window, {}).get("meta", {})
                        for window in ("W1", "W2")},
        }
    seed = RW_SEED_BASE + zlib.crc32(f"{target}/{horizon}".encode("utf-8")) % 100000
    block = {
        "status": "OK",
        "target": target,
        "horizon": int(horizon),
        "pair_key": PAIR_KEYS.get((target, int(horizon)), f"{target}/h{horizon}"),
        "ensemble_ids": list(ensemble_ids),
        "control": CONTROL,
        "replications": int(replications),
        "windows": {window: per_window.get(window, {}).get("meta", {}) for window in ("W1", "W2")},
        "origins": {
            "total": len(records),
            "first_origin": records[0]["origin"],
            "last_origin": records[-1]["origin"],
            "real_time": sum(1 for record in records if record["data_class"] == DATA_REAL_TIME),
            "pseudo_real_time": sum(1 for record in records
                                    if record["data_class"] == DATA_PSEUDO),
            "weight_source_counts": {
                ensemble_id: _merge_weight_sources(per_window, ensemble_id)
                for ensemble_id in ensemble_ids
            },
        },
        "overall": _subset_summary(records, "ALL_ORIGINS", horizon=horizon,
                                   ensemble_ids=ensemble_ids, replications=replications, seed=seed),
        "by_window": {
            window: _subset_summary(_window_records(per_window, window), window, horizon=horizon,
                                    ensemble_ids=ensemble_ids, replications=replications, seed=seed)
            for window in ("W1", "W2")
        },
        "by_data_class": {
            DATA_REAL_TIME: _subset_summary(_data_class_records(records, DATA_REAL_TIME),
                                            DATA_REAL_TIME, horizon=horizon,
                                            ensemble_ids=ensemble_ids,
                                            replications=replications, seed=seed),
            DATA_PSEUDO: _subset_summary(_data_class_records(records, DATA_PSEUDO),
                                         DATA_PSEUDO, horizon=horizon,
                                         ensemble_ids=ensemble_ids,
                                         replications=replications, seed=seed),
        },
        "by_regime": {
            REGIME_RECESSION: _subset_summary(_regime_records(records, REGIME_RECESSION),
                                              REGIME_RECESSION, horizon=horizon,
                                              ensemble_ids=ensemble_ids,
                                              replications=replications, seed=seed),
            REGIME_EXPANSION: _subset_summary(_regime_records(records, REGIME_EXPANSION),
                                              REGIME_EXPANSION, horizon=horizon,
                                              ensemble_ids=ensemble_ids,
                                              replications=replications, seed=seed),
        },
        "digests": {
            "origins_sha256": sha256_text("\n".join(record["origin"] for record in records)),
            "target_periods_sha256": sha256_text(
                "\n".join(record["target_period"] for record in records)),
            "squared_errors_sha256": sha256_array([record["losses"][ensemble_id]["squared_error"]
                                                   for record in records
                                                   for ensemble_id in ensemble_ids]),
            "crps_sha256": sha256_array([record["losses"][ensemble_id]["crps"]
                                         for record in records for ensemble_id in ensemble_ids]),
        },
    }
    return block


def _window_records(per_window: dict, window: str) -> list:
    return list(per_window.get(window, {}).get("records", []))


def _merge_weight_sources(per_window: dict, ensemble_id: str) -> dict:
    merged: dict = {}
    for window in ("W1", "W2"):
        counts = per_window.get(window, {}).get("meta", {}).get("weight_source_counts", {})
        for source, count in counts.get(ensemble_id, {}).items():
            merged[source] = merged.get(source, 0) + int(count)
    return dict(sorted(merged.items()))


def build_report(pairs=DEFAULT_PAIRS, *, mode: str = "auto", max_origins: int | None = None,
                 replications: int = DEFAULT_REPLICATIONS, protocol: dict | None = None,
                 nber: dict | None = None, ensemble_ids=ENSEMBLE_IDS,
                 forecast_fn=None, actual_fn=None) -> dict:
    """Full deterministic validation report (no timestamp; the CLI adds run metadata)."""
    protocol = load_protocol() if protocol is None else protocol
    nber = load_nber() if nber is None else nber
    recession_months = recession_month_set(nber)
    pairs_block = {}
    for target, horizon in pairs:
        per_window = {}
        for window in ("W1", "W2"):
            per_window[window] = collect_records(
                target, int(horizon), window, protocol=protocol,
                recession_months=recession_months, mode=mode, max_origins=max_origins,
                ensemble_ids=ensemble_ids, forecast_fn=forecast_fn, actual_fn=actual_fn)
        block = build_pair_block(target, int(horizon), per_window,
                                 ensemble_ids=ensemble_ids, replications=replications)
        pairs_block[block.get("pair_key", f"{target}/h{horizon}")] = block
    return {
        "report_id": "AMFE-ENSEMBLE-VALIDATION-V2",
        "schema_version": "1.0",
        "kind": "walk-forward validation of the ensemble weighting schemes (aggregated statistics)",
        "protocol": {
            "file": "protocols/evaluation_protocol_v4.json",
            "version": protocol.get("version"),
            "sha256": _sha256_file(PROTOCOL_PATH),
        },
        "nber_dates": {
            "file": "protocols/nber_dates.json",
            "sha256": _sha256_file(NBER_PATH),
            "coverage": nber.get("coverage"),
            "regime_rule": ("origin labelled RECESSION when any month of its target period is an NBER "
                            "recession month; final peak/trough chronology, retrospective, not USREC latest"),
        },
        "method": {
            "walk_forward": "every origin of the protocol windows W1 and W2, same origins as the harness",
            "candidates": list(ensemble_ids),
            "control": CONTROL,
            "actual_values_source": "first publication from the local dense ALFRED vintage archive",
            "scores": ["RMSE (squared-error differential for DM)", "CRPS (protocol 19-quantile)"],
            "significance": ("DM/GW HAC h-1 + HLN, one-sided candidate better; Romano-Wolf stepdown "
                             "with the stationary bootstrap over the weighted schemes, FWER 0.10"),
            "relative_loss": "candidate aggregate loss / ENSEMBLE_EQUAL aggregate loss in the same subset",
            "regime_split": "NBER peak/trough months of the target period (retrospective diagnostic)",
            "aggregation": "no per-origin values are stored: counts, aggregates and SHA-256 digests only",
        },
        "mode": mode,
        "max_origins_per_window": max_origins,
        "ensemble_validation": "not an admission decision; no C1-C6 gate is applied",
        "pairs": pairs_block,
        "limitations": list(LIMITATIONS),
        "licence_note": ("third-party actual values and member forecasts are not stored; only derived "
                         "statistics, counts and digests are published (docs/84)"),
    }


# -------------------------------------------------------------------------------- hygiene
FORBIDDEN_KEYS = {
    "actual", "actuals", "actual_primary", "actual_secondary", "point", "quantiles", "probability",
    "forecast", "forecasts", "loss", "losses", "errors", "member_points", "member_weights",
    "observations", "values", "series_values", "raw", "raw_values",
}


def find_raw_values(report, path: str = "report", max_aggregate_length: int = 16) -> list:
    """Return human-readable paths of values that must not appear in the published report."""
    problems: list = []
    if isinstance(report, dict):
        for key, value in report.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                problems.append(f"{path}.{key}: forbidden key")
            problems.extend(find_raw_values(value, f"{path}.{key}", max_aggregate_length))
    elif isinstance(report, list):
        if len(report) > max_aggregate_length and all(
                isinstance(item, (int, float)) for item in report):
            problems.append(f"{path}: numeric list of length {len(report)}")
        for index, value in enumerate(report):
            problems.extend(find_raw_values(value, f"{path}[{index}]", max_aggregate_length))
    return problems


def write_report(report: dict, path: Path | str = REPORT_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return target


def _clean(value):
    """Convert numpy scalars/arrays to plain JSON types (aggregates are small by construction)."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_clean(item) for item in value.tolist()]
    return value


# -------------------------------------------------------------------------------- CLI
def summary_lines(report: dict) -> list:
    lines = []
    for key, block in report.get("pairs", {}).items():
        if block.get("status") != "OK":
            lines.append(f"{key}: {block.get('status')} ({block.get('reason', '')})")
            continue
        lines.append(f"{key}: origins={block['origins']['total']} "
                     f"(REAL_TIME={block['origins']['real_time']}, "
                     f"PSEUDO={block['origins']['pseudo_real_time']})")
        overall = block["overall"]
        for ensemble_id, entry in overall["per_ensemble"].items():
            if entry.get("is_control"):
                lines.append(f"  {ensemble_id}: control (relative loss = 1)")
                continue
            lines.append(f"  {ensemble_id}: relative RMSE={entry['relative_rmse_vs_equal']:.4f} "
                         f"relative CRPS={entry['relative_crps_vs_equal']:.4f}")
        for loss, multi in overall.get("multiplicity", {}).items():
            if isinstance(multi, dict) and "rejected" in multi:
                lines.append(f"  RW {loss}: rejected={multi['rejected']}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="V2 walk-forward validation of ensemble candidates")
    parser.add_argument("--pairs", default="T1_CPI_YOY:1,T2_GDP_GROWTH:0",
                        help="comma-separated target:horizon pairs")
    parser.add_argument("--mode", default="auto", choices=("auto", "vintage", "latest"))
    parser.add_argument("--max-origins", type=int, default=None,
                        help="debug: evenly spaced origin sample per window (default: all)")
    parser.add_argument("--replications", type=int, default=DEFAULT_REPLICATIONS)
    parser.add_argument("--out", default=str(REPORT_PATH))
    parser.add_argument("--no-timestamp", action="store_true",
                        help="omit generated_at (byte-identical reruns for the same inputs)")
    args = parser.parse_args(argv)
    pairs = []
    for item in args.pairs.split(","):
        target, _, horizon = item.strip().partition(":")
        if not target or not horizon:
            raise SystemExit(f"bad pair {item!r}; expected target:horizon")
        pairs.append((target, int(horizon)))
    report = build_report(pairs, mode=args.mode, max_origins=args.max_origins,
                          replications=args.replications)
    report = _clean(report)
    if not args.no_timestamp:
        from datetime import datetime, timezone
        report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    problems = find_raw_values(report)
    if problems:
        raise SystemExit("report contains non-aggregate values: " + "; ".join(problems[:5]))
    path = write_report(report, args.out)
    print(json.dumps(json.loads(json.dumps(report)), ensure_ascii=False)[:200])
    for line in summary_lines(report):
        print(line)
    print(f"report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
