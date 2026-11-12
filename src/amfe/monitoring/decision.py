"""Monitoring decision rule: "candidate version against the active version" with automatic rollback.

The rule mirrors the frozen admission logic of protocol v4 so that production monitoring cannot be
more permissive than the admission harness:

  * C1 logic - the relative loss (candidate / active) must be <= the NF-SIM threshold for each
    applicable loss.  The forward-registry window is not part of the NF-SIM study, so the rule uses
    the **stricter** of the W1 and W2 thresholds for the pair (the minimum).  This is a documented
    conservative choice; an explicit override can be passed for research comparisons.
  * C2 logic - Giacomini-White with the constant as the only instrument (one-sided Diebold-Mariano),
    HAC with h-1 lags, Harvey-Leybourne-Newbold correction, p < 0.10, for each applicable loss.
  * A switch needs both conditions for every applicable loss and at least ``min_observations``
    admissible observations (the protocol's forward-registry minimum is 12).
  * If the candidate is *significantly worse* (opposite one-sided test p < alpha and relative loss
    above 1), the rule says REJECT; if it is not distinguishable, HOLD.  Neither changes the active
    version.

The decision function is pure: it never mutates the journal.  :class:`RollbackController` applies it
to the version journal, records the evidence and performs the actual switch or rollback.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .versions import ModelVersion, VersionError, VersionJournal, utc_now

ROOT = Path(__file__).resolve().parents[3]
THRESHOLD_TABLE = ROOT / "protocols" / "nf_sim_thresholds.json"

ALPHA = 0.10
MIN_OBSERVATIONS = 12
TARGET_LOSSES = {
    "T1_CPI_YOY": ("RMSE", "CRPS"),
    "T2_GDP_GROWTH": ("RMSE", "CRPS"),
    "T3_RECESSION": ("Brier", "log_score"),
}
# Losses without a frozen NF-SIM entry keep an explicit default threshold; monitoring must not
# invent a threshold silently, so the caller can also pass ``threshold_override``.
DEFAULT_THRESHOLDS = {"MAE": 0.97, "RMSE": 0.97, "CRPS": 0.97, "Brier": 0.97, "log_score": 0.97}
DECISION_ACTIONS = ("PROMOTE", "HOLD", "REJECT", "INSUFFICIENT_DATA", "NOOP", "ROLLBACK")


class DecisionError(RuntimeError):
    """Invalid monitoring decision input."""


# ------------------------------------------------------------------ frozen C1/C2 formula mirror
def relative_loss(candidate_loss: float, active_loss: float) -> float:
    if active_loss <= 0.0:
        raise DecisionError("active version loss must be positive")
    return candidate_loss / active_loss


def aggregate_loss(loss: str, series) -> float:
    values = np.asarray(series, dtype=float)
    if values.ndim != 1 or values.size == 0:
        raise DecisionError(f"{loss}: loss series must be one-dimensional and non-empty")
    if not np.all(np.isfinite(values)) or np.any(values < 0.0):
        raise DecisionError(f"{loss}: loss series must be finite and non-negative")
    if loss in ("RMSE",):
        return float(np.sqrt(np.mean(values)))
    return float(np.mean(values))


def _as_1d(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise DecisionError(f"{name} must be a non-empty one-dimensional array")
    if not np.all(np.isfinite(array)):
        raise DecisionError(f"{name} contains non-finite values")
    return array


def newey_west_variance(series, lags: int) -> float:
    """Bartlett-kernel HAC estimate of Var(sqrt(n) * mean(series)); mirrors tools/evaluate.py."""
    x = _as_1d(series, "series") - float(np.mean(series))
    n = x.size
    lags = int(max(0, min(lags, n - 1)))
    total = float(np.dot(x, x)) / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = float(np.dot(x[lag:], x[:-lag])) / n
        total += 2.0 * weight * gamma
    return max(total, 0.0)


def hln_factor(n: int, horizon: int) -> float:
    h = max(1, int(horizon))
    if h == 1:
        return 1.0
    return math.sqrt(max(1e-12, (n + 1 - 2 * h + h * (h - 1) / n) / n))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def one_sided_dm(differential, horizon: int = 1, alternative: str = "less") -> dict:
    """Mirror of the frozen harness DM/GW test (HAC h-1, HLN, normal approximation)."""
    d = _as_1d(differential, "differential")
    n = d.size
    mean = float(np.mean(d))
    variance = newey_west_variance(d, max(0, int(horizon) - 1))
    hln = hln_factor(n, horizon)
    if variance <= 0.0:
        if abs(mean) <= 1e-15:
            statistic, p_value = 0.0, 1.0 if alternative == "two-sided" else 0.5
        else:
            statistic = math.copysign(math.inf, mean)
            p_value = 1.0 if (mean > 0) == (alternative == "less") else 0.0
        return {"mean_differential": mean, "statistic": statistic, "p_value": p_value,
                "hac_lags": max(0, int(horizon) - 1), "hln_factor": hln, "n": n,
                "alternative": alternative, "degenerate": True}
    statistic = mean / math.sqrt(variance / n) * hln
    if alternative == "less":
        p_value = _normal_cdf(statistic)
    elif alternative == "greater":
        p_value = 1.0 - _normal_cdf(statistic)
    elif alternative == "two-sided":
        p_value = 2.0 * min(_normal_cdf(statistic), 1.0 - _normal_cdf(statistic))
    else:
        raise DecisionError(f"unknown alternative {alternative!r}")
    return {"mean_differential": mean, "statistic": statistic,
            "p_value": max(0.0, min(1.0, p_value)), "hac_lags": max(0, int(horizon) - 1),
            "hln_factor": hln, "n": n, "alternative": alternative, "degenerate": False}


# ------------------------------------------------------------------------------- thresholds
def load_threshold_table(path: Path | str = THRESHOLD_TABLE) -> dict:
    """Load the frozen NF-SIM table and re-derive every threshold from its q05 components."""
    source = Path(path)
    if not source.exists():
        raise DecisionError(f"NF-SIM threshold table not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    table = {}
    for entry in payload["entries"]:
        derived = round(min(0.97, entry["q05_single"], entry["q05_combination"]), 4)
        if derived != entry["threshold"]:
            raise DecisionError(f"threshold mismatch for {entry['target']}/{entry['horizon']}/"
                                f"{entry['loss']}/{entry['window']}")
        table[(entry["target"], int(entry["horizon"]), entry["loss"], entry["window"])] = (
            entry["threshold"])
    return table


def strict_threshold(table: dict, target: str, horizon: int, loss: str) -> float:
    """The stricter of the frozen W1/W2 thresholds for (target, horizon, loss)."""
    windows = [value for (t, h, l, w), value in table.items()
               if t == target and h == int(horizon) and l == loss and w in ("W1", "W2")]
    if not windows:
        raise DecisionError(f"no NF-SIM threshold for {target}/{horizon}/{loss}")
    return float(min(windows))


@dataclass
class SwitchRule:
    """C1/C2-logic rule for switching between the active and a candidate version."""

    alpha: float = ALPHA
    min_observations: int = MIN_OBSERVATIONS
    horizon: int = 1
    threshold_override: dict = field(default_factory=dict)

    def threshold(self, loss: str, target: str | None = None) -> float:
        if loss in self.threshold_override:
            return float(self.threshold_override[loss])
        if target is not None:
            try:
                return strict_threshold(load_threshold_table(), target, self.horizon, loss)
            except (DecisionError, KeyError):
                pass
        if loss not in DEFAULT_THRESHOLDS:
            raise DecisionError(f"no threshold for loss {loss!r}; pass threshold_override")
        return float(DEFAULT_THRESHOLDS[loss])

    def evaluate(self, candidate_losses: dict, active_losses: dict, *,
                 target: str | None = None, observations: int | None = None) -> dict:
        """Pure decision: candidate loss series against the active version loss series."""
        if not isinstance(candidate_losses, dict) or not isinstance(active_losses, dict):
            raise DecisionError("losses must be dictionaries {loss_name: per-observation series}")
        if set(candidate_losses) != set(active_losses):
            raise DecisionError("candidate and active losses must use the same loss names")
        if not candidate_losses:
            raise DecisionError("no loss series supplied")
        lengths = {np.asarray(values).size for values in candidate_losses.values()}
        lengths |= {np.asarray(values).size for values in active_losses.values()}
        if len(lengths) > 1:
            raise DecisionError(f"loss series have different lengths: {sorted(lengths)}")
        available = int(min(lengths))
        if observations is None:
            observations = available
        observations = int(observations)
        rule_meta = {"alpha": self.alpha, "min_observations": self.min_observations,
                     "horizon": self.horizon, "target": target,
                     "threshold_rule": "C1: relative loss <= NF-SIM threshold (stricter of W1/W2)",
                     "test_rule": "C2: one-sided DM/GW p < alpha, HAC h-1, HLN"}
        if observations <= 0 or available <= 0:
            return {"action": "INSUFFICIENT_DATA", "passed": False, "observations": observations,
                    "available": available, "reasons": ["no admissible observations"],
                    "per_loss": {}, "rule": rule_meta}
        if observations < self.min_observations or available < self.min_observations:
            return {"action": "INSUFFICIENT_DATA", "passed": False, "observations": observations,
                    "available": available,
                    "reasons": [f"{available} observations < minimum {self.min_observations}; "
                                f"no update without admissible observations"],
                    "per_loss": {}, "rule": rule_meta}
        per_loss = {}
        passed = True
        worse = False
        for loss in sorted(candidate_losses):
            candidate_series = _as_1d(candidate_losses[loss], f"candidate {loss}")
            active_series = _as_1d(active_losses[loss], f"active {loss}")
            candidate_aggregate = aggregate_loss(loss, candidate_series)
            active_aggregate = aggregate_loss(loss, active_series)
            threshold = self.threshold(loss, target)
            relative = relative_loss(candidate_aggregate, active_aggregate)
            differential = candidate_series - active_series
            c2 = one_sided_dm(differential, self.horizon, "less")
            opposite = one_sided_dm(differential, self.horizon, "greater")
            c1_met = relative <= threshold
            c2_met = c2["p_value"] < self.alpha
            loss_worse = bool(opposite["p_value"] < self.alpha and relative > 1.0)
            passed = passed and c1_met and c2_met
            worse = worse or loss_worse
            per_loss[loss] = {
                "candidate_loss": candidate_aggregate, "active_loss": active_aggregate,
                "relative_loss": relative, "threshold": threshold, "c1_met": bool(c1_met),
                "p_value": c2["p_value"], "c2_met": bool(c2_met),
                "p_opposite": opposite["p_value"], "candidate_worse": loss_worse,
                "hac_lags": c2["hac_lags"], "hln_factor": c2["hln_factor"],
            }
        if passed:
            action = "PROMOTE"
            reasons = ["C1 relative loss and C2 one-sided test pass for every applicable loss"]
        elif worse:
            action = "REJECT"
            reasons = ["the candidate is significantly worse than the active version"]
        else:
            action = "HOLD"
            reasons = ["the candidate is not significantly better; the active version is kept"]
        return {"action": action, "passed": bool(passed), "observations": observations,
                "available": available, "reasons": reasons, "per_loss": per_loss,
                "rule": rule_meta, "evaluated_at": utc_now()}


# ----------------------------------------------------------------------------- rollback control
class RollbackController:
    """Applies the decision rule to the version journal and performs switches and rollbacks.

    ``consider`` promotes a candidate version when the rule says PROMOTE; it never promotes on
    insufficient observations.  ``auto_rollback_check`` compares the active version with its
    predecessor and returns to the predecessor when the predecessor is demonstrably better (the
    automatic rollback of stage 8).  Both methods are idempotent: repeating the same action against
    the same state returns NOOP and writes no new journal entry.
    """

    def __init__(self, journal: VersionJournal, rule: SwitchRule | None = None,
                 actor: str = "monitoring"):
        self.journal = journal
        self.rule = rule or SwitchRule()
        self.actor = actor
        self.decisions: list[dict] = []

    def _missing(self, version_id: str) -> bool:
        return version_id not in self.journal.versions

    def consider(self, candidate: ModelVersion, active: ModelVersion | None,
                 candidate_losses: dict, active_losses: dict, *, target: str | None = None,
                 observations: int | None = None) -> dict:
        current = active.version_id if active is not None else None
        if current is not None and candidate.version_id == current:
            return {"action": "NOOP", "reason": "candidate is already the active version",
                    "active": current}
        if self._missing(candidate.version_id):
            self.journal.register(candidate)
        if current is not None and self._missing(current):
            raise VersionError(f"active version {current} is not in the journal")
        decision = self.rule.evaluate(candidate_losses, active_losses, target=target,
                                      observations=observations)
        decision["candidate_version"] = candidate.version_id
        decision["active_version"] = current
        decision["kind"] = "CANDIDATE_VS_ACTIVE"
        self.decisions.append(decision)
        if decision["action"] == "PROMOTE":
            reason = ("promoted by the stage-8 monitoring rule: C1 relative loss and C2 "
                      "one-sided test pass for every loss")
            try:
                entry = self.journal.activate(candidate.version_id, reason,
                                              observations=decision["observations"], actor=self.actor)
            except VersionError:
                return {"action": "NOOP", "reason": "activation race: version already active",
                        "active": self.journal.active, "decision": decision}
            return {"action": "PROMOTE", "active": self.journal.active, "entry": entry,
                    "decision": decision}
        self.journal.record_decision(decision, actor=self.actor)
        return {"action": decision["action"], "active": current, "decision": decision}

    def auto_rollback_check(self, previous: ModelVersion, current: ModelVersion | None = None,
                            previous_losses: dict | None = None, current_losses: dict | None = None,
                            *, target: str | None = None,
                            observations: int | None = None) -> dict:
        """Compare the active version against its predecessor; roll back when the predecessor wins."""
        if current is None:
            if self.journal.active is None:
                return {"action": "NOOP", "reason": "no active version"}
            try:
                current = self.journal.version(self.journal.active)
            except VersionError as error:
                return {"action": "NOOP", "reason": str(error)}
        if previous.version_id == current.version_id:
            return {"action": "NOOP", "reason": "predecessor equals the active version"}
        # no new admissible observations -> no update and no rollback
        if observations is not None and int(observations) < self.rule.min_observations:
            return {"action": "INSUFFICIENT_DATA",
                    "reason": f"{int(observations)} observations < minimum "
                              f"{self.rule.min_observations}"}
        if previous_losses is None or current_losses is None:
            raise DecisionError("previous_losses and current_losses are required")
        # evaluate the predecessor as the "candidate" against the active version
        decision = self.rule.evaluate(previous_losses, current_losses, target=target,
                                      observations=observations)
        decision["kind"] = "PREDECESSOR_VS_ACTIVE"
        decision["candidate_version"] = previous.version_id
        decision["active_version"] = current.version_id
        self.decisions.append(decision)
        if decision["action"] == "PROMOTE":
            try:
                entry = self.journal.rollback(
                    "automatic rollback: the predecessor is significantly better than the active "
                    "version", observations=decision["observations"],
                    target_version_id=previous.version_id, actor=self.actor)
            except VersionError as error:  # pragma: no cover - state guard
                return {"action": "NOOP", "reason": str(error)}
            return {"action": "ROLLBACK", "active": self.journal.active, "entry": entry,
                    "decision": decision}
        self.journal.record_decision(decision, actor=self.actor)
        return {"action": "KEEP", "active": current.version_id, "decision": decision}
