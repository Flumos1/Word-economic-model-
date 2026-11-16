"""Characteristic-based factor model for the finance prototype (docs/149).

The model is deliberately small and auditable (five factors, no neural nets, no vendor data):

1. **Factor construction** - long-short characteristic-sorted portfolios: for factor ``k`` at a
   return period the assets are ranked by the characteristic observed *before* the return; the top
   tercile is bought and the bottom tercile is sold in equal weights, so the factor return needs no
   estimation and no future information.
2. **Loadings** - per-asset OLS of the asset return on the five factor returns (with an intercept)
   over an estimation window.
3. **Premium forecast** - the expected next-period factor premium is the rolling mean (or median,
   or zero) of the factor returns *inside the estimation window only*.
4. **Forecast** - ``E[r_i] = alpha_i + sum_k beta_ik * E[f_k]``; the backtest compares this
   one-period forecast with naive benchmarks on out-of-sample data.

No-look-ahead invariants (tested in ``tests/test_markets_finance.py``):

* ``characteristics[t]`` is known at the close of the period that opens return ``t`` - the caller
  passes ``characteristics[:-1]`` of the price calendar when the returns start at ``dates[1]``;
* everything used at an OOS origin ``t`` lies in ``[t - window, t)``; future rows cannot change a
  prediction;
* no random number generator is involved at all - the model is a deterministic least-squares
  calculation.

Public API::

    model = FactorModel()
    fit = model.fit(returns_window, characteristics_window)
    expected = model.forecast(returns_window, characteristics_window)
    fit.r2, fit.loadings.shape, fit.premia
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import (DataClass, AssetClass, MarketDataError, ModelCard, ModelStatus, canonical_sha256,
                        ensure_finite)
from .data import FACTOR_NAMES

__all__ = ["FactorModelConfig", "FactorFit", "FactorModel", "build_factor_returns",
           "factor_summary"]


@dataclass(frozen=True)
class FactorModelConfig:
    """Model configuration; every value is validated (a bad config is an error, never a default)."""

    factor_names: tuple[str, ...] = FACTOR_NAMES
    estimation_window: int = 120
    quantile: float = 1.0 / 3.0
    premium_rule: str = "mean"
    min_assets: int = 6
    ridge: float = 0.0

    def __post_init__(self) -> None:
        names = tuple(self.factor_names)
        if not 1 <= len(names) <= 8:
            raise MarketDataError(f"1-8 factors are supported, got {len(names)}")
        if len(set(names)) != len(names):
            raise MarketDataError(f"duplicate factor names: {names}")
        if not set(names) <= set(FACTOR_NAMES):
            unknown = sorted(set(names) - set(FACTOR_NAMES))
            raise MarketDataError(f"unknown factors: {unknown}; known: {FACTOR_NAMES}")
        if self.estimation_window < 20:
            raise MarketDataError("estimation_window must be >= 20 observations")
        if not 0.0 < self.quantile < 0.5:
            raise MarketDataError(f"quantile must be in (0, 0.5), got {self.quantile}")
        if self.premium_rule not in ("mean", "median", "zero"):
            raise MarketDataError(f"premium_rule must be mean/median/zero, got {self.premium_rule!r}")
        if self.min_assets < 2:
            raise MarketDataError("min_assets must be >= 2")
        if self.ridge < 0.0:
            raise MarketDataError(f"ridge must be non-negative, got {self.ridge}")


@dataclass(frozen=True)
class FactorFit:
    """One fitted cross-section: loadings, intercepts, factor premia and diagnostics."""

    factor_names: tuple[str, ...]
    symbols: tuple[str, ...]
    intercepts: np.ndarray
    loadings: np.ndarray
    premia: np.ndarray
    factor_returns: np.ndarray
    observations: int
    r2: float
    factor_vol: np.ndarray

    def __post_init__(self) -> None:
        assets = len(self.symbols)
        factors = len(self.factor_names)
        if self.loadings.shape != (assets, factors):
            raise MarketDataError(f"loadings shape {self.loadings.shape} != ({assets}, {factors})")
        if self.intercepts.shape != (assets,):
            raise MarketDataError(f"intercepts shape {self.intercepts.shape} != ({assets},)")
        if self.premia.shape != (factors,):
            raise MarketDataError(f"premia shape {self.premia.shape} != ({factors},)")
        if self.factor_returns.shape != (self.observations, factors):
            raise MarketDataError("factor_returns must be (observations, factors)")

    def expected_returns(self) -> np.ndarray:
        return self.intercepts + self.loadings @ self.premia

    def to_dict(self) -> dict:
        return {"factor_names": list(self.factor_names), "symbols": list(self.symbols),
                "observations": int(self.observations), "r2": float(self.r2),
                "intercepts": np.asarray(self.intercepts).round(10).tolist(),
                "loadings": np.asarray(self.loadings).round(10).tolist(),
                "premia": np.asarray(self.premia).round(10).tolist(),
                "factor_vol": np.asarray(self.factor_vol).round(10).tolist(),
                "sha256": canonical_sha256({"loadings": np.asarray(self.loadings).round(10).tolist(),
                                            "premia": np.asarray(self.premia).round(10).tolist(),
                                            "intercepts": np.asarray(self.intercepts).round(10).tolist()})}


def _validate_panel(returns: np.ndarray, characteristics: np.ndarray,
                    factor_names: tuple[str, ...], min_assets: int) -> tuple[np.ndarray, np.ndarray]:
    matrix = ensure_finite(returns, "returns")
    tensor = ensure_finite(characteristics, "characteristics")
    if matrix.ndim != 2:
        raise MarketDataError(f"returns must be 2-D (T, N), got shape {matrix.shape}")
    if tensor.ndim != 3:
        raise MarketDataError(f"characteristics must be 3-D (T, N, K), got shape {tensor.shape}")
    if matrix.shape != tensor.shape[:2]:
        raise MarketDataError(f"returns {matrix.shape} and characteristics {tensor.shape} are "
                              "misaligned (equal T and N are required)")
    if tensor.shape[2] != len(factor_names):
        raise MarketDataError(f"characteristics carry {tensor.shape[2]} characteristics for "
                              f"{len(factor_names)} factors")
    if matrix.shape[1] < min_assets:
        raise MarketDataError(f"panel has {matrix.shape[1]} assets; min_assets={min_assets}")
    if matrix.shape[0] < 2:
        raise MarketDataError("at least two return periods are required")
    return matrix, tensor


def build_factor_returns(returns: np.ndarray, characteristics: np.ndarray,
                         factor_names: tuple[str, ...] = FACTOR_NAMES,
                         quantile: float = 1.0 / 3.0,
                         min_assets: int = 6) -> np.ndarray:
    """Long-short characteristic-sorted factor returns ``(T, K)``.

    ``characteristics[t]`` must be observable *before* the return of period ``t`` (the caller aligns
    it); the function itself reads only row ``t`` of the characteristics to price the return of row
    ``t`` of the returns, therefore no information from ``t+1`` is used.
    """
    matrix, tensor = _validate_panel(returns, characteristics, tuple(factor_names), min_assets)
    if not 0.0 < quantile < 0.5:
        raise MarketDataError(f"quantile must be in (0, 0.5), got {quantile}")
    periods, assets = matrix.shape
    per_side = max(1, int(round(assets * quantile)))
    if per_side * 2 > assets:
        raise MarketDataError(f"terciles of {per_side} exceed the {assets} assets")
    out = np.zeros((periods, len(factor_names)), dtype=float)
    for t in range(periods):
        row = matrix[t]
        for k in range(len(factor_names)):
            order = np.argsort(tensor[t, :, k], kind="stable")
            longs, shorts = order[-per_side:], order[:per_side]
            out[t, k] = float(np.mean(row[longs]) - np.mean(row[shorts]))
    if not np.all(np.isfinite(out)):
        raise MarketDataError("factor construction produced non-finite returns")
    return out


def _ols_loadings(returns: np.ndarray, factors: np.ndarray,
                  ridge: float) -> tuple[np.ndarray, np.ndarray, float]:
    design = np.column_stack([np.ones(factors.shape[0]), factors])
    target = returns
    if ridge > 0.0:
        penalty = np.eye(design.shape[1]) * ridge
        penalty[0, 0] = 0.0  # the intercept is not penalised
        gram = design.T @ design + penalty
        coef = np.linalg.solve(gram, design.T @ target)
    else:
        coef, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
    fitted = design @ coef
    residual = target - fitted
    total = target - target.mean(axis=0, keepdims=True)
    ss_res = float(np.sum(residual ** 2))
    ss_tot = float(np.sum(total ** 2))
    r2 = 0.0 if ss_tot <= 0.0 else 1.0 - ss_res / ss_tot
    return coef[0].copy(), coef[1:].T.copy(), float(r2)


def factor_summary(factor_returns: np.ndarray, factor_names: tuple[str, ...] = FACTOR_NAMES) -> dict:
    """Descriptive statistics of the constructed factors (annualised on a 252-day convention)."""
    matrix = ensure_finite(factor_returns, "factor_returns")
    if matrix.ndim != 2 or matrix.shape[1] != len(factor_names):
        raise MarketDataError(f"factor_returns must be (T, {len(factor_names)}), "
                              f"got {matrix.shape}")
    if matrix.shape[0] < 2:
        raise MarketDataError("factor_summary needs at least two observations")
    means = matrix.mean(axis=0) * 252.0
    vols = matrix.std(axis=0, ddof=1) * np.sqrt(252.0)
    corr = np.corrcoef(matrix, rowvar=False)
    return {"factor_names": list(factor_names),
            "annualised_mean": means.round(10).tolist(),
            "annualised_vol": vols.round(10).tolist(),
            "correlation": np.asarray(corr).round(6).tolist(),
            "observations": int(matrix.shape[0]),
            "mean_abs_pairwise_correlation": float(
                np.mean(np.abs(corr[np.triu_indices(len(factor_names), 1)]))
                if len(factor_names) > 1 else 0.0)}


class FactorModel:
    """Deterministic five-factor prototype: construct factors, fit loadings, forecast premia."""

    model_id = "AMFE-FINANCE-FACTOR"
    version = "0.1.0"

    def __init__(self, config: FactorModelConfig | None = None) -> None:
        self.config = FactorModelConfig() if config is None else config

    # ------------------------------------------------------------------ factor construction
    def factors(self, returns: np.ndarray, characteristics: np.ndarray) -> np.ndarray:
        """Construct factor returns with the configured quantile and factor set."""
        factor_returns = build_factor_returns(
            returns, characteristics, self.config.factor_names,
            quantile=self.config.quantile, min_assets=self.config.min_assets)
        return factor_returns

    def premia(self, factor_returns: np.ndarray) -> np.ndarray:
        matrix = ensure_finite(factor_returns, "factor_returns")
        if matrix.ndim != 2 or matrix.shape[1] != len(self.config.factor_names):
            raise MarketDataError(f"factor_returns must be (T, {len(self.config.factor_names)})")
        if matrix.shape[0] < 1:
            raise MarketDataError("premia need at least one factor-return observation")
        rule = self.config.premium_rule
        if rule == "mean":
            return matrix.mean(axis=0)
        if rule == "median":
            return np.median(matrix, axis=0)
        return np.zeros(matrix.shape[1], dtype=float)

    # ---------------------------------------------------------------------------- fitting
    def fit(self, returns: np.ndarray, characteristics: np.ndarray,
            symbols: tuple[str, ...] | None = None,
            premium_returns: np.ndarray | None = None) -> FactorFit:
        """Fit loadings and premium forecast on an estimation window (no future rows).

        ``premium_returns`` optionally supplies a *longer* factor-return history (e.g. the
        expanding history up to the OOS origin) for the premium forecast while the loadings keep
        using the estimation window.  With ``premium_returns=None`` and ``premium_rule='mean'``
        the forecast is algebraically identical to the historical mean return - a property tested
        and documented in docs/149, which is why the backtest always passes a longer history.
        """
        cfg = self.config
        matrix, tensor = _validate_panel(returns, characteristics, cfg.factor_names,
                                         cfg.min_assets)
        if matrix.shape[0] < cfg.estimation_window:
            raise MarketDataError(f"the estimation window needs {cfg.estimation_window} "
                                  f"observations, got {matrix.shape[0]}")
        window_returns = matrix[-cfg.estimation_window:]
        window_chars = tensor[-cfg.estimation_window:]
        factor_returns = self.factors(window_returns, window_chars)
        intercepts, loadings, r2 = _ols_loadings(window_returns, factor_returns, cfg.ridge)
        if premium_returns is None:
            premia = self.premia(factor_returns)
        else:
            history = ensure_finite(premium_returns, "premium_returns")
            if history.ndim != 2 or history.shape[1] != len(cfg.factor_names):
                raise MarketDataError(f"premium_returns must be (T, {len(cfg.factor_names)}), "
                                      f"got {history.shape}")
            if history.shape[0] < 1:
                raise MarketDataError("premium_returns is empty")
            premia = self.premia(history)
        names = tuple(symbols) if symbols is not None else tuple(
            f"A{index + 1:02d}" for index in range(matrix.shape[1]))
        if len(names) != matrix.shape[1]:
            raise MarketDataError(f"{len(names)} symbols for {matrix.shape[1]} assets")
        return FactorFit(factor_names=cfg.factor_names, symbols=names, intercepts=intercepts,
                         loadings=loadings, premia=premia, factor_returns=factor_returns,
                         observations=int(window_returns.shape[0]), r2=r2,
                         factor_vol=factor_returns.std(axis=0, ddof=1))

    def forecast(self, returns: np.ndarray, characteristics: np.ndarray,
                 symbols: tuple[str, ...] | None = None,
                 premium_returns: np.ndarray | None = None) -> tuple[np.ndarray, FactorFit]:
        """One-period expected returns ``(N,)`` and the fit that produced them."""
        fit = self.fit(returns, characteristics, symbols=symbols,
                       premium_returns=premium_returns)
        return fit.expected_returns(), fit

    # --------------------------------------------------------------------------- diagnostics
    def model_card(self) -> ModelCard:
        return ModelCard(
            model_id=self.model_id, version=self.version,
            kind="characteristic-based linear factor model (5 factors, OLS loadings)",
            inputs=("aligned simple-return matrix (T, N)",
                    "characteristic tensor (T, N, K) observable before each return",
                    "estimation window and premium rule"),
            outputs=("factor return matrix (T, K)", "loadings (N, K)", "intercepts (N,)",
                     "expected one-period returns (N,)"),
            data_classes=(DataClass.SYNTHETIC, DataClass.LOCAL_CSV),
            limitations=(
                "static linear loadings: no regime switching, no time-varying betas",
                "premium forecast is the rolling mean/median of realised factor returns - not a "
                "macro forecast",
                "long-short factors ignore transaction costs, borrow constraints and capacity",
                "characteristics are taken as given: value/quality definitions are not validated "
                "on real fundamentals",
                "the synthetic DGP is not an economic model; results do not transfer to markets",
                "no point-in-time fundamentals: real value/quality need vintage data (docs/149)",
            ),
            tests=("tests/test_markets_finance.py::FactorModelTests",
                   "tests/test_markets_finance.py::NoLookAheadTests"),
            status=ModelStatus.RESEARCH_ONLY, deterministic=True,
            notes="research prototype; not an admission, not a trading strategy")
