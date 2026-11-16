"""Rolling out-of-sample backtest for the finance prototype (docs/149).

Design decisions (and why they matter for honesty):

* **Strictly rolling OOS.**  At origin ``t`` the factor model (factors, loadings, premia) is fitted
  only on ``[t - window, t)``; the forecast targets the *next* period.  Future rows cannot change a
  prediction - this is enforced by a dedicated test that mutates the future and compares.
* **Naive benchmarks are reported next to the model.**  ``zero`` = random walk (no predictability),
  ``historical_mean`` = the rolling mean of past returns.  Returns are compared with RMSE/MAE/hit
  rate/correlation, so a model that loses is visible in numbers, not adjectives.
* **Portfolio comparison is not a strategy.**  We compare a long-only "top-tercile by forecast,
  equal weight, daily rebalance" book with an equal-weight buy-and-hold book.  The result is a
  *diagnostic*: no costs, no liquidity, no borrow, no capacity.  The verdict block says explicitly
  where the model does **not** beat the naive benchmark and publishes the losing metrics.
* **Determinism.**  No RNG, no clock: the same inputs produce the same forecasts and the same
  SHA-256 report digest.

Public API::

    result = walk_forward(returns, characteristics_for_returns, dates, symbols)
    numbers = result.to_dict()          # metrics, portfolio comparison, verdict, sha256
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import MarketDataError, canonical_sha256, ensure_finite
from .data import FACTOR_NAMES
from .factor_model import FactorModel, FactorModelConfig

__all__ = ["WalkForwardConfig", "BacktestResult", "ForecastMetrics", "PortfolioMetrics",
           "walk_forward", "forecast_metrics", "portfolio_metrics", "max_drawdown",
           "top_fraction_weights"]


@dataclass(frozen=True)
class WalkForwardConfig:
    """Rolling backtest configuration; validated so a silent default cannot hide look-ahead."""

    estimation_window: int = 120
    factor_names: tuple[str, ...] = FACTOR_NAMES
    premium_rule: str = "mean"
    quantile: float = 1.0 / 3.0
    top_fraction: float = 1.0 / 3.0
    periods_per_year: int = 252
    min_assets: int = 6
    premium_window: int = 252

    def __post_init__(self) -> None:
        if self.estimation_window < 40:
            raise MarketDataError("estimation_window must be >= 40 observations")
        if not 0.0 < self.top_fraction < 1.0:
            raise MarketDataError(f"top_fraction must be in (0, 1), got {self.top_fraction}")
        if self.periods_per_year < 12:
            raise MarketDataError("periods_per_year must be >= 12")
        if self.min_assets < 2:
            raise MarketDataError("min_assets must be >= 2")
        if self.premium_window < 20:
            raise MarketDataError("premium_window must be >= 20 observations")
        # construct the nested configs once: their own validation is the contract
        FactorModelConfig(factor_names=self.factor_names, estimation_window=self.estimation_window,
                          quantile=self.quantile, premium_rule=self.premium_rule,
                          min_assets=self.min_assets)


@dataclass(frozen=True)
class ForecastMetrics:
    rmse: float
    mae: float
    hit_rate: float
    correlation: float
    bias: float
    observations: int

    def to_dict(self) -> dict:
        return {"rmse": self.rmse, "mae": self.mae, "hit_rate": self.hit_rate,
                "correlation": self.correlation, "bias": self.bias,
                "observations": self.observations}


@dataclass(frozen=True)
class PortfolioMetrics:
    annualised_mean: float
    annualised_vol: float
    sharpe: float
    max_drawdown: float
    hit_rate: float
    cumulative_return: float
    observations: int

    def to_dict(self) -> dict:
        return {"annualised_mean": self.annualised_mean, "annualised_vol": self.annualised_vol,
                "sharpe": self.sharpe, "max_drawdown": self.max_drawdown,
                "hit_rate": self.hit_rate, "cumulative_return": self.cumulative_return,
                "observations": self.observations}


def _as_matrix(values, name: str) -> np.ndarray:
    array = ensure_finite(values, name)
    if array.ndim != 2:
        raise MarketDataError(f"{name} must be 2-D, got shape {array.shape}")
    return array


def forecast_metrics(predictions: np.ndarray, realized: np.ndarray) -> ForecastMetrics:
    """Forecast accuracy of a ``(T, N)`` prediction panel against realised returns."""
    forecast = _as_matrix(predictions, "predictions")
    actual = _as_matrix(realized, "realized")
    if forecast.shape != actual.shape:
        raise MarketDataError(f"predictions {forecast.shape} and realized {actual.shape} "
                              "are misaligned")
    error = forecast - actual
    rmse = float(np.sqrt(np.mean(error ** 2)))
    mae = float(np.mean(np.abs(error)))
    hit = float(np.mean(np.sign(forecast) == np.sign(actual)))
    flat_forecast = float(np.std(forecast))
    flat_actual = float(np.std(actual))
    correlation = 0.0
    if flat_forecast > 0.0 and flat_actual > 0.0:
        correlation = float(np.corrcoef(forecast.ravel(), actual.ravel())[0, 1])
    return ForecastMetrics(rmse=rmse, mae=mae, hit_rate=hit, correlation=correlation,
                           bias=float(np.mean(error)), observations=int(actual.size))


def max_drawdown(returns: np.ndarray) -> float:
    """Maximum drawdown of the compounded wealth path (a positive number, e.g. 0.12 = -12%)."""
    array = ensure_finite(returns, "returns")
    if array.size == 0:
        raise MarketDataError("returns: empty series")
    wealth = np.cumprod(1.0 + array)
    peaks = np.maximum.accumulate(wealth)
    return float(np.max(1.0 - wealth / peaks))


def portfolio_metrics(returns: np.ndarray, periods_per_year: int = 252) -> PortfolioMetrics:
    """Annualised statistics of a realised portfolio-return series (no risk-free adjustment)."""
    array = ensure_finite(returns, "returns")
    if array.size < 2:
        raise MarketDataError("at least two portfolio returns are required")
    if periods_per_year <= 0:
        raise MarketDataError("periods_per_year must be positive")
    mean = float(np.mean(array) * periods_per_year)
    vol = float(np.std(array, ddof=1) * np.sqrt(periods_per_year))
    sharpe = float(mean / vol) if vol > 0.0 else 0.0
    cumulative = float(np.prod(1.0 + array) - 1.0)
    return PortfolioMetrics(annualised_mean=mean, annualised_vol=vol, sharpe=sharpe,
                            max_drawdown=max_drawdown(array),
                            hit_rate=float(np.mean(array > 0.0)),
                            cumulative_return=cumulative, observations=int(array.size))


def top_fraction_weights(forecast: np.ndarray, top_fraction: float) -> np.ndarray:
    """Equal weights on the top-``fraction`` assets by forecast, zeros elsewhere (long-only)."""
    values = ensure_finite(forecast, "forecast")
    if values.ndim != 1:
        raise MarketDataError("forecast must be 1-D")
    if not 0.0 < top_fraction <= 1.0:
        raise MarketDataError(f"top_fraction must be in (0, 1], got {top_fraction}")
    count = max(1, int(round(values.size * top_fraction)))
    order = np.argsort(values, kind="stable")
    weights = np.zeros(values.size, dtype=float)
    weights[order[-count:]] = 1.0 / count
    return weights


@dataclass(frozen=True)
class BacktestResult:
    """OOS forecasts, naive benchmarks, realised returns and the full report dictionary."""

    dates: tuple[str, ...]
    symbols: tuple[str, ...]
    predictions: np.ndarray
    naive_zero: np.ndarray
    naive_mean: np.ndarray
    realized: np.ndarray
    factor_returns: np.ndarray
    config: WalkForwardConfig
    report: dict

    def to_dict(self) -> dict:
        return self.report


def walk_forward(returns: np.ndarray, characteristics: np.ndarray, dates: tuple[str, ...],
                 symbols: tuple[str, ...],
                 config: WalkForwardConfig | None = None) -> BacktestResult:
    """Strictly rolling OOS backtest of the factor model against naive benchmarks.

    ``characteristics[t]`` must be known before the return of period ``t`` (pass
    ``characteristics_for_returns = full_characteristics[:-1]`` when returns start at ``dates[1]``).
    """
    cfg = WalkForwardConfig() if config is None else config
    matrix = _as_matrix(returns, "returns")
    tensor = ensure_finite(characteristics, "characteristics")
    if tensor.ndim != 3:
        raise MarketDataError(f"characteristics must be 3-D (T, N, K), got {tensor.shape}")
    if tensor.shape[:2] != matrix.shape:
        raise MarketDataError(f"returns {matrix.shape} and characteristics {tensor.shape} are "
                              "misaligned")
    if tensor.shape[2] != len(cfg.factor_names):
        raise MarketDataError(f"{tensor.shape[2]} characteristics for {len(cfg.factor_names)} "
                              "factors")
    if len(dates) != matrix.shape[0]:
        raise MarketDataError(f"{len(dates)} dates for {matrix.shape[0]} return periods")
    names = tuple(symbols)
    if len(names) != matrix.shape[1]:
        raise MarketDataError(f"{len(names)} symbols for {matrix.shape[1]} assets")
    periods = matrix.shape[0]
    if periods <= cfg.estimation_window:
        raise MarketDataError(f"the panel has {periods} periods; the estimation window "
                              f"({cfg.estimation_window}) must leave OOS observations")
    model = FactorModel(FactorModelConfig(factor_names=cfg.factor_names,
                                          estimation_window=cfg.estimation_window,
                                          quantile=cfg.quantile,
                                          premium_rule=cfg.premium_rule,
                                          min_assets=cfg.min_assets))
    oos_periods = periods - cfg.estimation_window
    predictions = np.zeros((oos_periods, matrix.shape[1]), dtype=float)
    naive_mean = np.zeros_like(predictions)
    factor_returns = model.factors(matrix, tensor)  # diagnostics only (full sample, see docs)
    for step in range(oos_periods):
        origin = cfg.estimation_window + step
        train_returns = matrix[origin - cfg.estimation_window: origin]
        train_chars = tensor[origin - cfg.estimation_window: origin]
        premium_history = factor_returns[:origin][-cfg.premium_window:]
        expected, _fit = model.forecast(train_returns, train_chars, symbols=names,
                                        premium_returns=premium_history)
        predictions[step] = expected
        naive_mean[step] = np.mean(train_returns, axis=0)
    realized = matrix[cfg.estimation_window:]
    naive_zero = np.zeros_like(predictions)
    oos_dates = tuple(dates[cfg.estimation_window:])

    model_portfolio = np.array([float(top_fraction_weights(predictions[row], cfg.top_fraction)
                                      @ realized[row]) for row in range(oos_periods)])
    naive_mean_portfolio = np.array([float(top_fraction_weights(naive_mean[row],
                                                                cfg.top_fraction)
                                           @ realized[row]) for row in range(oos_periods)])
    buy_and_hold = realized.mean(axis=1)

    metrics = {
        "factor_model": forecast_metrics(predictions, realized).to_dict(),
        "naive_zero": forecast_metrics(naive_zero, realized).to_dict(),
        "naive_mean": forecast_metrics(naive_mean, realized).to_dict(),
    }
    portfolios = {
        "factor_top_fraction": portfolio_metrics(model_portfolio, cfg.periods_per_year).to_dict(),
        "naive_mean_top_fraction": portfolio_metrics(naive_mean_portfolio,
                                                     cfg.periods_per_year).to_dict(),
        "buy_and_hold_equal_weight": portfolio_metrics(buy_and_hold,
                                                       cfg.periods_per_year).to_dict(),
    }
    beats_zero = metrics["factor_model"]["rmse"] < metrics["naive_zero"]["rmse"]
    beats_mean = metrics["factor_model"]["rmse"] < metrics["naive_mean"]["rmse"]
    beats_buy_hold = (portfolios["factor_top_fraction"]["sharpe"]
                      > portfolios["buy_and_hold_equal_weight"]["sharpe"])
    report = {
        "schema": "AMFE-FINANCE-BACKTEST/1.0.0",
        "config": {"estimation_window": cfg.estimation_window,
                   "factor_names": list(cfg.factor_names), "premium_rule": cfg.premium_rule,
                   "quantile": cfg.quantile, "top_fraction": cfg.top_fraction,
                   "periods_per_year": cfg.periods_per_year,
                   "min_assets": cfg.min_assets, "premium_window": cfg.premium_window,
                   "premium_source": ("expanding factor-return history up to the origin; the "
                                      "loadings use the estimation window only")},
        "symbols": list(names), "oos_periods": int(oos_periods),
        "first_oos_date": oos_dates[0], "last_oos_date": oos_dates[-1],
        "forecast_metrics": metrics,
        "portfolio_metrics": portfolios,
        "verdict": {
            "factor_model_beats_random_walk_rmse": bool(beats_zero),
            "factor_model_beats_historical_mean_rmse": bool(beats_mean),
            "factor_portfolio_beats_buy_and_hold_sharpe": bool(beats_buy_hold),
            "statement": ("research diagnostic; no transaction costs, no borrow, no liquidity "
                          "model, synthetic data - not a trading strategy"),
            "where_not_better": [label for label, flag in (
                ("random walk (zero forecast)", beats_zero),
                ("historical mean forecast", beats_mean),
                ("equal-weight buy-and-hold (Sharpe)", beats_buy_hold)) if not flag],
        },
        "diagnostics": {
            "factor_returns_sha256": canonical_sha256(
                {"factors": np.asarray(factor_returns).round(10).tolist(),
                 "dates": [dates[0], dates[-1]]}),
            "predictions_sha256": canonical_sha256(
                np.asarray(predictions).round(10).tolist()),
            "realized_sha256": canonical_sha256(np.asarray(realized).round(10).tolist()),
            "factor_return_means": np.asarray(factor_returns).mean(axis=0).round(10).tolist(),
            "factor_return_vols": np.asarray(factor_returns).std(axis=0, ddof=1).round(10).tolist(),
        },
        "limitations": [
            "synthetic DGP with a known factor structure: the model is tested where it should "
            "work; real markets are harder (fat tails, breaks, microstructure)",
            "no transaction costs, slippage, borrow or capacity: the top-fraction book is a "
            "diagnostic, not an investable strategy",
            "single-factor-family linear model; no regime switching and no time-varying betas",
            "characteristics are synthetic and persistent: real value/quality need point-in-time "
            "fundamentals (vintage data)",
            "long-only top-fraction portfolio is one of many possible mappings of forecasts to "
            "weights; results are sensitive to that choice",
            "OOS periods are limited by the panel length (about 200 periods here): uncertainty of "
            "the reported metrics is not quantified",
        ],
    }
    return BacktestResult(dates=oos_dates, symbols=names, predictions=predictions,
                          naive_zero=naive_zero, naive_mean=naive_mean, realized=realized,
                          factor_returns=factor_returns, config=cfg, report=report)
