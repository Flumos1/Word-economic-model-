"""Contract between the external-sector model and a future accounting core.

The single-country accounting core is developed in parallel (``src/amfe/accounts``).
The international module must not import an unfinished module, so the coupling is
the narrow protocol below.  The core can implement it, or an adapter can translate
its event vocabulary; ``TwoCountryWorld`` only calls these methods and never reads
back state from the hook.

The contract is deliberately *observational*: a hook receives the exact signed
claim change and its numeraire value at the transaction rate, and a revaluation
event at close.  It is the core's job to post the corresponding sector accounts;
if the hook rejects a post, it must raise, and the caller sees the error before it
closes the period.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExternalAccountsHook(Protocol):
    """Minimal bridge to the single-country accounting core."""

    def on_external_claim(
        self,
        *,
        holder: str,
        issuer: str,
        instrument: str,
        currency: str,
        amount: Decimal,
        numeraire_value: Decimal,
    ) -> None:
        """Post a signed change of a bilateral external claim.

        ``amount`` is in ``currency``: positive for acquisition/increase of the
        claim, negative for repayment.  ``numeraire_value`` is the same change at
        the transaction FX rate.  The mirror liability is the issuer's post.
        """

    def on_external_flow(
        self,
        *,
        kind: str,
        category: str,
        country: str,
        numeraire_value: Decimal,
    ) -> None:
        """Post a current/capital/financial flow in the common numeraire."""

    def on_revaluation(
        self,
        *,
        country: str,
        currency: str,
        amount: Decimal,
    ) -> None:
        """Post a close-period revaluation (not a transaction) for a country."""


@dataclass
class RecordingHook:
    """Test/diagnostic hook that records the calls in order."""

    claims: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    revaluations: list[dict] = field(default_factory=list)

    def on_external_claim(self, *, holder, issuer, instrument, currency, amount, numeraire_value):
        self.claims.append(
            {
                "holder": holder,
                "issuer": issuer,
                "instrument": instrument,
                "currency": currency,
                "amount": amount,
                "numeraire_value": numeraire_value,
            }
        )

    def on_external_flow(self, *, kind, category, country, numeraire_value):
        self.flows.append(
            {
                "kind": kind,
                "category": category,
                "country": country,
                "numeraire_value": numeraire_value,
            }
        )

    def on_revaluation(self, *, country, currency, amount):
        self.revaluations.append({"country": country, "currency": currency, "amount": amount})
