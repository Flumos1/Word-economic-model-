#!/usr/bin/env python3
"""Admission harness C1-C6 of the frozen AMFE evaluation protocol v4.

The harness is deliberately independent of the forecast models: every function takes plain numpy
arrays, so it can be tested on synthetic data before src/amfe/forecast exists and never imports it.

Implemented from protocols/evaluation_protocol_v4.json:
  losses       RMSE (point), CRPS = twice the mean pinball loss over the 19-quantile grid 5%..95%
               (distribution), Brier and log score with probabilities clipped to [0.01, 0.99] (event);
  C1           relative loss candidate/comparator <= threshold from protocols/nf_sim_thresholds.json
               by (target, horizon, loss, window), on W1 and W2 separately;
  C2           Giacomini-White with the constant as the only instrument (= Diebold-Mariano with a
               finite estimation window), HAC with h-1 lags, Harvey-Leybourne-Newbold small-sample
               correction, one-sided test that the candidate is better, p < 0.10 on W1 and W2; the
               Romano-Wolf adjusted p-value enters through apply_romano_wolf();
  C3           Model Confidence Set - reported as NOT_IMPLEMENTED (non-gating by the protocol);
  C4           recession origins: relative loss <= 1.00 and the conditional GW test (instruments:
               constant and the recession-month indicator) does not reject; fewer than 12 recession
               origins is inconclusive, which counts as not passed;
  C5           the C2 test with the opposite one-sided alternative, p >= 0.10 on W1 and W2, on the
               secondary actuals (T3: secondary == primary, so C5 repeats C2);
  C6           forward registry: >= 12 resolved forecasts, cumulative relative loss <= 1.00 and
               interval coverage within 10 pp of nominal (T3: calibration in probability bins,
               at least 3 non-empty bins, within 10 pp).

Simplifications (documented in docs/72):
  * C3 (Model Confidence Set) is not implemented - the protocol makes it non-gating;
  * the Romano-Wolf stepdown is implemented as a Holm stepdown on the family of candidates
    (conservative, valid FWER control under arbitrary dependence; it does not exploit the
    dependence as a full bootstrap Romano-Wolf does) - see apply_romano_wolf();
  * p-values use the normal approximation with the HLN correction instead of a t distribution;
  * C4 relative loss is checked on the recession subsets of W1 and W2 separately; the conditional
    GW test is a HAC Wald test of the recession-indicator coefficient in d_t = a + b*r_t + e_t;
  * C6 coverage is checked on the central 50%, 80% and 90% intervals and all three must be within
    10 pp of nominal; this is stricter than an unspecified single interval.

Loss differentially:
  * RMSE is aggregated as sqrt(mean squared error), but the DM/C2 test uses the squared-error
    differential (the standard Diebold-Mariano formulation for RMSE), while C1 compares RMSE ratios.

Usage:
    python tools/evaluate.py                 # synthetic self-test (identical methods and a bad one)
    import tools.evaluate as ev
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = ROOT / "protocols" / "nf_sim_thresholds.json"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"

QUANTILES = np.round(np.arange(0.05, 1.0, 0.05), 2)          # 19 quantiles, 5%..95% step 5
QUANTILE_LEVELS = [round(0.05 * i, 2) for i in range(1, 20)]
LOG_CLIP = (0.01, 0.99)
TARGET_LOSSES = {
    "T1_CPI_YOY": ["RMSE", "CRPS"],
    "T2_GDP_GROWTH": ["RMSE", "CRPS"],
    "T3_RECESSION": ["Brier", "log_score"],
}
TARGET_HORIZONS = {
    "T1_CPI_YOY": [1, 3, 6, 12],
    "T2_GDP_GROWTH": [0, 1, 2, 4],
    "T3_RECESSION": [3, 6, 12],
}
ALPHA = 0.10          # C2/C4/C5 one-sided level; Romano-Wolf FWER
MIN_FORWARD = 12      # C6 minimum resolved forecasts
COVERAGE_TOLERANCE = 0.10


class EvaluationError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- input validation
def _vector(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise EvaluationError(f"{name} must be one-dimensional")
    if array.size == 0:
        raise EvaluationError(f"{name} is empty")
    if not np.all(np.isfinite(array)):
        raise EvaluationError(f"{name} contains non-finite values")
    return array


def _same_length(*named: tuple[str, np.ndarray]) -> None:
    lengths = {name: array.shape[0] for name, array in named if array is not None}
    if len(set(lengths.values())) > 1:
        raise EvaluationError(f"length mismatch: {lengths}")


def loss_names(target: str) -> list[str]:
    if target not in TARGET_LOSSES:
        raise EvaluationError(f"unknown target {target}")
    return list(TARGET_LOSSES[target])


# ------------------------------------------------------------------------------------ losses
def pinball_matrix(quantiles, actual) -> np.ndarray:
    """Per-observation pinball loss of every grid quantile: shape (n, 19)."""
    q = np.asarray(quantiles, dtype=float)
    y = _vector(actual, "actual")
    if q.ndim != 2 or q.shape[0] != y.shape[0] or q.shape[1] != len(QUANTILES):
        raise EvaluationError(f"quantiles must have shape (n, {len(QUANTILES)})")
    if not np.all(np.isfinite(q)):
        raise EvaluationError("quantiles contain non-finite values")
    diff = y[:, None] - q
    return np.where(diff >= 0.0, QUANTILES[None, :] * diff,
                    (QUANTILES[None, :] - 1.0) * diff)


def crps(quantiles, actual) -> np.ndarray:
    """Protocol CRPS per observation: twice the mean pinball loss over the 19 quantiles."""
    return 2.0 * pinball_matrix(quantiles, actual).mean(axis=1)


def rmse_squared_error(point, actual) -> np.ndarray:
    point = _vector(point, "point")
    y = _vector(actual, "actual")
    _same_length(("point", point), ("actual", y))
    return (y - point) ** 2


def brier_squared_error(probability, actual) -> np.ndarray:
    p = _vector(probability, "probability")
    y = _vector(actual, "actual")
    _same_length(("probability", p), ("actual", y))
    if np.any((p < 0.0) | (p > 1.0)):
        raise EvaluationError("probabilities must lie in [0, 1]")
    return (y - p) ** 2


def log_score_observation(probability, actual, clip=LOG_CLIP) -> np.ndarray:
    p = np.clip(_vector(probability, "probability"), *clip)
    y = _vector(actual, "actual")
    _same_length(("probability", p), ("actual", y))
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


def observation_loss(loss: str, actual, *, point=None, quantiles=None, probability=None) -> np.ndarray:
    """Per-observation loss series used for the tests (aggregated loss is loss_of())."""
    if loss == "RMSE":
        if point is None:
            raise EvaluationError("RMSE needs point forecasts")
        return rmse_squared_error(point, actual)
    if loss == "CRPS":
        if quantiles is None:
            raise EvaluationError("CRPS needs quantile forecasts")
        return crps(quantiles, actual)
    if loss == "Brier":
        if probability is None:
            raise EvaluationError("Brier needs event probabilities")
        return brier_squared_error(probability, actual)
    if loss == "log_score":
        if probability is None:
            raise EvaluationError("log_score needs event probabilities")
        return log_score_observation(probability, actual)
    raise EvaluationError(f"unknown loss {loss}")


def aggregate_loss(loss: str, series: np.ndarray) -> float:
    if loss == "RMSE":
        return float(np.sqrt(np.mean(series)))
    return float(np.mean(series))


# --------------------------------------------------------------------------------- threshold C1
def load_thresholds(path: Path | str = THRESHOLDS) -> dict:
    """Load the frozen NF-SIM table and re-derive every threshold from its q05 components."""
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for entry in table["entries"]:
        derived = round(min(0.97, entry["q05_single"], entry["q05_combination"]), 4)
        if derived != entry["threshold"]:
            raise EvaluationError(f"threshold mismatch for {entry['target']}/{entry['horizon']}/"
                                  f"{entry['loss']}/{entry['window']}: {entry['threshold']} != {derived}")
        key = (entry["target"], int(entry["horizon"]), entry["loss"], entry["window"])
        if key in out:
            raise EvaluationError(f"duplicate threshold entry {key}")
        out[key] = entry["threshold"]
    return out


def threshold_for(table: dict, target: str, horizon: int, loss: str, window: str) -> float:
    key = (target, int(horizon), loss, window)
    if key not in table:
        raise EvaluationError(f"no NF-SIM threshold for {key}")
    return float(table[key])


def relative_loss(candidate_loss: float, comparator_loss: float) -> float:
    if comparator_loss <= 0.0:
        raise EvaluationError("comparator loss must be positive to form a relative loss")
    return candidate_loss / comparator_loss


def c1_window(candidate_loss: float, comparator_loss: float, threshold: float) -> dict:
    ratio = relative_loss(candidate_loss, comparator_loss)
    return {"candidate_loss": candidate_loss, "comparator_loss": comparator_loss,
            "relative_loss": ratio, "threshold": threshold, "pass": bool(ratio <= threshold)}


# ----------------------------------------------------------------------- HAC / DM / GW tests
def newey_west_variance(series: np.ndarray, lags: int) -> float:
    """Bartlett-kernel HAC estimate of Var(sqrt(n) * mean(series))."""
    x = _vector(series, "series") - float(np.mean(series))
    n = x.size
    lags = int(max(0, min(lags, n - 1)))
    total = float(np.dot(x, x)) / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = float(np.dot(x[lag:], x[:-lag])) / n
        total += 2.0 * weight * gamma
    return max(total, 0.0)


def hln_factor(n: int, horizon: int) -> float:
    """Harvey-Leybourne-Newbold small-sample correction for h-step forecast errors."""
    h = max(1, int(horizon))
    if h == 1:
        return 1.0
    return math.sqrt(max(1e-12, (n + 1 - 2 * h + h * (h - 1) / n) / n))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _chi2_survival(stat: float, df: int) -> float:
    """Chi-square survival function; closed forms for df 1 and 2, Wilson-Hilferty otherwise."""
    if df <= 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(max(0.0, stat) / 2.0))
    if df == 2:
        return math.exp(-max(0.0, stat) / 2.0)
    z = ((stat / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    return 1.0 - _normal_cdf(z)


def dm_test(differential, horizon: int = 1, alternative: str = "less") -> dict:
    """Diebold-Mariano / Giacomini-White test with the constant as the only instrument.

    differential = loss_candidate - loss_comparator per observation (a negative mean favours the
    candidate).  alternative:
      "less"    H1: E[d] < 0 (candidate better)  - the C2 test;
      "greater" H1: E[d] > 0 (candidate worse)   - the opposite alternative used by C5;
      "two-sided".
    HAC bandwidth h-1, HLN correction applied, normal approximation for the p-value.
    """
    d = _vector(differential, "differential")
    n = d.size
    mean = float(np.mean(d))
    variance = newey_west_variance(d, max(0, int(horizon) - 1))
    hln = hln_factor(n, horizon)
    if variance <= 0.0:
        if abs(mean) <= 1e-15:
            statistic = 0.0
            p_value = 1.0 if alternative == "two-sided" else 0.5
        else:
            statistic = math.copysign(math.inf, mean)
            p_value = 1.0 if (mean > 0) == (alternative == "less") else 0.0
        return {"mean_differential": mean, "statistic": statistic, "p_value": p_value,
                "hac_lags": max(0, int(horizon) - 1), "hln_factor": hln, "n": n,
                "alternative": alternative,
                "note": "degenerate differential (zero HAC variance); p-value is the limit"}
    statistic = mean / math.sqrt(variance / n) * hln
    if alternative == "less":
        p_value = _normal_cdf(statistic)
    elif alternative == "greater":
        p_value = 1.0 - _normal_cdf(statistic)
    elif alternative == "two-sided":
        p_value = 2.0 * min(_normal_cdf(statistic), 1.0 - _normal_cdf(statistic))
    else:
        raise EvaluationError(f"unknown alternative {alternative}")
    return {"mean_differential": mean, "statistic": statistic, "p_value": max(0.0, min(1.0, p_value)),
            "hac_lags": max(0, int(horizon) - 1), "hln_factor": hln, "n": n,
            "alternative": alternative,
            "note": "HAC h-1 lags, HLN correction, normal approximation instead of t(n-1)"}


def gw_conditional_test(differential, instruments, horizon: int = 1) -> dict:
    """Giacomini-White test with instruments: HAC Wald test that the non-constant
    instrument coefficients of d_t = a + b' z_t + e_t are zero.

    `instruments` is a 1-D array or an (n, k) matrix of non-constant instruments.  For C4 it is
    the recession-month indicator.  Returns the joint p-value and, when there is exactly one
    instrument, the coefficient and the estimated mean differential in the instrument==1 group
    (a + b), which the protocol calls the mean loss difference in recession months.
    """
    d = _vector(differential, "differential")
    z = np.asarray(instruments, dtype=float)
    if z.ndim == 1:
        z = z[:, None]
    if z.ndim != 2 or z.shape[0] != d.shape[0]:
        raise EvaluationError("instruments must have one row per observation")
    if not np.all(np.isfinite(z)):
        raise EvaluationError("instruments contain non-finite values")
    n, k = z.shape
    if np.all(np.ptp(z, axis=0) == 0.0):
        result = dm_test(d, horizon, "two-sided")
        result["kind"] = "constant_only"
        return result
    design = np.column_stack([np.ones(n), z])
    cols = design.shape[1]
    if n <= cols:
        raise EvaluationError("too few observations for the instruments")
    bread = np.linalg.pinv(design.T @ design)
    beta = bread @ (design.T @ d)
    residual = d - design @ beta
    u = residual[:, None] * design
    meat = u.T @ u
    lags = max(0, int(horizon) - 1)
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = u[lag:].T @ u[:-lag]
        meat += weight * (gamma + gamma.T)
    covariance = bread @ meat @ bread
    slope = beta[1:]
    cov_slope = covariance[1:, 1:]
    try:
        wald = float(slope @ np.linalg.solve(cov_slope, slope))
    except np.linalg.LinAlgError:
        wald = float(slope @ np.linalg.pinv(cov_slope) @ slope)
    result = {"kind": "instruments", "p_value": max(0.0, min(1.0, _chi2_survival(wald, k))),
              "wald": wald, "df": k, "n": n, "hac_lags": lags,
              "coefficients": [float(v) for v in beta], "instrument_count": k,
              "note": "HAC Wald test of the instrument coefficients; normal/chi-square approximation"}
    if k == 1:
        result["slope"] = float(beta[1])
        result["mean_differential_instrument_1"] = float(beta[0] + beta[1])
    return result


# -------------------------------------------------------------------------------- C4 helpers
def c4_recession_condition(differential, recession_mask, horizon: int = 1,
                           relative_loss_recession: float | None = None,
                           min_recession_origins: int = 12) -> dict:
    """C4: recession origins split, the conditional GW test and the 12-month minimum."""
    d = _vector(differential, "differential")
    mask = np.asarray(recession_mask, dtype=bool)
    if mask.shape != d.shape:
        raise EvaluationError("recession_mask must match the differential length")
    count = int(np.count_nonzero(mask))
    conditional = gw_conditional_test(d, mask.astype(float), horizon)
    rejected = conditional["p_value"] < ALPHA
    mean_recession = conditional.get("mean_differential_instrument_1")
    favours_comparator = (mean_recession is not None) and (mean_recession > 0.0)
    passed_relative = (relative_loss_recession is None) or (relative_loss_recession <= 1.0)
    if count < min_recession_origins:
        status = "INCONCLUSIVE"
    elif not passed_relative:
        status = "FAIL"
    elif rejected and favours_comparator:
        status = "FAIL"
    else:
        status = "PASS"
    return {"status": status, "recession_origins": count,
            "min_recession_origins": min_recession_origins,
            "relative_loss_recession": relative_loss_recession,
            "relative_loss_met": passed_relative,
            "conditional_gw_p": conditional["p_value"],
            "conditional_gw_rejected": rejected,
            "mean_differential_recession": mean_recession,
            "favours_comparator_in_recession": favours_comparator,
            "pass": status == "PASS",
            "note": "C4 pass requires >= 12 recession origins, relative loss <= 1.00 and no "
                    "conditional rejection that favours the comparator"}


# -------------------------------------------------------------------------------- C6 helpers
def interval_coverage(quantiles, actual) -> dict:
    """Central-interval empirical coverage for the nominal 50%, 80% and 90% intervals."""
    q = np.asarray(quantiles, dtype=float)
    y = _vector(actual, "actual")
    intervals = {}
    for nominal, low_tau, high_tau in ((0.50, 0.25, 0.75), (0.80, 0.10, 0.90), (0.90, 0.05, 0.95)):
        low = q[:, QUANTILE_LEVELS.index(low_tau)]
        high = q[:, QUANTILE_LEVELS.index(high_tau)]
        coverage = float(np.mean((y >= low) & (y <= high)))
        intervals[f"nominal_{nominal:.2f}"] = {
            "coverage": coverage, "nominal": nominal,
            "deviation": coverage - nominal,
            "within_tolerance": abs(coverage - nominal) <= COVERAGE_TOLERANCE,
        }
    return intervals


def calibration_bins(probability, actual, bins: int = 5) -> dict:
    """C6/T3 calibration: mean forecast probability against event frequency in equal-width bins."""
    p = _vector(probability, "probability")
    y = _vector(actual, "actual")
    _same_length(("probability", p), ("actual", y))
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(p, edges[1:-1], right=False), 0, bins - 1)
    table = []
    for b in range(bins):
        member = index == b
        if not np.any(member):
            continue
        mean_probability = float(np.mean(p[member]))
        frequency = float(np.mean(y[member]))
        table.append({"bin": b, "range": [float(edges[b]), float(edges[b + 1])],
                      "n": int(np.count_nonzero(member)),
                      "mean_probability": mean_probability, "frequency": frequency,
                      "deviation": mean_probability - frequency,
                      "within_tolerance": abs(mean_probability - frequency) <= COVERAGE_TOLERANCE})
    passed = len(table) >= 3 and all(row["within_tolerance"] for row in table)
    return {"bins": table, "n_bins": len(table), "pass": bool(passed),
            "note": "at least 3 non-empty probability bins, |mean probability - frequency| <= 0.10"}


def c6_forward_check(target: str, realized, *, candidate_point=None, candidate_quantiles=None,
                     candidate_probability=None, comparator_point=None, comparator_quantiles=None,
                     comparator_probability=None, min_forecasts: int = MIN_FORWARD) -> dict:
    """C6 on the resolved forward-registry rows of one (target, horizon) pair."""
    y = _vector(realized, "realized")
    losses = loss_names(target)
    per_loss = {}
    passed_losses = True
    for loss in losses:
        cand = observation_loss(loss, y, point=candidate_point, quantiles=candidate_quantiles,
                                probability=candidate_probability)
        comp_arrays = {"RMSE": (comparator_point, None, None), "CRPS": (None, comparator_quantiles, None),
                       "Brier": (None, None, comparator_probability),
                       "log_score": (None, None, comparator_probability)}[loss]
        comp = observation_loss(loss, y, point=comp_arrays[0], quantiles=comp_arrays[1],
                                probability=comp_arrays[2])
        cumulative = relative_loss(float(np.sum(cand)), float(np.sum(comp)))
        entry = {"candidate_loss": aggregate_loss(loss, cand),
                 "comparator_loss": aggregate_loss(loss, comp),
                 "cumulative_relative_loss": cumulative,
                 "relative_loss_met": cumulative <= 1.0}
        if target == "T3_RECESSION":
            entry["calibration"] = calibration_bins(candidate_probability, y)
            passed_losses = passed_losses and entry["calibration"]["pass"]
        per_loss[loss] = entry
    coverage = None
    if target != "T3_RECESSION":
        coverage = interval_coverage(candidate_quantiles, y)
        coverage_pass = all(row["within_tolerance"] for row in coverage.values())
    else:
        coverage_pass = True
    count_ok = y.size >= min_forecasts
    loss_ok = all(entry["relative_loss_met"] for entry in per_loss.values())
    passed = count_ok and loss_ok and passed_losses and coverage_pass
    return {"target": target, "n_resolved": int(y.size), "min_forecasts": min_forecasts,
            "count_met": count_ok, "per_loss": per_loss, "coverage": coverage,
            "coverage_met": coverage_pass, "pass": bool(passed),
            "note": "T3 unresolved forecasts do not enter the count; the completeness deadline "
                    "delay must be stated in every T3 report"}


# ------------------------------------------------------------------------ Romano-Wolf (simplified)
def romano_wolf_simplified(p_values: dict[str, float], fwer: float = ALPHA) -> dict:
    """Conservative Romano-Wolf stepdown: Holm on the family of candidates.

    A full Romano-Wolf needs the joint bootstrap distribution of the test statistics; this
    implementation uses the Holm stepdown, which controls the family-wise error rate at `fwer`
    under arbitrary dependence but is conservative relative to the bootstrap version.
    The adjusted p-values are reported and compared with `fwer`.
    """
    if not p_values:
        raise EvaluationError("empty family")
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    m = len(ordered)
    adjusted: dict[str, float] = {}
    previous = 0.0
    for rank, (name, p) in enumerate(ordered, start=1):
        value = min(1.0, (m - rank + 1) * p)
        previous = max(previous, value)
        adjusted[name] = previous
    return {"method": "HOLM_STEPDOWN_CONSERVATIVE_ROMANO_WOLF_PROXY",
            "fwer": fwer,
            "adjusted_p_values": adjusted,
            "rejected": sorted(name for name, value in adjusted.items() if value < fwer),
            "note": "conservative stepdown proxy for Romano-Wolf; the full bootstrap version is "
                    "not implemented, the reported p-values are FWER-adjusted (Holm)"}


def family_p_value(report: dict) -> float:
    """Raw family test statistic of a candidate: the worst C2 one-sided p over losses and windows."""
    values = []
    windows = report.get("windows")
    if windows is None:  # full admission report: criteria.C2.windows
        windows = report.get("criteria", {}).get("C2", {}).get("windows", {})
    for block in windows.values():
        for loss, entry in block.items():
            if isinstance(entry, dict) and "p_value" in entry:
                values.append(entry["p_value"])
            elif isinstance(entry, dict) and "c2" in entry:
                values.append(entry["c2"]["p_value"])
    return max(values) if values else 1.0


def apply_romano_wolf(reports: dict[str, dict], fwer: float = ALPHA) -> dict:
    """Adjust the C2 p-values of a family of candidates and set the multiplicity verdict."""
    adjusted = romano_wolf_simplified({name: family_p_value(report)
                                       for name, report in reports.items()}, fwer)
    for name, report in reports.items():
        report["criteria"]["C2"]["romano_wolf_adjusted_p"] = adjusted["adjusted_p_values"][name]
        report["criteria"]["C2"]["romano_wolf_pass"] = (
            adjusted["adjusted_p_values"][name] < fwer)
    return adjusted


# --------------------------------------------------------------------------------- window block
def evaluate_window(window: str, target: str, horizon: int, actual_primary, actual_secondary=None,
                    *, candidate: dict, comparator: dict, table: dict | None = None,
                    recession_mask=None) -> dict:
    """All loss and test results for one evaluation window.

    candidate/comparator are dicts with the keys the target needs:
      point (array), quantiles ((n, 19) array), probability (array for T3).
    actual_secondary is the value in the vintage available 12 months after the reference period;
    for T3 it equals the primary actual.
    """
    table = load_thresholds() if table is None else table
    y_primary = _vector(actual_primary, "actual_primary")
    y_secondary = y_primary if actual_secondary is None else _vector(actual_secondary,
                                                                      "actual_secondary")
    if target == "T3_RECESSION" and actual_secondary is not None:
        _same_length(("primary", y_primary), ("secondary", y_secondary))
    losses = {}
    for loss in loss_names(target):
        cand = observation_loss(loss, y_primary, point=candidate.get("point"),
                                quantiles=candidate.get("quantiles"),
                                probability=candidate.get("probability"))
        comp = observation_loss(loss, y_primary, point=comparator.get("point"),
                                quantiles=comparator.get("quantiles"),
                                probability=comparator.get("probability"))
        cand_loss = aggregate_loss(loss, cand)
        comp_loss = aggregate_loss(loss, comp)
        threshold = threshold_for(table, target, horizon, loss, window)
        differential = cand - comp
        c2 = dm_test(differential, horizon, "less")
        secondary_cand = observation_loss(loss, y_secondary, point=candidate.get("point"),
                                          quantiles=candidate.get("quantiles"),
                                          probability=candidate.get("probability"))
        secondary_comp = observation_loss(loss, y_secondary, point=comparator.get("point"),
                                          quantiles=comparator.get("quantiles"),
                                          probability=comparator.get("probability"))
        c5 = dm_test(secondary_cand - secondary_comp, horizon, "greater")
        losses[loss] = {
            "c1": c1_window(cand_loss, comp_loss, threshold),
            "c2": c2,
            "c5": c5,
        }
    block = {"target": target, "horizon": int(horizon), "window": window,
             "n_origins": int(y_primary.size), "losses": losses}
    if recession_mask is not None:
        mask = np.asarray(recession_mask, dtype=bool)
        if mask.shape != y_primary.shape:
            raise EvaluationError("recession_mask must match the origins")
        block["recession"] = {}
        for loss in losses:
            cand = observation_loss(loss, y_primary, point=candidate.get("point"),
                                    quantiles=candidate.get("quantiles"),
                                    probability=candidate.get("probability"))
            comp = observation_loss(loss, y_primary, point=comparator.get("point"),
                                    quantiles=comparator.get("quantiles"),
                                    probability=comparator.get("probability"))
            ratio = None
            if np.any(mask) and np.sum(comp[mask]) > 0:
                ratio = float(np.sum(cand[mask]) / np.sum(comp[mask]))
            block["recession"][loss] = c4_recession_condition(cand - comp, mask, horizon,
                                                              relative_loss_recession=ratio)
    return block


# --------------------------------------------------------------------------------- admission
def evaluate_admission(target: str, horizon: int, windows: dict[str, dict], *,
                       forward: dict | None = None, min_members: int | None = None,
                       table: dict | None = None, data_class: str = "UNSTATED") -> dict:
    """Full C1-C6 admission report for one (target, horizon) pair.

    windows: {"W1": data, "W2": data} where data has actual_primary, actual_secondary, candidate,
    comparator and optionally recession.  `forward` is the output of c6_forward_check() for the
    resolved registry rows (None when the journal has no resolved forecast for the pair yet).

    data_class is the protocol v4 data-depth class of the evaluated forecasts: admission is only
    allowed for REAL_TIME or REAL_TIME_TAPE.  PSEUDO_REAL_TIME (the latest tape truncated to the
    origin) and UNSTATED can only produce a CANDIDATE report; they never advance a stage.
    """
    table = load_thresholds() if table is None else table
    if target not in TARGET_HORIZONS:
        raise EvaluationError(f"unknown target {target}")
    if int(horizon) not in TARGET_HORIZONS[target]:
        raise EvaluationError(f"horizon {horizon} is not defined for {target}")
    if set(windows) != {"W1", "W2"}:
        raise EvaluationError("the report needs exactly the W1 and W2 windows")
    blocks = {}
    for window in ("W1", "W2"):
        data = windows[window]
        blocks[window] = evaluate_window(
            window, target, horizon, data["actual_primary"], data.get("actual_secondary"),
            candidate=data["candidate"], comparator=data["comparator"], table=table,
            recession_mask=data.get("recession"))
    losses = loss_names(target)
    per_window = {}
    for window, block in blocks.items():
        per_window[window] = {loss: {"c1": block["losses"][loss]["c1"],
                                     "c2": block["losses"][loss]["c2"],
                                     "c5": block["losses"][loss]["c5"]}
                              for loss in losses}
    c1_pass = all(per_window[w][loss]["c1"]["pass"] for w in per_window for loss in losses)
    c2_pass = all(per_window[w][loss]["c2"]["p_value"] < ALPHA
                  for w in per_window for loss in losses)
    c5_pass = all(per_window[w][loss]["c5"]["p_value"] >= ALPHA
                  for w in per_window for loss in losses)
    c4_block = None
    if target != "T3_RECESSION":
        c4_block = {"applies": True, "per_window": {}}
        c4_pass = True
        for window in ("W1", "W2"):
            data = windows[window]
            if data.get("recession") is None:
                c4_block["per_window"][window] = {"status": "MISSING_RECESSION_MASK", "pass": False}
                c4_pass = False
                continue
            window_result = blocks[window].get("recession")
            c4_block["per_window"][window] = window_result
            c4_pass = c4_pass and all(result["pass"] for result in window_result.values())
        c4_block["pass"] = c4_pass
    else:
        c4_block = {"applies": False, "pass": True,
                    "reason": "protocol targets.T3_RECESSION.c4_not_applicable: C4 does not apply to T3"}
    c3_block = {"status": "NOT_IMPLEMENTED", "gating": False, "pass": None,
                "reason": "Model Confidence Set is not implemented by this harness; protocol "
                          "admission.criteria C3 makes it non-gating (membership and set size only)"}
    if forward is None:
        c6_block = {"status": "NO_RESOLVED_FORECASTS", "pass": False,
                    "reason": "the forward registry has no resolved forecast for the pair"}
    else:
        c6_block = dict(forward)
        c6_block["status"] = "PASS" if forward["pass"] else "FAIL"
    historical_gate = c1_pass and c2_pass and c5_pass and c4_block["pass"]
    if historical_gate and c6_block["pass"]:
        status = "LIMITED_DEPLOYMENT"
    elif historical_gate:
        status = "SHADOW_TEST"
    else:
        status = "CANDIDATE"
    if target == "T3_RECESSION" and status == "LIMITED_DEPLOYMENT":
        status = "LIMITED_DEPLOYMENT"
    admission_allowed = data_class in ("REAL_TIME", "REAL_TIME_TAPE")
    data_class_note = ("admission allowed: REAL_TIME data" if admission_allowed else
                       f"admission NOT allowed: data class is {data_class}; protocol v4 "
                       "data_depth.classification_rule counts only REAL_TIME and REAL_TIME_TAPE")
    if status != "CANDIDATE" and not admission_allowed:
        status = "CANDIDATE"
    report = {
        "protocol": "protocols/evaluation_protocol_v4.json",
        "target": target,
        "horizon": int(horizon),
        "min_comparator_members": min_members,
        "criteria": {
            "C1": {"gating": True, "pass": c1_pass,
                   "threshold_rule": "min(0.97, q05_equal_methods) from protocols/nf_sim_thresholds.json",
                   "windows": {w: {loss: per_window[w][loss]["c1"] for loss in losses} for w in per_window}},
            "C2": {"gating": True, "pass": c2_pass,
                   "windows": {w: {loss: {"p_value": per_window[w][loss]["c2"]["p_value"],
                                           "statistic": per_window[w][loss]["c2"]["statistic"],
                                           "hac_lags": per_window[w][loss]["c2"]["hac_lags"],
                                           "hln_factor": per_window[w][loss]["c2"]["hln_factor"]}
                                     for loss in losses} for w in per_window}},
            "C3": c3_block,
            "C4": dict(c4_block, gating=True),
            "C5": {"gating": True, "pass": c5_pass,
                   "alternative": "greater (candidate worse); p >= 0.10 required",
                   "windows": {w: {loss: per_window[w][loss]["c5"] for loss in losses}
                               for w in per_window}},
            "C6": dict(c6_block, gating=True),
        },
        "multiplicity": {"primary": "Romano-Wolf stepdown FWER 0.10 - see apply_romano_wolf()",
                         "secondary": "Benjamini-Hochberg FDR q=0.10, reported only"},
        "stages": {
            "CANDIDATE_to_SHADOW_TEST": historical_gate,
            "SHADOW_TEST_to_LIMITED_DEPLOYMENT": historical_gate and c6_block["pass"],
            "LIMITED_DEPLOYMENT_to_ACTIVE": False,
        },
        "status": status,
        "data_class": data_class,
        "admission_allowed": admission_allowed,
        "data_class_note": data_class_note,
        "notes": ["C3 not implemented (non-gating)", "C4/C5/C6 simplifications in docs/72",
                  "W1 for T3 starts 1996-12 (windows.target_overrides.T3_RECESSION.W1_start)",
                  data_class_note],
    }
    return report


# --------------------------------------------------------------------------------- self-test
def _synthetic_demo() -> dict:
    rng = np.random.default_rng(20261111)
    n = 60
    horizon = 3
    actual = rng.normal(2.0, 1.0, size=n)
    shock = rng.normal(0.0, 1.0, size=n)
    comparator_point = actual + shock
    candidate_point = actual + 0.7 * shock
    bad_point = actual + 2.5 * shock
    shapes = np.linspace(-1.5, 1.5, len(QUANTILES))
    comparator_q = comparator_point[:, None] + shapes[None, :]
    candidate_q = candidate_point[:, None] + shapes[None, :]
    recession = np.zeros(n, dtype=bool)
    recession[::5] = True

    def window(actual, cand_point, cand_q, comp_point, comp_q):
        return {"actual_primary": actual, "actual_secondary": actual + rng.normal(0, 0.1, n),
                "candidate": {"point": cand_point, "quantiles": cand_q},
                "comparator": {"point": comp_point, "quantiles": comp_q},
                "recession": recession}

    windows = {"W1": window(actual, candidate_point, candidate_q, comparator_point, comparator_q),
               "W2": window(actual, candidate_point, candidate_q, comparator_point, comparator_q)}
    good = evaluate_admission("T1_CPI_YOY", horizon, windows)
    bad = evaluate_admission("T1_CPI_YOY", horizon,
                             {"W1": window(actual, bad_point, bad_point[:, None] + shapes[None, :],
                                           comparator_point, comparator_q),
                              "W2": window(actual, bad_point, bad_point[:, None] + shapes[None, :],
                                           comparator_point, comparator_q)})
    identical = evaluate_admission("T1_CPI_YOY", horizon,
                                   {"W1": window(actual, comparator_point, comparator_q,
                                                 comparator_point, comparator_q),
                                    "W2": window(actual, comparator_point, comparator_q,
                                                 comparator_point, comparator_q)})
    p = np.clip(0.2 + 0.5 * (rng.random(n) < 0.3), 0.01, 0.99)
    event = (rng.random(n) < 0.3).astype(float)
    forward = c6_forward_check("T1_CPI_YOY", actual, candidate_point=candidate_point,
                               candidate_quantiles=candidate_q, comparator_point=comparator_point,
                               comparator_quantiles=comparator_q)
    return {
        "good_candidate": {"status": good["status"],
                           "C1": good["criteria"]["C1"]["pass"],
                           "C2": good["criteria"]["C2"]["pass"],
                           "C4": good["criteria"]["C4"]["pass"],
                           "C5": good["criteria"]["C5"]["pass"]},
        "identical_candidate": {
            "status": identical["status"],
            "relative_loss": identical["criteria"]["C1"]["windows"]["W1"]["RMSE"]["relative_loss"]},
        "bad_candidate": {"status": bad["status"], "C1": bad["criteria"]["C1"]["pass"],
                          "C2": bad["criteria"]["C2"]["pass"]},
        "c6_t1": {"n_resolved": forward["n_resolved"], "pass": forward["pass"],
                  "coverage": {k: v["coverage"] for k, v in forward["coverage"].items()}},
        "t3_forward_row": {"n_resolved": n, "brier": aggregate_loss(
            "Brier", brier_squared_error(p, event)), "log_score": aggregate_loss(
            "log_score", log_score_observation(p, event))},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Admission harness C1-C6 (protocol v4)")
    parser.add_argument("--self-test", action="store_true", help="run the synthetic self-test")
    args = parser.parse_args(argv)
    if args.self_test:
        print(json.dumps(_synthetic_demo(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
