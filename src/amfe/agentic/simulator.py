"""Minimal deterministic agent-based simulator with stock-flow consistency (AMFE 2.0, track E).

Scope: households, firms, one bank and the government in a closed economy. Firms produce with
labour, pay wages and taxes, buy intermediate inputs from each other (the share of procurement
delegated to AI agents is a parameter), invest out of retained earnings (own-account capital
formation) and set prices from unit costs through a damped fixed-point iteration. Households consume
with a marginal propensity to consume, save the rest, pay income taxes and receive transfers. The bank
issues loans on demand (with a per-firm credit limit), pays deposit interest and keeps equity. The
government spends, taxes, pays unemployment benefits and borrows from the bank when needed.

Accounting contract (enforced, fail-closed):

* **double entry** -- every transaction is a set of postings whose sum is exactly zero
  (:class:`LedgerError` otherwise); the ledger records every transaction for audit/digest;
* **financial conservation** -- the sum of all financial stock accounts is exactly zero after every
  period (:class:`SFCError` otherwise);
* **closed-economy SFC** -- saving (sum of sector saving flows) equals investment plus the current
  account (``S = I + CA``, ``CA = 0`` because there is no external sector);
* **bank identity** -- bank equity equals loans + government credit - deposits (liabilities);
* **convergence** -- the price iteration must converge within ``max_iterations``; otherwise
  :class:`ConvergenceError` (no infinite loops);
* **bounded parameters** -- invalid values raise :class:`ParameterError`; credit limits and
  non-positive prices fail closed.

The simulator is a **toy model for mechanism and accounting consistency**. It does not predict the
real economy, does not calibrate to data and is not an admission or investment advice. All values are
in arbitrary money units.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, field, fields
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Mapping

D = Decimal
_QUANTUM = D("0.000000000000000001")
CA = D(0)  # closed economy: no external sector


# ---------------------------------------------------------------------------------------- errors
class SimulationError(RuntimeError):
    """Base class for simulator failures (all are fail-closed)."""


class ParameterError(SimulationError):
    """Invalid or inconsistent parameters."""


class LedgerError(SimulationError):
    """A transaction does not balance (postings do not sum to zero)."""


class SFCError(SimulationError):
    """A stock-flow / accounting invariant is violated."""


class ConvergenceError(SimulationError):
    """The price iteration did not converge within the iteration budget."""


# ------------------------------------------------------------------------------------ parameters
@dataclass(frozen=True)
class Parameters:
    """Frozen simulator parameters (all money values in arbitrary units)."""

    n_households: int = 20
    n_firms: int = 5
    periods: int = 16
    seed: int = 20261116
    wage: float = 1.0
    labour_supply: int = 1               # units supplied per household
    productivity: float = 1.0            # output per labour unit, baseline per firm
    input_coeff: float = 0.08            # intermediate input per unit of output
    markup: float = 1.03
    tax_rate: float = 0.20               # household income tax
    benefit: float = 0.35                # unemployment benefit per household
    gov_share: float = 0.35              # government purchases, share of potential wage bill
    deposit_rate: float = 0.01
    loan_rate: float = 0.03
    invest_rate: float = 0.20            # investment out of current sales
    repay_rate: float = 0.10             # loan repayment out of deposits
    mpc_min: float = 0.85                # household MPC heterogeneity
    mpc_max: float = 0.98
    ai_share_start: float = 0.0          # share of procurement delegated to AI agents
    ai_share_end: float = 0.5
    ai_switch_start: int = 4
    ai_switch_end: int = 12
    shock_period: int = 8                # employment/income shock period
    shock_employment_drop: float = 0.15  # labour demand drop in the shock period
    damping: float = 0.6                 # price tâtonnement damping (0, 1]
    tolerance: float = 1e-10
    max_iterations: int = 100
    max_loan_per_firm: float = 1e9       # credit limit, fail-closed above it
    demo_label: str = "default"

    def validate(self) -> None:
        problems = []
        if self.n_households < 1:
            problems.append("n_households must be >= 1")
        if self.n_firms < 1:
            problems.append("n_firms must be >= 1")
        if self.periods < 1:
            problems.append("periods must be >= 1")
        if self.labour_supply < 1:
            problems.append("labour_supply must be >= 1")
        if not (0.0 <= self.ai_share_start <= 1.0 and 0.0 <= self.ai_share_end <= 1.0):
            problems.append("AI shares must be within [0, 1]")
        if self.ai_switch_end < self.ai_switch_start:
            problems.append("ai_switch_end must be >= ai_switch_start")
        if not (0.0 < self.damping <= 1.0):
            problems.append("damping must be within (0, 1]")
        if self.tolerance <= 0:
            problems.append("tolerance must be positive")
        if self.max_iterations < 1:
            problems.append("max_iterations must be >= 1")
        if self.max_loan_per_firm < 0:
            problems.append("max_loan_per_firm must be non-negative")
        if self.productivity <= 0 or self.wage <= 0:
            problems.append("wage and productivity must be positive")
        if not (0.0 <= self.tax_rate <= 1.0) or not (0.0 <= self.mpc_min <= self.mpc_max <= 1.0):
            problems.append("tax_rate must be in [0, 1] and 0 <= mpc_min <= mpc_max <= 1")
        if self.input_coeff < 0 or self.markup <= 0:
            problems.append("input_coeff must be non-negative and markup positive")
        if not (0 <= self.shock_period < self.periods):
            problems.append("shock_period must be inside the simulation horizon")
        if not (0.0 <= self.shock_employment_drop <= 1.0):
            problems.append("shock_employment_drop must be within [0, 1]")
        if problems:
            raise ParameterError("; ".join(problems))


# ----------------------------------------------------------------------------------------- ledger
class Ledger:
    """Double-entry ledger: every transaction must sum to exactly zero over accounts."""

    def __init__(self) -> None:
        self.accounts: dict[str, D] = {}
        self.transactions: list[dict] = []

    # -- balances -------------------------------------------------------------------------
    def balance(self, account: str) -> D:
        return self.accounts.get(account, D(0))

    def total(self) -> D:
        return sum(self.accounts.values(), D(0))

    def transaction_count(self) -> int:
        return len(self.transactions)

    # -- postings -------------------------------------------------------------------------
    def post(self, label: str, postings: Mapping[str, D]) -> dict:
        amounts: dict[str, D] = {}
        for account, value in postings.items():
            if not isinstance(account, str) or not account:
                raise LedgerError(f"{label}: account name must be a non-empty string")
            amount = D(value)
            if not amount.is_finite():
                raise LedgerError(f"{label}: non-finite amount for {account}")
            amounts[account] = amounts.get(account, D(0)) + amount
        total = sum(amounts.values(), D(0))
        if total != 0:
            raise LedgerError(f"{label}: transaction does not balance (sum={total})")
        for account, amount in amounts.items():
            if amount != 0:
                self.accounts[account] = self.balance(account) + amount
        record = {"label": label,
                  "postings": {name: str(amount) for name, amount in sorted(amounts.items())}}
        self.transactions.append(record)
        return record

    # -- audit ----------------------------------------------------------------------------
    def digest(self) -> str:
        payload = json.dumps(self.transactions, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _split(total: D, ratios: list[D]) -> list[D]:
    """Split ``total`` into parts proportional to ``ratios`` that sum exactly to ``total``."""
    if not ratios:
        return []
    weight = sum(ratios, D(0))
    if total == 0 or weight == 0:
        return [D(0) for _ in ratios]
    parts = []
    remaining = total
    for index, ratio in enumerate(ratios):
        if index == len(ratios) - 1:
            parts.append(remaining)
            break
        part = (total * ratio / weight).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
        parts.append(part)
        remaining -= part
    return parts


def _dec(value: float | int | str | D) -> D:
    return value if isinstance(value, D) else D(str(value))


# --------------------------------------------------------------------------------------- agents
@dataclass
class _Household:
    index: int
    mpc: D
    employed: bool = False


@dataclass
class _Firm:
    index: int
    productivity: D
    price: D = D(0)
    expected_output: D = D(0)
    labour: int = 0
    output: D = D(0)


# ---------------------------------------------------------------------------------------- result
@dataclass(frozen=True)
class PeriodResult:
    period: int
    ai_share: Decimal
    employed: int
    unemployment_rate: Decimal
    consumption: Decimal
    investment: Decimal
    output_nominal: Decimal
    output_real: Decimal
    price_index: Decimal
    saving: Decimal
    net_worth: Decimal
    financial_total: Decimal
    loans_total: Decimal
    transactions: int
    price_iterations: int
    checks: dict

    def as_dict(self) -> dict:
        return {key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class SimulationResult:
    parameters: dict
    periods: list
    checks: dict
    ledger_digest: str
    transactions: int
    label: str

    def as_dict(self) -> dict:
        return {"parameters": self.parameters,
                "periods": [period.as_dict() for period in self.periods],
                "checks": self.checks,
                "ledger_digest": self.ledger_digest,
                "transactions": self.transactions,
                "label": self.label}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)


# ------------------------------------------------------------------------------------- simulator
class AgenticSimulator:
    """Deterministic period simulator over households, firms, a bank and the government."""

    def __init__(self, parameters: Parameters | None = None) -> None:
        self.params = parameters or Parameters()
        self.params.validate()
        self.ledger = Ledger()
        self._rng = random.Random(self.params.seed)
        self._build_agents()
        self._previous_price_index = D(0)

    # -- setup ----------------------------------------------------------------------------
    def _build_agents(self) -> None:
        params = self.params
        self.households = [
            _Household(index=index,
                       mpc=_dec(params.mpc_min) + _dec(params.mpc_max - params.mpc_min)
                       * D(self._rng.random()))
            for index in range(params.n_households)]
        supply_total = params.n_households * params.labour_supply
        capacity = _dec(params.productivity) * D(supply_total) / D(params.n_firms)
        self.firms = []
        for index in range(params.n_firms):
            jitter = D("0.9") + D("0.2") * D(self._rng.random())
            firm = _Firm(index=index, productivity=_dec(params.productivity) * jitter,
                         price=D("1.0"), expected_output=capacity)
            self.firms.append(firm)
        # initialise stock and flow accounts so the chart is explicit
        for household in self.households:
            for name in ("deposit", "wage_income", "transfer_income", "interest_income",
                         "consumption", "tax", "interest_paid"):
                self.ledger.accounts.setdefault(f"hh.{household.index}.{name}", D(0))
        for firm in self.firms:
            for name in ("deposit", "loan", "capital", "sales", "wage_cost",
                         "procurement_cost", "interest_paid", "tax", "investment"):
                self.ledger.accounts.setdefault(f"firm.{firm.index}.{name}", D(0))
        for name in ("deposit", "loan", "tax_income", "transfer_paid", "spending"):
            self.ledger.accounts.setdefault(f"gov.{name}", D(0))
        for name in ("deposit_liability", "loan", "gov_loan",
                     "interest_income", "interest_paid"):
            self.ledger.accounts.setdefault(f"bank.{name}", D(0))
        self.ledger.accounts.setdefault("memo.investment_goods", D(0))

    # -- account helpers ------------------------------------------------------------------
    def _financial_stocks(self) -> list[str]:
        accounts = []
        for household in self.households:
            accounts.append(f"hh.{household.index}.deposit")
        for firm in self.firms:
            accounts.extend([f"firm.{firm.index}.deposit", f"firm.{firm.index}.loan"])
        accounts.extend(["gov.deposit", "gov.loan", "bank.deposit_liability", "bank.loan",
                         "bank.gov_loan"])
        return accounts

    def _real_stocks(self) -> list[str]:
        return [f"firm.{firm.index}.capital" for firm in self.firms]

    def _flow_accounts(self) -> list[str]:
        accounts = []
        for household in self.households:
            accounts.extend([f"hh.{household.index}.wage_income",
                             f"hh.{household.index}.transfer_income",
                             f"hh.{household.index}.interest_income",
                             f"hh.{household.index}.consumption",
                             f"hh.{household.index}.tax",
                             f"hh.{household.index}.interest_paid"])
        for firm in self.firms:
            accounts.extend([f"firm.{firm.index}.sales", f"firm.{firm.index}.wage_cost",
                             f"firm.{firm.index}.procurement_cost",
                             f"firm.{firm.index}.interest_paid", f"firm.{firm.index}.tax",
                             f"firm.{firm.index}.investment"])
        accounts.extend(["gov.tax_income", "gov.transfer_paid", "gov.spending",
                         "bank.interest_income", "bank.interest_paid"])
        return accounts

    def _financial_total(self) -> D:
        return sum((self.ledger.balance(account) for account in self._financial_stocks()), D(0))

    def _capital_total(self) -> D:
        return sum((self.ledger.balance(account) for account in self._real_stocks()), D(0))

    def _flow_snapshot(self) -> dict[str, D]:
        return {account: self.ledger.balance(account) for account in self._flow_accounts()}

    def _period_flows(self, before: dict[str, D]) -> dict[str, D]:
        return {account: self.ledger.balance(account) - before.get(account, D(0))
                for account in self._flow_accounts()}

    def _period_flow(self, account: str) -> D:
        """Flow accumulated in the current period only (income flows are cumulative in the ledger)."""
        return self.ledger.balance(account) - self._flow_start.get(account, D(0))

    def _net_worth(self) -> D:
        financial = self._financial_total()
        if financial != 0:
            raise SFCError(f"financial accounts do not net to zero: {financial}")
        return self._capital_total()

    # -- banking --------------------------------------------------------------------------
    def _borrow_firm(self, firm: _Firm, amount: D) -> None:
        if amount <= 0:
            return
        loan_balance = -self.ledger.balance(f"firm.{firm.index}.loan")
        if loan_balance + amount > _dec(self.params.max_loan_per_firm):
            raise SFCError(f"credit limit exceeded for firm {firm.index}: "
                           f"{loan_balance + amount} > {self.params.max_loan_per_firm}")
        self.ledger.post(f"loan:firm:{firm.index}", {
            f"firm.{firm.index}.deposit": amount,
            f"firm.{firm.index}.loan": -amount,
            "bank.loan": amount,
            "bank.deposit_liability": -amount,
        })

    def _borrow_gov(self, amount: D) -> None:
        if amount <= 0:
            return
        self.ledger.post("loan:gov", {
            "gov.deposit": amount,
            "gov.loan": -amount,
            "bank.gov_loan": amount,
            "bank.deposit_liability": -amount,
        })

    def _ensure_deposit(self, account: str, amount: D, borrower: str | None = None) -> None:
        if amount <= 0:
            return
        shortfall = amount - self.ledger.balance(account)
        if shortfall <= 0:
            return
        if borrower == "gov":
            self._borrow_gov(shortfall)
        elif borrower is not None:
            firm = self.firms[int(borrower)]
            self._borrow_firm(firm, shortfall)
        else:
            raise SFCError(f"insufficient deposit on {account}: need {amount}")

    def _pay_interest(self) -> None:
        for firm in self.firms:
            loan = -self.ledger.balance(f"firm.{firm.index}.loan")
            interest = (loan * _dec(self.params.loan_rate)).quantize(_QUANTUM)
            if interest <= 0:
                continue
            self._ensure_deposit(f"firm.{firm.index}.deposit", interest, borrower=str(firm.index))
            self.ledger.post(f"interest:firm:{firm.index}", {
                f"firm.{firm.index}.deposit": -interest,
                f"firm.{firm.index}.interest_paid": -interest,
                "bank.interest_income": interest,
                "bank.deposit_liability": interest,
            })
        for household in self.households:
            deposit = self.ledger.balance(f"hh.{household.index}.deposit")
            interest = (deposit * _dec(self.params.deposit_rate)).quantize(_QUANTUM)
            if interest <= 0:
                continue
            self.ledger.post(f"interest:hh:{household.index}", {
                "bank.deposit_liability": -interest,
                "bank.interest_paid": -interest,
                f"hh.{household.index}.deposit": interest,
                f"hh.{household.index}.interest_income": interest,
            })

    def _repay_loans(self) -> None:
        fraction = _dec(self.params.repay_rate)
        for firm in self.firms:
            loan = -self.ledger.balance(f"firm.{firm.index}.loan")
            deposit = self.ledger.balance(f"firm.{firm.index}.deposit")
            repayment = min(loan, (loan * fraction).quantize(_QUANTUM), max(deposit, D(0)))
            if repayment <= 0:
                continue
            self.ledger.post(f"repay:firm:{firm.index}", {
                f"firm.{firm.index}.loan": repayment,
                f"firm.{firm.index}.deposit": -repayment,
                "bank.loan": -repayment,
                "bank.deposit_liability": repayment,
            })

    # -- labour and income -----------------------------------------------------------------
    def _ai_share(self, period: int) -> D:
        params = self.params
        if period < params.ai_switch_start:
            return _dec(params.ai_share_start)
        if period >= params.ai_switch_end:
            return _dec(params.ai_share_end)
        span = max(1, params.ai_switch_end - params.ai_switch_start)
        step = D(period - params.ai_switch_start) / D(span)
        return _dec(params.ai_share_start) + (_dec(params.ai_share_end)
                                              - _dec(params.ai_share_start)) * step

    def _hire(self, period: int) -> None:
        params = self.params
        supply = params.n_households * params.labour_supply
        desired = []
        for firm in self.firms:
            units = int(max(0, math.floor(firm.expected_output / firm.productivity)))
            desired.append(units)
        total_desired = sum(desired)
        if period == params.shock_period and total_desired > 0:
            factor = D(1) - _dec(params.shock_employment_drop)
            target = int((D(total_desired) * factor).to_integral_value(
                rounding=ROUND_HALF_EVEN))
            parts = _split(D(target), [D(units) for units in desired])
            desired = [int(part) for part in parts]
            total_desired = sum(desired)
        hired_total = max(0, min(total_desired, supply))
        if total_desired > 0:
            weights = [_dec(units) for units in desired]
            raw = _split(D(hired_total), weights)
            hired = [int(part) for part in raw]
        else:
            hired = [0 for _ in self.firms]
        # deterministic rotation of employed households
        starts = (period * params.n_firms) % max(1, params.n_households)
        employed_ids = [(starts + offset) % params.n_households for offset in range(hired_total)]
        cursor = 0
        for firm, units in zip(self.firms, hired):
            firm.labour = units
            firm.output = firm.productivity * D(units)
            cursor += units
        for household in self.households:
            household.employed = household.index in employed_ids
        self._employed_ids = employed_ids
        self._worker_map: dict[int, list[int]] = {}
        cursor = 0
        for firm, units in zip(self.firms, hired):
            self._worker_map[firm.index] = employed_ids[cursor:cursor + units]
            cursor += units
        self._total_employed = hired_total
        self._labour_supply = supply

    def _pay_wages(self) -> None:
        wage = _dec(self.params.wage)
        for firm in self.firms:
            workers = self._worker_map[firm.index]
            total = wage * D(len(workers))
            if total <= 0:
                continue
            self._ensure_deposit(f"firm.{firm.index}.deposit", total,
                                 borrower=str(firm.index))
            postings: dict[str, D] = {f"firm.{firm.index}.deposit": -total,
                                      f"firm.{firm.index}.wage_cost": -total}
            for worker in workers:
                postings[f"hh.{worker}.deposit"] = postings.get(f"hh.{worker}.deposit", D(0)) + wage
                postings[f"hh.{worker}.wage_income"] = \
                    postings.get(f"hh.{worker}.wage_income", D(0)) + wage
            self.ledger.post(f"wages:firm:{firm.index}", postings)

    def _pay_transfers(self) -> None:
        unemployed = max(0, self._labour_supply - self._total_employed)
        total = _dec(self.params.benefit) * D(unemployed)
        if total <= 0:
            return
        self._ensure_deposit("gov.deposit", total, borrower="gov")
        unemployed_ids = [household.index for household in self.households
                          if not household.employed]
        if not unemployed_ids:
            return
        parts = _split(total, [D(1) for _ in unemployed_ids])
        postings: dict[str, D] = {"gov.deposit": -total, "gov.transfer_paid": -total}
        for worker, part in zip(unemployed_ids, parts):
            if part == 0:
                continue
            postings[f"hh.{worker}.deposit"] = postings.get(f"hh.{worker}.deposit", D(0)) + part
            postings[f"hh.{worker}.transfer_income"] = \
                postings.get(f"hh.{worker}.transfer_income", D(0)) + part
        self.ledger.post("transfers", postings)

    def _collect_taxes(self) -> None:
        rate = _dec(self.params.tax_rate)
        total_tax = D(0)
        for household in self.households:
            base = (self._period_flow(f"hh.{household.index}.wage_income")
                    + self._period_flow(f"hh.{household.index}.transfer_income")
                    + self._period_flow(f"hh.{household.index}.interest_income"))
            tax = (max(base, D(0)) * rate).quantize(_QUANTUM)
            deposit = self.ledger.balance(f"hh.{household.index}.deposit")
            tax = min(tax, max(deposit, D(0)))
            if tax <= 0:
                continue
            self.ledger.post(f"tax:hh:{household.index}", {
                f"hh.{household.index}.deposit": -tax,
                f"hh.{household.index}.tax": -tax,
                "gov.deposit": tax,
                "gov.tax_income": tax,
            })
            total_tax += tax
        self._tax_total = total_tax

    # -- spending -------------------------------------------------------------------------
    def _consume(self) -> None:
        consumption = D(0)
        for household in self.households:
            flow = (self._period_flow(f"hh.{household.index}.wage_income")
                    + self._period_flow(f"hh.{household.index}.transfer_income")
                    + self._period_flow(f"hh.{household.index}.interest_income")
                    - self._period_flow(f"hh.{household.index}.tax"))
            desired = household.mpc * max(flow, D(0))
            deposit = self.ledger.balance(f"hh.{household.index}.deposit")
            spend = min(desired, max(deposit, D(0))).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
            if spend <= 0:
                continue
            shares = _split(spend, [max(firm.output, D(0)) or D(1) for firm in self.firms])
            postings: dict[str, D] = {f"hh.{household.index}.deposit": -spend,
                                      f"hh.{household.index}.consumption": -spend}
            for firm, share in zip(self.firms, shares):
                postings[f"firm.{firm.index}.deposit"] = \
                    postings.get(f"firm.{firm.index}.deposit", D(0)) + share
                postings[f"firm.{firm.index}.sales"] = \
                    postings.get(f"firm.{firm.index}.sales", D(0)) + share
            self.ledger.post(f"consumption:hh:{household.index}", postings)
            consumption += spend
        self._consumption_total = consumption

    def _gov_spend(self) -> None:
        params = self.params
        total = _dec(params.gov_share) * _dec(params.wage) * D(self._labour_supply)
        total = total.quantize(_QUANTUM)
        if total <= 0:
            self._gov_total = D(0)
            return
        self._ensure_deposit("gov.deposit", total, borrower="gov")
        shares = _split(total, [max(firm.output, D(0)) or D(1) for firm in self.firms])
        postings: dict[str, D] = {"gov.deposit": -total, "gov.spending": -total}
        for firm, share in zip(self.firms, shares):
            postings[f"firm.{firm.index}.deposit"] = \
                postings.get(f"firm.{firm.index}.deposit", D(0)) + share
            postings[f"firm.{firm.index}.sales"] = \
                postings.get(f"firm.{firm.index}.sales", D(0)) + share
        self.ledger.post("gov:spending", postings)
        self._gov_total = total

    def _procure(self, ai_share: D) -> None:
        params = self.params
        coefficient = _dec(params.input_coeff) * (D(1) - ai_share)
        total = D(0)
        for firm in self.firms:
            supplier = self.firms[(firm.index + 1) % len(self.firms)]
            amount = (coefficient * firm.output * supplier.price).quantize(_QUANTUM)
            if amount <= 0:
                continue
            if amount > _dec(params.max_loan_per_firm) and \
                    self.ledger.balance(f"firm.{firm.index}.deposit") < amount:
                raise SFCError(f"procurement exceeds credit limit for firm {firm.index}")
            self._ensure_deposit(f"firm.{firm.index}.deposit", amount,
                                 borrower=str(firm.index))
            self.ledger.post(f"procurement:firm:{firm.index}", {
                f"firm.{firm.index}.deposit": -amount,
                f"firm.{firm.index}.procurement_cost": -amount,
                f"firm.{supplier.index}.deposit": amount,
                f"firm.{supplier.index}.sales": amount,
            })
            total += amount
        self._procurement_total = total

    def _invest(self) -> None:
        total = D(0)
        for firm in self.firms:
            sales = max(self._period_flow(f"firm.{firm.index}.sales"), D(0))
            investment = (sales * _dec(self.params.invest_rate)).quantize(_QUANTUM)
            if investment <= 0:
                continue
            self.ledger.post(f"investment:firm:{firm.index}", {
                f"firm.{firm.index}.capital": investment,
                f"firm.{firm.index}.investment": -investment,
                f"firm.{firm.index}.sales": investment,
                "memo.investment_goods": -investment,
            })
            total += investment
        self._investment_total = total

    # -- prices ---------------------------------------------------------------------------
    def _update_prices(self, ai_share: D) -> tuple[list[D], int]:
        params = self.params
        damping = _dec(params.damping)
        tolerance = _dec(params.tolerance)
        coefficient = _dec(params.input_coeff) * (D(1) - ai_share)
        prices = [firm.price if firm.price > 0 else D(1) for firm in self.firms]
        iterations = 0
        for iteration in range(1, params.max_iterations + 1):
            new_prices = []
            for firm, price in zip(self.firms, prices):
                supplier = self.firms[(firm.index + 1) % len(self.firms)]
                procurement = coefficient * firm.output * prices[supplier.index]
                # expenditure accounts are negative by the double-entry convention
                wage_cost = -self._period_flow(f"firm.{firm.index}.wage_cost")
                interest = -self._period_flow(f"firm.{firm.index}.interest_paid")
                cost = wage_cost + procurement + interest
                if firm.output > 0:
                    target = _dec(params.markup) * cost / firm.output
                else:
                    target = price
                candidate = price + damping * (target - price)
                if candidate <= 0:
                    raise ConvergenceError(f"non-positive price for firm {firm.index}: {candidate}")
                new_prices.append(candidate)
            diff = max((abs(new - old) for new, old in zip(new_prices, prices)), default=D(0))
            prices = new_prices
            iterations = iteration
            if diff <= tolerance:
                break
        else:
            raise ConvergenceError(
                f"price iteration did not converge in {params.max_iterations} iterations "
                f"(last diff {diff})")
        for firm, price in zip(self.firms, prices):
            firm.price = price
        weights = [max(firm.output, D(0)) for firm in self.firms]
        total_weight = sum(weights, D(0))
        if total_weight > 0:
            self._previous_price_index = sum((price * weight for price, weight
                                              in zip(prices, weights)), D(0)) / total_weight
        else:
            self._previous_price_index = sum(prices, D(0)) / D(len(prices))
        return prices, iterations

    # -- checks ---------------------------------------------------------------------------
    def _sector_saving(self, flows: dict[str, D]) -> dict[str, D]:
        """Sector saving = income accounts (positive) plus expenditure accounts (negative)."""
        saving: dict[str, D] = {}
        household_total = D(0)
        for household in self.households:
            prefix = f"hh.{household.index}."
            household_total += (flows[prefix + "wage_income"]
                                + flows[prefix + "transfer_income"]
                                + flows[prefix + "interest_income"]
                                + flows[prefix + "consumption"]
                                + flows[prefix + "tax"]
                                + flows[prefix + "interest_paid"])
        saving["households"] = household_total
        firm_total = D(0)
        for firm in self.firms:
            prefix = f"firm.{firm.index}."
            firm_total += (flows[prefix + "sales"] + flows[prefix + "wage_cost"]
                           + flows[prefix + "procurement_cost"]
                           + flows[prefix + "interest_paid"] + flows[prefix + "tax"])
        saving["firms"] = firm_total
        saving["government"] = (flows["gov.tax_income"] + flows["gov.transfer_paid"]
                                + flows["gov.spending"])
        saving["bank"] = flows["bank.interest_income"] + flows["bank.interest_paid"]
        return saving

    def _check_period(self, previous_capital: D, flows: dict[str, D],
                      iterations: int) -> dict:
        financial_total = self._financial_total()
        if financial_total != 0:
            raise SFCError(f"financial conservation violated: {financial_total}")
        bank_net_worth = (self.ledger.balance("bank.loan")
                          + self.ledger.balance("bank.gov_loan")
                          + self.ledger.balance("bank.deposit_liability"))
        bank_profit = (self.ledger.balance("bank.interest_income")
                       + self.ledger.balance("bank.interest_paid"))
        if bank_net_worth != bank_profit:
            raise SFCError(f"bank identity violated: net_worth={bank_net_worth}, "
                           f"profit={bank_profit}")
        capital = self._capital_total()
        investment = capital - previous_capital
        saving = sum(self._sector_saving(flows).values(), D(0))
        if saving != investment + CA:
            raise SFCError(f"SFC identity violated: saving={saving}, investment={investment}, "
                           f"CA={CA}")
        if self._financial_total() != 0:
            raise SFCError("financial accounts do not net to zero after the period")
        return {"financial_total": financial_total,
                "saving": saving, "investment": investment,
                "current_account": CA,
                "bank_net_worth": bank_net_worth, "capital": capital,
                "price_iterations": iterations}

    # -- run ------------------------------------------------------------------------------
    def run(self) -> SimulationResult:
        params = self.params
        periods: list[PeriodResult] = []
        previous_capital = self._capital_total()
        checks: dict = {}
        for period in range(params.periods):
            flow_before = self._flow_snapshot()
            self._flow_start = flow_before
            ai_share = self._ai_share(period)
            self._pay_interest()
            self._repay_loans()
            self._hire(period)
            self._pay_wages()
            self._pay_transfers()
            self._collect_taxes()
            self._consume()
            self._gov_spend()
            self._procure(ai_share)
            self._invest()
            prices, iterations = self._update_prices(ai_share)
            flows = self._period_flows(flow_before)
            for firm in self.firms:
                market_sales = (flows[f"firm.{firm.index}.sales"]
                                + flows[f"firm.{firm.index}.investment"])
                firm.expected_output = max(D(0), market_sales / firm.price)
            period_checks = self._check_period(previous_capital, flows, iterations)
            previous_capital = self._capital_total()
            employed = self._total_employed
            unemployment = (D(self._labour_supply - employed) / D(self._labour_supply)
                            if self._labour_supply else D(0))
            output_nominal = sum(flows[f"firm.{firm.index}.sales"] for firm in self.firms)
            output_real = sum((firm.output for firm in self.firms), D(0))
            net_worth = self._net_worth()
            result = PeriodResult(
                period=period,
                ai_share=ai_share,
                employed=employed,
                unemployment_rate=unemployment,
                consumption=self._consumption_total,
                investment=period_checks["investment"],
                output_nominal=output_nominal,
                output_real=output_real,
                price_index=self._previous_price_index,
                saving=period_checks["saving"],
                net_worth=net_worth,
                financial_total=period_checks["financial_total"],
                loans_total=sum((-self.ledger.balance(f"firm.{firm.index}.loan")
                                 for firm in self.firms), D(0)),
                transactions=self.ledger.transaction_count(),
                price_iterations=iterations,
                checks={"financial_total": str(period_checks["financial_total"]),
                        "saving": str(period_checks["saving"]),
                        "investment": str(period_checks["investment"]),
                        "current_account": str(period_checks["current_account"]),
                        "bank_net_worth": str(period_checks["bank_net_worth"]),
                        "price_iterations": iterations,
                        "ok": True},
            )
            periods.append(result)
        total_saving = sum((period.saving for period in periods), D(0))
        total_investment = sum((period.investment for period in periods), D(0))
        checks = {
            "double_entry": True,
            "financial_conservation": all(period.financial_total == 0 for period in periods),
            "sfc_saving_equals_investment": total_saving == total_investment + CA * len(periods),
            "closed_economy_no_external_accounts": not any(
                account.startswith("ext.") for account in self.ledger.accounts),
            "convergence": all(period.price_iterations <= params.max_iterations
                               for period in periods),
            "periods": len(periods),
            "transactions": self.ledger.transaction_count(),
        }
        if not all(checks[key] for key in ("double_entry", "financial_conservation",
                                           "sfc_saving_equals_investment",
                                           "closed_economy_no_external_accounts",
                                           "convergence")):
            raise SFCError(f"simulation checks failed: {checks}")
        return SimulationResult(parameters={item.name: getattr(params, item.name)
                                            for item in fields(Parameters)},
                                periods=periods,
                                checks=checks,
                                ledger_digest=self.ledger.digest(),
                                transactions=self.ledger.transaction_count(),
                                label=params.demo_label)


# ------------------------------------------------------------------------------------------ demo
def run_demo(parameters: Parameters | None = None) -> dict:
    """Run the documented demo: baseline, income shock, and income shock + AI procurement 0 -> 50%.

    Returns a dict with three :class:`SimulationResult` values. All runs satisfy the accounting
    checks; comparisons are descriptive and are not forecasts.
    """
    base = parameters or Parameters()
    runs = {
        "baseline": Parameters(**{**asdict(base), "demo_label": "baseline",
                                  "ai_share_start": 0.0, "ai_share_end": 0.0,
                                  "shock_employment_drop": 0.0}),
        "income_shock": Parameters(**{**asdict(base), "demo_label": "income_shock",
                                      "ai_share_start": 0.0, "ai_share_end": 0.0}),
        "income_shock_ai": Parameters(**{**asdict(base), "demo_label": "income_shock_ai"}),
    }
    return {name: AgenticSimulator(params).run() for name, params in runs.items()}


def _format_periods(result: SimulationResult) -> list[str]:
    lines = ["period | AI share | employed | unemp. | consumption | investment | "
             "output | price idx | saving | iterations"]
    for period in result.periods:
        lines.append(
            f"{period.period:>6} | {float(period.ai_share):>8.3f} | {period.employed:>8} | "
            f"{float(period.unemployment_rate):>6.3f} | {float(period.consumption):>11.3f} | "
            f"{float(period.investment):>10.3f} | {float(period.output_nominal):>8.3f} | "
            f"{float(period.price_index):>9.4f} | {float(period.saving):>6.3f} | "
            f"{period.price_iterations:>10}")
    return lines


def format_demo(demo: dict) -> str:
    """Plain-text report of a demo dictionary (deterministic, no wall clock)."""
    lines = ["AMFE agentic simulator: demo (research toy, not a forecast)", ""]
    for name, result in demo.items():
        lines.append(f"== {name} ({result.label}) ==")
        lines.extend(_format_periods(result))
        checks = result.checks
        lines.append(f"checks: double_entry={checks['double_entry']}, "
                     f"financial_conservation={checks['financial_conservation']}, "
                     f"sfc_saving_equals_investment={checks['sfc_saving_equals_investment']}, "
                     f"closed_economy={checks['closed_economy_no_external_accounts']}, "
                     f"convergence={checks['convergence']}; "
                     f"transactions={checks['transactions']}; "
                     f"ledger_sha256={result.ledger_digest[:16]}…")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    demo = run_demo()
    sys.stdout.write(format_demo(demo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
