"""Government-debt dynamics: the r-g identity and its stress scenario.

The debt-to-GDP ratio evolves by an accounting IDENTITY (given the real interest rate r, the real
growth rate g and the primary balance pb, all in the same units):

    b_{t+1} = (1 + r_t) / (1 + g_t) * b_t - pb_t

It is an identity, not a behavioural hypothesis: it holds whatever the government does, and it
does not say why r, g or pb take their values.  The stabilising primary balance is
pb* = (r - g)/(1 + g) * b; with b constant, debt is stable exactly when pb = pb*.

The r-g stress is a SCENARIO (kind == SCENARIO): the shocks are exogenous assumptions and are not
causal estimates.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_IDENTITY, KIND_SCENARIO, Mechanism, Scenario

DEBT_DYNAMICS_MECHANISM = Mechanism(
    mechanism_id="DYN-DEBT-IDENTITY",
    title_ru="Динамика долга к ВВП (тождество r-g)",
    kind=KIND_IDENTITY,
    description_ru=(
        "b_{t+1} = (1+r_t)/(1+g_t)*b_t - pb_t: тождество отношения долга к ВВП при заданных реальной "
        "ставке, реальном росте и первичном балансе. Не объясняет r, g и pb и не является гипотезой "
        "о поведении правительства."),
    assumptions=("r, g, pb заданы экзогенно или другой моделью", "одна и та же единица измерения",
                 "нет переоценки валюты долга и разовых операций"),
    source="docs/34; docs/38 block 12",
)

DEBT_STRESS_MECHANISM = Mechanism(
    mechanism_id="DYN-DEBT-STRESS",
    title_ru="Стресс r-g по долгу",
    kind=KIND_SCENARIO,
    description_ru=(
        "Сценарий: шок реальной ставки и/или роста при заданном первичном балансе. Сравниваются "
        "базовая и шоковая траектории долга; размеры шоков экзогенны и не являются оценкой "
        "причинного эффекта."),
    assumptions=("шок r/g постоянен и не зависит от долга", "первичный баланс не меняется"),
    source="docs/42 stage 5 scenario layer",
)


def debt_step(debt_ratio: float, real_rate: float, growth: float,
              primary_balance: float) -> float:
    """One period of the debt-ratio identity."""
    debt_ratio, real_rate, growth, primary_balance = (float(debt_ratio), float(real_rate),
                                                      float(growth), float(primary_balance))
    if not all(math.isfinite(item) for item in (debt_ratio, real_rate, growth, primary_balance)):
        raise DynamicsError("debt inputs must be finite")
    if growth <= -1.0:
        raise DynamicsError("growth must be greater than -1 (gross growth positive)")
    return (1.0 + real_rate) / (1.0 + growth) * debt_ratio - primary_balance


def debt_path(initial_debt: float, real_rate: float, growth: float, primary_balance: float,
              periods: int = 30) -> np.ndarray:
    """Debt-ratio path including the initial observation."""
    initial_debt, real_rate = float(initial_debt), float(real_rate)
    growth, primary_balance = float(growth), float(primary_balance)
    periods = int(periods)
    if not all(math.isfinite(item) for item in (initial_debt, real_rate, growth,
                                                primary_balance)):
        raise DynamicsError("debt inputs must be finite")
    if growth <= -1.0:
        raise DynamicsError("growth must be greater than -1")
    if periods < 1:
        raise DynamicsError("periods must be a positive integer")
    path = np.empty(periods + 1)
    path[0] = initial_debt
    for step in range(periods):
        path[step + 1] = debt_step(path[step], real_rate, growth, primary_balance)
    return path


def stabilizing_primary_balance(debt_ratio: float, real_rate: float,
                                growth: float) -> float:
    """Primary balance that keeps the debt ratio constant: (r - g)/(1 + g) * b."""
    debt_ratio, real_rate, growth = float(debt_ratio), float(real_rate), float(growth)
    if not all(math.isfinite(item) for item in (debt_ratio, real_rate, growth)):
        raise DynamicsError("debt inputs must be finite")
    if growth <= -1.0:
        raise DynamicsError("growth must be greater than -1")
    return (real_rate - growth) / (1.0 + growth) * debt_ratio


def sustainability(initial_debt: float, real_rate: float, growth: float,
                   primary_balance: float, periods: int = 30,
                   tolerance: float = 1e-9) -> dict:
    """Report the trend, the stabilising balance and the r-g regime (identity, no causality)."""
    path = debt_path(initial_debt, real_rate, growth, primary_balance, periods=periods)
    drift = debt_step(path[-1], real_rate, growth, primary_balance) - path[-1]
    if abs(drift) <= tolerance:
        trend = "STABLE"
    elif drift < 0.0:
        trend = "DECLINING"
    else:
        trend = "GROWING"
    required = stabilizing_primary_balance(initial_debt, real_rate, growth)
    return {
        "path": path,
        "initial_debt": float(initial_debt),
        "final_debt": float(path[-1]),
        "drift_per_period": float(drift),
        "trend": trend,
        "r_minus_g": float(real_rate) - float(growth),
        "r_less_g": bool(real_rate < growth),
        "stabilizing_primary_balance": float(required),
        "primary_balance": float(primary_balance),
        "balance_gap": float(primary_balance) - float(required),
        "snowball_effect": float((float(real_rate) - float(growth))
                                 / (1.0 + float(growth)) * float(initial_debt)),
        "mechanism": DEBT_DYNAMICS_MECHANISM.to_dict(),
        "note": "тождество; trend описывает арифметику пути при экзогенных r, g, pb",
    }


def debt_stress_scenario(initial_debt: float, real_rate: float, growth: float,
                         primary_balance: float, rate_shock: float = 1.0,
                         growth_shock: float = -1.0, periods: int = 20) -> dict:
    """Compare a baseline debt path with an exogenous r-g stress (kind == SCENARIO)."""
    rate_shock, growth_shock = float(rate_shock), float(growth_shock)
    if not math.isfinite(rate_shock) or not math.isfinite(growth_shock):
        raise DynamicsError("shocks must be finite")
    scenario = Scenario(
        scenario_id="DEBT-RG-STRESS",
        title_ru="Шок r-g по долгу",
        shocks={"real_rate": rate_shock, "growth": growth_shock},
        horizon=periods,
        description_ru="Постоянный шок реальной ставки и роста при неизменном первичном балансе.",
    )
    baseline = debt_path(initial_debt, real_rate, growth, primary_balance, periods=periods)
    stressed = debt_path(initial_debt, real_rate + rate_shock, growth + growth_shock,
                         primary_balance, periods=periods)
    return {
        "scenario": scenario.mechanism().to_dict(),
        "baseline_path": baseline,
        "stressed_path": stressed,
        "baseline_final": float(baseline[-1]),
        "stressed_final": float(stressed[-1]),
        "final_gap": float(stressed[-1] - baseline[-1]),
        "stressed_sustainability": sustainability(initial_debt, real_rate + rate_shock,
                                                  growth + growth_shock, primary_balance,
                                                  periods=periods),
        "caveat": "экзогенный сценарий: размеры шоков не оценены как причинные эффекты",
    }
