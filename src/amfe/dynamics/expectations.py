"""Expectations: adaptive rules, rational benchmark and least-squares learning.

Three different objects live here, and the classification matters more than the algebra:

* adaptive expectations are a behavioural HYPOTHESIS (a fixed-gain error-correction rule);
* rational expectations are a benchmark HYPOTHESIS about model-consistent forecasts: the forecast
  equals the conditional mean of the true process - they are *not* a statement that agents know
  the future, and they are only "rational" relative to the stated model;
* recursive least-squares learning is a HYPOTHESIS about how beliefs are updated from data; its
  convergence is a property of the estimator given the process, not evidence about the world.

All classes are deterministic given a seed.  No module here reads data or protocols.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_HYPOTHESIS, Mechanism

ADAPTIVE_EXPECTATIONS_MECHANISM = Mechanism(
    mechanism_id="DYN-EXPECT-ADAPTIVE",
    title_ru="Адаптивные ожидания",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Ожидание следующего значения корректируется на долю ошибки: E_{t+1} = E_t + gain*(y_t - E_t). "
        "Гипотеза о поведении, а не тождество: gain оценивается/калибруется и проверяется на данных."),
    assumptions=("ожидания обновляются только по прошлым наблюдениям", "gain постоянен"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 14",
)

RATIONAL_EXPECTATIONS_MECHANISM = Mechanism(
    mechanism_id="DYN-EXPECT-RATIONAL",
    title_ru="Рациональные ожидания (эталон)",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Ожидание равно условному среднему истинного процесса: E_t[y_{t+1}] = E[y_{t+1} | I_t]. "
        "Это эталонная гипотеза о согласованности ожиданий с моделью; ошибка прогноза в среднем "
        "нулевая и не коррелирует с доступной информацией. Не утверждает, что агенты знают будущее."),
    assumptions=("истинный процесс известен постановщику задачи", "агенты используют всю I_t"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 14; docs/34 M06",
)

LEARNING_MECHANISM = Mechanism(
    mechanism_id="DYN-EXPECT-LEARNING",
    title_ru="Обучение методом рекурсивных наименьших квадратов",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Агенты оценивают модель AR(1) рекурсивно (RLS) и используют текущую оценку как ожидание. "
        "Гипотеза: оценки сходятся к параметрам процесса по мере накопления наблюдений."),
    assumptions=("спецификация AR(1) верна", "забывание отсутствует (fixed-gain вариант — расширение)"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 14; docs/41 V2",
)


class AdaptiveExpectations:
    """Fixed-gain error-correction expectations (behavioural hypothesis)."""

    def __init__(self, gain: float = 0.3, initial: float | None = None):
        gain = float(gain)
        if not math.isfinite(gain) or not 0.0 < gain <= 1.0:
            raise DynamicsError("gain must be in (0, 1]")
        self.gain = gain
        self.initial = initial
        self._forecast = None if initial is None else float(initial)

    def reset(self, initial: float | None = None) -> None:
        self.initial = initial
        self._forecast = None if initial is None else float(initial)

    def update(self, observed: float) -> float:
        """One period: forecast_{t+1} = forecast_t + gain*(observed_t - forecast_t)."""
        observed = float(observed)
        if not math.isfinite(observed):
            raise DynamicsError("observed value must be finite")
        if self._forecast is None:
            self._forecast = observed
        else:
            self._forecast += self.gain * (observed - self._forecast)
        return self._forecast

    def run(self, observations) -> dict:
        values = np.asarray(observations, dtype=float)
        if values.ndim != 1 or values.size == 0:
            raise DynamicsError("observations must be a non-empty 1-D array")
        if not np.all(np.isfinite(values)):
            raise DynamicsError("observations contain non-finite values")
        self.reset(self.initial)
        forecasts = np.empty(values.size)
        for step, value in enumerate(values):
            forecasts[step] = self.update(value)
        errors = forecasts - values
        return {"forecasts": forecasts, "errors": errors,
                "final_forecast": float(forecasts[-1]),
                "mean_absolute_error": float(np.mean(np.abs(errors)))}


class RationalExpectationsAR1:
    """Rational-expectations benchmark for a stationary AR(1) process."""

    def __init__(self, intercept: float = 0.0, phi: float = 0.5, sigma: float = 1.0):
        intercept, phi, sigma = float(intercept), float(phi), float(sigma)
        if not all(math.isfinite(item) for item in (intercept, phi, sigma)):
            raise DynamicsError("intercept/phi/sigma must be finite")
        if abs(phi) >= 1.0:
            raise DynamicsError("|phi| < 1 is required for a stationary AR(1)")
        if sigma <= 0.0:
            raise DynamicsError("sigma must be positive")
        self.intercept = intercept
        self.phi = phi
        self.sigma = sigma

    @property
    def unconditional_mean(self) -> float:
        return self.intercept / (1.0 - self.phi)

    def forecast(self, current_value: float) -> float:
        """Rational one-step forecast E_t[y_{t+1}] = intercept + phi*y_t."""
        current_value = float(current_value)
        if not math.isfinite(current_value):
            raise DynamicsError("current_value must be finite")
        return self.intercept + self.phi * current_value

    def simulate(self, periods: int, seed: int | None = None) -> np.ndarray:
        periods = int(periods)
        if periods < 1:
            raise DynamicsError("periods must be a positive integer")
        rng = np.random.default_rng(seed)
        values = np.empty(periods)
        innovation_scale = self.sigma / math.sqrt(1.0 - self.phi * self.phi)
        values[0] = self.unconditional_mean + innovation_scale * rng.normal()
        for step in range(1, periods):
            values[step] = self.intercept + self.phi * values[step - 1] + self.sigma * rng.normal()
        return values


class RecursiveLeastSquaresLearning:
    """Recursive least squares estimation of an AR(1) process (learning hypothesis).

    The regression is y_t = a + phi*y_{t-1} + e_t with the standard RLS update; the initial
    covariance is a large multiple of the identity (diffuse prior).  Convergence is judged by the
    gap between the final estimate and the true parameter.
    """

    def __init__(self, true_intercept: float = 0.0, true_phi: float = 0.5,
                 prior_scale: float = 1e3):
        true_intercept, true_phi = float(true_intercept), float(true_phi)
        prior_scale = float(prior_scale)
        if not all(math.isfinite(item) for item in (true_intercept, true_phi, prior_scale)):
            raise DynamicsError("parameters must be finite")
        if abs(true_phi) >= 1.0:
            raise DynamicsError("|true_phi| < 1 is required for a stationary AR(1)")
        if prior_scale <= 0.0:
            raise DynamicsError("prior_scale must be positive")
        self.true_intercept = true_intercept
        self.true_phi = true_phi
        self.prior_scale = prior_scale

    def run(self, observations, convergence_gap: float = 0.1) -> dict:
        values = np.asarray(observations, dtype=float)
        if values.ndim != 1 or values.size < 3:
            raise DynamicsError("at least three observations are required for learning")
        if not np.all(np.isfinite(values)):
            raise DynamicsError("observations contain non-finite values")
        theta = np.zeros(2)
        covariance = self.prior_scale * np.eye(2)
        estimates = np.empty((values.size, 2))
        for step in range(values.size):
            estimates[step] = theta
            if step == 0:
                continue
            design = np.array([1.0, values[step - 1]])
            denominator = 1.0 + float(design @ covariance @ design)
            gain = (covariance @ design) / denominator
            prediction = float(design @ theta)
            theta = theta + gain * (values[step] - prediction)
            covariance = covariance - np.outer(gain, design @ covariance)
        final_intercept, final_phi = float(theta[0]), float(theta[1])
        gap = abs(final_phi - self.true_phi)
        return {"coefficient_path": estimates,
                "final_intercept": final_intercept,
                "final_phi": final_phi,
                "phi_gap": gap,
                "converged": bool(gap <= convergence_gap),
                "convergence_gap": float(convergence_gap),
                "n_observations": int(values.size)}

    def simulate_and_learn(self, periods: int, seed: int | None = None,
                           convergence_gap: float = 0.1) -> dict:
        process = RationalExpectationsAR1(intercept=self.true_intercept, phi=self.true_phi)
        observations = process.simulate(periods, seed=seed)
        result = self.run(observations, convergence_gap=convergence_gap)
        result["observations"] = observations
        return result

