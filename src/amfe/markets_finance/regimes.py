"""Volatility regimes for the finance prototype (docs/149).

Two deterministic regime models are provided, both fitted **only on the estimation window**:

* :class:`VolatilityRegimeModel` - a quantile classifier: rolling realised volatility is split into
  LOW / NORMAL / HIGH regimes by training-window quantiles.  Cheap, transparent and hard to
  overfit; the thresholds are frozen at fit time, so classifying later data uses no future
  information.
* :class:`GaussianMixtureRegimeModel` - a two/three-state one-dimensional Gaussian mixture fitted by
  expectation-maximisation with a deterministic quantile initialisation (no RNG).  States are
  ordered by volatility, so ``LOW`` always means the lowest-variance state.  The transition matrix
  is the row-normalised count matrix of the hard state sequence; no smoothing is applied.

What the regime block is **not**: it is not a trading signal and it does not claim predictability.
Its honest job in the prototype is to describe heteroskedasticity (state shares, persistence,
factor behaviour by regime) and to let the backtest report regime-conditional numbers.

Public API::

    model = VolatilityRegimeModel(window=60).fit(train_returns)
    classification = model.classify(returns)
    model.stats(classification)["shares"]
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import DataClass, MarketDataError, ModelCard, ModelStatus, ensure_finite, rolling_vol

__all__ = ["REGIME_LABELS", "RegimeConfig", "RegimeClassification", "VolatilityRegimeModel",
           "GaussianMixtureRegimeModel"]

REGIME_LABELS: tuple[str, ...] = ("LOW", "NORMAL", "HIGH")
_ALLOWED_LABELS: tuple[str, ...] = REGIME_LABELS + ("WARMUP",)


@dataclass(frozen=True)
class RegimeConfig:
    """Shared configuration of the regime block."""

    window: int = 60
    low_quantile: float = 1.0 / 3.0
    high_quantile: float = 2.0 / 3.0

    def __post_init__(self) -> None:
        if self.window < 10:
            raise MarketDataError("regime window must be >= 10 observations")
        if not 0.0 < self.low_quantile < self.high_quantile < 1.0:
            raise MarketDataError("require 0 < low_quantile < high_quantile < 1")


@dataclass(frozen=True)
class RegimeClassification:
    """Regime labels with the volatility used to classify and the thresholds applied."""

    labels: tuple[str, ...]
    volatility: np.ndarray
    thresholds: tuple[float, float]
    warmup: int

    def __post_init__(self) -> None:
        if len(self.labels) != self.volatility.size:
            raise MarketDataError("labels and volatility are misaligned")
        if any(label not in _ALLOWED_LABELS for label in self.labels):
            raise MarketDataError(f"unknown regime labels: "
                                  f"{set(self.labels) - set(_ALLOWED_LABELS)}")
        if self.thresholds[0] > self.thresholds[1]:
            raise MarketDataError("regime thresholds are not ordered")

    def count(self, label: str) -> int:
        return int(sum(1 for item in self.labels if item == label))

    def shares(self) -> dict[str, float]:
        classified = sum(1 for label in self.labels if label in REGIME_LABELS)
        return {label: (self.count(label) / classified if classified else 0.0)
                for label in REGIME_LABELS}

    def to_dict(self) -> dict:
        return {"labels": list(self.labels),
                "volatility": np.asarray(self.volatility).round(10).tolist(),
                "thresholds": [float(self.thresholds[0]), float(self.thresholds[1])],
                "warmup": int(self.warmup),
                "shares": {key: round(value, 6) for key, value in self.shares().items()}}


class VolatilityRegimeModel:
    """Quantile volatility classifier: thresholds fitted on the estimation window only."""

    model_id = "AMFE-FINANCE-VOL-REGIME"
    version = "0.1.0"

    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = RegimeConfig() if config is None else config
        self._thresholds: tuple[float, float] | None = None

    @property
    def fitted(self) -> bool:
        return self._thresholds is not None

    def fit(self, returns: np.ndarray) -> "VolatilityRegimeModel":
        array = ensure_finite(returns, "returns")
        if array.size < self.config.window + 1:
            raise MarketDataError(f"fit needs >= {self.config.window + 1} returns, got {array.size}")
        vols = rolling_vol(array, self.config.window)
        usable = vols[np.isfinite(vols)]
        if usable.size < 3:
            raise MarketDataError("not enough usable volatility observations to fit thresholds")
        low = float(np.quantile(usable, self.config.low_quantile))
        high = float(np.quantile(usable, self.config.high_quantile))
        if low == high:
            raise MarketDataError("degenerate volatility distribution: quantiles coincide")
        self._thresholds = (low, high)
        return self

    def classify(self, returns: np.ndarray) -> RegimeClassification:
        """Classify with frozen thresholds; early rolling-vol points are labelled ``WARMUP``."""
        if self._thresholds is None:
            raise MarketDataError("the regime model is not fitted: call fit() first")
        array = ensure_finite(returns, "returns")
        vols = rolling_vol(array, self.config.window)
        low, high = self._thresholds
        labels: list[str] = []
        warmup = 0
        for value in vols:
            if not np.isfinite(value):
                labels.append("WARMUP")
                warmup += 1
            elif value <= low:
                labels.append("LOW")
            elif value >= high:
                labels.append("HIGH")
            else:
                labels.append("NORMAL")
        return RegimeClassification(labels=tuple(labels), volatility=vols,
                                    thresholds=(low, high), warmup=warmup)

    def stats(self, classification: RegimeClassification,
              factor_returns: np.ndarray | None = None) -> dict:
        """Shares, persistence and (optionally) mean factor returns by regime."""
        counts = {"WARMUP": classification.warmup}
        counts.update({label: classification.count(label) for label in REGIME_LABELS})
        result: dict = {"counts": counts, "shares": classification.shares(),
                        "thresholds": list(classification.thresholds)}
        if factor_returns is not None:
            matrix = ensure_finite(factor_returns, "factor_returns")
            if matrix.shape[0] != len(classification.labels):
                raise MarketDataError("factor_returns and regime labels are misaligned")
            by_regime = {}
            for label in REGIME_LABELS:
                mask = np.array([item == label for item in classification.labels])
                if mask.any():
                    by_regime[label] = np.asarray(matrix[mask].mean(axis=0)).round(10).tolist()
            result["factor_mean_by_regime"] = by_regime
        return result

    def model_card(self) -> ModelCard:
        return ModelCard(
            model_id=self.model_id, version=self.version,
            kind="quantile classifier of rolling realised volatility (LOW/NORMAL/HIGH)",
            inputs=("return series", "rolling window", "quantiles"),
            outputs=("regime labels", "realised volatility", "regime statistics"),
            data_classes=(DataClass.SYNTHETIC, DataClass.LOCAL_CSV),
            limitations=("no forecasting: labels are contemporaneous, not predictive",
                         "thresholds are sample-dependent and drift across markets",
                         "volatility clustering is described, not modelled with a state process",
                         "single-asset classifier: no cross-asset regime inference"),
            tests=("tests/test_markets_finance.py::RegimeTests",),
            status=ModelStatus.RESEARCH_ONLY, deterministic=True)


class GaussianMixtureRegimeModel:
    """Deterministic 2-3 state Gaussian mixture on one-dimensional returns (EM, quantile init)."""

    model_id = "AMFE-FINANCE-GMM-REGIME"
    version = "0.1.0"

    def __init__(self, n_states: int = 2, max_iter: int = 300, tolerance: float = 1e-7,
                 variance_floor: float = 0.05, min_weight: float = 0.05) -> None:
        if n_states not in (2, 3):
            raise MarketDataError(f"n_states must be 2 or 3, got {n_states}")
        if max_iter < 5:
            raise MarketDataError("max_iter must be >= 5")
        if tolerance <= 0.0:
            raise MarketDataError("tolerance must be positive")
        if not 0.0 <= variance_floor < 1.0:
            raise MarketDataError("variance_floor must be in [0, 1)")
        if not 0.0 <= min_weight < 0.5:
            raise MarketDataError("min_weight must be in [0, 0.5)")
        self.n_states = n_states
        self.max_iter = int(max_iter)
        self.tolerance = float(tolerance)
        self.variance_floor = float(variance_floor)
        self.min_weight = float(min_weight)
        self.means: np.ndarray | None = None
        self.variances: np.ndarray | None = None
        self.weights: np.ndarray | None = None
        self.log_likelihood_history: list[float] = []
        self.converged = False

    @property
    def fitted(self) -> bool:
        return self.means is not None

    def _mixture_density(self, values: np.ndarray) -> np.ndarray:
        assert self.means is not None and self.variances is not None and self.weights is not None
        densities = np.empty((values.size, self.n_states), dtype=float)
        for state in range(self.n_states):
            variance = max(float(self.variances[state]), 1e-16)
            densities[:, state] = self.weights[state] * np.exp(
                -0.5 * (values - self.means[state]) ** 2 / variance) / np.sqrt(
                2.0 * np.pi * variance)
        return densities

    def _responsibilities(self, values: np.ndarray) -> np.ndarray:
        densities = self._mixture_density(values)
        totals = densities.sum(axis=1, keepdims=True)
        totals[totals <= 0.0] = 1.0
        return densities / totals

    def fit(self, returns: np.ndarray) -> "GaussianMixtureRegimeModel":
        values = ensure_finite(returns, "returns")
        if values.size < 20:
            raise MarketDataError("the Gaussian mixture needs >= 20 returns")
        quantiles = np.linspace(0.0, 1.0, self.n_states + 2)[1:-1]
        self.means = np.quantile(values, quantiles).astype(float)
        base_variance = float(max(np.var(values, ddof=1), 1e-12))
        floor = max(1e-12, self.variance_floor * base_variance)
        self.variances = np.full(self.n_states, base_variance, dtype=float)
        self.weights = np.full(self.n_states, 1.0 / self.n_states, dtype=float)
        self.log_likelihood_history = []
        previous = -np.inf
        for _ in range(self.max_iter):
            responsibilities = self._responsibilities(values)
            totals = responsibilities.sum(axis=0) + 1e-12
            weights = totals / values.size
            if self.min_weight > 0.0:
                weights = np.clip(weights, self.min_weight, 1.0 - self.min_weight)
            self.weights = weights / weights.sum()
            self.means = (responsibilities * values[:, None]).sum(axis=0) / totals
            centered = (values[:, None] - self.means) ** 2
            self.variances = np.maximum((responsibilities * centered).sum(axis=0) / totals, floor)
            log_likelihood = float(np.sum(np.log(
                np.maximum(self._mixture_density(values).sum(axis=1), 1e-300))))
            self.log_likelihood_history.append(log_likelihood)
            if abs(log_likelihood - previous) < self.tolerance:
                self.converged = True
                break
            previous = log_likelihood
        order = np.argsort(self.variances, kind="stable")  # states ordered by volatility
        self.means = self.means[order]
        self.variances = self.variances[order]
        self.weights = self.weights[order]
        return self

    def classify(self, returns: np.ndarray) -> np.ndarray:
        """Hard labels ordered by volatility (``LOW`` .. ``HIGH``); warmup is not used here."""
        if not self.fitted:
            raise MarketDataError("the Gaussian mixture is not fitted: call fit() first")
        values = ensure_finite(returns, "returns")
        responsibilities = self._responsibilities(values)
        states = np.argmax(responsibilities, axis=1)
        labels = tuple(REGIME_LABELS[: self.n_states])
        return np.asarray([labels[state] for state in states], dtype=object)

    def transition_matrix(self, returns: np.ndarray) -> np.ndarray:
        labels = self.classify(returns)
        names = tuple(REGIME_LABELS[: self.n_states])
        matrix = np.zeros((self.n_states, self.n_states), dtype=float)
        for previous, current in zip(labels[:-1], labels[1:]):
            matrix[names.index(previous), names.index(current)] += 1.0
        row_sums = matrix.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0.0] = 1.0
        return matrix / row_sums

    def stats(self, returns: np.ndarray) -> dict:
        if not self.fitted:
            raise MarketDataError("the Gaussian mixture is not fitted: call fit() first")
        labels = self.classify(returns)
        names = tuple(REGIME_LABELS[: self.n_states])
        counts = {label: int(np.sum(labels == label)) for label in names}
        matrix = self.transition_matrix(returns)
        durations = {}
        for index, label in enumerate(names):
            probability_stay = float(matrix[index, index])
            durations[label] = (1.0 / (1.0 - probability_stay)
                                if probability_stay < 1.0 else float("inf"))
        return {"states": list(names), "counts": counts,
                "weights": np.round(np.asarray(self.weights, dtype=float), 10).tolist(),
                "means": np.round(np.asarray(self.means, dtype=float), 10).tolist(),
                "volatilities": np.round(np.sqrt(np.asarray(self.variances, dtype=float)), 10).tolist(),
                "transition": matrix.round(10).tolist(),
                "expected_durations": durations,
                "iterations": len(self.log_likelihood_history),
                "converged": bool(self.converged)}

    def model_card(self) -> ModelCard:
        return ModelCard(
            model_id=self.model_id, version=self.version,
            kind="Gaussian mixture (2-3 states) on returns, EM with quantile initialisation",
            inputs=("return series", "n_states", "max_iter", "tolerance"),
            outputs=("state means/variances/weights", "hard labels", "transition matrix"),
            data_classes=(DataClass.SYNTHETIC, DataClass.LOCAL_CSV),
            limitations=("Gaussian states are a crude approximation of fat tails",
                         "EM converges to a local optimum; the deterministic initialisation fixes "
                         "which one",
                         "states are identified by volatility order only: economic interpretation "
                         "is not established",
                         "no smoothing / no online filtering: labels are in-sample assignments"),
            tests=("tests/test_markets_finance.py::RegimeTests",),
            status=ModelStatus.RESEARCH_ONLY, deterministic=True)
