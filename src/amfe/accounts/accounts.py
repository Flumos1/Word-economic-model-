"""Atomic sector-accounts ledger for AMFE-17 stage 2.

:class:`AccountSystem` maintains the signed financial positions and real assets
of the four institutional sectors, applies atomic double-entry transactions
with full rollback, records revaluations/other volume changes and produces a
typed :class:`~amfe.accounts.model.PeriodReport` whose invariants are checked
independently by :mod:`amfe.accounts.invariants`.

Atomicity
---------
``apply`` validates the whole transaction and the resulting stocks against a
copy of the state.  Only when every check passes are the postings committed and
the journal entry appended.  A rejected transaction leaves balances, the
journal and the period state exactly as they were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from . import invariants
from .model import (
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
    PeriodReport,
    PeriodStateError,
    Posting,
    RealAsset,
    Sector,
    Snapshot,
    Transaction,
    TransactionKind,
    TransactionRejected,
    to_decimal,
    code_string,
)


_ZERO = Decimal(0)


def _nonnegative(value: object, *, what: str) -> Decimal:
    amount = to_decimal(value, what=what)
    if amount < 0:
        raise TransactionRejected(f"negative {what}: {amount}", code="negative_amount")
    return amount


@dataclass(frozen=True)
class OpeningBalances:
    """Initial stocks of the accounting system (no journal transaction).

    Keys are ``(sector, instrument)`` for financial positions and
    ``(sector, asset)`` for real assets.  The opening financial positions must
    already mirror each other (sum to zero per instrument).
    """

    financial: Mapping[tuple[object, object], object] = field(default_factory=dict)
    real: Mapping[tuple[object, object], object] = field(default_factory=dict)


class AccountSystem:
    """A one-country stock-flow-consistent ledger of four institutional sectors."""

    def __init__(
        self,
        chart: ChartOfAccounts = DEFAULT_CHART,
        period: str = "P1",
        *,
        opening: OpeningBalances | None = None,
    ) -> None:
        if not isinstance(chart, ChartOfAccounts):
            raise AccountingError("chart must be a ChartOfAccounts instance")
        self._chart = chart
        self._financial: dict[tuple[str, str], Decimal] = {}
        self._real: dict[tuple[str, str], Decimal] = {}
        self._journal: list[JournalEntry] = []
        self._nontransaction: list[NonTransactionEntry] = []
        self._periods: list[PeriodReport] = []
        self._last_report: PeriodReport | None = None
        self._period = code_string(period, what="period")
        self._period_open = True
        self._period_start = 0
        self._nt_start = 0
        self._sequence = 0
        if opening is not None:
            self._load_opening(opening)
        self._opening_nw = {sector: self.net_worth(sector) for sector in chart.sectors}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def chart(self) -> ChartOfAccounts:
        return self._chart

    @property
    def period(self) -> str:
        return self._period

    @property
    def period_open(self) -> bool:
        return self._period_open

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return tuple(self._journal)

    @property
    def nontransaction(self) -> tuple[NonTransactionEntry, ...]:
        return tuple(self._nontransaction)

    @property
    def periods(self) -> tuple[PeriodReport, ...]:
        return tuple(self._periods)

    @property
    def last_report(self) -> PeriodReport | None:
        return self._last_report

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"AccountSystem(period={self._period!r}, open={self._period_open}, "
                f"entries={len(self._journal)}, periods={len(self._periods)})")

    # ------------------------------------------------------------------
    # Stock accessors
    # ------------------------------------------------------------------
    def balance(self, sector: object, instrument: object) -> Decimal:
        spec = self._chart.spec(Dimension.FINANCIAL, instrument)
        sector_code = self._chart.require_sector(sector)
        if sector_code not in spec.posting_sectors:
            return _ZERO
        return self._financial.get((sector_code, spec.code), _ZERO)

    def real_balance(self, sector: object, asset: object) -> Decimal:
        spec = self._chart.spec(Dimension.REAL, asset)
        sector_code = self._chart.require_sector(sector)
        if sector_code not in spec.posting_sectors:
            return _ZERO
        return self._real.get((sector_code, spec.code), _ZERO)

    def net_financial(self, sector: object) -> Decimal:
        sector_code = self._chart.require_sector(sector)
        return sum((amount for (code, _), amount in self._financial.items()
                    if code == sector_code), _ZERO)

    def net_worth(self, sector: object) -> Decimal:
        sector_code = self._chart.require_sector(sector)
        return self.net_financial(sector_code) + sum(
            (amount for (code, _), amount in self._real.items()
             if code == sector_code), _ZERO)

    def balance_sheet(self, sector: object) -> BalanceSheet:
        sector_code = self._chart.require_sector(sector)
        return BalanceSheet(
            sector=sector_code,
            financial={instrument: amount
                       for (code, instrument), amount in self._financial.items()
                       if code == sector_code},
            real={asset: amount
                  for (code, asset), amount in self._real.items()
                  if code == sector_code},
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(period=self._period, balances={
            sector: self.balance_sheet(sector) for sector in self._chart.sectors})

    def financial_items(self) -> Mapping[tuple[str, str], Decimal]:
        return MappingProxyType(dict(self._financial))

    def real_items(self) -> Mapping[tuple[str, str], Decimal]:
        return MappingProxyType(dict(self._real))

    # ------------------------------------------------------------------
    # Atomic transaction application
    # ------------------------------------------------------------------
    def apply(self, transaction: Transaction) -> JournalEntry:
        """Apply one atomic transaction or reject it without side effects."""
        if not self._period_open:
            raise PeriodStateError(
                f"period {self._period} is closed; begin a new period first")
        if not isinstance(transaction, Transaction):
            raise AccountingError("apply expects a Transaction instance")
        self._chart.require_kind(transaction.kind)

        totals_by_code: dict[tuple[Dimension, str], Decimal] = {}
        current_sums: dict[str, Decimal] = {}
        capital_sums: dict[str, Decimal] = {}
        real_sums: dict[str, Decimal] = {}
        financial_sums: dict[str, Decimal] = {}
        updates_financial: dict[tuple[str, str], Decimal] = {}
        updates_real: dict[tuple[str, str], Decimal] = {}
        sectors = self._chart.sectors
        lookup = self._chart.lookup
        for posting in transaction.postings:
            sector = posting.sector
            dimension = posting.dimension
            code = posting.code
            amount = posting.amount
            if sector not in sectors:
                raise TransactionRejected(
                    f"unknown sector: {sector}", code="unknown_sector",
                    details={"sector": sector})
            spec = lookup(dimension, code)
            if spec is None:
                # Slow path: produce the documented unknown-account error.
                self._chart.spec(dimension, code)
                continue  # pragma: no cover - defensive
            role_ok = (sector in spec.posting_sectors
                       and (dimension is Dimension.FINANCIAL
                            or (amount > 0 and sector in spec.positive_sectors)
                            or (amount < 0 and sector in spec.negative_sectors)
                            or amount == 0))
            if not role_ok:
                raise TransactionRejected(
                    f"{sector} may not post {amount} to "
                    f"{dimension.value}/{code}",
                    code="posting_role",
                    details={"sector": sector,
                             "dimension": dimension.value,
                             "code": code,
                             "amount": str(amount)})
            if spec.mirrored:
                key = (dimension, code)
                totals_by_code[key] = totals_by_code.get(key, _ZERO) + amount
            if dimension is Dimension.CURRENT:
                current_sums[sector] = current_sums.get(sector, _ZERO) + amount
            elif dimension is Dimension.CAPITAL:
                capital_sums[sector] = capital_sums.get(sector, _ZERO) + amount
            elif dimension is Dimension.REAL:
                real_sums[sector] = real_sums.get(sector, _ZERO) + amount
                key = (sector, code)
                updates_real[key] = updates_real.get(key, _ZERO) + amount
            else:
                financial_sums[sector] = financial_sums.get(sector, _ZERO) + amount
                key = (sector, code)
                updates_financial[key] = updates_financial.get(key, _ZERO) + amount

        for (dimension, code), total in totals_by_code.items():
            if total != 0:
                raise TransactionRejected(
                    f"unbalanced {dimension.value} account {code}: {total}",
                    code="unbalanced_transaction",
                    details={"dimension": dimension.value, "code": code,
                             "total": str(total)})

        touched = set(current_sums) | set(capital_sums) | set(real_sums) | set(financial_sums)
        for sector in touched:
            current = current_sums.get(sector, _ZERO)
            capital = capital_sums.get(sector, _ZERO)
            real = real_sums.get(sector, _ZERO)
            financial = financial_sums.get(sector, _ZERO)
            if current + capital != real + financial:
                raise TransactionRejected(
                    f"sector closure violated for {sector}: "
                    f"current({current}) + capital({capital})"
                    f" != real({real}) + financial({financial})",
                    code="sector_closure",
                    details={"sector": sector,
                             "current": str(current),
                             "capital": str(capital),
                             "real": str(real),
                             "financial": str(financial)})

        new_financial = dict(self._financial)
        new_real = dict(self._real)
        for key, amount in updates_financial.items():
            if amount == 0:
                continue
            new_value = new_financial.get(key, _ZERO) + amount
            if new_value == 0:
                new_financial.pop(key, None)
            else:
                new_financial[key] = new_value
        for key, amount in updates_real.items():
            if amount == 0:
                continue
            new_value = new_real.get(key, _ZERO) + amount
            if new_value == 0:
                new_real.pop(key, None)
            else:
                new_real[key] = new_value
        try:
            invariants.check_stock_signs(new_financial, new_real, self._chart)
        except InvariantViolation as exc:
            raise TransactionRejected(str(exc), code=exc.code,
                                      details=exc.details) from exc

        self._financial = new_financial
        self._real = new_real
        self._sequence += 1
        entry = JournalEntry(
            sequence=self._sequence,
            period=self._period,
            kind=transaction.kind,
            postings=transaction.postings,
            memo=transaction.memo,
        )
        self._journal.append(entry)
        return entry

    @staticmethod
    def _accumulate(balances: dict[tuple[str, str], Decimal],
                    key: tuple[str, str], amount: Decimal) -> None:
        if amount == 0:
            return
        new_value = balances.get(key, _ZERO) + amount
        if new_value == 0:
            balances.pop(key, None)
        else:
            balances[key] = new_value

    def _post(self, kind: object, postings: Iterable[Posting], memo: str = "") -> JournalEntry:
        return self.apply(Transaction(kind, tuple(postings), memo))

    # ------------------------------------------------------------------
    # Standard transactions (default chart semantics)
    # ------------------------------------------------------------------
    def loan(self, amount: object, *, borrower: object = Sector.FIRM,
             lender: object = Sector.BANK, memo: str = "") -> JournalEntry:
        """Bank credit creation: a loan asset, a loan liability and a deposit."""
        n = _nonnegative(amount, what="loan")
        return self._post(TransactionKind.LOAN, (
            Posting.financial(lender, FinancialInstrument.LOAN, n),
            Posting.financial(borrower, FinancialInstrument.LOAN, -n),
            Posting.financial(borrower, FinancialInstrument.DEPOSIT, n),
            Posting.financial(lender, FinancialInstrument.DEPOSIT, -n),
        ), memo)

    def repay_loan(self, amount: object, *, borrower: object = Sector.FIRM,
                   lender: object = Sector.BANK, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="loan repayment")
        return self._post(TransactionKind.LOAN_REPAYMENT, (
            Posting.financial(borrower, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(lender, FinancialInstrument.DEPOSIT, n),
            Posting.financial(lender, FinancialInstrument.LOAN, -n),
            Posting.financial(borrower, FinancialInstrument.LOAN, n),
        ), memo)

    def issue_bond(self, amount: object, *, issuer: object = Sector.GOVERNMENT,
                   holder: object = Sector.BANK, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="bond issue")
        return self._post(TransactionKind.BOND_ISSUE, (
            Posting.financial(holder, FinancialInstrument.BOND, n),
            Posting.financial(issuer, FinancialInstrument.BOND, -n),
            Posting.financial(issuer, FinancialInstrument.DEPOSIT, n),
            Posting.financial(holder, FinancialInstrument.DEPOSIT, -n),
        ), memo)

    def repay_bond(self, amount: object, *, issuer: object = Sector.GOVERNMENT,
                   holder: object = Sector.BANK, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="bond repayment")
        return self._post(TransactionKind.BOND_REPAYMENT, (
            Posting.financial(issuer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(holder, FinancialInstrument.DEPOSIT, n),
            Posting.financial(holder, FinancialInstrument.BOND, -n),
            Posting.financial(issuer, FinancialInstrument.BOND, n),
        ), memo)

    def produce(self, amount: object, *, producer: object = Sector.FIRM,
                memo: str = "") -> JournalEntry:
        """Produce output: a current receipt and a real inventory increase."""
        n = _nonnegative(amount, what="output")
        return self._post(TransactionKind.PRODUCTION, (
            Posting.current(producer, CurrentFlow.OUTPUT, n),
            Posting.real(producer, RealAsset.INVENTORY, n),
        ), memo)

    def pay_wages(self, amount: object, *, payer: object = Sector.FIRM,
                  worker: object = Sector.HOUSEHOLD, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="wages")
        return self._post(TransactionKind.WAGES, (
            Posting.current(payer, CurrentFlow.WAGES, -n),
            Posting.current(worker, CurrentFlow.WAGES, n),
            Posting.financial(payer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(worker, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def pay_direct_tax(self, amount: object, *, payer: object = Sector.FIRM,
                       collector: object = Sector.GOVERNMENT,
                       memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="direct tax")
        return self._post(TransactionKind.DIRECT_TAX, (
            Posting.current(payer, CurrentFlow.DIRECT_TAX, -n),
            Posting.current(collector, CurrentFlow.DIRECT_TAX, n),
            Posting.financial(payer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(collector, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def pay_transfer(self, amount: object, *, payer: object, receiver: object,
                     memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="transfer")
        payer_code = code_string(payer, what="payer")
        receiver_code = code_string(receiver, what="receiver")
        if payer_code == receiver_code:
            raise TransactionRejected("transfer payer and receiver must differ",
                                      code="self_transfer")
        return self._post(TransactionKind.TRANSFER, (
            Posting.current(payer, CurrentFlow.TRANSFER, -n),
            Posting.current(receiver, CurrentFlow.TRANSFER, n),
            Posting.financial(payer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(receiver, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def pay_interest(self, amount: object, *, debtor: object = Sector.FIRM,
                     creditor: object = Sector.BANK, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="interest")
        return self._post(TransactionKind.INTEREST, (
            Posting.current(debtor, CurrentFlow.INTEREST, -n),
            Posting.current(creditor, CurrentFlow.INTEREST, n),
            Posting.financial(debtor, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(creditor, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def household_purchase(self, amount: object, *,
                           buyer: object = Sector.HOUSEHOLD,
                           seller: object = Sector.FIRM,
                           memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="household purchase")
        return self._post(TransactionKind.HOUSEHOLD_PURCHASE, (
            Posting.current(buyer, CurrentFlow.HOUSEHOLD_CONSUMPTION, -n),
            Posting.financial(buyer, FinancialInstrument.DEPOSIT, -n),
            Posting.real(seller, RealAsset.INVENTORY, -n),
            Posting.financial(seller, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def government_purchase(self, amount: object, *,
                            buyer: object = Sector.GOVERNMENT,
                            seller: object = Sector.FIRM,
                            memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="government purchase")
        return self._post(TransactionKind.GOVERNMENT_PURCHASE, (
            Posting.current(buyer, CurrentFlow.GOVERNMENT_CONSUMPTION, -n),
            Posting.financial(buyer, FinancialInstrument.DEPOSIT, -n),
            Posting.real(seller, RealAsset.INVENTORY, -n),
            Posting.financial(seller, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def capital_transfer(self, amount: object, *, payer: object,
                         receiver: object, memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="capital transfer")
        payer_code = code_string(payer, what="payer")
        receiver_code = code_string(receiver, what="receiver")
        if payer_code == receiver_code:
            raise TransactionRejected("capital transfer parties must differ",
                                      code="self_transfer")
        return self._post(TransactionKind.CAPITAL_TRANSFER, (
            Posting.capital(payer, CapitalFlow.CAPITAL_TRANSFER, -n),
            Posting.capital(receiver, CapitalFlow.CAPITAL_TRANSFER, n),
            Posting.financial(payer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(receiver, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def accrue_wages(self, amount: object, *, employer: object = Sector.FIRM,
                     worker: object = Sector.HOUSEHOLD,
                     memo: str = "") -> JournalEntry:
        """Accrue wages: a receivable/liability appears, no deposit moves."""
        n = _nonnegative(amount, what="accrued wages")
        return self._post(TransactionKind.ACCRUED_WAGES, (
            Posting.current(employer, CurrentFlow.WAGES, -n),
            Posting.current(worker, CurrentFlow.WAGES, n),
            Posting.financial(employer, FinancialInstrument.RECEIVABLE, -n),
            Posting.financial(worker, FinancialInstrument.RECEIVABLE, n),
        ), memo)

    def settle_accrued_wages(self, amount: object, *, employer: object = Sector.FIRM,
                             worker: object = Sector.HOUSEHOLD,
                             memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="accrued wages settlement")
        return self._post(TransactionKind.SETTLE_ACCRUED_WAGES, (
            Posting.financial(employer, FinancialInstrument.RECEIVABLE, n),
            Posting.financial(worker, FinancialInstrument.RECEIVABLE, -n),
            Posting.financial(employer, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(worker, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def invest_fixed_capital(self, amount: object, *,
                             investor: object = Sector.FIRM,
                             memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="fixed investment")
        return self._post(TransactionKind.FIXED_INVESTMENT, (
            Posting.real(investor, RealAsset.INVENTORY, -n),
            Posting.real(investor, RealAsset.FIXED_CAPITAL, n),
        ), memo)

    def depreciate(self, amount: object, *, owner: object = Sector.FIRM,
                   memo: str = "") -> JournalEntry:
        n = _nonnegative(amount, what="depreciation")
        return self._post(TransactionKind.DEPRECIATION, (
            Posting.current(owner, CurrentFlow.DEPRECIATION, -n),
            Posting.real(owner, RealAsset.FIXED_CAPITAL, -n),
        ), memo)

    # ------------------------------------------------------------------
    # Non-transaction stock changes
    # ------------------------------------------------------------------
    def _apply_nontransaction(
        self,
        category: str,
        changes: Sequence[tuple[str, Dimension, str, Decimal]],
    ) -> tuple[NonTransactionEntry, ...]:
        if not self._period_open:
            raise PeriodStateError(
                f"period {self._period} is closed; begin a new period first")
        if category not in ("revaluation", "other_volume"):
            raise AccountingError(f"unknown non-transaction category: {category}")
        new_financial = dict(self._financial)
        new_real = dict(self._real)
        recorded: list[NonTransactionEntry] = []
        next_sequence = self._sequence + 1
        for sector, dimension, code, amount in changes:
            sector = self._chart.require_sector(sector)
            self._chart.spec(dimension, code)
            if amount == 0:
                continue
            if dimension is Dimension.FINANCIAL:
                self._accumulate(new_financial, (sector, code), amount)
            else:
                self._accumulate(new_real, (sector, code), amount)
            recorded.append(NonTransactionEntry(
                sequence=next_sequence, period=self._period, category=category,
                sector=sector, dimension=dimension, code=code, amount=amount))
            next_sequence += 1
        if not recorded:
            raise TransactionRejected("non-transaction change is empty",
                                      code="empty_change")
        try:
            invariants.check_stock_signs(new_financial, new_real, self._chart)
        except InvariantViolation as exc:
            raise TransactionRejected(str(exc), code=exc.code,
                                      details=exc.details) from exc
        if any(entry.dimension is Dimension.FINANCIAL for entry in recorded):
            totals: dict[str, Decimal] = {}
            for entry in recorded:
                if entry.dimension is Dimension.FINANCIAL:
                    totals[entry.code] = totals.get(entry.code, _ZERO) + entry.amount
            for code, total in totals.items():
                if total != 0:
                    raise TransactionRejected(
                        f"unbalanced financial revaluation of {code}: {total}",
                        code="unbalanced_transaction",
                        details={"instrument": code, "total": str(total)})
        self._financial = new_financial
        self._real = new_real
        self._nontransaction.extend(recorded)
        self._sequence = next_sequence - 1
        return tuple(recorded)

    def revalue_real(self, asset: object, delta: object, *,
                     sector: object = Sector.FIRM) -> tuple[NonTransactionEntry, ...]:
        """Revalue a real asset without a transaction (holding gain/loss)."""
        amount = to_decimal(delta, what="revaluation")
        return self._apply_nontransaction("revaluation", (
            (code_string(sector, what="sector"), Dimension.REAL,
             code_string(asset, what="asset"), amount),))

    def other_volume_change_real(
        self, asset: object, delta: object, *, sector: object = Sector.FIRM,
    ) -> tuple[NonTransactionEntry, ...]:
        """Record an other volume change of a real asset (loss/write-off)."""
        amount = to_decimal(delta, what="other volume change")
        return self._apply_nontransaction("other_volume", (
            (code_string(sector, what="sector"), Dimension.REAL,
             code_string(asset, what="asset"), amount),))

    def revalue_financial(
        self, instrument: object, changes: Mapping[object, object],
    ) -> tuple[NonTransactionEntry, ...]:
        """Revalue a financial instrument with mirrored sector write-ups/downs."""
        instrument_code = code_string(instrument, what="instrument")
        if not isinstance(changes, Mapping):
            raise AccountingError("financial revaluation expects a mapping")
        rows: list[tuple[str, Dimension, str, Decimal]] = []
        total = Decimal(0)
        for sector, delta in changes.items():
            amount = to_decimal(delta, what="financial revaluation")
            total += amount
            rows.append((code_string(sector, what="sector"), Dimension.FINANCIAL,
                         instrument_code, amount))
        if total != 0:
            raise TransactionRejected(
                f"financial revaluation of {instrument_code} must be mirrored; "
                f"sum is {total}", code="unbalanced_transaction",
                details={"instrument": instrument_code, "total": str(total)})
        if not rows:
            raise TransactionRejected("financial revaluation is empty",
                                      code="empty_change")
        return self._apply_nontransaction("revaluation", rows)

    # ------------------------------------------------------------------
    # Period lifecycle
    # ------------------------------------------------------------------
    def close_period(self) -> PeriodReport:
        """Close the open period, check all invariants and return the report."""
        if not self._period_open:
            raise PeriodStateError(f"period {self._period} is already closed")
        entries = tuple(self._journal[self._period_start:])
        nontransaction = tuple(self._nontransaction[self._nt_start:])
        report = self._build_report(entries, nontransaction)
        try:
            invariants.check_report(report, self._chart)
        except InvariantViolation as exc:
            raise InvariantViolation(
                f"period {self._period} does not close: {exc}",
                code=exc.code, details=exc.details) from exc
        self._period_open = False
        self._last_report = report
        self._periods.append(report)
        return report

    def begin_period(self, period: str) -> None:
        """Open the next period; the closing stocks become the opening stocks."""
        if self._period_open:
            raise PeriodStateError(
                f"period {self._period} is still open; close it before starting a new one")
        label = code_string(period, what="period")
        if any(report.period == label for report in self._periods):
            raise PeriodStateError(f"period {label} already exists")
        self._period = label
        self._period_open = True
        self._period_start = len(self._journal)
        self._nt_start = len(self._nontransaction)
        self._opening_nw = {
            sector: self.net_worth(sector) for sector in self._chart.sectors}

    def assert_invariants(self) -> None:
        """Check stock invariants now and period invariants of the last report."""
        invariants.check_snapshot(self.snapshot(), self._chart)
        if self._last_report is not None:
            invariants.check_report(self._last_report, self._chart)

    # ------------------------------------------------------------------
    # Report construction
    # ------------------------------------------------------------------
    def _build_report(
        self,
        entries: Sequence[JournalEntry],
        nontransaction: Sequence[NonTransactionEntry],
    ) -> PeriodReport:
        sectors = tuple(self._chart.sectors)
        current: dict[str, dict[str, Decimal]] = {sector: {} for sector in sectors}
        capital_transfers = {sector: _ZERO for sector in sectors}
        real_change: dict[str, dict[str, Decimal]] = {sector: {} for sector in sectors}
        financial_change: dict[str, dict[str, Decimal]] = {sector: {} for sector in sectors}
        output = _ZERO
        household_consumption = _ZERO
        government_consumption = _ZERO
        wages_received = _ZERO

        for entry in entries:
            for posting in entry.postings:
                sector = posting.sector
                amount = posting.amount
                if posting.dimension is Dimension.CURRENT:
                    detail = current.setdefault(sector, {})
                    detail[posting.code] = detail.get(posting.code, _ZERO) + amount
                    if posting.code == CurrentFlow.OUTPUT.value and amount > 0:
                        output += amount
                    elif posting.code == CurrentFlow.HOUSEHOLD_CONSUMPTION.value:
                        household_consumption -= amount
                    elif posting.code == CurrentFlow.GOVERNMENT_CONSUMPTION.value:
                        government_consumption -= amount
                    elif posting.code == CurrentFlow.WAGES.value and amount > 0:
                        wages_received += amount
                elif posting.dimension is Dimension.CAPITAL:
                    capital_transfers[sector] = (
                        capital_transfers.get(sector, _ZERO) + amount)
                elif posting.dimension is Dimension.REAL:
                    detail = real_change.setdefault(sector, {})
                    detail[posting.code] = detail.get(posting.code, _ZERO) + amount
                else:
                    detail = financial_change.setdefault(sector, {})
                    detail[posting.code] = detail.get(posting.code, _ZERO) + amount

        savings = {sector: sum(current.get(sector, {}).values(), _ZERO)
                   for sector in sectors}
        consumption_of_fixed_capital = {
            sector: -current.get(sector, {}).get(CurrentFlow.DEPRECIATION.value, _ZERO)
            for sector in sectors}
        gross_saving = {sector: savings[sector] + consumption_of_fixed_capital[sector]
                        for sector in sectors}
        inventory_change = {
            sector: real_change.get(sector, {}).get(RealAsset.INVENTORY.value, _ZERO)
            for sector in sectors}
        fixed_capital_change = {
            sector: real_change.get(sector, {}).get(RealAsset.FIXED_CAPITAL.value, _ZERO)
            for sector in sectors}
        gross_fixed_capital_formation = {sector: _ZERO for sector in sectors}
        depreciation_kind = TransactionKind.DEPRECIATION.value
        for entry in entries:
            if entry.kind == depreciation_kind:
                continue
            for posting in entry.postings:
                if (posting.dimension is Dimension.REAL
                        and posting.code == RealAsset.FIXED_CAPITAL.value):
                    gross_fixed_capital_formation[posting.sector] = (
                        gross_fixed_capital_formation.get(posting.sector, _ZERO)
                        + posting.amount)
        capital_formation = {
            sector: inventory_change[sector] + gross_fixed_capital_formation[sector]
            for sector in sectors}
        net_lending = {
            sector: gross_saving[sector] + capital_transfers[sector]
            - capital_formation[sector]
            for sector in sectors}
        net_financial_change = {
            sector: sum(financial_change.get(sector, {}).values(), _ZERO)
            for sector in sectors}

        revaluation_real = {sector: _ZERO for sector in sectors}
        revaluation_financial = {sector: _ZERO for sector in sectors}
        other_volume = {sector: _ZERO for sector in sectors}
        for entry in nontransaction:
            if entry.category == "revaluation":
                if entry.dimension is Dimension.REAL:
                    revaluation_real[entry.sector] += entry.amount
                else:
                    revaluation_financial[entry.sector] += entry.amount
            else:
                other_volume[entry.sector] += entry.amount
        revaluation = {
            sector: revaluation_real[sector] + revaluation_financial[sector]
            for sector in sectors}

        firm = Sector.FIRM.value
        firm_detail = current.get(firm, {})
        gross_operating_surplus = (firm_detail.get(CurrentFlow.OUTPUT.value, _ZERO)
                                   + firm_detail.get(CurrentFlow.WAGES.value, _ZERO))

        snapshot = self.snapshot()
        closing_net_worth = {sector: snapshot.net_worth(sector) for sector in sectors}
        inventory_total = sum(inventory_change.values(), _ZERO)
        gfcf_total = sum(gross_fixed_capital_formation.values(), _ZERO)
        gdp_production = output
        gdp_expenditure = (household_consumption + government_consumption
                           + inventory_total + gfcf_total)
        gdp_income = wages_received + gross_operating_surplus

        return PeriodReport(
            period=self._period,
            opening_net_worth={sector: self._opening_nw.get(sector, _ZERO)
                               for sector in sectors},
            closing_net_worth=closing_net_worth,
            current=current,
            savings=savings,
            consumption_of_fixed_capital=consumption_of_fixed_capital,
            gross_saving=gross_saving,
            capital_transfers=capital_transfers,
            real_change=real_change,
            inventory_change=inventory_change,
            fixed_capital_change=fixed_capital_change,
            gross_fixed_capital_formation=gross_fixed_capital_formation,
            capital_formation=capital_formation,
            financial_change=financial_change,
            net_financial_change=net_financial_change,
            net_lending=net_lending,
            revaluation_real=revaluation_real,
            revaluation_financial=revaluation_financial,
            revaluation=revaluation,
            other_volume=other_volume,
            output=gdp_production,
            household_consumption=household_consumption,
            government_consumption=government_consumption,
            wages_received=wages_received,
            gross_operating_surplus=gross_operating_surplus,
            gdp_production=gdp_production,
            gdp_expenditure=gdp_expenditure,
            gdp_income=gdp_income,
            entries=entries,
            nontransaction=nontransaction,
            snapshot=snapshot,
        )

    # ------------------------------------------------------------------
    # Opening balances and export
    # ------------------------------------------------------------------
    def _load_opening(self, opening: OpeningBalances) -> None:
        if not isinstance(opening, OpeningBalances):
            raise AccountingError("opening must be an OpeningBalances instance")
        financial: dict[tuple[str, str], Decimal] = {}
        real: dict[tuple[str, str], Decimal] = {}
        for (sector, instrument), amount in dict(opening.financial).items():
            sector_code = self._chart.require_sector(sector)
            spec = self._chart.spec(Dimension.FINANCIAL, instrument)
            value = to_decimal(amount, what="opening financial position")
            if value != 0:
                financial[(sector_code, spec.code)] = value
        for (sector, asset), amount in dict(opening.real).items():
            sector_code = self._chart.require_sector(sector)
            spec = self._chart.spec(Dimension.REAL, asset)
            value = to_decimal(amount, what="opening real asset")
            if value != 0:
                real[(sector_code, spec.code)] = value
        try:
            invariants.check_financial_mirror(financial, self._chart)
            invariants.check_stock_signs(financial, real, self._chart)
        except InvariantViolation as exc:
            raise AccountingError(
                f"invalid opening balances: {exc}", code=exc.code,
                details=exc.details) from exc
        self._financial = financial
        self._real = real

    def export(self) -> dict[str, object]:
        """JSON-ready export of the chart, journal, snapshot and period reports."""
        return {
            "chart": {
                "sectors": sorted(self._chart.sectors),
                "kinds": sorted(self._chart.kinds),
                "accounts": {
                    dimension.value: {
                        spec.code: {
                            "posting_sectors": sorted(spec.posting_sectors),
                            "positive_sectors": sorted(spec.positive_sectors),
                            "negative_sectors": sorted(spec.negative_sectors),
                            "mirrored": spec.mirrored,
                            "asset_sectors": sorted(spec.asset_sectors),
                            "liability_sectors": sorted(spec.liability_sectors),
                        }
                        for spec in self._chart.specs.values()
                        if spec.dimension is dimension}
                    for dimension in Dimension},
            },
            "period": self._period,
            "period_open": self._period_open,
            "journal": [
                {"sequence": entry.sequence, "period": entry.period,
                 "kind": entry.kind, "memo": entry.memo,
                 "postings": [
                     {"sector": posting.sector,
                      "dimension": posting.dimension.value,
                      "code": posting.code,
                      "amount": str(posting.amount)}
                     for posting in entry.postings]}
                for entry in self._journal],
            "nontransaction": [
                {"sequence": entry.sequence, "period": entry.period,
                 "category": entry.category, "sector": entry.sector,
                 "dimension": entry.dimension.value, "code": entry.code,
                 "amount": str(entry.amount)}
                for entry in self._nontransaction],
            "snapshot": {
                "period": self.snapshot().period,
                "balances": {
                    sector: {"financial": {code: str(amount) for code, amount
                                           in sheet.financial.items()},
                             "real": {code: str(amount) for code, amount
                                      in sheet.real.items()},
                             "net_financial": str(sheet.net_financial),
                             "net_worth": str(sheet.net_worth)}
                    for sector, sheet in self.snapshot().balances.items()},
            },
            "periods": [report.as_dict() for report in self._periods],
        }


__all__ = ["AccountSystem", "OpeningBalances"]
