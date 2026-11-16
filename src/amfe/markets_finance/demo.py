"""Reproducible end-to-end demo of the finance prototype (docs/149).

``python -m amfe.markets_finance.demo --json`` prints one JSON document with every number cited in
docs/149: the synthetic-data digest, the factor fit, the volatility regimes, the FX/rates identities
and the rolling OOS comparison against the naive benchmarks.  No network, no clock, no files: the
same ``--seed`` always prints byte-identical output, which is what makes the documented numbers
checkable.

The demo is a **research diagnostic**.  It does not recommend a trade, does not claim that the factor
model works on real markets and does not assign any admission status.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from .backtest import WalkForwardConfig, walk_forward
from .contracts import AssetClass, canonical_sha256
from .data import FACTOR_NAMES, SyntheticMarketConfig, synthetic_dataset
from .factor_model import FactorModel, FactorModelConfig, factor_summary
from .fx_rates import FXBlock, RateCurve, cip_forward, cip_residual, real_rate, term_spread
from .regimes import GaussianMixtureRegimeModel, RegimeConfig, VolatilityRegimeModel

__all__ = ["run_demo", "main"]

_LIMITATIONS = (
    "synthetic data with a known factor structure: this validates the machinery, not the market",
    "no transaction costs, borrow, liquidity or capacity anywhere in the comparison",
    "long-only top-tercile mapping is one arbitrary way to turn forecasts into weights",
    "no point-in-time fundamentals for value/quality; characteristics here are synthetic AR(1)",
    "crypto and FX blocks are separate research panels, not integrated into the equity backtest",
    "no uncertainty quantification on the reported OOS metrics (single history, no bootstrap)",
    "not an admission, not a forecast product, not investment advice",
)


def _asset_panel(dataset, asset_class, window: int, min_assets: int = 6) -> dict:
    """Roll one OOS backtest for an asset class on the synthetic panel."""
    series, returns = dataset.returns_matrix(asset_class)
    names = tuple(sorted(series))
    _char_names, characteristics = dataset.characteristics_matrix(asset_class)
    dates = series[names[0]].dates
    # characteristics[i] is known at the close that opens return i -> drop the last calendar row
    characteristics_for_returns = characteristics[:-1]
    result = walk_forward(returns, characteristics_for_returns, dates, names,
                          WalkForwardConfig(estimation_window=window,
                                            min_assets=min_assets))
    payload = result.to_dict()
    return {"asset_class": asset_class.value, "symbols": list(names),
            "observations": int(returns.shape[0]), "oos": payload}


def run_demo(seed: int = 20261116, window: int = 120) -> dict:
    """Compute the full demo document (deterministic in ``seed`` and ``window``)."""
    config = SyntheticMarketConfig(seed=seed, n_periods=320)
    dataset = synthetic_dataset(config)
    equities = _asset_panel(dataset, AssetClass.EQUITY, window)
    crypto = _asset_panel(dataset, AssetClass.CRYPTO, window, min_assets=4)

    # factor fit and factor summary on the equity panel
    series, returns = dataset.returns_matrix(
        AssetClass.EQUITY)
    names = tuple(sorted(series))
    _char_names, characteristics = dataset.characteristics_matrix(
        AssetClass.EQUITY)
    model = FactorModel(FactorModelConfig(estimation_window=window))
    factors = model.factors(returns, characteristics[:-1])
    fit = model.fit(returns[:window], characteristics[:window])
    summary = factor_summary(factors)
    premia = model.premia(factors[:window])

    # volatility regimes on the equal-weight equity market return
    market = returns.mean(axis=1)
    regime_model = VolatilityRegimeModel(RegimeConfig(window=60)).fit(market[:200])
    classification = regime_model.classify(market)
    regime_stats = regime_model.stats(classification, factor_returns=factors)
    # EM on this panel converges slowly because the second state shrinks towards the weight floor;
    # the demo configures the iteration budget explicitly so the documented numbers are converged
    gmm = GaussianMixtureRegimeModel(n_states=2, max_iter=1500, tolerance=1e-6).fit(market[:200])
    gmm_stats = gmm.stats(market)
    gmm_stats["note"] = ("EM converged on the 200-observation training window; the second state "
                         "sits near the weight floor, so its interpretation is weak")

    # FX / rates block: identities on explicit rates, then the synthetic EURUSD panel
    curve = RateCurve("2026-11-16", (90, 730, 3650), (0.040, 0.043, 0.042))
    eurusd_spot = float(dataset.fx["EURUSD"].prices[-1])
    usd_rate, eur_rate = 0.040, 0.020
    forward = cip_forward(eurusd_spot, usd_rate, eur_rate, 90)
    fx_block = FXBlock("EURUSD", dataset.fx["EURUSD"].prices,
                       dataset.rate("DGS3M"), np.full(len(dataset.dates), eur_rate),
                       dataset.dates)
    fx = {
        "spot": eurusd_spot,
        "usd_rate": usd_rate,
        "eur_rate_placeholder": eur_rate,
        "cip_forward_90d": forward,
        "cip_residual_of_implied_forward": cip_residual(eurusd_spot, forward, usd_rate, eur_rate, 90),
        "carry_90d": fx_block.carry(90),
        "term_spread_10y_3m": term_spread(curve.rate_at(3650), curve.rate_at(90)),
        "real_rate_10y_inflation_2pct": real_rate(curve.rate_at(3650), 0.02),
        "note": ("EUR rate 2% is a documented placeholder: the synthetic panel carries USD rates "
                 "only; the block is an identity/diagnostic, not an FX model"),
    }

    document = {
        "schema": "AMFE-FINANCE-DEMO/1.0.0",
        "seed": seed,
        "estimation_window": window,
        "dataset": dataset.to_metadata(),
        "dataset_sha256": dataset.sha256,
        "factors": {"names": list(FACTOR_NAMES), "summary": summary,
                    "premia_last_window": premia.round(10).tolist(),
                    "fit_r2": fit.r2, "fit_observations": fit.observations,
                    "loadings_market": fit.loadings[:, 0].round(6).tolist()},
        "regimes": {"quantile": {"stats": regime_stats,
                                 "thresholds": list(classification.thresholds)},
                    "gaussian_mixture": gmm_stats},
        "fx_rates": fx,
        "oos": {"equities": equities, "crypto": crypto},
        "verdict": {
            "equities": equities["oos"]["verdict"],
            "crypto": crypto["oos"]["verdict"],
            "statement": ("research prototype: the OOS comparison is published with the losing "
                          "metrics; not a trading strategy, not investment advice, not an "
                          "admission"),
        },
        "limitations": list(_LIMITATIONS),
    }
    document["pipeline_sha256"] = canonical_sha256(
        {key: value for key, value in document.items() if key != "pipeline_sha256"})
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reproducible demo of the AMFE 2.0 finance prototype (docs/149)")
    parser.add_argument("--seed", type=int, default=20261116, help="synthetic data seed")
    parser.add_argument("--window", type=int, default=120, help="OOS estimation window")
    parser.add_argument("--json", action="store_true", help="print the full JSON document")
    parser.add_argument("--out", default=None, help="write the JSON document to this path")
    args = parser.parse_args(argv)
    document = run_demo(seed=args.seed, window=args.window)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(payload, encoding="utf-8", newline="\n")
    if args.json:
        print(payload)
    else:
        summary = {"schema": document["schema"], "seed": document["seed"],
                   "dataset_sha256": document["dataset_sha256"],
                   "pipeline_sha256": document["pipeline_sha256"],
                   "equities": document["verdict"]["equities"],
                   "crypto": document["verdict"]["crypto"],
                   "factor_summary": document["factors"]["summary"],
                   "regimes": {"quantile": document["regimes"]["quantile"]["stats"]["shares"],
                               "gmm": document["regimes"]["gaussian_mixture"]["states"]},
                   "fx": document["fx_rates"]}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
