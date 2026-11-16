"""Deterministic market-clearing solver for the agri archetype (docs/148).

The price multiplier ``m`` is the only endogenous variable.  Demand responds to price with a
constant elasticity, and the market clears when demand plus the target stocks-to-use absorb the
available supply::

    U(m) = U0 * m**(-e_eff)
    U(m) * (1 + u_target) = available_supply
    ->  m = (U0 * (1 + u_target) / available_supply)**(1 / e_eff)

The closed-form root is bracketed and found by bisection with a fixed iteration cap and tolerance:
the solver is deterministic and fails closed when the bracket does not contain a root or when the
result leaves the documented multiplier bounds (no "explosive price" extrapolation).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import AgriMarketError

MIN_MULTIPLIER = 0.20
MAX_MULTIPLIER = 5.00
DEFAULT_TOLERANCE = 1e-10
MAX_ITERATIONS = 200


@dataclass(frozen=True)
class SolverResult:
    multiplier: float
    iterations: int
    residual: float
    elasticity: float
    target_stocks_use: float
    available_supply_mt: float
    baseline_use_mt: float

    def to_dict(self) -> dict:
        return {"multiplier": round(float(self.multiplier), 9),
                "iterations": int(self.iterations),
                "residual": float(self.residual),
                "elasticity": round(float(self.elasticity), 9),
                "target_stocks_use": round(float(self.target_stocks_use), 6),
                "available_supply_mt": round(float(self.available_supply_mt), 6),
                "baseline_use_mt": round(float(self.baseline_use_mt), 6)}


def _excess(multiplier: float, *, baseline_use_mt: float, available_supply_mt: float,
            target_stocks_use: float, elasticity: float) -> float:
    demand = float(baseline_use_mt) * float(multiplier) ** (-float(elasticity))
    required = float(available_supply_mt) / (1.0 + float(target_stocks_use))
    return demand - required


def solve_price_multiplier(*, baseline_use_mt: float, available_supply_mt: float,
                           target_stocks_use: float, elasticity: float,
                           lower: float = MIN_MULTIPLIER, upper: float = MAX_MULTIPLIER,
                           tolerance: float = DEFAULT_TOLERANCE,
                           max_iterations: int = MAX_ITERATIONS) -> SolverResult:
    """Bisection root of the deterministic clearing condition (monotone decreasing in m)."""
    values = (float(baseline_use_mt), float(available_supply_mt), float(target_stocks_use),
              float(elasticity), float(lower), float(upper))
    if not all(math.isfinite(value) for value in values):
        raise AgriMarketError("solver inputs must be finite")
    if baseline_use_mt <= 0.0 or available_supply_mt <= 0.0:
        raise AgriMarketError("baseline use and available supply must be positive")
    if target_stocks_use < 0.0:
        raise AgriMarketError("target stocks-to-use must be non-negative")
    if elasticity <= 0.0:
        raise AgriMarketError("elasticity must be positive")
    if not (0.0 < lower < upper):
        raise AgriMarketError("solver bounds must satisfy 0 < lower < upper")
    if tolerance <= 0.0 or not math.isfinite(tolerance):
        raise AgriMarketError("tolerance must be positive and finite")
    if max_iterations < 1:
        raise AgriMarketError("max_iterations must be >= 1")
    function = lambda multiplier: _excess(  # noqa: E731 - small local monotone function
        multiplier, baseline_use_mt=baseline_use_mt,
        available_supply_mt=available_supply_mt, target_stocks_use=target_stocks_use,
        elasticity=elasticity)
    f_low, f_high = function(lower), function(upper)
    if not (f_low > 0.0 and f_high < 0.0):
        raise AgriMarketError(
            "no price-multiplier bracket: the scenario does not clear inside "
            f"[{lower}, {upper}] (excess at bounds: {f_low:.6f}, {f_high:.6f})")
    low, high = float(lower), float(upper)
    iterations = 0
    for iteration in range(1, max_iterations + 1):
        middle = 0.5 * (low + high)
        value = function(middle)
        iterations = iteration
        if abs(value) <= tolerance or 0.5 * (high - low) <= tolerance:
            low = high = middle
            break
        if value > 0.0:
            low = middle
        else:
            high = middle
    multiplier = 0.5 * (low + high)
    residual = function(multiplier)
    if not math.isfinite(multiplier) or multiplier <= 0.0:
        raise AgriMarketError("solver produced a non-positive multiplier")
    if abs(residual) > 1e-6:
        raise AgriMarketError(f"solver did not converge: residual {residual:.3e}")
    return SolverResult(multiplier=multiplier, iterations=iterations, residual=residual,
                        elasticity=elasticity, target_stocks_use=target_stocks_use,
                        available_supply_mt=available_supply_mt,
                        baseline_use_mt=baseline_use_mt)


__all__ = ["MAX_ITERATIONS", "MAX_MULTIPLIER", "MIN_MULTIPLIER", "SolverResult",
           "solve_price_multiplier"]
