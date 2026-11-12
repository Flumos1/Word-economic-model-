"""Exchange rates and currency conversion.

Convention
----------
``rates[c]`` is the number of numeraire units per one unit of currency ``c``.
The numeraire itself always has rate 1.  With two currencies A/B this gives a
single cross rate without triangular arbitrage; it is intentionally a minimal
FX layer, not a market microstructure model.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from .errors import FXError, as_amount, as_rate


class FXMarket:
    """A complete, positive quote table against one common numeraire."""

    def __init__(self, numeraire: str, rates: Mapping[str, object]):
        if not isinstance(numeraire, str) or not numeraire:
            raise FXError("numeraire must be a non-empty currency code")
        if not isinstance(rates, Mapping) or len(rates) < 2:
            raise FXError("at least two currencies are required")
        parsed: dict[str, Decimal] = {}
        for currency, value in rates.items():
            if not isinstance(currency, str) or not currency:
                raise FXError("currency codes must be non-empty strings")
            if currency in parsed:
                raise FXError(f"duplicate currency {currency!r}")
            parsed[currency] = as_rate(value, name=f"rate[{currency}]")
        if numeraire not in parsed:
            parsed[numeraire] = Decimal(1)
        else:
            parsed[numeraire] = Decimal(1)
        self.numeraire = numeraire
        self._rates = parsed

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(self._rates)

    def rate(self, currency: str) -> Decimal:
        try:
            return self._rates[currency]
        except KeyError as exc:
            raise FXError(f"unknown currency {currency!r}") from exc

    def numeraire_value(self, amount, currency: str) -> Decimal:
        """Value of ``amount`` units of ``currency`` in numeraire units."""
        return as_amount(amount, name="amount") * self.rate(currency)

    def convert(self, amount, from_currency: str, to_currency: str) -> Decimal:
        """Convert an amount between any two currencies in the quote table."""
        source = self.rate(from_currency)
        target = self.rate(to_currency)
        return as_amount(amount, name="amount") * source / target

    def local_value(self, amount, currency: str, local_currency: str) -> Decimal:
        """Value of a currency amount in a country's local currency."""
        return self.convert(amount, currency, local_currency)

    def with_rates(self, rates: Mapping[str, object]) -> "FXMarket":
        merged = dict(self._rates)
        merged.update(rates)
        merged[self.numeraire] = Decimal(1)
        return FXMarket(self.numeraire, merged)

    def snapshot(self) -> dict[str, str]:
        return {currency: str(rate) for currency, rate in self._rates.items()}

    def __eq__(self, other) -> bool:
        return isinstance(other, FXMarket) and self.numeraire == other.numeraire and self._rates == other._rates

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"FXMarket(numeraire={self.numeraire!r}, rates={self.snapshot()!r})"


def fx_market(numeraire: str, **rates) -> FXMarket:
    """Convenience constructor: ``fx_market('AAA', BBB=2)``."""
    return FXMarket(numeraire, rates)
