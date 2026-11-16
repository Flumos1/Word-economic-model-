"""Tests for the AMFE 2.0 finance layer (docs/145, docs/149).

The suite is fast and offline: every dataset is synthetic, every model is deterministic and no
network call is possible.  Coverage: data contracts and fail-closed validation, the synthetic DGP,
factor construction and loadings (including the algebraic degeneracy of the mean-premium rule),
no-look-ahead in the regime classifier and in the rolling backtest, FX/rates identities, forecast
and portfolio metrics, the local CSV adapter with its point-in-time guard, and the honesty block of
the demo (losing metrics and limitations are published, not hidden).
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import finance_data as fd  # noqa: E402
from amfe.markets_finance import (  # noqa: E402
    AssetClass, ContractError, FactorModel, FactorModelConfig, GaussianMixtureRegimeModel,
    MarketDataError, ModelCard, ModelCardError, PriceSeries, Provenance, RegimeConfig,
    ReturnSeries, SyntheticMarketConfig, VolatilityRegimeModel, WalkForwardConfig, annualise,
    business_days, canonical_sha256, cip_forward, cip_residual, ensure_finite, forecast_metrics,
    fx_cross, log_returns, max_drawdown, portfolio_metrics, real_rate, run_demo, simple_returns,
    synthetic_dataset, term_spread, top_fraction_weights, unhedged_return, walk_forward,
)
from amfe.markets_finance.data import FACTOR_NAMES  # noqa: E402

ASSET_EQUITY = AssetClass.EQUITY


def provenance(**overrides) -> Provenance:
    params = dict(source_id="SYNTHETIC_MARKET", dataset_id="synthetic/test/EQ01",
                  retrieved_at="2026-11-16", licence="internal synthetic",
                  sha256="a" * 64)
    params.update(overrides)
    return Provenance(**params)


def equity_dataset(periods: int = 160, seed: int = 20261116, extra: int = 0) -> object:
    return synthetic_dataset(SyntheticMarketConfig(seed=seed, n_periods=periods,
                                                   n_equities=12 + extra))


class ContractsTests(unittest.TestCase):
    def test_simple_and_log_returns_are_consistent(self):
        prices = np.array([100.0, 110.0, 99.0, 99.0])
        simple = simple_returns(prices)
        logs = log_returns(prices)
        self.assertEqual(simple.shape, (3,))
        self.assertAlmostEqual(simple[0], 0.10, places=12)
        np.testing.assert_allclose(np.expm1(logs), simple, rtol=0, atol=1e-15)

    def test_price_series_validation_fails_closed(self):
        dates = business_days("2026-01-05", 3)
        prices = np.array([100.0, 101.0, 102.0])
        series = PriceSeries("EQ01", AssetClass.EQUITY, dates, prices, provenance())
        self.assertEqual(series.returns().values.shape, (2,))
        self.assertEqual(series.returns().dates, dates[1:])
        with self.assertRaises(MarketDataError):
            PriceSeries("EQ01", AssetClass.EQUITY, (dates[0], dates[2], dates[1]), prices,
                        provenance())
        with self.assertRaises(MarketDataError):
            PriceSeries("EQ01", AssetClass.EQUITY, dates, np.array([100.0, -1.0, 102.0]),
                        provenance())
        with self.assertRaises(MarketDataError):
            PriceSeries("EQ01", AssetClass.EQUITY, dates, np.array([100.0, np.nan, 102.0]),
                        provenance())
        with self.assertRaises(MarketDataError):
            PriceSeries("EQ01", AssetClass.EQUITY, dates, prices[:2], provenance())
        with self.assertRaises(MarketDataError):
            PriceSeries("EQ01", AssetClass.EQUITY, dates * 2, np.repeat(prices, 2), provenance())
        with self.assertRaises(MarketDataError):
            simple_returns(np.array([100.0, np.inf]))

    def test_return_series_and_annualisation(self):
        dates = ("2026-01-06", "2026-01-07", "2026-01-08")
        series = ReturnSeries("EQ01", AssetClass.EQUITY, dates,
                              np.array([0.01, -0.02, 0.03]), provenance=provenance())
        self.assertAlmostEqual(series.annualised_mean(), 0.02 / 3 * 252, places=12)
        self.assertAlmostEqual(annualise(np.array([0.01, 0.01]), 252, "mean"), 2.52, places=12)
        with self.assertRaises(MarketDataError):
            ReturnSeries("EQ01", AssetClass.EQUITY, dates, np.array([-1.5, 0.0, 0.1]),
                         provenance=provenance())
        with self.assertRaises(ContractError):
            annualise(np.array([0.01]), 252, "median")

    def test_provenance_and_model_card_contracts(self):
        with self.assertRaises(ContractError):
            Provenance("", "d", "2026-11-16", "lic", "a" * 64)
        with self.assertRaises(ContractError):
            Provenance("s", "d", "not-a-date", "lic", "a" * 64)
        with self.assertRaises(ContractError):
            Provenance("s", "d", "2026-11-16", "lic", "short")
        card = FactorModel().model_card()
        self.assertEqual(card.status.value, "RESEARCH_ONLY")
        self.assertTrue(card.limitations)
        self.assertTrue(card.tests)
        self.assertEqual(card.sha256, FactorModel().model_card().sha256)
        with self.assertRaises(ModelCardError):
            ModelCard("m", "0.1", "k", ("in",), ("out",), (), (), ("t",))
        with self.assertRaises(ModelCardError):
            ModelCard("m", "badversion", "k", ("in",), ("out",), (), ("lim",), ("t",))

    def test_canonical_sha256_is_stable(self):
        first = canonical_sha256({"b": 1, "a": [1.5, 2.5]})
        second = canonical_sha256({"a": [1.5, 2.5], "b": 1})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)


class SyntheticDataTests(unittest.TestCase):
    def test_dataset_is_deterministic_and_seed_sensitive(self):
        first = equity_dataset()
        second = equity_dataset()
        self.assertEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.sha256, equity_dataset(seed=7).sha256)
        np.testing.assert_array_equal(first.equities["EQ01"].prices,
                                      second.equities["EQ01"].prices)
        self.assertEqual(first.to_metadata()["digest"], second.to_metadata()["digest"])

    def test_panels_are_aligned_and_finite(self):
        dataset = equity_dataset()
        series, matrix = dataset.returns_matrix(ASSET_EQUITY)
        self.assertEqual(matrix.shape, (159, 12))
        self.assertEqual(len(series["EQ01"].dates), 159)
        self.assertEqual(dataset.characteristics_matrix(ASSET_EQUITY)[1].shape, (160, 12, 5))
        self.assertTrue(np.all(np.isfinite(matrix)))
        self.assertTrue(all(series[name].provenance.data_class.value == "SYNTHETIC"
                            for name in series))
        with self.assertRaises(MarketDataError):
            dataset.rate("UNKNOWN")

    def test_bad_configuration_is_rejected(self):
        with self.assertRaises(MarketDataError):
            SyntheticMarketConfig(n_equities=3)
        with self.assertRaises(MarketDataError):
            SyntheticMarketConfig(n_periods=10)
        with self.assertRaises(MarketDataError):
            SyntheticMarketConfig(characteristic_phi=1.5)


class FactorModelTests(unittest.TestCase):
    def test_factor_construction_uses_only_current_characteristics(self):
        dataset = equity_dataset()
        _series, returns = dataset.returns_matrix(ASSET_EQUITY)
        _names, characteristics = dataset.characteristics_matrix(ASSET_EQUITY)
        model = FactorModel(FactorModelConfig(estimation_window=60))
        factors = model.factors(returns, characteristics[:-1])
        self.assertEqual(factors.shape, (returns.shape[0], len(FACTOR_NAMES)))
        mutated = characteristics.copy()
        mutated[1:, :, :] = 123.0  # only the future is changed
        again = model.factors(returns, mutated[:-1])
        np.testing.assert_allclose(again[0], factors[0], rtol=0, atol=0)  # row 0 unchanged
        self.assertFalse(np.allclose(again[-1], factors[-1]))

    def test_loadings_recover_the_beta_order(self):
        rng = np.random.default_rng(11)
        periods, assets = 200, 9
        betas = np.linspace(0.5, 1.5, assets)
        common = rng.normal(0.0, 0.01, size=periods)
        returns = common[:, None] * betas[None, :] + rng.normal(0.0, 0.001, (periods, assets))
        chars = np.zeros((periods, assets, 5))
        chars[:, :, 0] = betas[None, :]
        model = FactorModel(FactorModelConfig(estimation_window=120))
        fit = model.fit(returns[-120:], chars[-120:])
        order = np.argsort(fit.loadings[:, 0])
        self.assertGreater(float(np.corrcoef(order, np.argsort(betas))[0, 1]), 0.95)

    def test_mean_premium_rule_equals_the_historical_mean(self):
        dataset = equity_dataset()
        _series, returns = dataset.returns_matrix(ASSET_EQUITY)
        _names, characteristics = dataset.characteristics_matrix(ASSET_EQUITY)
        model = FactorModel(FactorModelConfig(estimation_window=80, premium_rule="mean"))
        chars_for_returns = characteristics[:-1]
        expected, _fit = model.forecast(returns[-80:], chars_for_returns[-80:],
                                        symbols=tuple(sorted(dataset.equities)))
        # the identity: with an OLS intercept and an in-window mean premium the forecast IS the mean
        self.assertTrue(np.allclose(expected, returns[-80:].mean(axis=0), atol=1e-12))

    def test_premium_history_changes_the_forecast(self):
        dataset = equity_dataset()
        _series, returns = dataset.returns_matrix(ASSET_EQUITY)
        _names, characteristics = dataset.characteristics_matrix(ASSET_EQUITY)
        model = FactorModel(FactorModelConfig(estimation_window=80))
        chars_for_returns = characteristics[:-1]
        in_window, _ = model.forecast(returns[-80:], chars_for_returns[-80:])
        long_history = model.factors(returns, chars_for_returns)[-200:]
        longer, _ = model.forecast(returns[-80:], chars_for_returns[-80:],
                                   premium_returns=long_history)
        self.assertFalse(np.allclose(in_window, longer))

    def test_configuration_and_window_validation(self):
        with self.assertRaises(MarketDataError):
            FactorModelConfig(quantile=0.6)
        with self.assertRaises(MarketDataError):
            FactorModelConfig(estimation_window=5)
        with self.assertRaises(MarketDataError):
            FactorModelConfig(factor_names=("MARKET", "MARKET"))
        with self.assertRaises(MarketDataError):
            FactorModelConfig(premium_rule="best")
        model = FactorModel(FactorModelConfig(estimation_window=100))
        with self.assertRaises(MarketDataError):
            model.fit(np.zeros((40, 6)), np.zeros((40, 6, 5)))


class RegimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset = equity_dataset()
        _series, returns = dataset.returns_matrix(ASSET_EQUITY)
        cls.market = returns.mean(axis=1)

    def test_quantile_classifier_orders_regimes_and_shares_sum_to_one(self):
        model = VolatilityRegimeModel(RegimeConfig(window=30)).fit(self.market[:120])
        classification = model.classify(self.market)
        self.assertEqual(classification.warmup, 29)
        self.assertAlmostEqual(sum(classification.shares().values()), 1.0, places=12)
        means = {}
        for label in ("LOW", "NORMAL", "HIGH"):
            values = classification.volatility[[item == label for item in classification.labels]]
            self.assertTrue(np.all(np.isfinite(values)))
            means[label] = float(np.mean(values))
        self.assertLess(means["LOW"], means["HIGH"])
        stats = model.stats(classification)
        self.assertIn("counts", stats)

    def test_thresholds_are_frozen_after_fit(self):
        model = VolatilityRegimeModel(RegimeConfig(window=30)).fit(self.market[:120])
        first = model.classify(self.market[:120])
        extended = model.classify(self.market)
        self.assertEqual(first.labels, extended.labels[:120])
        with self.assertRaises(MarketDataError):
            VolatilityRegimeModel(RegimeConfig(window=30)).classify(self.market)

    def test_gaussian_mixture_is_deterministic_and_ordered(self):
        first = GaussianMixtureRegimeModel(n_states=2).fit(self.market[:120])
        second = GaussianMixtureRegimeModel(n_states=2).fit(self.market[:120])
        np.testing.assert_array_equal(first.means, second.means)
        np.testing.assert_array_equal(first.weights, second.weights)
        self.assertTrue(np.all(np.diff(np.sort(np.sqrt(first.variances))) >= 0))
        history = np.asarray(first.log_likelihood_history)
        self.assertTrue(np.all(np.diff(history) >= -1e-9))  # EM log-likelihood never decreases
        self.assertEqual(first.classify(self.market[:20]).shape, (20,))
        stats = first.stats(self.market[:120])
        transition = np.asarray(stats["transition"])
        np.testing.assert_allclose(transition.sum(axis=1), np.ones(2), atol=1e-12)
        self.assertIn("expected_durations", stats)

    def test_regime_validation(self):
        with self.assertRaises(MarketDataError):
            GaussianMixtureRegimeModel(n_states=4)
        with self.assertRaises(MarketDataError):
            GaussianMixtureRegimeModel(variance_floor=1.2)
        with self.assertRaises(MarketDataError):
            GaussianMixtureRegimeModel().fit(self.market[:5])


class FXRatesTests(unittest.TestCase):
    def test_covered_interest_parity_identity(self):
        spot, r_quote, r_base, days = 1.10, 0.043, 0.020, 90
        forward = cip_forward(spot, r_quote, r_base, days)
        self.assertAlmostEqual(cip_residual(spot, forward, r_quote, r_base, days), 0.0, places=14)
        self.assertGreater(forward, spot)  # quote rate above base rate -> forward premium
        self.assertAlmostEqual(fx_cross(1.10, 1.25), 1.10 / 1.25, places=14)
        with self.assertRaises(MarketDataError):
            cip_forward(-1.0, 0.04, 0.02, 90)
        with self.assertRaises(MarketDataError):
            cip_forward(1.10, 0.04, 0.02, 0)

    def test_conversion_and_rate_identities(self):
        local, fx = 0.05, 0.02
        self.assertAlmostEqual(unhedged_return(local, fx), 1.05 * 1.02 - 1.0, places=14)
        self.assertAlmostEqual(real_rate(0.043, 0.02), 1.043 / 1.02 - 1.0, places=14)
        self.assertAlmostEqual(term_spread(0.043, 0.040), 0.003, places=14)
        with self.assertRaises(MarketDataError):
            unhedged_return(-1.2, 0.0)

    def test_rate_curve_and_cip_block(self):
        from amfe.markets_finance import FXBlock, RateCurve
        curve = RateCurve("2026-11-16", (90, 730, 3650), (0.040, 0.043, 0.042))
        self.assertAlmostEqual(curve.rate_at(90), 0.040, places=12)
        self.assertAlmostEqual(curve.rate_at(20_000), 0.042, places=12)  # flat extrapolation
        self.assertLess(curve.discount_factor(3650), curve.discount_factor(90))
        self.assertTrue(np.isfinite(curve.forward_rate(90, 3650)))
        with self.assertRaises(MarketDataError):
            RateCurve("2026-11-16", (730, 90), (0.04, 0.05))
        dataset = equity_dataset(periods=80)
        rates = dataset.rate("DGS3M")
        block = FXBlock("EURUSD", dataset.fx["EURUSD"].prices, rates,
                        np.full(len(rates), 0.02), dataset.dates)
        residuals = block.cip_residuals(90)
        self.assertLess(float(np.max(np.abs(residuals))), 1e-12)
        self.assertEqual(block.log_returns().shape, (79,))
        self.assertTrue(np.isfinite(block.carry(90)))
        self.assertEqual(block.model_card().status.value, "RESEARCH_ONLY")


class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset = equity_dataset(periods=160)
        series, returns = dataset.returns_matrix(ASSET_EQUITY)
        _names, characteristics = dataset.characteristics_matrix(ASSET_EQUITY)
        cls.series = series
        cls.returns = returns
        cls.chars = characteristics[:-1]
        cls.dates = series["EQ01"].dates
        cls.names = tuple(sorted(series))
        cls.config = WalkForwardConfig(estimation_window=60)

    def run_backtest(self, returns=None, chars=None, config=None):
        return walk_forward(self.returns if returns is None else returns,
                            self.chars if chars is None else chars,
                            self.dates, self.names, config or self.config)

    def test_alignment_determinism_and_report_shape(self):
        first = self.run_backtest().to_dict()
        second = self.run_backtest().to_dict()
        self.assertEqual(first["diagnostics"]["predictions_sha256"],
                         second["diagnostics"]["predictions_sha256"])
        self.assertEqual(first["oos_periods"], len(self.dates) - 60)
        self.assertEqual(first["first_oos_date"], self.dates[60])
        self.assertEqual(first["last_oos_date"], self.dates[-1])
        for block in ("forecast_metrics", "portfolio_metrics", "verdict", "limitations"):
            self.assertIn(block, first)
        self.assertEqual(set(first["forecast_metrics"]),
                         {"factor_model", "naive_zero", "naive_mean"})

    def test_no_look_ahead_when_the_future_is_mutated(self):
        base = self.run_backtest()
        horizon = 60 + 30  # keep 30 OOS predictions untouched
        mutated_returns = self.returns.copy()
        mutated_returns[horizon:] = np.random.default_rng(3).normal(0.0, 0.5,
                                                                   mutated_returns[horizon:].shape)
        mutated_chars = self.chars.copy()
        mutated_chars[horizon:] = 99.0
        mutated = self.run_backtest(mutated_returns, mutated_chars)
        np.testing.assert_allclose(mutated.predictions[:30], base.predictions[:30],
                                   rtol=0, atol=0)
        self.assertFalse(np.allclose(mutated.predictions[-1], base.predictions[-1]))

    def test_forecast_metrics_on_known_values(self):
        predictions = np.zeros((2, 2))
        realized = np.array([[0.1, -0.1], [0.2, -0.2]])
        metrics = forecast_metrics(predictions, realized)
        self.assertAlmostEqual(metrics.rmse, np.sqrt((0.01 + 0.01 + 0.04 + 0.04) / 4), places=12)
        self.assertAlmostEqual(metrics.mae, 0.15, places=12)
        self.assertAlmostEqual(metrics.bias, -0.0, places=12)
        with self.assertRaises(MarketDataError):
            forecast_metrics(np.zeros((2, 3)), realized)

    def test_portfolio_metrics_and_drawdown(self):
        series = np.array([0.10, -0.20, 0.05])
        self.assertAlmostEqual(max_drawdown(series), 0.20, places=12)
        metrics = portfolio_metrics(np.full(24, 0.001), periods_per_year=12)
        self.assertAlmostEqual(metrics.annualised_mean, 0.012, places=12)
        self.assertEqual(metrics.observations, 24)
        with self.assertRaises(MarketDataError):
            portfolio_metrics(np.array([0.01]))

    def test_top_fraction_weights(self):
        weights = top_fraction_weights(np.array([0.01, 0.05, -0.02, 0.03, 0.0]), 0.4)
        self.assertAlmostEqual(float(weights.sum()), 1.0, places=12)
        self.assertEqual(int(np.count_nonzero(weights)), 2)
        self.assertGreater(weights[1], 0.0)  # the highest forecast is held
        with self.assertRaises(MarketDataError):
            top_fraction_weights(np.zeros(5), 1.5)

    def test_verdict_publishes_the_losing_comparisons(self):
        report = self.run_backtest().to_dict()
        verdict = report["verdict"]
        self.assertIn("factor_model_beats_random_walk_rmse", verdict)
        self.assertIn("factor_model_beats_historical_mean_rmse", verdict)
        self.assertIn("where_not_better", verdict)
        self.assertIn("not a trading strategy", verdict["statement"])
        metrics = report["forecast_metrics"]
        self.assertTrue(metrics["factor_model"]["rmse"] > 0.0)
        self.assertTrue(all("rmse" in metrics[name] for name in metrics))
        with self.assertRaises(MarketDataError):
            self.run_backtest(config=WalkForwardConfig(estimation_window=10_000))


class DataToolTests(unittest.TestCase):
    def test_sources_are_specification_only(self):
        self.assertFalse(fd.NETWORK_ALLOWED)
        sources = fd.list_sources()
        self.assertEqual({item["source_id"] for item in sources}, {"yahoo", "fred", "coingecko"})
        for item in sources:
            self.assertTrue(item["licence"])
            self.assertTrue(item["pit_caveats"])
            self.assertFalse(item["network_allowed"])
        schema = fd.schema_document()
        self.assertIn("point_in_time", schema["rules"])
        with self.assertRaises(fd.DatasetError):
            fd.describe("nasdaq")

    def test_templates_roundtrip_and_pit_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fd.write_templates(tmp)
            self.assertEqual(len(paths), 3)
            yahoo = fd.load_local_csv(paths[0], "yahoo", as_of="2026-11-12")
            self.assertEqual(yahoo.rows, 3)
            self.assertEqual(yahoo.value_column, "close")
            self.assertEqual(len(yahoo.sha256), 64)
            self.assertEqual(yahoo.to_dict()["provenance"]["network_allowed"], False)
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(paths[0], "yahoo", as_of="2026-11-11")  # PIT guard
            fred = fd.load_local_csv(paths[1], "fred", as_of="2026-11-12")
            self.assertEqual(fred.rows, 3)
            crypto = fd.load_local_csv(paths[2], "coingecko", as_of="2026-11-12")
            self.assertEqual(crypto.value_column, "price_usd")

    def test_csv_adapter_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            broken = base / "broken.csv"
            broken.write_text("date,close\n2026-11-12,10\n2026-11-11,11\n", encoding="utf-8")
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(broken, "yahoo")
            negative = base / "negative.csv"
            negative.write_text("date,close\n2026-11-10,-1\n2026-11-11,2\n", encoding="utf-8")
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(negative, "yahoo")
            gap = base / "gap.csv"
            gap.write_text("date,close\n2026-01-01,10\n2026-03-01,11\n2026-03-02,12\n",
                           encoding="utf-8")
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(gap, "yahoo")
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(base / "missing.csv", "yahoo")
            with self.assertRaises(fd.DatasetError):
                fd.load_local_csv(broken, "nasdaq")

    def test_self_test_and_cli(self):
        result = fd.self_test()
        self.assertTrue(result["ok"], result["checks"])
        self.assertEqual(fd.self_test()["sha256"], result["sha256"])
        listing = subprocess.run([sys.executable, str(ROOT / "tools" / "finance_data.py"),
                                  "--list", "--json"], capture_output=True, text=True,
                                 timeout=120)
        self.assertEqual(listing.returncode, 0)
        self.assertEqual(len(json.loads(listing.stdout)["sources"]), 3)
        schema = subprocess.run([sys.executable, str(ROOT / "tools" / "finance_data.py"),
                                 "--schema", "--json"], capture_output=True, text=True, timeout=120)
        self.assertEqual(schema.returncode, 0)
        self.assertIn("licences", json.loads(schema.stdout))
        failure = subprocess.run([sys.executable, str(ROOT / "tools" / "finance_data.py"),
                                  "--load", str(ROOT / "no-such-file.csv"), "--json"],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(failure.returncode, 2)
        self.assertIn("BLOCKED_DATA", failure.stderr)
        help_done = subprocess.run([sys.executable, str(ROOT / "tools" / "finance_data.py"),
                                    "--help"], capture_output=True, text=True, timeout=120)
        self.assertEqual(help_done.returncode, 0)


class DemoAndComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = run_demo(seed=20261116)
        cls.second = run_demo(seed=20261116)

    def test_demo_is_deterministic(self):
        self.assertEqual(self.first["pipeline_sha256"], self.second["pipeline_sha256"])
        self.assertEqual(json.dumps(self.first, sort_keys=True, ensure_ascii=False),
                         json.dumps(self.second, sort_keys=True, ensure_ascii=False))
        self.assertNotEqual(run_demo(seed=7)["pipeline_sha256"], self.first["pipeline_sha256"])

    def test_demo_publishes_losing_metrics_and_limitations(self):
        oos = self.first["oos"]
        self.assertEqual(set(oos), {"equities", "crypto"})
        for label, payload in oos.items():
            verdict = payload["oos"]["verdict"]
            self.assertIn("where_not_better", verdict)
            self.assertTrue(verdict["where_not_better"], f"{label} hides its losing metrics")
            self.assertIn("not a trading strategy", verdict["statement"])
        limitations = " ".join(self.first["limitations"]).lower()
        for token in ("transaction costs", "synthetic", "not investment advice", "not an admission"):
            self.assertIn(token, limitations)
        self.assertIn("not a trading strategy", self.first["verdict"]["statement"])

    def test_all_model_cards_are_research_only(self):
        from amfe.markets_finance import model_cards
        cards = model_cards()
        self.assertEqual(set(cards), {"factor_model", "volatility_regime",
                                      "gaussian_mixture_regime", "fx_rates"})
        for card in cards.values():
            self.assertEqual(card.status.value, "RESEARCH_ONLY")
            self.assertTrue(card.limitations)
            self.assertRegex(card.sha256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
