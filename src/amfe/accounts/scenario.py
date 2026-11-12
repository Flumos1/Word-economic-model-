"""Reusable scenarios for the typed sector accounts (stage 2).

The scenarios reproduce the four-sector examples of docs/39, docs/40, docs/44
and root the 10,000-case reproducible parameter grid of
``tests/test_government_parameter_grid_v03.py`` on the typed kernel.  They are
reference data for other layers (stage 3 two-country prototype, future state
space models) and for backward-compatibility tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

from .accounts import AccountSystem
from .invariants import GovernmentBudget, check_government_budget
from .model import (
    AccountingError,
    FinancialInstrument,
    Sector,
    to_decimal,
)

# ---------------------------------------------------------------------------
# Documented reference outcomes
# ---------------------------------------------------------------------------
#: docs/44: final net worth of the four sectors in the baseline scenario.
BASELINE_NET_WORTH = {
    Sector.HOUSEHOLD.value: Decimal("40"),
    Sector.FIRM.value: Decimal("-35"),
    Sector.BANK.value: Decimal("5"),
    Sector.GOVERNMENT.value: Decimal("5"),
}
BASELINE_INVENTORY = Decimal("15")
BASELINE_GOVERNMENT_CASH = Decimal("5")
BASELINE_LOAN_OUTSTANDING = Decimal("50")
BASELINE_GDP = Decimal("100")

#: docs/40, debt-financed deficit variant (sales=70, tax=5, transfer=10,
#: public purchases=15, government borrowing=20).
DEBT_NET_WORTH = {
    Sector.HOUSEHOLD.value: Decimal("40"),
    Sector.FIRM.value: Decimal("-5"),
    Sector.BANK.value: Decimal("0"),
    Sector.GOVERNMENT.value: Decimal("-20"),
}
DEBT_BOND_LIABILITY = Decimal("-20")
DEBT_GOVERNMENT_CASH = Decimal("0")
DEBT_INVENTORY = Decimal("15")

#: Grid of tests/test_government_parameter_grid_v03.py (40 x 25 x 10 = 10 000).
GRID_COMBINATIONS = 10_000
GRID_SALES = range(50, 90)
GRID_TAX = range(25)
GRID_PUBLIC_PURCHASES = range(10)


def _system(system: AccountSystem | None) -> AccountSystem:
    if system is None:
        return AccountSystem()
    if not isinstance(system, AccountSystem):
        raise AccountingError("scenario expects an AccountSystem instance")
    return system


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def baseline_scenario(system: AccountSystem | None = None) -> AccountSystem:
    """docs/44 baseline: loan 100, output 100, wages 100, C=70, tax=30,
    transfer=10, G=15, interest=5, loan repayment 50."""
    accounts = _system(system)
    accounts.loan(100, memo="bank credit")
    accounts.produce(100, memo="production")
    accounts.pay_wages(100, memo="wages")
    accounts.household_purchase(70, memo="household consumption")
    accounts.pay_direct_tax(30, memo="direct firm levy")
    accounts.pay_transfer(10, payer=Sector.GOVERNMENT,
                          receiver=Sector.HOUSEHOLD, memo="social transfer")
    accounts.government_purchase(15, memo="government consumption")
    accounts.pay_interest(5, memo="interest")
    accounts.repay_loan(50, memo="loan repayment")
    return accounts


def inventory_scenario(system: AccountSystem | None = None,
                       purchases: object = 90) -> AccountSystem:
    """docs/39 second test: unsold output remains as inventory investment."""
    accounts = _system(system)
    sold = to_decimal(purchases, what="household purchases")
    accounts.loan(100, memo="bank credit")
    accounts.produce(100, memo="production")
    accounts.pay_wages(100, memo="wages")
    accounts.household_purchase(sold, memo="household consumption")
    return accounts


def government_debt_scenario(system: AccountSystem | None = None) -> AccountSystem:
    """docs/40 debt-financed deficit: tax 5, transfer 10, G 15, borrowing 20."""
    accounts = _system(system)
    accounts.loan(100, memo="bank credit")
    accounts.issue_bond(20, memo="government bond issue")
    accounts.produce(100, memo="production")
    accounts.pay_wages(100, memo="wages")
    accounts.household_purchase(70, memo="household consumption")
    accounts.pay_direct_tax(5, memo="direct firm levy")
    accounts.pay_transfer(10, payer=Sector.GOVERNMENT,
                          receiver=Sector.HOUSEHOLD, memo="social transfer")
    accounts.government_purchase(15, memo="government consumption")
    accounts.repay_loan(80, memo="loan repayment")
    return accounts


def closed_economy_cycle(system: AccountSystem | None = None,
                         purchases: object = 100,
                         repay: bool = True) -> AccountSystem:
    """docs/39 first kernel: H/F/B only, no government flows."""
    accounts = _system(system)
    sold = to_decimal(purchases, what="household purchases")
    accounts.loan(100, memo="bank credit")
    accounts.produce(100, memo="production")
    accounts.pay_wages(100, memo="wages")
    accounts.household_purchase(sold, memo="household consumption")
    if repay:
        outstanding = accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT)
        if outstanding > 0:
            accounts.repay_loan(outstanding, memo="loan repayment")
    return accounts


def accrual_scenario(system: AccountSystem | None = None) -> AccountSystem:
    """docs/47 accrual example: accrue 25, settle 10, hold 15 receivable."""
    accounts = _system(system)
    accounts.loan(100, memo="bank credit")
    accounts.accrue_wages(25, memo="accrued wages")
    accounts.settle_accrued_wages(10, memo="partial settlement")
    return accounts


# ---------------------------------------------------------------------------
# Reproducible 10,000-case parameter grid
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GridResult:
    """Outcome of the deterministic parameter grid."""

    combinations: int
    digest: str
    net_lending_total: Decimal
    inventory_total: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "combinations": str(self.combinations),
            "digest": self.digest,
            "net_lending_total": str(self.net_lending_total),
            "inventory_total": str(self.inventory_total),
        }


def iter_grid_parameters() -> Iterator[tuple[int, int, int, int, int]]:
    """Yield ``(sales, tax, public_purchases, transfer, borrowing)``.

    Mirrors the documented grid of ``tests/test_government_parameter_grid_v03.py``:
    40 sales values x 25 tax values x 10 public-purchase values = 10 000
    admissible, order-valid combinations (tax <= sales revenue before
    government purchases; transfer = tax // 2; borrowing covers the deficit).
    """
    for sales in GRID_SALES:
        for tax in GRID_TAX:
            for public_purchases in GRID_PUBLIC_PURCHASES:
                transfer = tax // 2
                borrowing = max(0, transfer + public_purchases - tax)
                yield (sales, tax, public_purchases, transfer, borrowing)


def run_grid_10000() -> GridResult:
    """Run the 10 000-case grid through the typed kernel and digest the results.

    Every combination is closed and checked by all report invariants.  The
    digest is a SHA-256 of the canonical per-case result lines, so two runs of
    the same code produce the same digest (reproducibility test).
    """
    lines: list[str] = []
    net_lending_total = Decimal(0)
    inventory_total = Decimal(0)
    checked = 0
    for sales, tax, public_purchases, transfer, borrowing in iter_grid_parameters():
        try:
            accounts = AccountSystem()
            accounts.loan(100, memo="credit")
            accounts.produce(100, memo="production")
            accounts.pay_wages(100, memo="wages")
            accounts.household_purchase(sales, memo="household consumption")
            accounts.pay_direct_tax(tax, memo="direct levy")
            if transfer:
                accounts.pay_transfer(transfer, payer=Sector.GOVERNMENT,
                                      receiver=Sector.HOUSEHOLD, memo="transfer")
            if borrowing:
                accounts.issue_bond(borrowing, memo="deficit financing")
            accounts.government_purchase(public_purchases,
                                         memo="government consumption")
            repay = min(Decimal(100),
                        accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT))
            if repay > 0:
                accounts.repay_loan(repay, memo="loan repayment")
            report = accounts.close_period()
            budget: GovernmentBudget = check_government_budget(report)
            expected_inventory = 100 - sales - public_purchases
            actual_inventory = report.inventory_change[Sector.FIRM.value]
            if actual_inventory != expected_inventory:
                raise AccountingError(
                    f"grid inventory mismatch: {actual_inventory} != "
                    f"{expected_inventory}", code="grid_inventory")
            expected_deficit = Decimal(transfer + public_purchases - tax)
            if budget.deficit != expected_deficit:
                raise AccountingError(
                    f"grid deficit mismatch: {budget.deficit} != "
                    f"{expected_deficit}", code="grid_deficit")
            if budget.net_borrowing != budget.deficit:
                raise AccountingError(
                    f"grid bond financing mismatch: {budget.net_borrowing} != "
                    f"{budget.deficit}", code="grid_borrowing")
            if budget.delta_debt != Decimal(borrowing):
                raise AccountingError(
                    f"grid debt mismatch: {budget.delta_debt} != {borrowing}",
                    code="grid_debt")
            if budget.revenue != Decimal(tax):
                raise AccountingError(
                    f"grid tax mismatch: {budget.revenue} != {tax}",
                    code="grid_tax")
            expected_expenditure = Decimal(transfer + public_purchases)
            if budget.expenditure != expected_expenditure:
                raise AccountingError(
                    f"grid expenditure mismatch: {budget.expenditure} != "
                    f"{expected_expenditure}", code="grid_expenditure")
            expected_cash = max(0, tax - transfer - public_purchases)
            if budget.delta_cash != Decimal(expected_cash):
                raise AccountingError(
                    f"grid cash mismatch: {budget.delta_cash} != {expected_cash}",
                    code="grid_cash")
            net_lending_total += sum(report.net_lending.values(), Decimal(0))
            inventory_total += actual_inventory
            closing = report.closing_net_worth
            lines.append("|".join(str(part) for part in (
                sales, tax, public_purchases, transfer, borrowing, repay,
                report.gdp_production, report.gdp_expenditure, report.gdp_income,
                closing[Sector.HOUSEHOLD.value], closing[Sector.FIRM.value],
                closing[Sector.BANK.value], closing[Sector.GOVERNMENT.value],
                actual_inventory, report.net_lending[Sector.FIRM.value],
                report.net_financial_change[Sector.FIRM.value],
            )))
            checked += 1
        except AccountingError as exc:
            raise AccountingError(
                f"grid case sales={sales} tax={tax} public={public_purchases} "
                f"transfer={transfer} borrowing={borrowing}: {exc}",
                code=getattr(exc, "code", "grid_failure")) from exc
    if checked != GRID_COMBINATIONS:
        raise AccountingError(
            f"expected {GRID_COMBINATIONS} combinations, got {checked}",
            code="grid_count")
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return GridResult(combinations=checked, digest=digest,
                      net_lending_total=net_lending_total,
                      inventory_total=inventory_total)


__all__ = [
    "BASELINE_NET_WORTH", "BASELINE_INVENTORY", "BASELINE_GOVERNMENT_CASH",
    "BASELINE_LOAN_OUTSTANDING", "BASELINE_GDP",
    "DEBT_NET_WORTH", "DEBT_BOND_LIABILITY", "DEBT_GOVERNMENT_CASH",
    "DEBT_INVENTORY",
    "GRID_COMBINATIONS", "GRID_SALES", "GRID_TAX", "GRID_PUBLIC_PURCHASES",
    "GridResult", "iter_grid_parameters", "run_grid_10000",
    "baseline_scenario", "inventory_scenario", "government_debt_scenario",
    "closed_economy_cycle", "accrual_scenario",
]
