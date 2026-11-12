"""Two-country, two-currency external sector for AMFE-17 stage 3.

The package deliberately keeps the external accounts self-contained.  It does not
import or modify the single-country accounting core; the bridge to that core is a
small protocol (``amfe.international.integration.ExternalAccountsHook``) documented
in ``docs/78_Две_страны_и_валюты_этап3.md``.

Public API
----------
``Country``, ``FXMarket``, ``TwoCountryWorld``, ``ExternalAccountsHook``
and the error classes.  Amounts are :class:`decimal.Decimal` internally; exponents
and booleans are rejected so that financial values stay exact.
"""
from .errors import InternationalError, FXError, BalanceError
from .fx import FXMarket, fx_market
from .integration import ExternalAccountsHook, RecordingHook
from .model import Country, TwoCountryWorld

__all__ = [
    "BalanceError",
    "Country",
    "ExternalAccountsHook",
    "FXError",
    "FXMarket",
    "InternationalError",
    "RecordingHook",
    "TwoCountryWorld",
    "fx_market",
]
