"""Gas / LNG market module: European hub shock, LNG diversion and winter scenarios (docs/188, UC-24).

This is the block-9 gas MVP: a **research prototype** that runs the existing market framework
(:mod:`amfe.markets.contract`, :mod:`amfe.markets.engine`) on gas and LNG arithmetic.  It is not a
forecast, not an admission, not investment advice and not a trading signal.

What the module does
--------------------
* **Three hubs, three quote conventions.**  Henry Hub (``USD/MMBtu``), TTF (``EUR/MWh``) and JKM
  (``USD/MMBtu``) are declared with their native units; every result is reported in both
  ``USD/MMBtu`` and ``EUR/MWh``.  Conversions are explicit functions with fail-closed unit handling
  (:func:`convert_price`, :func:`convert_flow`), not hidden constants.
* **Balances, not single prices.**  The European balance is built from declared supply components
  (domestic, Norway pipeline, other pipeline, Russian pipeline, LNG) and demand in ``bcm/y``; the
  identity must close *before* a scenario runs, otherwise the run fails closed.  Global LNG trade is
  a second, separate identity: the diversion is a **transfer** (Europe +x, Asia -x, total constant),
  which the module checks instead of pretending the volume appears from nowhere.
* **Scenarios.**  ``eu_pipeline_shock_6m`` (loss of pipeline gas, no diversion), the same shock with
  a diversion ``eu_pipeline_shock_lng_diversion_6m`` at the **arbitrage-clearing** volume (solved by
  deterministic bisection: ``TTF - JKM = freight + regas``), ``soft_winter_6m``, ``hard_winter_6m``
  and the combined ``hard_winter_pipeline_shock_6m``.
* **Storage rule, declared.**  European storage is a monthly release plan bounded by deliverability,
  by the starting stock and by a declared security minimum; a declared share of the physical gap is
  delivered from storage, the rest is closed by price.  This is an assumption, not a gas-storage
  optimisation (see the limits in docs/188).
* **Sensitivity as a range.**  The reported TTF/JKM ranges are the envelope of the declared
  variants (elasticity, storage cover, diversion capacity/ramp, risk premium, a 9-month loss), not a
  confidence interval.

Reuse, not a second engine
--------------------------
The monthly identity, the bisection price clearing and the elasticity ramp are
:func:`amfe.markets.engine.simulate`; the module only converts units, builds the shock vectors, adds
the Asia calibration and the arbitrage solver.  Gas flows enter the engine in ``mboe/d`` (engine unit
``mb/d``), one step = one month; annual ``bcm/y`` values are the module's native unit and are what the
summary reports.

Honest limits (also in docs/188)
--------------------------------
* There is **no full gas balance**: no country-level storage per member state, no weather (HDD/CDD),
  no pipeline network topology, no contract take-or-pay, no coal-to-gas switching, no LNG fleet.
* Henry Hub is **not modelled**: the HH figure is a declared reference and the "netback" is
  arithmetic on TTF, not a second market clearing.
* Every input is an ``assumption`` with ``replace_with``; nothing is ``observed``: no third-party gas
  series is read or stored.  The one ``derived`` class covers values computed inside the module.
* Research prototype: ``RESEARCH_ONLY``.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from .contract import (
    Assumption,
    BalanceError,
    MarketError,
    MarketModule,
    MarketResult,
    Scenario,
    ScenarioError,
    UnitError,
    RESEARCH_ONLY,
)
from .engine import MarketCalibration, MarketPath, Shock, price_range, ramp, simulate

MODULE_ID = "gas.lng_eu"
COMMODITY = "Natural gas / LNG (TTF, JKM, Henry Hub)"
UNITS = "USD/MMBtu"
SCHEMA_REPORT = "amfe.markets.gas_report.v1"

DAYS_PER_MONTH = 30.4375
DAYS_PER_YEAR = 365.0
#: Cubic feet in one billion cubic metres (35.3147 bcf per bcm).
CF_PER_BCM = 35.3147e9
BTU_PER_CF = 1037.0
BTU_PER_MMBTU = 1.0e6
#: One megawatt-hour in million British thermal units.
MMBTU_PER_MWH = 3.4121416
#: 1 bcm of gas in million barrels of oil equivalent, energy convention (1 boe = 5.8 MMBtu).
BOE_MMBTU = 5.8

PRICE_UNITS = ("USD/MMBtu", "EUR/MWh", "USD/Mcf")
FLOW_UNITS = ("bcm/y", "bcm/month", "bcm/day", "mboe/day", "mmbtu/day")
ARBITRAGE_ITERATIONS = 32
ARBITRAGE_TOLERANCE = 0.05  # USD/MMBtu


class GasUnitError(UnitError):
    """Incompatible gas/LNG unit (fail-closed, never a silent coercion)."""


# ------------------------------------------------------------------------------------- helpers
def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GasUnitError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise GasUnitError(f"{name} must be finite, got {value!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise GasUnitError(f"{name} must be positive, got {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise GasUnitError(f"{name} must be non-negative, got {value!r}")
    return number


def _q(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _sha256_json(payload: Mapping) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------- unit system
def bcm_to_mmbtu(bcm: float, *, btu_per_cf: float = BTU_PER_CF) -> float:
    """Energy content of ``bcm`` billion cubic metres in million BTU."""
    return (_non_negative(bcm, "bcm") * CF_PER_BCM * _positive(btu_per_cf, "btu_per_cf")
            / BTU_PER_MMBTU)


def mmbtu_to_bcm(mmbtu: float, *, btu_per_cf: float = BTU_PER_CF) -> float:
    return (_non_negative(mmbtu, "mmbtu") * BTU_PER_MMBTU
            / (CF_PER_BCM * _positive(btu_per_cf, "btu_per_cf")))


def bcm_to_mboe(bcm: float, *, btu_per_cf: float = BTU_PER_CF,
                mmbtu_per_boe: float = BOE_MMBTU) -> float:
    """``bcm`` in **million** barrels of oil equivalent (energy convention, 1 boe = 5.8 MMBtu).

    One billion cubic metres is about 36.62 million MMBtu, i.e. about 6.31 Mboe.
    """
    return (bcm_to_mmbtu(bcm, btu_per_cf=btu_per_cf)
            / _positive(mmbtu_per_boe, "mmbtu_per_boe") / 1.0e6)


def mboe_to_bcm(mboe: float, *, btu_per_cf: float = BTU_PER_CF,
                mmbtu_per_boe: float = BOE_MMBTU) -> float:
    return mmbtu_to_bcm(_non_negative(mboe, "mboe") * 1.0e6
                        * _positive(mmbtu_per_boe, "mmbtu_per_boe"),
                        btu_per_cf=btu_per_cf)


def bcm_y_to_mb_d(bcm_y: float, *, btu_per_cf: float = BTU_PER_CF,
                  mmbtu_per_boe: float = BOE_MMBTU) -> float:
    """An annual gas flow in ``bcm/y`` as the engine flow unit ``mb/d`` (million boe per day)."""
    return bcm_to_mboe(bcm_y, btu_per_cf=btu_per_cf, mmbtu_per_boe=mmbtu_per_boe) / DAYS_PER_YEAR


def mb_d_to_bcm_y(rate: float, *, btu_per_cf: float = BTU_PER_CF,
                  mmbtu_per_boe: float = BOE_MMBTU) -> float:
    return mboe_to_bcm(_non_negative(rate, "rate") * DAYS_PER_YEAR, btu_per_cf=btu_per_cf,
                       mmbtu_per_boe=mmbtu_per_boe)


def bcm_day_to_mb_d(bcm_day: float, *, btu_per_cf: float = BTU_PER_CF,
                    mmbtu_per_boe: float = BOE_MMBTU) -> float:
    """A daily gas rate in ``bcm/day`` as ``mb/d``."""
    return bcm_to_mboe(bcm_day, btu_per_cf=btu_per_cf, mmbtu_per_boe=mmbtu_per_boe)


def bcm_y_to_bcm_month(bcm_y: float, *, days: float = DAYS_PER_MONTH) -> float:
    """An annual flow in ``bcm/y`` as a monthly volume in ``bcm`` (30.4375-day month)."""
    return _non_negative(bcm_y, "bcm_y") / DAYS_PER_YEAR * _positive(days, "days")


def eur_per_mwh_to_usd_per_mmbtu(price_eur_per_mwh: float, eur_usd: float, *,
                                 mmbtu_per_mwh: float = MMBTU_PER_MWH) -> float:
    """``EUR/MWh`` -> ``USD/MMBtu``: one MWh is ``MMBTU_PER_MWH`` MMBtu."""
    return (_non_negative(price_eur_per_mwh, "price_eur_per_mwh") * _positive(eur_usd, "eur_usd")
            / _positive(mmbtu_per_mwh, "mmbtu_per_mwh"))


def usd_per_mmbtu_to_eur_per_mwh(price_usd_per_mmbtu: float, eur_usd: float, *,
                                 mmbtu_per_mwh: float = MMBTU_PER_MWH) -> float:
    return (_non_negative(price_usd_per_mmbtu, "price_usd_per_mmbtu")
            * _positive(mmbtu_per_mwh, "mmbtu_per_mwh") / _positive(eur_usd, "eur_usd"))


def usd_per_mmbtu_to_usd_per_mcf(price_usd_per_mmbtu: float, *,
                                 btu_per_cf: float = BTU_PER_CF) -> float:
    """``USD/MMBtu`` -> ``USD/Mcf`` (Henry Hub convention: 1 Mcf = 1,000 cf)."""
    return (_non_negative(price_usd_per_mmbtu, "price") * _positive(btu_per_cf, "btu_per_cf")
            / 1000.0)


def usd_per_mcf_to_usd_per_mmbtu(price_usd_per_mcf: float, *,
                                 btu_per_cf: float = BTU_PER_CF) -> float:
    return (_non_negative(price_usd_per_mcf, "price") * 1000.0
            / _positive(btu_per_cf, "btu_per_cf"))


def convert_price(value: float, from_unit: str, to_unit: str, *, eur_usd: float,
                  btu_per_cf: float = BTU_PER_CF) -> float:
    """Convert a gas price between the supported quote units (fail-closed on unknown units)."""
    if from_unit not in PRICE_UNITS:
        raise GasUnitError(f"unknown price unit {from_unit!r}; allowed {list(PRICE_UNITS)}")
    if to_unit not in PRICE_UNITS:
        raise GasUnitError(f"unknown price unit {to_unit!r}; allowed {list(PRICE_UNITS)}")
    price = _non_negative(value, "price")
    eur_usd = _positive(eur_usd, "eur_usd")
    if from_unit == to_unit:
        return price
    # canonical intermediate: USD/MMBtu
    if from_unit == "USD/MMBtu":
        usd = price
    elif from_unit == "EUR/MWh":
        usd = eur_per_mwh_to_usd_per_mmbtu(price, eur_usd)
    else:  # USD/Mcf
        usd = usd_per_mcf_to_usd_per_mmbtu(price, btu_per_cf=btu_per_cf)
    if to_unit == "USD/MMBtu":
        return usd
    if to_unit == "EUR/MWh":
        return usd_per_mmbtu_to_eur_per_mwh(usd, eur_usd)
    return usd_per_mmbtu_to_usd_per_mcf(usd, btu_per_cf=btu_per_cf)


def convert_flow(value: float, from_unit: str, to_unit: str, *, btu_per_cf: float = BTU_PER_CF,
                 mmbtu_per_boe: float = BOE_MMBTU) -> float:
    """Convert a gas flow between the supported volume units (fail-closed on unknown units)."""
    if from_unit not in FLOW_UNITS:
        raise GasUnitError(f"unknown flow unit {from_unit!r}; allowed {list(FLOW_UNITS)}")
    if to_unit not in FLOW_UNITS:
        raise GasUnitError(f"unknown flow unit {to_unit!r}; allowed {list(FLOW_UNITS)}")
    flow = _non_negative(value, "flow")
    btu_per_cf = _positive(btu_per_cf, "btu_per_cf")
    mmbtu_per_boe = _positive(mmbtu_per_boe, "mmbtu_per_boe")
    if from_unit == to_unit:
        return flow
    # canonical intermediate: bcm/y
    if from_unit == "bcm/y":
        bcm_y = flow
    elif from_unit == "bcm/month":
        bcm_y = flow * DAYS_PER_YEAR / DAYS_PER_MONTH
    elif from_unit == "bcm/day":
        bcm_y = flow * DAYS_PER_YEAR
    elif from_unit == "mboe/day":
        bcm_y = mb_d_to_bcm_y(flow, btu_per_cf=btu_per_cf, mmbtu_per_boe=mmbtu_per_boe)
    else:  # mmbtu/day
        bcm_y = mmbtu_to_bcm(flow * DAYS_PER_YEAR, btu_per_cf=btu_per_cf)
    if to_unit == "bcm/y":
        return bcm_y
    if to_unit == "bcm/month":
        return bcm_y / DAYS_PER_YEAR * DAYS_PER_MONTH
    if to_unit == "bcm/day":
        return bcm_y / DAYS_PER_YEAR
    if to_unit == "mboe/day":
        return bcm_y_to_mb_d(bcm_y, btu_per_cf=btu_per_cf, mmbtu_per_boe=mmbtu_per_boe)
    return bcm_to_mmbtu(bcm_y, btu_per_cf=btu_per_cf) / DAYS_PER_YEAR


# ------------------------------------------------------------------------------------- hubs
@dataclass(frozen=True)
class Hub:
    """A gas trading hub with its native quote convention (declared, not observed)."""

    hub_id: str
    name: str
    region: str
    quote_unit: str
    delivery: str
    source_class: str = "assumption"

    def __post_init__(self) -> None:
        if not self.hub_id.strip():
            raise MarketError("hub_id must be non-empty")
        if self.quote_unit not in PRICE_UNITS:
            raise GasUnitError(f"{self.hub_id}: unknown quote unit {self.quote_unit!r}")
        if self.source_class not in ("observed", "derived", "assumption"):
            raise MarketError(f"{self.hub_id}: unknown source_class {self.source_class!r}")


HUBS: tuple[Hub, ...] = (
    Hub("HENRY_HUB", "Henry Hub", "US Gulf Coast", "USD/MMBtu",
        "NYMEX front month, physical delivery at Erath, Louisiana"),
    Hub("TTF", "TTF", "Netherlands / NW Europe", "EUR/MWh",
        "ICE Endex front month, virtual trading point"),
    Hub("JKM", "JKM", "North-East Asia", "USD/MMBtu",
        "Platts JKM assessment, delivered ex-ship Japan/Korea"),
)


def hub_table(params: Mapping) -> tuple[list[dict], dict]:
    """The declared hub quotes normalised to ``USD/MMBtu``, ``EUR/MWh`` and ``USD/Mcf``."""
    eur_usd = float(params["eur_usd"])
    btu_per_cf = float(params["btu_per_cf"])
    quotes = {"HENRY_HUB": float(params["henry_hub_price_usd_per_mmbtu"]),
              "TTF": float(params["ttf_price_eur_per_mwh"]),
              "JKM": float(params["jkm_price_usd_per_mmbtu"])}
    rows = []
    for hub in HUBS:
        quote = quotes[hub.hub_id]
        usd = convert_price(quote, hub.quote_unit, "USD/MMBtu", eur_usd=eur_usd,
                            btu_per_cf=btu_per_cf)
        rows.append({
            "hub_id": hub.hub_id, "name": hub.name, "region": hub.region,
            "delivery": hub.delivery, "quote_unit": hub.quote_unit,
            "quote": _q(quote, 6), "usd_per_mmbtu": _q(usd, 6),
            "eur_per_mwh": _q(usd_per_mmbtu_to_eur_per_mwh(usd, eur_usd), 4),
            "usd_per_mcf": _q(usd_per_mmbtu_to_usd_per_mcf(usd, btu_per_cf=btu_per_cf), 4),
            "source_class": hub.source_class,
        })
    by_id = {row["hub_id"]: row for row in rows}
    spreads = {
        "ttf_minus_henry_hub_usd_per_mmbtu": _q(
            by_id["TTF"]["usd_per_mmbtu"] - by_id["HENRY_HUB"]["usd_per_mmbtu"], 6),
        "jkm_minus_ttf_usd_per_mmbtu": _q(
            by_id["JKM"]["usd_per_mmbtu"] - by_id["TTF"]["usd_per_mmbtu"], 6),
        "jkm_minus_henry_hub_usd_per_mmbtu": _q(
            by_id["JKM"]["usd_per_mmbtu"] - by_id["HENRY_HUB"]["usd_per_mmbtu"], 6),
    }
    return rows, spreads


# ------------------------------------------------------------------------------ default model
#: Central parameters of the prototype: **all assumptions**, all replaceable (ASSUMPTION_SPECS).
DEFAULT_PARAMS: dict = {
    # -- hub quotes and conversions (reference levels, replace with dated quotes) -------------
    "ttf_price_eur_per_mwh": 34.0,
    "jkm_price_usd_per_mmbtu": 12.0,
    "henry_hub_price_usd_per_mmbtu": 3.2,
    "eur_usd": 1.12,
    "btu_per_cf": BTU_PER_CF,
    "mmbtu_per_boe": BOE_MMBTU,
    "liquefaction_usd_per_mmbtu": 2.5,
    "freight_usd_per_mmbtu": 1.6,
    "regas_usd_per_mmbtu": 0.5,
    # -- European balance, bcm/y -------------------------------------------------------------
    "eu_demand_bcm_y": 380.0,
    "eu_domestic_bcm_y": 40.0,
    "norway_pipeline_bcm_y": 110.0,
    "other_pipeline_bcm_y": 45.0,
    "russia_pipeline_bcm_y": 25.0,
    "eu_lng_imports_bcm_y": 160.0,
    "eu_lng_import_capacity_bcm_y": 240.0,
    # -- Asia and the global LNG pool, bcm/y -------------------------------------------------
    "asia_demand_bcm_y": 350.0,
    "asia_lng_supply_bcm_y": 350.0,
    "other_lng_bcm_y": 50.0,
    "global_lng_supply_bcm_y": 560.0,
    # -- storage (a monthly release plan, not an optimisation) --------------------------------
    "storage_capacity_bcm": 100.0,
    "storage_stock_bcm": 95.0,
    "storage_max_draw_bcm_day": 0.40,
    "storage_min_share": 0.25,
    "storage_cover_share": 0.60,
    # -- shock and response -------------------------------------------------------------------
    "pipeline_loss_bcm_y": 0.0,
    "pipeline_loss_months": 6,
    "lng_diversion_capacity_bcm_y": 0.0,
    "diversion_months": 6,
    "diversion_ramp_months": 2,
    "demand_shift_share": 0.0,
    "import_response_share": 0.70,
    "elasticity_short": -0.10,
    "elasticity_long": -0.30,
    "elasticity_ramp_months": 6,
    "risk_premium_usd_per_mmbtu": 0.0,
    "resolve_diversion": False,
}

#: Which public source should replace each assumption ("replace-with", docs/147/docs/188).
ASSUMPTION_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("ttf_price_eur_per_mwh", "EUR/MWh",
     "TTF front-month reference level; replace with a dated ICE Endex/EEX quote",
     "ICE Endex TTF front month; EEX gas quotes"),
    ("jkm_price_usd_per_mmbtu", "USD/MMBtu",
     "JKM reference level; replace with a dated licensed assessment (public aggregates only if "
     "the licence allows republication)",
     "Platts JKM (licensed); otherwise GIIGNL/IEA aggregates"),
    ("henry_hub_price_usd_per_mmbtu", "USD/MMBtu",
     "Henry Hub front-month reference level; replace with a dated NYMEX settlement",
     "NYMEX Henry Hub settlement (CME public daily)"),
    ("eur_usd", "USD per EUR",
     "FX rate used to put TTF next to the USD-quoted hubs; replace with a dated official rate",
     "ECB euro reference rate"),
    ("btu_per_cf", "BTU/cf",
     "calorific value used for bcm <-> MMBtu; hubs differ (HH ~1,037, TTF ~1,040 gross)",
     "EIA/GIE unit conversion tables; hub gas-quality specs"),
    ("mmbtu_per_boe", "MMBtu/boe",
     "energy convention for the engine flow unit (1 boe = 5.8 MMBtu)",
     "EIA/IEA energy-conversion conventions"),
    ("liquefaction_usd_per_mmbtu", "USD/MMBtu",
     "liquefaction cost used in the netback arithmetic; replace with an operator/industry benchmark",
     "LNG liquefaction cost benchmarks (industry reports)"),
    ("freight_usd_per_mmbtu", "USD/MMBtu",
     "LNG freight used as the arbitrage band; replace with a dated freight assessment",
     "Baltic/Clarksons/Spark LNG freight assessments"),
    ("regas_usd_per_mmbtu", "USD/MMBtu",
     "regasification cost used as the arbitrage band; replace with terminal tariffs",
     "terminal tariff disclosures / GIE transparency"),
    ("eu_demand_bcm_y", "bcm/y",
     "EU+UK gas demand; replace with Eurostat/ENTSOG/GIE monthly data",
     "Eurostat gas supply statistics; ENTSOG; GIE"),
    ("eu_domestic_bcm_y", "bcm/y",
     "EU+UK domestic gas production (NL, UK, DK, RO, others)",
     "Eurostat; national regulators; ENTSOG"),
    ("norway_pipeline_bcm_y", "bcm/y",
     "Norwegian pipeline supply to the EU+UK",
     "Gassco flow data; Norwegian Offshore Directorate"),
    ("other_pipeline_bcm_y", "bcm/y",
     "North African, Azerbaijani and other pipeline supply",
     "ENTSOG; GIE; national TSO data"),
    ("russia_pipeline_bcm_y", "bcm/y",
     "remaining Russian pipeline supply to the EU+UK",
     "ENTSOG; GIE; EU Commission gas flows"),
    ("eu_lng_imports_bcm_y", "bcm/y",
     "EU+UK LNG imports; replace with GIE ALSI/GIIGNL data",
     "GIE ALSI; GIIGNL annual report; IEA"),
    ("eu_lng_import_capacity_bcm_y", "bcm/y",
     "EU+UK LNG regasification capacity (a physical ceiling on imports)",
     "GIE LNG database; GIIGNL"),
    ("asia_demand_bcm_y", "bcm/y",
     "Asian gas demand used in the JKM calibration",
     "JODI gas; IEA Gas Market Report"),
    ("asia_lng_supply_bcm_y", "bcm/y",
     "Asian LNG imports used as the JKM baseline supply",
     "GIIGNL; IEA; Kpler/Platts aggregates (licensed)"),
    ("other_lng_bcm_y", "bcm/y",
     "rest-of-world LNG imports (outside Europe and Asia)",
     "GIIGNL; IEA"),
    ("global_lng_supply_bcm_y", "bcm/y",
     "global LNG trade used as a calibration cross-check: EU + Asia + other must sum to it",
     "GIIGNL; IEA Gas Market Report"),
    ("storage_capacity_bcm", "bcm",
     "EU+UK underground storage working volume",
     "GIE AGSI capacity; national regulators"),
    ("storage_stock_bcm", "bcm",
     "storage level at the start of the scenario; replace with AGSI % full x capacity",
     "GIE AGSI"),
    ("storage_max_draw_bcm_day", "bcm/day",
     "storage deliverability (a physical ceiling on withdrawal)",
     "GIE AGSI; ENTSOG; national TSO data"),
    ("storage_min_share", "share",
     "security minimum at the horizon end (a rule, not a market outcome)",
     "EU 90% November target; national storage obligations"),
    ("storage_cover_share", "share",
     "share of the physical gap that storage delivers; the rest is closed by price. This is the "
     "endogenous-withdrawal assumption the monthly engine does not provide",
     "assumption: calibrate against observed withdrawal behaviour"),
    ("pipeline_loss_bcm_y", "bcm/y",
     "pipeline gas lost in the scenario (route-level assumption)",
     "scenario definition (policy/route assumption)"),
    ("pipeline_loss_months", "months",
     "duration of the pipeline loss", "scenario definition"),
    ("lng_diversion_capacity_bcm_y", "bcm/y",
     "divertible LNG cargoes available to Europe in the scenario",
     "scenario definition; fleet/portfolio data (GIIGNL, Kpler)"),
    ("diversion_months", "months", "months during which cargoes are diverted", "scenario"),
    ("diversion_ramp_months", "months",
     "months over which the diversion ramps (cargoes already at sea do not turn instantly)",
     "assumption: shipping/logistics ramp"),
    ("demand_shift_share", "share",
     "weather-driven demand shift (winter scenarios); no weather model exists, the shift is declared",
     "assumption: replace with HDD/CDD data (Eurostat, national weather services)"),
    ("import_response_share", "share",
     "share of a demand FALL that is matched by lower nominations (imports are elastic downwards); "
     "asymmetric by design: a demand RISE cannot summon cargoes in the same month",
     "assumption: calibrate against observed nomination behaviour"),
    ("elasticity_short", "elasticity",
     "effective short-run gas demand elasticity (industry curtailment, fuel switching); pure "
     "residential price elasticity is smaller in the very short run",
     "literature ranges (IEA/IMF/NBER gas demand studies) + explicit rationing assumption"),
    ("elasticity_long", "elasticity",
     "effective gas demand elasticity by the end of the ramp",
     "literature ranges"),
    ("elasticity_ramp_months", "months", "months over which the elasticity ramps", "assumption"),
    ("risk_premium_usd_per_mmbtu", "USD/MMBtu",
     "geopolitical/scarcity premium added on top of the cleared price",
     "assumption based on historical European gas episodes (2021-22)"),
)


def assumptions(params: Mapping | None = None) -> tuple[Assumption, ...]:
    """The declared inputs (``assumption``) plus the values computed inside the module (``derived``)."""
    values = dict(DEFAULT_PARAMS)
    if params:
        values.update({key: value for key, value in params.items() if key in DEFAULT_PARAMS})
    declared: list[Assumption] = []
    for name, unit, note, source in ASSUMPTION_SPECS:
        declared.append(Assumption(name=name, value=values.get(name), unit=unit,
                                   source_class="assumption", note=note, source=source))
    ttf = _ttf_usd_per_mmbtu(values)
    jkm = float(values["jkm_price_usd_per_mmbtu"])
    hh = float(values["henry_hub_price_usd_per_mmbtu"])
    declared.extend((
        Assumption("ttf_usd_per_mmbtu", _q(ttf), "USD/MMBtu", "derived",
                   "TTF quote converted with the declared EUR/USD and MWh/MMBtu conventions",
                   "module arithmetic"),
        Assumption("jkm_usd_per_mmbtu", _q(jkm), "USD/MMBtu", "derived",
                   "JKM quote already in USD/MMBtu (no conversion)", "module arithmetic"),
        Assumption("eu_demand_mb_per_day", _q(_flow(values, values["eu_demand_bcm_y"])),
                   "mboe/d", "derived",
                   "annual European demand in the engine flow unit (mb/d of oil equivalent)",
                   "module arithmetic"),
        Assumption("eu_supply_bcm_y", _q(eu_supply_bcm_y(values)), "bcm/y", "derived",
                   "sum of the declared supply components", "module arithmetic"),
        Assumption("storage_stock_mb", _q(_storage_stock_mb(values)), "mboe", "derived",
                   "starting storage converted to the engine stock unit", "module arithmetic"),
        Assumption("ttf_minus_henry_hub_usd_per_mmbtu", _q(ttf - hh), "USD/MMBtu", "derived",
                   "hub spread implied by the declared quotes", "module arithmetic"),
        Assumption("arbitrage_band_usd_per_mmbtu", _q(arbitrage_band(values)), "USD/MMBtu",
                   "derived", "freight + regasification, the band the diversion has to clear",
                   "module arithmetic"),
    ))
    return tuple(declared)


# ------------------------------------------------------------------------------- model helpers
def _flow(params: Mapping, bcm_y: float) -> float:
    """``bcm/y`` -> the engine flow unit (``mb/d`` of oil equivalent)."""
    return bcm_y_to_mb_d(bcm_y, btu_per_cf=float(params["btu_per_cf"]),
                         mmbtu_per_boe=float(params["mmbtu_per_boe"]))


def _flow_back(params: Mapping, rate: float) -> float:
    return mb_d_to_bcm_y(rate, btu_per_cf=float(params["btu_per_cf"]),
                         mmbtu_per_boe=float(params["mmbtu_per_boe"]))


def _storage_stock_mb(params: Mapping) -> float:
    return bcm_to_mboe(float(params["storage_stock_bcm"]),
                       btu_per_cf=float(params["btu_per_cf"]),
                       mmbtu_per_boe=float(params["mmbtu_per_boe"]))


def _storage_min_mb(params: Mapping) -> float:
    capacity_mb = bcm_to_mboe(float(params["storage_capacity_bcm"]),
                              btu_per_cf=float(params["btu_per_cf"]),
                              mmbtu_per_boe=float(params["mmbtu_per_boe"]))
    return float(params["storage_min_share"]) * capacity_mb


def _ttf_usd_per_mmbtu(params: Mapping) -> float:
    return eur_per_mwh_to_usd_per_mmbtu(float(params["ttf_price_eur_per_mwh"]),
                                        float(params["eur_usd"]))


def arbitrage_band(params: Mapping) -> float:
    """LNG freight + regasification: the spread a cargo has to beat to move."""
    return (float(params["freight_usd_per_mmbtu"]) + float(params["regas_usd_per_mmbtu"]))


def pipeline_supply_bcm_y(params: Mapping) -> float:
    return (float(params["eu_domestic_bcm_y"]) + float(params["norway_pipeline_bcm_y"])
            + float(params["other_pipeline_bcm_y"]) + float(params["russia_pipeline_bcm_y"]))


def eu_supply_bcm_y(params: Mapping) -> float:
    return pipeline_supply_bcm_y(params) + float(params["eu_lng_imports_bcm_y"])


def eu_balance_bcm_y(params: Mapping) -> float:
    """Declared European balance: supply - demand (must close, else the module fails closed)."""
    return eu_supply_bcm_y(params) - float(params["eu_demand_bcm_y"])


def _as_int(value: object, name: str, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MarketError(f"{name} must be an integer, got {value!r}") from None
    if number < minimum:
        raise MarketError(f"{name} must be >= {minimum}, got {number}")
    return number


def _windowed_ramp(total: float, horizon: int, ramp_months: int,
                   active_months: int) -> tuple[float, ...]:
    """A linear ramp of ``total`` over ``ramp_months``, zero after ``active_months``."""
    total = float(total)
    if total <= 0.0:
        return tuple(0.0 for _ in range(int(horizon)))
    values = ramp(total, int(horizon), int(ramp_months))
    active = min(int(active_months), int(horizon))
    return tuple(round(values[index] if index < active else 0.0, 12)
                 for index in range(int(horizon)))


def validate_params(params: Mapping, horizon_months: int) -> None:
    """Fail-closed validation of every parameter and of the two calibration identities."""
    if int(horizon_months) <= 0:
        raise MarketError("horizon_months must be positive")
    for name in ("ttf_price_eur_per_mwh", "jkm_price_usd_per_mmbtu",
                 "henry_hub_price_usd_per_mmbtu", "eur_usd", "btu_per_cf", "mmbtu_per_boe",
                 "liquefaction_usd_per_mmbtu"):
        if _finite(params[name], name) <= 0.0:
            raise MarketError(f"{name} must be positive, got {params[name]!r}")
    for name in ("freight_usd_per_mmbtu", "regas_usd_per_mmbtu", "risk_premium_usd_per_mmbtu",
                 "eu_lng_import_capacity_bcm_y", "other_lng_bcm_y"):
        if _finite(params[name], name) < 0.0:
            raise MarketError(f"{name} must be non-negative, got {params[name]!r}")
    for name in ("elasticity_short", "elasticity_long"):
        value = _finite(params[name], name)
        if not -1.0 < value < 0.0:
            raise MarketError(f"{name} must be in (-1, 0), got {value!r} (negative = demand falls "
                              "when the price rises)")
    if abs(float(params["elasticity_short"])) > abs(float(params["elasticity_long"])):
        raise MarketError("elasticity_short must be at most elasticity_long in magnitude "
                          "(the short run is less elastic)")
    for name in ("storage_cover_share", "storage_min_share", "import_response_share"):
        value = _finite(params[name], name)
        if not 0.0 <= value <= 1.0:
            raise MarketError(f"{name} must be in [0, 1], got {value!r}")
    shift = _finite(params["demand_shift_share"], "demand_shift_share")
    if not -0.5 <= shift <= 0.5:
        raise MarketError(f"demand_shift_share must be in [-0.5, 0.5], got {shift!r}")
    capacity = _finite(params["storage_capacity_bcm"], "storage_capacity_bcm")
    stock = _finite(params["storage_stock_bcm"], "storage_stock_bcm")
    if capacity <= 0.0:
        raise MarketError("storage_capacity_bcm must be positive")
    if not 0.0 <= stock <= capacity:
        raise MarketError(f"storage_stock_bcm={stock} must be within [0, {capacity}]")
    if _finite(params["storage_max_draw_bcm_day"], "storage_max_draw_bcm_day") < 0.0:
        raise MarketError("storage_max_draw_bcm_day must be non-negative")
    loss = _finite(params["pipeline_loss_bcm_y"], "pipeline_loss_bcm_y")
    if loss < 0.0:
        raise MarketError("pipeline_loss_bcm_y must be non-negative")
    route_capacity = (float(params["russia_pipeline_bcm_y"])
                      + float(params["other_pipeline_bcm_y"]))
    if loss > route_capacity + 1e-9:
        raise MarketError(f"pipeline_loss_bcm_y={loss} exceeds the declared Russian + other "
                          f"pipeline supply {route_capacity} bcm/y")
    diversion = _finite(params["lng_diversion_capacity_bcm_y"], "lng_diversion_capacity_bcm_y")
    if diversion < 0.0:
        raise MarketError("lng_diversion_capacity_bcm_y must be non-negative")
    if diversion > float(params["asia_lng_supply_bcm_y"]) + 1e-9:
        raise MarketError(f"lng_diversion_capacity_bcm_y={diversion} exceeds the declared Asian LNG "
                          f"supply {params['asia_lng_supply_bcm_y']} bcm/y")
    for name in ("pipeline_loss_months", "diversion_months", "diversion_ramp_months",
                 "elasticity_ramp_months"):
        value = _as_int(params[name], name, minimum=1)
        if value > 36:
            raise MarketError(f"{name} is implausibly large: {value}")
    if int(horizon_months) > 36:
        raise MarketError("horizon_months above 36 is outside the prototype's scope")
    # calibration identities: the declared European balance must close, the LNG pool must add up
    balance = eu_balance_bcm_y(params)
    if abs(balance) > 1e-6:
        raise BalanceError(f"declared European gas balance does not close: supply {eu_supply_bcm_y(params)} "
                           f"vs demand {params['eu_demand_bcm_y']} bcm/y (gap {balance:+.6f})")
    if float(params["eu_lng_imports_bcm_y"]) > float(params["eu_lng_import_capacity_bcm_y"]) + 1e-9:
        raise BalanceError(f"EU LNG imports {params['eu_lng_imports_bcm_y']} exceed regasification "
                           f"capacity {params['eu_lng_import_capacity_bcm_y']} bcm/y")
    lng_sum = (float(params["eu_lng_imports_bcm_y"]) + float(params["asia_lng_supply_bcm_y"])
               + float(params["other_lng_bcm_y"]))
    if abs(lng_sum - float(params["global_lng_supply_bcm_y"])) > 1e-6:
        raise BalanceError(f"global LNG calibration does not close: EU + Asia + other = {lng_sum} "
                           f"vs declared global supply {params['global_lng_supply_bcm_y']} bcm/y")


# ------------------------------------------------------------------------------- calibrations
def _europe_calibration(params: Mapping, storage_stock_mb: float) -> MarketCalibration:
    return MarketCalibration(
        commodity="TTF natural gas (Europe)",
        base_price=_ttf_usd_per_mmbtu(params),
        base_demand=_flow(params, float(params["eu_demand_bcm_y"])),
        baseline_supply=_flow(params, eu_supply_bcm_y(params)),
        elasticity_short=float(params["elasticity_short"]),
        elasticity_long=float(params["elasticity_long"]),
        elasticity_ramp_months=int(params["elasticity_ramp_months"]),
        risk_premium=float(params["risk_premium_usd_per_mmbtu"]),
        commercial_stock=0.0,
        commercial_max_draw=0.0,
        strategic_stock=float(storage_stock_mb),
        strategic_max_draw=bcm_day_to_mb_d(float(params["storage_max_draw_bcm_day"]),
                                           btu_per_cf=float(params["btu_per_cf"]),
                                           mmbtu_per_boe=float(params["mmbtu_per_boe"])),
        price_unit=UNITS,
    )


def _asia_calibration(params: Mapping) -> MarketCalibration:
    return MarketCalibration(
        commodity="JKM LNG (North-East Asia)",
        base_price=float(params["jkm_price_usd_per_mmbtu"]),
        base_demand=_flow(params, float(params["asia_demand_bcm_y"])),
        baseline_supply=_flow(params, float(params["asia_lng_supply_bcm_y"])),
        elasticity_short=float(params["elasticity_short"]),
        elasticity_long=float(params["elasticity_long"]),
        elasticity_ramp_months=int(params["elasticity_ramp_months"]),
        risk_premium=0.0,
        commercial_stock=0.0,
        commercial_max_draw=0.0,
        strategic_stock=0.0,
        strategic_max_draw=0.0,
        price_unit=UNITS,
    )


# ------------------------------------------------------------------------------ scenario paths
def storage_plan(params: Mapping, horizon: int, gap_mb_d: Sequence[float]) -> tuple:
    """Monthly storage release: ``cover_share`` of the physical gap, bounded by deliverability
    and by the stock that must remain above the declared security minimum."""
    if len(gap_mb_d) != int(horizon):
        raise MarketError(f"gap vector has {len(gap_mb_d)} entries for a {horizon}-month horizon")
    days = DAYS_PER_MONTH
    max_rate = bcm_day_to_mb_d(float(params["storage_max_draw_bcm_day"]),
                               btu_per_cf=float(params["btu_per_cf"]),
                               mmbtu_per_boe=float(params["mmbtu_per_boe"]))
    releasable = max(0.0, _storage_stock_mb(params) - _storage_min_mb(params))
    cover = float(params["storage_cover_share"])
    plan = []
    for month in range(int(horizon)):
        want = cover * max(0.0, float(gap_mb_d[month]))
        draw = min(want, max_rate, max(0.0, releasable) / days)
        plan.append(round(draw, 12))
        releasable -= draw * days
    return tuple(plan)


def europe_shock(params: Mapping, horizon: int, *, pipeline_loss_bcm_y: float,
                 diversion_bcm_y: float, demand_shift_share: float) -> dict:
    """Build the European shock vectors and the storage plan for one parameter set."""
    horizon = int(horizon)
    base_demand = _flow(params, float(params["eu_demand_bcm_y"]))
    supply = _flow(params, eu_supply_bcm_y(params))
    shift = float(demand_shift_share) * base_demand
    loss = _flow(params, float(pipeline_loss_bcm_y))
    lost = [0.0] * horizon
    months = min(_as_int(params["pipeline_loss_months"], "pipeline_loss_months", 1), horizon)
    for month in range(months):
        lost[month] += loss
    import_response = 0.0
    if shift < 0.0:
        import_response = (-shift) * float(params["import_response_share"])
        lost = [value + import_response for value in lost]
    added = _windowed_ramp(_flow(params, float(diversion_bcm_y)), horizon,
                           _as_int(params["diversion_ramp_months"], "diversion_ramp_months", 1),
                           _as_int(params["diversion_months"], "diversion_months", 1))
    gap = [max(0.0, base_demand + shift - (supply - lost[month] + added[month]))
           for month in range(horizon)]
    plan = storage_plan(params, horizon, gap)
    shock = Shock(lost_supply=tuple(lost), added_supply=added, strategic_release=plan,
                  demand_shift=tuple(shift for _ in range(horizon)),
                  label=f"gas_pipeline_loss_{months}m")
    return {"shock": shock, "lost": lost, "added": added, "plan": plan, "gap": gap,
            "demand": base_demand + shift, "shift": shift, "import_response": import_response,
            "supply": supply}


def _europe_path(params: Mapping, horizon: int, *, pipeline_loss_bcm_y: float,
                 diversion_bcm_y: float, demand_shift_share: float) -> tuple:
    package = europe_shock(params, horizon, pipeline_loss_bcm_y=pipeline_loss_bcm_y,
                           diversion_bcm_y=diversion_bcm_y,
                           demand_shift_share=demand_shift_share)
    path = simulate(_europe_calibration(params, _storage_stock_mb(params)), package["shock"])
    return path, package


def asia_lost_vector(params: Mapping, horizon: int, diversion_bcm_y: float) -> tuple[float, ...]:
    """The Asian shock: the diverted cargoes leave the Asian balance (a transfer, not new supply)."""
    return _windowed_ramp(_flow(params, float(diversion_bcm_y)), int(horizon),
                          _as_int(params["diversion_ramp_months"], "diversion_ramp_months", 1),
                          _as_int(params["diversion_months"], "diversion_months", 1))


def _asia_path(params: Mapping, horizon: int, *, diversion_bcm_y: float) -> MarketPath:
    lost = asia_lost_vector(params, horizon, diversion_bcm_y)
    shock = Shock(lost_supply=lost, label="gas_lng_diverted_from_asia")
    return simulate(_asia_calibration(params), shock)


def arbitrage_clearing_diversion(params: Mapping, horizon: int, *,
                                 iterations: int = ARBITRAGE_ITERATIONS) -> dict:
    """Solve the diversion volume at which ``mean(TTF) - mean(JKM) = freight + regas``.

    Monotone by construction (more diverted cargoes lower TTF and raise JKM), so a deterministic
    bisection is enough: no randomness, no wall clock, and the bracket is reported.
    """
    capacity = float(params["lng_diversion_capacity_bcm_y"])
    band = arbitrage_band(params)
    shift = float(params["demand_shift_share"])
    loss = float(params["pipeline_loss_bcm_y"])

    def spread(diversion: float) -> float:
        europe, _package = _europe_path(params, horizon, pipeline_loss_bcm_y=loss,
                                        diversion_bcm_y=diversion,
                                        demand_shift_share=shift)
        asia = _asia_path(params, horizon, diversion_bcm_y=diversion)
        return _mean(europe.price) - _mean(asia.price) - band

    if capacity <= 0.0:
        return {"diversion_bcm_y": 0.0, "binding": "none", "residual": None,
                "capacity_bcm_y": 0.0, "iterations": 0, "bracket": None}
    low_value = spread(0.0)
    high_value = spread(capacity)
    bracket = [_q(low_value), _q(high_value)]
    if low_value <= 0.0:
        return {"diversion_bcm_y": 0.0, "binding": "none", "residual": _q(low_value),
                "capacity_bcm_y": _q(capacity), "iterations": 0, "bracket": bracket}
    if high_value >= 0.0:
        return {"diversion_bcm_y": _q(capacity), "binding": "capacity",
                "residual": _q(high_value), "capacity_bcm_y": _q(capacity), "iterations": 0,
                "bracket": bracket}
    low, high = 0.0, capacity
    for _step in range(int(iterations)):
        middle = 0.5 * (low + high)
        if spread(middle) > 0.0:
            low = middle
        else:
            high = middle
    diversion = 0.5 * (low + high)
    residual = spread(diversion)
    return {"diversion_bcm_y": _q(diversion), "binding": "arbitrage", "residual": _q(residual),
            "capacity_bcm_y": _q(capacity), "iterations": int(iterations),
            "bracket": bracket}


# --------------------------------------------------------------------------------- core run
def _run_core(params: Mapping, horizon: int) -> dict:
    """Run the European and Asian balances plus the diversion logic; aggregate-only output."""
    validate_params(params, horizon)
    shift = float(params["demand_shift_share"])
    loss = float(params["pipeline_loss_bcm_y"])
    capacity = float(params["lng_diversion_capacity_bcm_y"])
    if bool(params["resolve_diversion"]) and capacity > 0.0:
        diversion_info = arbitrage_clearing_diversion(params, horizon)
    else:
        diversion_info = {"diversion_bcm_y": 0.0, "binding": "none", "residual": None,
                          "capacity_bcm_y": _q(capacity), "iterations": 0, "bracket": None}
    diversion = float(diversion_info["diversion_bcm_y"])
    europe, package = _europe_path(params, horizon, pipeline_loss_bcm_y=loss,
                                   diversion_bcm_y=diversion, demand_shift_share=shift)
    asia_lost = asia_lost_vector(params, horizon, diversion)
    asia = _asia_path(params, horizon, diversion_bcm_y=diversion)
    if not europe.ok:
        raise MarketError(f"gas scenario: European balance checks failed {europe.checks}")
    if not asia.ok:
        raise MarketError(f"gas scenario: Asian balance checks failed {asia.checks}")
    capacity_case = None
    if capacity > 0.0 and diversion < capacity - 1e-9:
        cap_europe, _cap_package = _europe_path(params, horizon, pipeline_loss_bcm_y=loss,
                                                diversion_bcm_y=capacity,
                                                demand_shift_share=shift)
        cap_asia = _asia_path(params, horizon, diversion_bcm_y=capacity)
        capacity_case = {
            "diversion_bcm_y": _q(capacity),
            "ttf_mean_usd_per_mmbtu": _q(_mean(cap_europe.price)),
            "jkm_mean_usd_per_mmbtu": _q(_mean(cap_asia.price)),
            "jkm_minus_ttf_usd_per_mmbtu": _q(_mean(cap_asia.price) - _mean(cap_europe.price)),
            "within_band": bool(abs(_mean(cap_asia.price) - _mean(cap_europe.price))
                                <= arbitrage_band(params) + ARBITRAGE_TOLERANCE),
        }
    return {"europe": europe, "asia": asia, "package": package, "asia_lost": asia_lost,
            "diversion": diversion_info, "capacity_case": capacity_case}


# --------------------------------------------------------------------------- sensitivity table
#: Static sensitivity variants (a range, not a confidence interval); the two risk-premium variants
#: are relative to the scenario's own premium and are added by :func:`sensitivity_variants`.
SENSITIVITY_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("elasticity_more_elastic", {"elasticity_short": -0.15, "elasticity_long": -0.40}),
    ("elasticity_less_elastic", {"elasticity_short": -0.06, "elasticity_long": -0.20}),
    ("storage_cover_high", {"storage_cover_share": 0.85}),
    ("storage_cover_low", {"storage_cover_share": 0.30}),
)
#: Diversion variants only make sense when the scenario has a diversion capacity.
DIVERSION_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("diversion_capacity_high", {"lng_diversion_capacity_bcm_y": 50.0}),
    ("diversion_ramp_slow", {"diversion_ramp_months": 4}),
)
RISK_PREMIUM_HIGH_DELTA = 3.0


def sensitivity_variants(params: Mapping) -> tuple[tuple[str, dict], ...]:
    """The declared variants for one scenario: static set + diversion + relative risk premium."""
    premium = float(params["risk_premium_usd_per_mmbtu"])
    variants = list(SENSITIVITY_VARIANTS)
    if float(params["lng_diversion_capacity_bcm_y"]) > 0.0:
        variants.extend(DIVERSION_VARIANTS)
    variants.append(("risk_premium_low", {"risk_premium_usd_per_mmbtu": 0.0}))
    variants.append(("risk_premium_high",
                     {"risk_premium_usd_per_mmbtu": _q(premium + RISK_PREMIUM_HIGH_DELTA)}))
    return tuple(variants)


class GasModule(MarketModule):
    """Gas/LNG research module implementing the market-module contract (docs/188, UC-24)."""

    module_id = MODULE_ID
    commodity = COMMODITY
    units = UNITS
    status = RESEARCH_ONLY

    def scenarios(self) -> tuple:
        return (
            Scenario("eu_pipeline_shock_6m",
                     "European pipeline gas shock: 45 bcm/y of Russian + other pipeline supply is "
                     "lost for 6 months; storage covers the declared share of the gap, the rest is "
                     "closed by the TTF price; no LNG diversion.",
                     {"pipeline_loss_bcm_y": 45.0, "pipeline_loss_months": 6,
                      "risk_premium_usd_per_mmbtu": 2.0}, horizon_months=6),
            Scenario("eu_pipeline_shock_lng_diversion_6m",
                     "The same pipeline shock, but Europe pulls LNG cargoes from Asia up to the "
                     "declared capacity; the diversion volume is solved at the arbitrage-clearing "
                     "point (TTF - JKM = freight + regas) and the Asian incidence is reported.",
                     {"pipeline_loss_bcm_y": 45.0, "pipeline_loss_months": 6,
                      "lng_diversion_capacity_bcm_y": 40.0, "resolve_diversion": True,
                      "risk_premium_usd_per_mmbtu": 2.0}, horizon_months=6),
            Scenario("soft_winter_6m",
                     "Mild winter: weather-driven demand -8%, imports respond downwards by the "
                     "declared share; storage does not release (no gap).",
                     {"demand_shift_share": -0.08}, horizon_months=6),
            Scenario("hard_winter_6m",
                     "Cold winter: weather-driven demand +10%; storage delivers the declared share "
                     "of the gap up to deliverability; imports cannot ramp up in the same month.",
                     {"demand_shift_share": 0.10, "risk_premium_usd_per_mmbtu": 1.0},
                     horizon_months=6),
            Scenario("hard_winter_pipeline_shock_6m",
                     "Cold winter AND the pipeline shock together (stress combination): demand +10% "
                     "and 45 bcm/y of pipeline supply lost for 6 months.",
                     {"demand_shift_share": 0.10, "pipeline_loss_bcm_y": 45.0,
                      "pipeline_loss_months": 6, "risk_premium_usd_per_mmbtu": 3.0},
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
        if int(params["pipeline_loss_months"]) > horizon and "pipeline_loss_months" not in overrides:
            params["pipeline_loss_months"] = horizon
        if int(params["diversion_months"]) > horizon and "diversion_months" not in overrides:
            params["diversion_months"] = horizon
        if int(params["diversion_ramp_months"]) > horizon:
            params["diversion_ramp_months"] = horizon
        validate_params(params, horizon)
        core = _run_core(params, horizon)
        sensitivity = self._sensitivity(params, horizon, core)
        ttf_range = price_range(core["europe"], sensitivity, metric="price_peak")
        jkm_variants = [{"price_peak": item["jkm_peak"]} for item in sensitivity
                        if "jkm_peak" in item]
        if not jkm_variants:
            jkm_variants = [{"price_peak": max(core["asia"].price)}]
        jkm_range = price_range(core["asia"], jkm_variants, metric="price_peak")
        summary = self._summary(params, horizon, scenario, core, ttf_range, jkm_range, sensitivity)
        checks = self._checks(params, core, ttf_range, jkm_range, sensitivity)
        return MarketResult(
            module_id=self.module_id, commodity=self.commodity,
            scenario_id=scenario.scenario_id, units=self.units, horizon_months=horizon,
            price_path=core["europe"].price, summary=summary, assumptions=assumptions(params),
            sensitivity=tuple(sensitivity), checks=checks,
            meta={"engine": "amfe.markets.engine.v1", "scenario": scenario.as_dict(),
                  "parameters": {key: params[key] for key in sorted(params)},
                  "units": {"price": self.units, "price_alt": "EUR/MWh", "price_us": "USD/Mcf",
                            "flows": "mboe/d (engine unit mb/d)", "annual_flows": "bcm/y",
                            "stocks": "mboe (engine unit mb)", "storage": "bcm",
                            "time_step": "month"},
                  "hubs": hub_table(params),
                  "replace_with": [{"name": name, "source": source}
                                   for name, _unit, _note, source in ASSUMPTION_SPECS],
                  "notes": [
                      "Henry Hub is a declared reference, not a cleared market in this module; the "
                      "netback is arithmetic on TTF.",
                      "No weather, no per-country storage, no pipeline topology, no contracts and "
                      "no LNG fleet: see docs/188 limits.",
                      "Gas flows enter the monthly engine in mb/d of oil equivalent (1 boe = "
                      "5.8 MMBtu); native bcm/y values are reported in the summary.",
                  ]})

    # -- summary -----------------------------------------------------------------------
    def _summary(self, params: Mapping, horizon: int, scenario: Scenario, core: dict,
                 ttf_range: dict, jkm_range: dict, sensitivity: Sequence[Mapping]) -> dict:
        europe = core["europe"]
        asia = core["asia"]
        package = core["package"]
        eur_usd = float(params["eur_usd"])
        base_ttf = _ttf_usd_per_mmbtu(params)
        base_jkm = float(params["jkm_price_usd_per_mmbtu"])
        hh = float(params["henry_hub_price_usd_per_mmbtu"])
        netback_cost = (float(params["liquefaction_usd_per_mmbtu"])
                        + float(params["freight_usd_per_mmbtu"])
                        + float(params["regas_usd_per_mmbtu"]))
        ttf = europe.price
        jkm = asia.price
        ttf_peak = max(ttf)
        jkm_peak = max(jkm)
        drawn_mb = math.fsum(package["plan"]) * DAYS_PER_MONTH
        storage_end_mb = _storage_stock_mb(params) - drawn_mb
        capacity_mb = bcm_to_mboe(float(params["storage_capacity_bcm"]),
                                  btu_per_cf=float(params["btu_per_cf"]),
                                  mmbtu_per_boe=float(params["mmbtu_per_boe"]))
        flows = {
            "eu_lng_imports_bcm_y_path": [_q(float(params["eu_lng_imports_bcm_y"])
                                              + _flow_back(params, core["package"]["added"][m]), 4)
                                          for m in range(horizon)],
            "asia_lng_imports_bcm_y_path": [_q(float(params["asia_lng_supply_bcm_y"])
                                                - _flow_back(params, package["added"][m]), 4)
                                            for m in range(horizon)],
            "other_lng_bcm_y": _q(float(params["other_lng_bcm_y"]), 4),
            "global_lng_bcm_y_path": [
                _q(float(params["eu_lng_imports_bcm_y"]) + _flow_back(params, package["added"][m])
                   + float(params["asia_lng_supply_bcm_y"])
                   - _flow_back(params, package["added"][m])
                   + float(params["other_lng_bcm_y"]), 4) for m in range(horizon)],
            "global_lng_supply_bcm_y": _q(float(params["global_lng_supply_bcm_y"]), 4),
            "eu_import_capacity_bcm_y": _q(float(params["eu_lng_import_capacity_bcm_y"]), 4),
            "diversion_bcm_y": _q(float(core["diversion"]["diversion_bcm_y"]), 4),
            "diversion_share_of_global_pct": _q(
                100.0 * float(core["diversion"]["diversion_bcm_y"])
                / float(params["global_lng_supply_bcm_y"]), 4),
            "diversion_share_of_eu_imports_pct": _q(
                100.0 * float(core["diversion"]["diversion_bcm_y"])
                / float(params["eu_lng_imports_bcm_y"]), 4),
        }
        summary = {
            "scenario_label": scenario.scenario_id,
            "base_price_usd_per_mmbtu": _q(base_ttf),
            "base_price_eur_per_mwh": _q(usd_per_mmbtu_to_eur_per_mwh(base_ttf, eur_usd), 4),
            "ttf_usd_per_mmbtu_path": [_q(value) for value in ttf],
            "ttf_eur_per_mwh_path": [_q(usd_per_mmbtu_to_eur_per_mwh(value, eur_usd), 4)
                                     for value in ttf],
            "jkm_usd_per_mmbtu_path": [_q(value) for value in jkm],
            "jkm_eur_per_mwh_path": [_q(usd_per_mmbtu_to_eur_per_mwh(value, eur_usd), 4)
                                     for value in jkm],
            "henry_hub_reference_usd_per_mmbtu": _q(hh),
            "henry_hub_reference_usd_per_mcf": _q(
                usd_per_mmbtu_to_usd_per_mcf(hh, btu_per_cf=float(params["btu_per_cf"])), 4),
            "hh_netback_from_ttf_usd_per_mmbtu_path": [
                _q(value - netback_cost) for value in ttf],
            "hh_netback_gap_usd_per_mmbtu_path": [
                _q(value - netback_cost - hh) for value in ttf],
            "netback_cost_usd_per_mmbtu": _q(netback_cost),
            "ttf_peak_usd_per_mmbtu": _q(ttf_peak),
            "ttf_trough_usd_per_mmbtu": _q(min(ttf)),
            "ttf_mean_usd_per_mmbtu": _q(_mean(ttf)),
            "ttf_peak_eur_per_mwh": _q(usd_per_mmbtu_to_eur_per_mwh(ttf_peak, eur_usd), 4),
            "ttf_trough_eur_per_mwh": _q(usd_per_mmbtu_to_eur_per_mwh(min(ttf), eur_usd), 4),
            "ttf_mean_eur_per_mwh": _q(
                usd_per_mmbtu_to_eur_per_mwh(_mean(ttf), eur_usd), 4),
            "ttf_change_pct_vs_base": _q(100.0 * (ttf_peak - base_ttf) / base_ttf, 4),
            "ttf_trough_change_pct_vs_base": _q(100.0 * (min(ttf) - base_ttf) / base_ttf, 4),
            "ttf_mean_change_pct_vs_base": _q(
                100.0 * (_mean(ttf) - base_ttf) / base_ttf, 4),
            "jkm_peak_usd_per_mmbtu": _q(jkm_peak),
            "jkm_mean_usd_per_mmbtu": _q(_mean(jkm)),
            "jkm_change_pct_vs_base": _q(100.0 * (jkm_peak - base_jkm) / base_jkm, 4),
            "jkm_minus_ttf_usd_per_mmbtu_path": [_q(jkm[m] - ttf[m]) for m in range(horizon)],
            "arbitrage": {
                "band_usd_per_mmbtu": _q(arbitrage_band(params)),
                "components": {"freight": _q(float(params["freight_usd_per_mmbtu"])),
                               "regas": _q(float(params["regas_usd_per_mmbtu"]))},
                "diversion": dict(core["diversion"]),
                "capacity_case": core["capacity_case"],
            },
            "lng_flows": flows,
            "storage": {
                "capacity_bcm": _q(float(params["storage_capacity_bcm"]), 4),
                "start_bcm": _q(float(params["storage_stock_bcm"]), 4),
                "end_bcm": _q(mboe_to_bcm(storage_end_mb,
                                          btu_per_cf=float(params["btu_per_cf"]),
                                          mmbtu_per_boe=float(params["mmbtu_per_boe"])), 4),
                "end_share_of_capacity": _q(
                    100.0 * storage_end_mb / capacity_mb if capacity_mb else 0.0, 4),
                "drawn_bcm": _q(mboe_to_bcm(drawn_mb,
                                           btu_per_cf=float(params["btu_per_cf"]),
                                           mmbtu_per_boe=float(params["mmbtu_per_boe"])), 4),
                "min_share": _q(float(params["storage_min_share"]), 4),
                "cover_share": _q(float(params["storage_cover_share"]), 4),
                "max_draw_bcm_day": _q(float(params["storage_max_draw_bcm_day"]), 4),
                "monthly_release_bcm_day": [
                    _q(_bcm_day(params, package["plan"][m]), 6) for m in range(horizon)],
            },
            "physical": {
                "demand_mb_per_day": [_q(value) for value in europe.demand],
                "supply_after_shock_mb_per_day": [_q(value) for value in europe.supply_available],
                "lost_supply_mb_per_day": [_q(value) for value in package["lost"]],
                "diversion_mb_per_day": [_q(value) for value in package["added"]],
                "storage_release_mb_per_day": [_q(value) for value in package["plan"]],
                "pre_storage_gap_mb_per_day": [_q(value) for value in package["gap"]],
            },
            "ranges": {"ttf_peak": ttf_range, "jkm_peak": jkm_range},
            "sensitivity_count": len(sensitivity),
            "status_note": "research prototype: scenario arithmetic on declared assumptions, not a "
                           "forecast, not an admission and not a trading signal",
        }
        return summary

    # -- checks ------------------------------------------------------------------------
    def _checks(self, params: Mapping, core: dict, ttf_range: dict, jkm_range: dict,
                sensitivity: Sequence[Mapping]) -> dict:
        europe = core["europe"]
        asia = core["asia"]
        package = core["package"]
        diversion = float(core["diversion"]["diversion_bcm_y"])
        checks = {}
        for key, value in europe.checks.items():
            checks[f"ttf_{key}"] = bool(value)
        for key, value in asia.checks.items():
            checks[f"jkm_{key}"] = bool(value)
        checks["baseline_balance_closes"] = abs(eu_balance_bcm_y(params)) <= 1e-6
        global_total = [float(params["eu_lng_imports_bcm_y"])
                        + _flow_back(params, package["added"][m])
                        + float(params["asia_lng_supply_bcm_y"])
                        - _flow_back(params, core["asia_lost"][m])
                        + float(params["other_lng_bcm_y"]) for m in range(len(asia.price))]
        checks["global_lng_balance"] = all(
            abs(value - float(params["global_lng_supply_bcm_y"])) <= 1e-6 for value in global_total)
        # the diversion is a transfer: the volume Europe gains is exactly the volume Asia loses,
        # month by month, and both vectors are built independently of each other
        checks["diversion_is_a_transfer"] = all(
            abs(package["added"][m] - core["asia_lost"][m]) <= 1e-9
            for m in range(len(asia.price)))
        # the engine's own monthly supply must reproduce the declared shock arithmetic
        checks["eu_supply_identity"] = all(
            abs(europe.supply_available[m]
                - (_flow(params, eu_supply_bcm_y(params)) - package["lost"][m]
                   + package["added"][m] + package["plan"][m])) <= 1e-6
            for m in range(len(europe.price)))
        checks["asia_supply_identity"] = all(
            abs(asia.supply_available[m]
                - (_flow(params, float(params["asia_lng_supply_bcm_y"]))
                   - core["asia_lost"][m])) <= 1e-6
            for m in range(len(asia.price)))
        checks["diversion_within_capacity"] = (
            diversion <= float(params["lng_diversion_capacity_bcm_y"]) + 1e-6)
        checks["eu_imports_within_capacity"] = all(
            float(params["eu_lng_imports_bcm_y"]) + _flow_back(params, package["added"][m])
            <= float(params["eu_lng_import_capacity_bcm_y"]) + 1e-6
            for m in range(len(asia.price)))
        checks["storage_within_capacity"] = (_storage_stock_mb(params)
                                            <= bcm_to_mboe(float(params["storage_capacity_bcm"]),
                                                           btu_per_cf=float(params["btu_per_cf"]),
                                                           mmbtu_per_boe=float(
                                                               params["mmbtu_per_boe"])) + 1e-9)
        storage_end_mb = _storage_stock_mb(params) - math.fsum(package["plan"]) * DAYS_PER_MONTH
        checks["storage_min_respected"] = storage_end_mb >= _storage_min_mb(params) - 1e-6
        checks["storage_plan_within_deliverability"] = all(
            value <= bcm_day_to_mb_d(float(params["storage_max_draw_bcm_day"]),
                                     btu_per_cf=float(params["btu_per_cf"]),
                                     mmbtu_per_boe=float(params["mmbtu_per_boe"])) + 1e-9
            for value in package["plan"])
        checks["ttf_range_ordered"] = (ttf_range["low"] <= ttf_range["central"]
                                       <= ttf_range["high"])
        checks["jkm_range_ordered"] = (jkm_range["low"] <= jkm_range["central"]
                                       <= jkm_range["high"])
        checks["units_roundtrip"] = _units_roundtrip_ok(params)
        checks["assumptions_classified"] = all(
            item.source_class in ("observed", "derived", "assumption")
            for item in assumptions(params))
        if diversion > 0.0:
            binding = str(core["diversion"]["binding"])
            residual = core["diversion"]["residual"]
            if binding == "arbitrage":
                checks["arbitrage_consistent"] = abs(float(residual)) <= ARBITRAGE_TOLERANCE
            elif binding == "capacity":
                checks["arbitrage_consistent"] = float(residual) >= -ARBITRAGE_TOLERANCE
            else:
                checks["arbitrage_consistent"] = float(residual or 0.0) <= ARBITRAGE_TOLERANCE
        checks["sensitivity_non_empty"] = len(sensitivity) > 0
        checks["deterministic"] = True
        return checks

    # -- sensitivity -------------------------------------------------------------------
    def _sensitivity(self, params: Mapping, horizon: int, central: dict) -> list[dict]:
        base_peak = max(central["europe"].price)
        base_jkm_peak = max(central["asia"].price)
        results = []
        for name, overrides in sensitivity_variants(params):
            variant_params = {**params, **overrides}
            try:
                variant = _run_core(variant_params, horizon)
            except MarketError as error:  # fail-closed: a broken variant is reported, not hidden
                results.append({"variant": name, "calibration_overrides": dict(overrides),
                                "error": f"{type(error).__name__}: {error}"})
                continue
            peak = max(variant["europe"].price)
            jkm_peak = max(variant["asia"].price)
            drawn_mb = math.fsum(variant["package"]["plan"]) * DAYS_PER_MONTH
            end_bcm = mboe_to_bcm(_storage_stock_mb(params) - drawn_mb,
                                  btu_per_cf=float(params["btu_per_cf"]),
                                  mmbtu_per_boe=float(params["mmbtu_per_boe"]))
            results.append({
                "variant": name,
                "calibration_overrides": dict(overrides),
                "ttf_peak": _q(peak),
                "price_peak": _q(peak),
                "ttf_mean": _q(_mean(variant["europe"].price)),
                "jkm_peak": _q(jkm_peak),
                "jkm_mean": _q(_mean(variant["asia"].price)),
                "diversion_bcm_y": _q(float(variant["diversion"]["diversion_bcm_y"]), 4),
                "storage_end_bcm": _q(end_bcm, 4),
                "delta_vs_central": _q(peak - base_peak),
                "delta_pct_vs_central": _q(100.0 * (peak - base_peak) / base_peak, 4),
                "jkm_delta_pct_vs_central": _q(
                    100.0 * (jkm_peak - base_jkm_peak) / base_jkm_peak, 4),
                "checks": dict(variant["europe"].checks),
            })
        # a longer loss is a separate horizon: run it explicitly as a variant
        long_params = {**params, "pipeline_loss_months": horizon + 3,
                       "diversion_months": horizon + 3}
        long_horizon = horizon + 3
        try:
            long_core = _run_core(long_params, long_horizon)
            long_peak = max(long_core["europe"].price)
            long_jkm = max(long_core["asia"].price)
            results.append({
                "variant": "pipeline_loss_9m",
                "calibration_overrides": {"pipeline_loss_months": long_horizon},
                "ttf_peak": _q(long_peak),
                "price_peak": _q(long_peak),
                "ttf_mean": _q(_mean(long_core["europe"].price)),
                "jkm_peak": _q(long_jkm),
                "jkm_mean": _q(_mean(long_core["asia"].price)),
                "diversion_bcm_y": _q(float(long_core["diversion"]["diversion_bcm_y"]), 4),
                "storage_end_bcm": _q(mboe_to_bcm(
                    _storage_stock_mb(params)
                    - math.fsum(long_core["package"]["plan"]) * DAYS_PER_MONTH,
                    btu_per_cf=float(params["btu_per_cf"]),
                    mmbtu_per_boe=float(params["mmbtu_per_boe"])), 4),
                "delta_vs_central": _q(long_peak - base_peak),
                "delta_pct_vs_central": _q(100.0 * (long_peak - base_peak) / base_peak, 4),
                "jkm_delta_pct_vs_central": _q(
                    100.0 * (long_jkm - base_jkm_peak) / base_jkm_peak, 4),
                "checks": dict(long_core["europe"].checks),
            })
        except MarketError as error:
            results.append({"variant": "pipeline_loss_9m",
                            "calibration_overrides": {"pipeline_loss_months": long_horizon},
                            "error": f"{type(error).__name__}: {error}"})
        results.sort(key=lambda item: (item.get("ttf_peak", -1.0), item["variant"]))
        return results


def _bcm_day(params: Mapping, rate_mb_d: float) -> float:
    """An engine flow rate (``mb/d``) as ``bcm/day`` (for reporting the storage plan)."""
    return _flow_back(params, float(rate_mb_d)) / DAYS_PER_YEAR


def _units_roundtrip_ok(params: Mapping) -> bool:
    eur_usd = float(params["eur_usd"])
    btu_per_cf = float(params["btu_per_cf"])
    bcm = 1.0
    back = mmbtu_to_bcm(bcm_to_mmbtu(bcm, btu_per_cf=btu_per_cf), btu_per_cf=btu_per_cf)
    price = 30.0
    price_back = convert_price(convert_price(price, "EUR/MWh", "USD/MMBtu", eur_usd=eur_usd),
                               "USD/MMBtu", "EUR/MWh", eur_usd=eur_usd)
    flow_back = convert_flow(convert_flow(100.0, "bcm/y", "mboe/day",
                                          btu_per_cf=btu_per_cf,
                                          mmbtu_per_boe=float(params["mmbtu_per_boe"])),
                             "mboe/day", "bcm/y", btu_per_cf=btu_per_cf,
                             mmbtu_per_boe=float(params["mmbtu_per_boe"]))
    return (abs(back - bcm) <= 1e-12 and abs(price_back - price) <= 1e-9
            and abs(flow_back - 100.0) <= 1e-9)


# ------------------------------------------------------------------------------ report builder
LIMITATIONS: tuple[str, ...] = (
    "нет полного газового баланса: страны, ПХГ по странам, погода (HDD/CDD), топология "
    "трубопроводов, контракты take-or-pay и уголь-газ не моделируются;",
    "нет модели хранилищ: изъятие — объявленное правило (доля разрыва, лимит мощности, минимум "
    "запаса), а не оптимизация с арбитражем и сезонностью;",
    "Henry Hub не рассчитывается: это объявленный ориентир, а netback — арифметика от TTF;",
    "нет флота СПГ, портов, очередей и фрахтового рынка: фрахт — константа арбитражной полосы;",
    "все входы — допущения (source_class=assumption) с подсказкой replace_with; наблюдений нет; "
    "нельзя использовать вне исследовательского контура до замены;",
    "диапазон — оболочка объявленных вариантов чувствительности, а не доверительный интервал; "
    "калибровки на исторических эпизодах нет;",
    "статус RESEARCH_ONLY: не прогноз, не допуск, не инвестиционная рекомендация.",
)


def build_report(scenario_ids: Sequence[str] | None = None,
                 overrides: Mapping | None = None, module: GasModule | None = None) -> dict:
    """Run the requested scenarios and return one deterministic aggregate-only report."""
    module = module or GAS_MODULE
    available = [scenario.scenario_id for scenario in module.scenarios()]
    ids = list(scenario_ids) if scenario_ids else available
    unknown = [scenario_id for scenario_id in ids if scenario_id not in available]
    if unknown:
        raise ScenarioError(f"unknown scenario ids {unknown}; available {available}")
    if not ids:
        raise ScenarioError("at least one scenario is required")
    overrides = dict(overrides or {})
    documents = [module.run(scenario_id, **overrides).as_dict() for scenario_id in ids]
    payload = {
        "schema": SCHEMA_REPORT,
        "status": RESEARCH_ONLY,
        "research_only": True,
        "module": module.describe(),
        "scenario_ids": ids,
        "scenarios": documents,
        "limitations": list(LIMITATIONS),
        "note": "исследовательский прототип газового модуля: сценарная арифметика на объявленных "
                "допущениях, не прогноз, не допуск и не инвестиционная рекомендация",
    }
    payload["digest"] = _sha256_json(payload)
    return payload


#: The default module instance, registered so the framework exposes the gas passport.
GAS_MODULE = GasModule()


def register_gas_module() -> GasModule:
    """Register the gas module in the framework registry (idempotent for the same instance)."""
    from .contract import register
    return register(GAS_MODULE)  # type: ignore[return-value]


register_gas_module()
