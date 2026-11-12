"""Open-economy world core: external sector posted into the domestic accounts (stage 3).

This module removes the main stub of :mod:`amfe.world.model` (docs/85 §6): previously the
external trade flows were only observed through an ``ExternalAccountsHook`` and reconciled
at the national-wealth level.  Here each country keeps an
:class:`amfe.accounts.open.OpenAccountSystem` with a rest-of-world (``row``) sector, and the
external events are **posted** into those books:

* :class:`OpenPostingHook` implements :class:`amfe.international.ExternalAccountsHook`.
  When ``TwoCountryWorld.record_trade`` fires, the hook converts the numeraire values to
  the local currency and posts an export transaction in the exporter's book and an import
  transaction in the importer's book (goods, ROW current flows and mirrored external
  claims/liabilities);
* before the external close, the core computes the FX revaluation of every bilateral
  claim in both countries and hands it to the hook as *pending* revaluations; the hook
  posts them as non-transaction revaluations when ``TwoCountryWorld.close_period`` fires
  its revaluation events.  FX translation is therefore inside the accounts, not outside.

Cycle of one period (differences from the closed core are marked):

1. decisions (unchanged: inflation, expectations, Taylor rule, regime);
2. production (unchanged, physical layer);
3. domestic phase A - working loan, production, wages;
4. **external trade** - two balanced/unbalanced bilateral trade credits are recorded;
   the hook posts exports and imports into both open account books;
5. domestic phase B - transfers, household and government purchases of *domestic*
   goods (``C_dom = C_total - imports``), taxes, interest, public-debt financing,
   loan repayment; then **FX revaluation** and the external close (the hook posts the
   revaluation while the books are still open), then the accounts close;
6. invariants - physical, accounts (open GDP), government budget, debt identity,
   NFA/IPP/BoP reconciliation, two-book external mirroring in the numeraire, world wealth;
7. period report (the closed report plus the ``open_sfc`` block).

The closed :class:`~amfe.world.model.WorldCore` is untouched; its tests remain green.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from amfe.accounts import (
    AccountingError,
    FinancialInstrument,
    OpeningBalances,
    Sector,
)
from amfe.accounts.invariants import check_external_mirroring
from amfe.accounts.model import BalanceSheet, Snapshot, to_decimal
from amfe.accounts.open import (
    EXTERNAL_CLAIM,
    EXTERNAL_LIABILITY,
    EXPORTS,
    IMPORTS,
    OpenAccountSystem,
    ROW,
)
from amfe.dynamics import (
    AdaptiveExpectations,
    InflationTargetRegime,
    MarkovSwitchingModel,
    NewKeynesianDeterminacySystem,
    TaylorRule,
)
from amfe.international import Country, FXMarket, TwoCountryWorld
from amfe.resources import Scenario as PhysicalScenario

from .config import CountryConfig, WorldConfig, WorldConfigError
from .integration import (
    WorldError,
    accounts_checks,
    debt_checks,
    external_checks,
    is_hook,
    merge_checks,
    money,
    physical_checks,
)
from .model import (
    _ZERO,
    _CountryRuntime,
    WorldCore,
    _clip,
    _finite_float_list,
    _q,
    _share_decimal,
)
from .physical import PhysicalOutcome
from .report import CountryPeriodReport, WorldPeriodReport, WorldRunResult, report_digest
from .scenarios import WorldScenario, baseline_scenario

_TOLERANCE = Decimal("1e-9")
_REVAL_TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# Hook: posts the external events into the open account books
# ---------------------------------------------------------------------------
class OpenPostingHook:
    """``ExternalAccountsHook`` that actually posts the events into the open books.

    The closed world-core uses an observational recorder; this hook is the stage-3
    integration point: the exporter's book gets the export transaction, the importer's
    book gets the import transaction, and the FX revaluation events are posted as the
    driver of ``fx_revaluation``.  All events are also recorded for the period report.
    """

    def __init__(self, books: Mapping[str, OpenAccountSystem],
                 currencies: Mapping[str, str],
                 fx_provider,
                 extra: object | None = None):
        self.books = dict(books)
        self.currencies = dict(currencies)
        self._fx_provider = fx_provider
        self.extra = extra
        self.claims: list[dict] = []
        self.flows: list[dict] = []
        self.revaluations: list[dict] = []
        self.claim_events: list[dict] = []
        self.import_local: dict[str, Decimal] = {}
        self.export_local: dict[str, Decimal] = {}
        self.pending: dict[str, tuple[dict, dict]] = {}
        self.posted_revaluations: list[dict] = []

    # -- infrastructure -------------------------------------------------
    def reset(self) -> None:
        self.claims.clear()
        self.flows.clear()
        self.revaluations.clear()
        self.claim_events.clear()
        self.import_local.clear()
        self.export_local.clear()
        self.pending.clear()
        self.posted_revaluations.clear()

    def begin_period(self) -> None:
        """Reset the period-level buffers (the event lists are append-only)."""
        self.claim_events.clear()
        self.import_local.clear()
        self.export_local.clear()
        self.pending.clear()
        self.posted_revaluations.clear()

    def _rate(self, currency: str) -> Decimal:
        return self._fx_provider().rate(currency)

    def _book(self, country: str) -> OpenAccountSystem:
        try:
            return self.books[country]
        except KeyError as exc:
            raise WorldError(f"the hook has no account book for {country!r}") from exc

    # -- protocol --------------------------------------------------------
    def on_external_claim(self, *, holder, issuer, instrument, currency,
                          amount: Decimal, numeraire_value: Decimal) -> None:
        entry = {
            "holder": holder, "issuer": issuer, "instrument": instrument,
            "currency": currency, "amount": amount,
            "numeraire_value": numeraire_value,
        }
        self.claims.append(entry)
        holder_currency = self.currencies[holder]
        issuer_currency = self.currencies[issuer]
        self.claim_events.append({
            **entry,
            "holder_local": _q(numeraire_value / self._rate(holder_currency)),
            "issuer_local": _q(-numeraire_value / self._rate(issuer_currency)),
        })
        if self.extra is not None:
            self.extra.on_external_claim(holder=holder, issuer=issuer,
                                         instrument=instrument, currency=currency,
                                         amount=amount, numeraire_value=numeraire_value)

    def on_external_flow(self, *, kind, category, country, numeraire_value: Decimal) -> None:
        entry = {"kind": kind, "category": category, "country": country,
                 "numeraire_value": numeraire_value}
        self.flows.append(entry)
        if self.extra is not None:
            self.extra.on_external_flow(kind=kind, category=category, country=country,
                                        numeraire_value=numeraire_value)
        if kind != "CA":
            raise WorldError(
                "the open SFC prototype posts only current-account external flows; "
                f"got kind={kind!r} (capital transfers are not implemented)")
        if numeraire_value == 0:
            return
        local = _q(abs(numeraire_value) / self._rate(self.currencies[country]))
        book = self._book(country)
        try:
            if numeraire_value > 0:      # this country is the exporter
                book.export_goods(local)
                self.export_local[country] = self.export_local.get(country, _ZERO) + local
            else:                        # this country is the importer
                book.import_goods(local)
                self.import_local[country] = self.import_local.get(country, _ZERO) + local
        except AccountingError as exc:
            raise WorldError(
                f"posting the external trade of {country} broke the accounts: {exc}") from exc

    def on_revaluation(self, *, country, currency, amount: Decimal) -> None:
        entry = {"country": country, "currency": currency, "amount": amount}
        self.revaluations.append(entry)
        if self.extra is not None:
            self.extra.on_revaluation(country=country, currency=currency, amount=amount)
        pending = self.pending.get(country)
        if pending is None:
            if abs(amount) <= _TOLERANCE:
                return
            raise WorldError(
                f"FX revaluation of {amount} for {country} has no prepared posting")
        claim_changes, liability_changes = pending
        expected = sum(claim_changes.values(), _ZERO) + sum(liability_changes.values(), _ZERO)
        if abs(expected - amount) > max(_TOLERANCE, _TOLERANCE * abs(amount)):
            raise WorldError(
                f"FX revaluation mismatch for {country}: prepared {expected}, "
                f"external layer reported {amount}")
        try:
            entries = self.books[country].fx_revaluation(
                claim_changes=claim_changes, liability_changes=liability_changes)
        except AccountingError as exc:
            raise WorldError(
                f"posting the FX revaluation of {country} broke the accounts: {exc}") from exc
        self.posted_revaluations.append({
            "country": country, "external_amount": amount,
            "posted_amount": expected, "entries": len(entries)})

    # -- reporting -------------------------------------------------------
    def summary(self, start_claim: int = 0, start_flow: int = 0,
                start_revaluation: int = 0) -> dict:
        claims = self.claims[start_claim:]
        flows = self.flows[start_flow:]
        revaluations = self.revaluations[start_revaluation:]
        return {
            "claims": len(claims),
            "claim_numeraire_by_holder": {
                holder: str(sum((entry["numeraire_value"] for entry in claims
                                 if entry["holder"] == holder), _ZERO))
                for holder in sorted({entry["holder"] for entry in claims})},
            "flows": len(flows),
            "flow_numeraire_by_country": {
                country: str(sum((entry["numeraire_value"] for entry in flows
                                  if entry["country"] == country), _ZERO))
                for country in sorted({entry["country"] for entry in flows})},
            "revaluations": [
                {"country": entry["country"], "currency": entry["currency"],
                 "amount": str(entry["amount"])}
                for entry in revaluations],
            "posted_revaluations": [
                {"country": item["country"],
                 "external_amount": str(item["external_amount"]),
                 "posted_amount": str(item["posted_amount"])}
                for item in self.posted_revaluations],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valued_snapshot(book: OpenAccountSystem, rate: Decimal) -> Snapshot:
    """The book's snapshot translated into the common numeraire at ``rate``."""
    source = book.snapshot()
    balances = {}
    for sector, sheet in source.balances.items():
        balances[sector] = BalanceSheet(
            sector=sector,
            financial={code: value * rate for code, value in sheet.financial.items()},
            real={code: value * rate for code, value in sheet.real.items()},
        )
    return Snapshot(period=f"{source.period}-valued", balances=balances)


def _residual(value: Decimal) -> Decimal:
    return value if value >= 0 else -value


class OpenWorldCore(WorldCore):
    """Two-country world core whose external sector is posted into open SFC books."""

    def __init__(self, config: WorldConfig | None = None,
                 scenario: WorldScenario | None = None, *,
                 external_hook: object | None = None):
        super().__init__(config, scenario, external_hook=external_hook)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _initialize(self) -> None:
        config = self.config
        self._physical: dict[str, object] = {}
        self._runtimes: dict[str, _CountryRuntime] = {}
        self._baseline_va: dict[str, float] = {}
        initial_rates = {country.currency: country.initial_fx_rate
                         for country in config.countries}
        try:
            self._fx = FXMarket(config.numeraire, initial_rates)
        except Exception as exc:
            raise WorldConfigError(f"invalid initial FX market: {exc}") from exc

        books: dict[str, OpenAccountSystem] = {}
        openings: dict[str, OpeningBalances] = {}
        initials: dict[str, dict] = {}
        for country in config.countries:
            try:
                from .physical import CountryPhysical
                physical = CountryPhysical(country.physical)
            except WorldConfigError:
                raise
            except Exception as exc:
                raise WorldConfigError(
                    f"country {country.name}: physical layer rejected the configuration: "
                    f"{exc}") from exc
            self._physical[country.name] = physical
            baseline = physical.baseline
            if baseline.value_added <= 0.0:
                raise WorldConfigError(
                    f"country {country.name}: baseline physical value added must be positive")
            self._baseline_va[country.name] = baseline.value_added
            initial_gdp = _q(_share_decimal(country.initial_price_level)
                             * money(baseline.value_added))
            debt = _q(_share_decimal(country.initial_debt_ratio) * initial_gdp)
            loan = _q(_share_decimal(country.working_loan_ratio) * initial_gdp)
            opening = OpeningBalances(financial={
                (Sector.BANK, FinancialInstrument.BOND): debt,
                (Sector.GOVERNMENT, FinancialInstrument.BOND): -debt,
                (Sector.BANK, FinancialInstrument.LOAN): loan,
                (Sector.FIRM, FinancialInstrument.LOAN): -loan,
            })
            try:
                book = OpenAccountSystem(period="P1", opening=opening)
            except AccountingError as exc:  # pragma: no cover - configuration dependent
                raise WorldConfigError(
                    f"country {country.name}: invalid initial stocks: {exc}") from exc
            books[country.name] = book
            openings[country.name] = opening
            initials[country.name] = {"gdp": initial_gdp, "debt": debt, "loan": loan}
            # store physical for the runtime construction below
            self._physical[country.name] = physical

        currencies = {country.name: country.currency for country in config.countries}
        self._open_books = books
        self._hook = OpenPostingHook(books, currencies, lambda: self._fx,
                                     extra=self._provided_hook)
        try:
            self._external = TwoCountryWorld(
                Country(config.country_a.name, config.country_a.currency),
                Country(config.country_b.name, config.country_b.currency),
                self._fx, hook=self._hook)
        except Exception as exc:
            raise WorldConfigError(f"cannot build the external layer: {exc}") from exc

        for country in config.countries:
            physical = self._physical[country.name]
            initial = initials[country.name]
            policy = TaylorRule(r_star=country.policy_r_star,
                                pi_target=country.inflation_target,
                                phi_pi=country.policy_phi_pi,
                                phi_y=country.policy_phi_y)
            expectations = AdaptiveExpectations(
                gain=country.expectations_gain, initial=country.inflation_target)
            inflation_regime = InflationTargetRegime(
                target=country.inflation_target,
                source="open world-core prototype assumption (docs/93)")
            determinacy = NewKeynesianDeterminacySystem(rule=policy).determinacy()
            self._runtimes[country.name] = _CountryRuntime(
                config=country,
                physical=physical,
                accounts=books[country.name],
                policy=policy,
                expectations=expectations,
                inflation_regime=inflation_regime,
                determinacy=determinacy,
                expected_inflation=country.inflation_target,
                price_level=_share_decimal(country.initial_price_level),
                previous_gap=0.0,
                previous_policy_rate=0.0,
                previous_nominal_gdp=initial["gdp"],
                previous_debt=initial["debt"],
                previous_cash=_ZERO,
                previous_loan=initial["loan"],
                previous_net_worth=_ZERO,
                previous_rate=self._fx.rate(country.currency),
                previous_nfa=_ZERO,
            )

        regime = MarkovSwitchingModel(config.transition_matrix,
                                      config.regime_means, config.regime_sigmas,
                                      seed=config.regime_seed)
        self._regime_model = regime
        simulation = regime.simulate(config.max_periods, seed=config.regime_seed)
        self._regime_states = [int(state) for state in simulation["states"]]
        self._regime_stationary = _finite_float_list(regime.stationary_distribution())
        self._regime_durations = _finite_float_list(regime.expected_durations())
        self._period_index = 0
        self._reports: list[WorldPeriodReport] = []

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------
    @property
    def hook(self) -> OpenPostingHook:
        return self._hook

    def accounts(self, country: str) -> OpenAccountSystem:
        return self._runtime(country).accounts

    def open_books(self) -> Mapping[str, OpenAccountSystem]:
        return dict(self._open_books)

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------
    def step(self) -> WorldPeriodReport:
        config = self.config
        scenario = self.scenario
        period = self._period_index
        if period >= config.max_periods:
            raise WorldError(
                f"the configured horizon max_periods={config.max_periods} is exhausted")
        regime_state = int(self._regime_states[period])
        regime_factor = 1.0 if regime_state == 0 else config.regime_contraction_factor

        hook: OpenPostingHook = self._hook
        claim_start = len(hook.claims)
        flow_start = len(hook.flows)
        revaluation_start = len(hook.revaluations)
        opening_claims = dict(self._external.claims)
        opening_fx = self._fx
        opening_nfa = {name: books.nfa() for name, books in self._open_books.items()}
        hook.begin_period()

        # 1-3: decisions, production and the first part of the domestic monetary block.
        states: dict[str, dict] = {}
        for country in config.countries:
            runtime = self._runtime(country.name)
            accounts = runtime.accounts
            if not accounts.period_open:
                accounts.begin_period(f"P{period + 1}")
            decisions = self._decide(country, runtime, scenario, regime_factor, period)
            physical = self._produce(country, runtime, scenario, regime_factor, period)
            state = self._prepare_domestic(country, runtime, accounts, decisions, physical)
            state["output_gap"] = (physical.value_added
                                   / decisions["potential_output"] - 1.0)
            state["rate"] = self._fx.rate(country.currency)
            states[country.name] = state

        # 4: external trade - the hook posts exports and imports into both books.
        rate_a = self._fx.rate(config.country_a.currency)
        rate_b = self._fx.rate(config.country_b.currency)
        export_a = _q(_share_decimal(config.country_a.export_share)
                      * states[config.country_a.name]["nominal_gdp"])
        export_b = _q(_share_decimal(config.country_b.export_share)
                      * states[config.country_b.name]["nominal_gdp"])
        states[config.country_a.name]["exports_local"] = export_a
        states[config.country_b.name]["exports_local"] = export_b
        states[config.country_a.name]["exports_numeraire"] = _q(export_a * rate_a)
        states[config.country_b.name]["exports_numeraire"] = _q(export_b * rate_b)
        try:
            self._external.record_trade(
                config.country_a.name, config.country_b.name, export_a,
                config.country_a.currency, category="goods")
            self._external.record_trade(
                config.country_b.name, config.country_a.name, export_b,
                config.country_b.currency, category="goods")
        except WorldError:
            raise
        except Exception as exc:
            raise WorldError(
                f"external trade failed in period {period + 1}: {exc}") from exc

        # 5a: finish the domestic block with the posted import values.
        for country in config.countries:
            state = states[country.name]
            imports_local = hook.import_local.get(country.name, _ZERO)
            state["imports_local"] = imports_local
            state["imports_numeraire"] = _q(imports_local
                                            * self._fx.rate(country.currency))
            state["net_exports_local"] = (state["exports_local"] - imports_local)
            state["bop_current_account_local"] = state["net_exports_local"]
            state["bop_financial_account_local"] = state["net_exports_local"]
            self._finish_domestic(country, self._runtime(country.name), state)

        # 5b: FX revaluation is prepared as data, then the external close fires the
        # hook which posts it while the account books are still open.
        closing_rate = self._fx_rate_for_period(period, scenario)
        closing_fx = self._fx
        if closing_rate is not None:
            non_numeraire = (config.country_b if config.country_b.currency
                             != config.numeraire else config.country_a)
            closing_fx = self._fx.with_rates({non_numeraire.currency: closing_rate})
        pending = self._prepare_fx_revaluation(opening_claims, opening_fx,
                                               closing_fx, hook.claim_events)
        hook.pending = pending
        try:
            external_report = self._external.close_period(closing_fx)
        except WorldError:
            raise
        except Exception as exc:
            raise WorldError(
                f"external close failed in period {period + 1}: {exc}") from exc
        self._fx = self._external.fx
        for country in config.countries:
            payload = external_report["countries"][country.name]
            states[country.name]["external"] = payload
            states[country.name]["closing_rate"] = self._fx.rate(country.currency)
            states[country.name]["nfa_numeraire"] = payload["iip"]["net"]
            states[country.name]["iip_revaluation_numeraire"] = payload["iip"]["revaluation"]
            states[country.name]["iip_transactions_numeraire"] = payload["iip"]["transactions"]

        # 5c: close the open account books (the revaluations are already posted).
        for country in config.countries:
            runtime = self._runtime(country.name)
            state = states[country.name]
            try:
                report = runtime.accounts.close_period()
            except AccountingError as exc:
                raise WorldError(
                    f"country {country.name}: the open monetary block broke the accounting "
                    f"invariants in period {period + 1}: {exc}") from exc
            state["accounts_report"] = report
            state["open_gdp_expenditure"] = report.open_gdp_expenditure
            state["nfa_local"] = report.nfa
            state["external_assets_local"] = report.external_assets
            state["external_liabilities_local"] = report.external_liabilities
            state["row_net_worth_local"] = report.row_net_worth
            state["net_worth_local"] = report.net_worth_residents
            state["investment"] = (report.inventory_change.get(
                Sector.FIRM.value, _ZERO)
                + report.gross_fixed_capital_formation.get(Sector.FIRM.value, _ZERO))
            state["household_saving"] = (state["household_income"]
                                         - state["consumption"])
            state["primary_balance"] = (state["taxes"]
                                        - state["government_consumption"]
                                        - state["transfers"])
            state["debt"] = -runtime.accounts.balance(
                Sector.GOVERNMENT, FinancialInstrument.BOND)
            state["cash"] = runtime.accounts.balance(
                Sector.GOVERNMENT, FinancialInstrument.DEPOSIT)
            state["loan"] = -runtime.accounts.balance(
                Sector.FIRM, FinancialInstrument.LOAN)
            state["debt_ratio"] = (float(state["debt"] / state["nominal_gdp"])
                                   if state["nominal_gdp"] > 0 else float("nan"))
            state["cash_change"] = state["cash"] - state["cash_open"]
            state["expected_inflation_next"] = runtime.expectations.update(
                state["inflation"])

        # 6: per-country and world-level invariants.
        world_checks: dict[str, bool] = {}
        world_residuals: dict[str, float] = {}
        country_reports: dict[str, CountryPeriodReport] = {}
        for country in config.countries:
            runtime = self._runtime(country.name)
            state = states[country.name]
            checks, residuals = self._open_country_checks(
                country, runtime, state, opening_nfa, external_report)
            prefix = f"{country.name}."
            world_checks.update({prefix + key: value for key, value in checks.items()})
            world_residuals.update({prefix + key: value for key, value in residuals.items()})
            country_reports[country.name] = self._build_open_country_report(
                country, state, checks, residuals, external_report)

        open_world_checks, open_world_residuals = self._open_world_checks(
            states, external_report, opening_nfa)
        world_checks.update(open_world_checks)
        world_residuals.update(open_world_residuals)

        hook_claims = hook.claims[claim_start:]
        hook_flows = hook.flows[flow_start:]
        hook_revaluations = hook.revaluations[revaluation_start:]
        expected_trade = {
            config.country_a.name: export_a * rate_a,
            config.country_b.name: export_b * rate_b,
        }
        external_layer_checks, external_layer_residuals = external_checks(
            external_report, hook_claims, hook_flows, hook_revaluations, expected_trade)
        world_checks.update(external_layer_checks)
        world_residuals.update(external_layer_residuals)

        dynamics = self._dynamics_summary(regime_state, regime_factor)
        report = WorldPeriodReport(
            period=period,
            scenario=self.scenario.name,
            fx=self._external.fx.snapshot(),
            countries=country_reports,
            external={
                "numeraire": external_report["numeraire"],
                "opening_rates": external_report["opening_rates"],
                "closing_rates": external_report["closing_rates"],
                "mirror": external_report["mirror"],
                "checks": external_report["checks"],
                "hook": hook.summary(claim_start, flow_start, revaluation_start),
                "open_sfc": {
                    "two_book_mirror": open_world_checks.get(
                        "open_two_book_mirror", False),
                    "nfa_mirror": open_world_checks.get("open_nfa_mirror", False),
                    "world_wealth": open_world_checks.get(
                        "open_world_wealth", False),
                    "posted_revaluations": list(hook.posted_revaluations),
                },
            },
            dynamics=dynamics,
            checks=world_checks,
            residuals=world_residuals,
        )
        if not report.ok:
            failed = sorted(name for name, value in world_checks.items() if not value)
            raise WorldError(f"open world-core invariants failed: {failed}")

        # 7: carry the state.
        for country in config.countries:
            runtime = self._runtime(country.name)
            state = states[country.name]
            runtime.expected_inflation = state["expected_inflation_next"]
            runtime.price_level = state["price_level"]
            runtime.previous_gap = state["output_gap"]
            runtime.previous_policy_rate = state["policy_rate"]
            runtime.previous_nominal_gdp = state["nominal_gdp"]
            runtime.previous_debt = state["debt"]
            runtime.previous_cash = state["cash"]
            runtime.previous_loan = state["loan"]
            runtime.previous_net_worth = state["net_worth_local"]
            runtime.previous_rate = state["closing_rate"]
            runtime.previous_nfa = state["nfa_local"]
        self._period_index += 1
        self._reports.append(report)
        return report

    # ------------------------------------------------------------------
    # Cycle sub-steps
    # ------------------------------------------------------------------
    def _prepare_domestic(self, country: CountryConfig, runtime: _CountryRuntime,
                          accounts: OpenAccountSystem, decisions: dict,
                          physical: PhysicalOutcome) -> dict:
        """Decisions -> producer income, wages and the working-capital loan."""
        if physical.value_added <= 0.0:
            raise WorldError(
                f"country {country.name}: period {self._period_index + 1} has no "
                "monetisable physical output")
        price = decisions["price_level"]
        nominal_gdp = _q(price * money(physical.value_added))
        consumption = _q(nominal_gdp
                         * _share_decimal(country.physical.household_demand_share))
        government = _q(nominal_gdp
                        * _share_decimal(country.physical.government_demand_share))
        planned_investment = nominal_gdp - consumption - government
        wages = _q(nominal_gdp * _share_decimal(country.wage_share))
        transfers = _q(nominal_gdp * _share_decimal(country.transfer_rate))
        debt_open = -accounts.balance(Sector.GOVERNMENT, FinancialInstrument.BOND)
        cash_open = accounts.balance(Sector.GOVERNMENT, FinancialInstrument.DEPOSIT)
        loan_open = -accounts.balance(Sector.FIRM, FinancialInstrument.LOAN)
        firm_cash_open = accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT)
        policy_fraction = _share_decimal(decisions["policy_rate"]) / Decimal(100)
        interest_government = _q(policy_fraction * debt_open)
        interest_firm = _q(policy_fraction * loan_open)
        previous_gdp = runtime.previous_nominal_gdp
        debt_ratio_previous = (float(debt_open / previous_gdp)
                               if previous_gdp > 0 else country.initial_debt_ratio)
        from amfe.dynamics import stabilizing_primary_balance  # local import: tiny layer
        growth_expectation = (country.real_growth + decisions["inflation"] / 100.0)
        stabilising = stabilizing_primary_balance(
            debt_ratio_previous, float(policy_fraction), growth_expectation)
        primary_target = stabilising + country.debt_response * (
            debt_ratio_previous - country.debt_target)
        tax_target = government + transfers + _q(
            _share_decimal(primary_target) * nominal_gdp)
        taxes = min(max(tax_target, _q(_share_decimal(country.tax_min) * nominal_gdp)),
                    _q(_share_decimal(country.tax_max) * nominal_gdp))
        liquidity_need = wages + taxes + interest_firm - firm_cash_open - (
            consumption + government)
        draw = max(_ZERO, liquidity_need, wages - firm_cash_open)
        gross_bond_issue = max(_ZERO, government + transfers
                               + interest_government - cash_open)
        try:
            if draw > 0:
                accounts.loan(draw, memo=f"working capital, period {self._period_index + 1}")
            accounts.produce(nominal_gdp, memo="physical value added")
            accounts.pay_wages(wages)
        except AccountingError as exc:
            raise WorldError(
                f"country {country.name}: the domestic block broke the accounting "
                f"invariants: {exc}") from exc
        return {
            "nominal_gdp": nominal_gdp,
            "consumption": consumption,
            "government_consumption": government,
            "planned_investment": planned_investment,
            "investment": planned_investment,
            "wages": wages,
            "transfers": transfers,
            "taxes": taxes,
            "interest_firm": interest_firm,
            "interest_government": interest_government,
            "household_income": wages + transfers,
            "debt_open": debt_open,
            "cash_open": cash_open,
            "draw": draw,
            "gross_bond_issue": gross_bond_issue,
            "policy_rate": decisions["policy_rate"],
            "expected_inflation": decisions["expected_inflation"],
            "price_level": decisions["price_level"],
            "inflation": decisions["inflation"],
            "extra_inflation": decisions["extra_inflation"],
            "potential_output": decisions["potential_output"],
            "output_gap_previous": decisions["output_gap_previous"],
            "regime_factor": decisions["regime_factor"],
            "physical": physical,
            "executions": ["loan", "produce", "wages"],
        }

    def _finish_domestic(self, country: CountryConfig, runtime: _CountryRuntime,
                         state: dict) -> None:
        """Transfers, purchases of domestic goods, taxes, interest, debt and repayment."""
        accounts: OpenAccountSystem = runtime.accounts
        imports_local = state["imports_local"]
        total_consumption = state["consumption"]
        domestic_consumption = total_consumption - imports_local
        if domestic_consumption < 0:
            raise WorldError(
                f"country {country.name}: imports ({imports_local}) exceed total household "
                f"consumption ({total_consumption}); the accounting blocks are inconsistent")
        inventory = accounts.real_balance(Sector.FIRM, "inventory")
        needed = domestic_consumption + state["government_consumption"]
        if needed > inventory:
            raise WorldError(
                f"country {country.name}: domestic purchases ({needed}) exceed the "
                f"available inventory ({inventory}) after exports; reduce the export share")
        state["domestic_consumption"] = domestic_consumption
        try:
            if state["gross_bond_issue"] > 0:
                accounts.issue_bond(state["gross_bond_issue"],
                                    memo="gross deficit financing")
            accounts.pay_transfer(state["transfers"], payer=Sector.GOVERNMENT,
                                  receiver=Sector.HOUSEHOLD)
            accounts.household_purchase(domestic_consumption)
            accounts.government_purchase(state["government_consumption"])
            if state["taxes"] > 0:
                accounts.pay_direct_tax(state["taxes"])
            if state["interest_firm"] > 0:
                accounts.pay_interest(state["interest_firm"], debtor=Sector.FIRM,
                                      creditor=Sector.BANK)
            if state["interest_government"] > 0:
                accounts.pay_interest(state["interest_government"],
                                      debtor=Sector.GOVERNMENT, creditor=Sector.BANK)
            loan_now = -accounts.balance(Sector.FIRM, FinancialInstrument.LOAN)
            firm_cash = accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT)
            repayment = min(loan_now, max(_ZERO, firm_cash))
            if repayment > 0:
                accounts.repay_loan(repayment)
            bond_now = -accounts.balance(Sector.GOVERNMENT, FinancialInstrument.BOND)
            government_cash = accounts.balance(Sector.GOVERNMENT,
                                               FinancialInstrument.DEPOSIT)
            bond_repayment = min(bond_now, max(_ZERO, government_cash))
            if bond_repayment > 0:
                accounts.repay_bond(bond_repayment)
        except AccountingError as exc:
            raise WorldError(
                f"country {country.name}: the domestic block broke the accounting "
                f"invariants: {exc}") from exc

    # ------------------------------------------------------------------
    # FX revaluation preparation
    # ------------------------------------------------------------------
    def _prepare_fx_revaluation(self, opening_claims: Mapping, opening_fx: FXMarket,
                                closing_fx: FXMarket,
                                claim_events: list[dict]) -> dict[str, tuple[dict, dict]]:
        """Replicate the external layer's FX revaluation in local currency, per position."""
        transactions: dict[tuple, dict] = {}
        for event in claim_events:
            key = (event["holder"], event["issuer"], event["instrument"],
                   event["currency"])
            agg = transactions.setdefault(
                key, {"amount": _ZERO, "holder_local": _ZERO,
                      "issuer_local": _ZERO})
            agg["amount"] += to_decimal(event["amount"])
            agg["holder_local"] += to_decimal(event["holder_local"])
            agg["issuer_local"] += to_decimal(event["issuer_local"])
        current_claims = dict(self._external.claims)
        keys = set(opening_claims) | set(current_claims) | set(transactions)
        currency_of = {country.name: country.currency
                       for country in self.config.countries}
        claim_revaluation: dict[str, Decimal] = {}
        liability_revaluation: dict[str, Decimal] = {}
        for key in sorted(keys):
            holder, issuer, _instrument, currency = key
            opening_amount = opening_claims.get(key, _ZERO)
            closing_amount = current_claims.get(key, _ZERO)
            tx = transactions.get(key, {"amount": _ZERO, "holder_local": _ZERO,
                                        "issuer_local": _ZERO})
            rate_open = opening_fx.rate(currency)
            rate_close = closing_fx.rate(currency)
            holder_open_rate = opening_fx.rate(currency_of[holder])
            holder_close_rate = closing_fx.rate(currency_of[holder])
            issuer_open_rate = opening_fx.rate(currency_of[issuer])
            issuer_close_rate = closing_fx.rate(currency_of[issuer])
            opening_holder_local = opening_amount * rate_open / holder_open_rate
            closing_holder_local = closing_amount * rate_close / holder_close_rate
            holder_revaluation = (closing_holder_local - opening_holder_local
                                  - tx["holder_local"])
            opening_issuer_local = opening_amount * rate_open / issuer_open_rate
            closing_issuer_local = closing_amount * rate_close / issuer_close_rate
            issuer_revaluation = (-closing_issuer_local + opening_issuer_local
                                  - tx["issuer_local"])
            claim_revaluation[holder] = (claim_revaluation.get(holder, _ZERO)
                                         + holder_revaluation)
            liability_revaluation[issuer] = (liability_revaluation.get(issuer, _ZERO)
                                             + issuer_revaluation)
        pending: dict[str, tuple[dict, dict]] = {}
        for country in self.config.countries:
            claim = claim_revaluation.get(country.name, _ZERO)
            liability = liability_revaluation.get(country.name, _ZERO)
            claim = _q(claim)
            liability = _q(liability)
            if claim == 0 and liability == 0:
                continue
            claim_changes = {Sector.FIRM.value: claim} if claim != 0 else {}
            liability_changes = {Sector.HOUSEHOLD.value: liability} if liability != 0 else {}
            pending[country.name] = (claim_changes, liability_changes)
        return pending

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def _open_country_checks(self, country: CountryConfig, runtime: _CountryRuntime,
                             state: dict, opening_nfa: Mapping[str, Decimal],
                             external_report: dict) -> tuple[dict, dict]:
        physical_result = physical_checks(state["physical"], runtime.physical,
                                          self.scenario)
        report = state["accounts_report"]
        accounts_result = accounts_checks(
            report, state["nominal_gdp"], state["consumption"],
            state["government_consumption"], state["investment"],
            state["household_income"], state["consumption"])
        debt_result = debt_checks(
            state["debt_open"], state["debt"], runtime.previous_nominal_gdp,
            state["nominal_gdp"], state["interest_government"],
            state["primary_balance"], state["cash_change"])
        checks, residuals = merge_checks(physical_result, accounts_result, debt_result)
        checks["open_gdp_identity"] = (
            report.gdp_production == report.gdp_income
            == report.open_gdp_expenditure)
        checks["open_accounts_checked"] = True
        # NFA vs IIP (numeraire, closing rate)
        rate = state["closing_rate"]
        nfa_numeraire = report.nfa * rate
        iip_net = external_report["countries"][country.name]["iip"]["net"]
        residuals["open_nfa_iip"] = float(_residual(nfa_numeraire - iip_net))
        checks["open_nfa_iip"] = _residual(nfa_numeraire - iip_net) <= _TOLERANCE
        # BoP current account against the posted trade flows
        net_exports_numeraire = state["bop_current_account_local"] * state["rate"]
        current_account = external_report["countries"][country.name][
            "current_account_total"]
        residuals["open_bop_current"] = float(_residual(
            net_exports_numeraire - current_account))
        checks["open_bop_current"] = _residual(
            net_exports_numeraire - current_account) <= _TOLERANCE
        # Financial account: the posted external positions change
        financial_account = external_report["countries"][country.name][
            "financial_account"]
        residuals["open_bop_financial"] = float(_residual(
            net_exports_numeraire - financial_account))
        checks["open_bop_financial"] = _residual(
            net_exports_numeraire - financial_account) <= _TOLERANCE
        # IIP identity reconciled with the posted revaluation
        revaluation_from_accounts = (
            report.nfa * rate
            - opening_nfa[country.name] * runtime.previous_rate
            - net_exports_numeraire)
        iip_revaluation = external_report["countries"][country.name]["iip"][
            "revaluation"]
        residuals["open_iip_revaluation"] = float(_residual(
            revaluation_from_accounts - iip_revaluation))
        checks["open_iip_revaluation"] = _residual(
            revaluation_from_accounts - iip_revaluation) <= _TOLERANCE
        # Inventory bridge: capital formation = planned investment + imports - exports
        actual_formation = report.inventory_change.get(Sector.FIRM.value, _ZERO)
        expected_formation = (state["planned_investment"] + state["imports_local"]
                              - state["exports_local"])
        residuals["open_inventory_bridge"] = float(_residual(
            actual_formation - expected_formation))
        checks["open_inventory_bridge"] = _residual(
            actual_formation - expected_formation) <= _TOLERANCE
        # The trade postings in the books are exactly the hook's local values
        residuals["open_trade_posted"] = float(
            _residual(report.exports - state["exports_local"])
            + _residual(report.imports - state["imports_local"]))
        checks["open_trade_posted"] = residuals["open_trade_posted"] <= 1e-9
        # National wealth bridge in the numeraire (residents only: NW = real + NFA)
        savings = report.savings_residents * rate
        revaluation = report.revaluation_residents * rate
        translation = runtime.previous_net_worth * (
            rate - runtime.previous_rate)
        delta_wealth = report.net_worth_residents * rate - (
            runtime.previous_net_worth * runtime.previous_rate)
        expected = savings + revaluation + translation
        residuals["open_national_wealth_bridge"] = float(_residual(
            delta_wealth - expected))
        checks["open_national_wealth_bridge"] = _residual(
            delta_wealth - expected) <= _TOLERANCE
        # ROW mirror and NFA identity inside the book
        residuals["open_row_mirror"] = float(_residual(
            report.nfa + report.row_net_worth))
        checks["open_row_mirror"] = _residual(
            report.nfa + report.row_net_worth) <= _TOLERANCE
        checks["open_domestic_consumption_nonnegative"] = (
            state["domestic_consumption"] >= 0)
        # Policy and expectations (same consistency checks as the closed core)
        expected_inflation = state["expected_inflation"]
        policy_ok = abs(state["policy_rate"] - _clip(
            runtime.policy.rate(expected_inflation, runtime.previous_gap),
            0.0, 20.0)) <= 1e-12
        checks["policy_rule_consistent"] = policy_ok
        checks["expectations_consistent"] = abs(
            expected_inflation - runtime.expected_inflation) <= 1e-12
        residuals["policy_rule_gap"] = abs(state["policy_rate"] - _clip(
            runtime.policy.rate(expected_inflation, runtime.previous_gap), 0.0, 20.0))
        return checks, residuals

    @staticmethod
    def _two_book_mirror_residual(left: Snapshot, right: Snapshot) -> Decimal:
        """Largest residual of the two-book external mirroring in the numeraire."""
        residents_a = tuple(Sector(value).value for value in Sector)
        pairs = ((EXTERNAL_CLAIM, EXTERNAL_LIABILITY),
                 (EXTERNAL_LIABILITY, EXTERNAL_CLAIM))
        worst = _ZERO
        for left_instrument, right_instrument in pairs:
            left_residents = sum(
                (left.balances[sector].get_financial(left_instrument)
                 for sector in residents_a), _ZERO)
            right_residents = sum(
                (right.balances[sector].get_financial(right_instrument)
                 for sector in residents_a), _ZERO)
            left_row = left.balances[ROW].get_financial(left_instrument)
            right_row = right.balances[ROW].get_financial(right_instrument)
            for residual in (left_residents + right_residents,
                             left_row - right_residents,
                             right_row - left_residents):
                if _residual(residual) > worst:
                    worst = _residual(residual)
        return worst

    def _open_world_checks(self, states: dict, external_report: dict,
                           opening_nfa: Mapping[str, Decimal]) -> tuple[dict, dict]:
        config = self.config
        checks: dict[str, bool] = {}
        residuals: dict[str, float] = {}
        # 1) two-book external mirroring in the common numeraire (tolerance because the
        # two books round their local-currency postings independently)
        rates = {name: state["closing_rate"] for name, state in states.items()}
        left = _valued_snapshot(
            self._open_books[config.country_a.name], rates[config.country_a.name])
        right = _valued_snapshot(
            self._open_books[config.country_b.name], rates[config.country_b.name])
        mirror_residual = self._two_book_mirror_residual(left, right)
        residuals["open_two_book_mirror"] = float(mirror_residual)
        checks["open_two_book_mirror"] = mirror_residual <= _TOLERANCE
        if not checks["open_two_book_mirror"]:
            raise WorldError(
                f"two-book external mirroring failed with residual {mirror_residual}")
        # 2) NFA mirror in the numeraire
        nfa_mirror = sum((state["nfa_local"] * state["closing_rate"]
                          for state in states.values()), _ZERO)
        residuals["open_nfa_mirror"] = float(_residual(nfa_mirror))
        checks["open_nfa_mirror"] = _residual(nfa_mirror) <= _TOLERANCE
        # 3) world net worth = real assets (residents NW already includes NFA)
        world_wealth = sum(
            (state["net_worth_local"] * state["closing_rate"]
             for state in states.values()), _ZERO)
        real_assets_num = sum(
            (state["accounts_report"].real_assets_total * state["closing_rate"]
             for state in states.values()), _ZERO)
        residuals["open_world_wealth"] = float(_residual(
            world_wealth - real_assets_num))
        checks["open_world_wealth"] = _residual(
            world_wealth - real_assets_num) <= _TOLERANCE
        # 4) row positions mirror to zero in the numeraire
        row_wealth_num = sum(
            (state["row_net_worth_local"] * state["closing_rate"]
             for state in states.values()), _ZERO)
        residuals["open_row_positions"] = float(_residual(row_wealth_num))
        checks["open_row_positions"] = _residual(row_wealth_num) <= _TOLERANCE
        # 5) hook posted exactly the prepared revaluations
        external_checks_map = external_report["checks"]
        checks["open_external_checks_ok"] = bool(external_checks_map.get("ok", False))
        return checks, residuals

    def _build_open_country_report(self, country: CountryConfig, state: dict,
                                   checks: dict, residuals: dict,
                                   external_report: dict) -> CountryPeriodReport:
        external_country = dict(state["external"])
        external_country["numeraire"] = external_report["numeraire"]
        report = state["accounts_report"]
        return CountryPeriodReport(
            name=country.name,
            currency=country.currency,
            period=self._period_index,
            price_level=state["price_level"],
            inflation=state["inflation"],
            expected_inflation=state["expected_inflation"],
            expected_inflation_next=state["expected_inflation_next"],
            policy_rate=state["policy_rate"],
            output_gap=state["output_gap"],
            physical=state["physical"],
            nominal_gdp=state["nominal_gdp"],
            consumption=state["consumption"],
            government_consumption=state["government_consumption"],
            investment=state["investment"],
            wages=state["wages"],
            transfers=state["transfers"],
            taxes=state["taxes"],
            interest_firm=state["interest_firm"],
            interest_government=state["interest_government"],
            household_income=state["household_income"],
            household_saving=state["household_saving"],
            debt=state["debt"],
            debt_ratio=state["debt_ratio"],
            cash=state["cash"],
            exports_local=state["exports_local"],
            exports_numeraire=state["exports_numeraire"],
            accounts=report,
            external=external_country,
            checks=checks,
            residuals=residuals,
            open_economy=True,
            imports_local=state["imports_local"],
            net_exports_local=state["net_exports_local"],
            nfa_local=state["nfa_local"],
            external_assets_local=state["external_assets_local"],
            external_liabilities_local=state["external_liabilities_local"],
            row_net_worth_local=state["row_net_worth_local"],
            open_gdp_expenditure=report.open_gdp_expenditure,
            bop_current_account_local=state["bop_current_account_local"],
            bop_financial_account_local=state["bop_financial_account_local"],
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        payload = super().describe()
        payload["open_economy"] = True
        payload["open_books"] = {
            name: {
                "nfa": str(book.nfa()),
                "row_net_worth": str(book.row_net_worth()),
                "external_assets": str(book.external_assets()),
                "external_liabilities": str(book.external_liabilities()),
            }
            for name, book in self._open_books.items()}
        return payload


__all__ = ["OpenPostingHook", "OpenWorldCore"]
