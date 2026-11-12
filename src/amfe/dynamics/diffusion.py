"""Innovation/technology diffusion: S-curves as a testable hypothesis + a capacity limit.

Two devices are separated:

* the logistic curve is a HYPOTHESIS about how adoption spreads (slow start, acceleration, saturation);
* the ceiling is a PHYSICAL_LIMIT: cumulative adoption cannot exceed the market/resource capacity.

The Bass discrete recursion is used as a second, more flexible S-shape family.  Neither device is an
identity: both must be tested out of sample, and the fitted rate is not a causal effect of any policy.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_HYPOTHESIS, KIND_PHYSICAL_LIMIT, Mechanism

LOGISTIC_DIFFUSION_MECHANISM = Mechanism(
    mechanism_id="DYN-DIFFUSION-LOGISTIC",
    title_ru="Логистическая S-кривая диффузии",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Доля внедрения растёт по логистической кривой: медленный старт, ускорение, насыщение на "
        "потолке. Гипотеза о механизме распространения; скорость и потолок оцениваются и проверяются "
        "вне выборки."),
    assumptions=("потолок задан экзогенно", "скорость постоянна внутри периода"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 17; docs/34",
)

ADOPTION_CEILING_MECHANISM = Mechanism(
    mechanism_id="DYN-DIFFUSION-CEILING",
    title_ru="Потолок внедрения (физическое ограничение)",
    kind=KIND_PHYSICAL_LIMIT,
    description_ru=(
        "Кумулятивная доля внедрения не может превысить ёмкость рынка/ресурса: x_t <= ceiling. "
        "Это ограничение, а не гипотеза; оно проверяется по построению траектории."),
    assumptions=("ёмкость задана и не меняется внутри сценария",),
    source="docs/38 I10; docs/42 stage 5",
)

BASS_DIFFUSION_MECHANISM = Mechanism(
    mechanism_id="DYN-DIFFUSION-BASS",
    title_ru="Дискретная модель Басса",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "F_{t+1} = F_t + (p + q*F_t)*(1 - F_t): инновационный (p) и имитационный (q) каналы "
        "диффузии. Гипотеза о распространении; p и q оцениваются по данным внедрения."),
    assumptions=("потолок рынка фиксирован", "p, q постоянны"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 17",
)


def _check_fraction(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        raise DynamicsError(f"{name} must be a fraction strictly between 0 and 1")
    return value


class LogisticDiffusion:
    """Discrete logistic diffusion x_{t+1} = x_t + rate*x_t*(1 - x_t/ceiling)."""

    def __init__(self, rate: float = 0.30, ceiling: float = 1.0, initial_fraction: float = 0.01):
        rate, ceiling, initial_fraction = float(rate), float(ceiling), float(initial_fraction)
        if not all(math.isfinite(item) for item in (rate, ceiling, initial_fraction)):
            raise DynamicsError("diffusion parameters must be finite")
        if rate <= 0.0:
            raise DynamicsError("rate must be positive")
        if ceiling <= 0.0:
            raise DynamicsError("ceiling must be positive")
        if not 0.0 < initial_fraction < ceiling:
            raise DynamicsError("initial_fraction must lie strictly between 0 and the ceiling")
        self.rate = rate
        self.ceiling = ceiling
        self.initial_fraction = initial_fraction

    def step(self, level: float) -> float:
        level = float(level)
        if not math.isfinite(level):
            raise DynamicsError("level must be finite")
        updated = level + self.rate * level * (1.0 - level / self.ceiling)
        return float(min(updated, self.ceiling))  # physical limit: never above capacity

    def simulate(self, periods: int = 60) -> np.ndarray:
        periods = int(periods)
        if periods < 1:
            raise DynamicsError("periods must be a positive integer")
        path = np.empty(periods + 1)
        path[0] = self.initial_fraction
        for step in range(periods):
            path[step + 1] = self.step(path[step])
        return path

    def time_to(self, fraction: float, periods: int = 400) -> int | None:
        """First period at which the level reaches `fraction` * ceiling (None if never)."""
        target = _check_fraction(fraction, "fraction") * self.ceiling
        path = self.simulate(periods)
        hits = np.nonzero(path >= target)[0]
        return None if hits.size == 0 else int(hits[0])

    def report(self, periods: int = 80) -> dict:
        path = self.simulate(periods)
        bounded = bool(np.all(path <= self.ceiling * (1.0 + 1e-12)))
        monotone = bool(np.all(np.diff(path) >= -1e-12))
        increments = np.diff(path)
        peak_index = int(np.argmax(increments)) if increments.size else 0
        return {
            "path": path,
            "monotone": monotone,
            "bounded": bounded,
            "ceiling": self.ceiling,
            "inflection_level": 0.5 * self.ceiling,
            "peak_growth_period": peak_index,
            "times_to_reach": {f"fraction_{fraction:.2f}": self.time_to(fraction)
                               for fraction in (0.10, 0.50, 0.90)},
            "mechanism": LOGISTIC_DIFFUSION_MECHANISM.to_dict(),
            "physical_limit": ADOPTION_CEILING_MECHANISM.to_dict(),
            "note": "гипотеза о форме кривой; потолок — ограничение, а не оценка",
        }


def bass_simulate(p: float, q: float, ceiling: float = 1.0,
                  periods: int = 60, initial_fraction: float = 0.0) -> np.ndarray:
    """Discrete Bass cumulative adoption F_{t+1} = F_t + (p + qF_t)(1 - F_t) (fractions)."""
    p, q, ceiling = float(p), float(q), float(ceiling)
    periods = int(periods)
    if not all(math.isfinite(item) for item in (p, q, ceiling)):
        raise DynamicsError("Bass parameters must be finite")
    if p <= 0.0:
        raise DynamicsError("p must be positive")
    if q < 0.0:
        raise DynamicsError("q must be non-negative")
    if ceiling <= 0.0:
        raise DynamicsError("ceiling must be positive")
    if periods < 1:
        raise DynamicsError("periods must be a positive integer")
    initial_fraction = float(initial_fraction)
    if not math.isfinite(initial_fraction) or not 0.0 <= initial_fraction < 1.0:
        raise DynamicsError("initial_fraction must be a fraction in [0, 1)")
    level = initial_fraction
    path = np.empty(periods + 1)
    path[0] = level
    for step in range(periods):
        level = level + (p + q * level) * (1.0 - level)
        level = min(level, 1.0)  # physical limit
        path[step + 1] = level
    return path * ceiling


def bass_peak_time(p: float, q: float) -> float | None:
    """Analytic continuous-time Bass peak of new adoption: ln(q/p)/(p+q), None when q <= p."""
    p, q = float(p), float(q)
    if p <= 0.0:
        raise DynamicsError("p must be positive")
    if q < 0.0:
        raise DynamicsError("q must be non-negative")
    if q <= p:
        return None
    return math.log(q / p) / (p + q)


def diffusion_report(p: float, q: float, periods: int = 60) -> dict:
    """S-curve diagnostics: monotone growth, capacity bound, peak of new adoption."""
    path = bass_simulate(p, q, ceiling=1.0, periods=periods)
    increments = np.diff(path)
    peak_index = int(np.argmax(increments)) if increments.size else 0
    analytic = bass_peak_time(p, q)
    return {
        "path": path,
        "monotone": bool(np.all(increments >= -1e-12)),
        "bounded": bool(np.all(path <= 1.0 + 1e-12)),
        "peak_growth_period": peak_index,
        "analytic_peak_time": analytic,
        "peak_gap": None if analytic is None else float(peak_index - analytic),
        "mechanism": BASS_DIFFUSION_MECHANISM.to_dict(),
        "physical_limit": ADOPTION_CEILING_MECHANISM.to_dict(),
        "note": "S-кривая — гипотеза; форма не доказывает причинный эффект инновации",
    }
