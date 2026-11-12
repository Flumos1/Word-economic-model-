"""Data-drift and concept-drift detectors.

Two different questions (docs/41 G4):

* **data drift** - did the input distribution move?  Two-sample Kolmogorov-Smirnov per feature,
  Population Stability Index (PSI) on quantile bins and a Welch mean-shift test.  A feature is
  reported as drifted when at least two methods agree (or one extreme method fires); this keeps
  single-test false alarms down on short windows.
* **concept drift** - did the input-to-target relationship change?  The observable proxy is the
  forecast error: its location, scale and shape are compared between a reference window and a recent
  window, and a relationship test compares the correlation of the input with the target in both
  windows.  A persistent error shift that the data-drift detector does not explain is the signature
  of concept drift.

All functions are pure numpy and return explicit reports (no decisions); the decision layer turns a
drift report into a monitoring action.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


class DriftError(RuntimeError):
    """Invalid drift-detector input."""


def _as_1d(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise DriftError(f"{name} must be one-dimensional")
    if array.size < 2:
        raise DriftError(f"{name} needs at least two observations")
    if not np.all(np.isfinite(array)):
        raise DriftError(f"{name} contains non-finite values")
    return array


# ------------------------------------------------------------------------------------ KS test
def ks_statistic(reference, current) -> float:
    """Two-sample Kolmogorov-Smirnov D statistic."""
    a = np.sort(_as_1d(reference, "reference"))
    b = np.sort(_as_1d(current, "current"))
    grid = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, grid, side="right") / a.size
    cdf_b = np.searchsorted(b, grid, side="right") / b.size
    return float(np.max(np.abs(cdf_a - cdf_b)))


def ks_p_value(statistic: float, n1: int, n2: int) -> float:
    """Asymptotic Kolmogorov p-value with the small-sample correction of Numerical Recipes."""
    if n1 <= 0 or n2 <= 0:
        raise DriftError("sample sizes must be positive")
    effective = math.sqrt(n1 * n2 / (n1 + n2))
    lam = (effective + 0.12 + 0.11 / effective) * float(statistic)
    if lam <= 0.0:
        return 1.0
    total = 0.0
    for k in range(1, 101):
        term = 2.0 * (-1) ** (k - 1) * math.exp(-2.0 * k * k * lam * lam)
        total += term
        if abs(term) < 1e-12:
            break
    return float(min(1.0, max(0.0, total)))


def ks_test(reference, current, alpha: float = 0.05) -> dict:
    statistic = ks_statistic(reference, current)
    p_value = ks_p_value(statistic, np.size(reference), np.size(current))
    return {"method": "KS", "statistic": statistic, "p_value": p_value,
            "drift": bool(p_value < alpha)}


# ------------------------------------------------------------------------------------ PSI
def psi(reference, current, bins: int = 10, epsilon: float = 1e-6) -> dict:
    """Population Stability Index on reference quantile bins (>= 0.2 = material shift)."""
    ref = _as_1d(reference, "reference")
    cur = _as_1d(current, "current")
    if bins < 2:
        raise DriftError("bins must be at least two")
    edges = np.quantile(ref, np.linspace(0.0, 1.0, bins + 1))
    edges = np.unique(edges)
    if edges.size < 3:  # degenerate reference: fall back to equal-width bins
        lower, upper = float(ref.min()), float(ref.max())
        if upper <= lower:
            raise DriftError("reference is constant; PSI is undefined")
        edges = np.linspace(lower, upper, bins + 1)
    ref_counts = np.histogram(ref, bins=edges)[0] / ref.size
    cur_counts = np.histogram(cur, bins=edges)[0] / cur.size
    ref_share = np.clip(ref_counts, epsilon, None)
    cur_share = np.clip(cur_counts, epsilon, None)
    value = float(np.sum((cur_share - ref_share) * np.log(cur_share / ref_share)))
    return {"method": "PSI", "psi": value, "drift": bool(value >= 0.2), "bins": int(bins),
            "edges": [float(edge) for edge in edges]}


# ------------------------------------------------------------------------------- mean shift
def mean_shift_test(reference, current, alpha: float = 0.05) -> dict:
    """Welch mean-shift test with normal approximation (two-sided)."""
    a = _as_1d(reference, "reference")
    b = _as_1d(current, "current")
    mean_a, mean_b = float(np.mean(a)), float(np.mean(b))
    var_a = float(np.var(a, ddof=1)) / a.size
    var_b = float(np.var(b, ddof=1)) / b.size
    standard_error = math.sqrt(var_a + var_b)
    if standard_error == 0.0:
        z_score = 0.0 if mean_a == mean_b else math.copysign(math.inf, mean_b - mean_a)
    else:
        z_score = (mean_b - mean_a) / standard_error
    p_value = math.erfc(abs(z_score) / math.sqrt(2.0)) if math.isfinite(z_score) else 0.0
    return {"method": "MEAN_SHIFT", "reference_mean": mean_a, "current_mean": mean_b,
            "difference": mean_b - mean_a, "z": z_score, "p_value": p_value,
            "drift": bool(p_value < alpha)}


def variance_ratio(reference, current, threshold: float = 2.0) -> dict:
    a = _as_1d(reference, "reference")
    b = _as_1d(current, "current")
    var_a = float(np.var(a, ddof=1))
    var_b = float(np.var(b, ddof=1))
    if var_a <= 0.0:
        raise DriftError("reference variance is zero")
    ratio = var_b / var_a
    return {"method": "VARIANCE_RATIO", "ratio": ratio,
            "drift": bool(ratio > threshold or ratio < 1.0 / threshold)}


# ---------------------------------------------------------------------------------- reports
@dataclass
class FeatureDriftReport:
    feature: str
    drift: bool
    votes: int
    methods: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"feature": self.feature, "drift": self.drift, "votes": self.votes,
                "methods": {name: {k: v for k, v in result.items() if k != "edges"}
                            for name, result in self.methods.items()}}


@dataclass
class DriftReport:
    drift_detected: bool
    features: dict[str, FeatureDriftReport] = field(default_factory=dict)
    methods_used: tuple = ()
    min_votes: int = 2

    def to_dict(self) -> dict:
        return {"drift_detected": self.drift_detected, "methods_used": list(self.methods_used),
                "min_votes": self.min_votes,
                "features": {name: report.to_dict()
                             for name, report in self.features.items()}}


def detect_data_drift(reference: dict, current: dict, *, alpha: float = 0.05,
                      min_votes: int = 2) -> DriftReport:
    """Compare feature distributions; features must be dictionaries with the same keys."""
    if not reference or not current:
        raise DriftError("reference and current feature dictionaries must be non-empty")
    if set(reference) != set(current):
        raise DriftError(f"feature sets differ: {sorted(set(reference) ^ set(current))}")
    features = {}
    for name in sorted(reference):
        methods = {
            "ks": ks_test(reference[name], current[name], alpha=alpha),
            "psi": psi(reference[name], current[name]),
            "mean": mean_shift_test(reference[name], current[name], alpha=alpha),
        }
        votes = sum(1 for result in methods.values() if result["drift"])
        features[name] = FeatureDriftReport(feature=name, drift=votes >= min_votes,
                                            votes=votes, methods=methods)
    return DriftReport(drift_detected=any(report.drift for report in features.values()),
                       features=features, methods_used=("KS", "PSI", "MEAN_SHIFT"),
                       min_votes=min_votes)


@dataclass
class ConceptDriftReport:
    drift_detected: bool
    error_drift: bool
    relation_drift: bool
    methods: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"drift_detected": self.drift_detected, "error_drift": self.error_drift,
                "relation_drift": self.relation_drift,
                "methods": {name: {k: v for k, v in result.items() if k != "edges"}
                            for name, result in self.methods.items()}}


def detect_concept_drift(reference_errors, recent_errors, *, alpha: float = 0.05) -> ConceptDriftReport:
    """Concept drift from the forecast-error distribution (location, scale, shape)."""
    methods = {
        "ks": ks_test(reference_errors, recent_errors, alpha=alpha),
        "mean": mean_shift_test(reference_errors, recent_errors, alpha=alpha),
        "variance": variance_ratio(reference_errors, recent_errors),
    }
    error_drift = sum(1 for result in methods.values() if result["drift"]) >= 2
    return ConceptDriftReport(drift_detected=bool(error_drift), error_drift=bool(error_drift),
                              relation_drift=False, methods=methods)


def relationship_drift(reference_inputs, reference_targets, recent_inputs, recent_targets,
                       *, alpha: float = 0.05, delta_threshold: float = 0.3) -> ConceptDriftReport:
    """Concept drift from a change of the input/target correlation."""
    a = _as_1d(reference_inputs, "reference_inputs")
    b = _as_1d(reference_targets, "reference_targets")
    c = _as_1d(recent_inputs, "recent_inputs")
    d = _as_1d(recent_targets, "recent_targets")
    if a.size != b.size or c.size != d.size:
        raise DriftError("inputs and targets must have equal length inside each window")
    rho_reference = float(np.corrcoef(a, b)[0, 1])
    rho_recent = float(np.corrcoef(c, d)[0, 1])
    delta = abs(rho_recent - rho_reference)
    # Fisher z difference test
    def fisher(rho):
        clipped = float(np.clip(rho, -0.999999, 0.999999))
        return math.atanh(clipped)

    def standard_error(n):
        return math.sqrt(1.0 / max(3, n - 3))

    z_score = ((fisher(rho_recent) - fisher(rho_reference))
               / math.sqrt(standard_error(a.size) ** 2 + standard_error(c.size) ** 2))
    p_value = math.erfc(abs(z_score) / math.sqrt(2.0))
    drift = (p_value < alpha) and (delta >= delta_threshold)
    methods = {"correlation": {"method": "FISHER_Z", "rho_reference": rho_reference,
                               "rho_recent": rho_recent, "delta": delta, "z": z_score,
                               "p_value": p_value, "drift": drift,
                               "delta_threshold": delta_threshold}}
    return ConceptDriftReport(drift_detected=bool(drift), error_drift=False,
                              relation_drift=bool(drift), methods=methods)


class DriftMonitor:
    """Stateful facade: stores the reference windows and checks new data against them."""

    def __init__(self, reference_features: dict | None = None, reference_errors=None,
                 *, alpha: float = 0.05, min_votes: int = 2):
        self.reference_features = ({key: np.asarray(value, dtype=float)
                                    for key, value in reference_features.items()}
                                   if reference_features else None)
        self.reference_errors = (np.asarray(reference_errors, dtype=float)
                                 if reference_errors is not None else None)
        self.alpha = alpha
        self.min_votes = min_votes
        self.reports: list[DriftReport | ConceptDriftReport] = []

    def check_features(self, features: dict) -> DriftReport:
        if self.reference_features is None:
            raise DriftError("no reference features stored")
        report = detect_data_drift(self.reference_features, features, alpha=self.alpha,
                                   min_votes=self.min_votes)
        self.reports.append(report)
        return report

    def check_errors(self, errors) -> ConceptDriftReport:
        if self.reference_errors is None:
            raise DriftError("no reference errors stored")
        report = detect_concept_drift(self.reference_errors, errors, alpha=self.alpha)
        self.reports.append(report)
        return report

    def shift_reference(self, features: dict | None = None, errors=None) -> None:
        """After a controlled update the reference window is re-baselined explicitly."""
        if features is not None:
            self.reference_features = {key: np.asarray(value, dtype=float)
                                       for key, value in features.items()}
        if errors is not None:
            self.reference_errors = np.asarray(errors, dtype=float)
