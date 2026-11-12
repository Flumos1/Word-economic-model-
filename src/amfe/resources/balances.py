"""Material balances: product supply-use identity and the no-double-counting invariant.

These are the accounting invariants of stage 4 in executable form (docs/38 I01, E01):

* every product's supply equals its use in one unit and one valuation base
  (``domestic_output + imports + other_supply = intermediate_use + final_consumption +
  capital_formation + exports + other_use``, stock changes counted exactly once);
* gross output and intermediate use are never added together: value added is
  ``x - A x``, and for a closed economy the sum of value added equals the sum of final
  uses, which is the prototype's production/expenditure reconciliation;
* all entries are non-negative physical (or one consistent value) quantities - a negative
  supply has to be explained as a stock change, not hidden in the data.

Interface with the accounting core (stage 2) and the two-country prototype (stage 3):
``ProductBalance.supply``/``use`` labels are free, so the trade flows of the two-country
model enter as ``imports``/``exports`` in their own units, and ``gross_output`` /
``value_added`` are the quantities a typed sector account would receive.  The prototype
does not post money: distribution, taxes and financial claims stay in the accounting core.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

TOLERANCE = 1e-9


class BalanceError(ValueError):
    """A material balance is inconsistent or a quantity is invalid."""


def _non_negative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise BalanceError(f"{name} must be a finite non-negative number, got {value!r}")
    return number


@dataclass
class ProductBalance:
    """Supply-use identity of one product in one period (docs/38 I01)."""

    product: str
    unit: str
    supply: Mapping[str, float]
    use: Mapping[str, float]
    period: str = "period"
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        if not isinstance(self.product, str) or not self.product.strip():
            raise BalanceError("product must be a non-empty string")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise BalanceError("unit must be a non-empty string")
        self.supply = {str(key): _non_negative(value, f"supply[{key}]")
                       for key, value in dict(self.supply).items()}
        self.use = {str(key): _non_negative(value, f"use[{key}]")
                    for key, value in dict(self.use).items()}
        if not self.supply or not self.use:
            raise BalanceError(f"product {self.product!r}: supply and use must both be non-empty")
        if self.tolerance <= 0.0:
            raise BalanceError("tolerance must be positive")

    @property
    def supply_total(self) -> float:
        return math.fsum(self.supply.values())

    @property
    def use_total(self) -> float:
        return math.fsum(self.use.values())

    @property
    def residual(self) -> float:
        return self.supply_total - self.use_total

    def validate(self, tolerance: Optional[float] = None) -> float:
        """Raise unless supply equals use; returns the residual."""
        limit = self.tolerance if tolerance is None else float(tolerance)
        residual = self.residual
        if abs(residual) > limit:
            raise BalanceError(
                f"product {self.product!r} in {self.period}: supply {self.supply_total} != "
                f"use {self.use_total} (residual {residual:.6g})"
            )
        return residual

    def describe(self) -> dict:
        return {"product": self.product, "unit": self.unit, "period": self.period,
                "supply_total": self.supply_total, "use_total": self.use_total}


@dataclass
class MaterialBalances:
    """A validated collection of product balances for one period and economy."""

    products: Sequence[ProductBalance]
    territory: str = "domestic"
    period: str = "period"

    def __post_init__(self) -> None:
        self.products = list(self.products)
        if not self.products:
            raise BalanceError("MaterialBalances needs at least one product")
        names = [balance.product for balance in self.products]
        if len(set(names)) != len(names):
            raise BalanceError(f"duplicate products in one balance set: {names}")

    def validate(self) -> Dict[str, float]:
        """Validate every product (each in its own unit); returns the residuals."""
        residuals = {}
        for balance in self.products:
            residuals[balance.product] = balance.validate()
        return residuals

    def residuals(self) -> Dict[str, float]:
        return {balance.product: balance.residual for balance in self.products}

    def describe(self) -> dict:
        return {"territory": self.territory, "period": self.period,
                "products": [balance.describe() for balance in self.products]}


def balances_from_io(model, gross_output: Sequence[float], final_demand: Sequence[float],
                     imports: Optional[Mapping[str, float]] = None,
                     exports: Optional[Mapping[str, float]] = None) -> MaterialBalances:
    """Build product balances from an IO solution (closed economy by default).

    ``domestic_output = x``, ``imports`` and ``exports`` are optional stage-3 hooks.  For the
    closed case (no trade) every product balances exactly because ``x = A x + d``; with
    trade the caller must supply flows that satisfy the same identity, and a residual
    signals an inconsistent trade vector rather than being silently absorbed.
    """
    sectors = list(model.sector_ids)
    intermediate = model.intermediate_use(gross_output)
    output = [float(value) for value in gross_output]
    demand = [float(value) for value in final_demand]
    if len(output) != len(sectors) or len(demand) != len(sectors):
        raise BalanceError("gross_output and final_demand must match the model sectors")
    imports = dict(imports or {})
    exports = dict(exports or {})
    unknown = (set(imports) | set(exports)) - set(sectors)
    if unknown:
        raise BalanceError(f"trade flows for unknown sectors: {sorted(unknown)}")
    balances = []
    for index, sector in enumerate(sectors):
        balances.append(ProductBalance(
            product=sector,
            unit="physical_unit",
            supply={"domestic_output": output[index],
                    "imports": imports.get(sector, 0.0)},
            use={"intermediate_use": intermediate[index],
                 "final_consumption": demand[index],
                 "capital_formation": 0.0,
                 "exports": exports.get(sector, 0.0)},
        ))
    return MaterialBalances(balances)


def check_no_double_counting(model, gross_output: Sequence[float],
                             final_demand: Sequence[float],
                             tolerance: float = TOLERANCE) -> dict:
    """Production/expenditure reconciliation of the closed IO prototype (docs/38 E01).

    ``sum(x) - sum(A x) == sum(d)`` by construction; the function returns the three
    quantities and the residuals so a test can assert the identity and the magnitude of
    the arithmetic error.  Gross output and intermediate use are reported separately and
    never added: only their difference (value added) is comparable to final uses.
    """
    output = [float(value) for value in gross_output]
    demand = [float(value) for value in final_demand]
    if len(output) != model.size or len(demand) != model.size:
        raise BalanceError("gross_output and final_demand must match the model sectors")
    intermediate = model.intermediate_use(output)
    gross_output_total = math.fsum(output)
    intermediate_total = math.fsum(intermediate)
    value_added = gross_output_total - intermediate_total
    final_total = math.fsum(demand)
    residual = value_added - final_total
    if abs(residual) > tolerance:
        raise BalanceError(
            f"value added {value_added} != final uses {final_total} (residual {residual:.6g}); "
            "the IO solution is not consistent with the final demand"
        )
    return {
        "gross_output": gross_output_total,
        "intermediate_use": intermediate_total,
        "value_added": value_added,
        "final_uses": final_total,
        "residual": residual,
    }


def check_non_negative(name: str, values: Iterable[float]) -> None:
    """Raise on a negative entry; a physical quantity cannot be negative."""
    for index, value in enumerate(values):
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise BalanceError(f"{name}[{index}] must be a finite non-negative number, got {value!r}")
