#!/usr/bin/env python3
"""Deterministic learning-loop demo: three model versions, Q, registry gates, rollback.

Runs entirely offline on a synthetic series (no network, no data files, no wall clock):

* v0 ``RW_BASELINE`` — random walk, the naive benchmark, promoted to ACTIVE with a **simulated**
  independent review (the demo clearly marks this: a real ACTIVE requires a human reviewer);
* v1 ``AR1_IMPROVED`` — AR(1) with an 80% predictive interval; its walk-forward MAE/RMSE beat v0,
  so Q is higher; it reaches LIMITED, and the ACTIVE attempt is refused (first without any review,
  then with a non-independent "review") — the version stays at LIMITED pending a human;
* v2 ``AR1_BIASED_DEGRADED`` — the same AR(1) plus a fixed bias; it is worse than the naive
  benchmark, Q is lower, the version is REJECTED;
* rollback: v1 (LIMITED) is reverted to the stable v0 ACTIVE version, and the append-only log keeps
  every event.

Usage:
    python tools/learning_demo.py                 # human summary + JSON
    python tools/learning_demo.py --json          # JSON only
    python tools/learning_demo.py --out demo.json # write the JSON report
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.learning import (  # noqa: E402
    QualityError,
    RegistryError,
    compare_versions,
    evaluate_quality,
    load_events,
    promote,
    register,
    reject,
    revert,
    run_walk_forward,
    sha256_text,
    state,
    validate_registry,
)

DEMO_DATE = "2026-11-16"
DEMO_EVALUATED = "2026-11-15"
MIN_TRAIN = 60
HORIZON = 1
Z_80 = 1.2815515655446004  # two-sided 80% normal quantile
WARNINGS = ["independent review in this demo is SIMULATED: a real ACTIVE promotion needs a human "
            "reviewer (docs/145, docs/151)"]


def synthetic_series(n: int = 120) -> list[float]:
    """Deterministic mean-reverting series with a seasonal component (no randomness)."""
    values: list[float] = []
    level = 20.0
    for index in range(n):
        seasonal = 2.0 * math.sin(2.0 * math.pi * index / 12.0)
        level = 20.0 + 0.55 * (level - 20.0) + 0.35 * seasonal
        values.append(round(level, 9))
    return values


def _quantiles(point: float, sigma: float) -> dict:
    return {0.10: round(point - Z_80 * sigma, 9), 0.90: round(point + Z_80 * sigma, 9)}


def _forecast_rw(train, horizon: int) -> dict:
    last = float(train[-1])
    diffs = np.diff(np.asarray(train, dtype=float))
    sigma = float(np.std(diffs)) if diffs.size else 0.0
    return {"point": [last] * horizon, "quantiles": [_quantiles(last, sigma)] * horizon}


def _ar1(train, horizon: int) -> tuple[list[float], float, float, float]:
    y = np.asarray(train, dtype=float)
    x = np.column_stack([np.ones(y.size - 1), y[:-1]])
    beta, *_ = np.linalg.lstsq(x, y[1:], rcond=None)
    intercept, phi = float(beta[0]), float(beta[1])
    residuals = y[1:] - (intercept + phi * y[:-1])
    sigma = float(np.std(residuals, ddof=1)) if residuals.size > 1 else 0.0
    path = []
    last = float(y[-1])
    for _ in range(horizon):
        last = intercept + phi * last
        path.append(last)
    return path, sigma, intercept, phi


def _forecast_ar1(train, horizon: int) -> dict:
    path, sigma, _, _ = _ar1(train, horizon)
    return {"point": path, "quantiles": [_quantiles(value, sigma) for value in path]}


def _forecast_ar1_biased(train, horizon: int) -> dict:
    path, sigma, _, _ = _ar1(train, horizon)
    shifted = [value + 5.0 for value in path]
    return {"point": shifted, "quantiles": [_quantiles(value, sigma) for value in shifted]}


def _stability_cv(records: list[dict]) -> float:
    fold_mae = [abs(sum(record["model_errors"]) / len(record["model_errors"]))
                for record in records]
    if not fold_mae:
        return 0.0
    mean = float(np.mean(fold_mae))
    return float(np.std(fold_mae, ddof=1) / mean) if mean else 0.0


def evaluate_version(name: str, forecast_fn) -> dict:
    """Walk-forward metrics + Q for one version."""
    result = run_walk_forward(synthetic_series(), forecast_fn, min_train=MIN_TRAIN,
                              horizon=HORIZON, include_raw=True)
    metrics = {
        "error": result["model"]["mae"],
        "benchmark_error": result["benchmark"]["mae"],
        "balance_residual_max": 0.0,
        "n_oos": result["model"]["n_forecasts"],
        "stability_cv": round(_stability_cv(result["records"]), 9),
        "interval_coverage": result["quality_inputs"]["interval_coverage"],
        "consistency_violations": 0,
    }
    quality = evaluate_quality(metrics)
    return {"name": name, "walkforward": result, "quality": quality.as_dict(),
            "quality_inputs": metrics}


def run_demo(*, registry_path: Path | None = None) -> dict:
    """Execute the full demo; returns a JSON-friendly report."""
    versions = {
        "v0": evaluate_version("RW_BASELINE", _forecast_rw),
        "v1": evaluate_version("AR1_IMPROVED", _forecast_ar1),
        "v2": evaluate_version("AR1_BIASED_DEGRADED", _forecast_ar1_biased),
    }
    report: dict = {"schema": "AMFE-LEARNING-DEMO/1.0.0", "date": DEMO_DATE,
                    "warnings": list(WARNINGS), "versions": {}, "refusals": [], "events": None,
                    "registry_state": None}
    for key, version in versions.items():
        result = version["walkforward"]
        report["versions"][key] = {
            "name": version["name"],
            "q": version["quality"]["q"],
            "grade": version["quality"]["grade"],
            "components": version["quality"]["components"],
            "metrics": {"mae": result["model"]["mae"], "rmse": result["model"]["rmse"],
                        "n_forecasts": result["model"]["n_forecasts"],
                        "benchmark_mae": result["benchmark"]["mae"],
                        "relative_mae": result["comparison"]["relative_mae"],
                        "beats_naive": result["comparison"]["beats_naive"],
                        "crps": result["model"]["crps"]},
            "quality_inputs": version["quality_inputs"],
            "hard_constraints_pass": version["quality"]["hard_constraints"]["pass"],
        }
    decisions = {
        "v1_vs_v0": compare_versions(versions["v0"]["quality"], versions["v1"]["quality"]),
        "v2_vs_v0": compare_versions(versions["v0"]["quality"], versions["v2"]["quality"]),
    }
    report["decisions"] = decisions

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(registry_path) if registry_path else Path(tmp) / "registry.jsonl"
        criteria_v0 = {"registered_at": "2026-11-01",
                       "criteria": {"min_relative_mae": 0.95, "min_q": 0.60}}
        criteria_v1 = {"registered_at": "2026-11-10",
                       "criteria": {"min_relative_mae": 0.80, "min_q": 0.70}}
        criteria_v2 = {"registered_at": "2026-11-10",
                       "criteria": {"min_relative_mae": 0.80, "min_q": 0.70}}

        def code_hash(name: str) -> str:
            return sha256_text(f"demo-code:{name}")

        data_hash = sha256_text("demo-series:synthetic-120")

        register(path, model_id="DEMO_MODEL", version="v0_rw_baseline",
                 code_sha256=code_hash("v0"), data_sha256=data_hash,
                 metrics=report["versions"]["v0"]["metrics"],
                 quality=versions["v0"]["quality"], evaluated_at=DEMO_EVALUATED,
                 author="automation", criteria=criteria_v0, note="naive benchmark version")
        promote(path, model_id="DEMO_MODEL", version="v0_rw_baseline", to_status="SHADOW",
                at=DEMO_DATE)
        promote(path, model_id="DEMO_MODEL", version="v0_rw_baseline", to_status="LIMITED",
                at=DEMO_DATE)
        register(path, model_id="DEMO_MODEL", version="v1_ar1_improved",
                 code_sha256=code_hash("v1"), data_sha256=data_hash,
                 metrics=report["versions"]["v1"]["metrics"],
                 quality=versions["v1"]["quality"], evaluated_at=DEMO_EVALUATED,
                 author="automation", criteria=criteria_v1, note="improved parameterisation")
        promote(path, model_id="DEMO_MODEL", version="v1_ar1_improved", to_status="SHADOW",
                at=DEMO_DATE)
        promote(path, model_id="DEMO_MODEL", version="v1_ar1_improved", to_status="LIMITED",
                at=DEMO_DATE)
        for label, review in (
            ("no independent review", None),
            ("non-independent review", {"reviewer": "automation", "independent": False,
                                        "pass": True, "reviewed_at": DEMO_DATE,
                                        "report_sha256": sha256_text("demo-review-bad")}),
        ):
            try:
                promote(path, model_id="DEMO_MODEL", version="v1_ar1_improved",
                        to_status="ACTIVE", at=DEMO_DATE, independent_review=review)
                report["refusals"].append({"attempt": label, "refused": False,
                                           "error": None})
            except RegistryError as exc:
                report["refusals"].append({"attempt": label, "refused": True,
                                           "error": str(exc)})

        # Only now promote the baseline: both v1->ACTIVE attempts above were made while v0 was
        # still LIMITED, so each refusal comes from the review gate itself (no independent review /
        # non-independent reviewer), never from "already has an ACTIVE version".
        review_v0 = {"reviewer": "independent-reviewer-demo", "independent": True, "pass": True,
                     "reviewed_at": DEMO_DATE,
                     "report_sha256": sha256_text("demo-review-v0")}
        promote(path, model_id="DEMO_MODEL", version="v0_rw_baseline", to_status="ACTIVE",
                at=DEMO_DATE, independent_review=review_v0,
                note="SIMULATED independent review (demo only); a real ACTIVE needs a human")

        register(path, model_id="DEMO_MODEL", version="v2_ar1_biased_degraded",
                 code_sha256=code_hash("v2"), data_sha256=data_hash,
                 metrics=report["versions"]["v2"]["metrics"],
                 quality=versions["v2"]["quality"], evaluated_at=DEMO_EVALUATED,
                 author="automation", criteria=criteria_v2, note="degraded parameterisation")
        reject(path, model_id="DEMO_MODEL", version="v2_ar1_biased_degraded", at=DEMO_DATE,
               reason="walk-forward MAE worse than the naive benchmark and Q below the gate")

        revert(path, model_id="DEMO_MODEL", from_version="v1_ar1_improved",
               to_version="v0_rw_baseline", at=DEMO_DATE,
               reason="candidate cannot be promoted without a human independent review; "
                      "restore the stable ACTIVE baseline")

        events = load_events(path)
        report["events"] = len(events)
        report["registry_state"] = state(events)
        validation = validate_registry(path)
        if registry_path is None:
            validation.pop("path", None)
        report["registry_validation"] = validation
        report["registry_sha256"] = sha256_text(
            "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                    for event in events))
        if registry_path is None:
            report["registry_path"] = None
        else:
            report["registry_path"] = str(registry_path)
    return report


def _summary(report: dict) -> list[str]:
    lines = [f"Learning-loop demo ({report['date']}): 3 версии, Q и ворота реестра"]
    for key, version in report["versions"].items():
        metrics = version["metrics"]
        lines.append(
            f"  {key} {version['name']}: Q={version['q']:.4f} grade={version['grade']}; "
            f"MAE={metrics['mae']:.4f} vs naive {metrics['benchmark_mae']:.4f} "
            f"(rel {metrics['relative_mae']:.3f}); beats_naive={metrics['beats_naive']}")
    for name, decision in report["decisions"].items():
        lines.append(f"  {name}: {decision['decision']} (dQ={decision['delta_q']:+.4f})")
    for refusal in report["refusals"]:
        lines.append(f"  отказ ACTIVE ({refusal['attempt']}): "
                     f"{'да' if refusal['refused'] else 'НЕТ'}")
    state_data = report["registry_state"]["DEMO_MODEL"]
    statuses = report["registry_validation"]["models"]["DEMO_MODEL"]["versions"]
    lines.append(f"  реестр: events={report['events']}, active={state_data['active_version']}, "
                 f"versions={statuses}")
    lines.append(f"  validate: {report['registry_validation']['valid']}")
    for warning in report["warnings"]:
        lines.append(f"  ВНИМАНИЕ: {warning}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Learning-loop demo (Q, registry, walk-forward)")
    parser.add_argument("--out", default=None, help="write the JSON report to this path")
    parser.add_argument("--registry", default=None,
                        help="append-only registry JSONL path (default: temporary file)")
    parser.add_argument("--json", action="store_true", help="print the JSON report only")
    args = parser.parse_args(argv)
    report = run_demo(registry_path=Path(args.registry) if args.registry else None)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8", newline="\n")
    if args.json:
        print(payload)
    else:
        print("\n".join(_summary(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
