"""Geopolitical risk as an EXOGENOUS scenario, never as a causal estimate.

The mechanism is deliberately minimal: a risk index in [0, 1] follows a deterministic (optionally
persistent or event-driven) path, and a set of *assumed* loadings converts it into macro "shocks"
(GDP drag, inflation push, uncertainty premium).  The loadings are assumptions, not estimated causal
effects; the object is classified as SCENARIO, cannot gate admission and cannot claim causality.

This keeps the docs/38 contract: geopolitics is a real block (16) of the 17 mechanisms, but the
prototype does not pretend to identify its transmission.  An identified version would need an
exogenous event instrument and out-of-sample validation - future work.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_SCENARIO, Mechanism

GEOPOLITICAL_RISK_MECHANISM = Mechanism(
    mechanism_id="DYN-GEOPOLITICS-SCENARIO",
    title_ru="Геополитический риск как экзогенный сценарий",
    kind=KIND_SCENARIO,
    description_ru=(
        "Индекс риска в [0,1] задаётся сценарием (постоянный, затухающий или событийный) и "
        "переводится в макро-шоки через априорные нагрузки. Это не оценка причинного эффекта и не "
        "гейт допуска: сценарий публикуется как «что если»."),
    assumptions=("индекс риска экзогенен", "нагрузки — допущение, а не оценка",
                 "передача линейна по индексу"),
    source="docs/38 block 16; docs/42 stage 5 scenario layer",
)

DEFAULT_LOADINGS = {
    "gdp_drag": 0.02,          # доля ВВП на единицу риска (отрицательный шок)
    "inflation_push": 0.01,    # п.п. инфляции на единицу риска
    "uncertainty_premium": 0.005,  # п.п. к реальной ставке на единицу риска
}


def risk_index_from_events(events, decay: float = 0.5, horizon: int | None = None) -> np.ndarray:
    """Build a risk-index path from event severities: idx_t = sum(severity * decay^(t - t_event)).

    events: iterable of dicts {"period": int, "severity": float in [0,1]}.  The result is clipped
    to [0, 1], so the index stays a fraction (a scenario assumption, not a measurement).
    """
    decay = float(decay)
    if not math.isfinite(decay) or not 0.0 <= decay <= 1.0:
        raise DynamicsError("decay must be in [0, 1]")
    parsed = []
    for event in events:
        if not isinstance(event, dict) or "period" not in event or "severity" not in event:
            raise DynamicsError("each event needs 'period' and 'severity'")
        period = int(event["period"])
        severity = float(event["severity"])
        if period < 0:
            raise DynamicsError("event period must be non-negative")
        if not math.isfinite(severity) or not 0.0 <= severity <= 1.0:
            raise DynamicsError("event severity must be a fraction in [0, 1]")
        parsed.append((period, severity))
    if not parsed:
        raise DynamicsError("at least one event is required")
    length = int(horizon) if horizon is not None else max(period for period, _ in parsed) + 1
    if length < 1:
        raise DynamicsError("horizon must be positive")
    index = np.zeros(length)
    for period, severity in parsed:
        for step in range(period, length):
            index[step] += severity * decay ** (step - period)
    return np.clip(index, 0.0, 1.0)


class GeopoliticalRiskScenario:
    """Exogenous geopolitical-risk path translated into macro shocks by assumed loadings."""

    def __init__(self, risk_index: float = 0.3, persistence: float = 0.8,
                 loadings: dict | None = None):
        risk_index, persistence = float(risk_index), float(persistence)
        if not math.isfinite(risk_index) or not 0.0 <= risk_index <= 1.0:
            raise DynamicsError("risk_index must be a fraction in [0, 1]")
        if not math.isfinite(persistence) or not 0.0 <= persistence <= 1.0:
            raise DynamicsError("persistence must be in [0, 1]")
        loadings = dict(DEFAULT_LOADINGS if loadings is None else loadings)
        unknown = set(loadings) - set(DEFAULT_LOADINGS)
        if unknown:
            raise DynamicsError(f"unknown loadings: {sorted(unknown)}")
        for name, value in loadings.items():
            value = float(value)
            if not math.isfinite(value):
                raise DynamicsError(f"loading {name!r} must be finite")
            if name != "gdp_drag" and value < 0.0:
                raise DynamicsError(f"loading {name!r} must be non-negative")
            loadings[name] = value
        self.risk_index = risk_index
        self.persistence = persistence
        self.loadings = loadings

    def risk_path(self, periods: int = 20) -> np.ndarray:
        """Geometrically decaying risk path: risk_t = risk_0 * persistence**t."""
        periods = int(periods)
        if periods < 1:
            raise DynamicsError("periods must be a positive integer")
        return self.risk_index * self.persistence ** np.arange(periods)

    def macro_effects(self, periods: int = 20) -> dict:
        """Assumed macro effects: gdp_drag negative, inflation/uncertainty positive."""
        path = self.risk_path(periods)
        return {
            "risk_index": path,
            "gdp_drag": -abs(self.loadings["gdp_drag"]) * path,
            "inflation_push": self.loadings["inflation_push"] * path,
            "uncertainty_premium": self.loadings["uncertainty_premium"] * path,
        }

    def mechanism(self) -> Mechanism:
        return Mechanism(
            mechanism_id=GEOPOLITICAL_RISK_MECHANISM.mechanism_id,
            title_ru=GEOPOLITICAL_RISK_MECHANISM.title_ru,
            kind=GEOPOLITICAL_RISK_MECHANISM.kind,
            description_ru=(GEOPOLITICAL_RISK_MECHANISM.description_ru
                            + " Нагрузки сценария: "
                            + ", ".join(f"{name}={value}" for name, value in self.loadings.items())
                            + "."),
            assumptions=GEOPOLITICAL_RISK_MECHANISM.assumptions,
            source=GEOPOLITICAL_RISK_MECHANISM.source,
        )

    def report(self, periods: int = 20) -> dict:
        effects = self.macro_effects(periods)
        return {
            "mechanism": self.mechanism().to_dict(),
            "risk_index": [float(value) for value in effects["risk_index"]],
            "gdp_drag": [float(value) for value in effects["gdp_drag"]],
            "inflation_push": [float(value) for value in effects["inflation_push"]],
            "uncertainty_premium": [float(value) for value in effects["uncertainty_premium"]],
            "causal_claim": False,
            "caveat": ("экзогенный сценарий: индекс и нагрузки являются допущениями, а не оценкой "
                       "причинного эффекта; сценарий не гейтит допуск и не прогнозирует конфликт"),
        }


def scenario_comparison(periods: int = 20, risk_levels=(0.2, 0.5, 0.8)) -> dict:
    """Side-by-side macro effects of several risk levels (each is an exogenous scenario)."""
    outputs = {}
    for level in risk_levels:
        scenario = GeopoliticalRiskScenario(risk_index=float(level))
        effects = scenario.macro_effects(periods)
        outputs[f"risk_{float(level):.2f}"] = {
            "risk_index": float(effects["risk_index"][0]),
            "gdp_drag_total": float(np.sum(effects["gdp_drag"])),
            "inflation_push_total": float(np.sum(effects["inflation_push"])),
        }
    return {"scenarios": outputs,
            "caveat": "сравнение сценариев, не причинный эффект и не прогноз"}
