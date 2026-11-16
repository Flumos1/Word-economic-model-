"""Quality function Q for the learning/validation loop (wave 21, track F, docs/145, docs/151).

Q = f(E, C, S, R): four normalized components in [0, 1] combined with documented weights, plus a
separate set of **hard constraints** that are not tradable: a model that violates a balance, leaks
future data, has too small an out-of-sample sample or has no naive benchmark cannot receive a Q at
all (the function fails closed instead of returning a misleading score).

Components (all normalized to [0, 1], higher is better)
-------------------------------------------------------
E  error        ``rel = error / benchmark_error`` (MAE, RMSE or CRPS against the naive benchmark);
                ``E = clip((ceiling - rel) / (ceiling - floor), 0, 1)`` with floor 0.5 and
                ceiling 2.0: a perfect half-of-naive model scores 1, a model equal to naive scores
                2/3, a model twice as bad scores 0.
C  consistency  ``C = clip(1 - violations / max_violations, 0, 1)``: balance/accounting or protocol
                mismatches found by the validators (default ``max_violations = 3``).
S  stability    from the trailing error ratio ``cv = std(fold errors) / mean(fold errors)``:
                ``S = clip((ceiling - cv) / (ceiling - floor), 0, 1)`` with floor 0.25, ceiling 1.0;
                absent stability input -> S is not used and the weights are renormalized.
R  risk         interval coverage / calibration: ``R = clip(1 - |coverage - nominal| / tolerance, 0,
                1)`` with nominal 0.80 and tolerance 0.20; absent coverage -> R is not used and the
                weights are renormalized (the result records ``components_used``).

Weights: E 0.40, C 0.20, S 0.20, R 0.20 (documented constants; a caller may pass explicit weights,
but they must cover exactly the components used and sum to 1).

Hard constraints (fail-closed, independent of the score)
--------------------------------------------------------
1. ``balance_residual_max <= balance_tolerance`` (default 1e-6 absolute; scale-aware if
   ``balance_scale`` is given);
2. ``future_leakage`` must be false (walk-forward/PIT check);
3. ``n_oos >= min_oos`` (default 12 out-of-sample observations);
4. ``benchmark_error > 0`` (a naive benchmark is required to normalize the error);
5. all numeric inputs finite and non-negative where they are counts/errors.

Grades: A >= 0.75, B >= 0.60, C >= 0.45, otherwise D.  Version comparison returns
``IMPROVED``/``DEGRADED``/``TIE`` on the Q delta with a documented minimum improvement
(``min_improvement = 0.01``) and requires both results to pass the hard constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

TOLERANCE = 1e-6
DEFAULT_WEIGHTS = {"E": 0.40, "C": 0.20, "S": 0.20, "R": 0.20}
COMPONENTS = ("E", "C", "S", "R")
ERROR_FLOOR = 0.5
ERROR_CEILING = 2.0
STABILITY_FLOOR = 0.25
STABILITY_CEILING = 1.0
NOMINAL_COVERAGE = 0.80
COVERAGE_TOLERANCE = 0.20
MAX_VIOLATIONS = 3
MIN_OOS = 12
MIN_IMPROVEMENT = 0.01
GRADES = ((0.75, "A"), (0.60, "B"), (0.45, "C"), (0.0, "D"))


class QualityError(ValueError):
    """Invalid input or a violated hard constraint: no Q is assigned."""


@dataclass(frozen=True)
class QualityResult:
    """A computed quality result (immutable, JSON-friendly through :meth:`as_dict`)."""

    q: float
    grade: str
    components: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    hard_constraints: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"schema": "AMFE-QUALITY/1.0.0", "q": round(self.q, 6), "grade": self.grade,
                "components": {key: round(value, 6) for key, value in self.components.items()},
                "weights": dict(self.weights),
                "hard_constraints": dict(self.hard_constraints),
                "inputs": dict(self.inputs)}


def _finite(value, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise QualityError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise QualityError(f"{name} must be >= {minimum}, got {number}")
    return number


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def check_hard_constraints(metrics: Mapping, *, balance_tolerance: float = TOLERANCE,
                           min_oos: int = MIN_OOS) -> dict:
    """Validate the non-tradable constraints; raises :class:`QualityError` on the first violation."""
    if not isinstance(metrics, Mapping):
        raise QualityError("metrics must be a mapping")
    required = ("error", "benchmark_error", "balance_residual_max", "n_oos")
    missing = [key for key in required if key not in metrics]
    if missing:
        raise QualityError(f"metrics are missing hard-constraint inputs: {missing}")
    checks: dict = {}
    error = _finite(metrics["error"], "error", minimum=0.0)
    benchmark = _finite(metrics["benchmark_error"], "benchmark_error", minimum=0.0)
    if benchmark <= 0.0:
        raise QualityError("benchmark_error must be positive (a naive benchmark is required)")
    residual = _finite(metrics["balance_residual_max"], "balance_residual_max", minimum=0.0)
    scale = metrics.get("balance_scale")
    if scale is not None:
        scale = abs(_finite(scale, "balance_scale"))
        tolerance = balance_tolerance * max(1.0, scale)
    else:
        tolerance = float(balance_tolerance)
    if residual > tolerance:
        raise QualityError(
            f"balance constraint violated: max residual {residual:.6e} > {tolerance:.3e}")
    leakage = metrics.get("future_leakage", False)
    if leakage:
        raise QualityError("future leakage is not allowed (walk-forward/PIT constraint)")
    n_oos = metrics["n_oos"]
    if isinstance(n_oos, bool) or not isinstance(n_oos, int) or n_oos < int(min_oos):
        raise QualityError(f"out-of-sample count must be an integer >= {min_oos}, got {n_oos!r}")
    checks.update({"error": error, "benchmark_error": benchmark,
                   "relative_error": error / benchmark,
                   "balance_residual_max": residual, "balance_tolerance": tolerance,
                   "future_leakage": False, "n_oos": int(n_oos), "min_oos": int(min_oos),
                   "pass": True})
    return checks


def _error_component(relative_error: float) -> float:
    return _clip((ERROR_CEILING - relative_error) / (ERROR_CEILING - ERROR_FLOOR))


def _stability_component(stability_cv: float) -> float:
    return _clip((STABILITY_CEILING - stability_cv) / (STABILITY_CEILING - STABILITY_FLOOR))


def _risk_component(coverage: float, nominal: float = NOMINAL_COVERAGE,
                    tolerance: float = COVERAGE_TOLERANCE) -> float:
    return _clip(1.0 - abs(float(coverage) - float(nominal)) / float(tolerance))


def grade_for(q: float) -> str:
    for threshold, letter in GRADES:
        if q >= threshold:
            return letter
    return "D"


def evaluate_quality(metrics: Mapping, *, weights: Mapping | None = None,
                     balance_tolerance: float = TOLERANCE, min_oos: int = MIN_OOS,
                     max_violations: int = MAX_VIOLATIONS) -> QualityResult:
    """Compute the quality result; raises :class:`QualityError` when a hard constraint fails."""
    checks = check_hard_constraints(metrics, balance_tolerance=balance_tolerance,
                                    min_oos=min_oos)
    components: dict = {"E": _error_component(checks["relative_error"])}
    violations = metrics.get("consistency_violations", 0)
    if isinstance(violations, bool) or not isinstance(violations, int) or violations < 0:
        raise QualityError(f"consistency_violations must be a non-negative integer, got {violations!r}")
    if max_violations <= 0:
        raise QualityError("max_violations must be positive")
    components["C"] = _clip(1.0 - violations / float(max_violations))
    if metrics.get("stability_cv") is not None:
        components["S"] = _stability_component(
            _finite(metrics["stability_cv"], "stability_cv", minimum=0.0))
    if metrics.get("interval_coverage") is not None:
        coverage = _finite(metrics["interval_coverage"], "interval_coverage")
        if not 0.0 <= coverage <= 1.0:
            raise QualityError("interval_coverage must be in [0, 1]")
        components["R"] = _risk_component(coverage)
    used = tuple(key for key in COMPONENTS if key in components)
    if weights is None:
        raw = {key: DEFAULT_WEIGHTS[key] for key in used}
        subtotal = math.fsum(raw.values())
        if subtotal <= 0.0:
            raise QualityError("default weights are empty for the used components")
        chosen = {key: value / subtotal for key, value in raw.items()}
    else:
        if set(weights) != set(used):
            raise QualityError(f"weights must cover exactly the used components {list(used)}")
        chosen = {key: _finite(weights[key], f"weights[{key}]", minimum=0.0) for key in used}
        total = math.fsum(chosen.values())
        if abs(total - 1.0) > 1e-9:
            raise QualityError(f"weights must sum to 1, got {total}")
    q = math.fsum(components[key] * chosen[key] for key in used)
    q = _clip(q)
    inputs = {"error": checks["error"], "benchmark_error": checks["benchmark_error"],
              "relative_error": round(checks["relative_error"], 9),
              "consistency_violations": violations,
              "stability_cv": metrics.get("stability_cv"),
              "interval_coverage": metrics.get("interval_coverage"),
              "n_oos": checks["n_oos"]}
    return QualityResult(q=q, grade=grade_for(q), components=components, weights=chosen,
                         hard_constraints=checks, inputs=inputs)


def compare_versions(previous: Mapping, current: Mapping, *,
                     min_improvement: float = MIN_IMPROVEMENT) -> dict:
    """Compare two :meth:`QualityResult.as_dict` documents on Q (both must pass constraints)."""
    for label, document in (("previous", previous), ("current", current)):
        if not isinstance(document, Mapping) or "q" not in document:
            raise QualityError(f"{label} must be a quality result document with 'q'")
        constraints = document.get("hard_constraints") or {}
        if not constraints.get("pass"):
            raise QualityError(f"{label} quality result does not pass the hard constraints")
        _finite(document["q"], f"{label}.q")
    delta = float(current["q"]) - float(previous["q"])
    if delta >= float(min_improvement):
        decision = "IMPROVED"
    elif delta <= -float(min_improvement):
        decision = "DEGRADED"
    else:
        decision = "TIE"
    return {"decision": decision, "delta_q": round(delta, 6),
            "min_improvement": float(min_improvement),
            "previous_q": round(float(previous["q"]), 6),
            "current_q": round(float(current["q"]), 6),
            "previous_grade": previous.get("grade"), "current_grade": current.get("grade")}


__all__ = ["COMPONENTS", "DEFAULT_WEIGHTS", "GRADES", "MIN_IMPROVEMENT", "MIN_OOS", "QualityError",
           "QualityResult", "grade_for", "check_hard_constraints", "compare_versions",
           "evaluate_quality"]
