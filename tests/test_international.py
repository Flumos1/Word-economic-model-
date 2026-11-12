"""Tests for the stage-3 two-country/two-currency external sector.

The test suite checks accounting identities, mirroring, FX revaluation separated
from flows, observation discrepancies, stress scenarios, edge cases and that the
existing single-country/international prototypes still import and run.
"""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.international import (  # noqa: E402
    BalanceError,
    Country,
    FXError,
    FXMarket,
    InternationalError,
    RecordingHook,
    TwoCountryWorld,
)
from amfe.international.model import TRADE_CATEGORIES  # noqa: E402


def world(rate_b=Decimal(2), hook=None, initial=True):
    a = Country("A", "AAA")
    b = Country("B", "BBB")
    w = TwoCountryWorld(a, b, FXMarket("AAA", {"AAA": Decimal(1), "BBB": rate_b}), hook=hook)
    if initial:
        w.add_initial_claim("A", "B", "bond", "AAA", Decimal(30))
        w.add_initial_claim("B", "A", "bond", "BBB", Decimal(10))
    return w


class FXMarketTests(unittest.TestCase):
    def test_conversion_and_inverse(self):
        fx = FXMarket("AAA", {"AAA": 1, "BBB": 2})
        self.assertEqual(fx.convert(10, "BBB", "AAA"), Decimal(20))
        self.assertEqual(fx.convert(20, "AAA", "BBB"), Decimal(10))
        self.assertEqual(fx.numeraire_value(5, "BBB"), Decimal(10))
        self.assertEqual(fx.local_value(4, "BBB", "AAA"), Decimal(8))
        self.assertEqual(fx.local_value(4, "AAA", "BBB"), Decimal(2))

    def test_invalid_rates_and_currencies(self):
        with self.assertRaises(FXError):
            FXMarket("AAA", {"AAA": 1, "BBB": 0})
        with self.assertRaises(FXError):
            FXMarket("AAA", {"AAA": 1, "BBB": -2})
        with self.assertRaises(FXError):
            FXMarket("AAA", {"AAA": 1})
        fx = FXMarket("AAA", {"AAA": 1, "BBB": 2})
        with self.assertRaises(FXError):
            fx.rate("CCC")
        with self.assertRaises(FXError):
            fx.convert(1, "AAA", "CCC")

    def test_with_rates_keeps_a_valid_single_numeraire(self):
        fx = FXMarket("AAA", {"AAA": 1, "BBB": 2})
        devalued = fx.with_rates({"BBB": 1})
        self.assertEqual(devalued.rate("AAA"), Decimal(1))
        self.assertEqual(devalued.rate("BBB"), Decimal(1))
        self.assertEqual(devalued.numeraire, "AAA")


class CountryAndValidationTests(unittest.TestCase):
    def test_two_distinct_countries_and_currencies(self):
        a = Country("A", "AAA")
        b = Country("B", "BBB")
        w = TwoCountryWorld(a, b, FXMarket("AAA", {"AAA": 1, "BBB": 2}))
        self.assertEqual(w.country("A").currency, "AAA")
        with self.assertRaises(InternationalError):
            TwoCountryWorld(Country("A", "AAA"), Country("A", "BBB"), w.fx)
        with self.assertRaises(InternationalError):
            TwoCountryWorld(Country("A", "AAA"), Country("B", "AAA"), w.fx)
        with self.assertRaises(InternationalError):
            w.country("C")

    def test_zero_trade_is_a_noop(self):
        w = world()
        before = (w.claims, len(w.journal), len(w._flows))
        w.record_trade("A", "B", 0, "AAA")
        report = w.close_period()
        self.assertEqual(w.claims, before[0])
        self.assertEqual(len(w._flows), 0)
        self.assertTrue(report["checks"]["ok"])
        self.assertEqual(report["countries"]["A"]["current_account_total"], Decimal(0))
        self.assertEqual(report["countries"]["A"]["financial_account"], Decimal(0))

    def test_invalid_inputs_are_atomic(self):
        w = world()
        before = (dict(w._claims), len(w.journal), len(w._flows))
        bad_calls = [
            lambda: w.record_trade("A", "A", 1, "AAA"),
            lambda: w.record_trade("A", "C", 1, "AAA"),
            lambda: w.record_trade("A", "B", -1, "AAA"),
            lambda: w.record_trade("A", "B", 1, "CCC"),
            lambda: w.record_trade("A", "B", 1, "AAA", category="unknown"),
            lambda: w.record_income("A", "B", -2, "AAA"),
            lambda: w.record_financial_transaction("A", "B", 1, "BBB", direction="wrong"),
            lambda: w.record_financial_transaction("A", "B", 1000, "BBB", direction="redeem"),
        ]
        for call in bad_calls:
            with self.assertRaises(InternationalError):
                call()
            self.assertEqual((dict(w._claims), len(w.journal), len(w._flows)), before)


class TradeTests(unittest.TestCase):
    def test_trade_mirrors_current_and_financial_accounts(self):
        w = world(initial=False)
        w.record_trade("A", "B", 10, "AAA")
        report = w.close_period()
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account_total"], Decimal(10))
        self.assertEqual(b["current_account_total"], Decimal(-10))
        self.assertEqual(a["financial_account"], Decimal(10))
        self.assertEqual(b["financial_account"], Decimal(-10))
        self.assertEqual(a["iip"]["net"], Decimal(10))
        self.assertEqual(b["iip"]["net"], Decimal(-10))
        self.assertEqual(report["mirror"]["current_account"], Decimal(0))
        self.assertEqual(report["mirror"]["financial_account"], Decimal(0))
        self.assertEqual(report["mirror"]["iip"], Decimal(0))
        self.assertTrue(report["checks"]["bop_identity"])
        self.assertTrue(report["checks"]["iip_identity"])

    def test_services_trade_mirrors_and_is_reported_separately(self):
        w = world(initial=False)
        w.record_trade("B", "A", 6, "BBB", category="services")
        report = w.close_period()
        self.assertEqual(report["countries"]["B"]["current_account"]["services"], Decimal(12))
        self.assertEqual(report["countries"]["A"]["current_account"]["services"], Decimal(-12))
        self.assertEqual(report["countries"]["A"]["current_account"].get("goods", Decimal(0)), Decimal(0))
        self.assertTrue(report["checks"]["ok"])

    def test_quantity_times_price_wrapper(self):
        w = world(initial=False)
        w.record_trade_quantity("A", "B", 4, 5, "AAA")
        report = w.close_period()
        self.assertEqual(report["countries"]["A"]["current_account"]["goods"], Decimal(20))

    def test_income_primary_and_secondary_mirror(self):
        w = world(initial=False)
        w.record_income("A", "B", 5, "BBB", category="primary_income")
        w.record_income("B", "A", 3, "AAA", category="secondary_income")
        report = w.close_period()
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account"]["primary_income"], Decimal(10))
        self.assertEqual(b["current_account"]["primary_income"], Decimal(-10))
        self.assertEqual(a["current_account"]["secondary_income"], Decimal(-3))
        self.assertEqual(b["current_account"]["secondary_income"], Decimal(3))
        self.assertTrue(report["checks"]["current_account_mirror"])
        self.assertTrue(report["checks"]["financial_account_mirror"])


class CapitalAndFinancialTests(unittest.TestCase):
    def test_capital_transfer_mirrors_capital_account(self):
        w = world(initial=False)
        w.record_capital_transfer("A", "B", 4, "AAA")
        report = w.close_period()
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account_total"], Decimal(0))
        self.assertEqual(a["capital_account_total"], Decimal(-4))
        self.assertEqual(b["capital_account_total"], Decimal(4))
        self.assertEqual(a["financial_account"], Decimal(-4))
        self.assertEqual(b["financial_account"], Decimal(4))
        self.assertEqual(a["iip"]["net"], Decimal(-4))
        self.assertEqual(b["iip"]["net"], Decimal(4))
        self.assertTrue(report["checks"]["ok"])

    def test_financial_portfolio_shift_keeps_net_financial_account_zero(self):
        w = world(initial=False)
        w.record_financial_transaction("A", "B", 5, "BBB", instrument="bond", direction="acquire")
        report = w.close_period()
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account_total"], Decimal(0))
        self.assertEqual(a["capital_account_total"], Decimal(0))
        # Gross claims change, but a portfolio shift is not net lending.
        self.assertEqual(a["financial_account"], Decimal(0))
        self.assertEqual(b["financial_account"], Decimal(0))
        self.assertEqual(a["iip"]["net"], Decimal(0))
        self.assertEqual(a["iip"]["assets"], Decimal(10))
        self.assertEqual(a["iip"]["liabilities"], Decimal(10))
        self.assertTrue(report["checks"]["ok"])

    def test_redeem_reduces_gross_stock(self):
        w = world(initial=False)
        w.record_financial_transaction("A", "B", 5, "BBB", instrument="bond", direction="acquire")
        w.close_period()
        w.record_financial_transaction("A", "B", 2, "BBB", instrument="bond", direction="redeem")
        report = w.close_period()
        self.assertEqual(report["countries"]["A"]["financial_account"], Decimal(0))
        self.assertEqual(report["countries"]["B"]["financial_account"], Decimal(0))
        self.assertEqual(report["countries"]["A"]["iip"]["net"], Decimal(0))
        # bond 5 - 2 = 3 BBB; settlement 10 - 2*2 = 6 AAA
        self.assertEqual(w.claims[("A", "B", "bond", "BBB")], Decimal(3))
        self.assertEqual(w.claims[("B", "A", "deposit", "AAA")], Decimal(6))
        self.assertTrue(report["checks"]["ok"])

    def test_redeem_above_outstanding_is_rejected(self):
        w = world(initial=False)
        w.record_financial_transaction("A", "B", 1, "AAA", instrument="bond")
        with self.assertRaises(BalanceError):
            w.record_financial_transaction("A", "B", 2, "AAA", instrument="bond", direction="redeem")


class FXRevaluationTests(unittest.TestCase):
    def test_revaluation_is_separate_from_transactions(self):
        w = TwoCountryWorld(
            Country("A", "AAA"), Country("B", "BBB"), FXMarket("AAA", {"AAA": 1, "BBB": 2})
        )
        w.add_initial_claim("A", "B", "bond", "BBB", 100)
        report = w.close_period(FXMarket("AAA", {"AAA": 1, "BBB": 1}))
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account_total"], Decimal(0))
        self.assertEqual(a["financial_account"], Decimal(0))
        self.assertEqual(a["iip"]["opening_net"], Decimal(200))
        self.assertEqual(a["iip"]["net"], Decimal(100))
        self.assertEqual(a["iip"]["revaluation"], Decimal(-100))
        self.assertEqual(a["iip"]["transactions"], Decimal(0))
        self.assertEqual(b["iip"]["revaluation"], Decimal(100))
        self.assertEqual(report["mirror"]["iip"], Decimal(0))
        self.assertTrue(report["checks"]["iip_identity"])
        self.assertTrue(report["checks"]["bop_identity"])

    def test_local_revaluation_respects_currency_of_denomination(self):
        w = TwoCountryWorld(
            Country("A", "AAA"), Country("B", "BBB"), FXMarket("AAA", {"AAA": 1, "BBB": 2})
        )
        w.add_initial_claim("A", "B", "bond", "BBB", 100)
        report = w.close_period(FXMarket("AAA", {"AAA": 1, "BBB": 1}))
        # A holds a foreign-currency asset: its local value changes. B owes its
        # own currency: its local value does not change, while its numeraire IIP does.
        self.assertEqual(report["countries"]["A"]["iip"]["revaluation_local"], Decimal(-100))
        self.assertEqual(report["countries"]["B"]["iip"]["revaluation_local"], Decimal(0))
        self.assertEqual(report["countries"]["B"]["iip"]["revaluation"], Decimal(100))

    def test_devaluation_stress_preserves_mirrors(self):
        w = world(initial=False)
        w.record_trade("A", "B", 100, "BBB")
        report = w.close_period(FXMarket("AAA", {"AAA": 1, "BBB": 1}))
        self.assertEqual(report["mirror"]["current_account"], Decimal(0))
        self.assertEqual(report["mirror"]["iip"], Decimal(0))
        self.assertTrue(report["checks"]["ok"])
        self.assertNotEqual(report["countries"]["A"]["iip"]["revaluation"], Decimal(0))

    def test_import_shock_keeps_accounting_consistent(self):
        w = world(initial=False)
        w.record_trade("B", "A", 1000, "BBB")
        report = w.close_period()
        a = report["countries"]["A"]
        b = report["countries"]["B"]
        self.assertEqual(a["current_account_total"], Decimal(-2000))
        self.assertEqual(b["current_account_total"], Decimal(2000))
        self.assertEqual(a["iip"]["net"], Decimal(-2000))
        self.assertEqual(b["iip"]["net"], Decimal(2000))
        self.assertEqual(report["mirror"]["current_account"], Decimal(0))
        self.assertTrue(report["checks"]["ok"])

    def test_mixed_currency_stocks_still_mirror(self):
        w = world(initial=False)
        w.add_initial_claim("A", "B", "bond", "BBB", 50)
        w.add_initial_claim("B", "A", "bond", "AAA", 20)
        report = w.close_period()
        self.assertEqual(report["countries"]["A"]["iip"]["assets"], Decimal(100))
        self.assertEqual(report["countries"]["A"]["iip"]["liabilities"], Decimal(20))
        self.assertEqual(report["countries"]["B"]["iip"]["assets"], Decimal(20))
        self.assertEqual(report["countries"]["B"]["iip"]["liabilities"], Decimal(100))
        self.assertEqual(report["mirror"]["iip"], Decimal(0))
        self.assertTrue(report["checks"]["ok"])


class ObservationDiscrepancyTests(unittest.TestCase):
    def test_discrepancy_is_not_a_flow(self):
        w = world(initial=False)
        d = w.record_observation("A", "B", 10, 8, "AAA")
        self.assertEqual(d, Decimal(2))
        report = w.close_period()
        self.assertEqual(report["countries"]["A"]["current_account_total"], Decimal(0))
        self.assertEqual(report["countries"]["B"]["current_account_total"], Decimal(0))
        self.assertEqual(report["countries"]["A"]["financial_account"], Decimal(0))
        self.assertEqual(len(report["discrepancies"]), 1)
        self.assertEqual(report["discrepancies"][0]["discrepancy"], Decimal(2))
        self.assertEqual(report["discrepancies"][0]["numeraire_discrepancy"], Decimal(2))

    def test_zero_discrepancy_when_reports_mirror(self):
        w = world(initial=False)
        d = w.record_observation("A", "B", 7, 7, "AAA")
        self.assertEqual(d, Decimal(0))
        report = w.close_period()
        self.assertEqual(report["discrepancies"][0]["discrepancy"], Decimal(0))
        self.assertEqual(w.observation_discrepancy_numeraire, Decimal(0))

    def test_discrepancy_in_foreign_currency_uses_numeraire(self):
        w = world(initial=False)
        w.record_observation("A", "B", 10, 8, "BBB")
        report = w.close_period()
        self.assertEqual(report["discrepancies"][0]["numeraire_discrepancy"], Decimal(4))
        self.assertEqual(report["countries"]["A"]["current_account_total"], Decimal(0))


class MultiPeriodAndHookTests(unittest.TestCase):
    def test_close_carries_stocks_into_the_next_period(self):
        w = world(initial=False)
        w.record_trade("A", "B", 10, "AAA")
        first = w.close_period()
        second = w.close_period()
        self.assertEqual(second["countries"]["A"]["iip"]["opening_net"], first["countries"]["A"]["iip"]["net"])
        self.assertEqual(second["countries"]["A"]["current_account_total"], Decimal(0))
        self.assertEqual(second["countries"]["A"]["financial_account"], Decimal(0))
        self.assertTrue(second["checks"]["ok"])

    def test_hook_receives_claims_flows_and_revaluation(self):
        hook = RecordingHook()
        w = world(initial=False, hook=hook)
        w.add_initial_claim("A", "B", "bond", "BBB", 10)
        w.record_trade("A", "B", 5, "AAA")
        w.close_period(FXMarket("AAA", {"AAA": 1, "BBB": 1}))
        self.assertTrue(hook.claims)
        self.assertTrue(any(f["kind"] == "CA" for f in hook.flows))
        self.assertTrue(hook.revaluations)

    def test_verify_checks_mirror_without_closing(self):
        w = world(initial=False)
        w.record_trade("A", "B", 3, "AAA")
        self.assertTrue(w.verify())
        self.assertEqual(w.claims, {("A", "B", "trade_credit", "AAA"): Decimal(3)})


class LegacyRegressionTests(unittest.TestCase):
    def test_legacy_two_country_prototype_still_works(self):
        from amfe.two_country_v01 import Country as LegacyCountry, World as LegacyWorld

        w = LegacyWorld((LegacyCountry("A", "AAA"), LegacyCountry("B", "BBB")))
        w.endow("A", "AAA", cash=100, goods=20)
        w.endow("B", "BBB", cash=100, goods=5)
        w.endow("B", "AAA", cash=20)
        w.trade("A", "B", 4, 3, "AAA")
        self.assertTrue(w.verify())

    def test_legacy_integrated_world_prototype_imports(self):
        from amfe.integrated_world_v01 import InternationalAccounts

        w = InternationalAccounts()
        w.endow("A", "AAA", 100, goods=20)
        w.endow("B", "BBB", 100, goods=5)
        w.exchange("A", "B", "AAA", 10, "BBB", 10)
        w.trade("A", "B", 4, 2, "AAA")
        w.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
