"""Open-economy extension of the typed sector accounts (AMFE-17 stages 2-3).

The closed kernel in :mod:`amfe.accounts.accounts` deliberately has four resident sectors
and no rest of world.  This module adds the fifth sector ``row`` and the minimal set of
external accounts needed to *post* cross-border operations instead of only observing them:

* current flows ``exports`` (the rest of the world's import expenditure) and ``imports``
  (the rest of the world's export revenue);
* two mirrored financial instruments: ``external_claim`` (residents' external assets,
  the ROW's liability) and ``external_liability`` (residents' external liabilities, the
  ROW's asset);
* :class:`OpenAccountSystem` -- a subclass that reuses the atomic double-entry engine and
  the closed-economy invariants unchanged, plus the open-economy GDP identity,
  the NFA identity and the external-revaluation bridge;
* :class:`OpenPeriodReport` -- the closed period report plus the external aggregates.

Accounting conventions (docs/93)
---------------------------------
Export of goods produced by the resident firm::

    firm: real inventory -X, financial external_claim +X
    row:  financial external_claim -X, current exports -X

A sale is an asset conversion in this kernel (the income was recognised at production),
so the exporter has no current entry; the ROW records its import expenditure and the
matching liability.  Import of goods consumed by resident households::

    household: current household_consumption -M, financial external_liability -M
    row:       financial external_liability +M, current imports +M

The open GDP identity is therefore::

    Y = C + G + dInventories + GFCF + exports - imports

where ``C`` is total household consumption (domestic plus imported).  The closed
invariants still hold for the book including the ROW sector: the ROW current account
``imports - exports`` closes the resident saving-investment gap, and the aggregate net
worth of residents plus the ROW equals the resident real assets.  ``NFA`` is the net
external position of residents::

    NFA = sum_residents external_claim + sum_residents external_liability  (signed)

and the ROW net worth is exactly ``-NFA``.  FX translation is an explicit non-transaction
revaluation: :meth:`OpenAccountSystem.fx_revaluation` posts the resident changes and the
mirrored ROW change, so ``dNW = savings + revaluation`` and the financial mirror stay
valid.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from . import invariants
from .accounts import AccountSystem, OpeningBalances
from .model import (
    AccountSpec,
    AccountingError,
    CapitalFlow,
    ChartOfAccounts,
    CurrentFlow,
    DEFAULT_CHART,
    Dimension,
    FinancialInstrument,
    InvariantViolation,
    NonTransactionEntry,
    PeriodReport,
    PeriodStateError,
    Posting,
    RealAsset,
    Sector,
    Snapshot,
    Transaction,
    TransactionRejected,
    code_string,
    to_decimal,
)

#: The rest-of-world sector code.
ROW = "row"
#: Residents' external assets (mirrored by the ROW's liability).
EXTERNAL_CLAIM = "external_claim"
#: Residents' external liabilities (mirrored by the ROW's asset).
EXTERNAL_LIABILITY = "external_liability"
#: Current flows: the ROW's expenditure on resident exports (negative on the ROW).
EXPORTS_GOODS = "exports_goods"
EXPORTS_SERVICES = "exports_services"
#: Current flows: the ROW's revenue from resident imports (positive on the ROW).
IMPORTS_GOODS = "imports_goods"
IMPORTS_SERVICES = "imports_services"
#: Backward-compatible aliases (goods are the default category).
EXPORTS = EXPORTS_GOODS
IMPORTS = IMPORTS_GOODS
#: Mirrored income flows (primary and secondary income with the ROW).
PRIMARY_INCOME = "primary_income"
SECONDARY_INCOME = "secondary_income"
#: Transaction kinds of the open extension.
EXPORT_KIND = "export"
IMPORT_KIND = "import"
SERVICE_EXPORT_KIND = "export_service"
SERVICE_IMPORT_KIND = "import_service"
INCOME_KIND = "external_income"
CAPITAL_TRANSFER_KIND = "external_capital_transfer"
SETTLEMENT_KIND = "external_settlement"
#: Resident sector codes, in the order used by the reports.
RESIDENTS = tuple(sector.value for sector in Sector)
RESIDENT_ENUM = tuple(Sector)
#: Absolute tolerance for the exact Decimal invariants.
TOLERANCE = Decimal("1e-9")


def _non_negative(value: object, *, what: str) -> Decimal:
    number = to_decimal(value, what=what)
    if number < 0:
        raise TransactionRejected(f"negative {what}: {number}", code="negative_amount")
    return number


def open_economy_chart(base: ChartOfAccounts = DEFAULT_CHART) -> ChartOfAccounts:
    """Return the four-sector chart extended with the ROW sector and external accounts.

    The chart contains the goods/services export and import flows, the mirrored primary
    and secondary income flows, the ROW roles of the capital account, two external
    financial instruments and a ROW deposit account (so that external positions can be
    settled through the domestic banking system).
    """
    residents = RESIDENTS
    everyone = residents + (ROW,)
    capital = base.spec(Dimension.CAPITAL, CapitalFlow.CAPITAL_TRANSFER)
    deposit = base.spec(Dimension.FINANCIAL, FinancialInstrument.DEPOSIT)
    specs = (
        AccountSpec(EXPORTS_GOODS, Dimension.CURRENT,
                    posting_sectors=(ROW,), negative_sectors=(ROW,)),
        AccountSpec(EXPORTS_SERVICES, Dimension.CURRENT,
                    posting_sectors=(ROW,), negative_sectors=(ROW,)),
        AccountSpec(IMPORTS_GOODS, Dimension.CURRENT,
                    posting_sectors=(ROW,), positive_sectors=(ROW,)),
        AccountSpec(IMPORTS_SERVICES, Dimension.CURRENT,
                    posting_sectors=(ROW,), positive_sectors=(ROW,)),
        AccountSpec(PRIMARY_INCOME, Dimension.CURRENT,
                    posting_sectors=everyone, positive_sectors=everyone,
                    negative_sectors=everyone, mirrored=True),
        AccountSpec(SECONDARY_INCOME, Dimension.CURRENT,
                    posting_sectors=everyone, positive_sectors=everyone,
                    negative_sectors=everyone, mirrored=True),
        AccountSpec(
            capital.code, capital.dimension,
            posting_sectors=capital.posting_sectors | {ROW},
            positive_sectors=capital.positive_sectors | {ROW},
            negative_sectors=capital.negative_sectors | {ROW},
            mirrored=True),
        AccountSpec(
            EXTERNAL_CLAIM, Dimension.FINANCIAL,
            posting_sectors=everyone, mirrored=True,
            asset_sectors=residents, liability_sectors=(ROW,)),
        AccountSpec(
            EXTERNAL_LIABILITY, Dimension.FINANCIAL,
            posting_sectors=everyone, mirrored=True,
            asset_sectors=(ROW,), liability_sectors=residents),
        AccountSpec(
            deposit.code, deposit.dimension,
            posting_sectors=deposit.posting_sectors | {ROW},
            positive_sectors=deposit.positive_sectors,
            negative_sectors=deposit.negative_sectors,
            mirrored=True,
            asset_sectors=deposit.asset_sectors | {ROW},
            liability_sectors=deposit.liability_sectors),
    )
    return base.extend(
        sectors=(ROW,), specs=specs,
        kinds=(EXPORT_KIND, IMPORT_KIND, SERVICE_EXPORT_KIND, SERVICE_IMPORT_KIND,
               INCOME_KIND, CAPITAL_TRANSFER_KIND, SETTLEMENT_KIND))


OPEN_CHART = open_economy_chart()


@dataclass(frozen=True)
class OpenPeriodReport:
    """A closed period of the open SFC kernel: closed report plus external aggregates."""

    base: PeriodReport
    exports: Decimal
    imports: Decimal
    net_exports: Decimal
    external_assets: Decimal
    external_liabilities: Decimal
    nfa: Decimal
    row_net_worth: Decimal
    open_gdp_expenditure: Decimal
    nfa_revaluation: Decimal
    exports_goods: Decimal = Decimal(0)
    exports_services: Decimal = Decimal(0)
    imports_goods: Decimal = Decimal(0)
    imports_services: Decimal = Decimal(0)
    net_primary_income: Decimal = Decimal(0)
    net_secondary_income: Decimal = Decimal(0)
    net_capital_transfers: Decimal = Decimal(0)
    current_account_balance: Decimal = Decimal(0)
    gross_national_income: Decimal = Decimal(0)
    gross_national_disposable_income: Decimal = Decimal(0)
    row_deposits: Decimal = Decimal(0)
    settlements: Decimal = Decimal(0)

    # -- closed-report delegation ---------------------------------------
    def __getattr__(self, name: str):
        # Delegate every closed-kernel attribute (savings, closing_net_worth, ...).
        return getattr(self.base, name)

    @property
    def closed_gdp_expenditure(self) -> Decimal:
        return self.base.gdp_expenditure

    @property
    def gdp_expenditure(self) -> Decimal:
        """The open-economy expenditure measure (used by the open GDP identity)."""
        return self.open_gdp_expenditure

    @property
    def period(self) -> str:
        return self.base.period

    @property
    def sectors(self) -> tuple[str, ...]:
        return self.base.sectors

    # -- resident aggregates --------------------------------------------
    @property
    def savings_residents(self) -> Decimal:
        return sum((self.base.savings[name] for name in RESIDENTS), Decimal(0))

    @property
    def revaluation_residents(self) -> Decimal:
        return sum((self.base.revaluation[name] for name in RESIDENTS), Decimal(0))

    @property
    def net_worth_residents(self) -> Decimal:
        return sum((self.base.closing_net_worth[name] for name in RESIDENTS),
                   Decimal(0))

    @property
    def real_assets_total(self) -> Decimal:
        return sum((sum(sheet.real.values(), Decimal(0))
                    for sheet in self.base.snapshot.balances.values()), Decimal(0))

    def nfa_identity_residual(self) -> Decimal:
        """``NFA + ROW net worth`` must be exactly zero (external mirroring)."""
        return self.nfa + self.row_net_worth

    def as_dict(self) -> dict:
        payload = self.base.as_dict()
        payload["open_sfc"] = {
            "exports": str(self.exports),
            "imports": str(self.imports),
            "net_exports": str(self.net_exports),
            "external_assets": str(self.external_assets),
            "external_liabilities": str(self.external_liabilities),
            "nfa": str(self.nfa),
            "row_net_worth": str(self.row_net_worth),
            "open_gdp_expenditure": str(self.open_gdp_expenditure),
            "closed_gdp_expenditure": str(self.closed_gdp_expenditure),
            "nfa_revaluation": str(self.nfa_revaluation),
            "exports_goods": str(self.exports_goods),
            "exports_services": str(self.exports_services),
            "imports_goods": str(self.imports_goods),
            "imports_services": str(self.imports_services),
            "net_primary_income": str(self.net_primary_income),
            "net_secondary_income": str(self.net_secondary_income),
            "net_capital_transfers": str(self.net_capital_transfers),
            "current_account_balance": str(self.current_account_balance),
            "gross_national_income": str(self.gross_national_income),
            "gross_national_disposable_income": str(
                self.gross_national_disposable_income),
            "row_deposits": str(self.row_deposits),
            "settlements": str(self.settlements),
        }
        return payload


class OpenAccountSystem(AccountSystem):
    """A five-sector (four residents + ROW) open-economy accounting system."""

    def __init__(self, chart: ChartOfAccounts | None = None, period: str = "P1",
                 *, opening: OpeningBalances | None = None) -> None:
        selected = OPEN_CHART if chart is None else chart
        if ROW not in selected.sectors or not selected.has_spec(
                Dimension.FINANCIAL, EXTERNAL_CLAIM):
            raise AccountingError(
                "an open-economy chart must contain the ROW sector and the external accounts")
        super().__init__(selected, period, opening=opening)
        self._opening_nfa = self.nfa()

    # ------------------------------------------------------------------
    # Period lifecycle
    # ------------------------------------------------------------------
    def begin_period(self, period: str) -> None:
        super().begin_period(period)
        self._opening_nfa = self.nfa()

    def assert_invariants(self) -> None:
        invariants.check_snapshot(self.snapshot(), self._chart)
        if self._last_report is not None:
            self._check_open_report(self._last_report)

    def close_period(self) -> OpenPeriodReport:
        """Close the period with the open-economy report and invariants."""
        if not self._period_open:
            raise PeriodStateError(f"period {self._period} is already closed")
        entries = tuple(self._journal[self._period_start:])
        nontransaction = tuple(self._nontransaction[self._nt_start:])
        base = self._build_report(entries, nontransaction)

        row_current = base.current.get(ROW, {})
        exports_goods = -row_current.get(EXPORTS_GOODS, Decimal(0))
        exports_services = -row_current.get(EXPORTS_SERVICES, Decimal(0))
        imports_goods = row_current.get(IMPORTS_GOODS, Decimal(0))
        imports_services = row_current.get(IMPORTS_SERVICES, Decimal(0))
        exports = exports_goods + exports_services
        imports = imports_goods + imports_services
        net_exports = exports - imports
        net_primary_income = sum(
            (base.current.get(sector, {}).get(PRIMARY_INCOME, Decimal(0))
             for sector in RESIDENTS), Decimal(0))
        net_secondary_income = sum(
            (base.current.get(sector, {}).get(SECONDARY_INCOME, Decimal(0))
             for sector in RESIDENTS), Decimal(0))
        net_capital_transfers = sum(
            (base.capital_transfers.get(sector, Decimal(0))
             for sector in RESIDENTS), Decimal(0))
        current_account_balance = (net_exports + net_primary_income
                                   + net_secondary_income)
        external_assets = self.external_assets()
        external_liabilities = self.external_liabilities()
        row_deposits = self.balance(ROW, FinancialInstrument.DEPOSIT)
        nfa = external_assets - external_liabilities - row_deposits
        row_net_worth = self.net_worth(ROW)
        nfa_revaluation = sum(
            (entry.amount for entry in nontransaction
             if entry.dimension is Dimension.FINANCIAL
             and entry.code in (EXTERNAL_CLAIM, EXTERNAL_LIABILITY)
             and entry.sector in RESIDENTS), Decimal(0))
        settlements = Decimal(0)
        for entry in entries:
            if entry.kind != SETTLEMENT_KIND:
                continue
            for posting in entry.postings:
                if posting.dimension is not Dimension.FINANCIAL:
                    continue
                if posting.sector not in RESIDENTS:
                    continue
                if posting.code == EXTERNAL_CLAIM and posting.amount < 0:
                    settlements += -posting.amount
                elif posting.code == EXTERNAL_LIABILITY and posting.amount > 0:
                    settlements += posting.amount
        open_gdp_expenditure = base.gdp_expenditure + net_exports
        gross_national_income = base.gdp_production + net_primary_income
        gross_national_disposable_income = (gross_national_income
                                            + net_secondary_income)
        report = OpenPeriodReport(
            base=base, exports=exports, imports=imports, net_exports=net_exports,
            external_assets=external_assets, external_liabilities=external_liabilities,
            nfa=nfa, row_net_worth=row_net_worth,
            open_gdp_expenditure=open_gdp_expenditure,
            nfa_revaluation=nfa_revaluation,
            exports_goods=exports_goods, exports_services=exports_services,
            imports_goods=imports_goods, imports_services=imports_services,
            net_primary_income=net_primary_income,
            net_secondary_income=net_secondary_income,
            net_capital_transfers=net_capital_transfers,
            current_account_balance=current_account_balance,
            gross_national_income=gross_national_income,
            gross_national_disposable_income=gross_national_disposable_income,
            row_deposits=row_deposits, settlements=settlements)
        try:
            self._check_open_report(report)
        except InvariantViolation as exc:
            raise InvariantViolation(
                f"period {self._period} does not close (open SFC): {exc}",
                code=exc.code, details=exc.details) from exc
        self._period_open = False
        self._last_report = report
        self._periods.append(report)
        return report

    def _check_open_report(self, report: OpenPeriodReport) -> None:
        """Open-economy invariants (all closed ones still hold with the ROW sector)."""
        chart = self._chart
        invariants.check_snapshot(report.base.snapshot, chart)
        invariants.check_period_identity(report.base)
        invariants.check_financial_accounts(report.base)
        invariants.check_closed_economy(report.base)
        invariants.check_government_budget(report.base)
        if report.exports < 0 or report.imports < 0:
            raise InvariantViolation("negative external trade flow",
                                     code="open_trade_negative")
        if not (report.base.gdp_production == report.base.gdp_income
                == report.open_gdp_expenditure):
            raise InvariantViolation(
                "open GDP measures differ: production="
                f"{report.base.gdp_production}, income={report.base.gdp_income}, "
                f"expenditure={report.open_gdp_expenditure}",
                code="open_gdp_identity")
        residual = report.nfa_identity_residual()
        if abs(residual) > TOLERANCE:
            raise InvariantViolation(
                f"external mirroring broken: NFA {report.nfa} + ROW net worth "
                f"{report.row_net_worth} = {residual}", code="nfa_identity")
        nfa_change = report.nfa - self._opening_nfa
        expected = (report.current_account_balance + report.net_capital_transfers
                    + report.nfa_revaluation)
        if abs(nfa_change - expected) > TOLERANCE:
            raise InvariantViolation(
                f"NFA bridge broken: dNFA {nfa_change} != current account "
                f"{report.current_account_balance} + capital transfers "
                f"{report.net_capital_transfers} + revaluation "
                f"{report.nfa_revaluation}", code="nfa_bridge")
        if report.gross_national_income != (report.base.gdp_production
                                            + report.net_primary_income):
            raise InvariantViolation("GNI definition broken", code="gni_identity")
        if report.gross_national_disposable_income != (
                report.gross_national_income + report.net_secondary_income):
            raise InvariantViolation("GNDI definition broken", code="gndi_identity")
        world_wealth = (report.net_worth_residents + report.row_net_worth)
        if abs(world_wealth - report.real_assets_total) > TOLERANCE:
            raise InvariantViolation(
                f"world net worth {world_wealth} != real assets "
                f"{report.real_assets_total}", code="world_wealth")

    # ------------------------------------------------------------------
    # External accessors
    # ------------------------------------------------------------------
    def external_assets(self) -> Decimal:
        return sum((self.balance(sector, EXTERNAL_CLAIM) for sector in RESIDENT_ENUM),
                   Decimal(0))

    def external_liabilities(self) -> Decimal:
        return -sum((self.balance(sector, EXTERNAL_LIABILITY)
                     for sector in RESIDENT_ENUM), Decimal(0))

    def nfa(self) -> Decimal:
        """Net external position: external assets - liabilities - ROW deposits.

        A deposit that the rest of the world holds at the domestic bank is a liability
        of the residents, so it enters the net external position with a negative sign.
        """
        return (self.external_assets() - self.external_liabilities()
                - self.balance(ROW, FinancialInstrument.DEPOSIT))

    def row_net_worth(self) -> Decimal:
        return self.net_worth(ROW)

    # ------------------------------------------------------------------
    # External transactions
    # ------------------------------------------------------------------
    def _export(self, amount: object, flow: str, kind: str,
                exporter: object, memo: str) -> object:
        n = _non_negative(amount, what="export")
        return self._post(kind, (
            Posting.real(exporter, RealAsset.INVENTORY, -n),
            Posting.financial(exporter, EXTERNAL_CLAIM, n),
            Posting.financial(ROW, EXTERNAL_CLAIM, -n),
            Posting.current(ROW, flow, -n),
        ), memo)

    def _import(self, amount: object, flow: str, kind: str,
                importer: object, memo: str) -> object:
        n = _non_negative(amount, what="import")
        importer_code = code_string(importer, what="importer")
        if importer_code == Sector.HOUSEHOLD.value:
            consumption = Posting.current(importer, CurrentFlow.HOUSEHOLD_CONSUMPTION, -n)
        elif importer_code == Sector.GOVERNMENT.value:
            consumption = Posting.current(importer, CurrentFlow.GOVERNMENT_CONSUMPTION, -n)
        else:
            raise TransactionRejected(
                "imports are modelled for households and government only",
                code="invalid_importer")
        return self._post(kind, (
            consumption,
            Posting.financial(importer, EXTERNAL_LIABILITY, -n),
            Posting.financial(ROW, EXTERNAL_LIABILITY, n),
            Posting.current(ROW, flow, n),
        ), memo)

    def export_goods(self, amount: object, *, exporter: object = Sector.FIRM,
                     memo: str = "") -> object:
        """Export goods to the ROW: resident claim, ROW import expenditure."""
        return self._export(amount, EXPORTS_GOODS, EXPORT_KIND, exporter, memo)

    def export_services(self, amount: object, *, exporter: object = Sector.FIRM,
                        memo: str = "") -> object:
        """Export services to the ROW (same accounting, separate flow code)."""
        return self._export(amount, EXPORTS_SERVICES, SERVICE_EXPORT_KIND,
                            exporter, memo)

    def import_goods(self, amount: object, *, importer: object = Sector.HOUSEHOLD,
                     memo: str = "") -> object:
        """Import goods from the ROW: resident consumption and external liability."""
        return self._import(amount, IMPORTS_GOODS, IMPORT_KIND, importer, memo)

    def import_services(self, amount: object, *, importer: object = Sector.HOUSEHOLD,
                        memo: str = "") -> object:
        """Import services from the ROW (same accounting, separate flow code)."""
        return self._import(amount, IMPORTS_SERVICES, SERVICE_IMPORT_KIND,
                            importer, memo)

    def _income(self, amount: object, flow: str, received: bool,
                resident: object, memo: str) -> object:
        n = _non_negative(amount, what="income")
        resident_code = code_string(resident, what="resident")
        if resident_code not in RESIDENTS:
            raise TransactionRejected("external income applies to resident sectors",
                                      code="invalid_sector")
        sign = Decimal(1) if received else Decimal(-1)
        return self._post(INCOME_KIND, (
            Posting.current(resident, flow, sign * n),
            Posting.financial(resident,
                              EXTERNAL_CLAIM if received else EXTERNAL_LIABILITY,
                              sign * n),
            Posting.current(ROW, flow, -sign * n),
            Posting.financial(ROW,
                              EXTERNAL_CLAIM if received else EXTERNAL_LIABILITY,
                              -sign * n),
        ), memo)

    def receive_primary_income(self, amount: object, *,
                               resident: object = Sector.HOUSEHOLD,
                               memo: str = "") -> object:
        """Primary income received from the ROW (increases GNI, not GDP)."""
        return self._income(amount, PRIMARY_INCOME, True, resident, memo)

    def pay_primary_income(self, amount: object, *,
                           resident: object = Sector.HOUSEHOLD,
                           memo: str = "") -> object:
        """Primary income paid to the ROW (decreases GNI, not GDP)."""
        return self._income(amount, PRIMARY_INCOME, False, resident, memo)

    def receive_secondary_income(self, amount: object, *,
                                 resident: object = Sector.HOUSEHOLD,
                                 memo: str = "") -> object:
        """Secondary income (current transfer) received from the ROW."""
        return self._income(amount, SECONDARY_INCOME, True, resident, memo)

    def pay_secondary_income(self, amount: object, *,
                             resident: object = Sector.HOUSEHOLD,
                             memo: str = "") -> object:
        """Secondary income (current transfer) paid to the ROW."""
        return self._income(amount, SECONDARY_INCOME, False, resident, memo)

    def _capital_transfer(self, amount: object, received: bool,
                          resident: object, memo: str) -> object:
        n = _non_negative(amount, what="capital transfer")
        resident_code = code_string(resident, what="resident")
        if resident_code not in RESIDENTS:
            raise TransactionRejected("external capital transfers apply to residents",
                                      code="invalid_sector")
        sign = Decimal(1) if received else Decimal(-1)
        return self._post(CAPITAL_TRANSFER_KIND, (
            Posting.capital(resident, CapitalFlow.CAPITAL_TRANSFER, sign * n),
            Posting.financial(resident,
                              EXTERNAL_CLAIM if received else EXTERNAL_LIABILITY,
                              sign * n),
            Posting.capital(ROW, CapitalFlow.CAPITAL_TRANSFER, -sign * n),
            Posting.financial(ROW,
                              EXTERNAL_CLAIM if received else EXTERNAL_LIABILITY,
                              -sign * n),
        ), memo)

    def receive_capital_transfer(self, amount: object, *,
                                 resident: object = Sector.HOUSEHOLD,
                                 memo: str = "") -> object:
        """Capital transfer received from the ROW (capital account + claim)."""
        return self._capital_transfer(amount, True, resident, memo)

    def pay_capital_transfer(self, amount: object, *,
                             resident: object = Sector.HOUSEHOLD,
                             memo: str = "") -> object:
        """Capital transfer paid to the ROW (capital account + liability)."""
        return self._capital_transfer(amount, False, resident, memo)

    def settle_external_claim(self, amount: object, *,
                              resident: object = Sector.FIRM,
                              memo: str = "") -> object:
        """The ROW settles a claim on it by transferring deposits to the resident."""
        n = _non_negative(amount, what="external claim settlement")
        resident_code = code_string(resident, what="resident")
        return self._post(SETTLEMENT_KIND, (
            Posting.financial(resident, EXTERNAL_CLAIM, -n),
            Posting.financial(resident, FinancialInstrument.DEPOSIT, n),
            Posting.financial(ROW, EXTERNAL_CLAIM, n),
            Posting.financial(ROW, FinancialInstrument.DEPOSIT, -n),
        ), memo)

    def settle_external_liability(self, amount: object, *,
                                  resident: object = Sector.HOUSEHOLD,
                                  memo: str = "") -> object:
        """The resident pays its external liability with a domestic deposit."""
        n = _non_negative(amount, what="external liability settlement")
        resident_code = code_string(resident, what="resident")
        return self._post(SETTLEMENT_KIND, (
            Posting.financial(resident, EXTERNAL_LIABILITY, n),
            Posting.financial(resident, FinancialInstrument.DEPOSIT, -n),
            Posting.financial(ROW, EXTERNAL_LIABILITY, -n),
            Posting.financial(ROW, FinancialInstrument.DEPOSIT, n),
        ), memo)

    def export_transfer(self, amount: object, *, counterpart: str = "") -> None:
        """Deprecated primitive: use ``receive/pay_capital_transfer`` or income methods.

        Kept for backward compatibility: a single "export transfer" primitive is not
        meaningful now that current and capital transfers are modelled separately.
        """
        raise AccountingError(
            "use receive/pay_capital_transfer or receive/pay_*_income instead of "
            "the old export_transfer primitive")

    # ------------------------------------------------------------------
    # FX translation (non-transaction revaluation)
    # ------------------------------------------------------------------
    def fx_revaluation(
        self,
        claim_changes: Mapping[object, object] | None = None,
        liability_changes: Mapping[object, object] | None = None,
    ) -> tuple[NonTransactionEntry, ...]:
        """Revalue external positions because of an exchange-rate change.

        ``claim_changes`` maps a resident sector to the signed local-currency delta of its
        external claim; ``liability_changes`` does the same for the signed external
        liability position.  The ROW mirror is derived automatically so that every
        instrument keeps summing to zero and ``dNW = savings + revaluation`` stays valid.
        """
        recorded: list[NonTransactionEntry] = []
        for instrument, changes in ((EXTERNAL_CLAIM, claim_changes),
                                    (EXTERNAL_LIABILITY, liability_changes)):
            if not changes:
                continue
            resident_changes: dict[str, Decimal] = {}
            for sector, delta in changes.items():
                sector_code = code_string(sector, what="sector")
                if sector_code not in RESIDENTS:
                    raise TransactionRejected(
                        f"external revaluation applies to resident sectors, got {sector_code}",
                        code="invalid_sector")
                value = to_decimal(delta, what="FX revaluation")
                if value != 0:
                    resident_changes[sector_code] = (
                        resident_changes.get(sector_code, Decimal(0)) + value)
            if not resident_changes:
                continue
            total = sum(resident_changes.values(), Decimal(0))
            mirrored = dict(resident_changes)
            mirrored[ROW] = -total
            recorded.extend(self.revalue_financial(instrument, mirrored))
        return tuple(recorded)

    # ------------------------------------------------------------------
    # Period-flow introspection (used by the world layer)
    # ------------------------------------------------------------------
    def current_period_flows(self) -> dict[str, dict[str, Decimal]]:
        """Current-account sums of the open period, by sector and flow code."""
        flows: dict[str, dict[str, Decimal]] = {}
        for entry in self._journal[self._period_start:]:
            for posting in entry.postings:
                if posting.dimension is not Dimension.CURRENT:
                    continue
                detail = flows.setdefault(posting.sector, {})
                detail[posting.code] = detail.get(posting.code, Decimal(0)) + posting.amount
        return flows


__all__ = [
    "ROW", "EXTERNAL_CLAIM", "EXTERNAL_LIABILITY",
    "EXPORTS", "EXPORTS_GOODS", "EXPORTS_SERVICES",
    "IMPORTS", "IMPORTS_GOODS", "IMPORTS_SERVICES",
    "PRIMARY_INCOME", "SECONDARY_INCOME",
    "EXPORT_KIND", "IMPORT_KIND", "SERVICE_EXPORT_KIND", "SERVICE_IMPORT_KIND",
    "INCOME_KIND", "CAPITAL_TRANSFER_KIND", "SETTLEMENT_KIND",
    "RESIDENTS", "RESIDENT_ENUM", "TOLERANCE",
    "open_economy_chart", "OPEN_CHART", "OpenPeriodReport", "OpenAccountSystem",
]
