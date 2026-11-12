"""Typed period reports and deterministic serialisation of the world core."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from .physical import PhysicalOutcome


def _f(value: float, digits: int = 12) -> float:
    number = round(float(value), digits)
    return 0.0 if number == 0.0 else number


def _d(value: Decimal | int | str) -> str:
    return str(value)


@dataclass(frozen=True)
class CountryPeriodReport:
    """One country's integrated period result (physical + monetary + external)."""

    name: str
    currency: str
    period: int
    price_level: Decimal
    inflation: float
    expected_inflation: float
    expected_inflation_next: float
    policy_rate: float
    output_gap: float
    physical: PhysicalOutcome
    nominal_gdp: Decimal
    consumption: Decimal
    government_consumption: Decimal
    investment: Decimal
    wages: Decimal
    transfers: Decimal
    taxes: Decimal
    interest_firm: Decimal
    interest_government: Decimal
    household_income: Decimal
    household_saving: Decimal
    debt: Decimal
    debt_ratio: float
    cash: Decimal
    exports_local: Decimal
    exports_numeraire: Decimal
    accounts: object          # amfe.accounts.model.PeriodReport
    external: Mapping[str, object]
    checks: Mapping[str, bool]
    residuals: Mapping[str, float]
    # -- open SFC extension (docs/93); defaults keep closed-economy reports unchanged
    open_economy: bool = False
    imports_local: Decimal = Decimal(0)
    net_exports_local: Decimal = Decimal(0)
    nfa_local: Decimal = Decimal(0)
    external_assets_local: Decimal = Decimal(0)
    external_liabilities_local: Decimal = Decimal(0)
    row_net_worth_local: Decimal = Decimal(0)
    open_gdp_expenditure: Decimal = Decimal(0)
    bop_current_account_local: Decimal = Decimal(0)
    bop_financial_account_local: Decimal = Decimal(0)
    services_exports_local: Decimal = Decimal(0)
    services_imports_local: Decimal = Decimal(0)
    net_primary_income_local: Decimal = Decimal(0)
    net_secondary_income_local: Decimal = Decimal(0)
    net_capital_transfers_local: Decimal = Decimal(0)
    current_account_local: Decimal = Decimal(0)
    gni_local: Decimal = Decimal(0)
    row_deposits_local: Decimal = Decimal(0)
    settlement_local: Decimal = Decimal(0)
    settlement_numeraire: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "external", MappingProxyType(dict(self.external)))
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "residuals", MappingProxyType(dict(self.residuals)))

    @property
    def ok(self) -> bool:
        return all(bool(value) for value in self.checks.values())

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "currency": self.currency,
            "period": self.period,
            "price_level": _d(self.price_level),
            "inflation": _f(self.inflation),
            "expected_inflation": _f(self.expected_inflation),
            "expected_inflation_next": _f(self.expected_inflation_next),
            "policy_rate": _f(self.policy_rate),
            "output_gap": _f(self.output_gap),
            "physical": self.physical.as_dict(),
            "nominal_gdp": _d(self.nominal_gdp),
            "consumption": _d(self.consumption),
            "government_consumption": _d(self.government_consumption),
            "investment": _d(self.investment),
            "wages": _d(self.wages),
            "transfers": _d(self.transfers),
            "taxes": _d(self.taxes),
            "interest_firm": _d(self.interest_firm),
            "interest_government": _d(self.interest_government),
            "household_income": _d(self.household_income),
            "household_saving": _d(self.household_saving),
            "debt": _d(self.debt),
            "debt_ratio": _f(self.debt_ratio),
            "cash": _d(self.cash),
            "exports_local": _d(self.exports_local),
            "exports_numeraire": _d(self.exports_numeraire),
            "accounts": {
                "period": self.accounts.period,
                "gdp_production": _d(self.accounts.gdp_production),
                "gdp_expenditure": _d(self.accounts.gdp_expenditure),
                "gdp_income": _d(self.accounts.gdp_income),
                "savings": {key: _d(value) for key, value in self.accounts.savings.items()},
                "net_lending": {key: _d(value)
                                for key, value in self.accounts.net_lending.items()},
                "revaluation": {key: _d(value)
                                for key, value in self.accounts.revaluation.items()},
                "closing_net_worth": {key: _d(value) for key, value
                                      in self.accounts.closing_net_worth.items()},
            },
            "external": _json_ready(self.external),
            "checks": {key: bool(value) for key, value in self.checks.items()},
            "residuals": {key: _f(value) for key, value in self.residuals.items()},
            **({"open_sfc": {
                "exports_local": _d(self.exports_local),
                "imports_local": _d(self.imports_local),
                "net_exports_local": _d(self.net_exports_local),
                "nfa_local": _d(self.nfa_local),
                "external_assets_local": _d(self.external_assets_local),
                "external_liabilities_local": _d(self.external_liabilities_local),
                "row_net_worth_local": _d(self.row_net_worth_local),
                "open_gdp_expenditure": _d(self.open_gdp_expenditure),
                "bop_current_account_local": _d(self.bop_current_account_local),
                "bop_financial_account_local": _d(self.bop_financial_account_local),
                "services_exports_local": _d(self.services_exports_local),
                "services_imports_local": _d(self.services_imports_local),
                "net_primary_income_local": _d(self.net_primary_income_local),
                "net_secondary_income_local": _d(self.net_secondary_income_local),
                "net_capital_transfers_local": _d(self.net_capital_transfers_local),
                "current_account_local": _d(self.current_account_local),
                "gni_local": _d(self.gni_local),
                "row_deposits_local": _d(self.row_deposits_local),
                "settlement_local": _d(self.settlement_local),
                "settlement_numeraire": _d(self.settlement_numeraire),
            }} if self.open_economy else {}),
        }


def _json_ready(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float):
        return _f(value)
    return value


@dataclass(frozen=True)
class WorldPeriodReport:
    """The integrated result of one world period."""

    period: int
    scenario: str
    fx: Mapping[str, str]
    countries: Mapping[str, CountryPeriodReport]
    external: Mapping[str, object]
    dynamics: Mapping[str, object]
    checks: Mapping[str, bool]
    residuals: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "countries", MappingProxyType(dict(self.countries)))
        object.__setattr__(self, "external", MappingProxyType(dict(self.external)))
        object.__setattr__(self, "dynamics", MappingProxyType(dict(self.dynamics)))
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "residuals", MappingProxyType(dict(self.residuals)))
        object.__setattr__(self, "fx", MappingProxyType(dict(self.fx)))

    @property
    def ok(self) -> bool:
        return all(bool(value) for value in self.checks.values())

    def country(self, name: str) -> CountryPeriodReport:
        try:
            return self.countries[name]
        except KeyError as exc:
            raise KeyError(f"unknown country in report: {name}") from exc

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "scenario": self.scenario,
            "fx": {currency: str(rate) for currency, rate in self.fx.items()},
            "countries": {name: report.as_dict()
                          for name, report in self.countries.items()},
            "external": _json_ready(self.external),
            "dynamics": _json_ready(self.dynamics),
            "checks": {key: bool(value) for key, value in self.checks.items()},
            "residuals": {key: _f(value) for key, value in self.residuals.items()},
            "ok": self.ok,
        }


def canonical_json(payload: object) -> str:
    """Deterministic JSON used for the reproducibility digest."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def report_digest(reports: list[WorldPeriodReport]) -> str:
    payload = [report.as_dict() for report in reports]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorldRunResult:
    """Result of a whole deterministic world run."""

    scenario: str
    periods: int
    reports: tuple[WorldPeriodReport, ...]
    digest: str = ""

    @property
    def ok(self) -> bool:
        return all(report.ok for report in self.reports)

    @property
    def last(self) -> WorldPeriodReport:
        if not self.reports:
            raise IndexError("the run has no completed periods")
        return self.reports[-1]

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "periods": self.periods,
            "digest": self.digest,
            "ok": self.ok,
            "reports": [report.as_dict() for report in self.reports],
        }


__all__ = [
    "CountryPeriodReport", "WorldPeriodReport", "WorldRunResult",
    "canonical_json", "report_digest",
]
