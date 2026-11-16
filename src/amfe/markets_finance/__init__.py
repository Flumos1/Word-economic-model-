"""AMFE 2.0 finance layer: equities, crypto and FX as a research prototype (docs/145, docs/149).

What this package *is*: a small, auditable finance block with explicit data contracts, a
characteristic-based five-factor model, volatility-regime models, an FX/rates bookkeeping block and
a strictly rolling out-of-sample backtest against naive benchmarks.  Every artefact is
deterministic: no clock reads, no unseeded randomness, no network access.

What this package is *not*: it is not a trading system, not a forecast product, not an investment
recommendation and not an admission of any model.  Its output status is ``RESEARCH_ONLY`` and the
OOS numbers are published together with the metrics on which the prototype loses.

Layers::

    contracts   AssetClass / Provenance / PriceSeries / ReturnSeries / ModelCard / validation
    data        deterministic synthetic market with a known five-factor DGP
    factor_model  long-short characteristic factors, OLS loadings, premium forecast
    regimes     quantile volatility classifier + deterministic Gaussian mixture (EM)
    fx_rates    CIP forward/residual, carry, USD conversion, rate curve
    backtest    rolling OOS, naive benchmarks, forecast and portfolio metrics
    demo        reproducible end-to-end document with the numbers cited in docs/149

Public API::

    from amfe.markets_finance import (
        AssetClass, ModelCard, Provenance, PriceSeries, ReturnSeries, synthetic_dataset,
        SyntheticMarketConfig, FactorModel, FactorModelConfig, VolatilityRegimeModel,
        GaussianMixtureRegimeModel, RateCurve, cip_forward, walk_forward, run_demo,
    )
    dataset = synthetic_dataset(SyntheticMarketConfig(seed=20261116))
    series, returns = dataset.returns_matrix(AssetClass.EQUITY)
    dates = series[sorted(series)[0]].dates
    _names, characteristics = dataset.characteristics_matrix(AssetClass.EQUITY)
    result = walk_forward(returns, characteristics[:-1], dates, tuple(sorted(series)))
    result.to_dict()["verdict"]

Determinism contract: the same inputs produce byte-identical JSON documents
(``run_demo(...)["pipeline_sha256"]`` is stable across runs and machines with the same numpy).
"""
from __future__ import annotations

from .backtest import (BacktestResult, ForecastMetrics, PortfolioMetrics, WalkForwardConfig,
                       forecast_metrics, max_drawdown, portfolio_metrics, top_fraction_weights,
                       walk_forward)
from .contracts import (AssetClass, ContractError, DataClass, MarketDataError, ModelCard,
                        ModelCardError, ModelStatus, PriceSeries, Provenance, ReturnSeries,
                        annualise, canonical_sha256, ensure_finite, log_returns, rolling_vol,
                        simple_returns, validate_dates)
from .data import FACTOR_NAMES, SyntheticDataset, SyntheticMarketConfig, business_days, \
    synthetic_dataset
from .demo import run_demo
from .factor_model import (FactorFit, FactorModel, FactorModelConfig, build_factor_returns,
                           factor_summary)
from .fx_rates import (FXBlock, FXQuote, RateCurve, carry_return, cip_forward, cip_residual,
                       fx_cross, hedged_return, real_rate, term_spread, unhedged_return, usd_return)
from .regimes import (GaussianMixtureRegimeModel, RegimeClassification, RegimeConfig,
                      VolatilityRegimeModel)

__version__ = "0.1.0"
__status__ = "RESEARCH_ONLY"

__all__ = [
    "AssetClass", "DataClass", "ModelStatus", "MarketDataError", "ContractError",
    "ModelCardError", "Provenance", "PriceSeries", "ReturnSeries", "ModelCard",
    "simple_returns", "log_returns", "annualise", "rolling_vol", "canonical_sha256",
    "ensure_finite", "validate_dates",
    "FACTOR_NAMES", "SyntheticMarketConfig", "SyntheticDataset", "synthetic_dataset",
    "business_days",
    "FactorModelConfig", "FactorFit", "FactorModel", "build_factor_returns", "factor_summary",
    "RegimeConfig", "RegimeClassification", "VolatilityRegimeModel", "GaussianMixtureRegimeModel",
    "RateCurve", "FXQuote", "FXBlock", "cip_forward", "cip_residual", "carry_return",
    "unhedged_return", "hedged_return", "usd_return", "real_rate", "term_spread", "fx_cross",
    "WalkForwardConfig", "BacktestResult", "ForecastMetrics", "PortfolioMetrics",
    "walk_forward", "forecast_metrics", "portfolio_metrics", "max_drawdown",
    "top_fraction_weights",
    "run_demo", "__version__", "__status__",
]


def model_cards() -> dict[str, ModelCard]:
    """Passports (level L2 of the platform plan) of every research model in this layer."""
    import numpy as np

    return {
        "factor_model": FactorModel().model_card(),
        "volatility_regime": VolatilityRegimeModel().model_card(),
        "gaussian_mixture_regime": GaussianMixtureRegimeModel().model_card(),
        "fx_rates": FXBlock("EURUSD", np.array([1.0, 1.01]), np.array([0.04, 0.04]),
                            np.array([0.02, 0.02]),
                            ("2026-11-15", "2026-11-16")).model_card(),
    }
