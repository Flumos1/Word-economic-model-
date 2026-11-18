"""Monetary/fiscal policy block (docs/177 blocks 11/12; docs/174 UC-04/11/38/39).

The block is a **research-only** integration layer.  It does not estimate anything and it reads no
data: it takes the already written pieces (the Taylor rule of :mod:`amfe.dynamics.policy`, the debt
identity of :mod:`amfe.dynamics.debt`, the SFC convention ``S = I + CA`` of the P2 0.2 release) and
puts them into one deterministic scenario engine that can be explained line by line.

What is classified how
-----------------------
* **IDENTITY** - ``D = r*b - pb`` / ``b' = (1+r)/(1+g)*b - pb``; the central-bank balance sheet
  ``assets = liabilities + equity``; the SFC aggregate ``S = I + CA``.  These hold by construction
  and are checked after every scenario, not estimated.
* **HYPOTHESIS** - the Taylor reaction with inertia.  ``phi_pi > 1`` is the Taylor principle, a
  *determinacy* condition for a linearised model, not evidence about a real central bank.
* **ASSUMPTION_BASED** - the transmission elasticities (real rate -> output gap, output gap ->
  inflation, deficit -> demand), the T3 recession-probability mapping and every scenario size.
  They are declared in :data:`ASSUMPTIONS` and are not estimates.
* **SCENARIO** - "+100 bp", "+2% of GDP deficit", "tariff/inflation shock".  A scenario is not a
  forecast, never gates admission and never claims causality.

Key equations (period ``t``, annual, percent for rates, GDP shares for fiscal ratios)
------------------------------------------------------------------------------------
Taylor rule with inertia (``i`` = policy rate, ``pi`` = inflation, ``y`` = output gap)::

    i_rule = r* + pi + phi_pi*(pi - pi_target) + phi_y*y
    i_t    = clip(rho*i_{t-1} + (1-rho)*(i_rule + shock_t), lower, upper)

Transmission (deliberately simple, one lag, all coefficients assumed)::

    r_t   = (i_t - pi_t)/100                                  real rate, decimal
    y_t   = y*_t + m_f*deficit_shock_t + gap_effect_t
            - beta*(r_t - r*_t)*100                            gap, percentage points
    pi_t  = pi*_t + inflation_shock_t + gamma*(y_{t-1} - y*_{t-1})

Fiscal block (``b`` = gross debt/GDP, ``pb`` = primary balance/GDP, ``r``, ``g`` decimals)::

    interest_t = r_t*b_t
    D_t        = r_t*b_t - pb_t                               overall deficit/GDP
    b_{t+1}    = (1+r_t)/(1+g_t)*b_t - pb_t                   r-g identity
    snowball_t = (r_t - g_t)/(1+g_t)*b_t
    pb*_t      = (r_t - g_t)/(1+g_t)*b_t                      stabilising primary balance
    g_t        = potential_growth + (y_t - y_{t-1})/100

Central bank (aggregated balance sheet, domestic currency)::

    assets      = bonds + fx_reserves + loans
    liabilities = currency + reserves
    equity      = assets - liabilities                        identity, checked
    QE: bonds += X, reserves += X                             identity preserved

SFC link (P2 0.2, docs/146 section 4)::

    S = I + CA;   S = S_private + S_government;   S_government = -D
    residual = S_private + S_government - I - CA = 0

T1/T3 links: a T1 CPI path replaces ``pi*_t`` and a T3 recession probability replaces the baseline
output gap through :func:`output_gap_from_recession_probability`.  Both are adapters with declared
assumptions, not an evaluation of T1/T3.

Determinism: pure arithmetic on the declared inputs, fixed iteration order, no wall clock, no
randomness, no file/network access.  Equal inputs produce byte-identical JSON after canonical
serialisation (checked by the CLI digest and the tests).

Status: ``RESEARCH_ONLY``, ``admission=false``, ``gating=false``, ``causal_claim=false``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, replace
from typing import Iterable, Mapping, Sequence

from amfe.dynamics.debt import debt_step as _debt_step
from amfe.dynamics.debt import stabilizing_primary_balance as _stabilizing_pb

# ---------------------------------------------------------------------------------------- constants
SCHEMA = "amfe.world.policy.v1"
SCENARIO_SCHEMA = "amfe.world.policy.scenario.v1"
INDEX_SCHEMA = "amfe.world.policy.index.v1"
TOOL_VERSION = "1.0.0"
STATUS = "RESEARCH_ONLY"
ADMISSION = False
GATING = False
CAUSAL_CLAIM = False
STATUS_NOTE = ("Исследовательский блок политики: правило ставки и фискальная динамика — сценарий, "
               "не прогноз, не допуск и не причинная оценка; admission=false, gating=false")
DEFAULT_PERIODS = 12
MAX_PERIODS = 120
TOLERANCE = 1e-9
DEFAULT_QE_RATIO = 0.05
SCENARIO_ID = re.compile(r"^[A-Z][A-Z0-9-]{2,60}$")
SCENARIO_KINDS = ("rate_shock", "deficit_shock", "inflation_shock")

#: The frozen evaluation protocol is referenced, not read at run time.  A test re-reads the file and
#: checks that the version/status below still match; the block never modifies or reinterprets it.
PROTOCOL_REFERENCE = {
    "file": "protocols/evaluation_protocol_v5.json",
    "protocol_id": "AMFE-EVAL",
    "version": "5.0.1",
    "status": "FROZEN",
    "admission": False,
    "note": ("Блок политики не является кандидатом прогнозного трека и не меняет admission; "
             "протокол v5 только цитируется по версии и статусу."),
}

#: Explicit assumptions.  Every number in a scenario report must be traceable to one of these.
ASSUMPTIONS = (
    {"id": "A1", "kind": "HYPOTHESIS",
     "statement": ("Правило Тейлора с инерцией — гипотеза о реакции, а не институциональный факт; "
                   "коэффициенты r*=0,5, phi_pi=1,5, phi_y=0,5, rho=0,8 не оценены."),
     "replace_with": "оценка реакции на данных (Т1/T3) после допуска соответствующей модели"},
    {"id": "A2", "kind": "ASSUMPTION_BASED",
     "statement": ("Передача реальной ставки в разрыв выпуска: beta=0,5 п.п. разрыва на 1 п.п. "
                   "реальной ставки, одновременно, без лагов и неопределённости."),
     "replace_with": "оценённая кривая IS / эмпирические импульсные отклики"},
    {"id": "A3", "kind": "ASSUMPTION_BASED",
     "statement": ("Кривая Филлипса: pi_t = pi*_t + gamma*(y_{t-1}-y*_{t-1}), gamma=0,25; "
                   "инфляционные шоки exogenous и затухают с заданным decay."),
     "replace_with": "оценённая кривая Филлипса и PIT-данные T1"},
    {"id": "A4", "kind": "ASSUMPTION_BASED",
     "statement": ("Фискальный импульс: +1 п.п. ВВП дефицита даёт +0,5 п.п. разрыва выпуска "
                   "(мультипликатор 0,5), без вытеснения и без реакции предложения."),
     "replace_with": "оценённые фискальные мультипликаторы по странам/режимам"},
    {"id": "A5", "kind": "IDENTITY",
     "statement": ("Долг: b'=(1+r)/(1+g)*b-pb; D=r*b-pb; pb*=(r-g)/(1+g)*b. Нет переоценки, "
                   "разовых операций, валютной структуры долга и обратной связи долга со ставкой."),
     "replace_with": "долг в рыночной стоимости, валютная структура, кредитный спред (UC-33)"},
    {"id": "A6", "kind": "IDENTITY",
     "statement": ("SFC: S=I+CA и S=S_private+S_government, S_government=-D; адресность конечного "
                   "спроса и разделение C/I не моделируются (допущение P2 0.2, docs/146 §4)."),
     "replace_with": "полный SFC-контур с адресностью конечного спроса"},
    {"id": "A7", "kind": "ASSUMPTION_BASED",
     "statement": ("T3: gap = -scale*max(0,(p-threshold)/(1-threshold)), scale=2,0 п.п., "
                   "threshold=0,35; линейная связь вероятности рецессии с разрывом — допущение."),
     "replace_with": "условная проекция T3 на разрыв выпуска, оценённая на данных"},
    {"id": "A8", "kind": "ASSUMPTION_BASED",
     "statement": ("Баланс ЦБ агрегирован (облигации, FX, кредиты; наличные, резервы, капитал); "
                   "QE меняет состав и размер баланса, но не передаётся в цены и доходности."),
     "replace_with": "портфельный канал QE, доходности и премии за срок"},
    {"id": "A9", "kind": "ASSUMPTION_BASED",
     "statement": ("Период — год; ставки, инфляция и разрыв — п.п.; долг, дефицит, рост и "
                   "сбережения — доли ВВП; нет сезонности, стохастики и ожиданий as-a-model."),
     "replace_with": "квартальная/месячная частота, сезонность, модельные ожидания"},
    {"id": "A10", "kind": "ASSUMPTION_BASED",
     "statement": ("Блок не читает данные: базовые пути и размеры шоков задаются явно; сценарий "
                   "не является прогнозом и не оценивает вероятность событий."),
     "replace_with": "калибровка на ставках/долге/CPI (блок 14 и внешние источники)"},
)

LIMITATIONS = (
    "все коэффициенты реакции и передачи — допущения (A1–A4), а не оценки; числа сценария "
    "показывают направление и порядок при данных допущениях, а не ожидаемый эффект политики;",
    "нет данных: базовый путь строится из объявленных констант, ставки/долг/CPI не калибруются "
    "(в проекте нет баланса ФРС/ЕЦБ, долга и фискальных рядов);",
    "фискальный блок — тождество r−g при экзогенных r, g, pb; нет обратной связи долга со ставкой, "
    "кредитного спреда (UC-33), валютной структуры и переоценки;",
    "баланс ЦБ агрегирован, QE не передаётся в доходности, цены активов и инфляцию; ликвидность "
    "измеряется только агрегатами (резервы + наличные), без рынка резервов;",
    "нет текстовой коммуникации ЦБ и модельных ожиданий (UC-38 остаётся PARTIAL): инерция правила — "
    "не то же самое, что информационный канал;",
    "SFC-связь — бухгалтерская (S=I+CA, docs/146 §4), а не поведенческая; адресность конечного "
    "спроса и разделение потребления/инвестиций не моделируются;",
    "сценарий не является причинной оценкой и не гейтит допуск: admission=false, gating=false, "
    "causal_claim=false.",
)


class PolicyError(ValueError):
    """Invalid input or a broken identity: fail closed (CLI exit 1, nothing written)."""


# ----------------------------------------------------------------------------------------- helpers
def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{name}: ожидается число, получено {value!r}") from exc
    if not math.isfinite(number):
        raise PolicyError(f"{name}: число должно быть конечным, получено {value!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise PolicyError(f"{name}: должно быть > 0, получено {number!r}")
    return number


def _ratio(value: object, name: str, *, low: float | None = 0.0,
           high: float | None = 1.0) -> float:
    number = _finite(value, name)
    if low is not None and number < low:
        raise PolicyError(f"{name}: должно быть >= {low}, получено {number!r}")
    if high is not None and number > high:
        raise PolicyError(f"{name}: должно быть <= {high}, получено {number!r}")
    return number


def _periods(value: object, name: str = "periods") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise PolicyError(f"{name}: ожидается целое число, получено {value!r}")
    if not 1 <= int(value) <= MAX_PERIODS:
        raise PolicyError(f"{name}: должно лежать в [1, {MAX_PERIODS}], получено {value!r}")
    return int(value)


def _series(values: object, name: str, periods: int) -> tuple[float, ...]:
    """Validate a sequence and make it exactly ``periods`` long (last value persists)."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise PolicyError(f"{name}: ожидается последовательность чисел")
    items = tuple(_finite(value, f"{name}[{index}]") for index, value in enumerate(values))
    if not items:
        raise PolicyError(f"{name}: последовательность не должна быть пустой")
    if len(items) >= periods:
        return items[:periods]
    return items + (items[-1],) * (periods - len(items))


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: object) -> str:
    """Stable SHA-256 of a canonical JSON payload (the payload must not contain 'digest')."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assumptions() -> list[dict]:
    return [dict(item) for item in ASSUMPTIONS]


def limitations() -> list[str]:
    return list(LIMITATIONS)


# ----------------------------------------------------------------------------------- Taylor rule
@dataclass(frozen=True)
class TaylorRuleWithInertia:
    """Taylor rule with interest-rate smoothing (HYPOTHESIS, not an institutional fact).

    ``i_t = clip(rho*i_{t-1} + (1-rho)*i_rule_t, lower_bound, upper_bound)`` with
    ``i_rule_t = r* + pi_t + phi_pi*(pi_t - pi_target) + phi_y*y_t``.  With ``pi = pi_target`` and
    ``y = 0`` the fixed point is the neutral rate ``r* + pi_target``; the rule is "active" when
    ``phi_pi > 1`` (Taylor principle, a determinacy condition under stated assumptions).
    """

    r_star: float = 0.5
    pi_target: float = 2.0
    phi_pi: float = 1.5
    phi_y: float = 0.5
    inertia: float = 0.8
    lower_bound: float = 0.0
    upper_bound: float = 20.0

    def __post_init__(self) -> None:
        r_star = _finite(self.r_star, "r_star")
        pi_target = _finite(self.pi_target, "pi_target")
        phi_pi = _finite(self.phi_pi, "phi_pi")
        phi_y = _finite(self.phi_y, "phi_y")
        inertia = _finite(self.inertia, "inertia")
        lower = _finite(self.lower_bound, "lower_bound")
        upper = _finite(self.upper_bound, "upper_bound")
        if pi_target <= 0.0:
            raise PolicyError("pi_target: цель должна быть > 0")
        if phi_pi <= 0.0:
            raise PolicyError("phi_pi: коэффициент при инфляции должен быть > 0")
        if phi_y < 0.0:
            raise PolicyError("phi_y: коэффициент при разрыве не может быть отрицательным")
        if not 0.0 <= inertia < 1.0:
            raise PolicyError("inertia: должна лежать в [0, 1)")
        if lower >= upper:
            raise PolicyError("lower_bound: должен быть меньше upper_bound")
        if not lower <= self.neutral_rate <= upper:
            raise PolicyError("нейтральная ставка должна лежать внутри границ правила")
        object.__setattr__(self, "r_star", r_star)
        object.__setattr__(self, "pi_target", pi_target)
        object.__setattr__(self, "phi_pi", phi_pi)
        object.__setattr__(self, "phi_y", phi_y)
        object.__setattr__(self, "inertia", inertia)
        object.__setattr__(self, "lower_bound", lower)
        object.__setattr__(self, "upper_bound", upper)

    @property
    def neutral_rate(self) -> float:
        return self.r_star + self.pi_target

    @property
    def taylor_principle(self) -> bool:
        return self.phi_pi > 1.0

    def rule_rate(self, inflation: float, output_gap: float = 0.0) -> float:
        inflation = _finite(inflation, "inflation")
        output_gap = _finite(output_gap, "output_gap")
        return (self.r_star + inflation
                + self.phi_pi * (inflation - self.pi_target)
                + self.phi_y * output_gap)

    def step(self, previous_rate: float, inflation: float, output_gap: float = 0.0) -> float:
        previous_rate = _finite(previous_rate, "previous_rate")
        target = self.rule_rate(inflation, output_gap)
        smoothed = self.inertia * previous_rate + (1.0 - self.inertia) * target
        return min(self.upper_bound, max(self.lower_bound, smoothed))

    def path(self, inflation_path: Sequence[float], gap_path: Sequence[float],
             initial_rate: float | None = None) -> tuple[float, ...]:
        inflation = tuple(_finite(value, "inflation_path") for value in inflation_path)
        gap = tuple(_finite(value, "gap_path") for value in gap_path)
        if len(inflation) != len(gap):
            raise PolicyError("inflation_path и gap_path должны быть одной длины")
        if not inflation:
            raise PolicyError("пути не должны быть пустыми")
        rate = self.neutral_rate if initial_rate is None else _finite(initial_rate, "initial_rate")
        result = []
        for index in range(len(inflation)):
            rate = self.step(rate, inflation[index], gap[index])
            result.append(rate)
        return tuple(result)

    def to_dict(self) -> dict:
        return {
            "kind": "HYPOTHESIS",
            "r_star": self.r_star,
            "pi_target": self.pi_target,
            "phi_pi": self.phi_pi,
            "phi_y": self.phi_y,
            "inertia": self.inertia,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "neutral_rate": self.neutral_rate,
            "taylor_principle": self.taylor_principle,
            "formula": ("i_t = rho*i_{t-1} + (1-rho)*(r* + pi + phi_pi*(pi-pi*) + phi_y*y); "
                        "фиксированная точка при pi=pi*, y=0 — r*+pi*"),
        }


# ---------------------------------------------------------------------------------- central bank
@dataclass(frozen=True)
class CentralBankBalance:
    """Aggregated central-bank balance sheet; ``assets = liabilities + equity`` is an identity."""

    bonds: float = 30.0
    fx_reserves: float = 5.0
    loans: float = 2.0
    currency: float = 10.0
    reserves: float = 27.0
    equity: float | None = None

    def __post_init__(self) -> None:
        for name in ("bonds", "fx_reserves", "loans", "currency", "reserves"):
            value = _finite(getattr(self, name), name)
            if value < 0.0:
                raise PolicyError(f"{name}: агрегат баланса не может быть отрицательным")
            object.__setattr__(self, name, value)
        assets = self.assets
        liabilities = self.liabilities
        equity = assets - liabilities if self.equity is None else _finite(self.equity, "equity")
        limit = 1e-9 * max(1.0, abs(assets))
        if self.equity is not None and abs(assets - liabilities - equity) > limit:
            raise PolicyError(
                "баланс ЦБ не сходится: assets - liabilities != equity "
                f"({assets} - {liabilities} != {equity})")
        object.__setattr__(self, "equity", equity)

    @property
    def assets(self) -> float:
        return self.bonds + self.fx_reserves + self.loans

    @property
    def liabilities(self) -> float:
        return self.currency + self.reserves

    @property
    def liquidity(self) -> float:
        """Cash in circulation plus bank reserves: the monetary base."""
        return self.currency + self.reserves

    def check(self, tolerance: float = 1e-9) -> dict:
        residual = self.assets - self.liabilities - float(self.equity)
        limit = float(tolerance) * max(1.0, abs(self.assets))
        return {
            "assets": self.assets,
            "liabilities": self.liabilities,
            "equity": float(self.equity),
            "residual": residual,
            "tolerance": limit,
            "ok": abs(residual) <= limit,
            "identity": "assets = currency + reserves + equity",
        }

    def buy_bonds(self, amount: float) -> "CentralBankBalance":
        """QE-style asset swap: bonds and reserves increase by the same amount."""
        amount = _positive(amount, "amount")
        return replace(self, bonds=self.bonds + amount, reserves=self.reserves + amount)

    def sell_bonds(self, amount: float) -> "CentralBankBalance":
        amount = _positive(amount, "amount")
        if amount > self.bonds + TOLERANCE:
            raise PolicyError("sell_bonds: нельзя продать больше облигаций на балансе")
        if amount > self.reserves + TOLERANCE:
            raise PolicyError("sell_bonds: резервов недостаточно для погашения")
        return replace(self, bonds=self.bonds - amount, reserves=self.reserves - amount)

    def net_interest_income(self, policy_rate_pp: float) -> float:
        """Interest on bonds minus interest on reserves, in balance-sheet units."""
        rate = _finite(policy_rate_pp, "policy_rate_pp") / 100.0
        return rate * (self.bonds - self.reserves)

    def to_dict(self, nominal_gdp: float | None = None) -> dict:
        payload = {
            "bonds": self.bonds,
            "fx_reserves": self.fx_reserves,
            "loans": self.loans,
            "currency": self.currency,
            "reserves": self.reserves,
            "equity": float(self.equity),
            "assets": self.assets,
            "liabilities": self.liabilities,
            "liquidity": self.liquidity,
            "balance_sheet_size": self.assets,
        }
        if nominal_gdp is not None:
            gdp = _positive(nominal_gdp, "nominal_gdp")
            for name in ("bonds", "fx_reserves", "loans", "currency", "reserves",
                         "assets", "liabilities", "liquidity", "balance_sheet_size"):
                payload[f"{name}_to_gdp"] = payload[name] / gdp
        return payload


# ----------------------------------------------------------------------------------- debt block
def _debt_arrays(debt_ratio: float, real_rates: Sequence[float], growth_rates: Sequence[float],
                 primary_balances: Sequence[float]) -> dict:
    """Roll the r-g identity and its deficit decomposition; return paths and residuals."""
    if not (len(real_rates) == len(growth_rates) == len(primary_balances)):
        raise PolicyError("debt arrays: r, g и pb должны быть одной длины")
    debt = [float(debt_ratio)]
    interest_path: list[float] = []
    overall_path: list[float] = []
    deficit_path: list[float] = []
    snowball_path: list[float] = []
    debt_residual = 0.0
    deficit_residual = 0.0
    for index, (real_rate, growth, primary) in enumerate(
            zip(real_rates, growth_rates, primary_balances)):
        if growth <= -1.0:
            raise PolicyError(f"growth[{index}]: должно быть > -1")
        current = debt[-1]
        interest = real_rate * current
        overall = primary - interest
        deficit = -overall
        snowball = (real_rate - growth) / (1.0 + growth) * current
        next_debt = _debt_step(current, real_rate, growth, primary)
        # Identity checks: the recurrence, the deficit decomposition and the snowball term.
        debt_residual = max(debt_residual, abs(next_debt - current - (snowball - primary)))
        deficit_residual = max(deficit_residual,
                              abs(deficit - (interest - primary)),
                              abs((next_debt - current) - (snowball - primary)))
        debt.append(next_debt)
        interest_path.append(interest)
        overall_path.append(overall)
        deficit_path.append(deficit)
        snowball_path.append(snowball)
    return {
        "debt_path": debt,
        "interest_path": interest_path,
        "overall_balance_path": overall_path,
        "deficit_path": deficit_path,
        "snowball_path": snowball_path,
        "max_debt_identity_residual": debt_residual,
        "max_deficit_identity_residual": deficit_residual,
    }


def project_debt(debt_ratio: float = 0.55, real_rate: float = 0.005, growth: float = 0.015,
                 primary_balance: float = 0.0, periods: int = DEFAULT_PERIODS,
                 deficit_shock: float = 0.0, debt_limit: float | None = None) -> dict:
    """Deterministic r-g debt projection with a constant real rate, growth and primary balance.

    All arguments are decimal ratios of GDP (``0.02`` = 2% of GDP); rates are real decimals.  The
    function is a thin wrapper around :func:`_debt_arrays` and the project's debt identity, kept
    here so that the fiscal block can be read and tested without the scenario engine.
    """
    debt_ratio = _finite(debt_ratio, "debt_ratio")
    real_rate = _finite(real_rate, "real_rate")
    growth = _finite(growth, "growth")
    primary_balance = _finite(primary_balance, "primary_balance")
    deficit_shock = _finite(deficit_shock, "deficit_shock")
    periods = _periods(periods)
    if growth <= -1.0:
        raise PolicyError("growth: должно быть > -1")
    if debt_ratio < 0.0:
        raise PolicyError("debt_ratio: валовой долг не может быть отрицательным")
    if deficit_shock < -1.0 or deficit_shock > 1.0:
        raise PolicyError("deficit_shock: должен лежать в [-1, 1]")
    pb = primary_balance - deficit_shock
    arrays = _debt_arrays(debt_ratio, [real_rate] * periods, [growth] * periods, [pb] * periods)
    path = arrays["debt_path"]
    final = path[-1]
    drift = final - path[0]
    if abs(drift) <= 1e-12:
        trend = "STABLE"
    elif drift < 0.0:
        trend = "DECLINING"
    else:
        trend = "GROWING"
    required = _stabilizing_pb(debt_ratio, real_rate, growth)
    breach = None if debt_limit is None else any(value > float(debt_limit) for value in path)
    return {
        "initial_debt_ratio": debt_ratio,
        "final_debt_ratio": final,
        "debt_change": drift,
        "trend": trend,
        "real_rate": real_rate,
        "growth": growth,
        "primary_balance": pb,
        "deficit_shock": deficit_shock,
        "r_minus_g": real_rate - growth,
        "stabilizing_primary_balance": required,
        "balance_gap": pb - required,
        "debt_limit": debt_limit,
        "debt_limit_breached": breach,
        "debt_path": path,
        "deficit_path": arrays["deficit_path"],
        "interest_path": arrays["interest_path"],
        "max_debt_identity_residual": arrays["max_debt_identity_residual"],
        "max_deficit_identity_residual": arrays["max_deficit_identity_residual"],
        "note": ("тождество r-g при экзогенных r, g, pb; D = r*b - pb; "
                 "pb* = (r-g)/(1+g)*b — стабилизирующий первичный баланс"),
    }


# ------------------------------------------------------------------------------------ SFC / T1-T3
def check_sfc_aggregates(savings: float, investment: float, current_account: float,
                         tolerance: float = 1e-6) -> dict:
    """Check the P2 accounting identity ``S = I + CA`` (all as GDP shares)."""
    savings = _finite(savings, "savings")
    investment = _finite(investment, "investment")
    current_account = _finite(current_account, "current_account")
    tolerance = _positive(tolerance, "tolerance")
    residual = savings - investment - current_account
    return {
        "savings": savings,
        "investment": investment,
        "current_account": current_account,
        "residual": residual,
        "tolerance": tolerance,
        "ok": abs(residual) <= tolerance,
        "identity": "S = I + CA",
        "source": "docs/146 §4; build/world_oecd_2025/.../sfc.assumptions",
    }


def inflation_path_from_t1(forecasts: Mapping, periods: int = DEFAULT_PERIODS) -> tuple[float, ...]:
    """Turn a T1 CPI forecast (h1..hN, percent y/y) into a scenario inflation path.

    Accepted keys are ``"h1".."hN"`` or integers ``1..N``.  Missing horizons persist the last
    available value; extra horizons are truncated.  This is a mapping adapter, not a T1 evaluation:
    the resulting path is used as ``pi*_t`` only when the caller explicitly asks for it.
    """
    periods = _periods(periods)
    if not isinstance(forecasts, Mapping) or not forecasts:
        raise PolicyError("t1_forecasts: нужен непустой отображение горизонт -> значение")
    horizon_values: dict[int, float] = {}
    for key, value in forecasts.items():
        if isinstance(key, str) and key.lower().startswith("h") and key[1:].isdigit():
            horizon = int(key[1:])
        elif isinstance(key, int) and not isinstance(key, bool):
            horizon = int(key)
        else:
            raise PolicyError(f"t1_forecasts: неизвестный горизонт {key!r} (ожидается h1..hN)")
        if horizon < 1:
            raise PolicyError("t1_forecasts: горизонты начинаются с 1")
        number = _finite(value, f"t1_forecasts[{key!r}]")
        if not -20.0 <= number <= 50.0:
            raise PolicyError(f"t1_forecasts[{key!r}]: значение вне диапазона [-20, 50] п.п.")
        if horizon in horizon_values:
            raise PolicyError(f"t1_forecasts: горизонт h{horizon} задан дважды")
        horizon_values[horizon] = number
    expected = list(range(1, max(horizon_values) + 1))
    if sorted(horizon_values) != expected:
        raise PolicyError("t1_forecasts: горизонты должны быть непрерывными начиная с h1")
    ordered = [horizon_values[key] for key in expected]
    return _series(ordered, "t1_forecasts", periods)


def output_gap_from_recession_probability(probability: float, scale: float = 2.0,
                                          threshold: float = 0.35) -> float:
    """Map a T3 recession probability to an output gap (ASSUMPTION A7, not an estimate)."""
    probability = _ratio(probability, "probability")
    scale = _finite(scale, "scale")
    threshold = _ratio(threshold, "threshold", high=None)
    if scale < 0.0:
        raise PolicyError("scale: должен быть >= 0")
    if not 0.0 <= threshold < 1.0:
        raise PolicyError("threshold: должен лежать в [0, 1)")
    excess = max(0.0, (probability - threshold) / (1.0 - threshold))
    return -scale * excess


# ------------------------------------------------------------------------------------- scenarios
@dataclass(frozen=True)
class PolicyScenario:
    """One declared policy scenario (kind + size + decay); never a forecast."""

    scenario_id: str
    title_ru: str
    kind: str
    size: float
    decay: float = 0.5
    output_gap_effect_pp: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not SCENARIO_ID.match(self.scenario_id):
            raise PolicyError(f"scenario_id: недопустимый id {self.scenario_id!r}")
        if not isinstance(self.title_ru, str) or not self.title_ru.strip():
            raise PolicyError("title_ru: не может быть пустым")
        if self.kind not in SCENARIO_KINDS:
            raise PolicyError(f"kind: должен быть одним из {SCENARIO_KINDS}")
        size = _finite(self.size, "size")
        decay = _finite(self.decay, "decay")
        gap_effect = _finite(self.output_gap_effect_pp, "output_gap_effect_pp")
        if size <= 0.0:
            raise PolicyError("size: должен быть > 0")
        if not 0.0 < decay <= 1.0:
            raise PolicyError("decay: должен лежать в (0, 1]")
        if self.kind == "rate_shock" and size > 10.0:
            raise PolicyError("size: шок ставки не может превышать 10 п.п.")
        if self.kind == "deficit_shock" and size > 0.20:
            raise PolicyError("size: шок дефицита не может превышать 20% ВВП")
        if self.kind == "inflation_shock" and size > 10.0:
            raise PolicyError("size: инфляционный шок не может превышать 10 п.п.")
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "decay", decay)
        object.__setattr__(self, "output_gap_effect_pp", gap_effect)

    def to_dict(self) -> dict:
        unit = {"rate_shock": "pp", "deficit_shock": "gdp_share", "inflation_shock": "pp"}[self.kind]
        return {
            "scenario_id": self.scenario_id,
            "title_ru": self.title_ru,
            "kind": self.kind,
            "size": self.size,
            "unit": unit,
            "decay": self.decay,
            "output_gap_effect_pp": self.output_gap_effect_pp,
            "notes": self.notes,
            "classification": "SCENARIO",
            "causal_claim": False,
        }


def default_scenarios() -> tuple[PolicyScenario, ...]:
    return (
        PolicyScenario(
            scenario_id="RATE-PLUS-100BP",
            title_ru="Ставка +100 б.п. (постоянный шок правила)",
            kind="rate_shock", size=1.0,
            notes=("Ставка держится на 1 п.п. выше правила; инфляция и разрыв отвечают по A2–A3, "
                   "поэтому наблюдаемая ставка может отклониться от +1 п.п. к концу горизонта."),
        ),
        PolicyScenario(
            scenario_id="DEFICIT-PLUS-2PCT",
            title_ru="Дефицит +2% ВВП (снижение первичного баланса)",
            kind="deficit_shock", size=0.02,
            notes="Первичный баланс ниже базы на 2% ВВП; импульс спроса — A4, долг — тождество A5.",
        ),
        PolicyScenario(
            scenario_id="TARIFF-INFLATION-SHOCK",
            title_ru="Тарифы/инфляционный шок +1,5 п.п.",
            kind="inflation_shock", size=1.5, decay=0.5, output_gap_effect_pp=-0.4,
            notes=("Прямой импульс инфляции 1,5 п.п. с затуханием 0,5 за период и отрицательный "
                   "эффект на разрыв −0,4 п.п.; это допущение, а не оценка тарифов (блок 16)."),
        ),
    )


@dataclass(frozen=True)
class PolicyBaseline:
    """The declared baseline path, balance sheet and SFC aggregates (no data, no calibration)."""

    periods: int = DEFAULT_PERIODS
    inflation_pp: float = 2.0
    output_gap_pp: float = 0.0
    potential_growth: float = 0.015
    primary_balance: float = 0.0
    debt_ratio: float = 0.55
    nominal_gdp: float = 100.0
    savings_ratio: float = 0.20
    investment_ratio: float = 0.21
    current_account_ratio: float = -0.01
    cb_bonds_ratio: float = 0.30
    cb_fx_ratio: float = 0.05
    cb_loans_ratio: float = 0.02
    cb_currency_ratio: float = 0.10
    cb_reserves_ratio: float = 0.27
    cb_equity_ratio: float = 0.0
    beta_gap: float = 0.5
    gamma_pi: float = 0.25
    fiscal_multiplier: float = 0.5
    inflation_path_pp: tuple[float, ...] | None = None
    output_gap_path_pp: tuple[float, ...] | None = None
    rule: TaylorRuleWithInertia = field(default_factory=TaylorRuleWithInertia)

    def __post_init__(self) -> None:
        periods = _periods(self.periods)
        object.__setattr__(self, "periods", periods)
        inflation = _finite(self.inflation_pp, "inflation_pp")
        gap = _finite(self.output_gap_pp, "output_gap_pp")
        growth = _finite(self.potential_growth, "potential_growth")
        primary = _finite(self.primary_balance, "primary_balance")
        debt = _finite(self.debt_ratio, "debt_ratio")
        gdp = _positive(self.nominal_gdp, "nominal_gdp")
        if not -20.0 <= inflation <= 50.0:
            raise PolicyError("inflation_pp: вне диапазона [-20, 50]")
        if not -50.0 <= gap <= 50.0:
            raise PolicyError("output_gap_pp: вне диапазона [-50, 50]")
        if growth <= -1.0:
            raise PolicyError("potential_growth: должно быть > -1")
        if not -1.0 <= primary <= 1.0:
            raise PolicyError("primary_balance: должно лежать в [-1, 1]")
        if debt < 0.0:
            raise PolicyError("debt_ratio: валовой долг не может быть отрицательным")
        savings = _finite(self.savings_ratio, "savings_ratio")
        investment = _finite(self.investment_ratio, "investment_ratio")
        current_account = _finite(self.current_account_ratio, "current_account_ratio")
        sfc = check_sfc_aggregates(savings, investment, current_account)
        if not sfc["ok"]:
            raise PolicyError(
                "SFC: S != I + CA в базовой конфигурации "
                f"(S-I-CA={sfc['residual']:.3e}); исправьте savings/investment/current_account")
        for name in ("cb_bonds_ratio", "cb_fx_ratio", "cb_loans_ratio", "cb_currency_ratio",
                     "cb_reserves_ratio", "cb_equity_ratio"):
            value = _finite(getattr(self, name), name)
            object.__setattr__(self, name, value)
            if name != "cb_equity_ratio" and value < 0.0:
                raise PolicyError(f"{name}: доля баланса не может быть отрицательной")
        cb = self.central_bank()
        if not cb.check()["ok"]:  # pragma: no cover - construction already enforces it
            raise PolicyError("баланс ЦБ не сходится в базовой конфигурации")
        beta = _finite(self.beta_gap, "beta_gap")
        gamma = _finite(self.gamma_pi, "gamma_pi")
        multiplier = _finite(self.fiscal_multiplier, "fiscal_multiplier")
        if beta < 0.0:
            raise PolicyError("beta_gap: должен быть >= 0")
        if gamma < 0.0:
            raise PolicyError("gamma_pi: должен быть >= 0")
        if multiplier < 0.0:
            raise PolicyError("fiscal_multiplier: должен быть >= 0")
        if not isinstance(self.rule, TaylorRuleWithInertia):
            raise PolicyError("rule: ожидается TaylorRuleWithInertia")
        if self.inflation_path_pp is not None:
            _series(self.inflation_path_pp, "inflation_path_pp", periods)
        if self.output_gap_path_pp is not None:
            _series(self.output_gap_path_pp, "output_gap_path_pp", periods)
        object.__setattr__(self, "inflation_pp", inflation)
        object.__setattr__(self, "output_gap_pp", gap)
        object.__setattr__(self, "potential_growth", growth)
        object.__setattr__(self, "primary_balance", primary)
        object.__setattr__(self, "debt_ratio", debt)
        object.__setattr__(self, "nominal_gdp", gdp)
        object.__setattr__(self, "savings_ratio", savings)
        object.__setattr__(self, "investment_ratio", investment)
        object.__setattr__(self, "current_account_ratio", current_account)
        object.__setattr__(self, "beta_gap", beta)
        object.__setattr__(self, "gamma_pi", gamma)
        object.__setattr__(self, "fiscal_multiplier", multiplier)

    def inflation_series(self) -> tuple[float, ...]:
        if self.inflation_path_pp is None:
            return (self.inflation_pp,) * self.periods
        return _series(self.inflation_path_pp, "inflation_path_pp", self.periods)

    def gap_series(self) -> tuple[float, ...]:
        if self.output_gap_path_pp is None:
            return (self.output_gap_pp,) * self.periods
        return _series(self.output_gap_path_pp, "output_gap_path_pp", self.periods)

    def central_bank(self) -> CentralBankBalance:
        gdp = self.nominal_gdp
        return CentralBankBalance(
            bonds=self.cb_bonds_ratio * gdp,
            fx_reserves=self.cb_fx_ratio * gdp,
            loans=self.cb_loans_ratio * gdp,
            currency=self.cb_currency_ratio * gdp,
            reserves=self.cb_reserves_ratio * gdp,
            equity=None if self.cb_equity_ratio == 0.0 else self.cb_equity_ratio * gdp,
        )

    def with_t1_t3(self, t1_forecasts: Mapping, recession_probability: float,
                   t3_scale: float = 2.0, t3_threshold: float = 0.35) -> "PolicyBaseline":
        """Return a baseline whose ``pi*_t`` and ``y*_t`` come from T1 and T3 adapters."""
        inflation = inflation_path_from_t1(t1_forecasts, self.periods)
        gap = output_gap_from_recession_probability(recession_probability, scale=t3_scale,
                                                     threshold=t3_threshold)
        return replace(self, inflation_path_pp=inflation, output_gap_path_pp=(gap,) * self.periods)

    def to_dict(self) -> dict:
        return {
            "periods": self.periods,
            "inflation_pp": self.inflation_pp,
            "output_gap_pp": self.output_gap_pp,
            "potential_growth": self.potential_growth,
            "primary_balance": self.primary_balance,
            "debt_ratio": self.debt_ratio,
            "nominal_gdp": self.nominal_gdp,
            "savings_ratio": self.savings_ratio,
            "investment_ratio": self.investment_ratio,
            "current_account_ratio": self.current_account_ratio,
            "beta_gap": self.beta_gap,
            "gamma_pi": self.gamma_pi,
            "fiscal_multiplier": self.fiscal_multiplier,
            "inflation_path_pp": list(self.inflation_series()),
            "output_gap_path_pp": list(self.gap_series()),
            "rule": self.rule.to_dict(),
            "central_bank_ratios": {
                "bonds": self.cb_bonds_ratio,
                "fx_reserves": self.cb_fx_ratio,
                "loans": self.cb_loans_ratio,
                "currency": self.cb_currency_ratio,
                "reserves": self.cb_reserves_ratio,
                "equity": self.cb_equity_ratio,
            },
            "assumptions_ref": [item["id"] for item in ASSUMPTIONS],
        }


def baseline_from_config(data: Mapping) -> PolicyBaseline:
    """Build a validated baseline from a mapping (CLI config); unknown keys fail closed."""
    if not isinstance(data, Mapping):
        raise PolicyError("config.baseline: ожидается объект")
    allowed = {item.name for item in fields(PolicyBaseline)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PolicyError(f"config.baseline: неизвестные поля {unknown}")
    kwargs = dict(data)
    rule = kwargs.get("rule")
    if isinstance(rule, Mapping):
        rule_allowed = {item.name for item in fields(TaylorRuleWithInertia)}
        unknown_rule = sorted(set(rule) - rule_allowed)
        if unknown_rule:
            raise PolicyError(f"config.baseline.rule: неизвестные поля {unknown_rule}")
        kwargs["rule"] = TaylorRuleWithInertia(**rule)
    for key in ("inflation_path_pp", "output_gap_path_pp"):
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = tuple(kwargs[key])
    return PolicyBaseline(**kwargs)


def scenarios_from_config(items: Sequence[Mapping]) -> tuple[PolicyScenario, ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise PolicyError("config.scenarios: ожидается список объектов")
    allowed = {item.name for item in fields(PolicyScenario)}
    scenarios = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise PolicyError(f"config.scenarios[{index}]: ожидается объект")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise PolicyError(f"config.scenarios[{index}]: неизвестные поля {unknown}")
        scenarios.append(PolicyScenario(**dict(item)))
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise PolicyError("config.scenarios: id сценариев должны быть уникальны")
    if not scenarios:
        raise PolicyError("config.scenarios: список не должен быть пустым")
    return tuple(scenarios)


# ---------------------------------------------------------------------------------- scenario engine
def _baseline_paths(baseline: PolicyBaseline) -> dict:
    inflation = baseline.inflation_series()
    gap = baseline.gap_series()
    rates: list[float] = []
    real_rates: list[float] = []
    previous = baseline.rule.neutral_rate
    for index in range(baseline.periods):
        gap_input = gap[index - 1] if index else gap[0]
        rate = baseline.rule.step(previous, inflation[index], gap_input)
        rates.append(rate)
        real_rates.append((rate - inflation[index]) / 100.0)
        previous = rate
    growth = []
    primary = []
    previous_gap = gap[0]
    for index in range(baseline.periods):
        growth.append(baseline.potential_growth + (gap[index] - previous_gap) / 100.0)
        primary.append(baseline.primary_balance)
        previous_gap = gap[index]
    debt = _debt_arrays(baseline.debt_ratio, real_rates, growth, primary)
    return {
        "inflation": inflation,
        "gap": gap,
        "rate": tuple(rates),
        "real_rate": tuple(real_rates),
        "growth": tuple(growth),
        "primary": tuple(primary),
        "debt": debt,
    }


def _shock_paths(scenario: PolicyScenario, periods: int) -> dict:
    size = scenario.size
    if scenario.kind == "rate_shock":
        return {"rate": (size,) * periods, "inflation": (0.0,) * periods,
                "deficit": (0.0,) * periods, "gap": (0.0,) * periods}
    if scenario.kind == "deficit_shock":
        return {"rate": (0.0,) * periods, "inflation": (0.0,) * periods,
                "deficit": (size,) * periods, "gap": (0.0,) * periods}
    if scenario.kind == "inflation_shock":
        inflation = tuple(size * scenario.decay ** index for index in range(periods))
        gap = tuple(scenario.output_gap_effect_pp * scenario.decay ** index
                    for index in range(periods))
        return {"rate": (0.0,) * periods, "inflation": inflation,
                "deficit": (0.0,) * periods, "gap": gap}
    raise PolicyError(f"kind: неизвестный тип сценария {scenario.kind!r}")  # pragma: no cover


def _identity_ok(residual: float) -> bool:
    return abs(float(residual)) <= TOLERANCE


def run_scenario(baseline: PolicyBaseline, scenario: PolicyScenario) -> dict:
    """Run one scenario and return aggregate paths, identities, SFC/CB blocks and a digest."""
    base = _baseline_paths(baseline)
    shock = _shock_paths(scenario, baseline.periods)
    rule = baseline.rule
    paths: list[dict] = []
    inflation_values: list[float] = []
    gap_values: list[float] = []
    rate_values: list[float] = []
    real_values: list[float] = []
    growth_values: list[float] = []
    primary_values: list[float] = []
    previous_rate = rule.neutral_rate
    previous_gap = base["gap"][0]
    for index in range(baseline.periods):
        gap_deviation = (gap_values[index - 1] - base["gap"][index - 1]) if index else 0.0
        inflation = (base["inflation"][index] + shock["inflation"][index]
                     + baseline.gamma_pi * gap_deviation)
        if not -20.0 <= inflation <= 60.0:
            raise PolicyError(f"period {index + 1}: инфляция вышла за пределы сценария "
                              f"({inflation:.3f} п.п.) — сценарий невалиден (fail-closed)")
        rule_rate = rule.rule_rate(inflation, previous_gap)
        rate = rule.inertia * previous_rate + (1.0 - rule.inertia) * (rule_rate
                                                                     + shock["rate"][index])
        rate = min(rule.upper_bound, max(rule.lower_bound, rate))
        real_rate = (rate - inflation) / 100.0
        gap = (base["gap"][index] + shock["gap"][index]
               + baseline.fiscal_multiplier * shock["deficit"][index] * 100.0
               - baseline.beta_gap * (real_rate - base["real_rate"][index]) * 100.0)
        growth = baseline.potential_growth + (
            gap - (gap_values[index - 1] if index else base["gap"][0])) / 100.0
        primary = baseline.primary_balance - shock["deficit"][index]
        inflation_values.append(inflation)
        gap_values.append(gap)
        rate_values.append(rate)
        real_values.append(real_rate)
        growth_values.append(growth)
        primary_values.append(primary)
        previous_rate = rate
        previous_gap = gap
    fiscal = _debt_arrays(baseline.debt_ratio, real_values, growth_values, primary_values)
    debt_path = fiscal["debt_path"]
    for index in range(baseline.periods):
        paths.append({
            "period": index + 1,
            "inflation_pp": inflation_values[index],
            "output_gap_pp": gap_values[index],
            "policy_rate_pp": rate_values[index],
            "rule_rate_pp": rule.rule_rate(inflation_values[index],
                                           gap_values[index - 1] if index else base["gap"][0]),
            "real_rate_pp": real_values[index] * 100.0,
            "growth": growth_values[index],
            "primary_balance": primary_values[index],
            "deficit_ratio": fiscal["deficit_path"][index],
            "interest_burden_ratio": fiscal["interest_path"][index],
            "debt_ratio": debt_path[index + 1],
            "delta_inflation_pp": inflation_values[index] - base["inflation"][index],
            "delta_output_gap_pp": gap_values[index] - base["gap"][index],
            "delta_policy_rate_pp": rate_values[index] - base["rate"][index],
            "delta_debt_ratio": debt_path[index + 1] - base["debt"]["debt_path"][index + 1],
            "delta_deficit_ratio": (fiscal["deficit_path"][index]
                                    - base["debt"]["deficit_path"][index]),
        })
    debt_identity = _identity_ok(fiscal["max_debt_identity_residual"])
    deficit_identity = _identity_ok(fiscal["max_deficit_identity_residual"])
    cb = baseline.central_bank()
    cb_check = cb.check()
    if not cb_check["ok"] or not debt_identity or not deficit_identity:
        raise PolicyError("сценарный блок нарушил тождества (fail-closed): "
                          f"debt={fiscal['max_debt_identity_residual']:.3e}, "
                          f"deficit={fiscal['max_deficit_identity_residual']:.3e}, "
                          f"cb={cb_check['residual']:.3e}")
    sfc = check_sfc_aggregates(baseline.savings_ratio, baseline.investment_ratio,
                               baseline.current_account_ratio)
    final_deficit = fiscal["deficit_path"][-1]
    government_net_lending = -final_deficit
    private_saving = baseline.savings_ratio - government_net_lending
    fiscal_sfc_residual = (private_saving + government_net_lending
                           - baseline.investment_ratio - baseline.current_account_ratio)
    if not sfc["ok"] or not _identity_ok(fiscal_sfc_residual):
        raise PolicyError("SFC-связь не сходится (fail-closed): "
                          f"S-I-CA={sfc['residual']:.3e}, fiscal={fiscal_sfc_residual:.3e}")
    qe_amount = DEFAULT_QE_RATIO * baseline.nominal_gdp
    cb_after_qe = cb.buy_bonds(qe_amount)
    delta_debt = debt_path[-1] - base["debt"]["debt_path"][-1]
    summary = {
        "final_policy_rate_pp": rate_values[-1],
        "final_policy_rate_delta_pp": rate_values[-1] - base["rate"][-1],
        "peak_policy_rate_pp": max(rate_values),
        "policy_rate_max_delta_pp": max(rate_values[index] - base["rate"][index]
                                        for index in range(baseline.periods)),
        "final_inflation_pp": inflation_values[-1],
        "final_inflation_delta_pp": inflation_values[-1] - base["inflation"][-1],
        "max_inflation_delta_pp": max(inflation_values[index] - base["inflation"][index]
                                      for index in range(baseline.periods)),
        "mean_inflation_delta_pp": math.fsum(
            inflation_values[index] - base["inflation"][index]
            for index in range(baseline.periods)) / baseline.periods,
        "final_output_gap_pp": gap_values[-1],
        "final_output_gap_delta_pp": gap_values[-1] - base["gap"][-1],
        "min_output_gap_delta_pp": min(gap_values[index] - base["gap"][index]
                                       for index in range(baseline.periods)),
        "mean_output_gap_delta_pp": math.fsum(
            gap_values[index] - base["gap"][index]
            for index in range(baseline.periods)) / baseline.periods,
        "initial_debt_ratio": baseline.debt_ratio,
        "final_debt_ratio": debt_path[-1],
        "final_debt_ratio_delta": delta_debt,
        "peak_debt_ratio": max(debt_path),
        "final_deficit_ratio": final_deficit,
        "final_deficit_ratio_delta": final_deficit - base["debt"]["deficit_path"][-1],
        "cumulative_deficit_ratio": math.fsum(fiscal["deficit_path"]),
        "final_interest_burden_ratio": fiscal["interest_path"][-1],
        "r_minus_g_initial": real_values[0] - growth_values[0],
        "stabilizing_primary_balance_initial": _stabilizing_pb(
            baseline.debt_ratio, real_values[0], growth_values[0]),
        "debt_trend": ("STABLE" if abs(delta_debt) <= 1e-12
                       else "GROWING" if delta_debt > 0 else "DECLINING"),
        "debt_identity_max_residual": fiscal["max_debt_identity_residual"],
        "deficit_identity_max_residual": fiscal["max_deficit_identity_residual"],
    }
    payload = {
        "schema": SCENARIO_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "scenario": scenario.to_dict(),
        "baseline": baseline.to_dict(),
        "paths": paths,
        "summary": summary,
        "debt": {
            "identity": "b' = (1+r)/(1+g)*b - pb; D = r*b - pb",
            "max_debt_identity_residual": fiscal["max_debt_identity_residual"],
            "max_deficit_identity_residual": fiscal["max_deficit_identity_residual"],
            "stabilizing_primary_balance_initial": summary["stabilizing_primary_balance_initial"],
            "r_minus_g_initial": summary["r_minus_g_initial"],
            "classification": "IDENTITY",
        },
        "central_bank": {
            "baseline": cb.to_dict(baseline.nominal_gdp),
            "net_interest_income_to_gdp_baseline": cb.net_interest_income(
                base["rate"][-1]) / baseline.nominal_gdp,
            "net_interest_income_to_gdp_scenario": cb.net_interest_income(
                rate_values[-1]) / baseline.nominal_gdp,
            "qe_illustration": {
                "purchase_to_gdp": DEFAULT_QE_RATIO,
                "after": cb_after_qe.to_dict(baseline.nominal_gdp),
                "delta_bonds_to_gdp": (cb_after_qe.bonds - cb.bonds) / baseline.nominal_gdp,
                "delta_reserves_to_gdp": ((cb_after_qe.reserves - cb.reserves)
                                          / baseline.nominal_gdp),
                "delta_liquidity_to_gdp": ((cb_after_qe.liquidity - cb.liquidity)
                                           / baseline.nominal_gdp),
                "identity_ok": cb_after_qe.check()["ok"],
                "note": "QE — иллюстрация состава баланса, не канал в цены/доходности (A8)",
            },
            "check": cb_check,
        },
        "sfc": {
            "baseline": sfc,
            "final_deficit_ratio": final_deficit,
            "government_net_lending_ratio": government_net_lending,
            "private_saving_ratio": private_saving,
            "fiscal_residual": fiscal_sfc_residual,
            "fiscal_identity_ok": _identity_ok(fiscal_sfc_residual),
            "note": ("S=I+CA — бухгалтерское тождество; S_private — остаток, а не поведение "
                     "(docs/146 §4)"),
        },
        "checks": {
            "debt_identity": debt_identity,
            "deficit_identity": deficit_identity,
            "central_bank_identity": cb_check["ok"],
            "sfc_identity": sfc["ok"],
            "fiscal_sfc_identity": _identity_ok(fiscal_sfc_residual),
            "gating": GATING,
            "causal_claim": CAUSAL_CLAIM,
        },
        "assumptions": assumptions(),
        "limitations": limitations(),
    }
    payload["digest"] = digest(payload)
    return payload


def run_all(baseline: PolicyBaseline | None = None,
            scenarios: Sequence[PolicyScenario] | None = None,
            only: Sequence[str] | None = None) -> dict:
    """Run the declared scenarios and return an index plus per-scenario reports."""
    baseline = PolicyBaseline() if baseline is None else baseline
    if not isinstance(baseline, PolicyBaseline):
        raise PolicyError("baseline: ожидается PolicyBaseline")
    pool = list(default_scenarios() if scenarios is None else scenarios)
    if not pool:
        raise PolicyError("scenarios: список не должен быть пустым")
    ids = [scenario.scenario_id for scenario in pool]
    if len(ids) != len(set(ids)):
        raise PolicyError("scenarios: id должны быть уникальны")
    if only is not None:
        only_set = {str(item).strip() for item in only if str(item).strip()}
        unknown = sorted(only_set - set(ids))
        if unknown:
            raise PolicyError(f"only: неизвестные сценарии {unknown}")
        pool = [scenario for scenario in pool if scenario.scenario_id in only_set]
    reports = [run_scenario(baseline, scenario) for scenario in pool]
    entries = []
    for report in reports:
        summary = report["summary"]
        entries.append({
            "id": report["scenario"]["scenario_id"],
            "title_ru": report["scenario"]["title_ru"],
            "kind": report["scenario"]["kind"],
            "size": report["scenario"]["size"],
            "unit": report["scenario"]["unit"],
            "digest": report["digest"],
            "headline": {
                "final_policy_rate_delta_pp": summary["final_policy_rate_delta_pp"],
                "max_inflation_delta_pp": summary["max_inflation_delta_pp"],
                "min_output_gap_delta_pp": summary["min_output_gap_delta_pp"],
                "final_debt_ratio_delta": summary["final_debt_ratio_delta"],
                "final_deficit_ratio_delta": summary["final_deficit_ratio_delta"],
            },
        })
    index = {
        "schema": INDEX_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "tool_version": TOOL_VERSION,
        "protocol": PROTOCOL_REFERENCE,
        "baseline": baseline.to_dict(),
        "scenarios": entries,
        "checks": {
            "scenario_count": len(reports),
            "all_debt_identities": all(report["checks"]["debt_identity"] for report in reports),
            "all_deficit_identities": all(report["checks"]["deficit_identity"]
                                          for report in reports),
            "all_central_bank_identities": all(report["checks"]["central_bank_identity"]
                                               for report in reports),
            "all_sfc_identities": all(report["checks"]["sfc_identity"] for report in reports),
            "all_fiscal_sfc_identities": all(report["checks"]["fiscal_sfc_identity"]
                                             for report in reports),
            "gating": GATING,
            "causal_claim": CAUSAL_CLAIM,
        },
        "max_identity_residual": max(
            [report["summary"]["debt_identity_max_residual"] for report in reports]
            + [report["summary"]["deficit_identity_max_residual"] for report in reports]),
        "assumptions": assumptions(),
        "limitations": limitations(),
    }
    index["digest"] = digest(index)
    return {"index": index, "reports": reports}


__all__ = [
    "SCHEMA", "SCENARIO_SCHEMA", "INDEX_SCHEMA", "TOOL_VERSION", "STATUS", "ADMISSION", "GATING",
    "CAUSAL_CLAIM", "STATUS_NOTE", "DEFAULT_PERIODS", "MAX_PERIODS", "TOLERANCE",
    "PROTOCOL_REFERENCE", "ASSUMPTIONS", "LIMITATIONS", "PolicyError",
    "TaylorRuleWithInertia", "CentralBankBalance", "project_debt",
    "check_sfc_aggregates", "inflation_path_from_t1", "output_gap_from_recession_probability",
    "PolicyScenario", "default_scenarios", "PolicyBaseline", "baseline_from_config",
    "scenarios_from_config", "run_scenario", "run_all", "canonical_json", "digest", "assumptions",
    "limitations",
]
