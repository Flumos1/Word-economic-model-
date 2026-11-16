#!/usr/bin/env python3
"""G3: historical validation of the agri/soy prototype and its MRIO bridge (docs/163).

Research validation only: this tool takes four documented historical episodes, encodes their
*public-approximation* actual price changes and assumption-based shocks, runs the agri/soy model
(through its public components: balance -> solver -> price ranges -> Africa projection) and the
G2 MRIO bridge (``tools/agri_bridge.py``, read-only), and reports honest error metrics:

* **direction**: does the predicted price-change midpoint have the sign of the actual change?
* **range coverage**: does the actual price change fall inside the predicted range?
* **MAE / relative error**: |midpoint - actual| for soybeans and soymeal (the midpoint is used for
  scoring arithmetic only; the module itself never emits a point forecast);
* **naive comparison**: a "no shock" forecast (zero price change), i.e. the random-walk baseline;
* **where_worse**: episodes where the model loses to the naive baseline, misses the range or
  over/under-predicts the magnitude.

Episodes (public approximations, rounded; ``source_class="assumption"``):
  1. 2012 US drought (US soybean yield shock, actual = Jun->Sep 2012 price move);
  2. 2021-22 Brazil drought / global price surge (Brazil yield shock);
  3. 2008 Argentina export restrictions (Argentine export cut);
  4. 2022 Indonesia palm-oil export ban (substitute-demand shock for soy).

The tool never downloads data, never writes into ``src/`` or ``build/agri_bridge``, and makes no
point forecast.  Raw third-party data are not committed.  The verdict is explicitly *not* a
validation certificate: four episodes and assumption inputs cannot validate the model.

Usage::

    python tools/agri_validation.py                      # real bridge cache if present
    python tools/agri_validation.py --synthetic-bridge   # offline synthetic cost structure
    python tools/agri_validation.py --json --no-write
    python tools/agri_validation.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets_agri import (  # noqa: E402
    AgriMarketError, AgriModuleResult, GlobalBalance, PriceRange, ScenarioParameters,
    AfricaConfig, CrushParams, build_global_balance, compute_africa_impact, crush_margin,
    default_regions, describe_seasonality, effective_elasticity,
    seasonal_path, solve_price_multiplier,
)
from amfe.markets_agri.pricing import scenario_available_supply  # noqa: E402,F811
from amfe.markets_agri.soy import SoyConfig  # noqa: E402

import agri_bridge as bridge_tool  # noqa: E402  (G2 tool: read-only import, never modified)

SCHEMA = "AMFE-AGRI-VALIDATION/1.0.0"
NOT_A_FORECAST = True
DEFAULT_OUT = ROOT / "build" / "agri_validation"
EPISODE_SOURCE_CLASS = "assumption"


class AgriValidationError(ValueError):
    """Invalid episode definition, prediction or bridge input: fail closed."""


# ------------------------------------------------------------------------------------- episodes
@dataclass(frozen=True)
class Episode:
    """One historical episode: assumption inputs + public-approximation actual price changes."""

    episode_id: str
    name: str
    period: str
    yield_region: str | None
    yield_shock: float
    export_region: str | None
    export_cut: float
    demand_shock_mt: float
    base_soy_price_usd_t: float
    base_meal_price_usd_t: float
    actual_soy_change: float
    actual_meal_change: float
    source_note: str
    assumption_note: str

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or not self.name.strip() or not self.period.strip():
            raise AgriValidationError("episode id/name/period must be non-empty")
        if not self.source_note.strip():
            raise AgriValidationError(f"{self.episode_id}: source_note must explain the actuals")
        if not self.assumption_note.strip():
            raise AgriValidationError(f"{self.episode_id}: assumption_note is required")
        regions = set(default_regions())
        if self.yield_region is not None:
            if self.yield_region not in regions:
                raise AgriValidationError(f"{self.episode_id}: unknown yield region")
            shock = float(self.yield_shock)
            if not math.isfinite(shock) or not (-1.0 < shock <= 0.0):
                raise AgriValidationError(f"{self.episode_id}: yield shock must be in (-1, 0]")
        else:
            if abs(float(self.yield_shock)) > 1e-12:
                raise AgriValidationError(f"{self.episode_id}: yield shock without a region")
        if self.export_region is not None:
            if self.export_region not in regions:
                raise AgriValidationError(f"{self.episode_id}: unknown export region")
            cut = float(self.export_cut)
            if not math.isfinite(cut) or not (0.0 <= cut < 1.0):
                raise AgriValidationError(f"{self.episode_id}: export cut must be in [0, 1)")
        elif abs(float(self.export_cut)) > 1e-12:
            raise AgriValidationError(f"{self.episode_id}: export cut without a region")
        demand = float(self.demand_shock_mt)
        if not math.isfinite(demand) or demand < 0.0:
            raise AgriValidationError(f"{self.episode_id}: demand shock must be finite and >= 0")
        for name in ("base_soy_price_usd_t", "base_meal_price_usd_t"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise AgriValidationError(f"{self.episode_id}: {name} must be positive")
        for name in ("actual_soy_change", "actual_meal_change"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise AgriValidationError(f"{self.episode_id}: {name} must be finite")
            if abs(value) > 5.0:
                raise AgriValidationError(f"{self.episode_id}: {name} looks implausible (>500 %)")
        if not (self.yield_region or self.export_region or demand > 0.0):
            raise AgriValidationError(f"{self.episode_id}: at least one shock must be non-zero")

    def shocks(self) -> list:
        out = []
        if self.yield_region:
            out.append({"kind": "weather_yield_shock", "target": self.yield_region,
                        "magnitude": float(self.yield_shock)})
        if self.export_region:
            out.append({"kind": "export_restriction", "target": self.export_region,
                        "magnitude": float(self.export_cut)})
        if self.demand_shock_mt > 0.0:
            out.append({"kind": "substitute_demand_shock", "target": "world",
                        "magnitude": float(self.demand_shock_mt), "unit": "Mt"})
        return out

    def to_dict(self) -> dict:
        return {"episode_id": self.episode_id, "name": self.name, "period": self.period,
                "shocks": self.shocks(),
                "base_prices_usd_t": {"soybeans": float(self.base_soy_price_usd_t),
                                      "soymeal": float(self.base_meal_price_usd_t)},
                "actual_price_change": {"soybeans": round(float(self.actual_soy_change), 6),
                                        "soymeal": round(float(self.actual_meal_change), 6)},
                "source_class": EPISODE_SOURCE_CLASS,
                "source_note": self.source_note, "assumption_note": self.assumption_note}


EPISODES = (
    Episode(
        episode_id="2012_us_drought",
        name="2012 US drought",
        period="2012-06 .. 2012-09",
        yield_region="United States", yield_shock=-0.10,
        export_region=None, export_cut=0.0, demand_shock_mt=0.0,
        base_soy_price_usd_t=430.0, base_meal_price_usd_t=350.0,
        actual_soy_change=0.38, actual_meal_change=0.49,
        source_note=("public approximations (USDA WASDE crop reports; World Bank Pink Sheet "
                     "monthly soybeans/soymeal, rounded): Jun->Sep 2012 beans about +38 %, "
                     "meal about +49 %"),
        assumption_note="US soybean yield shock -10 % is an assumption, not a WASDE estimate",
    ),
    Episode(
        episode_id="2021_22_brazil_prices",
        name="2021-22 Brazil drought and global price surge",
        period="2021-01 .. 2022-06",
        yield_region="Brazil", yield_shock=-0.09,
        export_region=None, export_cut=0.0, demand_shock_mt=0.0,
        base_soy_price_usd_t=480.0, base_meal_price_usd_t=400.0,
        actual_soy_change=0.32, actual_meal_change=0.30,
        source_note=("public approximations (CONAB/USDA Brazil crop estimates; World Bank Pink "
                     "Sheet, rounded): Jan 2021 -> Jun 2022 beans about +32 %, meal about +30 %"),
        assumption_note=("the price move mixed drought, freight and demand; the model receives only "
                         "a -9 % Brazil yield shock, so the decomposition is an assumption"),
    ),
    Episode(
        episode_id="2008_argentina_export_restriction",
        name="2008 Argentina export restrictions",
        period="2008-03 .. 2008-06",
        yield_region=None, yield_shock=0.0,
        export_region="Argentina", export_cut=0.60, demand_shock_mt=0.0,
        base_soy_price_usd_t=480.0, base_meal_price_usd_t=380.0,
        actual_soy_change=0.15, actual_meal_change=0.18,
        source_note=("public approximations (CBOT/World Bank monthly prices, rounded): "
                     "Mar->Jun 2008 beans about +15 %, meal about +18 %"),
        assumption_note=("the Argentine export tax conflict is represented as a 60 % export cut "
                         "with trade friction; the historical policy mix is more complex"),
    ),
    Episode(
        episode_id="2022_indonesia_palm_ban",
        name="2022 Indonesia palm-oil export ban (substitute demand)",
        period="2022-04 .. 2022-05",
        yield_region=None, yield_shock=0.0,
        export_region=None, export_cut=0.0, demand_shock_mt=3.0,
        base_soy_price_usd_t=620.0, base_meal_price_usd_t=550.0,
        actual_soy_change=0.05, actual_meal_change=0.02,
        source_note=("public approximations (World Bank Pink Sheet, rounded): Apr->May 2022 beans "
                     "about +5 %, meal about +2 %; the ban mainly hit palm oil"),
        assumption_note=("palm-oil export ban is represented as a +3 Mt soybean-equivalent "
                         "substitute-demand shock; substitution elasticity is an assumption"),
    ),
)


# ------------------------------------------------------------------------------ model adapter
def _regions_after_yield_shock(episode: Episode) -> dict:
    regions = dict(default_regions())
    if episode.yield_region is not None:
        regions[episode.yield_region] = regions[episode.yield_region].with_yield_shock(
            episode.yield_shock)
    return regions


def episode_agri_result(episode: Episode,
                        config: SoyConfig | None = None) -> AgriModuleResult:
    """Run the soy module's public components with episode-specific region shocks.

    The fixed Brazil-only ``SoyAgriModule`` scenario is not modified; instead the same public
    building blocks (balance, solver, pricing bounds, Africa projection) are driven with the
    episode shocks.  This keeps the validated model identical in mechanics and avoids touching
    ``src/amfe/markets_agri``.
    """
    config = SoyConfig(regions=default_regions()) if config is None else config
    if not isinstance(episode, Episode):
        raise AgriValidationError("episode must be an Episode")
    base_regions = dict(default_regions())
    balance = build_global_balance(base_regions)
    shocked_balance = GlobalBalance(_regions_after_yield_shock(episode))
    shocked_balance.validate()
    production_loss = balance.production_mt - shocked_balance.production_mt
    restricted = 0.0
    if episode.export_region is not None:
        restricted = (float(shocked_balance.regions[episode.export_region].exports_mt)
                      * float(episode.export_cut))
    corners = {}
    for corner in ("low", "high"):
        selection = config.parameters.corner(corner)
        available = scenario_available_supply(
            balance=balance, shocked_balance=shocked_balance,
            restricted_volume_mt=restricted, friction=selection["friction"])
        elasticity = effective_elasticity(selection["elasticity"], selection["substitution"])
        scenario_solve = solve_price_multiplier(
            baseline_use_mt=balance.use_mt + float(episode.demand_shock_mt),
            available_supply_mt=available, target_stocks_use=selection["target_stocks_use"],
            elasticity=elasticity)
        baseline_solve = solve_price_multiplier(
            baseline_use_mt=balance.use_mt, available_supply_mt=balance.available_mt,
            target_stocks_use=selection["target_stocks_use"], elasticity=elasticity)
        relative = scenario_solve.multiplier / baseline_solve.multiplier
        if not math.isfinite(relative) or relative <= 0.0:
            raise AgriValidationError(f"{episode.episode_id}: invalid relative multiplier")
        corners[corner] = {"relative": relative, "available": available,
                           "selection": selection, "scenario": scenario_solve,
                           "baseline": baseline_solve}
    low, high = corners["low"], corners["high"]
    if low["relative"] > high["relative"]:
        raise AgriValidationError(f"{episode.episode_id}: corners are not ordered low <= high")
    soybean_range = PriceRange(low=episode.base_soy_price_usd_t * low["relative"],
                               high=episode.base_soy_price_usd_t * high["relative"],
                               unit="USD/t", basis="validation_range")
    meal_range = PriceRange(
        low=episode.base_meal_price_usd_t * low["relative"]
        * float(low["selection"]["meal_sensitivity"]),
        high=episode.base_meal_price_usd_t * high["relative"]
        * float(high["selection"]["meal_sensitivity"]),
        unit="USD/t", basis="validation_range")
    freight_low = config.base_freight_usd_t * (1.0 + float(low["selection"]["freight_addon"]))
    freight_high = config.base_freight_usd_t * (1.0 + float(high["selection"]["freight_addon"]))
    africa = compute_africa_impact(
        config=config.africa, soy_price_range=soybean_range, meal_price_range=meal_range,
        freight_low_usd_t=freight_low, freight_high_usd_t=freight_high,
        base_soy_price=episode.base_soy_price_usd_t,
        base_meal_price=episode.base_meal_price_usd_t)
    margins = []
    oil_base = float(config.base_oil_price_usd_t)
    for bean_price, oil_multiplier, meal_price in (
            (soybean_range.high, low["relative"], meal_range.low),
            (soybean_range.low, high["relative"], meal_range.high)):
        margins.append(crush_margin(bean_price_usd_t=bean_price,
                                    oil_price_usd_t=oil_base * oil_multiplier,
                                    meal_price_usd_t=meal_price, params=config.crush))
    effective_loss_low = production_loss + float(low["selection"]["friction"]) * restricted
    effective_loss_high = production_loss + float(high["selection"]["friction"]) * restricted
    effects = {
        "production_loss_mt": round(production_loss, 6),
        "restricted_exports_mt": round(restricted, 6),
        "demand_shock_mt": round(float(episode.demand_shock_mt), 6),
        "effective_supply_loss_mt": {"low": round(effective_loss_low, 6),
                                     "high": round(effective_loss_high, 6)},
        "price_multiplier_range": {"low": round(low["relative"], 6),
                                   "high": round(high["relative"], 6)},
        "freight_usd_t": {"low": round(freight_low, 6), "high": round(freight_high, 6)},
        "crush_margin_usd_t": {"low": round(min(m["gross_margin_usd_t"] for m in margins), 6),
                               "high": round(max(m["gross_margin_usd_t"] for m in margins), 6)},
        "africa": africa,
    }
    scenario = {"name": episode.episode_id, "shocks": episode.shocks(),
                "parameters": config.parameters.to_dict()}
    balance_payload = balance.to_dict()
    scenario_payload = shocked_balance.to_dict()
    scenario_payload["stocks_to_use"] = round(
        max(shocked_balance.ending_stocks_mt, 0.0) / max(balance.use_mt, 1e-9), 6)
    seasonality = describe_seasonality()
    seasonality["baseline_seasonal_path_usd_t"] = seasonal_path(episode.base_soy_price_usd_t)
    return AgriModuleResult(
        module_id="AGRI_SOY_VALIDATION", version="0.1.0", scenario=scenario,
        price_ranges={"soybeans": soybean_range, "soymeal": meal_range},
        effects=effects,
        balance={"baseline": balance_payload, "scenario": scenario_payload,
                 "identity_check": {
                     "baseline_residual_mt": balance_payload["identity_residual_mt"],
                     "scenario_residual_mt": round(
                         shocked_balance.production_mt + shocked_balance.beginning_stocks_mt
                         - shocked_balance.use_mt - shocked_balance.ending_stocks_mt, 9)}},
        seasonality=seasonality, source_class="assumption",
        notes=("episode validation run: assumption-based shocks and public-approximation actuals",
               "the midpoint of the range is used only for MAE scoring, never as a forecast"),
        limitations=("research prototype; not an admission candidate",
                     "four episodes cannot validate the model"),
        not_a_forecast=NOT_A_FORECAST)


class EpisodeAgriModule:
    """Adapter exposing the module contract required by ``agri_bridge.run_scenario``."""

    module_id = "AGRI_SOY_VALIDATION"
    version = "0.1.0"

    def __init__(self, episode: Episode, config: SoyConfig | None = None) -> None:
        self.episode = episode
        self._result = episode_agri_result(episode, config)
        base = config if config is not None else SoyConfig(regions=default_regions())
        self.config = SimpleNamespace(base_soy_price_usd_t=episode.base_soy_price_usd_t,
                                      base_meal_price_usd_t=episode.base_meal_price_usd_t,
                                      base_oil_price_usd_t=base.base_oil_price_usd_t)

    def passport(self) -> dict:
        return {"module_id": self.module_id, "version": self.version,
                "status": "research_validation", "source_class": "assumption",
                "not_a_forecast": True}

    def validate(self) -> dict:
        return {"episode": self.episode.episode_id, "shocks": self.episode.shocks(),
                "not_a_forecast": True}

    def run(self) -> AgriModuleResult:
        return self._result


# ------------------------------------------------------------------------------------- scoring
def _change_range(price_range: dict, base_price: float) -> dict:
    low = float(price_range["low"]) / float(base_price) - 1.0
    high = float(price_range["high"]) / float(base_price) - 1.0
    return {"low": low, "high": high, "midpoint": 0.5 * (low + high)}


def score_prediction(*, predicted_low: float, predicted_high: float,
                     actual: float) -> dict:
    """Direction/coverage/MAE scoring for one commodity of one episode."""
    values = (float(predicted_low), float(predicted_high), float(actual))
    if not all(math.isfinite(value) for value in values):
        raise AgriValidationError("scoring inputs must be finite")
    if predicted_low > predicted_high:
        raise AgriValidationError("predicted range must satisfy low <= high")
    midpoint = 0.5 * (predicted_low + predicted_high)
    if abs(actual) < 1e-12:
        direction_correct = abs(midpoint) < 1e-12
    else:
        direction_correct = (midpoint > 0.0) == (actual > 0.0)
    covered = predicted_low - 1e-12 <= actual <= predicted_high + 1e-12
    model_error = abs(midpoint - actual)
    naive_error = abs(actual)
    relative_error = model_error / abs(actual) if abs(actual) > 1e-12 else None
    return {"predicted_low": round(predicted_low, 6),
            "predicted_high": round(predicted_high, 6),
            "predicted_midpoint": round(midpoint, 6),
            "actual": round(actual, 6),
            "direction_correct": bool(direction_correct),
            "covered": bool(covered),
            "midpoint_error": round(model_error, 6),
            "naive_error": round(naive_error, 6),
            "relative_error": (round(relative_error, 6) if relative_error is not None else None),
            "better_than_naive": bool(model_error < naive_error - 1e-12),
            "bias": round(midpoint - actual, 6),
            "midpoint_used_for_scoring_only": True}


def bridge_block(module: EpisodeAgriModule, structure) -> dict:
    """Run the G2 bridge for one episode (read-only import; failures are recorded, not hidden)."""
    try:
        scenario = bridge_tool.run_scenario(module=module)
        report = bridge_tool.run_bridge(
            structure, scenario=scenario,
            base_soy_price=module.episode.base_soy_price_usd_t,
            base_meal_price=module.episode.base_meal_price_usd_t)
    except Exception as exc:  # noqa: BLE001 - bridge failure must be reported, not fatal
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "digest": report.get("digest"),
            "soybean_change_pct": report["prices"]["soybeans"]["change_pct"],
            "soymeal_change_pct": report["prices"]["soymeal"]["change_pct"],
            "food_cpi_pp": report["consumer_food_prices"]["range_pp"],
            "africa_import_bill_change_usd_mn": report["africa"]["import_bill_change_usd_mn"],
            "producer_price_change_pct": report["leontief"]["producer_price_change_pct"],
            "included_in_world_totals": report["africa"]["included_in_world_totals"]}


def evaluate_episodes(episodes=EPISODES, *, structure=None,
                      config: SoyConfig | None = None) -> dict:
    """Run every episode through the agri model (+bridge when a structure is supplied)."""
    if not episodes:
        raise AgriValidationError("at least one episode is required")
    rows = []
    for episode in episodes:
        if not isinstance(episode, Episode):
            raise AgriValidationError("episodes must be Episode instances")
        module = EpisodeAgriModule(episode, config)
        result = module.run()
        payload = result.to_dict()
        predicted = {
            "soybeans": _change_range(payload["price_ranges"]["soybeans"],
                                      episode.base_soy_price_usd_t),
            "soymeal": _change_range(payload["price_ranges"]["soymeal"],
                                     episode.base_meal_price_usd_t),
        }
        metrics = {
            "soybeans": score_prediction(predicted_low=predicted["soybeans"]["low"],
                                          predicted_high=predicted["soybeans"]["high"],
                                          actual=episode.actual_soy_change),
            "soymeal": score_prediction(predicted_low=predicted["soymeal"]["low"],
                                        predicted_high=predicted["soymeal"]["high"],
                                        actual=episode.actual_meal_change),
        }
        row = {"episode": episode.to_dict(),
               "predicted_change": {name: {key: round(float(value), 6)
                                           for key, value in interval.items()}
                                    for name, interval in predicted.items()},
               "metrics": metrics,
               "effects": {key: payload["effects"][key] for key in
                           ("production_loss_mt", "restricted_exports_mt",
                            "demand_shock_mt", "effective_supply_loss_mt",
                            "price_multiplier_range")},
               "agri_digest": result.digest(),
               "bridge": bridge_block(module, structure) if structure is not None else
               {"available": False, "error": "no bridge structure supplied"}}
        rows.append(row)
    cells = [(row["metrics"][commodity], commodity, row["episode"]["episode_id"])
             for row in rows for commodity in ("soybeans", "soymeal")]
    n = len(cells)
    direction_hits = sum(1 for metric, _, _ in cells if metric["direction_correct"])
    coverage_hits = sum(1 for metric, _, _ in cells if metric["covered"])
    model_errors = [metric["midpoint_error"] for metric, _, _ in cells]
    naive_errors = [metric["naive_error"] for metric, _, _ in cells]
    biases = [metric["bias"] for metric, _, _ in cells]
    where_worse = []
    for metric, commodity, episode_id in cells:
        if not metric["better_than_naive"]:
            where_worse.append({"episode_id": episode_id, "commodity": commodity,
                               "reason": "model worse than naive"})
        elif not metric["covered"]:
            where_worse.append({"episode_id": episode_id, "commodity": commodity,
                               "reason": "range missed"})
    by_commodity = {}
    for commodity in ("soybeans", "soymeal"):
        subset = [row["metrics"][commodity] for row in rows]
        by_commodity[commodity] = {
            "n": len(subset),
            "direction_hits": sum(1 for metric in subset if metric["direction_correct"]),
            "coverage_hits": sum(1 for metric in subset if metric["covered"]),
            "mean_midpoint_error": round(
                sum(metric["midpoint_error"] for metric in subset) / len(subset), 6),
            "mean_naive_error": round(
                sum(metric["naive_error"] for metric in subset) / len(subset), 6),
            "mean_bias": round(sum(metric["bias"] for metric in subset) / len(subset), 6)}
    model_mae = sum(model_errors) / n
    naive_mae = sum(naive_errors) / n
    bias = sum(biases) / n
    coverage_rate = coverage_hits / n
    direction_rate = direction_hits / n
    bridge_runs = [row["bridge"] for row in rows if row["bridge"].get("available")]
    aggregate = {
        "n_episodes": len(rows),
        "n_commodity_cells": n,
        "direction_hits": direction_hits,
        "direction_rate": round(direction_rate, 6),
        "coverage_hits": coverage_hits,
        "coverage_rate": round(coverage_rate, 6),
        "model_mae": round(model_mae, 6),
        "naive_mae": round(naive_mae, 6),
        "mae_improvement_vs_naive": round(naive_mae - model_mae, 6),
        "mean_bias": round(bias, 6),
        "cells_missed_range": sum(1 for metric, _, _ in cells if not metric["covered"]),
        "cells_worse_than_naive": sum(1 for metric, _, _ in cells
                                      if not metric["better_than_naive"]),
        "by_commodity": by_commodity,
        "bridge_runs": len(bridge_runs),
        "bridge_errors": len(rows) - len(bridge_runs),
    }
    if coverage_rate >= 0.75 and direction_rate >= 0.75 and model_mae < naive_mae:
        status = "PARTIAL_SUPPORT"
    elif model_mae < naive_mae and direction_rate >= 0.75:
        status = "WEAK_SUPPORT_MAE_ONLY"
    else:
        status = "NOT_VALIDATED"
    verdict = {
        "status": status,
        "validated": False,
        "claim": "no forecast-accuracy or admission claim is made",
        "summary": ("all episodes are supply-negative, so direction is easy and not discriminative; "
                    "the ranges are too narrow / magnitudes are under-predicted, so coverage is "
                    "low; MAE can improve on the zero-change naive only by predicting a small "
                    "positive change"),
        "caveats": [
            "four episodes, public-approximation actuals, assumption-based shocks: not a validation",
            "no point-in-time data, no re-estimation, no cross-episode holdout",
            "the MAE midpoint is scoring arithmetic only; the module emits ranges, not points",
        ],
    }
    report = {
        "schema": SCHEMA,
        "generated_at": None,  # deliberately absent: the tool is clock-free and deterministic
        "not_a_forecast": NOT_A_FORECAST,
        "source_class": EPISODE_SOURCE_CLASS,
        "episodes": rows,
        "aggregate": aggregate,
        "where_worse": where_worse,
        "verdict": verdict,
        "limitations": [
            "actual price changes are rounded public approximations, not licensed PIT series",
            "shock sizes (yield/export/demand) are assumptions, not estimated decompositions",
            "the soy module itself is an assumption-based research prototype (docs/148)",
            "four episodes cannot support calibration; no cross-validation or OOS",
            "the bridge adds an MRIO price channel, not a quantity/GDP channel",
            "not an admission candidate: no registry, no protocol, no claims",
        ],
    }
    report["digest"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return report


# ------------------------------------------------------------------------------------- output
def render_markdown(report: dict) -> str:
    lines = ["# Agri/soy historical validation (G3, docs/163)", "",
             f"Schema `{report['schema']}` · digest `{report['digest']}`", "",
             "Research validation only: no point forecast, no admission claim.", "",
             "## Episodes", "",
             "| Episode | Period | Shock(s) | Soy Δ predicted / actual | Meal Δ predicted / actual | "
             "Coverage |",
             "|---|---|---|---|---|---|"]
    for row in report["episodes"]:
        episode = row["episode"]
        soy = row["metrics"]["soybeans"]
        meal = row["metrics"]["soymeal"]
        shocks = "; ".join(
            f"{item['target']}: {item['kind']} {item['magnitude']:+.2f}"
            for item in episode["shocks"])
        lines.append(
            f"| {episode['name']} | {episode['period']} | {shocks} | "
            f"{100 * soy['predicted_low']:+.1f}…{100 * soy['predicted_high']:+.1f} % / "
            f"{100 * soy['actual']:+.1f} % | "
            f"{100 * meal['predicted_low']:+.1f}…{100 * meal['predicted_high']:+.1f} % / "
            f"{100 * meal['actual']:+.1f} % | "
            f"soy {'yes' if soy['covered'] else 'NO'}, meal "
            f"{'yes' if meal['covered'] else 'NO'} |")
    aggregate = report["aggregate"]
    lines += ["", "## Metrics", "",
              f"- direction: {aggregate['direction_hits']}/{aggregate['n_commodity_cells']} "
              f"({100 * aggregate['direction_rate']:.1f} %) — all episodes are supply-negative, so "
              "this is easy and not discriminative;",
              f"- range coverage: {aggregate['coverage_hits']}/"
              f"{aggregate['n_commodity_cells']} ({100 * aggregate['coverage_rate']:.1f} %);",
              f"- MAE midpoint: {aggregate['model_mae']:.3f} vs naive zero-change "
              f"{aggregate['naive_mae']:.3f} (improvement "
              f"{aggregate['mae_improvement_vs_naive']:+.3f} п.п.);",
              f"- mean bias: {aggregate['mean_bias']:+.3f} п.п. "
              "(negative = model under-predicts the price increase).", "",
              "### By commodity", "",
              "| Commodity | direction | coverage | MAE model | MAE naive | mean bias |",
              "|---|---|---|---|---|---|"]
    for commodity, values in aggregate["by_commodity"].items():
        lines.append(f"| {commodity} | {values['direction_hits']}/{values['n']} | "
                     f"{values['coverage_hits']}/{values['n']} | "
                     f"{values['mean_midpoint_error']:.3f} | {values['mean_naive_error']:.3f} | "
                     f"{values['mean_bias']:+.3f} |")
    lines += ["", "## Where the model is worse / misses", ""]
    if report["where_worse"]:
        for item in report["where_worse"]:
            lines.append(f"- {item['episode_id']} / {item['commodity']}: {item['reason']}")
    else:
        lines.append("- none vs naive, but check coverage and magnitude")
    lines += ["", "## Verdict", "",
              f"**{report['verdict']['status']}** — {report['verdict']['summary']}", "",
              "No forecast-accuracy or admission claim is made.", "", "## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict, out_dir=DEFAULT_OUT) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    json_path = out / "agri_validation.json"
    md_path = out / "agri_validation.md"
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return {"json": str(json_path).replace("\\", "/"),
            "markdown": str(md_path).replace("\\", "/"),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": len(payload.encode("utf-8"))}


def self_test() -> dict:
    structure = bridge_tool.synthetic_cost_structure()
    report = evaluate_episodes(EPISODES[:2], structure=structure)
    checks = {
        "episodes_present": len(report["episodes"]) == 2,
        "not_a_forecast": report["not_a_forecast"] is True,
        "ranges_ordered": all(row["predicted_change"][commodity]["low"]
                              <= row["predicted_change"][commodity]["high"]
                              for row in report["episodes"]
                              for commodity in ("soybeans", "soymeal")),
        "bridge_ran": all(row["bridge"].get("available") for row in report["episodes"]),
        "no_point_estimate": all(not row["bridge"].get("point_estimate", False)
                                 for row in report["episodes"]),
        "digest_hex": len(report["digest"]) == 64,
        "verdict_not_validated": report["verdict"]["validated"] is False,
    }
    return {"schema": "AMFE-AGRI-VALIDATION-SELFTEST/1.0.0", "ok": all(checks.values()),
            "checks": checks, "digest": report["digest"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G3 historical validation of the agri/soy module (2012 US drought, 2021-22, "
                    "Argentina export restrictions, Indonesia palm ban); research only")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--synthetic-bridge", action="store_true",
                        help="use the deterministic synthetic cost structure instead of the cache")
    parser.add_argument("--no-write", action="store_true", help="compute only, write nothing")
    parser.add_argument("--json", action="store_true", help="print the full report to stdout")
    parser.add_argument("--self-test", action="store_true",
                        help="run the synthetic self-test and print the summary")
    return parser


def _structure(use_synthetic: bool):
    if use_synthetic:
        return bridge_tool.synthetic_cost_structure(), "synthetic"
    try:
        structure, origin = bridge_tool.load_or_build_structure()
        return structure, origin
    except Exception:  # noqa: BLE001 - missing cache/dataset falls back to synthetic, disclosed
        return bridge_tool.synthetic_cost_structure(), "synthetic_fallback"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        payload = self_test()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1))
        return 0 if payload["ok"] else 1
    try:
        structure, origin = _structure(args.synthetic_bridge)
        report = evaluate_episodes(EPISODES, structure=structure)
        report["data"] = {"bridge_structure_origin": origin,
                          "actuals_source_class": "assumption",
                          "raw_data_committed": False}
        report["digest"] = hashlib.sha256(
            json.dumps(report, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        written = {} if args.no_write else write_report(report, args.out)
        summary = {"status": "OK", "schema": report["schema"], "digest": report["digest"],
                   "bridge_structure_origin": origin, "written": written,
                   "aggregate": report["aggregate"], "verdict": report["verdict"],
                   "where_worse": report["where_worse"]}
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    except (AgriValidationError, AgriMarketError, bridge_tool.BridgeError) as error:
        print(json.dumps({"status": "BLOCKED_VALIDATION", "error": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
