"""Typed stock-flow sector accounts for AMFE-17 stage 2.

Public API of the accounting kernel:

* :class:`~amfe.accounts.accounts.AccountSystem` -- atomic four-sector ledger
  with current/capital/real/financial accounts, signed positions, net worth,
  revaluations and period reports;
* :class:`~amfe.accounts.model.ChartOfAccounts` -- typed chart of accounts and
  the extension point for stage 3 (two countries, two currencies);
* :func:`~amfe.accounts.invariants.check_report` -- the independent invariant
  suite (financial mirroring, ``dNW = savings + revaluation``, government
  budget, closed-economy residuals, GDP identity);
* :mod:`amfe.accounts.scenario` -- documented scenarios and the reproducible
  10 000-case parameter grid.

The package deliberately leaves the prototypes in ``src/amfe/accounting_core.py``
and ``src/amfe/extended_accounts.py`` untouched; the references there are
reproduced and cross-checked by ``tests/test_accounts.py``.
"""

from .accounts import AccountSystem, OpeningBalances
from .invariants import (
    GovernmentBudget,
    check_closed_economy,
    check_external_mirroring,
    check_financial_accounts,
    check_financial_mirror,
    check_gdp_identity,
    check_government_budget,
    check_period_identity,
    check_report,
    check_snapshot,
    check_stock_signs,
)
from .model import (
    AccountSpec,
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
    NonTransactionEntry,
    PeriodStateError,
    PeriodReport,
    Posting,
    RealAsset,
    Sector,
    Snapshot,
    Transaction,
    TransactionKind,
    TransactionRejected,
    build_default_chart,
    code_string,
    to_decimal,
)

__all__ = [
    # ledger
    "AccountSystem", "OpeningBalances",
    # types
    "Sector", "Dimension", "CurrentFlow", "CapitalFlow", "RealAsset",
    "FinancialInstrument", "TransactionKind",
    "Posting", "Transaction", "AccountSpec", "ChartOfAccounts",
    "DEFAULT_CHART", "build_default_chart",
    "BalanceSheet", "Snapshot", "JournalEntry", "NonTransactionEntry",
    "PeriodReport",
    # errors and helpers
    "AccountingError", "TransactionRejected", "InvariantViolation",
    "PeriodStateError", "to_decimal", "code_string",
    # invariants
    "GovernmentBudget", "check_stock_signs", "check_financial_mirror",
    "check_snapshot", "check_period_identity", "check_financial_accounts",
    "check_closed_economy", "check_government_budget", "check_gdp_identity",
    "check_report", "check_external_mirroring",
]
