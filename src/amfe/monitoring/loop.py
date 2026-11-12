"""End-to-end monitoring loop: observations, contours, drift checks, decisions and the journal.

The loop is deliberately thin: each stage lives in its own module and the loop only routes data and
records what happened.  It never fits a model and it never bypasses the version journal, so every
automatic action is auditable and reversible.
"""
from __future__ import annotations

from .contours import ContourCoordinator, Observation
from .decision import RollbackController, SwitchRule
from .drift import DriftMonitor
from .versions import ModelVersion, VersionJournal, utc_now


class LoopError(RuntimeError):
    """The monitoring loop was called in an inconsistent state."""


class MonitoringLoop:
    def __init__(self, journal: VersionJournal | None = None, *, rule: SwitchRule | None = None,
                 coordinator: ContourCoordinator | None = None,
                 drift_monitor: DriftMonitor | None = None,
                 require_admissible_to_decide: bool = True):
        self.journal = journal or VersionJournal()
        self.rule = rule or SwitchRule()
        self.controller = RollbackController(self.journal, self.rule)
        self.coordinator = coordinator or ContourCoordinator(journal=self.journal)
        self.drift_monitor = drift_monitor
        self.require_admissible_to_decide = bool(require_admissible_to_decide)
        self.steps = 0
        self.last_report: dict | None = None

    def observe(self, observation: Observation, losses: dict | None = None,
                features: dict | None = None, errors=None) -> dict:
        """Feed one observation through the gate and contours; optional drift checks."""
        if isinstance(observation, dict):
            observation = Observation(**observation)
        contour_report = self.coordinator.step(observation, losses)
        drift_report = None
        if self.drift_monitor is not None and contour_report["admissible"]:
            checks = {}
            if features is not None and self.drift_monitor.reference_features is not None:
                checks["features"] = self.drift_monitor.check_features(features).to_dict()
            if errors is not None and self.drift_monitor.reference_errors is not None:
                checks["errors"] = self.drift_monitor.check_errors(errors).to_dict()
            drift_report = checks or None
        self.steps += 1
        self.last_report = {"step": self.steps, "admissible": contour_report["admissible"],
                            "contours": contour_report, "drift": drift_report,
                            "recorded_at": utc_now()}
        return self.last_report

    def register(self, version: ModelVersion) -> dict:
        entry, created = self.journal.ensure_version(version)
        return {"version_id": version.version_id, "created": created, "seq": entry["seq"]}

    def consider_candidate(self, candidate: ModelVersion, candidate_losses: dict,
                           active_losses: dict, *, target: str | None = None,
                           observations: int | None = None) -> dict:
        active_id = self.journal.active
        active = self.journal.version(active_id) if active_id is not None else None
        self.register(candidate)
        if active is not None:
            self.register(active)
        result = self.controller.consider(candidate, active, candidate_losses, active_losses,
                                          target=target, observations=observations)
        self.last_report = {"step": self.steps + 1, "action": result,
                            "recorded_at": utc_now()}
        return result

    def check_rollback(self, predecessor: ModelVersion, predecessor_losses: dict,
                       active_losses: dict, *, target: str | None = None,
                       observations: int | None = None) -> dict:
        result = self.controller.auto_rollback_check(
            predecessor, None, predecessor_losses, active_losses, target=target,
            observations=observations)
        self.last_report = {"step": self.steps + 1, "action": result,
                            "recorded_at": utc_now()}
        return result

    def snapshot(self) -> dict:
        return {"journal": self.journal.summary(),
                "contours": self.coordinator.summary(),
                "rule": {"alpha": self.rule.alpha,
                         "min_observations": self.rule.min_observations,
                         "horizon": self.rule.horizon},
                "steps": self.steps,
                "decisions": len(self.controller.decisions)}
