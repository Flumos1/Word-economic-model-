"""WorldCore: one executable cycle over the four prototype layers (stages 2-5).

The cycle of one period is explicit and ordered:

1. **start of period** - opening the accounting period (accounts layer);
2. **decisions** - inflation (reduced-form rule), adaptive expectations, Taylor-rule
   policy rate, Markov regime state, exogenous scenario factors;
3. **production** - physical layer: Leontief IO output under energy and labour limits;
4. **income / expenditure / finance** - monetary block posting the sector-account
   transactions (production, wages, transfers, taxes, interest, public debt financing,
   household and government purchases) and closing the accounts period;
5. **external operations** - balanced two-country trade credits, FX close and
   revaluation through :class:`amfe.international.TwoCountryWorld`;
6. **invariants** - physical, monetary, external and cross-layer checks (see
   :mod:`amfe.world.integration`) are collected into one ``ok`` flag;
7. **period report** - a typed, deterministic :class:`~amfe.world.report.WorldPeriodReport`.

Only public APIs of the four packages are used; none of them is modified.  The
documented stub is that the domestic accounting kernel is a *closed-economy* kernel,
so external trade is observed through the international hook and reconciled at the
national-wealth level instead of being posted into the four domestic sectors
(docs/85 §4).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from amfe.accounts import (
    AccountSystem,
    AccountingError,
    FinancialInstrument,
    OpeningBalances,
    Sector,
    to_decimal,
)
from amfe.dynamics import (
    KIND_HYPOTHESIS,
    KIND_IDENTITY,
    KIND_INSTITUTIONAL_RULE,
    KIND_PHYSICAL_LIMIT,
    KIND_SCENARIO,
    AdaptiveExpectations,
    InflationTargetRegime,
    MarkovSwitchingModel,
    NewKeynesianDeterminacySystem,
    TaylorRule,
    counts as mechanism_counts,
    stabilizing_primary_balance,
)
from amfe.international import Country, FXMarket, TwoCountryWorld
from amfe.international.errors import InternationalError
from amfe.resources import Scenario as PhysicalScenario

from .config import CountryConfig, WorldConfig, WorldConfigError
from .integration import (
    CountryWealthState,
    IntegrationError,
    CompositeHook,
    IntegrationHook,
    WorldError,
    accounts_checks,
    assert_integration,
    debt_checks,
    external_checks,
    is_hook,
    merge_checks,
    money,
    physical_checks,
    wealth_bridge,
    world_wealth_check,
)
from .physical import CountryPhysical, PhysicalOutcome
from .report import CountryPeriodReport, WorldPeriodReport, WorldRunResult, report_digest
from .scenarios import WorldScenario, baseline_scenario

_QUANTUM = Decimal("1e-12")
_ZERO = Decimal(0)


def _q(value: object) -> Decimal:
    """Quantise a monetary amount to 1e-12 so that reports are reproducible."""
    return to_decimal(value).quantize(_QUANTUM)


def _share_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _finite_float_list(values) -> list:
    result = []
    for value in values:
        number = float(value)
        result.append(None if not math.isfinite(number) else round(number, 12))
    return result


@dataclass
class _CountryRuntime:
    """Mutable per-country state that carries over between periods."""

    config: CountryConfig
    physical: CountryPhysical
    accounts: AccountSystem
    policy: TaylorRule
    expectations: AdaptiveExpectations
    inflation_regime: InflationTargetRegime
    determinacy: dict
    expected_inflation: float
    price_level: Decimal
    previous_gap: float
    previous_policy_rate: float
    previous_nominal_gdp: Decimal
    previous_debt: Decimal
    previous_cash: Decimal
    previous_loan: Decimal
    previous_net_worth: Decimal
    previous_rate: Decimal
    previous_nfa: Decimal


class WorldCore:
    """Deterministic integrated world model over the four prototype layers."""

    def __init__(self, config: WorldConfig | None = None,
                 scenario: WorldScenario | None = None, *,
                 external_hook: object | None = None):
        self.config = WorldConfig() if config is None else config
        if not isinstance(self.config, WorldConfig):
            raise WorldConfigError("config must be a WorldConfig instance")
        self.scenario = baseline_scenario() if scenario is None else scenario
        if not isinstance(self.scenario, WorldScenario):
            raise WorldConfigError("scenario must be a WorldScenario instance")
        if external_hook is not None and not is_hook(external_hook):
            raise WorldConfigError(
                "external_hook must implement amfe.international.ExternalAccountsHook")
        self._provided_hook = external_hook
        self._initialize()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _initialize(self) -> None:
        config = self.config
        self._physical: dict[str, CountryPhysical] = {}
        self._runtimes: dict[str, _CountryRuntime] = {}
        self._baseline_va: dict[str, float] = {}
        initial_rates = {country.currency: country.initial_fx_rate
                         for country in config.countries}
        try:
            self._fx = FXMarket(config.numeraire, initial_rates)
        except Exception as exc:
            raise WorldConfigError(f"invalid initial FX market: {exc}") from exc
        self._hook = CompositeHook(self._provided_hook)
        try:
            self._external = TwoCountryWorld(
                Country(config.country_a.name, config.country_a.currency),
                Country(config.country_b.name, config.country_b.currency),
                self._fx, hook=self._hook)
        except Exception as exc:
            raise WorldConfigError(f"cannot build the external layer: {exc}") from exc

        for country in config.countries:
            try:
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
                accounts = AccountSystem(period="P1", opening=opening)
            except AccountingError as exc:  # pragma: no cover - configuration dependent
                raise WorldConfigError(
                    f"country {country.name}: invalid initial stocks: {exc}") from exc
            policy = TaylorRule(r_star=country.policy_r_star,
                                pi_target=country.inflation_target,
                                phi_pi=country.policy_phi_pi,
                                phi_y=country.policy_phi_y)
            expectations = AdaptiveExpectations(
                gain=country.expectations_gain, initial=country.inflation_target)
            inflation_regime = InflationTargetRegime(
                target=country.inflation_target,
                source="world-core prototype assumption (docs/85)")
            determinacy = NewKeynesianDeterminacySystem(rule=policy).determinacy()
            self._runtimes[country.name] = _CountryRuntime(
                config=country,
                physical=physical,
                accounts=accounts,
                policy=policy,
                expectations=expectations,
                inflation_regime=inflation_regime,
                determinacy=determinacy,
                expected_inflation=country.inflation_target,
                price_level=_share_decimal(country.initial_price_level),
                previous_gap=0.0,
                previous_policy_rate=0.0,
                previous_nominal_gdp=initial_gdp,
                previous_debt=debt,
                previous_cash=_ZERO,
                previous_loan=loan,
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

    def reset(self) -> None:
        """Re-initialise the whole state (the configuration and scenario are kept)."""
        hook = self._hook if hasattr(self._hook, "reset") else None
        if hook is not None:
            hook.reset()
        self._initialize()

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------
    @property
    def hook(self) -> IntegrationHook:
        return self._hook  # type: ignore[return-value]

    @property
    def external(self) -> TwoCountryWorld:
        return self._external

    @property
    def fx(self) -> FXMarket:
        return self._fx

    @property
    def period(self) -> int:
        return self._period_index

    @property
    def reports(self) -> tuple[WorldPeriodReport, ...]:
        return tuple(self._reports)

    @property
    def last_report(self) -> WorldPeriodReport | None:
        return self._reports[-1] if self._reports else None

    def accounts(self, country: str) -> AccountSystem:
        return self._runtime(country).accounts

    def _runtime(self, country: str) -> _CountryRuntime:
        try:
            return self._runtimes[country]
        except KeyError as exc:
            raise WorldError(f"unknown country {country!r}") from exc

    # ------------------------------------------------------------------
    # Run / step
    # ------------------------------------------------------------------
    def run(self, periods: int | None = None) -> WorldRunResult:
        """Run ``periods`` periods from the current state and return the typed result."""
        remaining = self.config.max_periods - self._period_index
        horizon = remaining if periods is None else int(periods)
        if horizon < 1:
            raise WorldError("periods must be a positive integer")
        if horizon > remaining:
            raise WorldError(
                f"cannot run {horizon} periods: only {remaining} of the configured "
                f"max_periods={self.config.max_periods} remain")
        reports = [self.step() for _ in range(horizon)]
        return WorldRunResult(scenario=self.scenario.name, periods=len(reports),
                              reports=tuple(reports),
                              digest=report_digest(reports))

    def step(self) -> WorldPeriodReport:
        """Advance exactly one period and return its integrated report."""
        config = self.config
        scenario = self.scenario
        period = self._period_index
        if period >= config.max_periods:
            raise WorldError(
                f"the configured horizon max_periods={config.max_periods} is exhausted")
        regime_state = int(self._regime_states[period])
        regime_factor = 1.0 if regime_state == 0 else config.regime_contraction_factor

        runtime_states: dict[str, dict] = {}
        current_states: dict[str, CountryWealthState] = {}
        country_reports: dict[str, CountryPeriodReport] = {}
        claim_start = len(getattr(self._hook, "claims", []))
        flow_start = len(getattr(self._hook, "flows", []))
        revaluation_start = len(getattr(self._hook, "revaluations", []))

        # 1-4: per-country cycle up to the accounts close.
        for country in config.countries:
            runtime = self._runtime(country.name)
            accounts = runtime.accounts
            if not accounts.period_open:
                accounts.begin_period(f"P{period + 1}")
            decisions = self._decide(country, runtime, scenario, regime_factor, period)
            physical = self._produce(country, runtime, scenario, regime_factor, period)
            monetary = self._monetise(country, runtime, accounts, decisions, physical)
            runtime_states[country.name] = {**decisions, **monetary}
            runtime_states[country.name]["physical"] = physical

        # 5: external operations (after both monetary blocks are known).
        external_report = self._external_step(runtime_states, period, scenario)

        # 6-7: per-country checks, bridges and the period report.
        world_checks: dict[str, bool] = {}
        world_residuals: dict[str, float] = {}
        hook_claims = self._hook_slice("claims", claim_start)
        hook_flows = self._hook_slice("flows", flow_start)
        hook_revaluations = self._hook_slice("revaluations", revaluation_start)
        expected_trade: dict[str, Decimal] = {}
        export_a = runtime_states[config.country_a.name]["exports_local"]
        rate_a = self._external.fx.rate(config.country_a.currency)
        expected_trade[config.country_a.name] = export_a * rate_a
        expected_trade[config.country_b.name] = runtime_states[
            config.country_b.name]["exports_numeraire"]
        external_ok, external_residuals = external_checks(
            external_report, hook_claims, hook_flows, hook_revaluations, expected_trade)
        world_checks.update(external_ok)
        world_residuals.update(external_residuals)

        for country in config.countries:
            runtime = self._runtime(country.name)
            state = runtime_states[country.name]
            previous_state = CountryWealthState(
                name=country.name, price_level=runtime.price_level,
                rate=runtime.previous_rate,
                net_worth_local=runtime.previous_net_worth,
                nfa_numeraire=runtime.previous_nfa)
            current_state = CountryWealthState(
                name=country.name, price_level=state["price_level"],
                rate=state["closing_rate"],
                net_worth_local=state["net_worth_local"],
                nfa_numeraire=state["nfa_numeraire"])
            current_states[country.name] = current_state
            country_checks, country_residuals = self._country_checks(
                country, runtime, state, previous_state, current_state,
                external_report, period)
            country_checks, country_residuals = merge_checks(
                (country_checks, country_residuals))
            prefix = f"{country.name}."
            world_checks.update({prefix + key: value
                                 for key, value in country_checks.items()})
            world_residuals.update({prefix + key: value
                                    for key, value in country_residuals.items()})
            state["expected_inflation_next"] = runtime.expectations.update(
                state["inflation"])
            country_reports[country.name] = self._build_country_report(
                country, state, country_checks, country_residuals, external_report)

        real_assets = {name: report.accounts.closing_net_worth
                       for name, report in country_reports.items()}
        real_totals = {
            name: sum(values.values(), _ZERO)
            for name, values in real_assets.items()}
        world_wealth_ok, world_wealth_residuals = world_wealth_check(
            list(current_states.values()), real_totals)
        world_checks.update(world_wealth_ok)
        world_residuals.update(world_wealth_residuals)

        assert_integration(world_checks, world_residuals)
        dynamics = self._dynamics_summary(regime_state, regime_factor)
        fx_snapshot = self._external.fx.snapshot()
        report = WorldPeriodReport(
            period=period,
            scenario=self.scenario.name,
            fx=fx_snapshot,
            countries=country_reports,
            external={
                "numeraire": external_report["numeraire"],
                "opening_rates": external_report["opening_rates"],
                "closing_rates": external_report["closing_rates"],
                "mirror": external_report["mirror"],
                "checks": external_report["checks"],
                "hook": self._hook_summary(claim_start, flow_start, revaluation_start),
            },
            dynamics=dynamics,
            checks=world_checks,
            residuals=world_residuals,
        )
        # 8: carry the state to the next period.
        for country in config.countries:
            runtime = self._runtime(country.name)
            state = runtime_states[country.name]
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
            runtime.previous_nfa = state["nfa_numeraire"]
        self._period_index += 1
        self._reports.append(report)
        return report

    # ------------------------------------------------------------------
    # Cycle steps
    # ------------------------------------------------------------------
    def _decide(self, country: CountryConfig, runtime: _CountryRuntime,
                scenario: WorldScenario, regime_factor: float,
                period: int) -> dict:
        extra_inflation = (scenario.extra_inflation.get(country.name, 0.0)
                           if period >= scenario.extra_inflation_start else 0.0)
        inflation = (country.inflation_target
                     + country.inflation_slope * runtime.previous_gap
                     + extra_inflation)
        inflation = _clip(inflation, -5.0, 15.0)
        price_level = _q(runtime.price_level
                         * (Decimal(1) + _share_decimal(inflation) / Decimal(100)))
        expected = runtime.expected_inflation
        raw_rate = runtime.policy.rate(expected, runtime.previous_gap)
        policy_rate = _clip(raw_rate, 0.0, 20.0)
        potential = self._baseline_va[country.name] * (
            1.0 + country.real_growth) ** period
        return {
            "inflation": inflation,
            "price_level": price_level,
            "expected_inflation": expected,
            "policy_rate": policy_rate,
            "raw_policy_rate": raw_rate,
            "output_gap_previous": runtime.previous_gap,
            "potential_output": potential,
            "extra_inflation": extra_inflation,
            "regime_factor": regime_factor,
        }

    def _produce(self, country: CountryConfig, runtime: _CountryRuntime,
                 scenario: WorldScenario, regime_factor: float,
                 period: int) -> PhysicalOutcome:
        demand_multiplier = scenario.demand_factor * regime_factor
        physical_scenario = PhysicalScenario(
            name=f"p{period + 1}",
            demand_factors={sector: demand_multiplier
                            for sector in country.physical.sectors},
            energy_factor=scenario.energy_factor,
            labour_factor=scenario.labour_factor,
        )
        try:
            return runtime.physical.run(physical_scenario)
        except WorldError:
            raise
        except Exception as exc:
            raise WorldError(
                f"country {country.name}: physical layer failed in period {period + 1}: "
                f"{exc}") from exc

    def _monetise(self, country: CountryConfig, runtime: _CountryRuntime,
                  accounts: AccountSystem, decisions: dict,
                  physical: PhysicalOutcome) -> dict:
        if physical.value_added <= 0.0:
            raise WorldError(
                f"country {country.name}: period {self._period_index + 1} has no "
                "monetisable physical output (value added is zero); an all-zero "
                "monetary period is not a supported state")
        price = decisions["price_level"]
        nominal_gdp = _q(price * money(physical.value_added))
        consumption = _q(nominal_gdp * _share_decimal(country.physical.household_demand_share))
        government = _q(nominal_gdp
                        * _share_decimal(country.physical.government_demand_share))
        investment = nominal_gdp - consumption - government
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
        growth_expectation = (country.real_growth + decisions["inflation"] / 100.0)
        stabilising = stabilizing_primary_balance(
            debt_ratio_previous, float(policy_fraction), growth_expectation)
        primary_target = stabilising + country.debt_response * (
            debt_ratio_previous - country.debt_target)
        tax_target = government + transfers + _q(
            _share_decimal(primary_target) * nominal_gdp)
        tax_floor = _q(_share_decimal(country.tax_min) * nominal_gdp)
        tax_ceiling = _q(_share_decimal(country.tax_max) * nominal_gdp)
        taxes = min(max(tax_target, tax_floor), tax_ceiling)

        liquidity_need = wages + taxes + interest_firm - firm_cash_open - (
            consumption + government)
        draw = max(_ZERO, liquidity_need, wages - firm_cash_open)
        gross_bond_issue = max(_ZERO, government + transfers
                               + interest_government - cash_open)

        executions: list[str] = []
        try:
            if draw > 0:
                accounts.loan(draw, memo=f"working capital, period {self._period_index + 1}")
                executions.append("loan")
            accounts.produce(nominal_gdp, memo="physical value added")
            executions.append("produce")
            accounts.pay_wages(wages)
            executions.append("wages")
            if gross_bond_issue > 0:
                accounts.issue_bond(gross_bond_issue, memo="gross deficit financing")
                executions.append("bond_issue")
            accounts.pay_transfer(transfers, payer=Sector.GOVERNMENT,
                                  receiver=Sector.HOUSEHOLD)
            executions.append("government_transfer")
            accounts.household_purchase(consumption)
            executions.append("household_consumption")
            accounts.government_purchase(government)
            executions.append("government_consumption")
            if taxes > 0:
                accounts.pay_direct_tax(taxes)
                executions.append("direct_tax")
            if interest_firm > 0:
                accounts.pay_interest(interest_firm, debtor=Sector.FIRM,
                                      creditor=Sector.BANK)
                executions.append("firm_interest")
            if interest_government > 0:
                accounts.pay_interest(interest_government, debtor=Sector.GOVERNMENT,
                                      creditor=Sector.BANK)
                executions.append("government_interest")
            loan_now = -accounts.balance(Sector.FIRM, FinancialInstrument.LOAN)
            firm_cash = accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT)
            loan_repayment = min(loan_now, max(_ZERO, firm_cash))
            if loan_repayment > 0:
                accounts.repay_loan(loan_repayment)
                executions.append("loan_repayment")
            bond_now = -accounts.balance(Sector.GOVERNMENT, FinancialInstrument.BOND)
            government_cash = accounts.balance(Sector.GOVERNMENT,
                                               FinancialInstrument.DEPOSIT)
            bond_repayment = min(bond_now, max(_ZERO, government_cash))
            if bond_repayment > 0:
                accounts.repay_bond(bond_repayment)
                executions.append("bond_repayment")
            accounts_report = accounts.close_period()
        except AccountingError as exc:
            raise WorldError(
                f"country {country.name}: the monetary block broke the accounting "
                f"invariants in period {self._period_index + 1}: {exc}") from exc

        debt = -accounts.balance(Sector.GOVERNMENT, FinancialInstrument.BOND)
        cash = accounts.balance(Sector.GOVERNMENT, FinancialInstrument.DEPOSIT)
        loan = -accounts.balance(Sector.FIRM, FinancialInstrument.LOAN)
        net_worth_local = sum(accounts_report.closing_net_worth.values(), _ZERO)
        household_income = wages + transfers
        output_gap = physical.value_added / decisions["potential_output"] - 1.0
        national_gdp = nominal_gdp
        export_local = _q(_share_decimal(country.export_share) * national_gdp)
        rate = self._fx.rate(country.currency)
        export_numeraire = export_local * rate
        return {
            "nominal_gdp": national_gdp,
            "consumption": consumption,
            "government_consumption": government,
            "investment": investment,
            "wages": wages,
            "transfers": transfers,
            "taxes": taxes,
            "interest_firm": interest_firm,
            "interest_government": interest_government,
            "household_income": household_income,
            "household_saving": household_income - consumption,
            "debt": debt,
            "cash": cash,
            "loan": loan,
            "net_worth_local": net_worth_local,
            "accounts_report": accounts_report,
            "output_gap": output_gap,
            "debt_ratio": float(debt / national_gdp) if national_gdp > 0 else float("nan"),
            "primary_balance": taxes - government - transfers,
            "cash_change": cash - cash_open,
            "debt_open": debt_open,
            "executions": executions,
            "exports_local": export_local,
            "exports_numeraire": export_numeraire,
            "rate": rate,
            "household_cash": accounts.balance(Sector.HOUSEHOLD,
                                               FinancialInstrument.DEPOSIT),
            "firm_cash": accounts.balance(Sector.FIRM, FinancialInstrument.DEPOSIT),
            "bank_net_worth": accounts.net_worth(Sector.BANK),
            "government_net_worth": accounts.net_worth(Sector.GOVERNMENT),
        }

    def _external_step(self, states: dict[str, dict], period: int,
                       scenario: WorldScenario) -> dict:
        config = self.config
        rate_a = self._fx.rate(config.country_a.currency)
        rate_b = self._fx.rate(config.country_b.currency)
        export_a_local = states[config.country_a.name]["exports_local"]
        # Balanced trade in the numeraire: A exports V_A and B exports exactly the
        # numeraire-equivalent value, so the current-account and financial-account
        # mirrors are zero by construction and only the gross positions grow.
        export_b_local = _q(export_a_local * rate_a / rate_b)
        states[config.country_a.name]["exports_numeraire"] = export_a_local * rate_a
        states[config.country_b.name]["exports_local"] = export_b_local
        states[config.country_b.name]["exports_numeraire"] = export_b_local * rate_b
        try:
            self._external.record_trade(
                config.country_a.name, config.country_b.name, export_a_local,
                config.country_a.currency, category="goods")
            self._external.record_trade(
                config.country_b.name, config.country_a.name, export_b_local,
                config.country_b.currency, category="goods")
            step_rate = self._fx_rate_for_period(period, scenario)
            closing_fx = self._fx
            if step_rate is not None:
                non_numeraire = (config.country_b if config.country_b.currency
                                 != config.numeraire else config.country_a)
                closing_fx = self._fx.with_rates({non_numeraire.currency: step_rate})
            external_report = self._external.close_period(closing_fx)
        except WorldError:
            raise
        except Exception as exc:
            raise WorldError(
                f"external layer failed in period {period + 1}: {exc}") from exc
        self._fx = self._external.fx
        for country in config.countries:
            country_payload = external_report["countries"][country.name]
            states[country.name]["closing_rate"] = self._fx.rate(country.currency)
            states[country.name]["nfa_numeraire"] = country_payload["iip"]["net"]
            states[country.name]["external"] = country_payload
        return external_report

    def _fx_rate_for_period(self, period: int, scenario: WorldScenario) -> float | None:
        applicable = [(key, value) for key, value in scenario.fx_rate_path.items()
                      if key <= period]
        if not applicable:
            return None
        return max(applicable, key=lambda item: item[0])[1]

    # ------------------------------------------------------------------
    # Checks and reports
    # ------------------------------------------------------------------
    def _country_checks(self, country: CountryConfig, runtime: _CountryRuntime,
                        state: dict, previous_state: CountryWealthState,
                        current_state: CountryWealthState,
                        external_report: dict, period: int) -> tuple[dict, dict]:
        physical_checks_result = physical_checks(
            state["physical"], runtime.physical, self.scenario)
        accounts_report = state["accounts_report"]
        accounts_checks_result = accounts_checks(
            accounts_report, state["nominal_gdp"], state["consumption"],
            state["government_consumption"], state["investment"],
            state["household_income"], state["consumption"])
        debt_checks_result = debt_checks(
            state["debt_open"], state["debt"], runtime.previous_nominal_gdp,
            state["nominal_gdp"], state["interest_government"],
            state["primary_balance"], state["cash_change"])
        external_country = external_report["countries"][country.name]
        bridge_checks, bridge_residuals = wealth_bridge(
            previous_state, current_state,
            sum(accounts_report.savings.values(), _ZERO),
            sum(accounts_report.revaluation.values(), _ZERO),
            external_country["iip"]["transactions"],
            external_country["iip"]["revaluation"])
        expected = state["expected_inflation"]
        policy_ok = abs(state["policy_rate"]
                        - _clip(runtime.policy.rate(expected, runtime.previous_gap),
                                0.0, 20.0)) <= 1e-12
        expectations_ok = abs(expected - runtime.expected_inflation) <= 1e-12
        checks, residuals = merge_checks(
            physical_checks_result, accounts_checks_result, debt_checks_result,
            (bridge_checks, bridge_residuals))
        checks["policy_rule_consistent"] = policy_ok
        checks["expectations_consistent"] = expectations_ok
        checks["external_mirror_country"] = all(
            value == 0 for value in external_report["mirror"].values())
        residuals["policy_rule_gap"] = abs(
            state["policy_rate"] - _clip(runtime.policy.rate(
                expected, runtime.previous_gap), 0.0, 20.0))
        residuals["expectations_gap"] = abs(expected - runtime.expected_inflation)
        return checks, residuals

    def _build_country_report(self, country: CountryConfig, state: dict,
                              checks: dict, residuals: dict,
                              external_report: dict) -> CountryPeriodReport:
        external_country = dict(state["external"])
        external_country["numeraire"] = external_report["numeraire"]
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
            accounts=state["accounts_report"],
            external=external_country,
            checks=checks,
            residuals=residuals,
        )

    def _dynamics_summary(self, regime_state: int, regime_factor: float) -> dict:
        config = self.config
        nk = {name: {
            "classification": runtime.determinacy["classification"],
            "taylor_principle": runtime.determinacy["taylor_principle"],
            "caveat": runtime.determinacy["caveat"],
        } for name, runtime in self._runtimes.items()}
        policy = {}
        for country in config.countries:
            runtime = self._runtimes[country.name]
            policy[country.name] = {
                **runtime.policy.to_dict(),
                "inflation_target": runtime.inflation_regime.target,
                "institutional_source": runtime.inflation_regime.source,
            }
        return {
            "regime_state": regime_state,
            "regime_factor": regime_factor,
            "regime_stationary_distribution": list(self._regime_stationary),
            "regime_expected_durations": list(self._regime_durations),
            "policy_rule": policy,
            "nk_determinacy": nk,
            "debt_rule": {
                "kind": "institutional rule / hypothesis: debt-target stabilising balance",
                "debt_target": {country.name: country.debt_target
                                for country in config.countries},
                "response": {country.name: country.debt_response
                             for country in config.countries},
            },
            "mechanism_counts": dict(mechanism_counts()),
            "classification": {
                "accounting": KIND_IDENTITY,
                "physical_limits": KIND_PHYSICAL_LIMIT,
                "inflation_target": KIND_INSTITUTIONAL_RULE,
                "policy_rule": KIND_HYPOTHESIS,
                "regime_switching": KIND_HYPOTHESIS,
                "debt_identity": KIND_IDENTITY,
                "fx_devaluation": KIND_SCENARIO,
            },
            "caveats": [
                "Local stability/determinacy is a property of the linearised model, "
                "not a crisis forecast (docs/dynamics stability caveat).",
                "The regime path is a synthetic hypothesis generated from a seeded "
                "Markov chain; it is not estimated on data.",
                "Devaluation inflation pass-through is an exogenous scenario "
                "assumption, not an estimated causal effect.",
            ],
        }

    # ------------------------------------------------------------------
    # Hook helpers
    # ------------------------------------------------------------------
    def _hook_slice(self, name: str, start: int) -> list[dict]:
        data = getattr(self._hook, name, None)
        if data is None:
            return []
        return list(data[start:])

    def _hook_summary(self, claim_start: int, flow_start: int,
                      revaluation_start: int) -> dict:
        if hasattr(self._hook, "summary"):
            return self._hook.summary(claim_start, flow_start, revaluation_start)
        return {"claims": 0, "flows": 0, "revaluations": []}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "config": {
                "numeraire": self.config.numeraire,
                "max_periods": self.config.max_periods,
                "regime_seed": self.config.regime_seed,
                "countries": {
                    country.name: {
                        "currency": country.currency,
                        "initial_fx_rate": country.initial_fx_rate,
                        "physical": self._physical[country.name].describe(),
                    }
                    for country in self.config.countries},
            },
            "scenario": self.scenario.describe(),
            "period": self._period_index,
            "hook": type(self._hook).__name__,
        }


__all__ = ["WorldCore"]
