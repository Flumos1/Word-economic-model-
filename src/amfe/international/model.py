"""Two-country external-sector model with balance of payments, IIP and FX revaluation.

Model scope
-----------
* Two countries A/B, each with its own currency.
* Goods/services trade, primary/secondary income and capital transfers are
  recorded bilaterally; every flow has an exact mirror in the other country.
* Every bilateral flow has a financial counterpart (a claim of one country on the
  other).  This keeps the balance-of-payments identity
  ``current + capital - financial = 0`` per country and ``CA_A = -CA_B``.
* External claims are stored in their currency of denomination.  A common
  numeraire (``FXMarket``) turns them into comparable values.  A change in the
  quote table between opening and closing produces revaluation, which is reported
  separately from transactions; the identity
  ``IIP_close = IIP_open + FA_transactions + revaluation`` holds in the numeraire.
* Observation discrepancies (mirror data) are stored separately and never enter
  the accounts.  Errors/exceptions are not imported as a residual.

The model is deliberately a closed two-country external sector, not a complete
SNA/BPM6 system.  See ``docs/78`` for the contract and remaining limitations.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .errors import BalanceError, InternationalError, as_amount, as_signed
from .fx import FXMarket

TRADE_CATEGORIES = ("goods", "services")
INCOME_CATEGORIES = ("primary_income", "secondary_income")
DIRECTIONS = ("acquire", "redeem")


@dataclass(frozen=True)
class Country:
    """A country with one local currency."""

    name: str
    currency: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise InternationalError("country name must be a non-empty string")
        if not isinstance(self.currency, str) or not self.currency:
            raise InternationalError("country currency must be a non-empty string")


def _empty_flow_table(countries: Iterable[str]) -> dict[str, dict[str, Decimal]]:
    return {country: {} for country in countries}


class TwoCountryWorld:
    """A two-country external sector in a common numeraire.

    Parameters
    ----------
    country_a, country_b:
        Distinct countries with distinct currencies.
    fx:
        Quote table against a common numeraire; must contain exactly the two
        country currencies (the model is explicitly two-currency).
    hook:
        Optional :class:`~amfe.international.integration.ExternalAccountsHook`
        adapter for the future accounting core.  It is observational only: the
        internal state remains authoritative and the hook cannot change it.
    """

    def __init__(self, country_a: Country, country_b: Country, fx: FXMarket, hook=None):
        if not isinstance(country_a, Country) or not isinstance(country_b, Country):
            raise InternationalError("country_a/country_b must be Country instances")
        if country_a.name == country_b.name:
            raise InternationalError("country names must be distinct")
        if country_a.currency == country_b.currency:
            raise InternationalError("the two countries must use different currencies")
        if not isinstance(fx, FXMarket):
            raise InternationalError("fx must be an FXMarket")
        expected = {country_a.currency, country_b.currency}
        if set(fx.currencies) != expected or len(fx.currencies) != 2:
            raise InternationalError(
                f"fx must contain exactly the two country currencies {sorted(expected)}"
            )
        self.country_a = country_a
        self.country_b = country_b
        self.countries = {country_a.name: country_a, country_b.name: country_b}
        self.fx = fx
        self.hook = hook
        self.period = 0
        self._claims: dict[tuple[str, str, str, str], Decimal] = {}
        self._opening_claims: dict[tuple[str, str, str, str], Decimal] = {}
        self._opening_fx = fx
        self._flows: list[dict] = []
        self._transactions: dict[tuple[str, str, str, str], dict] = {}
        self._discrepancies: list[dict] = []
        self.journal: list[tuple] = []

    # ------------------------------------------------------------------ lookup
    def country(self, name: str) -> Country:
        try:
            return self.countries[name]
        except KeyError as exc:
            raise InternationalError(f"unknown country {name!r}") from exc

    def _country(self, name: str) -> Country:
        return self.country(name)

    def _currency(self, currency: str) -> str:
        if currency not in {c.currency for c in self.countries.values()}:
            raise InternationalError(f"unknown currency {currency!r}")
        return currency

    def _instrument(self, instrument: str) -> str:
        if not isinstance(instrument, str) or not instrument:
            raise InternationalError("instrument must be a non-empty string")
        return instrument

    def _claim_key(self, holder: str, issuer: str, instrument: str, currency: str):
        self._country(holder)
        self._country(issuer)
        if holder == issuer:
            raise InternationalError("a country cannot hold a claim on itself")
        self._currency(currency)
        self._instrument(instrument)
        return (holder, issuer, instrument, currency)

    @property
    def claims(self) -> dict[tuple[str, str, str, str], Decimal]:
        """Copy of the current bilateral claims (positive amounts)."""
        return dict(self._claims)

    @property
    def discrepancies(self) -> list[dict]:
        return list(self._discrepancies)

    # -------------------------------------------------------------- primitives
    def _notify_claim(self, holder, issuer, instrument, currency, delta: Decimal, num_value: Decimal):
        if self.hook is not None:
            self.hook.on_external_claim(
                holder=holder,
                issuer=issuer,
                instrument=instrument,
                currency=currency,
                amount=delta,
                numeraire_value=num_value,
            )

    def _notify_flow(self, kind: str, category: str, country: str, value: Decimal):
        if self.hook is not None:
            self.hook.on_external_flow(
                kind=kind, category=category, country=country, numeraire_value=value
            )

    def _apply_claim(
        self,
        holder: str,
        issuer: str,
        instrument: str,
        currency: str,
        delta: Decimal,
        *,
        notify: bool = True,
    ) -> None:
        """Apply a signed claim change and record its transaction at current FX."""
        key = self._claim_key(holder, issuer, instrument, currency)
        current = self._claims.get(key, Decimal(0))
        new_amount = current + delta
        if new_amount < 0:
            raise BalanceError(
                f"claim {key} would become negative: {current} + {delta} < 0"
            )
        absolute = abs(delta)
        rate = self.fx.rate(currency)
        sign = Decimal(1) if delta >= 0 else Decimal(-1)
        num_value = sign * absolute * rate
        holder_local = sign * absolute * self.fx.local_value(Decimal(1), currency, self._country(holder).currency)
        issuer_local = sign * absolute * self.fx.local_value(Decimal(1), currency, self._country(issuer).currency)
        self._claims[key] = new_amount
        tx = self._transactions.setdefault(
            key,
            {"currency": Decimal(0), "numeraire": Decimal(0), "local": {holder: Decimal(0), issuer: Decimal(0)}},
        )
        tx["currency"] += delta
        tx["numeraire"] += num_value
        tx["local"][holder] += holder_local
        tx["local"][issuer] -= issuer_local
        if notify:
            self._notify_claim(holder, issuer, instrument, currency, delta, num_value)

    def add_initial_claim(self, holder: str, issuer: str, instrument: str, currency: str, amount) -> None:
        """Add an opening external claim without a transaction (a stock, not a flow)."""
        value = as_amount(amount, name="initial claim", allow_zero=False)
        key = self._claim_key(holder, issuer, instrument, currency)
        self._claims[key] = self._claims.get(key, Decimal(0)) + value
        self._opening_claims[key] = self._opening_claims.get(key, Decimal(0)) + value
        self._notify_claim(holder, issuer, instrument, currency, value, value * self.fx.rate(currency))
        self.journal.append(("opening_claim",) + key + (value,))

    def _record_flow(self, kind: str, category: str, values: dict[str, Decimal]) -> None:
        for country, value in values.items():
            if value == 0:
                continue
            self._flows.append({"kind": kind, "category": category, "country": country, "value": value})
            self._notify_flow(kind, category, country, value)

    # ------------------------------------------------------------- transactions
    def record_trade(
        self,
        exporter: str,
        importer: str,
        value,
        invoice_currency: str,
        *,
        category: str = "goods",
        instrument: str = "trade_credit",
    ) -> None:
        """Record a goods/services export from ``exporter`` to ``importer``.

        The flow is a current-account credit for the exporter and a debit for the
        importer.  It is settled by a bilateral trade-credit claim in the invoice
        currency, so the financial account has the same absolute value and the
        opposite sign.  ``value`` is the total transaction value (quantity × unit
        price); zero is an allowed no-op.
        """
        exporter_country = self._country(exporter)
        importer_country = self._country(importer)
        if exporter == importer:
            raise InternationalError("exporter and importer must differ")
        if category not in TRADE_CATEGORIES:
            raise InternationalError(f"unknown trade category {category!r}")
        self._currency(invoice_currency)
        amount = as_amount(value, name="trade value")
        if amount == 0:
            self.journal.append(("trade_zero", exporter, importer, category, invoice_currency))
            return
        num = self.fx.numeraire_value(amount, invoice_currency)
        self._apply_claim(exporter, importer, instrument, invoice_currency, amount)
        self._record_flow("CA", category, {exporter: num, importer: -num})
        self.journal.append(("trade", exporter, importer, amount, invoice_currency, category))

    def record_trade_quantity(
        self,
        exporter: str,
        importer: str,
        quantity,
        unit_price,
        invoice_currency: str,
        *,
        category: str = "goods",
    ) -> None:
        """Convenience wrapper: value = quantity × unit_price."""
        q = as_amount(quantity, name="quantity")
        p = as_amount(unit_price, name="unit price")
        self.record_trade(exporter, importer, q * p, invoice_currency, category=category)

    def record_income(
        self,
        receiver: str,
        payer: str,
        value,
        currency: str,
        *,
        category: str = "primary_income",
        instrument: str = "income_credit",
    ) -> None:
        """Primary or secondary income: a current-account flow with a claim."""
        receiver_country = self._country(receiver)
        self._country(payer)
        if receiver == payer:
            raise InternationalError("income payer and receiver must differ")
        if category not in INCOME_CATEGORIES:
            raise InternationalError(f"unknown income category {category!r}")
        self._currency(currency)
        amount = as_amount(value, name="income value")
        if amount == 0:
            self.journal.append(("income_zero", receiver, payer, category, currency))
            return
        num = self.fx.numeraire_value(amount, currency)
        self._apply_claim(receiver, payer, instrument, currency, amount)
        self._record_flow("CA", category, {receiver: num, payer: -num})
        self.journal.append(("income", receiver, payer, amount, currency, category))

    def record_capital_transfer(
        self,
        donor: str,
        receiver: str,
        value,
        currency: str,
        *,
        instrument: str = "capital_transfer_claim",
    ) -> None:
        """Capital transfer: donor debits the capital account, receiver credits it.

        The counterpart is a claim of the receiver on the donor (the donor's
        external liability), which keeps the BoP identity and mirror intact.
        """
        donor_country = self._country(donor)
        self._country(receiver)
        if donor == receiver:
            raise InternationalError("capital transfer parties must differ")
        self._currency(currency)
        amount = as_amount(value, name="capital transfer")
        if amount == 0:
            self.journal.append(("capital_transfer_zero", donor, receiver, currency))
            return
        num = self.fx.numeraire_value(amount, currency)
        self._apply_claim(receiver, donor, instrument, currency, amount)
        self._record_flow("KA", "capital_transfer", {donor: -num, receiver: num})
        self.journal.append(("capital_transfer", donor, receiver, amount, currency))

    def record_financial_transaction(
        self,
        holder: str,
        issuer: str,
        value,
        currency: str,
        *,
        instrument: str = "bond",
        direction: str = "acquire",
        settlement: str = "cash",
        settlement_instrument: str = "deposit",
    ) -> None:
        """Buy (``acquire``) or sell/repay (``redeem``) a bilateral external claim.

        A portfolio transaction is a *gross* financial-account event, not net
        lending.  In the two-country closed model it is settled with the other
        country's claim on the buyer/seller in the buyer's local currency: the
        numeraire financial-account effect nets to zero, while gross claims and
        liabilities change.  ``settlement="cash"`` is the only supported mode;
        a transaction without a settlement leg would leave a BoP residual and is
        rejected by the design (it belongs to a later, multi-instrument layer).
        """
        self._country(holder)
        self._country(issuer)
        if holder == issuer:
            raise InternationalError("holder and issuer must differ")
        if direction not in DIRECTIONS:
            raise InternationalError(f"unknown direction {direction!r}")
        if settlement != "cash":
            raise InternationalError(f"unsupported settlement {settlement!r}")
        self._currency(currency)
        amount = as_amount(value, name="financial transaction")
        self._instrument(instrument)
        self._instrument(settlement_instrument)
        if amount == 0:
            self.journal.append(("financial_zero", holder, issuer, instrument, currency, direction))
            return
        holder_currency = self._country(holder).currency
        settlement_amount = self.fx.convert(amount, currency, holder_currency)
        settlement_key = (issuer, holder, settlement_instrument, holder_currency)
        if direction == "acquire":
            self._apply_claim(holder, issuer, instrument, currency, amount)
            self._apply_claim(issuer, holder, settlement_instrument, holder_currency, settlement_amount)
            self.journal.append(("financial", "acquire", holder, issuer, amount, currency, instrument))
        else:
            if self._claims.get(settlement_key, Decimal(0)) < settlement_amount:
                raise BalanceError(
                    f"cannot settle redemption: {settlement_key} has "
                    f"{self._claims.get(settlement_key, Decimal(0))}, needs {settlement_amount}"
                )
            bond_key = (holder, issuer, instrument, currency)
            if self._claims.get(bond_key, Decimal(0)) < amount:
                raise BalanceError(f"cannot redeem {amount} of {bond_key}")
            self._apply_claim(holder, issuer, instrument, currency, -amount)
            self._apply_claim(issuer, holder, settlement_instrument, holder_currency, -settlement_amount)
            self.journal.append(("financial", "redeem", holder, issuer, amount, currency, instrument))

    # ------------------------------------------------------- observations/errors
    def record_observation(
        self,
        exporter: str,
        importer: str,
        exporter_reported_value,
        importer_reported_value,
        currency: str,
        *,
        category: str = "goods",
    ) -> Decimal:
        """Record mirrored source data without creating an economic flow.

        Returns the signed discrepancy ``exporter_reported - importer_reported``
        in the reporting currency.  Discrepancies are not errors and never enter
        the current/financial accounts; they are kept for a separate reconciliation
        line (net errors and omissions are *not* silently absorbed).
        """
        self._country(exporter)
        self._country(importer)
        if exporter == importer:
            raise InternationalError("observation parties must differ")
        if category not in TRADE_CATEGORIES:
            raise InternationalError(f"unknown observation category {category!r}")
        self._currency(currency)
        exp = as_amount(exporter_reported_value, name="exporter reported value")
        imp = as_amount(importer_reported_value, name="importer reported value")
        discrepancy = exp - imp
        num = self.fx.numeraire_value(abs(discrepancy), currency)
        signed_num = num if discrepancy >= 0 else -num
        record = {
            "exporter": exporter,
            "importer": importer,
            "category": category,
            "currency": currency,
            "exporter_reported": exp,
            "importer_reported": imp,
            "discrepancy": discrepancy,
            "numeraire_discrepancy": signed_num,
        }
        self._discrepancies.append(record)
        self.journal.append(("observation", exporter, importer, exp, imp, currency, category))
        return discrepancy

    @property
    def observation_discrepancy_numeraire(self) -> Decimal:
        return sum((d["numeraire_discrepancy"] for d in self._discrepancies), Decimal(0))

    # ------------------------------------------------------------------- close
    def _validate_closing_fx(self, fx: FXMarket) -> None:
        expected = {self.country_a.currency, self.country_b.currency}
        if not isinstance(fx, FXMarket) or set(fx.currencies) != expected or len(fx.currencies) != 2:
            raise InternationalError("closing fx must contain exactly the two country currencies")

    def _converted(self, amount: Decimal, currency: str, fx: FXMarket) -> Decimal:
        return amount * fx.rate(currency)

    def close_period(self, fx: FXMarket | None = None) -> dict:
        """Close the period, produce BoP/IIP/revaluation and carry stocks forward."""
        closing_fx = self.fx if fx is None else fx
        self._validate_closing_fx(closing_fx)
        opening_fx = self._opening_fx
        for currency in (self.country_a.currency, self.country_b.currency):
            opening_fx.rate(currency)
            closing_fx.rate(currency)

        names = [self.country_a.name, self.country_b.name]
        ca = _empty_flow_table(names)
        ka = _empty_flow_table(names)
        for flow in self._flows:
            table = ca if flow["kind"] == "CA" else ka
            table[flow["country"]][flow["category"]] = (
                table[flow["country"]].get(flow["category"], Decimal(0)) + flow["value"]
            )

        keys = set(self._opening_claims) | set(self._claims) | set(self._transactions)
        assets_open = {n: Decimal(0) for n in names}
        assets_close = {n: Decimal(0) for n in names}
        liabilities_open = {n: Decimal(0) for n in names}
        liabilities_close = {n: Decimal(0) for n in names}
        fa = {n: Decimal(0) for n in names}
        revaluation = {n: Decimal(0) for n in names}
        revaluation_local = {n: Decimal(0) for n in names}

        for key in sorted(keys):
            holder, issuer, _instrument, currency = key
            opening_amount = self._opening_claims.get(key, Decimal(0))
            closing_amount = self._claims.get(key, Decimal(0))
            tx = self._transactions.get(key)
            tx_num = tx["numeraire"] if tx else Decimal(0)
            same_currency = opening_fx.rate(currency)
            close_currency = closing_fx.rate(currency)
            value_open = opening_amount * same_currency
            value_close = closing_amount * close_currency
            assets_open[holder] += value_open
            assets_close[holder] += value_close
            liabilities_open[issuer] += value_open
            liabilities_close[issuer] += value_close
            if tx:
                fa[holder] += tx_num
                fa[issuer] -= tx_num

            holder_local = self._country(holder).currency
            issuer_local = self._country(issuer).currency
            opening_holder_local = opening_amount * same_currency / opening_fx.rate(holder_local)
            closing_holder_local = closing_amount * close_currency / closing_fx.rate(holder_local)
            opening_issuer_local = opening_amount * same_currency / opening_fx.rate(issuer_local)
            closing_issuer_local = closing_amount * close_currency / closing_fx.rate(issuer_local)
            tx_holder_local = tx["local"][holder] if tx else Decimal(0)
            tx_issuer_local = tx["local"][issuer] if tx else Decimal(0)
            revaluation[holder] += value_close - value_open - tx_num
            revaluation[issuer] -= value_close - value_open - tx_num
            revaluation_local[holder] += closing_holder_local - opening_holder_local - tx_holder_local
            revaluation_local[issuer] += -closing_issuer_local + opening_issuer_local - tx_issuer_local

        iip_open = {n: assets_open[n] - liabilities_open[n] for n in names}
        iip_close = {n: assets_close[n] - liabilities_close[n] for n in names}
        current = {n: sum(ca[n].values(), Decimal(0)) for n in names}
        capital = {n: sum(ka[n].values(), Decimal(0)) for n in names}
        net_lending = {n: current[n] + capital[n] for n in names}
        residual = {n: net_lending[n] - fa[n] for n in names}
        identity = {
            n: iip_close[n] - iip_open[n] - fa[n] - revaluation[n] for n in names
        }

        checks = {
            "current_account_mirror": current[names[0]] + current[names[1]] == 0,
            "capital_account_mirror": capital[names[0]] + capital[names[1]] == 0,
            "financial_account_mirror": fa[names[0]] + fa[names[1]] == 0,
            "iip_mirror": iip_close[names[0]] + iip_close[names[1]] == 0,
            "bop_identity": all(residual[n] == 0 for n in names),
            "iip_identity": all(identity[n] == 0 for n in names),
            "nonnegative_claims": all(v >= 0 for v in self._claims.values()),
        }
        checks["ok"] = all(checks.values())

        if self.hook is not None:
            for name in names:
                if revaluation[name] != 0 or revaluation_local[name] != 0:
                    self.hook.on_revaluation(
                        country=name,
                        currency=self._country(name).currency,
                        amount=revaluation_local[name],
                    )

        report = {
            "period": self.period,
            "numeraire": closing_fx.numeraire,
            "opening_rates": opening_fx.snapshot(),
            "closing_rates": closing_fx.snapshot(),
            "countries": {
                n: {
                    "currency": self._country(n).currency,
                    "current_account": dict(ca[n]),
                    "current_account_total": current[n],
                    "capital_account": dict(ka[n]),
                    "capital_account_total": capital[n],
                    "financial_account": fa[n],
                    "net_lending": net_lending[n],
                    "bop_residual": residual[n],
                    "iip": {
                        "assets": assets_close[n],
                        "liabilities": liabilities_close[n],
                        "net": iip_close[n],
                        "opening_net": iip_open[n],
                        "revaluation": revaluation[n],
                        "revaluation_local": revaluation_local[n],
                        "transactions": fa[n],
                    },
                }
                for n in names
            },
            "mirror": {
                "current_account": current[names[0]] + current[names[1]],
                "capital_account": capital[names[0]] + capital[names[1]],
                "financial_account": fa[names[0]] + fa[names[1]],
                "iip": iip_close[names[0]] + iip_close[names[1]],
            },
            "discrepancies": list(self._discrepancies),
            "checks": checks,
        }
        if not checks["ok"]:
            raise BalanceError(f"closed-period checks failed: {report['checks']}")

        self._opening_claims = dict(self._claims)
        self._opening_fx = closing_fx
        self.fx = closing_fx
        self._flows = []
        self._transactions = {}
        self._discrepancies = []
        self.period += 1
        self.journal.append(("close_period", report["period"] - 1))
        return report

    # ----------------------------------------------------------------- checks
    def verify(self) -> bool:
        """Non-destructive mirror check on the current stocks (no close)."""
        names = [self.country_a.name, self.country_b.name]
        iip = {n: Decimal(0) for n in names}
        for (holder, issuer, _instrument, currency), amount in self._claims.items():
            value = amount * self.fx.rate(currency)
            iip[holder] += value
            iip[issuer] -= value
        if iip[names[0]] + iip[names[1]] != 0:
            raise BalanceError(f"external positions do not mirror: {iip}")
        if any(v < 0 for v in self._claims.values()):
            raise BalanceError("negative claim stock")
        return True
