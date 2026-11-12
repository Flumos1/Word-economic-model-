"""Executable tests for the typed sector-accounts kernel (AMFE-17 stage 2).

Covers:

* the documented scenarios of docs/39, docs/40, docs/44 and the accrual
  example of docs/47;
* the required invariants: financial mirroring, ``dNW = savings + revaluation``
  (with capital transfers / other volume changes), the government budget
  constraint, and the closed economy without residuals;
* the reproducible 10 000-case parameter grid of
  ``tests/test_government_parameter_grid_v03.py``;
* backward compatibility with the v01, v02 and v06 prototypes (the old test
  files are imported read-only and never modified);
* negative and boundary cases (unbalanced entries, overspending, overselling,
  over-repayment, illegal signs, atomic rollback, closed periods, invalid
  amounts) and the independent invariant checkers on deliberately corrupted
  reports;
* the stage-3 extension contract (new sectors/instruments in the chart and the
  two-book rest-of-world mirroring check).

Run with the system interpreter::

    python tests/test_accounts.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.accounts import (  # noqa: E402
    AccountSpec,
    AccountSystem,
    AccountingError,
    BalanceSheet,
    CapitalFlow,
    ChartOfAccounts,
    CurrentFlow,
    DEFAULT_CHART,
    Dimension,
    FinancialInstrument,
    InvariantViolation,
    JournalEntry,
    OpeningBalances,
    PeriodReport,
    PeriodStateError,
    Posting,
    RealAsset,
    Sector,
    Snapshot,
    Transaction,
    TransactionRejected,
    check_closed_economy,
    check_external_mirroring,
    check_financial_accounts,
    check_gdp_identity,
    check_government_budget,
    check_period_identity,
    check_report,
    check_snapshot,
    check_stock_signs,
    code_string,
    to_decimal,
)
from amfe.accounts import scenario  # noqa: E402

D = Decimal

_REFERENCE_CACHE: dict[str, object] = {}


def load_reference(name: str, filename: str):
    """Import one of the original prototype test modules read-only."""
    if name not in _REFERENCE_CACHE:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REFERENCE_CACHE[name] = module
    return _REFERENCE_CACHE[name]


class TestDocumentedScenarios(unittest.TestCase):
    """docs/39, docs/40, docs/44 and docs/47 reference outcomes."""

    def test_empty_system_closes_with_zero_report(self):
        accounts = AccountSystem()
        report = accounts.close_period()
        self.assertEqual(len(report.entries), 0)
        self.assertEqual(set(report.closing_net_worth), set(chart_sector_codes()))
        for value in report.closing_net_worth.values():
            self.assertEqual(value, D(0))
        self.assertEqual(report.gdp_production, D(0))
        self.assertEqual(report.gdp_expenditure, D(0))
        self.assertEqual(report.gdp_income, D(0))
        self.assertEqual(sum(report.net_lending.values()), D(0))
        accounts.assert_invariants()

    def test_baseline_net_worth_and_inventory(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self.assertEqual(report.closing_net_worth,
                         scenario.BASELINE_NET_WORTH)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         scenario.BASELINE_INVENTORY)
        self.assertEqual(sum(report.closing_net_worth.values()),
                         scenario.BASELINE_INVENTORY)
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.LOAN),
                         scenario.BASELINE_LOAN_OUTSTANDING)
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.DEPOSIT),
                         scenario.BASELINE_GOVERNMENT_CASH)
        # The bank's net worth is the accumulated interest result (docs/40 F05).
        self.assertEqual(accounts.net_worth(Sector.BANK), D(5))
        self.assertEqual(len(accounts.journal), 9)

    def test_baseline_government_budget(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        budget = check_government_budget(report)
        self.assertEqual(budget.revenue, D(30))
        self.assertEqual(budget.expenditure, D(25))
        self.assertEqual(budget.net_saving, D(5))
        self.assertEqual(budget.deficit, D(-5))       # surplus
        self.assertEqual(budget.delta_cash, D(5))
        self.assertEqual(budget.delta_debt, D(0))
        self.assertEqual(budget.net_borrowing, D(-5))
        self.assertEqual(budget.net_lending, D(5))

    def test_baseline_gdp_three_measures(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_expenditure, D(100))
        self.assertEqual(report.gdp_income, D(100))
        self.assertEqual(report.household_consumption, D(70))
        self.assertEqual(report.government_consumption, D(15))
        self.assertEqual(report.inventory_change[Sector.FIRM.value], D(15))
        self.assertEqual(report.gross_fixed_capital_formation[Sector.FIRM.value], D(0))
        self.assertEqual(report.wages_received, D(100))
        self.assertEqual(report.gross_operating_surplus, D(0))

    def test_closed_economy_without_residuals(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self.assertEqual(sum(report.net_lending.values()), D(0))
        self.assertEqual(sum(report.net_financial_change.values()), D(0))
        self.assertEqual(sum(report.gross_saving.values()),
                         sum(report.capital_formation.values()))
        self.assertEqual(sum(report.savings.values()), D(15))
        instruments = set()
        for detail in report.financial_change.values():
            instruments.update(detail)
        for instrument in instruments:
            self.assertEqual(
                sum((detail.get(instrument, D(0))
                     for detail in report.financial_change.values()), D(0)),
                D(0), f"instrument {instrument} is not mirrored")
        check_report(report)

    def test_unsold_output_is_inventory_investment_docs39(self):
        accounts = scenario.inventory_scenario(purchases=90)
        report = accounts.close_period()
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(10))
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_expenditure, D(100))
        self.assertEqual(report.household_consumption, D(90))
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD,
                                          FinancialInstrument.DEPOSIT), D(10))
        self.assertEqual(accounts.net_worth(Sector.FIRM), D(0))
        self.assertEqual(sum(report.closing_net_worth.values()), D(10))

    def test_government_debt_financed_deficit_docs40(self):
        accounts = scenario.government_debt_scenario()
        report = accounts.close_period()
        self.assertEqual(report.closing_net_worth, scenario.DEBT_NET_WORTH)
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.BOND),
                         scenario.DEBT_BOND_LIABILITY)
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.DEPOSIT),
                         scenario.DEBT_GOVERNMENT_CASH)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         scenario.DEBT_INVENTORY)
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.LOAN),
                         D(20))
        budget = check_government_budget(report)
        self.assertEqual(budget.deficit, D(20))
        self.assertEqual(budget.net_borrowing, D(20))
        self.assertEqual(budget.delta_debt, D(20))
        self.assertEqual(budget.delta_cash, D(0))
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_expenditure, D(100))
        self.assertEqual(report.gdp_income, D(100))

    def test_accrued_wages_then_settlement_docs47(self):
        accounts = scenario.accrual_scenario()
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD,
                                          FinancialInstrument.RECEIVABLE), D(15))
        self.assertEqual(accounts.balance(Sector.FIRM,
                                          FinancialInstrument.RECEIVABLE), D(-15))
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD,
                                          FinancialInstrument.DEPOSIT), D(10))
        report = accounts.close_period()
        self.assertEqual(report.closing_net_worth[Sector.FIRM.value], D(-25))
        self.assertEqual(report.closing_net_worth[Sector.HOUSEHOLD.value], D(25))
        self.assertEqual(report.savings[Sector.FIRM.value], D(-25))
        self.assertEqual(report.savings[Sector.HOUSEHOLD.value], D(25))

    def test_fixed_capital_and_depreciation(self):
        accounts = AccountSystem()
        accounts.produce(20)
        accounts.invest_fixed_capital(12)
        accounts.depreciate(2)
        report = accounts.close_period()
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY), D(8))
        self.assertEqual(accounts.real_balance(Sector.FIRM,
                                               RealAsset.FIXED_CAPITAL), D(10))
        self.assertEqual(accounts.net_worth(Sector.FIRM), D(18))
        self.assertEqual(report.savings[Sector.FIRM.value], D(18))
        self.assertEqual(report.consumption_of_fixed_capital[Sector.FIRM.value], D(2))
        self.assertEqual(report.gross_saving[Sector.FIRM.value], D(20))
        self.assertEqual(report.capital_formation[Sector.FIRM.value], D(20))
        self.assertEqual(report.net_lending[Sector.FIRM.value], D(0))
        self.assertEqual(report.gdp_production, D(20))
        self.assertEqual(report.gdp_expenditure, D(20))
        self.assertEqual(report.gdp_income, D(20))


class TestNetWorthBridge(unittest.TestCase):
    """P01/P02: dNW = savings + revaluation (+ capital transfers/OVC)."""

    def test_baseline_change_in_net_worth_equals_saving(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        for sector in report.sectors:
            delta = report.closing_net_worth[sector] - report.opening_net_worth[sector]
            self.assertEqual(delta, report.savings[sector], sector)

    def test_savings_plus_revaluation_bridge(self):
        accounts = scenario.baseline_scenario()
        accounts.revalue_real(RealAsset.INVENTORY, 5)
        accounts.other_volume_change_real(RealAsset.INVENTORY, -3)
        accounts.capital_transfer(5, payer=Sector.GOVERNMENT,
                                  receiver=Sector.HOUSEHOLD)
        report = accounts.close_period()
        for sector in report.sectors:
            delta = report.closing_net_worth[sector] - report.opening_net_worth[sector]
            expected = (report.savings[sector] + report.capital_transfers[sector]
                        + report.revaluation[sector] + report.other_volume[sector])
            self.assertEqual(delta, expected, sector)
        self.assertEqual(report.revaluation[Sector.FIRM.value], D(5))
        self.assertEqual(report.other_volume[Sector.FIRM.value], D(-3))
        self.assertEqual(report.capital_transfers[Sector.HOUSEHOLD.value], D(5))
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(17))
        self.assertEqual(sum(report.closing_net_worth.values()),
                         accounts.snapshot().total_real_assets())
        self.assertEqual(sum(report.net_lending.values()), D(0))

    def test_financial_revaluation_is_mirrored(self):
        accounts = scenario.baseline_scenario()
        baseline = accounts.close_period()
        second = AccountSystem()
        scenario.baseline_scenario(second)
        second.revalue_financial(FinancialInstrument.LOAN,
                                 {Sector.BANK: -10, Sector.FIRM: 10})
        report = second.close_period()
        self.assertEqual(report.revaluation_financial[Sector.BANK.value], D(-10))
        self.assertEqual(report.revaluation_financial[Sector.FIRM.value], D(10))
        # The write-down shifts net worth between bank and firm, total unchanged.
        self.assertEqual(sum(report.closing_net_worth.values()),
                         sum(baseline.closing_net_worth.values()))
        self.assertEqual(report.closing_net_worth[Sector.FIRM.value], D(-25))
        self.assertEqual(report.closing_net_worth[Sector.BANK.value], D(-5))
        for sector in report.sectors:
            delta = report.closing_net_worth[sector] - report.opening_net_worth[sector]
            self.assertEqual(delta,
                             report.savings[sector] + report.revaluation[sector])

    def test_unbalanced_financial_revaluation_rejected_atomically(self):
        accounts = scenario.baseline_scenario()
        before = (dict(accounts.financial_items()), dict(accounts.real_items()),
                  len(accounts.nontransaction))
        with self.assertRaises(TransactionRejected) as context:
            accounts.revalue_financial(FinancialInstrument.LOAN,
                                       {Sector.BANK: -10, Sector.FIRM: 5})
        self.assertEqual(context.exception.code, "unbalanced_transaction")
        self.assertEqual(before, (dict(accounts.financial_items()),
                                  dict(accounts.real_items()),
                                  len(accounts.nontransaction)))


class TestPeriodLifecycle(unittest.TestCase):
    def test_two_periods_carry_opening_stocks(self):
        accounts = AccountSystem()
        scenario.closed_economy_cycle(accounts, purchases=80)
        first = accounts.close_period()
        accounts.begin_period("P2")
        self.assertFalse(accounts.last_report is None)
        accounts.household_purchase(10)
        second = accounts.close_period()
        self.assertEqual(second.opening_net_worth, first.closing_net_worth)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(10))
        self.assertEqual(accounts.balance(Sector.FIRM, FinancialInstrument.LOAN),
                         D(-20))  # firm repaid 80 of the 100 loan in period 1
        self.assertEqual(second.gdp_production, D(0))
        self.assertEqual(second.gdp_expenditure, D(0))
        self.assertEqual(second.inventory_change[Sector.FIRM.value], D(-10))
        self.assertEqual(second.household_consumption, D(10))

    def test_zero_amount_transactions_are_no_ops(self):
        accounts = AccountSystem()
        before = accounts.snapshot()
        accounts.loan(0)
        accounts.produce(0)
        accounts.pay_wages(0)
        accounts.pay_transfer(0, payer=Sector.FIRM, receiver=Sector.HOUSEHOLD)
        self.assertEqual(accounts.snapshot().balances, before.balances)
        self.assertEqual(len(accounts.journal), 4)
        report = accounts.close_period()
        self.assertEqual(report.gdp_production, D(0))
        self.assertEqual(sum(report.net_lending.values()), D(0))

    def test_opening_balances_are_loaded_and_carried(self):
        opening = OpeningBalances(
            financial={
                (Sector.BANK, FinancialInstrument.LOAN): 100,
                (Sector.FIRM, FinancialInstrument.LOAN): -100,
                (Sector.FIRM, FinancialInstrument.DEPOSIT): 100,
                (Sector.BANK, FinancialInstrument.DEPOSIT): -100,
            },
            real={(Sector.FIRM, RealAsset.INVENTORY): 20},
        )
        accounts = AccountSystem(opening=opening)
        self.assertEqual(accounts.net_worth(Sector.FIRM), D(20))
        empty = accounts.close_period()
        self.assertEqual(empty.opening_net_worth, empty.closing_net_worth)
        self.assertEqual(empty.gdp_production, D(0))
        accounts.begin_period("P2")
        accounts.pay_wages(50)
        accounts.household_purchase(10)
        report = accounts.close_period()
        self.assertEqual(report.opening_net_worth, empty.closing_net_worth)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(10))
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD,
                                          FinancialInstrument.DEPOSIT), D(40))
        self.assertEqual(report.gdp_production, D(0))
        self.assertEqual(report.gdp_expenditure, D(0))

    def test_invalid_opening_balances_rejected(self):
        with self.assertRaises(AccountingError) as context:
            AccountSystem(opening=OpeningBalances(
                financial={(Sector.BANK, FinancialInstrument.LOAN): 10}))
        self.assertEqual(context.exception.code, "financial_mirror")
        with self.assertRaises(AccountingError):
            AccountSystem(opening=OpeningBalances(
                financial={(Sector.BANK, FinancialInstrument.DEPOSIT): 10,
                           (Sector.HOUSEHOLD, FinancialInstrument.DEPOSIT): -10}))
        with self.assertRaises(AccountingError):
            AccountSystem(opening=OpeningBalances(
                real={(Sector.FIRM, RealAsset.INVENTORY): -5}))

    def test_export_is_json_ready(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        payload = accounts.export()
        text = json.dumps(payload)
        self.assertGreater(len(text), 1000)
        self.assertEqual(payload["periods"][0]["closing_net_worth"]["firm"], "-35")
        self.assertEqual(len(payload["journal"]), len(report.entries))
        self.assertEqual(payload["snapshot"]["balances"]["bank"]["net_worth"], "5")
        self.assertEqual(Decimal(payload["periods"][0]["output"]), D(100))


class TestBackwardCompatibility(unittest.TestCase):
    """The v01/v02/v06 prototypes must agree with the typed kernel."""

    def _assert_v01_fields(self, reference, accounts):
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.LOAN),
                         D(reference.bank_loans))
        self.assertEqual(accounts.balance(Sector.FIRM, FinancialInstrument.LOAN),
                         -D(reference.firm_debt))
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.DEPOSIT),
                         -D(reference.bank_deposits))
        self.assertEqual(accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT),
                         D(reference.firm_deposit))
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD,
                                          FinancialInstrument.DEPOSIT),
                         D(reference.household_deposit))
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(reference.inventory))

    def test_v01_full_cycle(self):
        reference_module = load_reference("ref_closed_economy_v01",
                                          "test_closed_economy_v01.py")
        reference = reference_module.Economy()
        reference.loan(100)
        reference.produce(100)
        reference.wages(100)
        reference.buy(100)
        reference.repay(100)
        accounts = scenario.closed_economy_cycle()
        report = accounts.close_period()
        self._assert_v01_fields(reference, accounts)
        self.assertEqual(reference.close(), (100, 100, 100))
        self.assertEqual(report.gdp_production, D(100))
        self.assertEqual(report.gdp_expenditure, D(100))
        self.assertEqual(report.gdp_income, D(100))

    def test_v01_unsold_inventory(self):
        reference_module = load_reference("ref_closed_economy_v01",
                                          "test_closed_economy_v01.py")
        reference = reference_module.Economy()
        reference.loan(100)
        reference.produce(100)
        reference.wages(100)
        reference.buy(90)
        accounts = scenario.closed_economy_cycle(purchases=90, repay=False)
        report = accounts.close_period()
        self._assert_v01_fields(reference, accounts)
        self.assertEqual(reference.close(), (100, 100, 100))
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(10))

    def _assert_v02_balances(self, reference_ledger, accounts):
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.LOAN),
                         reference_ledger.bal("bank", "loan_asset"))
        self.assertEqual(accounts.balance(Sector.FIRM, FinancialInstrument.LOAN),
                         reference_ledger.bal("firm", "loan_liability"))
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.DEPOSIT),
                         reference_ledger.bal("bank", "deposit_liability"))
        self.assertEqual(accounts.balance(Sector.BANK, FinancialInstrument.BOND),
                         reference_ledger.bal("bank", "government_bond_asset"))
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.BOND),
                         reference_ledger.bal("government", "bond_liability"))
        for sector in (Sector.FIRM, Sector.HOUSEHOLD, Sector.GOVERNMENT):
            self.assertEqual(accounts.balance(sector, FinancialInstrument.DEPOSIT),
                             reference_ledger.bal(sector.value, "cash"))

    def test_v02_baseline(self):
        reference_module = load_reference("ref_government_economy_v02",
                                          "test_government_economy_v02.py")
        ledger, inventory, repay = reference_module.scenario(
            sales=70, tax=30, transfer=10, public_purchase=15, interest=5)
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self._assert_v02_balances(ledger, accounts)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(inventory))
        self.assertEqual(repay, 50)
        self.assertEqual(report.gdp_production, D(100))
        # docs/40 F05: loans plus bonds equal deposits plus bank interest result.
        self.assertEqual(
            accounts.balance(Sector.BANK, FinancialInstrument.LOAN)
            + accounts.balance(Sector.BANK, FinancialInstrument.BOND),
            -accounts.balance(Sector.BANK, FinancialInstrument.DEPOSIT)
            + accounts.net_worth(Sector.BANK))

    def test_v02_debt_financed_deficit(self):
        reference_module = load_reference("ref_government_economy_v02",
                                          "test_government_economy_v02.py")
        ledger, inventory, repay = reference_module.scenario(
            sales=70, tax=5, transfer=10, public_purchase=15, interest=0,
            government_borrowing=20)
        accounts = scenario.government_debt_scenario()
        report = accounts.close_period()
        self._assert_v02_balances(ledger, accounts)
        self.assertEqual(accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY),
                         D(inventory))
        self.assertEqual(repay, 80)
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.DEPOSIT), D(0))
        self.assertEqual(accounts.balance(Sector.GOVERNMENT,
                                          FinancialInstrument.BOND), D(-20))

    def test_v02_parameter_grid_sample(self):
        reference_module = load_reference("ref_government_economy_v02",
                                          "test_government_economy_v02.py")
        cases = ((50, 0, 0), (50, 5, 9), (60, 2, 8), (70, 20, 9),
                 (89, 24, 9), (75, 5, 0))
        for sales, tax, public in cases:
            transfer = tax // 2
            borrowing = max(0, transfer + public - tax)
            ledger, inventory, repay = reference_module.scenario(
                sales=sales, tax=tax, transfer=transfer,
                public_purchase=public, interest=0,
                government_borrowing=borrowing)
            accounts = AccountSystem()
            accounts.loan(100)
            accounts.produce(100)
            accounts.pay_wages(100)
            accounts.household_purchase(sales)
            accounts.pay_direct_tax(tax)
            if transfer:
                accounts.pay_transfer(transfer, payer=Sector.GOVERNMENT,
                                      receiver=Sector.HOUSEHOLD)
            if borrowing:
                accounts.issue_bond(borrowing)
            accounts.government_purchase(public)
            kernel_repay = min(D(100), accounts.balance(
                Sector.FIRM, FinancialInstrument.DEPOSIT))
            if kernel_repay > 0:
                accounts.repay_loan(kernel_repay)
            report = accounts.close_period()
            self._assert_v02_balances(ledger, accounts)
            self.assertEqual(D(inventory),
                             accounts.real_balance(Sector.FIRM, RealAsset.INVENTORY))
            self.assertEqual(D(repay), kernel_repay)
            self.assertEqual(report.gdp_production, D(100))

    def test_v06_accounting_core_baseline(self):
        import amfe.accounting_core as core  # noqa: PLC0415

        reference = core.Accounts()
        for transaction in (
            core.credit(100),
            core.produce(100),
            core.cash_transfer("wages", "firm", "household", 100),
            core.purchase("household", 70),
            core.cash_transfer("levy", "firm", "government", 30),
            core.cash_transfer("transfer", "government", "household", 10),
            core.purchase("government", 15),
            core.interest(5),
            core.credit(50, True),
        ):
            reference.apply(transaction)
        reference_report = reference.close()
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self.assertEqual(report.closing_net_worth, reference_report["closing"])
        self.assertEqual(sum(report.savings.values()),
                         sum(reference_report["current"].values()))
        self.assertEqual(sum(report.net_lending.values()), D(0))
        self.assertEqual(sum(reference_report["financial"].values()), D(0))
        for sector, value in reference_report["financial"].items():
            self.assertEqual(report.net_financial_change[sector], value)


class TestInvariantCheckers(unittest.TestCase):
    """The checkers must detect deliberately corrupted reports/snapshots."""

    def setUp(self):
        self.accounts = scenario.baseline_scenario()
        self.report = self.accounts.close_period()

    def test_snapshot_mirror_checker_detects_tampering(self):
        balances = dict(self.report.snapshot.balances)
        bank = balances[Sector.BANK.value]
        balances[Sector.BANK.value] = BalanceSheet(
            Sector.BANK, {**bank.financial, "deposit": D(-44)}, bank.real)
        tampered = Snapshot("P1", balances)
        with self.assertRaises(InvariantViolation) as context:
            check_snapshot(tampered)
        self.assertEqual(context.exception.code, "financial_mirror")

    def test_stock_sign_checker_detects_negative_deposit(self):
        snapshot = Snapshot("P1", {
            Sector.BANK.value: BalanceSheet(Sector.BANK, {"deposit": D(10)}, {}),
            Sector.HOUSEHOLD.value: BalanceSheet(
                Sector.HOUSEHOLD, {"deposit": D(-10)}, {}),
        })
        with self.assertRaises(InvariantViolation) as context:
            check_snapshot(snapshot)
        self.assertEqual(context.exception.code, "position_sign")

    def test_period_bridge_checker_detects_tampering(self):
        tampered = replace(
            self.report,
            closing_net_worth={**self.report.closing_net_worth,
                               Sector.FIRM.value: D(-34)})
        with self.assertRaises(InvariantViolation) as context:
            check_period_identity(tampered)
        self.assertEqual(context.exception.code, "net_worth_bridge")

    def test_gdp_checker_detects_tampering(self):
        tampered = replace(self.report, gdp_income=D(99))
        with self.assertRaises(InvariantViolation) as context:
            check_gdp_identity(tampered)
        self.assertEqual(context.exception.code, "gdp_income")
        tampered = replace(self.report, gdp_expenditure=D(101))
        with self.assertRaises(InvariantViolation):
            check_gdp_identity(tampered)

    def test_closed_economy_checker_detects_residual(self):
        tampered = replace(
            self.report,
            net_lending={**self.report.net_lending,
                         Sector.HOUSEHOLD.value: D(41)})
        with self.assertRaises(InvariantViolation) as context:
            check_closed_economy(tampered)
        self.assertEqual(context.exception.code, "closed_economy_residual")

    def test_financial_account_checker_detects_mismatch(self):
        tampered = replace(
            self.report,
            net_financial_change={**self.report.net_financial_change,
                                  Sector.FIRM.value: D(-49)})
        with self.assertRaises(InvariantViolation) as context:
            check_financial_accounts(tampered)
        self.assertEqual(context.exception.code, "financial_account_definition")

    def test_government_budget_checker_detects_tampering(self):
        tampered = replace(
            self.report,
            net_lending={**self.report.net_lending,
                         Sector.GOVERNMENT.value: D(6)})
        with self.assertRaises(InvariantViolation) as context:
            check_government_budget(tampered)
        self.assertEqual(context.exception.code, "government_budget")


class TestNegativeAndBoundary(unittest.TestCase):
    def setUp(self):
        self.accounts = AccountSystem()

    def _assert_unchanged(self):
        before = (dict(self.accounts.financial_items()),
                  dict(self.accounts.real_items()),
                  len(self.accounts.journal),
                  len(self.accounts.nontransaction))
        return before

    def test_unbalanced_financial_entry_rejected_atomically(self):
        before = self._assert_unchanged()
        transaction = Transaction("loan", (
            Posting.financial(Sector.BANK, FinancialInstrument.LOAN, 100),))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "unbalanced_transaction")
        self.assertEqual(before, self._assert_unchanged())

    def test_unbalanced_current_transfer_rejected(self):
        transaction = Transaction("transfer", (
            Posting.current(Sector.FIRM, CurrentFlow.TRANSFER, -10),
            Posting.current(Sector.HOUSEHOLD, CurrentFlow.TRANSFER, 10),
            Posting.financial(Sector.FIRM, FinancialInstrument.DEPOSIT, -10),
        ))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "unbalanced_transaction")

    def test_sector_closure_violation_rejected(self):
        transaction = Transaction("wages", (
            Posting.current(Sector.FIRM, CurrentFlow.WAGES, -10),
            Posting.current(Sector.HOUSEHOLD, CurrentFlow.WAGES, 10),
        ))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "sector_closure")

    def test_overspending_rejected(self):
        scenario.baseline_scenario(self.accounts)
        with self.assertRaises(TransactionRejected):
            self.accounts.household_purchase(1000)
        self.assertEqual(self.accounts.balance(
            Sector.HOUSEHOLD, FinancialInstrument.DEPOSIT), D(40))

    def test_overselling_inventory_rejected(self):
        self.accounts = AccountSystem(opening=OpeningBalances(
            financial={(Sector.GOVERNMENT, FinancialInstrument.DEPOSIT): 10,
                       (Sector.BANK, FinancialInstrument.DEPOSIT): -10}))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.government_purchase(10)
        self.assertEqual(context.exception.code, "negative_real_asset")

    def test_overrepayment_rejected(self):
        self.accounts.loan(100)
        with self.assertRaises(TransactionRejected):
            self.accounts.repay_loan(150)
        self.assertEqual(self.accounts.balance(Sector.BANK,
                                               FinancialInstrument.LOAN), D(100))

    def test_government_capital_transfer_overdraft_rejected(self):
        scenario.baseline_scenario(self.accounts)
        with self.assertRaises(TransactionRejected):
            self.accounts.capital_transfer(10, payer=Sector.GOVERNMENT,
                                           receiver=Sector.HOUSEHOLD)
        self.assertEqual(self.accounts.balance(Sector.GOVERNMENT,
                                               FinancialInstrument.DEPOSIT), D(5))

    def test_illegal_financial_orientation_rejected(self):
        # A negative bond position is a liability: only the government may hold
        # it.  The posting sectors allow the bank to post, so the violation is
        # detected by the stock-orientation check.
        transaction = Transaction("transfer", (
            Posting.current(Sector.BANK, CurrentFlow.TRANSFER, -10),
            Posting.current(Sector.GOVERNMENT, CurrentFlow.TRANSFER, 10),
            Posting.financial(Sector.BANK, FinancialInstrument.BOND, -10),
            Posting.financial(Sector.GOVERNMENT, FinancialInstrument.BOND, 10),
        ))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "position_sign")

    def test_wrong_posting_role_rejected(self):
        transaction = Transaction("wages", (
            Posting.current(Sector.HOUSEHOLD, CurrentFlow.WAGES, -10),
            Posting.current(Sector.FIRM, CurrentFlow.WAGES, 10),
            Posting.financial(Sector.HOUSEHOLD, FinancialInstrument.DEPOSIT, -10),
            Posting.financial(Sector.FIRM, FinancialInstrument.DEPOSIT, 10),
        ))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "posting_role")

    def test_unknown_sector_rejected(self):
        transaction = Transaction("loan", (
            Posting.financial(Sector.BANK, FinancialInstrument.LOAN, 10),
            Posting.financial(Sector.FIRM, FinancialInstrument.LOAN, -10),
            Posting.financial(Sector.FIRM, FinancialInstrument.DEPOSIT, 10),
            Posting.financial(Sector.BANK, FinancialInstrument.DEPOSIT, -10),
            Posting.financial("rest_of_world", FinancialInstrument.DEPOSIT, 0),
        ))
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(transaction)
        self.assertEqual(context.exception.code, "unknown_sector")

    def test_unknown_account_and_kind_rejected(self):
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(Transaction("mystery_kind", (
                Posting.current(Sector.FIRM, CurrentFlow.OUTPUT, 0),)))
        self.assertEqual(context.exception.code, "unknown_kind")
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.apply(Transaction("loan", (
                Posting.financial(Sector.BANK, "mystery_account", 0),)))
        self.assertEqual(context.exception.code, "unknown_account")

    def test_invalid_amounts_rejected(self):
        for operation in (
            lambda: self.accounts.produce(True),
            lambda: self.accounts.produce("NaN"),
            lambda: self.accounts.produce("Infinity"),
            lambda: self.accounts.loan(""),
            lambda: self.accounts.pay_wages(-1),
            lambda: self.accounts.loan("-5"),
        ):
            with self.assertRaises(AccountingError):
                operation()
        with self.assertRaises(AccountingError):
            Posting.financial(Sector.BANK, FinancialInstrument.LOAN, True)
        with self.assertRaises(AccountingError):
            to_decimal(float("nan"))
        self.assertEqual(code_string(Sector.FIRM), "firm")
        self.assertEqual(to_decimal("10.25"), D("10.25"))

    def test_negative_net_worth_and_liabilities_are_allowed(self):
        accounts = scenario.baseline_scenario()
        report = accounts.close_period()
        self.assertLess(report.closing_net_worth[Sector.FIRM.value], 0)
        self.assertLess(accounts.balance(Sector.FIRM, FinancialInstrument.LOAN), 0)
        self.assertLess(accounts.balance(Sector.BANK, FinancialInstrument.DEPOSIT), 0)
        check_snapshot(accounts.snapshot())

    def test_revaluation_rollback_keeps_state(self):
        scenario.baseline_scenario(self.accounts)
        before = self._assert_unchanged()
        with self.assertRaises(TransactionRejected) as context:
            self.accounts.revalue_real(RealAsset.INVENTORY, -100)
        self.assertEqual(context.exception.code, "negative_real_asset")
        self.assertEqual(before, self._assert_unchanged())
        with self.assertRaises(TransactionRejected):
            self.accounts.revalue_financial(FinancialInstrument.BOND,
                                            {Sector.BANK: 5})
        self.assertEqual(before, self._assert_unchanged())

    def test_period_state_errors(self):
        with self.assertRaises(PeriodStateError):
            self.accounts.begin_period("P2")  # P1 is still open
        self.accounts.close_period()
        with self.assertRaises(PeriodStateError):
            self.accounts.loan(10)  # period closed
        with self.assertRaises(PeriodStateError):
            self.accounts.close_period()  # already closed
        self.accounts.begin_period("P2")
        self.accounts.loan(10)
        self.accounts.close_period()
        with self.assertRaises(PeriodStateError):
            self.accounts.begin_period("P2")  # duplicate label
        self.assertEqual(len(self.accounts.periods), 2)


class TestStage3Interface(unittest.TestCase):
    """The chart extension point and the two-book external mirroring contract."""

    def _extended_chart(self):
        deposit = DEFAULT_CHART.spec(Dimension.FINANCIAL,
                                     FinancialInstrument.DEPOSIT)
        return DEFAULT_CHART.extend(
            sectors=("row",),
            specs=(
                AccountSpec(
                    deposit.code, deposit.dimension,
                    posting_sectors=deposit.posting_sectors | {"row"},
                    positive_sectors=deposit.positive_sectors,
                    negative_sectors=deposit.negative_sectors,
                    mirrored=True,
                    asset_sectors=deposit.asset_sectors | {"row"},
                    liability_sectors=deposit.liability_sectors,
                ),
                AccountSpec(
                    "external_claim", Dimension.FINANCIAL,
                    posting_sectors=(Sector.HOUSEHOLD.value, "row"),
                    mirrored=True,
                    asset_sectors=(Sector.HOUSEHOLD.value,),
                    liability_sectors=("row",),
                ),
            ),
            kinds=("external_transaction",),
        )

    def test_chart_extension_keeps_global_mirroring(self):
        chart = self._extended_chart()
        self.assertIn("row", chart.sectors)
        self.assertIn("external_transaction", chart.kinds)
        self.assertIn("external_claim", chart.codes(Dimension.FINANCIAL))
        accounts = AccountSystem(
            chart,
            opening=OpeningBalances(financial={
                (Sector.HOUSEHOLD, FinancialInstrument.DEPOSIT): 50,
                (Sector.BANK, FinancialInstrument.DEPOSIT): -50,
            }),
        )
        accounts.apply(Transaction("external_transaction", (
            Posting.financial(Sector.HOUSEHOLD, FinancialInstrument.DEPOSIT, -50),
            Posting.financial("row", FinancialInstrument.DEPOSIT, 50),
            Posting.financial(Sector.HOUSEHOLD, "external_claim", 50),
            Posting.financial("row", "external_claim", -50),
        )))
        report = accounts.close_period()
        self.assertEqual(accounts.balance("row", "external_claim"), D(-50))
        self.assertEqual(accounts.balance(Sector.HOUSEHOLD, "external_claim"), D(50))
        self.assertEqual(sum(report.net_lending.values()), D(0))
        check_snapshot(accounts.snapshot(), chart)
        # A non-mirrored external position must be rejected by the stock check.
        with self.assertRaises(InvariantViolation):
            check_snapshot(Snapshot("X", {
                Sector.HOUSEHOLD.value: BalanceSheet(
                    Sector.HOUSEHOLD, {"external_claim": D(50)}, {}),
                "row": BalanceSheet("row", {"external_claim": D(-40)}, {}),
            }), chart)

    def test_cross_system_mirroring_contract(self):
        left = Snapshot("P1", {
            Sector.HOUSEHOLD.value: BalanceSheet(
                Sector.HOUSEHOLD, {"external_claim": D(50)}, {}),
            "row": BalanceSheet("row", {"external_claim": D(-50)}, {}),
        })
        right = Snapshot("P1", {
            Sector.GOVERNMENT.value: BalanceSheet(
                Sector.GOVERNMENT, {"external_claim": D(-50)}, {}),
            "row": BalanceSheet("row", {"external_claim": D(50)}, {}),
        })
        check_external_mirroring(left, right,
                                 instrument_pairs={"external_claim": "external_claim"})
        tampered = Snapshot("P1", {
            Sector.GOVERNMENT.value: BalanceSheet(
                Sector.GOVERNMENT, {"external_claim": D(-40)}, {}),
            "row": BalanceSheet("row", {"external_claim": D(50)}, {}),
        })
        with self.assertRaises(InvariantViolation) as context:
            check_external_mirroring(
                left, tampered,
                instrument_pairs={"external_claim": "external_claim"})
        self.assertEqual(context.exception.code, "world_net_position")


class TestReproducibleParameterGrid(unittest.TestCase):
    def test_parameter_grid_10000_is_reproducible(self):
        first = scenario.run_grid_10000()
        self.assertEqual(first.combinations, 10_000)
        self.assertEqual(first.net_lending_total, D(0))
        self.assertEqual(first.inventory_total, D(260_000))
        self.assertEqual(len(first.digest), 64)
        second = scenario.run_grid_10000()
        self.assertEqual(second.combinations, first.combinations)
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(second.net_lending_total, first.net_lending_total)


def chart_sector_codes():
    return {sector.value for sector in Sector}


if __name__ == "__main__":
    unittest.main(verbosity=2)
