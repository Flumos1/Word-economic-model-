"""Soybean-in-Africa pilot: scenario runner over the agri archetype (docs/148).

The pilot configuration is *illustrative* (``source_class="assumption"``): levels are rounded
assumptions, not USDA/FAO observations.  The scenario is fixed: a -15 % Brazilian soybean yield
shock combined with a Brazilian export restriction.  The output is a range for soybean and soymeal
prices and the implied range of the African import bill and food-inflation effect; no point forecast
of a price is produced.

The module implements the local ``AgriMarketModule`` contract (module id/version/passport/validate/
run) and, when the optional ``amfe.markets`` framework is present, can be adapted to it by the
caller; this package never imports track B code directly beyond the guarded try/except in
:mod:`amfe.markets_agri.contracts`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .africa import AfricaConfig, compute_africa_impact
from .balance import CrushParams, GlobalBalance, SoyRegion, build_global_balance, crush_margin
from .contracts import (MODULE_ID, MODULE_VERSION, NOT_A_FORECAST, AgriMarketError,
                        AgriModuleResult, PriceRange, ScenarioShock)
from .pricing import ScenarioParameters, price_range_from_corners, solve_corner
from .seasonality import describe_seasonality, seasonal_path


def default_regions() -> dict:
    """Illustrative 2024/25-style regional assumptions (Mt, Mha); not official statistics."""
    return {
        "Brazil": SoyRegion(name="Brazil", area_mha=45.0, yield_t_ha=3.40,
                            beginning_stocks_mt=15.0, crush_mt=65.0, other_use_mt=20.0,
                            exports_mt=100.0),
        "United States": SoyRegion(name="United States", area_mha=34.0, yield_t_ha=3.35,
                                   beginning_stocks_mt=20.0, crush_mt=65.0, other_use_mt=25.0,
                                   exports_mt=50.0),
        "Argentina": SoyRegion(name="Argentina", area_mha=17.0, yield_t_ha=2.90,
                               beginning_stocks_mt=5.0, crush_mt=50.0, other_use_mt=8.0,
                               exports_mt=8.0),
        "Rest of world": SoyRegion(name="Rest of world", area_mha=40.0, yield_t_ha=2.00,
                                   beginning_stocks_mt=15.0, crush_mt=105.0, other_use_mt=47.0,
                                   exports_mt=17.0),
    }


@dataclass(frozen=True)
class SoyConfig:
    """Assumption set for the pilot: prices, freight, crush yields and scenario bounds."""

    regions: Mapping[str, SoyRegion]
    base_soy_price_usd_t: float = 420.0
    base_meal_price_usd_t: float = 330.0
    base_oil_price_usd_t: float = 1000.0
    base_freight_usd_t: float = 55.0
    import_margin: float = 1.05
    crush: CrushParams = CrushParams()
    parameters: ScenarioParameters = ScenarioParameters()
    africa: AfricaConfig = AfricaConfig()

    def __post_init__(self) -> None:
        regions = dict(self.regions)
        if not regions:
            raise AgriMarketError("SoyConfig.regions must be non-empty")
        if "Brazil" not in regions:
            raise AgriMarketError("SoyConfig.regions must contain Brazil for the pilot scenario")
        clean = {}
        for name, region in regions.items():
            if not isinstance(region, SoyRegion):
                raise AgriMarketError(f"region {name!r} must be a SoyRegion")
            clean[name] = region
        object.__setattr__(self, "regions", clean)
        prices = (self.base_soy_price_usd_t, self.base_meal_price_usd_t,
                  self.base_oil_price_usd_t)
        if not all(math.isfinite(float(value)) and float(value) > 0.0 for value in prices):
            raise AgriMarketError("base prices must be finite and positive")
        freight = float(self.base_freight_usd_t)
        if not math.isfinite(freight) or freight < 0.0:
            raise AgriMarketError("base freight must be finite and non-negative")
        margin = float(self.import_margin)
        if not math.isfinite(margin) or margin < 1.0:
            raise AgriMarketError("import margin must be finite and >= 1")
        if not isinstance(self.crush, CrushParams):
            raise AgriMarketError("crush must be a CrushParams")
        if not isinstance(self.parameters, ScenarioParameters):
            raise AgriMarketError("parameters must be ScenarioParameters")
        if not isinstance(self.africa, AfricaConfig):
            raise AgriMarketError("africa must be an AfricaConfig")

    def to_dict(self) -> dict:
        return {
            "regions": {name: region.to_dict() for name, region in self.regions.items()},
            "base_soy_price_usd_t": float(self.base_soy_price_usd_t),
            "base_meal_price_usd_t": float(self.base_meal_price_usd_t),
            "base_oil_price_usd_t": float(self.base_oil_price_usd_t),
            "base_freight_usd_t": float(self.base_freight_usd_t),
            "import_margin": float(self.import_margin),
            "crush": self.crush.to_dict(),
            "parameters": self.parameters.to_dict(),
            "africa": self.africa.to_dict(),
            "source_class": "assumption",
        }


@dataclass(frozen=True)
class SoyScenario:
    """The registered pilot scenario: Brazilian weather shock + export restriction."""

    name: str = "Brazil_weather_shock_-15pct_plus_export_restriction"
    brazil_yield_shock: float = -0.15
    brazil_export_cut: float = 0.35
    description: str = ("Brazilian soybean yield -15 % and a Brazilian export restriction cutting "
                        "exports by 35 % of the baseline; trade friction makes part of the "
                        "restricted volume unavailable to the world market")

    def __post_init__(self) -> None:
        shock = float(self.brazil_yield_shock)
        cut = float(self.brazil_export_cut)
        if not math.isfinite(shock) or not (-1.0 < shock <= 0.0):
            raise AgriMarketError("brazil_yield_shock must be in (-1, 0]")
        if not math.isfinite(cut) or not (0.0 <= cut < 1.0):
            raise AgriMarketError("brazil_export_cut must be in [0, 1)")
        if not str(self.name).strip():
            raise AgriMarketError("scenario name must be non-empty")

    def shocks(self) -> tuple:
        return (ScenarioShock(kind="weather_yield_shock", target="Brazil", magnitude=float(
                    self.brazil_yield_shock)),
                ScenarioShock(kind="export_restriction", target="Brazil",
                              magnitude=float(self.brazil_export_cut)))

    def to_dict(self) -> dict:
        return {"name": self.name,
                "brazil_yield_shock": float(self.brazil_yield_shock),
                "brazil_export_cut": float(self.brazil_export_cut),
                "description": self.description,
                "shocks": [shock.to_dict() for shock in self.shocks()]}


def _pilot_result(*, config: SoyConfig, scenario: SoyScenario) -> AgriModuleResult:
    balance = build_global_balance(config.regions)
    brazil = config.regions["Brazil"]
    shocked = dict(config.regions)
    shocked["Brazil"] = brazil.with_yield_shock(scenario.brazil_yield_shock)
    shocked_balance = GlobalBalance(shocked)
    shocked_balance.validate()
    production_loss = balance.production_mt - shocked_balance.production_mt
    restricted_volume = shocked["Brazil"].exports_mt * float(scenario.brazil_export_cut)
    corners = {}
    for name in ("low", "high"):
        baseline_solve = solve_corner(
            balance=balance, shocked_balance=balance, restricted_volume_mt=0.0,
            parameters=config.parameters, corner=name)
        scenario_solve = solve_corner(
            balance=balance, shocked_balance=shocked_balance,
            restricted_volume_mt=restricted_volume, parameters=config.parameters, corner=name)
        relative = (scenario_solve["solver"].multiplier
                    / baseline_solve["solver"].multiplier)
        corners[name] = {"baseline": baseline_solve, "scenario": scenario_solve,
                         "relative_multiplier": relative}
    low, high = corners["low"], corners["high"]
    soybean_range = PriceRange(
        low=float(config.base_soy_price_usd_t) * float(low["relative_multiplier"]),
        high=float(config.base_soy_price_usd_t) * float(high["relative_multiplier"]),
        unit="USD/t", basis="scenario_range")
    # Soymeal follows the bean multiplier with the pre-declared meal-sensitivity band.
    meal_range = PriceRange(
        low=float(config.base_meal_price_usd_t) * float(low["relative_multiplier"]) * float(
            low["scenario"]["meal_sensitivity"]),
        high=float(config.base_meal_price_usd_t) * float(high["relative_multiplier"]) * float(
            high["scenario"]["meal_sensitivity"]),
        unit="USD/t", basis="scenario_range")
    freight_low = float(config.base_freight_usd_t) * (
        1.0 + float(low["scenario"]["freight_addon"]))
    freight_high = float(config.base_freight_usd_t) * (
        1.0 + float(high["scenario"]["freight_addon"]))
    africa_impact = compute_africa_impact(
        config=config.africa, soy_price_range=soybean_range, meal_price_range=meal_range,
        freight_low_usd_t=freight_low, freight_high_usd_t=freight_high,
        base_soy_price=float(config.base_soy_price_usd_t),
        base_meal_price=float(config.base_meal_price_usd_t))
    margins = []
    for bean_price, oil_price, meal_price in (
            (soybean_range.high, float(config.base_oil_price_usd_t) * float(
                low["relative_multiplier"]),
             meal_range.low),
            (soybean_range.low, float(config.base_oil_price_usd_t) * float(
                high["relative_multiplier"]),
             meal_range.high)):
        margins.append(crush_margin(bean_price_usd_t=bean_price,
                                    oil_price_usd_t=oil_price,
                                    meal_price_usd_t=meal_price,
                                    params=config.crush))
    margin_low = min(item["gross_margin_usd_t"] for item in margins)
    margin_high = max(item["gross_margin_usd_t"] for item in margins)
    effective_loss_low = (production_loss
                          + float(low["scenario"]["parameters"]["friction"]) * restricted_volume)
    effective_loss_high = (production_loss
                           + float(high["scenario"]["parameters"]["friction"]) * restricted_volume)
    effects = {
        "brazil_production_loss_mt": round(production_loss, 6),
        "brazil_restricted_exports_mt": round(restricted_volume, 6),
        "effective_supply_loss_mt": {"low": round(effective_loss_low, 6),
                                     "high": round(effective_loss_high, 6)},
        "price_multiplier_range": {"low": round(float(low["relative_multiplier"]), 6),
                                   "high": round(float(high["relative_multiplier"]), 6)},
        "freight_usd_t": {"low": round(freight_low, 6), "high": round(freight_high, 6)},
        "crush_margin_usd_t": {"low": round(margin_low, 6), "high": round(margin_high, 6)},
        "africa": africa_impact,
    }
    baseline_balance = balance.to_dict()
    scenario_balance = shocked_balance.to_dict()
    scenario_balance["stocks_to_use"] = round(
        shocked_balance.ending_stocks_mt / balance.use_mt, 6)
    scenario_balance["note"] = ("demand is held at the baseline level here; the price solve "
                                "applies the demand elasticity, see effects.price_multiplier_range")
    seasonality = describe_seasonality()
    seasonality["baseline_seasonal_path_usd_t"] = seasonal_path(float(config.base_soy_price_usd_t))
    notes = (
        "research prototype: the scenario is an assumption-based sensitivity, not a forecast",
        "price ranges are the two pre-declared corners of the assumption bounds; no point estimate",
        "Brazilian weather and export restriction are simultaneous shocks; friction loss is assumed",
        "Africa is one illustrative aggregate importer (import volumes and shares are assumptions)",
    )
    limitations = (
        "no PIT vintages and no real-data validation in this prototype",
        "levels/elasticities/import shares are rounded assumptions (source_class=assumption)",
        "reduced-form clearing: no storage arbitrage, futures, policy reaction or substitution "
        "across crops beyond the elasticity adjustment",
        "the export restriction is modelled as trade friction, not as a redistribution mechanism",
        "food-inflation effect is a pass-through assumption, not an estimated CPI response",
        "not an AMFE admission candidate: not connected to the protocol, registry or world core",
    )
    return AgriModuleResult(
        module_id=MODULE_ID, version=MODULE_VERSION,
        scenario={"name": scenario.name, "shocks": [shock.to_dict()
                                                    for shock in scenario.shocks()],
                  "parameters": config.parameters.to_dict()},
        price_ranges={"soybeans": soybean_range, "soymeal": meal_range},
        effects=effects,
        balance={"baseline": baseline_balance, "scenario": scenario_balance,
                 "identity_check": {
                     "baseline_residual_mt": baseline_balance["identity_residual_mt"],
                     "scenario_residual_mt": round(
                         shocked_balance.production_mt + shocked_balance.beginning_stocks_mt
                         - shocked_balance.use_mt - shocked_balance.ending_stocks_mt, 9)}},
        seasonality=seasonality,
        source_class="assumption", notes=notes, limitations=limitations,
        not_a_forecast=NOT_A_FORECAST)


class SoyAgriModule:
    """Default module instance: soy pilot over the illustrative configuration."""

    module_id = MODULE_ID
    version = MODULE_VERSION

    def __init__(self, config: SoyConfig | None = None,
                 scenario: SoyScenario | None = None) -> None:
        self.config = SoyConfig(regions=default_regions()) if config is None else config
        self.scenario = SoyScenario() if scenario is None else scenario
        if not isinstance(self.config, SoyConfig):
            raise AgriMarketError("config must be a SoyConfig")
        if not isinstance(self.scenario, SoyScenario):
            raise AgriMarketError("scenario must be a SoyScenario")

    def passport(self) -> dict:
        return module_passport()

    def validate(self) -> dict:
        balance = build_global_balance(self.config.regions)
        checks = {
            "regions_present": True,
            "brazil_present": "Brazil" in self.config.regions,
            "baseline_balance_clears": balance.ending_stocks_mt >= 0.0,
            "baseline_stocks_to_use": round(balance.stocks_to_use, 6),
            "crush_mass_within_one": sum((self.config.crush.oil_yield,
                                          self.config.crush.meal_yield,
                                          self.config.crush.loss_yield)) <= 1.0 + 1e-12,
            "source_class": "assumption",
            "not_a_forecast": True,
        }
        return checks

    def run(self, scenario: SoyScenario | None = None) -> AgriModuleResult:
        return _pilot_result(config=self.config,
                             scenario=self.scenario if scenario is None else scenario)


def module_passport() -> dict:
    """Module passport (docs/145 L2): identity, contract, dependencies, limitations, tests."""
    return {
        "module_id": MODULE_ID,
        "version": MODULE_VERSION,
        "display_name": "Agri archetype + soybean pilot (Africa)",
        "track": "C",
        "status": "research_prototype",
        "source_class": "assumption",
        "not_a_forecast": True,
        "contract": "AgriMarketModule (local fallback; optional amfe.markets framework)",
        "interfaces": ["passport()", "validate()", "run(scenario=None)",
                       "AgriModuleResult.to_dict()/to_json()/digest()"],
        "components": ["yield/weather", "seasonality", "stocks-to-use balance", "crush",
                       "substitutes", "export restrictions", "freight", "Africa projection"],
        "dependencies": ["numpy-free pure Python", "optional amfe.markets (try/except)"],
        "data_sources_template": "tools/agri_data.py",
        "limitations": [
            "assumption-based levels and elasticities; no real-data calibration yet",
            "no PIT vintages, no forecast evaluation, no admission",
            "single commodity (soy), single illustrative African aggregate",
        ],
        "tests": "tests/test_markets_agri.py",
        "docs": "docs/148_Агро_соя.md",
    }


def run_pilot(scenario: SoyScenario | None = None, config: SoyConfig | None = None) -> dict:
    """Convenience function returning the serialisable pilot result."""
    return SoyAgriModule(config=config, scenario=scenario).run().to_dict()


__all__ = ["SoyAgriModule", "SoyConfig", "SoyScenario", "default_regions", "module_passport",
           "run_pilot"]
