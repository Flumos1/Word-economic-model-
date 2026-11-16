"""Africa regional projection for the soybean pilot (docs/148).

Africa is treated as one illustrative aggregate importer (North/West/East/Southern Africa are not
modelled separately in this prototype).  The effect chain is::

    world soybean/soymeal price range + freight shock
        -> import unit cost range -> import bill range
        -> food-inflation effect range (soy share of the food basket x pass-through)

Every number is an assumption-based range, never a point estimate.  The pass-through is a
behavioural assumption and is not estimated here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import AgriMarketError, PriceRange
from .pricing import import_bill_range


@dataclass(frozen=True)
class AfricaConfig:
    """Illustrative African import profile (assumption, not official statistics)."""

    region_name: str = "Africa (aggregate, illustrative)"
    soybean_imports_mt: float = 4.0
    meal_imports_mt: float = 6.0
    baseline_freight_usd_t: float = 55.0
    import_margin: float = 1.05
    soy_food_basket_share_low: float = 0.005
    soy_food_basket_share_high: float = 0.012
    pass_through_low: float = 0.50
    pass_through_high: float = 0.90

    def __post_init__(self) -> None:
        if not str(self.region_name).strip():
            raise AgriMarketError("region_name must be non-empty")
        for name in ("soybean_imports_mt", "meal_imports_mt", "baseline_freight_usd_t"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise AgriMarketError(f"{name} must be finite and non-negative")
        margin = float(self.import_margin)
        if not math.isfinite(margin) or margin < 1.0:
            raise AgriMarketError("import_margin must be finite and >= 1")
        for name in ("soy_food_basket_share_low", "soy_food_basket_share_high"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not (0.0 <= value <= 1.0):
                raise AgriMarketError(f"{name} must be in [0, 1]")
        if self.soy_food_basket_share_low > self.soy_food_basket_share_high:
            raise AgriMarketError("soy basket share low must not exceed high")
        for name in ("pass_through_low", "pass_through_high"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not (0.0 <= value <= 1.0):
                raise AgriMarketError(f"{name} must be in [0, 1]")
        if self.pass_through_low > self.pass_through_high:
            raise AgriMarketError("pass-through low must not exceed high")

    def to_dict(self) -> dict:
        return {"region_name": self.region_name,
                "soybean_imports_mt": float(self.soybean_imports_mt),
                "meal_imports_mt": float(self.meal_imports_mt),
                "baseline_freight_usd_t": float(self.baseline_freight_usd_t),
                "import_margin": float(self.import_margin),
                "soy_food_basket_share": [float(self.soy_food_basket_share_low),
                                          float(self.soy_food_basket_share_high)],
                "pass_through": [float(self.pass_through_low),
                                 float(self.pass_through_high)]}


def _baseline_bill(config: AfricaConfig, *, base_soy_price: float,
                   base_meal_price: float) -> dict:
    beans = import_bill_range(
        volume_mt=config.soybean_imports_mt,
        price_range=PriceRange(low=base_soy_price, high=base_soy_price, basis="baseline"),
        freight_usd_t=config.baseline_freight_usd_t, import_margin=config.import_margin)
    meal = import_bill_range(
        volume_mt=config.meal_imports_mt,
        price_range=PriceRange(low=base_meal_price, high=base_meal_price, basis="baseline"),
        freight_usd_t=config.baseline_freight_usd_t, import_margin=config.import_margin)
    total = beans["bill_low_usd_mn"] + meal["bill_low_usd_mn"]
    if total <= 0.0:
        raise AgriMarketError("baseline African import bill must be positive")
    return {"beans": beans, "meal": meal, "total_usd_mn": total}


def compute_africa_impact(*, config: AfricaConfig, soy_price_range: PriceRange,
                          meal_price_range: PriceRange, freight_low_usd_t: float,
                          freight_high_usd_t: float, base_soy_price: float,
                          base_meal_price: float) -> dict:
    """Range of the import bill change and the food-inflation effect for the illustrative Africa."""
    baseline = _baseline_bill(config, base_soy_price=base_soy_price,
                              base_meal_price=base_meal_price)
    low_beans = import_bill_range(volume_mt=config.soybean_imports_mt,
                                  price_range=soy_price_range,
                                  freight_usd_t=float(freight_low_usd_t),
                                  import_margin=config.import_margin)
    low_meal = import_bill_range(volume_mt=config.meal_imports_mt,
                                 price_range=meal_price_range,
                                 freight_usd_t=float(freight_low_usd_t),
                                 import_margin=config.import_margin)
    high_beans = import_bill_range(
        volume_mt=config.soybean_imports_mt,
        price_range=PriceRange(low=soy_price_range.high, high=soy_price_range.high,
                               basis="scenario_high"),
        freight_usd_t=float(freight_high_usd_t), import_margin=config.import_margin)
    high_meal = import_bill_range(
        volume_mt=config.meal_imports_mt,
        price_range=PriceRange(low=meal_price_range.high, high=meal_price_range.high,
                               basis="scenario_high"),
        freight_usd_t=float(freight_high_usd_t), import_margin=config.import_margin)
    low_total = low_beans["bill_low_usd_mn"] + low_meal["bill_low_usd_mn"]
    high_total = high_beans["bill_high_usd_mn"] + high_meal["bill_high_usd_mn"]
    if low_total > high_total:
        raise AgriMarketError("Africa import bill corners are not ordered low <= high")
    baseline_total = baseline["total_usd_mn"]
    low_cost = low_beans["bill_low_usd_mn"] + low_meal["bill_low_usd_mn"]
    high_cost = high_beans["bill_high_usd_mn"] + high_meal["bill_high_usd_mn"]
    low_ratio = low_cost / baseline_total
    high_ratio = high_cost / baseline_total
    food_low = 100.0 * (low_ratio - 1.0) * float(config.soy_food_basket_share_low) * \
        float(config.pass_through_low)
    food_high = 100.0 * (high_ratio - 1.0) * float(config.soy_food_basket_share_high) * \
        float(config.pass_through_high)
    if low_ratio < 1.0 and high_ratio < 1.0:
        raise AgriMarketError("a negative supply shock cannot lower the import bill; check inputs")
    return {
        "region": config.region_name,
        "source_class": "assumption",
        "config": config.to_dict(),
        "baseline_import_bill_usd_mn": round(baseline_total, 6),
        "baseline_beans_usd_mn": round(baseline["beans"]["bill_low_usd_mn"], 6),
        "baseline_meal_usd_mn": round(baseline["meal"]["bill_low_usd_mn"], 6),
        "scenario_import_bill_usd_mn": {"low": round(low_cost, 6),
                                        "high": round(high_cost, 6)},
        "import_bill_change_usd_mn": {"low": round(low_cost - baseline_total, 6),
                                      "high": round(high_cost - baseline_total, 6)},
        "import_bill_change_pct": {"low": round(100.0 * (low_ratio - 1.0), 6),
                                   "high": round(100.0 * (high_ratio - 1.0), 6)},
        "food_inflation_pp_range": {"low": round(food_low, 6), "high": round(food_high, 6)},
        "unit_costs_usd_t": {
            "beans": {"low": low_beans["unit_cost_low_usd_t"],
                      "high": high_beans["unit_cost_high_usd_t"]},
            "meal": {"low": low_meal["unit_cost_low_usd_t"],
                     "high": high_meal["unit_cost_high_usd_t"]}},
        "notes": [
            "Africa is one illustrative aggregate importer; country heterogeneity is not modelled",
            "import volumes and shares are assumptions, not official trade statistics",
            "food-inflation effect = import-cost change x soy share of the food basket x "
            "pass-through (both assumption bounds)",
        ],
    }


__all__ = ["AfricaConfig", "compute_africa_impact"]
