"""Oil market module: Brent response to a six-month closure of the Strait of Hormuz (docs/147).

This is a **research prototype**: deterministic scenario arithmetic on explicitly declared
assumptions, not a forecast, not a trading signal and not an admission.  Every number that is not
derived inside the module is an :class:`~amfe.markets.contract.Assumption` with
``source_class="assumption"`` and a ``replace_with`` hint, so the scenario can be re-calibrated
with sourced values (EIA World Oil Transit Chokepoints, IEA Oil Market Report, OPEC MOMR, company
reports) without touching the engine.

Scenario mechanics
------------------
* closure removes ``hormuz_transit`` mb/d for ``closure_months`` months;
* bypass routes (Saudi East-West, UAE Fujairah/ADCOP, Ceyhan and other) add ``bypass_capacity`` with
  a linear ramp; non-Gulf supply response adds ``supply_response`` with its own ramp;
* strategic (SPR/IEA) and commercial stocks release at declared maximum monthly rates; the strategic
  plan is fail-closed when it exceeds the declared stock;
* the price is cleared by the balance engine with a demand elasticity ramped from
  ``elasticity_short`` (effective, includes non-price rationing) to ``elasticity_long``;
* a risk premium is added on top of the cleared price;
* the output is a **range** (low / central / high) over the declared sensitivity variants, never a
  single "exact" number.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from .contract import (
    Assumption,
    CostShock,
    MarketError,
    MarketModule,
    MarketResult,
    Scenario,
    ScenarioError,
    RESEARCH_ONLY,
)
from .engine import MarketCalibration, MarketPath, Shock, flat, price_range, ramp, simulate

MODULE_ID = "oil.brent"
COMMODITY = "Brent crude oil"
UNITS = "USD/bbl"
DAYS_PER_MONTH = 30.4375

#: Central parameters of the prototype (all replaceable; see :data:`ASSUMPTION_SPECS`).
DEFAULT_PARAMS: dict = {
    "base_price": 75.0,            # USD/bbl reference level
    "base_demand": 103.0,          # mb/d world oil demand
    "baseline_supply": 103.0,      # mb/d world oil supply
    "closure_share": 1.0,          # fraction of the strait flow lost
    "hormuz_transit": 20.5,        # mb/d crude + products through Hormuz
    "bypass_capacity": 4.2,        # mb/d usable pipeline bypass (total, ramped)
    "bypass_ramp_months": 3,
    "supply_response": 1.0,        # mb/d non-Gulf supply response
    "supply_response_ramp_months": 4,
    "spr_stock": 150.0,            # mb strategic stock available to the scenario
    "spr_max_draw": 1.5,           # mb/d
    "commercial_stock": 200.0,     # mb commercial crude/products stock
    "commercial_max_draw": 1.0,    # mb/d
    "elasticity_short": -0.25,     # effective short-run demand elasticity (incl. rationing)
    "elasticity_long": -0.55,      # effective elasticity by month 6
    "elasticity_ramp_months": 6,
    "risk_premium": 12.0,          # USD/bbl on top of the cleared price
    "closure_months": 6,
}

#: Which public source should replace each assumption (the "replace-with" list of docs/147).
ASSUMPTION_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("base_price", "USD/bbl", "reference Brent level at the start of the scenario; replace with a "
                              "dated market quote (ICE Brent front month)",
     "ICE/EIA daily Brent quote"),
    ("base_demand", "mb/d", "world oil demand level; replace with IEA OMR / EIA STEO monthly table",
     "IEA Oil Market Report; EIA Short-Term Energy Outlook"),
    ("baseline_supply", "mb/d", "world oil supply level; replace with IEA OMR supply table",
     "IEA Oil Market Report"),
    ("hormuz_transit", "mb/d", "crude + products transiting Hormuz; public approximations cluster "
                               "around 17-21 mb/d",
     "EIA World Oil Transit Chokepoints; IEA"),
    ("closure_share", "share", "share of the strait flow that is actually lost; 1.0 = full closure",
     "scenario definition (policy assumption)"),
    ("bypass_capacity", "mb/d", "usable spare pipeline capacity avoiding Hormuz (Saudi East-West, "
                                "UAE Fujairah/ADCOP, Ceyhan, other); country-level split documented "
                                "in docs/147",
     "company/operator disclosures; EIA chokepoints note"),
    ("bypass_ramp_months", "months", "how fast bypass capacity is used (linear ramp)",
     "assumption: logistics ramp"),
    ("supply_response", "mb/d", "non-Gulf supply response (shale, other producers) on top of bypass",
     "EIA STEO / IEA supply response analysis"),
    ("spr_stock", "mb", "strategic stock available to this scenario (IEA public stocks share)",
     "IEA oil stocks; US DOE SPR data"),
    ("spr_max_draw", "mb/d", "maximum coordinated strategic release rate",
     "IEA collective action precedents (1991, 2005, 2011, 2022)"),
    ("commercial_stock", "mb", "commercial crude/products stock available for drawdown",
     "IEA/EIA stocks data"),
    ("commercial_max_draw", "mb/d", "maximum commercial drawdown rate in the scenario",
     "assumption: logistics"),
    ("elasticity_short", "elasticity", "effective short-run demand elasticity INCLUDING non-price "
                                       "rationing/fuel switching; pure price elasticity (-0.05..-0.10) "
                                       "would imply a >500 USD/bbl arithmetic and is not used",
     "literature ranges (e.g. IMF/IEA elasticity surveys) + explicit rationing assumption"),
    ("elasticity_long", "elasticity", "effective demand elasticity by month 6 as substitution and "
                                      "efficiency responses build up",
     "literature ranges"),
    ("elasticity_ramp_months", "months", "months over which the elasticity ramps to the long value",
     "assumption"),
    ("risk_premium", "USD/bbl", "geopolitical risk premium added on top of the cleared price",
     "assumption based on historical Gulf risk episodes"),
    ("closure_months", "months", "duration of the closure (scenario definition)", "scenario"),
)


def assumptions(params: Mapping | None = None, closure_months: int | None = None) -> tuple:
    """The declared assumptions of the module with the current parameter values."""
    values = dict(DEFAULT_PARAMS)
    if params:
        values.update({key: value for key, value in params.items() if key in DEFAULT_PARAMS})
    if closure_months is not None:
        values["closure_months"] = int(closure_months)
    declared = []
    for name, unit, note, source in ASSUMPTION_SPECS:
        declared.append(Assumption(name=name, value=values.get(name), unit=unit,
                                   source_class="assumption", note=note, source=source))
    return tuple(declared)


def make_shock(params: Mapping, horizon_months: int) -> Shock:
    """Build the monthly shock vectors from the parameters of one scenario."""
    horizon = int(horizon_months)
    if horizon <= 0:
        raise MarketError("horizon_months must be positive")
    closure_months = min(int(params["closure_months"]), horizon)
    lost = [0.0] * horizon
    flow = float(params["closure_share"]) * float(params["hormuz_transit"])
    for month in range(closure_months):
        lost[month] = flow
    bypass = ramp(float(params["bypass_capacity"]), horizon,
                  int(params["bypass_ramp_months"]))
    response = ramp(float(params["supply_response"]), horizon,
                    int(params["supply_response_ramp_months"]))
    added = tuple(round(bypass[index] + response[index], 12) for index in range(horizon))
    max_rate = float(params["spr_max_draw"])
    remaining_mb = float(params["spr_stock"])
    capped = []
    for _month in range(horizon):
        # monthly stock draw = rate (mb/d) x days; keep the plan inside the declared stock
        draw = min(max_rate, max(0.0, remaining_mb) / DAYS_PER_MONTH)
        capped.append(round(draw, 12))
        remaining_mb -= draw * DAYS_PER_MONTH
    return Shock(lost_supply=tuple(lost), added_supply=added,
                 strategic_release=tuple(capped),
                 label=f"hormuz_closed_{closure_months}m")


def _calibration(params: Mapping) -> MarketCalibration:
    return MarketCalibration(
        commodity=COMMODITY, base_price=float(params["base_price"]),
        base_demand=float(params["base_demand"]),
        baseline_supply=float(params["baseline_supply"]),
        elasticity_short=float(params["elasticity_short"]),
        elasticity_long=float(params["elasticity_long"]),
        elasticity_ramp_months=int(params["elasticity_ramp_months"]),
        risk_premium=float(params["risk_premium"]),
        commercial_stock=float(params["commercial_stock"]),
        commercial_max_draw=float(params["commercial_max_draw"]),
        strategic_stock=float(params["spr_stock"]),
        strategic_max_draw=float(params["spr_max_draw"]),
    )


#: Sensitivity variants used for the reported Brent range (name -> parameter overrides).
SENSITIVITY_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("elasticity_more_elastic", {"elasticity_short": -0.35, "elasticity_long": -0.75}),
    ("elasticity_less_elastic", {"elasticity_short": -0.15, "elasticity_long": -0.40}),
    ("bypass_low", {"bypass_capacity": 2.5}),
    ("bypass_high", {"bypass_capacity": 6.0}),
    ("supply_response_low", {"supply_response": 0.5}),
    ("risk_premium_low", {"risk_premium": 0.0}),
    ("risk_premium_high", {"risk_premium": 25.0}),
)


class OilModule(MarketModule):
    """Brent/Hormuz research module implementing the market-module contract."""

    module_id = MODULE_ID
    commodity = COMMODITY
    units = UNITS
    status = RESEARCH_ONLY

    def scenarios(self) -> tuple:
        return (
            Scenario("hormuz_closed_6m",
                     "Strait of Hormuz fully closed for 6 months; bypass pipelines ramp over 3 "
                     "months, non-Gulf supply response over 4 months, strategic + commercial stocks "
                     "release at their declared maxima.",
                     {"closure_months": 6}, horizon_months=6),
            Scenario("hormuz_closed_3m",
                     "Strait of Hormuz fully closed for 3 months; same response mechanics, price "
                     "path continues for 6 months to show the relief.",
                     {"closure_months": 3}, horizon_months=6),
            Scenario("hormuz_closed_6m_no_spr",
                     "Strait of Hormuz fully closed for 6 months with NO strategic stock release "
                     "(commercial drawdown only).",
                     {"closure_months": 6, "spr_stock": 0.0, "spr_max_draw": 0.0},
                     horizon_months=6),
        )

    def run(self, scenario_id: str | None = None, **overrides) -> MarketResult:
        scenario = self.scenario(scenario_id) if scenario_id else self.default_scenario()
        params = dict(DEFAULT_PARAMS)
        params.update(dict(scenario.parameters))
        unknown = [key for key in overrides if key not in DEFAULT_PARAMS]
        if unknown:
            raise ScenarioError(f"unknown parameter overrides {unknown}; allowed "
                                f"{sorted(DEFAULT_PARAMS)}")
        params.update(overrides)
        horizon = int(scenario.horizon_months)
        if int(params.get("closure_months", horizon)) > horizon:
            if "closure_months" not in overrides:
                params["closure_months"] = horizon

        calibration = _calibration(params)
        shock = make_shock(params, horizon)
        path = simulate(calibration, shock)
        if not path.ok:
            raise MarketError(f"oil scenario {scenario.scenario_id}: balance checks failed "
                              f"{path.checks}")
        sensitivity = self._sensitivity(params, horizon, path)
        brent_range = price_range(path, sensitivity, metric="price_peak")
        brent_range_mean = price_range(path, sensitivity, metric="price_mean")
        peak_month = path.summary()["price_peak_month"]
        summary = {
            **path.summary(),
            "brent_range_usd_per_bbl": brent_range,
            "brent_range_mean_usd_per_bbl": brent_range_mean,
            "peak_month": peak_month,
            "scenario_label": scenario.scenario_id,
            "closure_months": int(params["closure_months"]),
            "hormuz_transit_mb_per_day": float(params["hormuz_transit"]),
            "bypass_capacity_mb_per_day": float(params["bypass_capacity"]),
            "spr_release_mb_per_day": float(params["spr_max_draw"]),
            "monthly_path": path.rows(),
            "status_note": "research prototype: scenario arithmetic on declared assumptions, "
                           "not a forecast and not an admission",
        }
        checks = {
            **path.checks,
            "range_ordered": brent_range["low"] <= brent_range["central"] <= brent_range["high"],
            "assumptions_classified": all(item.source_class in
                                          ("observed", "derived", "assumption")
                                          for item in assumptions(params)),
            "deterministic": True,
        }
        return MarketResult(
            module_id=self.module_id, commodity=self.commodity,
            scenario_id=scenario.scenario_id, units=self.units, horizon_months=horizon,
            price_path=path.price, summary=summary, assumptions=assumptions(params),
            sensitivity=tuple(sensitivity), checks=checks,
            meta={"engine": "amfe.markets.engine.v1", "scenario": scenario.as_dict(),
                  "parameters": {key: params[key] for key in sorted(params)},
                  "units": {"price": self.units, "flows": "mb/d", "stocks": "mb",
                            "time_step": "month"},
                  "replace_with": [{"name": name, "source": source}
                                   for name, _unit, _note, source in ASSUMPTION_SPECS]})

    # -- sensitivity -------------------------------------------------------------------
    def _sensitivity(self, params: Mapping, horizon: int, central: MarketPath) -> list[dict]:
        base_peak = max(central.price)
        results = []
        for name, overrides in SENSITIVITY_VARIANTS:
            variant_params = {**params, **overrides}
            try:
                variant = simulate(_calibration(variant_params),
                                   make_shock(variant_params, horizon))
            except MarketError as error:  # fail-closed: a broken variant is reported, not hidden
                results.append({"variant": name, "calibration_overrides": dict(overrides),
                                "error": f"{type(error).__name__}: {error}"})
                continue
            peak = max(variant.price)
            results.append({
                "variant": name,
                "calibration_overrides": dict(overrides),
                "price_peak": round(peak, 6),
                "price_mean": round(math.fsum(variant.price) / len(variant.price), 6),
                "shortage_total": round(math.fsum(variant.shortage), 6),
                "delta_vs_central": round(peak - base_peak, 6),
                "delta_pct_vs_central": round(100.0 * (peak - base_peak) / base_peak, 4),
                "checks": dict(variant.checks),
            })
        # a longer closure is a separate horizon: run it explicitly as a variant
        long_params = {**params, "closure_months": horizon + 3}
        long_path = simulate(_calibration(long_params), make_shock(long_params, horizon + 3))
        long_peak = max(long_path.price)
        results.append({
            "variant": "closure_9m",
            "calibration_overrides": {"closure_months": horizon + 3},
            "price_peak": round(long_peak, 6),
            "price_mean": round(math.fsum(long_path.price) / len(long_path.price), 6),
            "shortage_total": round(math.fsum(long_path.shortage), 6),
            "delta_vs_central": round(long_peak - base_peak, 6),
            "delta_pct_vs_central": round(100.0 * (long_peak - base_peak) / base_peak, 4),
            "checks": dict(long_path.checks),
        })
        results.sort(key=lambda item: (item.get("price_peak", -1.0), item["variant"]))
        return results

    # -- MRIO hook ---------------------------------------------------------------------
    def prepare_cost_shock(self, result: MarketResult, *, pass_through: float = 0.6,
                           sector_shares: Mapping | None = None) -> CostShock:
        """Translate the Brent scenario into sector cost multipliers for the MRIO layer.

        ``sector_shares`` maps a generic cost bucket to the share of the commodity cost in that
        bucket's total cost; the caller maps buckets to MRIO industries (the engine and the world
        core are not modified).  ``pass_through`` is the share of the price change that reaches
        production costs (declared assumption of the bridge, not of the market module).
        """
        summary = result.summary
        base_price = float(result.meta["parameters"]["base_price"])
        peak = float(summary["price_peak"])
        delta = peak - base_price
        delta_pct = delta / base_price
        if not 0.0 <= float(pass_through) <= 1.0:
            raise MarketError("pass_through must be in [0, 1]")
        shares = dict(sector_shares) if sector_shares else {
            "energy": 0.30, "transport": 0.12, "manufacturing": 0.04, "services": 0.02,
        }
        if any(float(value) < 0.0 for value in shares.values()):
            raise MarketError("sector_shares must be non-negative")
        multipliers = {
            name: round(1.0 + float(pass_through) * delta_pct * float(share), 6)
            for name, share in shares.items()
        }
        assumptions = tuple(item for item in result.assumptions
                            if item.name in ("base_price", "risk_premium"))
        bridge = Assumption(name="cost_pass_through", value=float(pass_through),
                            unit="share", source_class="assumption",
                            note="share of the Brent price change that reaches production costs",
                            source="assumption: bridge to the MRIO layer")
        return CostShock(
            shock_type="energy_price", commodity=result.commodity,
            brent_delta_usd=round(delta, 6), brent_peak_usd=round(peak, 6),
            brent_delta_pct=round(delta_pct, 9),
            sector_cost_multipliers=multipliers,
            default_multiplier=round(1.0 + float(pass_through) * delta_pct * 0.03, 6),
            assumptions=assumptions + (bridge,),
            notes="Buckets must be mapped to MRIO industries by the caller (e.g. energy -> mining "
                  "and utilities, transport -> land/water transport). No world-core changes.")
