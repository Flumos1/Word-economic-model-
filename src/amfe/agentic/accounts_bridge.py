"""G2 bridge: agentic simulator aggregates -> standard SFC sector accounts.

The bridge converts the ledger of :class:`amfe.agentic.simulator.AgenticSimulator` into the typed
sector-account vocabulary of the accounting kernel (:mod:`amfe.accounts.model`): sectors
``household`` / ``firm`` / ``bank`` / ``government``, dimensions ``current`` / ``capital`` /
``real`` / ``financial``, and standard codes (``output``, ``wages``, ``direct_tax``, ``transfer``,
``interest``, ``household_consumption``, ``government_consumption``, ``deposit``, ``loan``,
``fixed_capital``).

Public API:

* :class:`AccountsBridge` -- ``AccountsBridge(result, ledger).build()`` returns
  :class:`AccountAggregates`; construction validates types, and ``build`` re-checks every invariant;
* :func:`build_account_aggregates` -- convenience wrapper;
* :func:`export_sector_accounts` -- plain JSON-serialisable ``{sector: {...}}`` export;
* :func:`validate_against_sfc` -- independent validation of the aggregates (fail-closed by default);
* :func:`render_report` -- deterministic text report.

Checks (all fail-closed with :class:`AccountsBridgeError`):

1. ``double_entry`` -- every recorded transaction sums to exactly zero;
2. ``ledger_total`` -- the ledger (financial + real + memo accounts) nets to zero;
3. ``financial_mirroring`` -- the sum of signed financial positions over sectors is zero;
4. ``sector_saving`` -- ``saving = income + expenditure`` for every sector (expenditure is negative);
5. ``sfc_identity`` -- ``S = I + CA`` with ``CA = 0`` (closed economy, no external accounts);
6. ``net_worth`` -- ``NW = net financial worth + real assets`` per sector and in total;
7. ``result_consistency`` -- the ledger digest/transaction count and the per-period saving and
   investment sums equal the :class:`~amfe.agentic.simulator.SimulationResult` they came from.

The aggregates are **cumulative over the whole simulation** (the simulator ledger does not tag
periods); per-period checks already exist in ``SimulationResult``. Intermediate consumption
(``procurement_cost``) has no counterpart in the accounting kernel's ``CurrentFlow`` enum; the bridge
exports it as ``current:intermediate_consumption`` and documents this gap. The bridge does not modify
the simulator, the accounts kernel or the world model; it is a read-only adapter.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Mapping

from amfe.accounts.model import (
    CurrentFlow,
    Dimension,
    FinancialInstrument,
    RealAsset,
    Sector,
)

from .simulator import Ledger, SimulationResult

D = Decimal


class AccountsBridgeError(RuntimeError):
    """Aggregates cannot be built or a validation/invariant check failed (fail-closed)."""


# ------------------------------------------------------------------------------------- mapping
#: simulator account suffix -> (dimension, code, category)
FLOW_MAP: dict[str, tuple[Dimension, str, str]] = {
    # households
    "wage_income": (Dimension.CURRENT, CurrentFlow.WAGES.value, "income"),
    "transfer_income": (Dimension.CURRENT, CurrentFlow.TRANSFER.value, "income"),
    "interest_income": (Dimension.CURRENT, CurrentFlow.INTEREST.value, "income"),
    "consumption": (Dimension.CURRENT, CurrentFlow.HOUSEHOLD_CONSUMPTION.value, "expenditure"),
    "tax": (Dimension.CURRENT, CurrentFlow.DIRECT_TAX.value, "expenditure"),
    "interest_paid": (Dimension.CURRENT, CurrentFlow.INTEREST.value, "expenditure"),
    # firms
    "sales": (Dimension.CURRENT, CurrentFlow.OUTPUT.value, "income"),
    "wage_cost": (Dimension.CURRENT, CurrentFlow.WAGES.value, "expenditure"),
    "procurement_cost": (Dimension.CURRENT, "intermediate_consumption", "expenditure"),
    "investment": (Dimension.CAPITAL, "fixed_investment", "memo"),
    # government
    "tax_income": (Dimension.CURRENT, CurrentFlow.DIRECT_TAX.value, "income"),
    "transfer_paid": (Dimension.CURRENT, CurrentFlow.TRANSFER.value, "expenditure"),
    "spending": (Dimension.CURRENT, CurrentFlow.GOVERNMENT_CONSUMPTION.value, "expenditure"),
    # bank
}

STOCK_MAP: dict[str, tuple[Dimension, str, str]] = {
    "deposit": (Dimension.FINANCIAL, FinancialInstrument.DEPOSIT.value, "financial"),
    "loan": (Dimension.FINANCIAL, FinancialInstrument.LOAN.value, "financial"),
    "capital": (Dimension.REAL, RealAsset.FIXED_CAPITAL.value, "real"),
}

SECTOR_OF_PREFIX = {"hh": Sector.HOUSEHOLD.value, "firm": Sector.FIRM.value,
                    "gov": Sector.GOVERNMENT.value, "bank": Sector.BANK.value}
SECTORS = (Sector.HOUSEHOLD.value, Sector.FIRM.value, Sector.BANK.value,
           Sector.GOVERNMENT.value)


def _classify(account: str) -> dict:
    """Classify one ledger account; raise AccountsBridgeError for anything unmapped."""
    if account == "memo.investment_goods":
        return {"account": account, "sector": None, "dimension": Dimension.CAPITAL.value,
                "code": "investment_goods", "category": "memo"}
    parts = account.split(".")
    prefix = parts[0]
    if prefix not in SECTOR_OF_PREFIX:
        raise AccountsBridgeError(f"unknown account prefix: {account!r}")
    sector = SECTOR_OF_PREFIX[prefix]
    if prefix == "bank":
        if len(parts) != 2:
            raise AccountsBridgeError(f"malformed bank account: {account!r}")
        name = parts[1]
        if name in ("interest_income",):
            dimension, code, category = (Dimension.CURRENT, CurrentFlow.INTEREST.value, "income")
        elif name == "interest_paid":
            dimension, code, category = (Dimension.CURRENT, CurrentFlow.INTEREST.value,
                                         "expenditure")
        elif name in STOCK_MAP:
            dimension, code, category = STOCK_MAP[name]
        elif name == "deposit_liability":
            dimension, code, category = (Dimension.FINANCIAL, FinancialInstrument.DEPOSIT.value,
                                         "financial")
        elif name == "gov_loan":
            dimension, code, category = (Dimension.FINANCIAL, FinancialInstrument.LOAN.value,
                                         "financial")
        else:
            raise AccountsBridgeError(f"unknown bank account: {account!r}")
        return {"account": account, "sector": sector, "dimension": dimension.value,
                "code": code, "category": category}
    if prefix == "gov":
        if len(parts) != 2:
            raise AccountsBridgeError(f"malformed government account: {account!r}")
        name = parts[1]
        if name in STOCK_MAP:
            dimension, code, category = STOCK_MAP[name]
        elif name in ("tax_income", "transfer_paid", "spending"):
            dimension, code, category = FLOW_MAP[name]
        else:
            raise AccountsBridgeError(f"unknown government account: {account!r}")
        return {"account": account, "sector": sector, "dimension": dimension.value,
                "code": code, "category": category}
    # hh.{i}.name or firm.{j}.name
    if len(parts) != 3:
        raise AccountsBridgeError(f"malformed account: {account!r}")
    try:
        int(parts[1])
    except ValueError as exc:
        raise AccountsBridgeError(f"malformed agent index in {account!r}") from exc
    name = parts[2]
    if name in STOCK_MAP and name != "capital":
        dimension, code, category = STOCK_MAP[name]
    elif name == "capital" and prefix == "firm":
        dimension, code, category = STOCK_MAP["capital"]
    elif name in FLOW_MAP and prefix in ("hh", "firm"):
        dimension, code, category = FLOW_MAP[name]
    elif name in ("wage_income", "transfer_income", "interest_income",
                  "consumption", "tax", "interest_paid") and prefix == "hh":
        dimension, code, category = FLOW_MAP[name]
    elif name in ("sales", "wage_cost", "procurement_cost", "interest_paid", "tax",
                  "investment") and prefix == "firm":
        if name == "interest_paid":
            dimension, code, category = (Dimension.CURRENT, CurrentFlow.INTEREST.value,
                                         "expenditure")
        elif name == "tax":
            dimension, code, category = (Dimension.CURRENT, CurrentFlow.DIRECT_TAX.value,
                                         "expenditure")
        else:
            dimension, code, category = FLOW_MAP[name]
    else:
        raise AccountsBridgeError(f"unknown {prefix} account: {account!r}")
    return {"account": account, "sector": sector, "dimension": dimension.value,
            "code": code, "category": category}


# ------------------------------------------------------------------------------------ aggregates
@dataclass(frozen=True)
class SectorAccounts:
    sector: str
    income: Decimal
    expenditure: Decimal
    saving: Decimal
    investment: Decimal
    financial_assets: Decimal
    financial_liabilities: Decimal
    net_financial_worth: Decimal
    real_assets: Decimal
    net_worth: Decimal
    current: dict
    financial: dict

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["income"] = str(self.income)
        payload["expenditure"] = str(self.expenditure)
        payload["saving"] = str(self.saving)
        payload["investment"] = str(self.investment)
        payload["financial_assets"] = str(self.financial_assets)
        payload["financial_liabilities"] = str(self.financial_liabilities)
        payload["net_financial_worth"] = str(self.net_financial_worth)
        payload["real_assets"] = str(self.real_assets)
        payload["net_worth"] = str(self.net_worth)
        payload["current"] = {key: str(value) for key, value in self.current.items()}
        payload["financial"] = {key: str(value) for key, value in self.financial.items()}
        return payload


@dataclass(frozen=True)
class AccountAggregates:
    schema: str
    sectors: dict
    totals: dict
    checks: dict
    mapping: list
    ledger_digest: str
    transactions: int
    periods: int
    label: str

    def as_dict(self) -> dict:
        return {
            "schema": self.schema,
            "label": self.label,
            "periods": self.periods,
            "transactions": self.transactions,
            "ledger_digest": self.ledger_digest,
            "sectors": {name: accounts.as_dict() for name, accounts in self.sectors.items()},
            "totals": {key: (str(value) if isinstance(value, Decimal) else value)
                       for key, value in self.totals.items()},
            "checks": self.checks,
            "mapping": self.mapping,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)


SCHEMA = "AMFE-AGENTIC-ACCOUNTS/1.0.0"


# ---------------------------------------------------------------------------------------- bridge
class AccountsBridge:
    """Read-only adapter from the ABM ledger to standard SFC sector accounts."""

    def __init__(self, result: SimulationResult, ledger: Ledger) -> None:
        if not isinstance(result, SimulationResult):
            raise AccountsBridgeError("result must be a SimulationResult")
        if not isinstance(ledger, Ledger):
            raise AccountsBridgeError("ledger must be a Ledger")
        self.result = result
        self.ledger = ledger

    # -- checks ---------------------------------------------------------------------------
    def validate_against_sfc(self, raise_on_fail: bool = True) -> dict:
        """Run every aggregate check; raise AccountsBridgeError unless ``raise_on_fail`` is False."""
        aggregates = self.build(raise_on_fail=raise_on_fail)
        return dict(aggregates.checks)

    # -- build ----------------------------------------------------------------------------
    def build(self, raise_on_fail: bool = True) -> AccountAggregates:
        classified = {}
        for account in self.ledger.accounts:
            classified[account] = _classify(account)
        sectors: dict[str, dict] = {}
        for name in SECTORS:
            sectors[name] = {"income": D(0), "expenditure": D(0), "investment": D(0),
                             "financial_assets": D(0), "financial_liabilities": D(0),
                             "real_assets": D(0), "current": {}, "financial": {}}
        mapping = []
        for account, info in classified.items():
            balance = self.ledger.balance(account)
            mapping.append({**info, "balance": str(balance)})
            sector = info["sector"]
            if sector is None:
                continue
            bucket = sectors[sector]
            if info["dimension"] == Dimension.CURRENT.value:
                if info["category"] == "income":
                    bucket["income"] += balance
                elif info["category"] == "expenditure":
                    bucket["expenditure"] += balance
                else:
                    raise AccountsBridgeError(f"current account without category: {account!r}")
                key = f"{info['code']}:{info['category']}"
                bucket["current"][key] = bucket["current"].get(key, D(0)) + balance
            elif info["dimension"] == Dimension.FINANCIAL.value:
                if balance >= 0:
                    bucket["financial_assets"] += balance
                else:
                    bucket["financial_liabilities"] += balance
                key = info["code"]
                bucket["financial"][key] = bucket["financial"].get(key, D(0)) + balance
            elif info["dimension"] == Dimension.REAL.value:
                bucket["real_assets"] += balance
                bucket["investment"] += balance
            elif info["dimension"] == Dimension.CAPITAL.value:
                # memo/capital flows are not part of the aggregate sector accounts
                continue
            else:
                raise AccountsBridgeError(f"unmapped dimension for {account!r}")
        sector_accounts = {}
        for name, bucket in sectors.items():
            saving = bucket["income"] + bucket["expenditure"]
            net_financial = bucket["financial_assets"] + bucket["financial_liabilities"]
            net_worth = net_financial + bucket["real_assets"]
            sector_accounts[name] = SectorAccounts(
                sector=name,
                income=bucket["income"], expenditure=bucket["expenditure"], saving=saving,
                investment=bucket["investment"],
                financial_assets=bucket["financial_assets"],
                financial_liabilities=bucket["financial_liabilities"],
                net_financial_worth=net_financial,
                real_assets=bucket["real_assets"], net_worth=net_worth,
                current=dict(sorted(bucket["current"].items())),
                financial=dict(sorted(bucket["financial"].items())),
            )
        total_income = sum((item.income for item in sector_accounts.values()), D(0))
        total_expenditure = sum((item.expenditure for item in sector_accounts.values()), D(0))
        total_saving = sum((item.saving for item in sector_accounts.values()), D(0))
        total_investment = sum((item.investment for item in sector_accounts.values()), D(0))
        totals = {
            "income": total_income,
            "expenditure": total_expenditure,
            "saving": total_saving,
            "investment": total_investment,
            "current_account": D(0),
            "net_financial_worth": sum((item.net_financial_worth
                                        for item in sector_accounts.values()), D(0)),
            "real_assets": sum((item.real_assets for item in sector_accounts.values()), D(0)),
            "net_worth": sum((item.net_worth for item in sector_accounts.values()), D(0)),
        }
        checks = self._checks(sector_accounts, totals)
        aggregates = AccountAggregates(
            schema=SCHEMA, sectors=sector_accounts, totals=totals, checks=checks,
            mapping=mapping, ledger_digest=self.ledger.digest(),
            transactions=self.ledger.transaction_count(), periods=len(self.result.periods),
            label=self.result.label)
        if raise_on_fail and not all(checks.values()):
            failed = [name for name, value in checks.items() if not value]
            raise AccountsBridgeError(f"aggregate checks failed: {failed}")
        return aggregates

    # -- independent checks ---------------------------------------------------------------
    def _checks(self, sectors: Mapping[str, SectorAccounts], totals: Mapping[str, D]) -> dict:
        double_entry = True
        for record in self.ledger.transactions:
            total = sum((D(amount) for amount in record.get("postings", {}).values()), D(0))
            if total != 0:
                double_entry = False
                break
        ledger_total = self.ledger.total() == 0
        financial_mirroring = totals["net_financial_worth"] == 0
        sector_saving = all(item.saving == item.income + item.expenditure
                            for item in sectors.values())
        sfc_identity = totals["saving"] == totals["investment"] + totals["current_account"]
        net_worth = all(item.net_worth == item.net_financial_worth + item.real_assets
                        for item in sectors.values()) \
            and totals["net_worth"] == totals["real_assets"]
        closed = not any(account.startswith("ext.") for account in self.ledger.accounts)
        period_saving = sum((period.saving for period in self.result.periods), D(0))
        period_investment = sum((period.investment for period in self.result.periods), D(0))
        result_consistency = (
            self.ledger.digest() == self.result.ledger_digest
            and self.ledger.transaction_count() == self.result.transactions
            and period_saving == totals["saving"]
            and period_investment == totals["investment"]
        )
        return {
            "double_entry": double_entry,
            "ledger_total": ledger_total,
            "financial_mirroring": financial_mirroring,
            "sector_saving": sector_saving,
            "sfc_identity": sfc_identity,
            "net_worth": net_worth,
            "closed_economy": closed,
            "result_consistency": result_consistency,
        }


# ----------------------------------------------------------------------------------- functions
def build_account_aggregates(result: SimulationResult, ledger: Ledger) -> AccountAggregates:
    """Build standard SFC aggregate accounts from a simulator result and its ledger."""
    return AccountsBridge(result, ledger).build()


def export_sector_accounts(result: SimulationResult, ledger: Ledger) -> dict:
    """Plain ``{sector: {...}}`` export (all values strings for JSON safety)."""
    aggregates = build_account_aggregates(result, ledger)
    return {name: accounts.as_dict() for name, accounts in aggregates.sectors.items()}


def validate_against_sfc(result: SimulationResult, ledger: Ledger,
                         raise_on_fail: bool = True) -> dict:
    """Re-run the independent aggregate checks; fail-closed by default."""
    return AccountsBridge(result, ledger).validate_against_sfc(raise_on_fail=raise_on_fail)


def render_report(aggregates: AccountAggregates) -> str:
    """Deterministic text report of the aggregate sector accounts."""
    lines = [f"AMFE G2: ABM -> SFC accounts ({aggregates.schema})",
             f"label: {aggregates.label} · periods: {aggregates.periods} · "
             f"transactions: {aggregates.transactions} · ledger: {aggregates.ledger_digest[:16]}…",
             "",
             "sector       income   expenditure    saving  investment   fin.assets  fin.liab. "
             "net fin.   real assets  net worth"]
    for name in SECTORS:
        item = aggregates.sectors[name]
        lines.append(
            f"{name:<11} {float(item.income):>8.3f} {float(item.expenditure):>12.3f} "
            f"{float(item.saving):>9.3f} {float(item.investment):>10.3f} "
            f"{float(item.financial_assets):>11.3f} {float(item.financial_liabilities):>10.3f} "
            f"{float(item.net_financial_worth):>9.3f} {float(item.real_assets):>12.3f} "
            f"{float(item.net_worth):>10.3f}")
    lines += [
        "",
        f"total: income={totals_map(aggregates)['income']}, "
        f"saving={totals_map(aggregates)['saving']}, "
        f"investment={totals_map(aggregates)['investment']}, "
        f"CA={totals_map(aggregates)['current_account']}",
        "checks: " + ", ".join(f"{name}={value}" for name, value in aggregates.checks.items()),
        "",
        "This is a toy ABM aggregate, not a forecast or an admission.",
        "",
    ]
    return "\n".join(lines)


def totals_map(aggregates: AccountAggregates) -> dict:
    """String-friendly totals for reports/JSON."""
    return {key: (format(value, "f") if isinstance(value, Decimal) else value)
            for key, value in aggregates.totals.items()}


__all__ = [
    "AccountAggregates",
    "AccountsBridge",
    "AccountsBridgeError",
    "SectorAccounts",
    "build_account_aggregates",
    "export_sector_accounts",
    "render_report",
    "totals_map",
    "validate_against_sfc",
]
