"""Errors and exact-decimal validation for the international module."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


class InternationalError(ValueError):
    """Base class for all international-module errors."""


class FXError(InternationalError):
    """Invalid exchange rate, currency or conversion."""


class BalanceError(InternationalError):
    """An accounting/mirror identity failed."""


def as_amount(value, *, name: str = "amount", allow_zero: bool = True) -> Decimal:
    """Return a finite, non-negative Decimal.

    Booleans and non-finite values are rejected.  ``Decimal`` is used so that
    identities such as ``CA_A == -CA_B`` hold without binary-float noise.
    """
    if isinstance(value, bool):
        raise InternationalError(f"{name}: boolean is not an amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InternationalError(f"{name}: invalid amount {value!r}") from exc
    if not amount.is_finite():
        raise InternationalError(f"{name}: non-finite amount")
    if amount < 0:
        raise InternationalError(f"{name}: negative amount {amount}")
    if amount == 0 and not allow_zero:
        raise InternationalError(f"{name}: zero is not allowed")
    return amount


def as_signed(value, *, name: str = "amount") -> Decimal:
    """Return a finite Decimal of either sign (used for transactions/revaluation)."""
    if isinstance(value, bool):
        raise InternationalError(f"{name}: boolean is not an amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InternationalError(f"{name}: invalid amount {value!r}") from exc
    if not amount.is_finite():
        raise InternationalError(f"{name}: non-finite amount")
    return amount


def as_rate(value, *, name: str = "rate") -> Decimal:
    """Return a strictly positive Decimal exchange rate."""
    rate = as_signed(value, name=name)
    if rate <= 0:
        raise FXError(f"{name}: exchange rate must be positive")
    return rate
