"""Markov-switching regimes: a testable hypothesis, not an identity.

A hidden Markov chain selects the regime each period; conditional on the regime the observed
series is Gaussian with a regime-specific mean and standard deviation.  The model is a
HYPOTHESIS: it is useful only while out-of-sample tests (and the tests in
``tests/test_dynamics.py``) keep it alive.  Regimes are not observed, so the filtered/smoothed
probabilities are model-based beliefs, not measurements.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_HYPOTHESIS, Mechanism

MARKOV_SWITCHING_MECHANISM = Mechanism(
    mechanism_id="DYN-REGIME-MARKOV",
    title_ru="Марковские переключения режимов",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Наблюдаемый ряд порождается смесью режимов: скрытая марковская цепь с матрицей переходов "
        "P выбирает режим, внутри режима ряд имеет собственное среднее и дисперсию. Гипотеза "
        "проверяется сравнением OOS-качества с однорежимной моделью и тестами на переключение."),
    assumptions=("число режимов задано априори", "матрица переходов постоянна внутри окна",
                 "условно на режим наблюдения гауссовы и независимы"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 blocks 13/14; docs/34 M06",
)


def _normal_pdf(value: float, mean: float, sigma: float) -> float:
    z = (value - mean) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


class MarkovSwitchingModel:
    """Two-or-more-regime Markov switching model with a Hamilton filter."""

    def __init__(self, transition_matrix, means, sigmas, seed: int | None = None):
        matrix = np.asarray(transition_matrix, dtype=float)
        if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
            raise DynamicsError("transition_matrix must be a square matrix")
        regimes = matrix.shape[0]
        if regimes < 2:
            raise DynamicsError("at least two regimes are required")
        if not np.all(np.isfinite(matrix)):
            raise DynamicsError("transition_matrix contains non-finite values")
        if np.any(matrix < 0.0):
            raise DynamicsError("transition probabilities must be non-negative")
        if not np.allclose(matrix.sum(axis=1), 1.0, atol=1e-10):
            raise DynamicsError("each transition-matrix row must sum to 1")
        means = np.asarray(means, dtype=float)
        sigmas = np.asarray(sigmas, dtype=float)
        if means.shape != (regimes,) or sigmas.shape != (regimes,):
            raise DynamicsError(f"means and sigmas must have shape ({regimes},)")
        if not np.all(np.isfinite(means)) or not np.all(np.isfinite(sigmas)):
            raise DynamicsError("means/sigmas contain non-finite values")
        if np.any(sigmas <= 0.0):
            raise DynamicsError("sigmas must be strictly positive")
        self.transition_matrix = matrix
        self.means = means
        self.sigmas = sigmas
        self.seed = seed

    @property
    def n_regimes(self) -> int:
        return int(self.transition_matrix.shape[0])

    def stationary_distribution(self) -> np.ndarray:
        """Unconditional regime distribution pi with pi = pi P."""
        eigenvalues, eigenvectors = np.linalg.eig(self.transition_matrix.T)
        index = int(np.argmin(np.abs(eigenvalues - 1.0)))
        vector = np.real(eigenvectors[:, index])
        vector = np.clip(vector, 0.0, None)
        total = float(vector.sum())
        if total <= 0.0:
            raise DynamicsError("could not derive a positive stationary distribution")
        return vector / total

    def expected_durations(self) -> np.ndarray:
        """Expected number of periods spent in each regime (inf if absorbing)."""
        diagonals = np.diag(self.transition_matrix)
        with np.errstate(divide="ignore"):
            return np.where(diagonals < 1.0, 1.0 / (1.0 - diagonals), np.inf)

    def simulate(self, periods: int, seed: int | None = None) -> dict:
        """Simulate the hidden chain and the observations (deterministic given the seed)."""
        periods = int(periods)
        if periods < 1:
            raise DynamicsError("periods must be a positive integer")
        rng = np.random.default_rng(self.seed if seed is None else seed)
        states = np.empty(periods, dtype=np.int64)
        states[0] = int(rng.choice(self.n_regimes, p=self.stationary_distribution()))
        for step in range(1, periods):
            states[step] = int(rng.choice(self.n_regimes, p=self.transition_matrix[states[step - 1]]))
        observations = rng.normal(self.means[states], self.sigmas[states])
        return {"states": states, "observations": observations}

    def filter(self, observations) -> dict:
        """Hamilton filter: filtered regime probabilities and the log-likelihood."""
        values = np.asarray(observations, dtype=float)
        if values.ndim != 1 or values.size == 0:
            raise DynamicsError("observations must be a non-empty 1-D array")
        if not np.all(np.isfinite(values)):
            raise DynamicsError("observations contain non-finite values")
        n = values.size
        predicted = np.empty((n, self.n_regimes))
        filtered = np.empty((n, self.n_regimes))
        prior = self.stationary_distribution()
        log_likelihood = 0.0
        for step, value in enumerate(values):
            prediction = prior if step == 0 else filtered[step - 1] @ self.transition_matrix
            prediction = prediction / prediction.sum()
            likelihood = np.array([_normal_pdf(value, self.means[j], self.sigmas[j])
                                   for j in range(self.n_regimes)])
            evidence = float(np.dot(prediction, likelihood))
            if evidence <= 0.0:
                evidence = 1e-300
            posterior = prediction * likelihood / evidence
            predicted[step] = prediction
            filtered[step] = posterior
            log_likelihood += math.log(evidence)
            prior = posterior
        return {"filtered_probabilities": filtered,
                "predicted_probabilities": predicted,
                "log_likelihood": log_likelihood,
                "n_observations": n}

    def regime_probabilities(self, observations) -> np.ndarray:
        """Alias of the filtered regime probabilities (beliefs, not measurements)."""
        return self.filter(observations)["filtered_probabilities"]

    def most_likely_regime(self, observations) -> np.ndarray:
        return np.argmax(self.filter(observations)["filtered_probabilities"], axis=1)
