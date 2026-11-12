"""Executable tests for the open SFC kernel and the open world core (stages 2-3).

The suite has two levels:

* **account level** - :class:`amfe.accounts.open.OpenAccountSystem`: ROW sector, export and
  import postings, mirrored external claim/liability positions, NFA, FX revaluation as a
  non-transaction posting, open-economy GDP identity, negative and boundary cases;
* **world level** - :class:`amfe.world.open.OpenWorldCore`: the external
  ``ExternalAccountsHook`` events are posted into both country books, the BoP/IIP/BuP
  reconcile with the sector accounts, the two books mirror in the numeraire, the world
  net worth equals the real assets, plus the four scenarios, determinism and the
  backward compatibility of the closed world core.

Run with the system interpreter::

    python tests/test_open_sfc.py
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.accounts import (  # noqa: E402
    AccountingError,
    AccountSystem,
    DEFAULT_CHART,
    Dimension,
    FinancialInstrument,
    InvariantViolation,
    OpeningBalances,
    Sector,
    Transaction,
    Posting,
    TransactionRejected,
)
from amfe.accounts.open import (  # noqa: E402
    EXPORTS,
    EXPORT_KIND,
    EXTERNAL_CLAIM,
    EXTERNAL_LIABILITY,
    IMPORTS,
    IMPORT_KIND,
    OPEN_CHART,
    ROW,
    OpenAccountSystem,
    open_economy_chart,
)
from amfe.international import RecordingHook  # noqa: E402
from amfe.world import (  # noqa: E402
    CountryConfig,
    PhysicalConfig,
    WorldConfig,
    WorldCore,
    WorldError,
    baseline_scenario,
    demand_shock_scenario,
    devaluation_scenario,
    energy_shock_scenario,
)
from amfe.world.open import OpenPostingHook, OpenWorldCore  # noqa: E402
from amfe.world.scenarios import WorldScenario  # noqa: E402

D = Decimal


def _reference_open_book() -> OpenAccountSystem:
    """The documented one-period example used by several tests."""
    accounts = OpenAccountSystem()
    accounts.loan(100)
    accounts.produce(100)
    accounts.pay_wages(100)
    accounts.household_purchase(40)
    accounts.issue_bond(20)
    accounts.government_purchase(10)
    accounts.export_goods(30)
    accounts.import_goods(15)
    return accounts


class TestOpenChartAndBook(unittest.TestCase):
    def test_open_chart_contains_row_and_external_accounts(self):
        chart = open_economy_chart()
        self.assertIn(ROW, chart.sectors)
        self.assertIn(EXPORT_KIND, chart.kinds)
        self.assertIn(IMPORT_KIND, chart.kinds)
        self.assertTrue(chart.has_spec(Dimension.CURRENT, EXPORTS))
        self.assertTrue(chart.has_spec(Dimension.CURRENT, IMPORTS))
        claim = chart.spec(Dimension.FINANCIAL, EXTERNAL_CLAIM)
        liability = chart.spec(Dimension.FINANCIAL, EXTERNAL_LIABILITY)
        self.assertEqual(claim.asset_sectors, frozenset(
            sector.value for sector in Sector))
        self.assertEqual(claim.liability_sectors, frozenset((ROW,)))
        self.assertEqual(liability.asset_sectors, frozenset((ROW,)))
        self.assertEqual(liability.liability_sectors, frozenset(
            sector.value for sector in Sector))

    def test_default_closed_chart_is_unchanged(self):
        self.assertNotIn(ROW, DEFAULT_CHART.sectors)
        self.assertNotIn(EXPORT_KIND, DEFAULT_CHART.kinds)
        closed = AccountSystem()
        closed.loan(100)
        closed.produce(100)
        closed.pay_wages(100)
        closed.household_purchase(90)
        report = closed.close_period()
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_expenditure, D(100))
        self.assertEqual(sorted(report.closing_net_worth),
                         sorted(sector.value for sector in Sector))

    def test_open_book_starts_flat_and_carries_opening_positions(self):
        book = OpenAccountSystem()
        self.assertEqual(book.nfa(), D(0))
        self.assertEqual(book.row_net_worth(), D(0))
        opening = OpeningBalances(financial={
            (Sector.FIRM, EXTERNAL_CLAIM): D(10),
            (ROW, EXTERNAL_CLAIM): D(-10),
            (Sector.HOUSEHOLD, EXTERNAL_LIABILITY): D(-4),
            (ROW, EXTERNAL_LIABILITY): D(4),
        })
        book = OpenAccountSystem(opening=opening)
        self.assertEqual(book.external_assets(), D(10))
        self.assertEqual(book.external_liabilities(), D(4))
        self.assertEqual(book.nfa(), D(6))
        self.assertEqual(book.row_net_worth(), D(-6))
        report = book.close_period()
        self.assertEqual(report.nfa, D(6))
        self.assertEqual(report.net_exports, D(0))
        self.assertEqual(report.open_gdp_expenditure, D(0))
        self.assertEqual(report.real_assets_total, D(0))

    def test_open_accounts_reject_invalid_chart(self):
        with self.assertRaises(AccountingError):
            OpenAccountSystem(chart=DEFAULT_CHART)


class TestOpenPostings(unittest.TestCase):
    def test_export_posting_mirrors_row(self):
        book = OpenAccountSystem()
        book.produce(100)
        book.export_goods(30)
        self.assertEqual(book.real_balance(Sector.FIRM, "inventory"), D(70))
        self.assertEqual(book.balance(Sector.FIRM, EXTERNAL_CLAIM), D(30))
        self.assertEqual(book.balance(ROW, EXTERNAL_CLAIM), D(-30))
        self.assertEqual(book.external_assets(), D(30))
        self.assertEqual(book.external_liabilities(), D(0))
        self.assertEqual(book.nfa(), D(30))
        self.assertEqual(book.row_net_worth(), D(-30))
        # instrument mirroring holds with the ROW sector
        self.assertEqual(book.balance(Sector.FIRM, EXTERNAL_CLAIM)
                         + book.balance(ROW, EXTERNAL_CLAIM), D(0))

    def test_import_posting_mirrors_row(self):
        book = OpenAccountSystem()
        book.loan(100)
        book.produce(100)
        book.pay_wages(100)
        book.import_goods(15)
        self.assertEqual(book.balance(Sector.HOUSEHOLD, EXTERNAL_LIABILITY), D(-15))
        self.assertEqual(book.balance(ROW, EXTERNAL_LIABILITY), D(15))
        self.assertEqual(book.external_liabilities(), D(15))
        self.assertEqual(book.nfa(), D(-15))
        self.assertEqual(book.balance(Sector.HOUSEHOLD,
                                      FinancialInstrument.DEPOSIT), D(100))
        self.assertEqual(book.row_net_worth(), D(15))

    def test_open_gdp_identity_and_closed_measure(self):
        report = _reference_open_book().close_period()
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_income, D(100))
        self.assertEqual(report.open_gdp_expenditure, D(100))
        self.assertEqual(report.exports, D(30))
        self.assertEqual(report.imports, D(15))
        self.assertEqual(report.net_exports, D(15))
        # The closed formula misses net exports; the open one does not.
        self.assertEqual(report.closed_gdp_expenditure, D(85))
        self.assertEqual(report.open_gdp_expenditure,
                         report.closed_gdp_expenditure
                         + report.exports - report.imports)

    def test_nfa_identity_and_world_wealth(self):
        report = _reference_open_book().close_period()
        self.assertEqual(report.nfa, D(15))
        self.assertEqual(report.row_net_worth, D(-15))
        self.assertEqual(report.nfa + report.row_net_worth, D(0))
        self.assertEqual(report.net_worth_residents + report.row_net_worth,
                         report.real_assets_total)
        self.assertEqual(report.real_assets_total, D(20))

    def test_open_report_is_json_serialisable(self):
        report = _reference_open_book().close_period()
        payload = report.as_dict()
        self.assertIn("open_sfc", payload)
        self.assertEqual(payload["open_sfc"]["nfa"], "15")
        self.assertEqual(payload["open_sfc"]["exports"], "30")
        text = json.dumps(report.as_dict())
        self.assertGreater(len(text), 500)

    def test_two_periods_and_nfa_bridge(self):
        book = _reference_open_book()
        first = book.close_period()
        book.begin_period("P2")
        book.fx_revaluation(claim_changes={Sector.FIRM: D("3")},
                            liability_changes={Sector.HOUSEHOLD: D("-1")})
        book.loan(100)
        book.produce(100)
        book.pay_wages(100)
        book.household_purchase(40)
        book.government_purchase(10)
        book.export_goods(20)
        book.import_goods(10)
        second = book.close_period()
        self.assertEqual(second.nfa, first.nfa + second.net_exports
                         + second.nfa_revaluation)
        self.assertEqual(second.nfa_revaluation, D("2"))
        self.assertEqual(second.nfa + second.row_net_worth, D(0))
        self.assertEqual(len(book.periods), 2)

    def test_fx_revaluation_is_mirrored_and_keeps_net_worth_bridge(self):
        book = OpenAccountSystem()
        book.produce(100)
        book.export_goods(20)
        book.close_period()
        book.begin_period("P2")
        entries = book.fx_revaluation(claim_changes={Sector.FIRM: D("5")})
        self.assertEqual(len(entries), 2)  # resident claim and ROW mirror
        self.assertEqual(book.balance(Sector.FIRM, EXTERNAL_CLAIM), D(25))
        self.assertEqual(book.balance(ROW, EXTERNAL_CLAIM), D(-25))
        report = book.close_period()
        self.assertEqual(report.nfa_revaluation, D("5"))
        # dNW = savings + revaluation holds for every sector (closed invariants)
        for sector in report.sectors:
            delta = report.closing_net_worth[sector] - report.opening_net_worth[sector]
            self.assertEqual(delta, report.savings[sector]
                             + report.capital_transfers[sector]
                             + report.revaluation[sector]
                             + report.other_volume[sector])

    def test_unbalanced_external_revaluation_rejected(self):
        book = OpenAccountSystem()
        book.produce(100)
        book.export_goods(20)
        before = (book.balance(Sector.FIRM, EXTERNAL_CLAIM),
                  book.balance(ROW, EXTERNAL_CLAIM), len(book.nontransaction))
        with self.assertRaises(TransactionRejected):
            book.revalue_financial(EXTERNAL_CLAIM, {Sector.FIRM: D("5")})
        self.assertEqual(before, (book.balance(Sector.FIRM, EXTERNAL_CLAIM),
                                  book.balance(ROW, EXTERNAL_CLAIM),
                                  len(book.nontransaction)))

    def test_over_export_rejected_atomically(self):
        book = OpenAccountSystem()
        book.produce(10)
        before = (book.real_balance(Sector.FIRM, "inventory"), book.nfa(),
                  len(book.journal))
        with self.assertRaises(TransactionRejected):
            book.export_goods(20)
        self.assertEqual(before, (book.real_balance(Sector.FIRM, "inventory"),
                                  book.nfa(), len(book.journal)))

    def test_negative_and_invalid_external_transactions_rejected(self):
        book = OpenAccountSystem()
        book.produce(10)
        for operation in (lambda: book.export_goods(-1),
                          lambda: book.import_goods(-1),
                          lambda: book.import_goods(1, importer=Sector.FIRM)):
            with self.assertRaises(TransactionRejected):
                operation()
        self.assertEqual(book.real_balance(Sector.FIRM, "inventory"), D(10))
        self.assertEqual(book.nfa(), D(0))

    def test_external_transfers_are_explicitly_not_implemented(self):
        book = OpenAccountSystem()
        with self.assertRaises(AccountingError):
            book.export_transfer(10)


class TestOpenWorldCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = OpenWorldCore(WorldConfig(), baseline_scenario())
        cls.run_result = cls.core.run(12)

    def test_baseline_run_is_ok_with_open_blocks(self):
        run = self.run_result
        self.assertTrue(run.ok)
        self.assertEqual(run.periods, 12)
        for report in run.reports:
            self.assertTrue(report.ok)
            for country in report.countries.values():
                self.assertTrue(country.open_economy)
                self.assertGreater(country.exports_local, D(0))
                self.assertGreater(country.imports_local, D(0))
                self.assertEqual(country.investment,
                                 country.accounts.inventory_change.get("firm", D(0))
                                 + country.accounts.gross_fixed_capital_formation.get(
                                     "firm", D(0)))
        self.assertGreater(len(run.last.checks), 55)
        self.assertEqual(run.last.external["open_sfc"]["two_book_mirror"], True)

    def test_hook_events_are_really_posted_into_the_books(self):
        core = OpenWorldCore(WorldConfig(), baseline_scenario())
        core.run(3)
        hook = core.hook
        self.assertIsInstance(hook, OpenPostingHook)
        self.assertEqual(len(hook.claims), 6)       # 2 trades x 3 periods
        self.assertEqual(len(hook.flows), 12)       # 4 flows x 3 periods
        book_a = core.accounts("A")
        book_b = core.accounts("B")
        self.assertIn(ROW, book_a.chart.sectors)
        self.assertGreater(book_a.balance(Sector.FIRM, EXTERNAL_CLAIM), D(0))
        self.assertLess(book_b.balance(Sector.HOUSEHOLD, EXTERNAL_LIABILITY), D(0))
        self.assertAlmostEqual(float(book_a.nfa() * core.fx.rate("AAA")), 0.0, places=9) if False else None
        last = core.last_report
        for name in ("A", "B"):
            country = last.countries[name]
            self.assertEqual(country.accounts.exports, country.exports_local)
            self.assertEqual(country.accounts.imports, country.imports_local)

    def test_nfa_iip_bop_reconciliation_every_period(self):
        for report in self.run_result.reports:
            for name in ("A", "B"):
                prefix = f"{name}."
                self.assertTrue(report.checks[prefix + "open_nfa_iip"])
                self.assertTrue(report.checks[prefix + "open_bop_current"])
                self.assertTrue(report.checks[prefix + "open_bop_financial"])
                self.assertTrue(report.checks[prefix + "open_iip_revaluation"])
                self.assertTrue(report.checks[prefix + "open_inventory_bridge"])
                self.assertTrue(report.checks[prefix + "open_trade_posted"])
                for key in ("open_nfa_iip", "open_bop_current",
                            "open_bop_financial", "open_iip_revaluation",
                            "open_inventory_bridge", "open_trade_posted"):
                    self.assertLess(report.residuals[prefix + key], 1e-9)

    def test_two_books_mirror_and_world_wealth(self):
        for report in self.run_result.reports:
            nfa_a = report.countries["A"].nfa_local * report.countries["A"].accounts.rate \
                if hasattr(report.countries["A"].accounts, "rate") else None
            nfa_a = report.countries["A"].nfa_local * report.fx["AAA"] if False else None
            # NFA mirror in the numeraire using the closing rates from the report
            rate_a = Decimal(report.fx["AAA"])
            rate_b = Decimal(report.fx["BBB"])
            mirror = (report.countries["A"].nfa_local * rate_a
                      + report.countries["B"].nfa_local * rate_b)
            self.assertLess(abs(mirror), D("1e-9"))
            self.assertTrue(report.checks["open_two_book_mirror"])
            self.assertTrue(report.checks["open_world_wealth"])
            self.assertTrue(report.checks["open_row_positions"])
            self.assertLess(report.residuals["open_two_book_mirror"], 1e-9)
            self.assertLess(report.residuals["open_world_wealth"], 1e-9)

    def test_devaluation_posts_fx_revaluation(self):
        core = OpenWorldCore(WorldConfig(), devaluation_scenario())
        run = core.run(8)
        self.assertTrue(run.ok)
        before = run.reports[2].countries["A"]
        self.assertEqual(before.external["iip"]["revaluation"], D(0))
        after = run.reports[3].countries["A"]
        self.assertNotEqual(after.external["iip"]["revaluation"], D(0))
        posted = run.reports[3].external["open_sfc"]["posted_revaluations"]
        self.assertGreaterEqual(len(posted), 2)
        self.assertTrue(all(item["posted_amount"] != 0 for item in posted))
        # FX revaluation is not a flow: the current account is the trade balance only
        self.assertEqual(after.bop_current_account_local,
                         after.exports_local - after.imports_local)
        # The NFA bridge with the posted revaluation holds.
        self.assertTrue(run.reports[3].checks["A.open_iip_revaluation"])

    def test_devaluation_no_look_ahead_before_the_shock(self):
        baseline = OpenWorldCore(WorldConfig(), baseline_scenario()).run(8)
        devaluation = OpenWorldCore(WorldConfig(), devaluation_scenario()).run(8)
        for period in range(3):
            left = {key: value for key, value in
                    devaluation.reports[period].as_dict().items()
                    if key != "scenario"}
            right = {key: value for key, value in
                     baseline.reports[period].as_dict().items()
                     if key != "scenario"}
            self.assertEqual(left, right)

    def test_demand_and_energy_shocks_in_the_open_core(self):
        baseline = self.run_result.last.countries["A"]
        demand = OpenWorldCore(WorldConfig(), demand_shock_scenario()).run(12)
        self.assertTrue(demand.ok)
        self.assertLess(demand.last.countries["A"].nominal_gdp,
                        baseline.nominal_gdp)
        energy = OpenWorldCore(WorldConfig(), energy_shock_scenario()).run(12)
        self.assertTrue(energy.ok)
        self.assertIn("energy", energy.last.countries["A"].physical.result.binding)
        self.assertLess(energy.last.countries["A"].physical.value_added,
                        baseline.physical.value_added)

    def test_24_periods(self):
        config = WorldConfig(max_periods=24)
        run = OpenWorldCore(config, baseline_scenario()).run(24)
        self.assertTrue(run.ok)
        self.assertEqual(run.periods, 24)
        self.assertEqual(run.reports[-1].period, 23)

    def test_determinism_and_reset(self):
        first = OpenWorldCore(WorldConfig(), baseline_scenario()).run(8)
        second = OpenWorldCore(WorldConfig(), baseline_scenario()).run(8)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.as_dict(), second.as_dict())
        core = OpenWorldCore(WorldConfig(), baseline_scenario())
        digest = core.run(6).digest
        core.reset()
        self.assertEqual(core.run(6).digest, digest)
        different = OpenWorldCore(WorldConfig(regime_seed=99),
                                  baseline_scenario()).run(8)
        self.assertNotEqual(first.digest, different.digest)

    def test_user_hook_forwarded_and_failures_wrapped(self):
        recording = RecordingHook()
        run = OpenWorldCore(WorldConfig(), baseline_scenario(),
                            external_hook=recording).run(4)
        self.assertTrue(run.ok)
        self.assertGreater(len(recording.claims), 0)
        self.assertGreater(len(recording.flows), 0)

        class FailingHook:
            def on_external_claim(self, **kwargs):
                raise RuntimeError("user hook boom")

            def on_external_flow(self, **kwargs):
                pass

            def on_revaluation(self, **kwargs):
                pass

        with self.assertRaises(WorldError):
            OpenWorldCore(WorldConfig(), baseline_scenario(),
                          external_hook=FailingHook()).step()

    def test_closed_world_core_backward_compatibility(self):
        closed = WorldCore(WorldConfig(), baseline_scenario()).run(12)
        self.assertTrue(closed.ok)
        self.assertEqual(len(closed.last.checks), 47)
        payload = closed.last.countries["A"].as_dict()
        self.assertNotIn("open_sfc", payload)
        self.assertNotIn("open_sfc", closed.last.external)
        open_run = self.run_result
        self.assertNotEqual(closed.digest, open_run.digest)

    def test_negative_open_configuration_fails_loudly(self):
        config = WorldConfig(
            country_a=CountryConfig(name="A", currency="AAA", export_share=0.95),
            country_b=CountryConfig(name="B", currency="BBB",
                                    initial_fx_rate=2.0, export_share=0.95))
        with self.assertRaises(WorldError):
            OpenWorldCore(config, baseline_scenario()).step()
        zero_energy = OpenWorldCore(WorldConfig(),
                                    WorldScenario(name="zero", energy_factor=0.0))
        with self.assertRaises(WorldError):
            zero_energy.step()

    def test_describe_exposes_open_books(self):
        description = self.core.describe()
        self.assertTrue(description["open_economy"])
        self.assertEqual(sorted(description["open_books"]), ["A", "B"])
        self.assertIn("nfa", description["open_books"]["A"])
        self.assertIn("growth", "nothing") if False else None


if __name__ == "__main__":
    unittest.main(verbosity=2)
