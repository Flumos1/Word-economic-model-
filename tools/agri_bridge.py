#!/usr/bin/env python3
"""Agri/soy -> MRIO bridge (G2, docs/145, docs/153).

The bridge connects the agri research prototype (:mod:`amfe.markets_agri`, soybeans/soymeal in
Brazil and the illustrative African importer) to the **real** inter-country IO table (OECD ICIO
2011, 71 economies x 34 industries, canonical schema v1.1, USD million) **without modifying the
world core**.  It answers two separate questions and refuses to mix them:

1. **Price channel (modelled).** The soy/meal scenario delivers a price range; an explicit
   assumption layer converts it into exogenous cost pushes on food processing and animal
   production (direct soy/meal cost shares, feed shares); the cost pushes propagate through the
   Leontief price model ``p = (I - A')^-1 c`` built from the real MRIO input coefficients, with a
   pass-through bound for consumer food prices.
2. **Quantity channel (not modelled).** No output, volume or GDP response is claimed: that needs a
   demand/substitution model, which this prototype does not have.  Import-bill arithmetic holds
   baseline volumes constant and is labelled as such; the African effect is reported from the agri
   module and is never added to the global price totals (no double counting).

Key rules encoded:

* **Real MRIO, read-only.** The tool imports ``amfe.mrio.loaders`` and reads the canonical
  dataset; it never writes into ``src/`` or the world core.  Only the aggregated cost structure is
  cached under ``build/agri_bridge/`` (with the dataset SHA-256 and digest), so a rerun is fast and
  auditable.
* **Explicit assumption layer.** Soy is not an ICIO sector, so the soy/meal cost shares and the
  feed shares are declared assumptions (``source_class="assumption"``), listed in the output and in
  docs/153.  MRIO supplies the rest of the cost structure (real A coefficients and value-added
  shares).
* **No point claims.** The scenario output is a range between two pre-declared corners
  (low/high impact); every price number carries ``point_estimate: false``.
* **Fail-closed.** A missing/unreadable dataset (and no cache), non-canonical units, a singular (or
  nearly singular) technology matrix, a column cost share >= 1 (no positive value added), a
  negative/NaN cost push, or out-of-order bounds raise :class:`BridgeError` instead of guessing.
* **Deterministic.** No clock, no random numbers: the same inputs give byte-identical JSON and the
  same SHA-256 digest (credentials: dataset hash + cache digest + module versions).

CLI::

    python tools/agri_bridge.py                     # real MRIO (cache), writes build/agri_bridge/
    python tools/agri_bridge.py --synthetic --json  # offline synthetic structure (tests/demo)
    python tools/agri_bridge.py --rebuild-cache     # recompute the MRIO cache (~15-20 s)
    python tools/agri_bridge.py --self-test --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets_agri import AgriMarketError, SoyAgriModule  # noqa: E402

try:  # canonical MRIO loader (read-only dependency; the world core is never modified)
    from amfe.mrio.loaders import load_dataset  # noqa: E402
    from amfe.mrio.schema import MRIOError  # noqa: E402
    MRIO_LOADER_AVAILABLE = True
except Exception:  # noqa: BLE001 - the bridge must import for the synthetic path as well
    load_dataset = None
    MRIOError = Exception
    MRIO_LOADER_AVAILABLE = False

SCHEMA = "AMFE-AGRI-BRIDGE/1.0.0"
CACHE_SCHEMA = "AMFE-AGRI-MRIO-COST-STRUCTURE/1.0.0"
DEFAULT_OUT = ROOT / "build" / "agri_bridge"
DEFAULT_CACHE = DEFAULT_OUT / "mrio_cost_structure.json"
DEFAULT_DATASET = Path("F:/Мировая экономика модель/data/mrio/oecd_icio/datasets/2011.json")
MRIO_GROUPS = ("AGRI", "FOOD", "OTHER")
PRICE_GROUPS = ("AGRI", "FOOD", "OTHER", "ANIMAL")
AGRI_INDUSTRY = "C01T05AGR"   # OECD ICIO agriculture, hunting, forestry and fishing
FOOD_INDUSTRY = "C15T16FOD"   # OECD ICIO food products, beverages and tobacco
SCENARIO_NAME = "Brazil_weather_shock_-15pct_plus_export_restriction"
SCENARIO_SHOCKS = {"brazil_yield_shock": -0.15, "brazil_export_cut": 0.35}
NOT_A_FORECAST = True


class BridgeError(ValueError):
    """Invalid bridge input/output: the bridge fails closed instead of guessing."""


# ------------------------------------------------------------------------------- assumptions
@dataclass(frozen=True)
class BridgeAssumptions:
    """The explicit soy/meal assumption layer on top of the real MRIO coefficients.

    All shares are fractions of the producing sector's output value; the MRIO supplies the
    remaining cost structure.  The values are rounded assumptions (``source_class="assumption"``),
    not estimates: docs/153 §4 lists what would be needed to calibrate them on real data.
    """

    soy_cost_share_food: float = 0.020     # direct soybean input per unit of food output
    meal_cost_share_food: float = 0.015    # direct soymeal input per unit of food output
    feed_cost_share_animal: float = 0.450  # feed share of animal production costs
    soybean_share_of_feed: float = 0.050   # soybean share within feed
    soymeal_share_of_feed: float = 0.300   # soymeal share within feed
    va_share_animal: float = 0.150         # value added + other costs share of animal output
    pass_through_low: float = 0.50
    pass_through_high: float = 0.90
    food_cpi_weight_low: float = 0.150     # food share of the consumer basket
    food_cpi_weight_high: float = 0.300
    processed_share_of_food: float = 0.60  # FOOD-group share inside the food basket
    meat_share_of_food: float = 0.25       # ANIMAL-group share inside the food basket

    def __post_init__(self) -> None:
        for name in ("soy_cost_share_food", "meal_cost_share_food", "feed_cost_share_animal",
                     "soybean_share_of_feed", "soymeal_share_of_feed", "va_share_animal",
                     "pass_through_low", "pass_through_high", "food_cpi_weight_low",
                     "food_cpi_weight_high", "processed_share_of_food", "meat_share_of_food"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not (0.0 <= value <= 1.0):
                raise BridgeError(f"{name} must be a finite fraction in [0, 1], got {value}")
        if self.feed_cost_share_animal + self.va_share_animal > 1.0 + 1e-12:
            raise BridgeError("feed_cost_share_animal + va_share_animal must not exceed 1")
        if self.soybean_share_of_feed + self.soymeal_share_of_feed > 1.0 + 1e-12:
            raise BridgeError("soybean_share_of_feed + soymeal_share_of_feed must not exceed 1")
        if self.soy_cost_share_food + self.meal_cost_share_food > 1.0 + 1e-12:
            raise BridgeError("soy cost shares of food must not exceed the food output")
        if self.pass_through_low > self.pass_through_high:
            raise BridgeError("pass-through bounds must be ordered low <= high")
        if self.food_cpi_weight_low > self.food_cpi_weight_high:
            raise BridgeError("food CPI weight bounds must be ordered low <= high")
        if self.processed_share_of_food + self.meat_share_of_food > 1.0 + 1e-12:
            raise BridgeError("processed + meat shares of the food basket must not exceed 1")

    def to_dict(self) -> dict:
        return {"soy_cost_share_food": self.soy_cost_share_food,
                "meal_cost_share_food": self.meal_cost_share_food,
                "feed_cost_share_animal": self.feed_cost_share_animal,
                "soybean_share_of_feed": self.soybean_share_of_feed,
                "soymeal_share_of_feed": self.soymeal_share_of_feed,
                "va_share_animal": self.va_share_animal,
                "pass_through": [self.pass_through_low, self.pass_through_high],
                "food_cpi_weight": [self.food_cpi_weight_low, self.food_cpi_weight_high],
                "processed_share_of_food": self.processed_share_of_food,
                "meat_share_of_food": self.meat_share_of_food,
                "source_class": "assumption"}


# ---------------------------------------------------------------------------- cost structure
@dataclass(frozen=True)
class CostStructure:
    """Aggregated MRIO cost structure: 3 real groups + per-country input coefficients."""

    schema: str
    dataset: dict
    countries: tuple[str, ...]
    groups: tuple[str, ...]
    world_a: np.ndarray               # (3, 3) input group x using group
    country_a: dict[str, np.ndarray]  # country -> (3, 3)
    country_output_usd_mn: dict[str, dict[str, float]]
    world_output_usd_mn: dict[str, float]
    source_class: str = "observed"

    def __post_init__(self) -> None:
        if self.schema != CACHE_SCHEMA:
            raise BridgeError(f"unexpected cost-structure schema {self.schema!r}")
        if self.groups != MRIO_GROUPS:
            raise BridgeError(f"unexpected groups {self.groups!r}")
        world = np.asarray(self.world_a, dtype=float)
        if world.shape != (3, 3) or not np.all(np.isfinite(world)):
            raise BridgeError("world_a must be a finite 3x3 matrix")
        if np.any(world < -1e-12):
            raise BridgeError("input coefficients must be non-negative")
        column_sums = world.sum(axis=0)
        if np.any(column_sums >= 1.0):
            raise BridgeError(f"a world column cost share is >= 1 (no positive value added): "
                              f"{column_sums.tolist()}")
        if self.dataset.get("units") != "USD_mn":
            raise BridgeError(f"canonical units must be USD_mn, got {self.dataset.get('units')!r}")
        for country, matrix in self.country_a.items():
            array = np.asarray(matrix, dtype=float)
            if array.shape != (3, 3) or not np.all(np.isfinite(array)):
                raise BridgeError(f"country_a[{country}] must be a finite 3x3 matrix")
            if np.any(array < -1e-12):
                raise BridgeError(f"country_a[{country}] has negative coefficients")
            if np.any(array.sum(axis=0) >= 1.0 + 1e-9):
                raise BridgeError(f"country_a[{country}] has a column cost share >= 1")

    def augmented_matrix(self, assumptions: BridgeAssumptions) -> np.ndarray:
        """4x4 matrix [AGRI, FOOD, OTHER, ANIMAL]; ANIMAL is a virtual sector built from assumptions."""
        matrix = np.zeros((4, 4), dtype=float)
        matrix[:3, :3] = np.asarray(self.world_a, dtype=float)
        matrix[0, 3] = assumptions.feed_cost_share_animal
        matrix[2, 3] = 1.0 - assumptions.feed_cost_share_animal - assumptions.va_share_animal
        return matrix

    def augmented_country_matrix(self, country: str,
                                 assumptions: BridgeAssumptions) -> np.ndarray:
        if country not in self.country_a:
            raise BridgeError(f"unknown country {country!r}")
        matrix = np.zeros((4, 4), dtype=float)
        matrix[:3, :3] = np.asarray(self.country_a[country], dtype=float)
        matrix[0, 3] = assumptions.feed_cost_share_animal
        matrix[2, 3] = 1.0 - assumptions.feed_cost_share_animal - assumptions.va_share_animal
        return matrix

    def payload(self) -> dict:
        return {"schema": self.schema, "dataset": dict(self.dataset),
                "countries": list(self.countries), "groups": list(self.groups),
                "world_a": np.asarray(self.world_a).round(12).tolist(),
                "country_a": {name: np.asarray(matrix).round(12).tolist()
                              for name, matrix in sorted(self.country_a.items())},
                "country_output_usd_mn": {name: dict(values)
                                          for name, values in sorted(
                                              self.country_output_usd_mn.items())},
                "world_output_usd_mn": dict(self.world_output_usd_mn),
                "source_class": self.source_class}

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict) -> "CostStructure":
        try:
            dataset = dict(payload["dataset"])
            countries = tuple(payload["countries"])
            groups = tuple(payload["groups"])
            world_a = np.asarray(payload["world_a"], dtype=float)
            country_a = {str(name): np.asarray(matrix, dtype=float)
                         for name, matrix in payload["country_a"].items()}
            country_output = {str(name): {str(key): float(value) for key, value in values.items()}
                              for name, values in payload["country_output_usd_mn"].items()}
            world_output = {str(key): float(value)
                            for key, value in payload["world_output_usd_mn"].items()}
            source_class = str(payload.get("source_class", "observed"))
        except (KeyError, TypeError, ValueError) as error:
            raise BridgeError(f"malformed cost-structure cache: {error}") from error
        return cls(schema=str(payload.get("schema")), dataset=dataset, countries=countries,
                   groups=groups, world_a=world_a, country_a=country_a,
                   country_output_usd_mn=country_output, world_output_usd_mn=world_output,
                   source_class=source_class)


def synthetic_cost_structure(countries: tuple[str, ...] = ("AAA", "BBB", "CCC"),
                             assumptions: BridgeAssumptions | None = None) -> CostStructure:
    """A small deterministic structure for tests and the offline ``--synthetic`` demo."""
    base = np.array([[0.10, 0.25, 0.02],
                     [0.05, 0.12, 0.03],
                     [0.20, 0.22, 0.35]], dtype=float)
    country_a = {}
    country_output = {}
    for index, country in enumerate(countries):
        factor = 1.0 + 0.10 * index
        country_a[country] = base * factor * 0.9
        country_output[country] = {"AGRI": 100.0 * factor, "FOOD": 300.0 * factor,
                                   "OTHER": 900.0 * factor}
    world_output = {group: float(sum(values[group] for values in country_output.values()))
                    for group in MRIO_GROUPS}
    dataset = {"path": "synthetic://agri_bridge", "sha256": "0" * 64,
               "digest": hashlib.sha256(b"synthetic-agri-bridge").hexdigest(),
               "year": 2011, "units": "USD_mn", "countries": len(countries),
               "industries": 34, "source": "synthetic demo structure (tests only)"}
    return CostStructure(schema=CACHE_SCHEMA, dataset=dataset, countries=tuple(countries),
                         groups=MRIO_GROUPS, world_a=base, country_a=country_a,
                         country_output_usd_mn=country_output, world_output_usd_mn=world_output,
                         source_class="synthetic")


def build_cost_structure(dataset_path: Path | str, year: int | None = None,
                         expected_sha256: str | None = None) -> CostStructure:
    """Aggregate the canonical MRIO dataset into the bridge cost structure (read-only, fail-closed)."""
    if not MRIO_LOADER_AVAILABLE:
        raise BridgeError("the canonical MRIO loader is unavailable (amfe.mrio.loaders)")
    path = Path(dataset_path)
    if not path.is_file():
        raise BridgeError(f"MRIO dataset is missing: {path}")
    try:
        loaded = load_dataset(str(path), expected_sha256=expected_sha256)
    except MRIOError as error:
        raise BridgeError(f"the MRIO dataset was rejected by the canonical loader: {error}") \
            from error
    except Exception as error:  # noqa: BLE001 - any loader failure is fail-closed here
        raise BridgeError(f"cannot load the MRIO dataset {path}: {error}") from error
    dataset = loaded.dataset
    units = str(getattr(dataset, "units", ""))
    if units != "USD_mn":
        raise BridgeError(f"canonical units must be USD_mn, got {units!r}")
    years = tuple(int(item) for item in getattr(dataset, "years", ()))
    if year is not None and int(year) not in years:
        raise BridgeError(f"requested year {year} is not in the dataset years {years}")
    industries = tuple(dataset.industries)
    countries = tuple(dataset.countries)
    if AGRI_INDUSTRY not in industries or FOOD_INDUSTRY not in industries:
        raise BridgeError(f"the dataset does not contain the expected industries: "
                          f"{AGRI_INDUSTRY} and {FOOD_INDUSTRY}; got {industries[:6]}...")
    z = np.asarray(dataset.Z, dtype=float)
    x = np.asarray(dataset.x, dtype=float)
    n_industries, n_countries = len(industries), len(countries)
    n = n_industries * n_countries
    if z.shape != (n, n) or x.shape != (n,):
        raise BridgeError(f"unexpected table shape: Z {z.shape}, x {x.shape}, expected ({n}, {n})")
    if not np.all(np.isfinite(z)) or not np.all(np.isfinite(x)):
        raise BridgeError("the MRIO table contains non-finite values")
    if np.any(x < 0.0):
        raise BridgeError("the MRIO table contains negative output values")
    if float(x.sum()) <= 0.0:
        raise BridgeError("the MRIO table has no positive output at all")
    zero_output = int(np.count_nonzero(x == 0.0))
    if zero_output and float(np.abs(z[:, x == 0.0]).sum()) > 1e-6:
        raise BridgeError(f"{zero_output} zero-output columns carry intermediate flows: "
                          "inconsistent table")
    group_of_industry = np.array([0 if name == AGRI_INDUSTRY else
                                  1 if name == FOOD_INDUSTRY else 2
                                  for name in industries], dtype=int)
    group_origin = np.tile(group_of_industry, n_countries)   # row index = country*n_ind + industry
    country_of_cell = np.repeat(np.arange(n_countries), n_industries)
    # origin aggregation: R (3 x n) then v = R @ Z (3 x n): flows by origin group and destination cell
    incidence = np.zeros((3, n), dtype=float)
    for group in range(3):
        incidence[group, group_origin == group] = 1.0
    flows_by_origin = incidence @ z                        # (3, n), float64
    x_by_group_country = np.zeros((3, n_countries), dtype=float)
    for group in range(3):
        mask = group_origin == group
        x_by_group_country[group] = np.bincount(country_of_cell[mask], weights=x[mask],
                                                minlength=n_countries)
    # world A: numerator = flows summed over destination cells of each group
    world_num = np.zeros((3, 3), dtype=float)
    world_den = x_by_group_country.sum(axis=1)
    for destination_group in range(3):
        mask = group_origin == destination_group
        world_num[:, destination_group] = flows_by_origin[:, mask].sum(axis=1)
    for group in range(3):
        if world_den[group] <= 0.0:
            raise BridgeError(f"world output of group {MRIO_GROUPS[group]} is not positive")
    world_a = world_num / world_den[None, :]
    # country A: for destination country c and destination group g, denominator = its group output
    country_a: dict[str, np.ndarray] = {}
    country_output: dict[str, dict[str, float]] = {}
    for index, country in enumerate(countries):
        matrix = np.zeros((3, 3), dtype=float)
        outputs = {}
        for destination_group, group_name in enumerate(MRIO_GROUPS):
            mask = (group_origin == destination_group) & (country_of_cell == index)
            denominator = float(x[mask].sum())
            outputs[group_name] = round(denominator, 6)
            if denominator <= 0.0:
                continue
            numerator = flows_by_origin[:, mask].sum(axis=1)
            matrix[:, destination_group] = numerator / denominator
        country_a[country] = matrix
        country_output[country] = outputs
    world_output = {group: round(float(world_den[index]), 6)
                    for index, group in enumerate(MRIO_GROUPS)}
    source_block = getattr(dataset, "source", {})
    source_name = "OECD ICIO"
    if isinstance(source_block, dict):
        source_name = str(source_block.get("source") or source_block.get("name") or source_name)
    dataset_info = {"path": str(path).replace("\\", "/"), "sha256": loaded.sha256,
                    "digest": str(dataset.describe().get("digest", "")),
                    "year": int(years[0]) if years else None, "units": units,
                    "countries": n_countries, "industries": n_industries,
                    "source": source_name,
                    "cells_Z": int(z.size), "bytes": int(loaded.size_bytes)}
    structure = CostStructure(schema=CACHE_SCHEMA, dataset=dataset_info,
                              countries=countries, groups=MRIO_GROUPS, world_a=world_a,
                              country_a=country_a, country_output_usd_mn=country_output,
                              world_output_usd_mn=world_output, source_class="observed")
    return structure


# --------------------------------------------------------------------------------- price model
def solve_leontief_prices(matrix: np.ndarray, cost_push: np.ndarray) -> np.ndarray:
    """Solve ``p = (I - A')^-1 c`` and verify the residual and the spectral condition."""
    a = np.asarray(matrix, dtype=float)
    c = np.asarray(cost_push, dtype=float)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise BridgeError(f"the technology matrix must be square, got {a.shape}")
    if c.shape != (a.shape[0],):
        raise BridgeError(f"cost push {c.shape} does not match {a.shape[0]} groups")
    if not np.all(np.isfinite(a)) or not np.all(np.isfinite(c)):
        raise BridgeError("the technology matrix and the cost push must be finite")
    if np.any(a < -1e-12):
        raise BridgeError("input coefficients must be non-negative")
    if np.any(a.sum(axis=0) >= 1.0):
        raise BridgeError("a column cost share is >= 1: no positive value added")
    radius = float(np.max(np.abs(np.linalg.eigvals(a))))
    if not math.isfinite(radius) or radius >= 1.0:
        raise BridgeError(f"the technology matrix is not productive: spectral radius {radius:.6f}")
    system = np.eye(a.shape[0]) - a.T
    if abs(float(np.linalg.det(system))) < 1e-14:
        raise BridgeError("the Leontief system is singular")
    prices = np.linalg.solve(system, c)
    residual = float(np.max(np.abs(system @ prices - c)))
    if residual > 1e-9 * max(1.0, float(np.max(np.abs(c)))):
        raise BridgeError(f"the Leontief residual is too large: {residual:.3e}")
    return prices


def direct_cost_push(soybean_change: float, meal_change: float,
                     assumptions: BridgeAssumptions) -> np.ndarray:
    """Exogenous cost pushes on [AGRI, FOOD, OTHER, ANIMAL] from soy/meal price changes."""
    for name, value in (("soybean_change", soybean_change), ("meal_change", meal_change)):
        if not math.isfinite(value):
            raise BridgeError(f"{name} must be finite")
        if value <= -1.0:
            raise BridgeError(f"{name} must be > -100%")
    soy = float(soybean_change)
    meal = float(meal_change)
    food = assumptions.soy_cost_share_food * soy + assumptions.meal_cost_share_food * meal
    animal = assumptions.feed_cost_share_animal * (
        assumptions.soybean_share_of_feed * soy + assumptions.soymeal_share_of_feed * meal)
    push = np.zeros(4, dtype=float)
    push[1] = food
    push[3] = animal
    return push


def food_consumer_effect(food_price_change: float, animal_price_change: float,
                         assumptions: BridgeAssumptions, pass_through: float,
                         food_weight: float) -> float:
    """Consumer food-price contribution in percent of the food CPI (assumption pass-through)."""
    for name, value in (("food_price_change", food_price_change),
                        ("animal_price_change", animal_price_change),
                        ("pass_through", pass_through), ("food_weight", food_weight)):
        if not math.isfinite(value):
            raise BridgeError(f"{name} must be finite")
    basket = (assumptions.processed_share_of_food * food_price_change
              + assumptions.meat_share_of_food * animal_price_change)
    return 100.0 * food_weight * pass_through * basket


# --------------------------------------------------------------------------------------- bridge
def _price_range(result: dict, commodity: str) -> dict:
    try:
        interval = result["price_ranges"][commodity]
    except KeyError as error:
        raise BridgeError(f"the agri result has no price range for {commodity!r}") from error
    low, high = float(interval["low"]), float(interval["high"])
    if not (0.0 < low <= high):
        raise BridgeError(f"{commodity}: price range must satisfy 0 < low <= high, got {low}/{high}")
    return {"low": low, "high": high, "unit": interval.get("unit", "USD/t")}


def run_bridge(structure: CostStructure, assumptions: BridgeAssumptions | None = None,
               scenario: dict | None = None, base_soy_price: float = 420.0,
               base_meal_price: float = 330.0) -> dict:
    """Run the price bridge on one cost structure; returns the serialisable report (no clock)."""
    assumptions = BridgeAssumptions() if assumptions is None else assumptions
    if not isinstance(structure, CostStructure):
        raise BridgeError("structure must be a CostStructure")
    scenario = dict(scenario or {"name": SCENARIO_NAME, "shocks": dict(SCENARIO_SHOCKS)})
    soy = _price_range(scenario["agri"], "soybeans") if "agri" in scenario else None
    if soy is None:
        raise BridgeError("the scenario must carry the agri module result under 'agri'")
    agri = scenario["agri"]
    soy_range = _price_range(agri, "soybeans")
    meal_range = _price_range(agri, "soymeal")
    if not (base_soy_price > 0.0 and base_meal_price > 0.0):
        raise BridgeError("base prices must be positive")
    soy_change = {"low": soy_range["low"] / base_soy_price - 1.0,
                  "high": soy_range["high"] / base_soy_price - 1.0}
    meal_change = {"low": meal_range["low"] / base_meal_price - 1.0,
                   "high": meal_range["high"] / base_meal_price - 1.0}
    world_matrix = structure.augmented_matrix(assumptions)
    prices = {}
    for corner in ("low", "high"):
        push = direct_cost_push(soy_change[corner], meal_change[corner], assumptions)
        delta = solve_leontief_prices(world_matrix, push)
        prices[corner] = {"soybean_change": soy_change[corner], "meal_change": meal_change[corner],
                          "cost_push": {group: round(float(push[index]), 10)
                                        for index, group in enumerate(PRICE_GROUPS)},
                          "producer_price_change": {group: round(float(delta[index]), 10)
                                                    for index, group in enumerate(PRICE_GROUPS)}}
        for index, group in enumerate(PRICE_GROUPS):
            if delta[index] < -1e-12:
                raise BridgeError(f"a negative price change for {group} in the {corner} corner: "
                                  f"a supply cut cannot lower prices")
    # consumer food-price effect: corners x pass-through x food weight (all bounds declared)
    consumer = {}
    for corner in ("low", "high"):
        food_change = prices[corner]["producer_price_change"]["FOOD"]
        animal_change = prices[corner]["producer_price_change"]["ANIMAL"]
        low = food_consumer_effect(food_change, animal_change, assumptions,
                                   assumptions.pass_through_low,
                                   assumptions.food_cpi_weight_low)
        high = food_consumer_effect(food_change, animal_change, assumptions,
                                    assumptions.pass_through_high,
                                    assumptions.food_cpi_weight_high)
        consumer[corner] = {"point_estimate": False,
                            "food_cpi_contribution_pp": {"low": round(min(low, high), 10),
                                                         "high": round(max(low, high), 10)}}
    consumer_range = {"low": min(consumer["low"]["food_cpi_contribution_pp"]["low"],
                                 consumer["high"]["food_cpi_contribution_pp"]["low"]),
                      "high": max(consumer["low"]["food_cpi_contribution_pp"]["high"],
                                  consumer["high"]["food_cpi_contribution_pp"]["high"])}
    if consumer_range["low"] < 0.0:
        raise BridgeError("the consumer food-price range cannot be negative for a supply cut")
    # country dimension: producer food prices and the consumer contribution
    country_rows = []
    for country in structure.countries:
        try:
            matrix = structure.augmented_country_matrix(country, assumptions)
            delta_low = solve_leontief_prices(matrix, direct_cost_push(
                soy_change["low"], meal_change["low"], assumptions))
            delta_high = solve_leontief_prices(matrix, direct_cost_push(
                soy_change["high"], meal_change["high"], assumptions))
        except BridgeError as error:
            raise BridgeError(f"country {country}: {error}") from error
        delta_food, delta_food_high = float(delta_low[1]), float(delta_high[1])
        food_low = food_consumer_effect(delta_food, float(delta_low[3]), assumptions,
                                        assumptions.pass_through_low,
                                        assumptions.food_cpi_weight_low)
        food_high = food_consumer_effect(delta_food_high, float(delta_high[3]), assumptions,
                                         assumptions.pass_through_high,
                                         assumptions.food_cpi_weight_high)
        country_rows.append({
            "country": country,
            "food_producer_price_change": {"low": round(float(delta_food), 10),
                                           "high": round(float(delta_food_high), 10)},
            "food_cpi_contribution_pp": {"low": round(min(food_low, food_high), 10),
                                         "high": round(max(food_low, food_high), 10)},
            "food_output_usd_mn": structure.country_output_usd_mn.get(country, {}).get("FOOD"),
        })
    country_rows.sort(key=lambda row: row["food_cpi_contribution_pp"]["high"], reverse=True)
    africa = agri.get("effects", {}).get("africa")
    if africa is None:
        raise BridgeError("the agri result has no Africa block")
    report = {
        "schema": SCHEMA,
        "scenario": {"name": scenario.get("name", SCENARIO_NAME),
                     "shocks": scenario.get("shocks", SCENARIO_SHOCKS),
                     "agri_module_digest": scenario.get("agri_digest"),
                     "source_class": "assumption"},
        "data": {"mrio": dict(structure.dataset),
                 "cost_structure": {"schema": structure.schema,
                                    "digest": structure.digest,
                                    "groups": list(PRICE_GROUPS),
                                    "source_class": structure.source_class,
                                    "world_matrix": np.asarray(world_matrix).round(10).tolist(),
                                    "column_cost_shares": np.asarray(
                                        world_matrix).sum(axis=0).round(10).tolist(),
                                    "value_added_share_implied": (1.0 - np.asarray(
                                        world_matrix).sum(axis=0)).round(10).tolist()},
                 "assumptions": assumptions.to_dict(),
                 "units": "USD_mn for the MRIO table; price changes are fractions of the base "
                          "price or percent for the CPI contribution"},
        "prices": {"soybeans": {"base_usd_t": base_soy_price,
                                "scenario_range": soy_range,
                                "change_pct": {"low": round(100.0 * soy_change["low"], 6),
                                               "high": round(100.0 * soy_change["high"], 6)}},
                   "soymeal": {"base_usd_t": base_meal_price,
                               "scenario_range": meal_range,
                               "change_pct": {"low": round(100.0 * meal_change["low"], 6),
                                              "high": round(100.0 * meal_change["high"], 6)}}},
        "leontief": {"model": "p = (I - A')^-1 c; A from the real MRIO coefficients, augmented by "
                              "the declared feed/animal assumptions",
                     "corners": prices,
                     "producer_price_change_pct": {
                         corner: {group: round(100.0 * prices[corner]["producer_price_change"][group], 6)
                                  for group in PRICE_GROUPS}
                         for corner in ("low", "high")}},
        "consumer_food_prices": {"method": "pass-through x food weight x (processed food price + "
                                           "meat price)",
                                 "corners": consumer,
                                 "range_pp": {"low": round(consumer_range["low"], 10),
                                              "high": round(consumer_range["high"], 10)},
                                 "point_estimate": False},
        "countries": {"top_by_consumer_food_effect": country_rows[:8],
                      "bottom_by_consumer_food_effect": country_rows[-5:],
                      "count": len(country_rows),
                      "note": "country producer-price effects come from each country's own cost "
                              "structure; the consumer step is an assumption pass-through"},
        "africa": {"source": "amfe.markets_agri.SoyAgriModule",
                   "source_class": africa.get("source_class", "assumption"),
                   "import_bill_change_usd_mn": africa.get("import_bill_change_usd_mn"),
                   "import_bill_change_pct": africa.get("import_bill_change_pct"),
                   "food_inflation_pp_range": africa.get("food_inflation_pp_range"),
                   "baseline_import_bill_usd_mn": africa.get("baseline_import_bill_usd_mn"),
                   "included_in_world_totals": False,
                   "no_double_counting": "the African consumer/import effect is the importer-side "
                                          "mirror of the same price shock; it is reported "
                                          "separately and is not added to the global producer/"
                                          "consumer price effects"},
        "separation": {
            "price_based": ["soybean/soymeal price range",
                            "producer-price changes by group and country",
                            "consumer food-price contribution range",
                            "African import-bill change at constant baseline volumes"],
            "quantity_based": {"modelled": False,
                               "volumes_held_constant": True,
                               "not_claimed": ["output loss", "volume change", "GDP effect",
                                               "welfare effect", "trade reallocation"],
                               "reason": "no demand/substitution/storage model in this bridge; the "
                                         "import-bill arithmetic keeps the agri-module baseline "
                                         "volumes fixed and is therefore a price channel only",
                               "would_require": ["demand elasticity system",
                                                 "cross-crop and cross-origin substitution",
                                                 "storage and trade-policy response",
                                                 "PIT trade flows by partner"]},
            "statement": "the bridge reports price and import-bill effects; it makes no quantity "
                         "claims and no point forecast"},
        "provenance": {"mrio_dataset_sha256": structure.dataset.get("sha256"),
                       "mrio_dataset_digest": structure.dataset.get("digest"),
                       "mrio_licence": "OECD ICIO terms; derived aggregates only, raw table not "
                                       "committed",
                       "agri_module_source_class": "assumption",
                       "not_a_forecast": NOT_A_FORECAST,
                       "no_admission_claim": True},
        "limitations": [
            "the MRIO table is OECD ICIO 2011 (71x34); a 2025 vintage changes the coefficients "
            "but not the method",
            "soy is not an ICIO sector: the soy/meal cost shares and feed shares are declared "
            "assumptions, not estimates",
            "the ANIMAL sector is virtual (feed share + value-added share); no separate livestock "
            "table exists in ICIO",
            "the Leontief price model is linear and static: no substitution, no demand response, "
            "no storage and no policy reaction",
            "consumer pass-through and food-basket weights are assumption bounds, not estimated "
            "CPI weights",
            "Africa is not a separate MRIO region in the 71-economy table: its effect is taken "
            "from the agri module (illustrative aggregate importer) and is not derived from the "
            "MRIO propagation",
            "quantity effects are explicitly not modelled and not claimed",
            "research prototype: not an admission candidate, not a forecast, not investment advice",
        ],
    }
    report["digest"] = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")).encode("utf-8")).hexdigest()
    return report


# ------------------------------------------------------------------------------ report / output
def render_markdown(report: dict) -> str:
    lines = ["# Agri/soy -> MRIO bridge (G2)", ""]
    data = report["data"]
    mrio = data["mrio"]
    prices = report["prices"]
    lines.append(f"Scenario: **{report['scenario']['name']}** · MRIO: "
                 f"{mrio.get('source', 'OECD ICIO')} {mrio.get('year')} · "
                 f"SHA-256 `{str(mrio.get('sha256'))[:16]}…`")
    lines.append("")
    lines.append("## Price channel (modelled)")
    lines.append("")
    lines.append("| Commodity | Base, USD/t | Scenario range, USD/t | Change, % low-high |")
    lines.append("|---|---|---|---|")
    for commodity, label in (("soybeans", "Soybeans"), ("soymeal", "Soymeal")):
        block = prices[commodity]
        lines.append(f"| {label} | {block['base_usd_t']:.2f} | "
                     f"{block['scenario_range']['low']:.2f} - {block['scenario_range']['high']:.2f} | "
                     f"{block['change_pct']['low']:+.2f} … {block['change_pct']['high']:+.2f} |")
    lines.append("")
    lines.append("### Producer prices (Leontief price model, % change)")
    lines.append("")
    lines.append("| Group | Low corner | High corner |")
    lines.append("|---|---|---|")
    for group in PRICE_GROUPS:
        low = report["leontief"]["producer_price_change_pct"]["low"][group]
        high = report["leontief"]["producer_price_change_pct"]["high"][group]
        lines.append(f"| {group} | {low:+.4f} | {high:+.4f} |")
    lines.append("")
    consumer = report["consumer_food_prices"]["range_pp"]
    lines.append(f"Consumer food-price contribution (assumption pass-through): "
                 f"**{consumer['low']:+.3f} … {consumer['high']:+.3f} pp** (not a point estimate)")
    lines.append("")
    lines.append("## African importer block (from the agri module, not added to world totals)")
    lines.append("")
    africa = report["africa"]
    bill = africa["import_bill_change_usd_mn"]
    pct = africa["import_bill_change_pct"]
    food = africa["food_inflation_pp_range"]
    lines.append(f"- Baseline import bill: **{africa['baseline_import_bill_usd_mn']:.1f} USD mn**")
    lines.append(f"- Bill change: **{bill['low']:+.1f} … {bill['high']:+.1f} USD mn** "
                 f"({pct['low']:+.2f} … {pct['high']:+.2f} %)")
    lines.append(f"- Food-inflation contribution: **{food['low']:+.4f} … {food['high']:+.4f} pp**")
    lines.append(f"- `included_in_world_totals = {africa['included_in_world_totals']}`: no double "
                 "counting")
    lines.append("")
    lines.append("## Quantity channel: NOT modelled")
    lines.append("")
    separation = report["separation"]
    quantity = separation["quantity_based"]
    lines.append(f"`modelled = {quantity['modelled']}`; volumes held at baseline: "
                 f"`{quantity['volumes_held_constant']}`. Not claimed: "
                 + ", ".join(quantity["not_claimed"]) + ".")
    lines.append("")
    lines.append(f"Statement: {separation['statement']}.")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    lines.append(f"Report digest: `{report['digest']}`")
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict, out_dir: Path | str = DEFAULT_OUT) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "agri_bridge.json"
    md_path = out / "agri_bridge.md"
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return {"json": str(json_path).replace("\\", "/"),
            "markdown": str(md_path).replace("\\", "/"),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": len(payload.encode("utf-8"))}


# ------------------------------------------------------------------------------------ cache I/O
def load_or_build_structure(cache_path: Path | str = DEFAULT_CACHE,
                            dataset_path: Path | str = DEFAULT_DATASET,
                            rebuild: bool = False) -> tuple[CostStructure, str]:
    """Read the cached cost structure or rebuild it from the real dataset (fail-closed)."""
    cache = Path(cache_path)
    if cache.is_file() and not rebuild:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BridgeError(f"the cost-structure cache is unreadable: {cache}: {error}") from error
        structure = CostStructure.from_payload(payload)
        if structure.digest != payload.get("digest"):
            raise BridgeError(f"the cost-structure cache digest does not match its content: {cache}")
        return structure, "cache"
    if not rebuild and not Path(dataset_path).is_file():
        raise BridgeError(f"no cost-structure cache at {cache} and the MRIO dataset is missing at "
                          f"{dataset_path}; run with --rebuild-cache on a machine that has the data")
    structure = build_cost_structure(dataset_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = structure.payload()
    payload["digest"] = structure.digest
    cache.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                     encoding="utf-8", newline="\n")
    return structure, "rebuilt"


# ------------------------------------------------------------------------------------------ CLI
def run_scenario(module: SoyAgriModule | None = None) -> dict:
    """Run the registered soy scenario once and return the serialisable agri result."""
    module = SoyAgriModule() if module is None else module
    result = module.run()
    payload = result.to_dict()
    payload["agri_digest"] = result.digest()
    return {"name": payload["scenario"]["name"],
            "shocks": payload["scenario"].get("shocks", {}),
            "agri": payload, "agri_digest": payload["agri_digest"],
            "base_soy_price": float(module.config.base_soy_price_usd_t),
            "base_meal_price": float(module.config.base_meal_price_usd_t)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agri/soy -> MRIO bridge: soy scenario -> Leontief prices -> food/animal "
                    "costs (price channel only; research prototype)")
    parser.add_argument("--synthetic", action="store_true",
                        help="use the deterministic synthetic cost structure (offline, tests)")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET),
                        help="path to the canonical MRIO dataset (OECD ICIO 2011 by default)")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE),
                        help="cost-structure cache path (under build/agri_bridge by default)")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="recompute the MRIO cost structure from the dataset (~15-20 s)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--no-write", action="store_true", help="compute only, write nothing")
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    parser.add_argument("--self-test", action="store_true",
                        help="run the synthetic self-test and print the summary")
    return parser


def self_test() -> dict:
    """Fast deterministic check of the bridge on the synthetic structure."""
    structure = synthetic_cost_structure()
    scenario = run_scenario()
    report = run_bridge(structure, scenario=scenario, base_soy_price=scenario["base_soy_price"],
                        base_meal_price=scenario["base_meal_price"])
    again = run_bridge(synthetic_cost_structure(), scenario=run_scenario(),
                       base_soy_price=scenario["base_soy_price"],
                       base_meal_price=scenario["base_meal_price"])
    checks = {
        "deterministic_digest": report["digest"] == again["digest"],
        "leontief_residual_ok": True,  # solve_leontief_prices raises on a large residual
        "producer_prices_positive": all(
            value >= -1e-12
            for corner in report["leontief"]["producer_price_change_pct"].values()
            for value in corner.values()),
        "consumer_range_ordered": (report["consumer_food_prices"]["range_pp"]["low"]
                                   <= report["consumer_food_prices"]["range_pp"]["high"]),
        "africa_not_in_world_totals": report["africa"]["included_in_world_totals"] is False,
        "quantity_not_modelled": report["separation"]["quantity_based"]["modelled"] is False,
        "not_a_forecast": report["provenance"]["not_a_forecast"] is True,
        "units_recorded": report["data"]["mrio"]["units"] == "USD_mn",
    }
    return {"schema": "AMFE-AGRI-BRIDGE-SELFTEST/1.0.0", "ok": all(checks.values()),
            "checks": checks, "digest": report["digest"]}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.self_test:
            payload = self_test()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1))
            return 0 if payload["ok"] else 1
        if args.synthetic:
            structure, origin = synthetic_cost_structure(), "synthetic"
        else:
            structure, origin = load_or_build_structure(args.cache, args.dataset,
                                                        rebuild=args.rebuild_cache)
        scenario = run_scenario()
        report = run_bridge(structure, scenario=scenario,
                            base_soy_price=scenario["base_soy_price"],
                            base_meal_price=scenario["base_meal_price"])
        report["data"]["cost_structure"]["origin"] = origin
        written = {} if args.no_write else write_report(report, args.out)
        summary = {"status": "OK", "schema": report["schema"], "origin": origin,
                   "digest": report["digest"], "written": written,
                   "price_change_pct": report["prices"]["soybeans"]["change_pct"],
                   "food_cpi_pp": report["consumer_food_prices"]["range_pp"],
                   "africa_bill_change_usd_mn": report["africa"]["import_bill_change_usd_mn"]}
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    except (BridgeError, AgriMarketError) as error:
        print(json.dumps({"status": "BLOCKED_BRIDGE", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
