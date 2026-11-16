"""Soybean/soymeal supply-use balance, crush and substitute mechanics (docs/148).

All levels are *illustrative assumptions* for the research prototype (``source_class="assumption"``),
not official USDA/FAO figures.  The balance identity is enforced::

    production + beginning_stocks = crush + other_use + ending_stocks

and the module refuses to continue if the resulting ending stocks are negative or if a region
exports more than it produces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Mapping

from .contracts import AgriMarketError


@dataclass(frozen=True)
class SoyRegion:
    """One soybean-producing region (million tonnes, million hectares)."""

    name: str
    area_mha: float
    yield_t_ha: float
    beginning_stocks_mt: float
    crush_mt: float
    other_use_mt: float
    exports_mt: float

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise AgriMarketError("region name must be non-empty")
        for field_name in ("area_mha", "yield_t_ha", "beginning_stocks_mt", "crush_mt",
                           "other_use_mt", "exports_mt"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value):
                raise AgriMarketError(f"{self.name}: {field_name} is not finite")
            if value < 0.0:
                raise AgriMarketError(f"{self.name}: {field_name} must be non-negative")
        if float(self.area_mha) <= 0.0 or float(self.yield_t_ha) <= 0.0:
            raise AgriMarketError(f"{self.name}: area and yield must be positive")

    @property
    def production_mt(self) -> float:
        return float(self.area_mha) * float(self.yield_t_ha)

    @property
    def use_mt(self) -> float:
        return float(self.crush_mt) + float(self.other_use_mt)

    def with_yield_shock(self, shock: float) -> "SoyRegion":
        """Apply a multiplicative yield shock (e.g. -0.15 = -15 % yield) to the region."""
        shock = float(shock)
        if not math.isfinite(shock) or not (-1.0 < shock <= 0.0):
            raise AgriMarketError("yield shock must be in (-1, 0]")
        return SoyRegion(name=self.name, area_mha=float(self.area_mha),
                         yield_t_ha=float(self.yield_t_ha) * (1.0 + shock),
                         beginning_stocks_mt=float(self.beginning_stocks_mt),
                         crush_mt=float(self.crush_mt), other_use_mt=float(self.other_use_mt),
                         exports_mt=float(self.exports_mt))

    def to_dict(self) -> dict:
        return {"name": self.name, "area_mha": round(float(self.area_mha), 6),
                "yield_t_ha": round(float(self.yield_t_ha), 6),
                "production_mt": round(self.production_mt, 6),
                "beginning_stocks_mt": round(float(self.beginning_stocks_mt), 6),
                "crush_mt": round(float(self.crush_mt), 6),
                "other_use_mt": round(float(self.other_use_mt), 6),
                "exports_mt": round(float(self.exports_mt), 6)}


def ensure_regions(regions: Mapping[str, SoyRegion]) -> Dict[str, SoyRegion]:
    if not isinstance(regions, Mapping) or not regions:
        raise AgriMarketError("regions must be a non-empty mapping name -> SoyRegion")
    clean: Dict[str, SoyRegion] = {}
    for key, region in regions.items():
        if not isinstance(region, SoyRegion):
            raise AgriMarketError(f"region {key!r} must be a SoyRegion")
        if key != region.name:
            raise AgriMarketError(f"region key {key!r} differs from name {region.name!r}")
        if region.exports_mt > region.production_mt + 1e-9:
            raise AgriMarketError(f"{region.name}: exports exceed production")
        clean[key] = region
    return clean


@dataclass(frozen=True)
class GlobalBalance:
    """Global soybean balance built from regional assumptions."""

    regions: Mapping[str, SoyRegion]

    def __post_init__(self) -> None:
        object.__setattr__(self, "regions", ensure_regions(self.regions))

    @property
    def production_mt(self) -> float:
        return math.fsum(region.production_mt for region in self.regions.values())

    @property
    def beginning_stocks_mt(self) -> float:
        return math.fsum(float(region.beginning_stocks_mt) for region in self.regions.values())

    @property
    def crush_mt(self) -> float:
        return math.fsum(float(region.crush_mt) for region in self.regions.values())

    @property
    def other_use_mt(self) -> float:
        return math.fsum(float(region.other_use_mt) for region in self.regions.values())

    @property
    def exports_mt(self) -> float:
        return math.fsum(float(region.exports_mt) for region in self.regions.values())

    @property
    def use_mt(self) -> float:
        return self.crush_mt + self.other_use_mt

    @property
    def available_mt(self) -> float:
        return self.production_mt + self.beginning_stocks_mt

    @property
    def ending_stocks_mt(self) -> float:
        return self.available_mt - self.use_mt

    @property
    def stocks_to_use(self) -> float:
        if self.use_mt <= 0.0:
            raise AgriMarketError("global use must be positive")
        return self.ending_stocks_mt / self.use_mt

    def validate(self) -> None:
        if self.ending_stocks_mt < -1e-9:
            raise AgriMarketError(
                f"negative ending stocks: {self.ending_stocks_mt:.3f} Mt (balance does not clear)")
        if self.use_mt <= 0.0:
            raise AgriMarketError("global use must be positive")
        if self.stocks_to_use < 0.0:
            raise AgriMarketError("stocks-to-use must be non-negative")

    def to_dict(self) -> dict:
        return {
            "regions": {name: region.to_dict() for name, region in self.regions.items()},
            "production_mt": round(self.production_mt, 6),
            "beginning_stocks_mt": round(self.beginning_stocks_mt, 6),
            "crush_mt": round(self.crush_mt, 6),
            "other_use_mt": round(self.other_use_mt, 6),
            "use_mt": round(self.use_mt, 6),
            "ending_stocks_mt": round(self.ending_stocks_mt, 6),
            "stocks_to_use": round(self.stocks_to_use, 6),
            "identity_residual_mt": round(
                self.production_mt + self.beginning_stocks_mt - self.use_mt
                - self.ending_stocks_mt, 9),
        }


def build_global_balance(regions: Mapping[str, SoyRegion]) -> GlobalBalance:
    balance = GlobalBalance(regions)
    balance.validate()
    return balance


@dataclass(frozen=True)
class CrushParams:
    """Soybean crush: 1 t of beans yields oil + meal + loss (mass must not exceed 1)."""

    oil_yield: float = 0.185
    meal_yield: float = 0.790
    loss_yield: float = 0.025

    def __post_init__(self) -> None:
        values = (float(self.oil_yield), float(self.meal_yield), float(self.loss_yield))
        if not all(math.isfinite(value) and value >= 0.0 for value in values):
            raise AgriMarketError("crush yields must be finite and non-negative")
        if sum(values) > 1.0 + 1e-12:
            raise AgriMarketError("crush yields (oil + meal + loss) must not exceed 1")

    def products(self, beans_mt: float) -> dict:
        beans = float(beans_mt)
        if not math.isfinite(beans) or beans < 0.0:
            raise AgriMarketError("crush volume must be finite and non-negative")
        return {"oil_mt": beans * float(self.oil_yield),
                "meal_mt": beans * float(self.meal_yield),
                "loss_mt": beans * float(self.loss_yield)}

    def to_dict(self) -> dict:
        return {"oil_yield": float(self.oil_yield), "meal_yield": float(self.meal_yield),
                "loss_yield": float(self.loss_yield)}


def crush_margin(*, bean_price_usd_t: float, oil_price_usd_t: float,
                 meal_price_usd_t: float, params: CrushParams | None = None) -> dict:
    """Gross crush margin per tonne of beans (USD/t); may be negative, which is reported."""
    params = CrushParams() if params is None else params
    prices = (float(bean_price_usd_t), float(oil_price_usd_t), float(meal_price_usd_t))
    if not all(math.isfinite(value) and value > 0.0 for value in prices):
        raise AgriMarketError("crush prices must be finite and positive")
    revenue = (float(params.oil_yield) * prices[1] + float(params.meal_yield) * prices[2])
    margin = revenue - prices[0]
    return {"bean_price_usd_t": round(prices[0], 6),
            "oil_price_usd_t": round(prices[1], 6),
            "meal_price_usd_t": round(prices[2], 6),
            "gross_margin_usd_t": round(margin, 6),
            "gross_margin_pct_of_bean": round(100.0 * margin / prices[0], 6)}


def effective_elasticity(elasticity: float, substitution_share: float) -> float:
    """Effective demand elasticity: substitutes make demand MORE price-responsive (switching)."""
    elasticity = float(elasticity)
    substitution = float(substitution_share)
    if not math.isfinite(elasticity) or elasticity <= 0.0:
        raise AgriMarketError("elasticity must be finite and positive")
    if not math.isfinite(substitution) or not (0.0 <= substitution < 1.0):
        raise AgriMarketError("substitution share must be in [0, 1)")
    return elasticity * (1.0 + substitution)


__all__ = ["CrushParams", "GlobalBalance", "SoyRegion", "build_global_balance", "crush_margin",
           "effective_elasticity", "ensure_regions"]
