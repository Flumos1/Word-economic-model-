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
#: Current flow: the ROW's expenditure on resident exports (negative on the ROW).
EXPORTS = "exports"
#: Current flow: the ROW's revenue from resident imports (positive on the ROW).
IMPORTS = "imports"
#: Transaction kinds of the open extension.
EXPORT_KIND = "export"
IMPORT_KIND = "import"
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
    """Return the four-sector chart extended with the ROW sector and external accounts."""
    residents = RESIDENTS
    specs = (
        AccountSpec(
            EXPORTS, Dimension.CURRENT,
            posting_sectors=(ROW,), negative_sectors=(ROW,)),
        AccountSpec(
            IMPORTS, Dimension.CURRENT,
            posting_sectors=(ROW,), positive_sectors=(ROW,)),
        AccountSpec(
            EXTERNAL_CLAIM, Dimension.FINANCIAL,
            posting_sectors=residents + (ROW,), mirrored=True,
            asset_sectors=residents, liability_sectors=(ROW,)),
        AccountSpec(
            EXTERNAL_LIABILITY, Dimension.FINANCIAL,
            posting_sectors=residents + (ROW,), mirrored=True,
            asset_sectors=(ROW,), liability_sectors=residents),
    )
    return base.extend(sectors=(ROW,), specs=specs,
                       kinds=(EXPORT_KIND, IMPORT_KIND))


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
        exports = -row_current.get(EXPORTS, Decimal(0))
        imports = row_current.get(IMPORTS, Decimal(0))
        net_exports = exports - imports
        external_assets = self.external_assets()
        external_liabilities = self.external_liabilities()
        nfa = external_assets - external_liabilities
        row_net_worth = self.net_worth(ROW)
        nfa_revaluation = sum(
            (entry.amount for entry in nontransaction
             if entry.dimension is Dimension.FINANCIAL
             and entry.code in (EXTERNAL_CLAIM, EXTERNAL_LIABILITY)
             and entry.sector in RESIDENTS), Decimal(0))
        open_gdp_expenditure = base.gdp_expenditure + exports - imports
        report = OpenPeriodReport(
            base=base, exports=exports, imports=imports, net_exports=net_exports,
            external_assets=external_assets, external_liabilities=external_liabilities,
            nfa=nfa, row_net_worth=row_net_worth,
            open_gdp_expenditure=open_gdp_expenditure,
            nfa_revaluation=nfa_revaluation)
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
        expected = report.net_exports + report.nfa_revaluation
        if abs(nfa_change - expected) > TOLERANCE:
            raise InvariantViolation(
                f"NFA bridge broken: dNFA {nfa_change} != net exports "
                f"{report.net_exports} + revaluation {report.nfa_revaluation}",
                code="nfa_bridge")
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
        """Net external position of the residents (assets minus liabilities)."""
        return self.external_assets() - self.external_liabilities()

    def row_net_worth(self) -> Decimal:
        return self.net_worth(ROW)

    # ------------------------------------------------------------------
    # External transactions
    # ------------------------------------------------------------------
    def export_goods(self, amount: object, *, exporter: object = Sector.FIRM,
                     memo: str = "") -> object:
        """Export goods to the ROW: resident claim, ROW import expenditure."""
        n = _non_negative(amount, what="export")
        return self._post(EXPORT_KIND, (
            Posting.real(exporter, RealAsset.INVENTORY, -n),
            Posting.financial(exporter, EXTERNAL_CLAIM, n),
            Posting.financial(ROW, EXTERNAL_CLAIM, -n),
            Posting.current(ROW, EXPORTS, -n),
        ), memo)

    def import_goods(self, amount: object, *, importer: object = Sector.HOUSEHOLD,
                     memo: str = "") -> object:
        """Import goods from the ROW: resident consumption and external liability."""
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
        return self._post(IMPORT_KIND, (
            consumption,
            Posting.financial(importer, EXTERNAL_LIABILITY, -n),
            Posting.financial(ROW, EXTERNAL_LIABILITY, n),
            Posting.current(ROW, IMPORTS, n),
        ), memo)

    def export_transfer(self, amount: object, *, counterpart: str = "") -> None:
        """Explicit capital/current transfer to the ROW (not used by the prototype)."""
        raise AccountingError(
            "external transfers are not part of the prototype; only goods trade is posted")

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
    "ROW", "EXTERNAL_CLAIM", "EXTERNAL_LIABILITY", "EXPORTS", "IMPORTS",
    "EXPORT_KIND", "IMPORT_KIND", "RESIDENTS", "RESIDENT_ENUM", "TOLERANCE",
    "open_economy_chart", "OPEN_CHART", "OpenPeriodReport", "OpenAccountSystem",
]
