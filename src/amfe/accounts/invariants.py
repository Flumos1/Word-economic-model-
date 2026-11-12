"""Independent invariants of the typed sector accounts (stage 2).

The checkers in this module are pure functions over snapshots and period
reports.  They are kept separate from the ledger so that they can be tested on
deliberately corrupted reports (negative tests) and so that stage 3 can reuse
them for two-country consolidation.

Checked identities
------------------
F01  For every financial instrument the sum of signed positions over all
     sectors is zero (mirroring).
F02  Every financial balance respects the account orientation: assets are
     positive, liabilities negative; real assets are non-negative.
F03  The aggregate net worth equals the aggregate real assets
     (``sum_s NW_s == sum_s real_s``).
P01  For every sector: ``NW_close - NW_open == savings + capital transfers
     + revaluation + other volume changes``.  Without capital transfers,
     revaluation and other changes this is the required ``dNW = savings``.
P02  ``gross_saving == savings + consumption_of_fixed_capital``; the capital
     account ``net_lending == gross_saving + capital_transfers
     - capital_formation``; the transaction real-asset change equals
     ``capital_formation - consumption_of_fixed_capital``.
P03  The financial account closes the capital account:
     ``net_lending == net_financial_change`` for every sector; per instrument
     the financial changes over all sectors sum to zero.
C01  Closed economy without residuals: ``sum_s net_lending_s == 0``,
     ``sum_s gross_saving_s == sum_s capital_formation_s`` and
     ``sum_s net_financial_change_s == 0``.
G01  Government budget constraint: revenue - expenditure == net saving;
     the deficit is financed by net borrowing (net of capital transfers,
     consumption of fixed capital and government capital formation):
     ``net_borrowing == deficit - CFC - capital_transfers + capital_formation``.
G02  GDP by production, expenditure and income are equal for the toy kernel:
     ``Y = C + G + dInventories + GFCF`` and
     ``Y = compensation_of_employees + gross_operating_surplus``
     (no taxes on production and no mixed income in this kernel).

Stage 3 helper
--------------
``check_external_mirroring`` verifies the two-country contract: residents'
external positions in the two books sum to zero and each country's
rest-of-world mirror book equals the other country's resident position.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from .model import (
    AccountingError,
    ChartOfAccounts,
    DEFAULT_CHART,
    Dimension,
    FinancialInstrument,
    InvariantViolation,
    PeriodReport,
    Sector,
    Snapshot,
    code_string,
)


def _violation(code: str, message: str, **details: object) -> None:
    raise InvariantViolation(message, code=code, details=details)


# ---------------------------------------------------------------------------
# Snapshot-level invariants
# ---------------------------------------------------------------------------
def check_stock_signs(
    financial: Mapping[tuple[str, str], Decimal],
    real: Mapping[tuple[str, str], Decimal],
    chart: ChartOfAccounts = DEFAULT_CHART,
) -> None:
    """F02: orientation of every stock position against the chart."""
    sectors = chart.sectors
    for (sector, instrument), amount in financial.items():
        if sector not in sectors:
            _violation("unknown_sector", f"unknown sector in balances: {sector}",
                       sector=sector)
        spec = chart.lookup(Dimension.FINANCIAL, instrument)
        if spec is None:
            _violation("unknown_account",
                       f"unknown financial account in balances: {instrument}",
                       instrument=instrument)
        if sector not in spec.posting_sectors:
            _violation("position_role",
                       f"{sector} may not hold financial account {instrument}",
                       sector=sector, instrument=instrument)
        if amount > 0 and sector not in spec.asset_sectors:
            _violation("position_sign",
                       f"{sector} cannot hold a positive {instrument} position",
                       sector=sector, instrument=instrument, amount=str(amount))
        if amount < 0 and sector not in spec.liability_sectors:
            _violation("position_sign",
                       f"{sector} cannot hold a negative {instrument} position",
                       sector=sector, instrument=instrument, amount=str(amount))
    for (sector, asset), amount in real.items():
        if sector not in sectors:
            _violation("unknown_sector", f"unknown sector in balances: {sector}",
                       sector=sector)
        spec = chart.lookup(Dimension.REAL, asset)
        if spec is None:
            _violation("unknown_account",
                       f"unknown real account in balances: {asset}", asset=asset)
        if sector not in spec.posting_sectors:
            _violation("position_role",
                       f"{sector} may not hold real asset {asset}",
                       sector=sector, asset=asset)
        if amount < 0 and not spec.allow_negative_balance:
            _violation("negative_real_asset",
                       f"negative balance of real asset {asset}: {amount}",
                       sector=sector, asset=asset, amount=str(amount))


def check_financial_mirror(
    financial: Mapping[tuple[str, str], Decimal],
    chart: ChartOfAccounts = DEFAULT_CHART,
) -> None:
    """F01: for each instrument the signed positions sum to zero."""
    sectors = chart.sectors
    by_instrument: dict[str, Decimal] = {}
    for (sector, instrument), amount in financial.items():
        if sector not in sectors:
            _violation("unknown_sector", f"unknown sector in balances: {sector}",
                       sector=sector)
        if chart.lookup(Dimension.FINANCIAL, instrument) is None:
            _violation("unknown_account", f"unknown instrument in balances: {instrument}",
                       instrument=instrument)
        by_instrument[instrument] = by_instrument.get(instrument, Decimal(0)) + amount
    for instrument in sorted(by_instrument):
        total = by_instrument[instrument]
        if total != 0:
            _violation("financial_mirror",
                       f"instrument {instrument} does not sum to zero: {total}",
                       instrument=instrument, total=str(total))


def check_snapshot(snapshot: Snapshot, chart: ChartOfAccounts = DEFAULT_CHART) -> None:
    """F01+F02+F03 on a consolidated snapshot."""
    financial: dict[tuple[str, str], Decimal] = {}
    real: dict[tuple[str, str], Decimal] = {}
    unknown = set(snapshot.balances) - set(chart.sectors)
    if unknown:
        _violation("unknown_sector", f"unknown sectors in snapshot: {sorted(unknown)}",
                   sectors=sorted(unknown))
    for sector, sheet in snapshot.balances.items():
        for instrument, amount in sheet.financial.items():
            financial[(sector, instrument)] = amount
        for asset, amount in sheet.real.items():
            real[(sector, asset)] = amount
    check_financial_mirror(financial, chart)
    check_stock_signs(financial, real, chart)
    net_worth = sum((sheet.net_worth for sheet in snapshot.balances.values()),
                    Decimal(0))
    real_assets = sum((sum(sheet.real.values(), Decimal(0))
                       for sheet in snapshot.balances.values()), Decimal(0))
    if net_worth != real_assets:
        _violation("net_worth_partition",
                   f"aggregate net worth {net_worth} != real assets {real_assets}",
                   net_worth=str(net_worth), real_assets=str(real_assets))
    financial_total = sum(financial.values(), Decimal(0))
    if financial_total != 0:
        _violation("financial_mirror",
                   f"total financial positions do not sum to zero: {financial_total}",
                   total=str(financial_total))


# ---------------------------------------------------------------------------
# Period-level invariants
# ---------------------------------------------------------------------------
def check_period_identity(report: PeriodReport) -> None:
    """P01+P02: net worth bridge and capital account decomposition."""
    for sector in report.sectors:
        current_detail = report.current.get(sector, {})
        savings = sum(current_detail.values(), Decimal(0))
        if savings != report.savings[sector]:
            _violation("savings_definition",
                       f"{sector}: savings {report.savings[sector]} != current sum {savings}",
                       sector=sector)
        depreciation = -current_detail.get("depreciation", Decimal(0))
        if depreciation != report.consumption_of_fixed_capital[sector]:
            _violation("cfc_definition",
                       f"{sector}: CFC mismatch", sector=sector)
        gross_saving = report.savings[sector] + report.consumption_of_fixed_capital[sector]
        if gross_saving != report.gross_saving[sector]:
            _violation("gross_saving_definition",
                       f"{sector}: gross saving mismatch", sector=sector)
        real_change = sum(report.real_change.get(sector, {}).values(), Decimal(0))
        if real_change != (report.capital_formation[sector]
                           - report.consumption_of_fixed_capital[sector]):
            _violation("real_change_definition",
                       f"{sector}: transaction real change {real_change} != "
                       f"capital formation - CFC {report.capital_formation[sector]}"
                       f" - {report.consumption_of_fixed_capital[sector]}",
                       sector=sector)
        capital_formation = (report.inventory_change[sector]
                             + report.gross_fixed_capital_formation[sector])
        if capital_formation != report.capital_formation[sector]:
            _violation("capital_formation_definition",
                       f"{sector}: capital formation mismatch", sector=sector)
        expected_net_lending = (report.gross_saving[sector]
                                + report.capital_transfers[sector]
                                - report.capital_formation[sector])
        if expected_net_lending != report.net_lending[sector]:
            _violation("net_lending_definition",
                       f"{sector}: net lending mismatch", sector=sector)
        expected_closing = (report.opening_net_worth[sector]
                            + report.savings[sector]
                            + report.capital_transfers[sector]
                            + report.revaluation[sector]
                            + report.other_volume[sector])
        if expected_closing != report.closing_net_worth[sector]:
            _violation(
                "net_worth_bridge",
                f"{sector}: dNW {report.closing_net_worth[sector] - report.opening_net_worth[sector]}"
                f" != savings + capital transfers + revaluation + other volume"
                f" ({report.savings[sector]} + {report.capital_transfers[sector]}"
                f" + {report.revaluation[sector]} + {report.other_volume[sector]})",
                sector=sector,
                delta_net_worth=str(report.closing_net_worth[sector]
                                    - report.opening_net_worth[sector]),
            )
        total_revaluation = (report.revaluation_real[sector]
                             + report.revaluation_financial[sector])
        if total_revaluation != report.revaluation[sector]:
            _violation("revaluation_definition",
                       f"{sector}: revaluation decomposition mismatch", sector=sector)


def check_financial_accounts(report: PeriodReport) -> None:
    """P03: the financial account closes the capital account."""
    for sector in report.sectors:
        detail = report.financial_change.get(sector, {})
        total = sum(detail.values(), Decimal(0))
        if total != report.net_financial_change[sector]:
            _violation("financial_account_definition",
                       f"{sector}: financial change total mismatch", sector=sector)
        if report.net_financial_change[sector] != report.net_lending[sector]:
            _violation(
                "financial_account_closure",
                f"{sector}: net lending {report.net_lending[sector]} != "
                f"financial account {report.net_financial_change[sector]}",
                sector=sector)
    instruments: set[str] = set()
    for detail in report.financial_change.values():
        instruments.update(detail)
    for instrument in sorted(instruments):
        total = sum((detail.get(instrument, Decimal(0))
                     for detail in report.financial_change.values()), Decimal(0))
        if total != 0:
            _violation("financial_mirror_flow",
                       f"period financial change of {instrument} does not sum to zero: {total}",
                       instrument=instrument, total=str(total))


def check_closed_economy(report: PeriodReport) -> None:
    """C01: no residual in the closed one-country economy."""
    net_lending = sum(report.net_lending.values(), Decimal(0))
    if net_lending != 0:
        _violation("closed_economy_residual",
                   f"net lending over all sectors does not sum to zero: {net_lending}",
                   total=str(net_lending))
    net_financial = sum(report.net_financial_change.values(), Decimal(0))
    if net_financial != 0:
        _violation("closed_economy_residual",
                   f"net financial change does not sum to zero: {net_financial}",
                   total=str(net_financial))
    gross_saving = sum(report.gross_saving.values(), Decimal(0))
    capital_formation = sum(report.capital_formation.values(), Decimal(0))
    if gross_saving != capital_formation:
        _violation("closed_economy_saving_investment",
                   f"gross saving {gross_saving} != capital formation {capital_formation}",
                   saving=str(gross_saving), investment=str(capital_formation))
    savings = sum(report.savings.values(), Decimal(0))
    cfc = sum(report.consumption_of_fixed_capital.values(), Decimal(0))
    if savings != capital_formation - cfc:
        _violation("closed_economy_saving_investment",
                   f"net saving {savings} != capital formation - CFC "
                   f"{capital_formation - cfc}",
                   saving=str(savings), investment=str(capital_formation - cfc))


# ---------------------------------------------------------------------------
# Government budget
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GovernmentBudget:
    """Government budget and financing of one period."""

    period: str
    revenue: Decimal
    expenditure: Decimal
    net_saving: Decimal
    deficit: Decimal
    consumption_of_fixed_capital: Decimal
    capital_transfers: Decimal
    capital_formation: Decimal
    net_lending: Decimal
    net_borrowing: Decimal
    delta_financial_assets: Decimal
    delta_financial_liabilities: Decimal
    delta_cash: Decimal
    delta_debt: Decimal

    def as_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in (
            "period", "revenue", "expenditure", "net_saving", "deficit",
            "consumption_of_fixed_capital", "capital_transfers",
            "capital_formation", "net_lending", "net_borrowing",
            "delta_financial_assets", "delta_financial_liabilities",
            "delta_cash", "delta_debt")}


def check_government_budget(
    report: PeriodReport, government: object = Sector.GOVERNMENT,
) -> GovernmentBudget:
    """G01: the government budget constraint and its financing."""
    sector = code_string(government, what="government sector")
    current_detail = report.current.get(sector, {})
    revenue = sum((value for value in current_detail.values() if value > 0), Decimal(0))
    expenditure = -sum((value for value in current_detail.values() if value < 0),
                       Decimal(0))
    net_saving = revenue - expenditure
    if net_saving != report.savings[sector]:
        _violation("government_saving",
                   f"{sector}: revenue - expenditure {net_saving} != net saving "
                   f"{report.savings[sector]}", sector=sector)
    deficit = expenditure - revenue
    cfc = report.consumption_of_fixed_capital[sector]
    capital_transfers = report.capital_transfers[sector]
    capital_formation = report.capital_formation[sector]
    net_lending = report.net_lending[sector]
    net_borrowing = -net_lending
    expected_net_borrowing = deficit - cfc - capital_transfers + capital_formation
    if net_borrowing != expected_net_borrowing:
        _violation(
            "government_budget",
            f"{sector}: net borrowing {net_borrowing} != deficit {deficit} - CFC {cfc}"
            f" - capital transfers {capital_transfers} + capital formation "
            f"{capital_formation} = {expected_net_borrowing}",
            sector=sector)
    financial_detail = report.financial_change.get(sector, {})
    delta_assets = sum((value for value in financial_detail.values() if value > 0),
                       Decimal(0))
    delta_liabilities = -sum((value for value in financial_detail.values() if value < 0),
                             Decimal(0))
    if delta_liabilities - delta_assets != net_borrowing:
        _violation("government_financing",
                   f"{sector}: net borrowing {net_borrowing} != increase of liabilities"
                   f" {delta_liabilities} - increase of assets {delta_assets}",
                   sector=sector)
    delta_cash = financial_detail.get(FinancialInstrument.DEPOSIT.value, Decimal(0))
    delta_debt = -sum((value for instrument, value in financial_detail.items()
                       if instrument in (FinancialInstrument.BOND.value,
                                         FinancialInstrument.LOAN.value)
                       and value < 0), Decimal(0))
    return GovernmentBudget(
        period=report.period,
        revenue=revenue,
        expenditure=expenditure,
        net_saving=net_saving,
        deficit=deficit,
        consumption_of_fixed_capital=cfc,
        capital_transfers=capital_transfers,
        capital_formation=capital_formation,
        net_lending=net_lending,
        net_borrowing=net_borrowing,
        delta_financial_assets=delta_assets,
        delta_financial_liabilities=delta_liabilities,
        delta_cash=delta_cash,
        delta_debt=delta_debt,
    )


# ---------------------------------------------------------------------------
# GDP identity
# ---------------------------------------------------------------------------
def check_gdp_identity(report: PeriodReport) -> None:
    """G02: production, expenditure and income measures of the toy GDP."""
    inventory_total = sum(report.inventory_change.values(), Decimal(0))
    gfcf_total = sum(report.gross_fixed_capital_formation.values(), Decimal(0))
    expenditure = (report.household_consumption + report.government_consumption
                   + inventory_total + gfcf_total)
    if expenditure != report.gdp_expenditure:
        _violation("gdp_expenditure",
                   f"expenditure side {expenditure} != reported "
                   f"{report.gdp_expenditure}",
                   expected=str(expenditure))
    income = report.wages_received + report.gross_operating_surplus
    if income != report.gdp_income:
        _violation("gdp_income",
                   f"income side {income} != reported {report.gdp_income}",
                   expected=str(income))
    if not (report.gdp_production == report.gdp_expenditure
            == report.gdp_income):
        _violation(
            "gdp_three_measures",
            f"GDP measures differ: production={report.gdp_production}, "
            f"expenditure={report.gdp_expenditure}, income={report.gdp_income}",
            production=str(report.gdp_production),
            expenditure=str(report.gdp_expenditure),
            income=str(report.gdp_income))


# ---------------------------------------------------------------------------
# Aggregate entry point
# ---------------------------------------------------------------------------
def check_report(report: PeriodReport, chart: ChartOfAccounts = DEFAULT_CHART) -> GovernmentBudget:
    """Run every report-level invariant and return the government budget."""
    check_period_identity(report)
    check_financial_accounts(report)
    check_closed_economy(report)
    budget = check_government_budget(report)
    check_gdp_identity(report)
    check_snapshot(report.snapshot, chart)
    return budget


# ---------------------------------------------------------------------------
# Stage 3 (two countries) contract
# ---------------------------------------------------------------------------
def _balance_or_zero(snapshot: Snapshot, sector: str, instrument: str) -> Decimal:
    sheet = snapshot.balances.get(sector)
    if sheet is None:
        return Decimal(0)
    return sheet.get_financial(instrument)


def check_external_mirroring(
    left: Snapshot,
    right: Snapshot,
    *,
    instrument_pairs: Mapping[object, object],
    left_row: object = "row",
    right_row: object = "row",
    left_residents: Sequence[object] | None = None,
    right_residents: Sequence[object] | None = None,
) -> None:
    """Stage-3 contract: mirrored external positions across two country books.

    Each country book contains a rest-of-world (``row``) sector.  For every
    matched pair of cross-border instruments ``(left_instrument,
    right_instrument)`` the contract is:

    * the residents' positions in the two books sum to zero (the world has no
      net position);
    * the left ROW mirror equals the right residents' position and the right
      ROW mirror equals the left residents' position.

    This makes ``sum_s NW_s == sum_s real_s`` valid for the consolidated world.
    """
    pair_items = list(instrument_pairs.items())
    if not pair_items:
        raise AccountingError("at least one instrument pair is required")
    left_row = code_string(left_row, what="left row sector")
    right_row = code_string(right_row, what="right row sector")
    if left_row not in left.balances:
        raise AccountingError(f"left snapshot has no row sector {left_row!r}")
    if right_row not in right.balances:
        raise AccountingError(f"right snapshot has no row sector {right_row!r}")
    if left_residents is None:
        left_residents = [s for s in left.balances if s != left_row]
    if right_residents is None:
        right_residents = [s for s in right.balances if s != right_row]
    left_residents = [code_string(s, what="left resident") for s in left_residents]
    right_residents = [code_string(s, what="right resident") for s in right_residents]
    for sector in left_residents:
        if sector not in left.balances:
            raise AccountingError(f"unknown left resident sector {sector!r}")
    for sector in right_residents:
        if sector not in right.balances:
            raise AccountingError(f"unknown right resident sector {sector!r}")
    for left_instrument, right_instrument in pair_items:
        left_inst = code_string(left_instrument, what="left instrument")
        right_inst = code_string(right_instrument, what="right instrument")
        left_position = sum((_balance_or_zero(left, sector, left_inst)
                             for sector in left_residents), Decimal(0))
        right_position = sum((_balance_or_zero(right, sector, right_inst)
                              for sector in right_residents), Decimal(0))
        if left_position + right_position != 0:
            _violation(
                "world_net_position",
                f"residents' positions of {left_inst}/{right_inst} do not sum to zero:"
                f" {left_position} + {right_position}",
                left_instrument=left_inst, right_instrument=right_inst,
                left=str(left_position), right=str(right_position))
        left_mirror = _balance_or_zero(left, left_row, left_inst)
        if left_mirror != right_position:
            _violation(
                "row_mirror_left",
                f"left ROW {left_mirror} != right residents' position {right_position}",
                instrument=left_inst, left=str(left_mirror), right=str(right_position))
        right_mirror = _balance_or_zero(right, right_row, right_inst)
        if right_mirror != left_position:
            _violation(
                "row_mirror_right",
                f"right ROW {right_mirror} != left residents' position {left_position}",
                instrument=right_inst, left=str(left_position), right=str(right_mirror))


__all__ = [
    "GovernmentBudget",
    "check_stock_signs", "check_financial_mirror", "check_snapshot",
    "check_period_identity", "check_financial_accounts", "check_closed_economy",
    "check_government_budget", "check_gdp_identity", "check_report",
    "check_external_mirroring",
]
