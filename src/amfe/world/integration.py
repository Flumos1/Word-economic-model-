"""Integration layer: the external-accounts hook and cross-layer invariants.

This module sits between the four prototype layers.  It does **not** import the layers'
internals beyond their public APIs and does not modify them; it only

* implements the :class:`amfe.international.ExternalAccountsHook` protocol with an
  observational recorder (the international contract of docs/78 §5);
* collects the cross-layer residuals (physical vs monetary, accounts vs external,
  FX revaluation vs national wealth, external mirroring);
* turns them into a single ``ok`` flag and raises :class:`IntegrationError` when a
  required identity is violated.

The important limitation is explicit: the domestic :class:`~amfe.accounts.AccountSystem`
is a *closed-economy* kernel, so external trade is not posted into its four sectors.  The
hook records the external events and the cross-layer check verifies that the external
positions and the domestic net worth reconcile; docs/85 §4 describes what is a stub.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Mapping

from amfe.international import ExternalAccountsHook

TOLERANCE = Decimal("1e-9")


class WorldError(ValueError):
    """Base class for world-core errors."""


class IntegrationError(WorldError):
    """A cross-layer identity is violated or the integration contract is broken."""


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------
def money(value: object) -> Decimal:
    """Convert a physical float/int to an exact Decimal for the monetary layer."""
    if isinstance(value, bool):
        raise IntegrationError("boolean is not a monetary amount")
    if isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(str(value))
    if not number.is_finite():
        raise IntegrationError(f"non-finite monetary amount: {value!r}")
    return number


def _abs(value: Decimal) -> Decimal:
    return value if value >= 0 else -value


def residual_ok(value: object, tolerance: float = 1e-9) -> bool:
    """Tolerance comparison for *float* physical residuals."""
    if isinstance(value, Decimal):
        return _abs(value) <= Decimal(str(tolerance))
    number = float(value)
    return math.isfinite(number) and abs(number) <= tolerance


# ---------------------------------------------------------------------------
# External accounts hook (observational adapter, docs/78 §5)
# ---------------------------------------------------------------------------
@dataclass
class IntegrationHook:
    """Records the external events without changing the external model.

    The lists are append-only.  A period report stores the slice that belongs to the
    closed period; the recorder is never authoritative over ``TwoCountryWorld``.
    """

    claims: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    revaluations: list[dict] = field(default_factory=list)

    def on_external_claim(self, *, holder, issuer, instrument, currency,
                          amount: Decimal, numeraire_value: Decimal) -> None:
        self.claims.append({
            "holder": holder, "issuer": issuer, "instrument": instrument,
            "currency": currency, "amount": amount,
            "numeraire_value": numeraire_value})

    def on_external_flow(self, *, kind, category, country,
                         numeraire_value: Decimal) -> None:
        self.flows.append({"kind": kind, "category": category, "country": country,
                           "numeraire_value": numeraire_value})

    def on_revaluation(self, *, country, currency, amount: Decimal) -> None:
        # The external layer fires on any non-zero local revaluation; numerical dust
        # from currency conversion (1e-27) is not an economic event, so the recorder
        # ignores it instead of polluting the period report.
        if abs(amount) <= Decimal("1e-18"):
            return
        self.revaluations.append({"country": country, "currency": currency,
                                  "amount": amount})

    def reset(self) -> None:
        self.claims.clear()
        self.flows.clear()
        self.revaluations.clear()

    # -- period slices ---------------------------------------------------
    def claim_slice(self, start: int) -> list[dict]:
        return self.claims[start:]

    def flow_slice(self, start: int) -> list[dict]:
        return self.flows[start:]

    def revaluation_slice(self, start: int) -> list[dict]:
        return self.revaluations[start:]

    def summary(self, start_claim: int = 0, start_flow: int = 0,
                start_revaluation: int = 0) -> dict:
        claims = self.claim_slice(start_claim)
        flows = self.flow_slice(start_flow)
        revaluations = self.revaluation_slice(start_revaluation)
        return {
            "claims": len(claims),
            "claim_numeraire_by_holder": {
                holder: str(sum((entry["numeraire_value"] for entry in claims
                                 if entry["holder"] == holder), Decimal(0)))
                for holder in sorted({entry["holder"] for entry in claims})},
            "flows": len(flows),
            "flow_numeraire_by_country": {
                country: str(sum((entry["numeraire_value"] for entry in flows
                                  if entry["country"] == country), Decimal(0)))
                for country in sorted({entry["country"] for entry in flows})},
            "revaluations": [
                {"country": entry["country"], "currency": entry["currency"],
                 "amount": str(entry["amount"])}
                for entry in revaluations],
        }


class CompositeHook(IntegrationHook):
    """The core's own recorder plus an optional user hook (observational forwarding).

    The core always needs its own record of the external events for the cross-layer
    checks; a caller-supplied hook (for diagnostics or tests) is forwarded every call
    *after* the recorder.  If the user hook raises, the exception propagates and the
    core wraps it into :class:`WorldError`, so a rejected post stops the close exactly
    as the international contract requires.
    """

    def __init__(self, extra: object | None = None):
        super().__init__()
        self.extra = extra

    def on_external_claim(self, **kwargs) -> None:
        super().on_external_claim(**kwargs)
        if self.extra is not None:
            self.extra.on_external_claim(**kwargs)

    def on_external_flow(self, **kwargs) -> None:
        super().on_external_flow(**kwargs)
        if self.extra is not None:
            self.extra.on_external_flow(**kwargs)

    def on_revaluation(self, **kwargs) -> None:
        super().on_revaluation(**kwargs)
        if self.extra is not None:
            self.extra.on_revaluation(**kwargs)


def is_hook(value: object) -> bool:
    """Runtime protocol check (the international module exposes it as runtime_checkable)."""
    return isinstance(value, ExternalAccountsHook)


# ---------------------------------------------------------------------------
# Wealth state and bridges
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CountryWealthState:
    """End-of-period national wealth in the local currency and in the numeraire."""

    name: str
    price_level: Decimal
    rate: Decimal
    net_worth_local: Decimal
    nfa_numeraire: Decimal

    def numeraire_net_worth(self) -> Decimal:
        return self.net_worth_local * self.rate + self.nfa_numeraire


def wealth_bridge(previous: CountryWealthState, current: CountryWealthState,
                  savings_local: Decimal, revaluation_local: Decimal,
                  nfa_transactions_numeraire: Decimal,
                  nfa_revaluation_numeraire: Decimal) -> tuple[dict, dict]:
    """Check the two reconciliation identities of one country.

    ``local`` bridge::

        dNW_local = savings + revaluation                (accounts layer)

    ``national`` bridge (numeraire)::

        d(NW_local*rate + NFA) = savings*rate + revaluation*rate
                                 + NW_prev*(rate - rate_prev)
                                 + NFA_transactions + NFA_revaluation

    The translation term is the FX revaluation of the domestic net worth; it is not a
    flow.  Both identities are exact algebra given the two layers' reports; the check
    catches integration mistakes (wrong rate, missing hook revaluation, double counting).
    """
    if previous.name != current.name:
        raise IntegrationError("wealth bridge compares different countries")
    d_nw = current.net_worth_local - previous.net_worth_local
    local_residual = d_nw - (savings_local + revaluation_local)
    translation = previous.net_worth_local * (current.rate - previous.rate)
    savings_numeraire = savings_local * current.rate
    revaluation_numeraire = revaluation_local * current.rate
    d_nfa = current.nfa_numeraire - previous.nfa_numeraire
    nfa_residual = d_nfa - (nfa_transactions_numeraire + nfa_revaluation_numeraire)
    d_wealth = (current.numeraire_net_worth()
                - previous.numeraire_net_worth())
    expected = (savings_numeraire + revaluation_numeraire + translation
                + nfa_transactions_numeraire + nfa_revaluation_numeraire)
    wealth_residual = d_wealth - expected
    checks = {
        "net_worth_bridge_local": _abs(local_residual) <= TOLERANCE,
        "nfa_identity": _abs(nfa_residual) <= TOLERANCE,
        "national_wealth_bridge": _abs(wealth_residual) <= TOLERANCE,
    }
    residuals = {
        "net_worth_bridge_local": float(_abs(local_residual)),
        "nfa_identity": float(_abs(nfa_residual)),
        "national_wealth_bridge": float(_abs(wealth_residual)),
    }
    return checks, residuals


def world_wealth_check(states: Iterable[CountryWealthState],
                       real_assets_local: Mapping[str, Decimal]) -> tuple[dict, dict]:
    """World net worth (domestic + external) equals the world's real assets in numeraire."""
    states = list(states)
    total_wealth = sum((state.numeraire_net_worth() for state in states), Decimal(0))
    total_real = sum((real_assets_local[state.name] * state.rate for state in states),
                     Decimal(0))
    residual = total_wealth - total_real
    nfa_mirror = sum((state.nfa_numeraire for state in states), Decimal(0))
    checks = {
        "world_net_worth_equals_real_assets": _abs(residual) <= TOLERANCE,
        "external_nfa_mirror": _abs(nfa_mirror) <= TOLERANCE,
    }
    residuals = {
        "world_net_worth_equals_real_assets": float(_abs(residual)),
        "external_nfa_mirror": float(_abs(nfa_mirror)),
    }
    return checks, residuals


# ---------------------------------------------------------------------------
# Layer checks
# ---------------------------------------------------------------------------
def physical_checks(outcome, physical, scenario) -> tuple[dict, dict]:
    """Physical layer: material balance, no double counting and limit feasibility."""
    checks = {
        "physical_material_balance": residual_ok(outcome.material_residual),
        "physical_value_added_equals_final_uses": residual_ok(outcome.value_added_residual),
        "physical_no_double_counting": (outcome.gross_output
                                        >= outcome.value_added - 1e-9),
        "physical_scale_in_unit_interval": 0.0 <= outcome.result.scale <= 1.0,
        "energy_limit_respected": physical.energy_feasible(outcome, scenario.energy_factor),
        "labour_limit_respected": physical.labour_feasible(outcome, scenario.labour_factor),
    }
    residuals = {
        "physical_material_balance": float(outcome.material_residual),
        "physical_value_added_equals_final_uses": float(outcome.value_added_residual),
        "physical_gross_output_minus_value_added": float(
            outcome.gross_output - outcome.value_added),
    }
    return checks, residuals


def accounts_checks(accounts_report, expected_nominal_gdp: Decimal,
                    expected_consumption: Decimal,
                    expected_government: Decimal,
                    expected_investment: Decimal,
                    household_income: Decimal,
                    household_consumption: Decimal) -> tuple[dict, dict]:
    """Accounts layer: three GDP measures and the physical-to-nominal valuation."""
    report = accounts_report
    gdp_production = report.gdp_production
    gdp_expenditure = report.gdp_expenditure
    gdp_income = report.gdp_income
    three_measures = (gdp_production == gdp_expenditure == gdp_income)
    total_expenditure = (report.household_consumption
                         + report.government_consumption
                         + sum(report.inventory_change.values(), Decimal(0))
                         + sum(report.gross_fixed_capital_formation.values(), Decimal(0)))
    checks = {
        "accounts_gdp_three_measures": three_measures,
        "accounts_gdp_matches_physical": _abs(
            gdp_production - expected_nominal_gdp) <= TOLERANCE,
        "accounts_expenditure_matches_shares": (
            _abs(report.household_consumption - expected_consumption) <= TOLERANCE
            and _abs(report.government_consumption - expected_government) <= TOLERANCE
            and _abs(report.inventory_change.get("firm", Decimal(0))
                     + report.gross_fixed_capital_formation.get("firm", Decimal(0))
                     - expected_investment) <= TOLERANCE),
        "accounts_household_liquidity": household_consumption <= household_income,
    }
    residuals = {
        "accounts_gdp_production_minus_physical": float(
            _abs(gdp_production - expected_nominal_gdp)),
        "accounts_gdp_expenditure_minus_total": float(_abs(total_expenditure - gdp_expenditure)),
        "accounts_consumption_minus_expected": float(
            _abs(report.household_consumption - expected_consumption)),
        "accounts_government_minus_expected": float(
            _abs(report.government_consumption - expected_government)),
        "accounts_household_income_minus_consumption": float(
            _abs(household_income - household_consumption)),
    }
    return checks, residuals


def external_checks(external_report: dict, claim_entries: list[dict],
                    flow_entries: list[dict], revaluation_entries: list[dict],
                    expected_trade_numeraire: Mapping[str, Decimal]) -> tuple[dict, dict]:
    """External layer: mirroring, BoP/IIP checks and the trade-claim bridge."""
    checks_map = dict(external_report["checks"])
    checks_map.pop("ok", None)
    checks = {f"external_{name}": bool(value) for name, value in checks_map.items()}
    mirror = external_report["mirror"]
    checks["external_mirror_zero"] = all(value == 0 for value in mirror.values())
    # The two trade credits per period must carry exactly the modelled export value for
    # each holder; otherwise the hook and the monetary block would disagree.
    holder_values: dict[str, Decimal] = {}
    for entry in claim_entries:
        holder_values[entry["holder"]] = holder_values.get(
            entry["holder"], Decimal(0)) + entry["numeraire_value"]
    trade_residual = Decimal(0)
    for holder, expected in expected_trade_numeraire.items():
        trade_residual += _abs(holder_values.get(holder, Decimal(0)) - expected)
    checks["external_trade_claims_match"] = trade_residual <= TOLERANCE
    residual = {
        "external_mirror_total": float(_abs(sum((value for value in mirror.values()),
                                                 Decimal(0)))),
        "external_trade_claims": float(trade_residual),
    }
    return checks, residual


def debt_checks(debt_previous: Decimal, debt_next: Decimal,
                nominal_gdp_previous: Decimal, nominal_gdp_next: Decimal,
                interest_paid: Decimal, primary_balance: Decimal,
                cash_change: Decimal) -> tuple[dict, dict]:
    """Debt identity from :func:`amfe.dynamics.debt.debt_step` fitted to accounts data.

    ``b_t = debt_t / Y_t``; the government budget identity implies
    ``debt_{t+1} = debt_t + interest_t - PB_t + dCash_t`` and therefore, with
    ``r = interest_t / debt_t`` and ``pb_t = PB_t / Y_{t+1}``::

        b_{t+1} = debt_step(b_t, r, g, pb_t) + dCash_t / Y_{t+1}

    ``r`` is the effective nominal rate implied by the interest actually paid in the
    accounts layer; ``g`` is nominal GDP growth.  The dynamics formula is re-used rather
    than re-implemented, and an exact Decimal flow identity
    ``debt_{t+1} = debt_t + interest_t - PB_t + dCash_t`` is checked alongside it.
    """
    from amfe.dynamics import debt_step

    if nominal_gdp_previous <= 0 or nominal_gdp_next <= 0:
        return ({"debt_identity": False, "debt_flow_identity": False},
                {"debt_identity": float("inf"), "debt_flow_identity": float("inf")})
    b_previous = debt_previous / nominal_gdp_previous
    b_next = debt_next / nominal_gdp_next
    growth = float(nominal_gdp_next / nominal_gdp_previous) - 1.0
    pb_share = primary_balance / nominal_gdp_next
    rate = float(interest_paid / debt_previous) if debt_previous > 0 else 0.0
    expected_next = debt_step(float(b_previous), rate, growth, float(pb_share))
    expected_next += float(cash_change / nominal_gdp_next)
    residual = float(b_next) - expected_next
    exact_expected = debt_previous + interest_paid - primary_balance + cash_change
    exact_residual = debt_next - exact_expected
    checks = {
        "debt_identity": abs(residual) <= 1e-9,
        "debt_flow_identity": _abs(exact_residual) <= TOLERANCE,
    }
    residuals = {
        "debt_identity": abs(residual),
        "debt_flow_identity": float(_abs(exact_residual)),
    }
    return checks, residuals


# ---------------------------------------------------------------------------
# Combined assertion
# ---------------------------------------------------------------------------
def merge_checks(*pairs: tuple[dict, dict]) -> tuple[dict, dict]:
    checks: dict[str, bool] = {}
    residuals: dict[str, float] = {}
    for group_checks, group_residuals in pairs:
        checks.update(group_checks)
        residuals.update(group_residuals)
    return checks, residuals


def assert_integration(checks: Mapping[str, bool],
                       residuals: Mapping[str, float] | None = None) -> None:
    """Raise :class:`IntegrationError` unless every required cross-layer check passes."""
    if not isinstance(checks, Mapping) or not checks:
        raise IntegrationError("integration checks must be a non-empty mapping")
    failed = sorted(name for name, value in checks.items() if not bool(value))
    if failed:
        details = ""
        if residuals:
            details = "; residuals: " + ", ".join(
                f"{name}={residuals.get(name)}" for name in failed
                if name in residuals)
        raise IntegrationError(f"integration invariants failed: {failed}{details}")


__all__ = [
    "WorldError", "IntegrationError",
    "money", "residual_ok", "IntegrationHook", "CompositeHook", "is_hook",
    "CountryWealthState", "wealth_bridge", "world_wealth_check",
    "debt_checks", "physical_checks", "accounts_checks", "external_checks",
    "merge_checks", "assert_integration",
]
