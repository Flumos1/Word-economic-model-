"""Fast and slow update contours of the controlled self-learning loop (docs/41, stage 8).

Two contours, one gate:

* **fast contour** - state estimates (latent level, nowcast corrections) updated on every admissible
  observation; it may move often but only carries a small, well-specified state;
* **slow contour** - parameters and model weights, updated only after enough admissible observations
  and only through an explicit updater; it never rewrites accounting identities or the frozen
  protocol;
* **observation gate** - the prohibition of docs/41 A04: nothing updates without admissible
  observations.  An observation is admissible when it is REAL_TIME (or another allowed data class)
  and passes the optional vintage-hash requirement; PSEUDO_REAL_TIME, CONTAMINATED and explicitly
  rejected observations are counted and skipped.

Synthetic implementations are deliberately simple and deterministic: a scalar Kalman update for the
fast contour and inverse-MSE performance weights for the slow contour.  The interfaces are the
contract; real estimators plug in later without changing the monitoring loop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .versions import canonical_json, sha256_text

ALLOWED_DATA_CLASSES = ("REAL_TIME",)
KNOWN_DATA_CLASSES = ("REAL_TIME", "PSEUDO_REAL_TIME", "CONTAMINATED")


class MonitoringError(RuntimeError):
    """A contour or observation-gate contract was violated."""


@dataclass
class Observation:
    """One admissible-or-not observation fed to the monitoring loop."""

    origin: str
    values: dict
    data_class: str = "REAL_TIME"
    vintage_hash: str | None = None
    admissible: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        return {"origin": self.origin, "values": {k: float(v) for k, v in self.values.items()},
                "data_class": self.data_class, "vintage_hash": self.vintage_hash,
                "admissible": self.admissible, "reason": self.reason}


class ObservationGate:
    """Admissibility filter: no fast or slow update without admissible observations."""

    def __init__(self, allowed_data_classes=ALLOWED_DATA_CLASSES,
                 require_vintage_hash: bool = False):
        self.allowed = tuple(allowed_data_classes)
        self.require_vintage_hash = bool(require_vintage_hash)
        self.accepted = 0
        self.rejected = 0
        self.rejections: list[dict] = []

    def check(self, observation: Observation) -> tuple[bool, str]:
        ok, reason = self._check(observation)
        if ok:
            self.accepted += 1
        else:
            self.rejected += 1
            self.rejections.append({"origin": observation.origin, "reason": reason})
        return ok, reason

    def _check(self, observation: Observation) -> tuple[bool, str]:
        if not isinstance(observation, Observation):
            raise MonitoringError("observation must be an Observation")
        if not observation.admissible:
            return False, observation.reason or "observation marked inadmissible"
        if observation.data_class not in KNOWN_DATA_CLASSES:
            return False, f"unknown data class {observation.data_class!r}"
        if observation.data_class not in self.allowed:
            return False, f"data class {observation.data_class!r} is not admissible for updating"
        if not observation.values:
            return False, "observation has no values"
        for name, value in observation.values.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False, f"value {name!r} is not a number"
            if not np.isfinite(number):
                return False, f"value {name!r} is not finite"
        if self.require_vintage_hash and not observation.vintage_hash:
            return False, "vintage hash is required for updating"
        return True, "admissible"

    def summary(self) -> dict:
        return {"accepted": self.accepted, "rejected": self.rejected,
                "allowed_data_classes": list(self.allowed),
                "require_vintage_hash": self.require_vintage_hash}


@dataclass
class ContourUpdate:
    """Outcome of one contour update."""

    updated: bool
    observations: int
    reason: str
    weights: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    fingerprint: str | None = None
    rejected: int = 0

    def to_dict(self) -> dict:
        return {"updated": self.updated, "observations": self.observations,
                "reason": self.reason, "weights": dict(self.weights),
                "state": dict(self.state), "fingerprint": self.fingerprint,
                "rejected": self.rejected}


class FastContour:
    """Scalar Kalman filter per state variable; updates only on admissible observations."""

    def __init__(self, initial_state: dict | None = None, *, process_variance: float = 0.05,
                 observation_variance: float = 1.0, gate: ObservationGate | None = None):
        if process_variance <= 0.0 or observation_variance <= 0.0:
            raise MonitoringError("variances must be positive")
        self.process_variance = float(process_variance)
        self.observation_variance = float(observation_variance)
        self.gate = gate or ObservationGate()
        self.state: dict[str, float] = {key: float(value)
                                        for key, value in (initial_state or {}).items()}
        self.variance: dict[str, float] = {key: 1.0 for key in self.state}
        self.updates = 0

    def update(self, observations) -> ContourUpdate:
        if isinstance(observations, Observation):
            observations = [observations]
        accepted = rejected = 0
        for observation in observations:
            ok, reason = self.gate.check(observation)
            if not ok:
                rejected += 1
                continue
            accepted += 1
            for name, value in observation.values.items():
                number = float(value)
                prior = self.state.get(name, number)
                prior_variance = self.variance.get(name, 1.0) + self.process_variance
                gain = prior_variance / (prior_variance + self.observation_variance)
                self.state[name] = prior + gain * (number - prior)
                self.variance[name] = (1.0 - gain) * prior_variance
            self.updates += 1
        if accepted == 0:
            return ContourUpdate(updated=False, observations=0,
                                 reason="no admissible observations", rejected=rejected,
                                 state=dict(self.state))
        fingerprint = sha256_text(canonical_json(
            {"state": sorted(self.state.items()), "updates": self.updates}))
        return ContourUpdate(updated=True, observations=accepted,
                             reason="fast contour updated", state=dict(self.state),
                             fingerprint=fingerprint, rejected=rejected)

    def snapshot(self) -> dict:
        return {"state": {key: float(value) for key, value in self.state.items()},
                "variance": {key: float(value) for key, value in self.variance.items()},
                "updates": self.updates,
                "gate": self.gate.summary()}


class WeightUpdater(ABC):
    """Interface: map per-model loss histories to a normalised weight vector."""

    name = "abstract"

    @abstractmethod
    def update(self, loss_history: dict) -> dict:
        raise NotImplementedError


class InverseMSEWeights(WeightUpdater):
    """Weights proportional to the inverse mean squared / mean loss over the available window."""

    name = "INVERSE_MSE"

    def __init__(self, floor: float = 1e-9):
        if floor <= 0.0:
            raise MonitoringError("floor must be positive")
        self.floor = float(floor)

    def update(self, loss_history: dict) -> dict:
        if not loss_history:
            raise MonitoringError("loss history is empty")
        inverse = {}
        for model_id, values in loss_history.items():
            series = np.asarray(values, dtype=float)
            if series.ndim != 1 or series.size == 0:
                raise MonitoringError(f"{model_id}: loss history must be non-empty and 1-D")
            if not np.all(np.isfinite(series)) or np.any(series < 0.0):
                raise MonitoringError(f"{model_id}: losses must be finite and non-negative")
            mean_loss = float(np.mean(series))
            inverse[model_id] = 1.0 / max(mean_loss, self.floor)
        total = sum(inverse.values())
        if total <= 0.0:
            raise MonitoringError("weights are degenerate")
        return {model_id: value / total for model_id, value in sorted(inverse.items())}


class SlowContour:
    """Parameters/weights contour: updates only with enough new admissible observations."""

    def __init__(self, updater: WeightUpdater | None = None, *, min_observations: int = 12):
        if min_observations < 1:
            raise MonitoringError("min_observations must be positive")
        self.updater = updater or InverseMSEWeights()
        self.min_observations = int(min_observations)
        self.weights: dict[str, float] = {}
        self.updates = 0
        self._fingerprint: str | None = None
        self.last_reason = "not updated yet"

    def update(self, loss_history: dict | None, observations: int | None = None) -> ContourUpdate:
        if not loss_history:
            self.last_reason = "no admissible observations; slow contour not updated"
            return ContourUpdate(updated=False, observations=0, reason=self.last_reason,
                                 weights=dict(self.weights))
        lengths = {np.asarray(values).size for values in loss_history.values()}
        if len(lengths) > 1:
            raise MonitoringError(f"loss histories have different lengths: {sorted(lengths)}")
        available = int(min(lengths))
        if observations is None:
            observations = available
        observations = int(observations)
        if observations <= 0 or available <= 0:
            self.last_reason = "no admissible observations; slow contour not updated"
            return ContourUpdate(updated=False, observations=observations, reason=self.last_reason,
                                 weights=dict(self.weights))
        if observations < self.min_observations or available < self.min_observations:
            self.last_reason = (f"{available} observations < minimum {self.min_observations}; "
                                f"parameters are frozen")
            return ContourUpdate(updated=False, observations=observations, reason=self.last_reason,
                                 weights=dict(self.weights))
        fingerprint = sha256_text(canonical_json(
            {model_id: [float(v) for v in np.asarray(values, dtype=float).tolist()]
             for model_id, values in sorted(loss_history.items())}))
        if fingerprint == self._fingerprint:
            self.last_reason = "no new observations since the last parameter update"
            return ContourUpdate(updated=False, observations=observations,
                                 reason=self.last_reason, weights=dict(self.weights),
                                 fingerprint=fingerprint)
        weights = self.updater.update(loss_history)
        self.weights = weights
        self.updates += 1
        self._fingerprint = fingerprint
        self.last_reason = f"parameters updated by {self.updater.name}"
        return ContourUpdate(updated=True, observations=observations, reason=self.last_reason,
                             weights=dict(weights), fingerprint=fingerprint)

    def snapshot(self) -> dict:
        return {"weights": dict(self.weights), "updates": self.updates,
                "min_observations": self.min_observations, "updater": self.updater.name,
                "last_reason": self.last_reason}


class ContourCoordinator:
    """Routes observations through the gate, the fast contour and (periodically) the slow contour."""

    def __init__(self, fast: FastContour | None = None, slow: SlowContour | None = None,
                 *, gate: ObservationGate | None = None, slow_every: int = 5,
                 journal=None):
        if slow_every < 1:
            raise MonitoringError("slow_every must be positive")
        self.gate = gate or ObservationGate()
        self.fast = fast or FastContour(gate=self.gate)
        self.slow = slow or SlowContour()
        self.slow_every = int(slow_every)
        self.journal = journal
        self.admissible = 0
        self.rejected = 0
        self.loss_history: dict[str, list[float]] = {}
        self.last_slow_update: ContourUpdate | None = None

    def step(self, observation: Observation, losses: dict | None = None) -> dict:
        ok, reason = self.gate.check(observation)
        if not ok:
            self.rejected += 1
            return {"admissible": False, "reason": reason, "fast": None, "slow": None}
        self.admissible += 1
        fast_update = self.fast.update([observation])
        for model_id, value in (losses or {}).items():
            number = float(value)
            if not np.isfinite(number) or number < 0.0:
                raise MonitoringError(f"{model_id}: loss must be finite and non-negative")
            self.loss_history.setdefault(model_id, []).append(number)
        slow_update = None
        if (losses and self.slow
                and self.admissible % self.slow_every == 0
                and all(len(values) >= self.slow.min_observations
                        for values in self.loss_history.values())):
            slow_update = self.slow.update(self.loss_history, observations=self.admissible)
            self.last_slow_update = slow_update
            if slow_update.updated and self.journal is not None:
                self.journal.record_update("SLOW_CONTOUR", {
                    "weights": slow_update.weights, "observations": slow_update.observations,
                    "updater": self.slow.updater.name, "fingerprint": slow_update.fingerprint})
        return {"admissible": True, "reason": reason, "fast": fast_update.to_dict(),
                "slow": slow_update.to_dict() if slow_update else None,
                "admissible_count": self.admissible}

    def summary(self) -> dict:
        return {"gate": self.gate.summary(), "admissible": self.admissible,
                "rejected": self.rejected, "fast": self.fast.snapshot(),
                "slow": self.slow.snapshot(), "models": sorted(self.loss_history)}
