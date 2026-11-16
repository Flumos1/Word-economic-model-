"""Oil module v3: real EIA monthly Brent series, regime-specific calibration and a demand channel.

Why v3 exists (docs/165)
------------------------
The v1/v2 work (docs/155, docs/158) had three structural problems, all named in the documents:
one global elasticity cannot describe both an acute one-month disruption (2019) and a prolonged
regime (1990); the "actual" Brent changes were rounded public approximations; and the engine had no
demand-shock channel, so the 2020 demand collapse could not be represented at all.

v3 fixes exactly these three things:

1. **Real observations.**  The observed monthly Brent average (EIA series RBRTE, Europe Brent Spot
   Price FOB, public domain) is loaded from a committed, hash-pinned CSV and used as the actual
   monthly path, not a rounded peak approximation.  Monthly averages damp intraday spikes, which is
   stated as a limitation instead of hidden.
2. **Regime split.**  Episodes are pre-registered as ``acute`` (supply loss lasting <= 2 months) or
   ``prolonged`` (>= 3 months); the grid is fitted **separately** per regime and the parameters are
   applied to the held-out episodes of that regime.  A third, uncalibrated ``demand`` regime is
   declared for the 2020 demand collapse.
3. **Demand channel.**  :class:`amfe.markets.engine.Shock` now accepts a ``demand_shift`` vector, so
   a demand collapse reduces the base demand before the elasticity and the price falls through the
   same balance identity (no second model, no invented mechanism).

Discipline is the same as v1: the protocol (grids, regime rules, train/OOS ids, selection rule,
success criteria) is built and hashed **before** the OOS scoring; ``fit`` only accepts the train
episodes; OOS actuals never enter the fit (a test perturbs them).  The report is research-only.

Honest caveats (also in docs/165)
---------------------------------
* 1990/2019/2022 were already exposed in earlier waves, so the v3 protocol is pre-registered *within
  v3* but cannot claim blindness to earlier rounds; only the 2020 demand episode is new.
* The engines' scenario mechanics are unchanged: the regime split is a calibration statement, not a
  behavioural model.
* Monthly averages understate the daily peak (2019 intraday 72 vs monthly 62.8): the model is scored
  on the same monthly convention, and the limitation is reported.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .contract import MarketError, RESEARCH_ONLY
from .engine import MarketCalibration, Shock, ramp, simulate

ROOT = Path(__file__).resolve().parents[3]
DATA_PATH = ROOT / "data" / "oil" / "brent_monthly_eia.csv"

PROTOCOL_VERSION = "amfe.markets.oil_v3.protocol.v1"
REPORT_VERSION = "amfe.markets.oil_v3.report.v1"
PROTOCOL_CREATED = "2026-11-16"
SERIES_ID = "RBRTE"
SERIES_NAME = "Crude Oil Prices: Brent - Europe (EIA, monthly average, USD/bbl)"
SERIES_SOURCE = "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RBRTE&f=M"
SERIES_LICENSE = "public domain (U.S. Energy Information Administration)"
ORDER_BAND = (0.5, 2.0)
REGIMES = ("acute", "prolonged", "demand")
ACUTE_MAX_MONTHS = 2


class OilV3Error(MarketError):
    """Fail-closed v3 error (bad series, unknown episode, train/OOS overlap, engine failure)."""


# --------------------------------------------------------------------------------------- series
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_series(path: Path | None = None) -> dict:
    """Load the committed monthly Brent averages (fail-closed on gaps or malformed rows)."""
    path = Path(path) if path is not None else DATA_PATH
    if not path.is_file():
        raise OilV3Error(f"Brent monthly series not found: {path}; see docs/165")
    prices: dict[tuple[int, int], float] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:  # pragma: no cover - empty file
            raise OilV3Error(f"{path}: empty series file") from None
        if header[:1] != ["year"] or len(header) != 13:
            raise OilV3Error(f"{path}: expected header year,01..12; got {header!r}")
        for line_number, row in enumerate(reader, start=2):
            if not row or not row[0].strip():
                continue
            if len(row) != 13:
                raise OilV3Error(f"{path}:{line_number}: expected 13 columns, got {len(row)}")
            try:
                year = int(row[0])
            except ValueError:
                raise OilV3Error(f"{path}:{line_number}: bad year {row[0]!r}") from None
            for month, token in enumerate(row[1:], start=1):
                token = token.strip()
                if not token:
                    continue
                try:
                    value = float(token)
                except ValueError:
                    raise OilV3Error(f"{path}:{line_number}:{month:02d}: bad value {token!r}") \
                        from None
                if not math.isfinite(value) or value <= 0.0:
                    raise OilV3Error(f"{path}:{line_number}:{month:02d}: non-positive price {value}")
                prices[(year, month)] = value
    if not prices:
        raise OilV3Error(f"{path}: no observations")
    years = sorted({year for year, _month in prices})
    if len(prices) < 12 * 30:
        raise OilV3Error(f"{path}: only {len(prices)} observations; series is incomplete")
    return {
        "prices": prices,
        "path": str(path).replace("\\", "/"),
        "sha256": _sha256(path),
        "observations": len(prices),
        "first_month": f"{years[0]}-{min(m for y, m in prices if y == years[0]):02d}",
        "last_month": f"{years[-1]}-{max(m for y, m in prices if y == years[-1]):02d}",
        "series_id": SERIES_ID,
        "series_name": SERIES_NAME,
        "source": SERIES_SOURCE,
        "license": SERIES_LICENSE,
        "unit": "USD/bbl",
        "frequency": "monthly average (damps intraday peaks; see docs/165 limitations)",
    }


# ------------------------------------------------------------------------------------- episodes
def _month(value: str) -> tuple[int, int]:
    try:
        year, month = value.split("-")
        year_i, month_i = int(year), int(month)
    except (AttributeError, ValueError):
        raise OilV3Error(f"bad month {value!r}; expected YYYY-MM") from None
    if not 1 <= month_i <= 12:
        raise OilV3Error(f"bad month {value!r}; expected YYYY-MM")
    return year_i, month_i


def _sequence(start: str, horizon: int) -> tuple[tuple[int, int], ...]:
    year, month = _month(start)
    values = []
    for _ in range(int(horizon)):
        values.append((year, month))
        month += 1
        if month > 12:
            year += 1
            month = 1
    return tuple(values)


@dataclass(frozen=True)
class EpisodeV3:
    """One historical episode encoded for the v3 monthly validation (definition only)."""

    episode_id: str
    name: str
    regime: str
    base_month: str             # month whose observed average is the episode's base price
    start: str                  # first month of the shock
    horizon_months: int
    base_demand: float          # mb/d world demand at the time
    baseline_supply: float      # mb/d world supply at the time
    supply_loss: float = 0.0    # mb/d peak lost supply
    loss_months: int = 0
    #: Optional monthly lost-supply profile in mb/d (partial months are averaged into the month,
    #: e.g. a one-week outage of 5.7 mb/d is 5.7*7/30 = 1.33 mb/d for that month).  Empty = flat.
    loss_profile: tuple[float, ...] = ()
    demand_shift: tuple[float, ...] = ()   # mb/d, negative = demand collapse
    bypass_capacity: float = 0.0
    bypass_ramp_months: int = 1
    supply_response: float = 0.0
    supply_response_ramp_months: int = 1
    spr_stock: float = 0.0
    spr_max_draw: float = 0.0
    commercial_stock: float = 0.0
    commercial_max_draw: float = 0.0
    reference_premium: float = 0.0   # risk premium used by the v1 reference calibration
    risk_premium: float = 0.0        # pre-registered v3 episode premium (declared assumption)
    source_note: str = ""

    def __post_init__(self) -> None:
        if self.regime not in REGIMES:
            raise OilV3Error(f"{self.episode_id}: unknown regime {self.regime!r}")
        if self.horizon_months <= 0:
            raise OilV3Error(f"{self.episode_id}: horizon must be positive")
        if self.horizon_months > 24:
            raise OilV3Error(f"{self.episode_id}: horizon is unreasonably long")
        if self.regime == "demand":
            if len(self.demand_shift) != self.horizon_months:
                raise OilV3Error(f"{self.episode_id}: demand_shift needs {self.horizon_months} "
                                 f"monthly values")
            if any(value > 0.0 for value in self.demand_shift):
                raise OilV3Error(f"{self.episode_id}: a demand collapse must not have positive "
                                 f"shifts")
        else:
            if self.supply_loss <= 0.0 or self.loss_months <= 0:
                raise OilV3Error(f"{self.episode_id}: supply episodes need supply_loss and "
                                 f"loss_months")
            if self.loss_profile and len(self.loss_profile) > self.horizon_months:
                raise OilV3Error(f"{self.episode_id}: loss_profile is longer than the horizon")
            if any(value < 0.0 for value in self.loss_profile):
                raise OilV3Error(f"{self.episode_id}: loss_profile must be non-negative")
            if self.demand_shift:
                raise OilV3Error(f"{self.episode_id}: supply episodes cannot declare a demand shift")
        if self.base_demand + min(self.demand_shift or (0.0,)) <= 0.0:
            raise OilV3Error(f"{self.episode_id}: demand shift would make demand non-positive")
        if self.risk_premium < 0.0:
            raise OilV3Error(f"{self.episode_id}: risk_premium must be non-negative")
        if self.regime == "demand" and self.risk_premium:
            raise OilV3Error(f"{self.episode_id}: demand episodes cannot carry a risk premium")

    def month_prices(self, series: Mapping) -> dict:
        """Observed base price and the actual monthly path (fail-closed on missing months)."""
        observed = series["prices"]
        years = _month(self.base_month)
        if years not in observed:
            raise OilV3Error(f"{self.episode_id}: base month {self.base_month} missing in series")
        actual = []
        for year, month in _sequence(self.start, self.horizon_months):
            key = (year, month)
            if key not in observed:
                raise OilV3Error(f"{self.episode_id}: {year}-{month:02d} missing in series")
            actual.append(observed[key])
        return {"base_price": observed[years], "actual_prices": tuple(actual)}

    def actual_summary(self, series: Mapping) -> dict:
        block = self.month_prices(series)
        pick = min if self.regime == "demand" else max
        extremum = pick(block["actual_prices"])
        return {"base_price": block["base_price"], "actual_peak_price": extremum,
                "actual_peak_change": round(extremum - block["base_price"], 6),
                "observed_months": len(block["actual_prices"]),
                "metric": "trough" if self.regime == "demand" else "peak"}


#: Pre-registered episodes (the actual monthly values come from the EIA series at run time).
EPISODES: tuple[EpisodeV3, ...] = (
    EpisodeV3(
        episode_id="2002_venezuela",
        name="2002-03 Venezuela strike (December 2002)",
        regime="prolonged", base_month="2002-11", start="2002-12", horizon_months=6,
        base_demand=78.0, baseline_supply=78.0, supply_loss=2.5, loss_months=3,
        bypass_capacity=0.2, bypass_ramp_months=2, supply_response=0.6,
        supply_response_ramp_months=3, commercial_stock=200.0, commercial_max_draw=1.0,
        source_note="Venezuelan production collapse ~2.5 mb/d for about three months; monthly "
                    "Brent averages from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="2005_katrina",
        name="2005 Hurricane Katrina (29 August 2005)",
        regime="acute", base_month="2005-07", start="2005-08", horizon_months=3,
        base_demand=84.5, baseline_supply=84.5, supply_loss=1.5, loss_months=2,
        loss_profile=(0.1, 1.0, 0.2),
        bypass_capacity=0.2, bypass_ramp_months=1, supply_response=0.5,
        supply_response_ramp_months=2, spr_stock=30.0, spr_max_draw=0.4,
        commercial_stock=150.0, commercial_max_draw=0.5, risk_premium=5.0,
        source_note="Gulf of Mexico output loss ~1.5 mb/d for weeks; IEA/US SPR release; monthly "
                    "Brent averages from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="2011_libya",
        name="2011 Libya civil war (February 2011)",
        regime="prolonged", base_month="2011-01", start="2011-02", horizon_months=6,
        base_demand=89.0, baseline_supply=89.0, supply_loss=1.5, loss_months=6,
        bypass_capacity=0.2, bypass_ramp_months=2, supply_response=1.2,
        supply_response_ramp_months=4, spr_stock=60.0, spr_max_draw=1.0,
        commercial_stock=200.0, commercial_max_draw=1.0, reference_premium=0.0, risk_premium=15.0,
        source_note="Libyan output ~1.5 mb/d lost; IEA released ~60 mb; monthly Brent averages "
                    "from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="2018_iran",
        name="2018 Iran sanctions and Venezuela decline (March-October 2018)",
        regime="prolonged", base_month="2018-02", start="2018-03", horizon_months=8,
        base_demand=100.0, baseline_supply=100.0, supply_loss=2.0, loss_months=8,
        bypass_capacity=0.2, bypass_ramp_months=2, supply_response=1.5,
        supply_response_ramp_months=4, commercial_stock=200.0, commercial_max_draw=1.0,
        source_note="Iran export loss plus Venezuela decline ~2 mb/d; monthly Brent averages "
                    "from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="1990_gulf",
        name="1990 Gulf: Iraq invades Kuwait (August 1990)",
        regime="prolonged", base_month="1990-07", start="1990-08", horizon_months=6,
        base_demand=65.0, baseline_supply=65.0, supply_loss=4.3, loss_months=3,
        bypass_capacity=0.5, bypass_ramp_months=2, supply_response=0.5,
        supply_response_ramp_months=3, spr_stock=50.0, spr_max_draw=0.5,
        commercial_stock=300.0, commercial_max_draw=1.5, reference_premium=0.0, risk_premium=10.0,
        source_note="Iraq+Kuwait supply ~4.3 mb/d lost; monthly Brent averages from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="2019_abqaiq",
        name="2019 Abqaiq/Khurais attack (14 September 2019)",
        regime="acute", base_month="2019-08", start="2019-09", horizon_months=2,
        base_demand=100.0, baseline_supply=100.0, supply_loss=5.7, loss_months=1,
        loss_profile=(1.33, 0.0),
        bypass_capacity=0.0, bypass_ramp_months=1, supply_response=0.3,
        supply_response_ramp_months=1, commercial_stock=0.0, commercial_max_draw=0.0,
        reference_premium=0.0,
        source_note="~5.7 mb/d Saudi output lost for days; monthly Brent averages from EIA RBRTE "
                    "(intraday peak ~72 is damped by the monthly convention)",
    ),
    EpisodeV3(
        episode_id="2022_russia",
        name="2022 Russia/Ukraine: feared export loss (February-March 2022)",
        regime="prolonged", base_month="2022-01", start="2022-02", horizon_months=6,
        base_demand=100.0, baseline_supply=100.0, supply_loss=5.0, loss_months=3,
        loss_profile=(5.0, 5.0, 5.0, 0.0, 0.0, 0.0),
        bypass_capacity=0.3, bypass_ramp_months=2, supply_response=0.5,
        supply_response_ramp_months=3, spr_stock=150.0, spr_max_draw=1.5,
        commercial_stock=200.0, commercial_max_draw=1.0, reference_premium=12.0,
        risk_premium=10.0,
        source_note="Feared export loss ~5 mb/d (realised smaller) for three months plus a "
                    "declared war-risk premium; monthly Brent averages from EIA RBRTE",
    ),
    EpisodeV3(
        episode_id="2020_covid",
        name="2020 COVID-19 demand collapse (February-July 2020)",
        regime="demand", base_month="2020-01", start="2020-02", horizon_months=6,
        base_demand=100.0, baseline_supply=100.0,
        demand_shift=(-0.6, -3.0, -13.0, -12.5, -8.5, -7.0),
        commercial_stock=300.0, commercial_max_draw=1.5,
        source_note="Demand-shift schedule is a declared assumption (IEA/EIA public demand "
                    "estimates, rounded); actual monthly Brent averages from EIA RBRTE",
    ),
)
EPISODES_BY_ID = {item.episode_id: item for item in EPISODES}


def episode(episode_id: str) -> EpisodeV3:
    if episode_id not in EPISODES_BY_ID:
        raise OilV3Error(f"unknown episode {episode_id!r}; available {sorted(EPISODES_BY_ID)}")
    return EPISODES_BY_ID[episode_id]


# ---------------------------------------------------------------------------- protocol and grid
PROTOCOL_VERSION_GRID = {
    "acute": {
        "elasticity_short_magnitude": (0.30, 0.40, 0.50, 0.60, 0.70, 0.80),
        "elasticity_long_multiplier": (1.1, 1.2),
    },
    "prolonged": {
        "elasticity_short_magnitude": (0.02, 0.03, 0.05, 0.07, 0.09, 0.12, 0.15),
        "elasticity_long_multiplier": (1.0, 1.2, 1.4, 1.6, 2.0, 2.5),
    },
}
#: The demand regime is declared but not fitted (no train demand episode); these are the v1 defaults.
DEMAND_PARAMS = {"elasticity_short": -0.25, "elasticity_long": -0.55}

TRAIN_IDS = {"acute": ("2005_katrina",), "prolonged": ("2002_venezuela", "2011_libya",
                                                       "2018_iran")}
OOS_IDS = ("1990_gulf", "2019_abqaiq", "2022_russia", "2020_covid")
SELECTION_RULE = ("per regime: minimize the mean monthly path RMSE on that regime's train "
                  "episodes; ties broken by (|elasticity_short|, |elasticity_long|)")
SUCCESS_CRITERIA = {
    "OOS_PASS": ("direction correct on 4/4 OOS episodes AND order hits (0.5-2x) >= 3/4 AND mean "
                 "path RMSE below the v1 reference calibration AND mean path MAE below the "
                 "no-shock baseline"),
    "OOS_PARTIAL": ("direction correct on 4/4 AND (order hits >= 3/4 OR mean path RMSE below the "
                    "v1 reference OR mean peak-change MAE below the v1 reference)"),
    "OOS_FAIL": "otherwise",
}


def _grid(regime: str) -> tuple[dict, ...]:
    spec = PROTOCOL_VERSION_GRID[regime]
    points = []
    for short in spec["elasticity_short_magnitude"]:
        for multiplier in spec["elasticity_long_multiplier"]:
            points.append({"elasticity_short": -float(short),
                           "elasticity_long": -float(short * multiplier)})
    return tuple(points)


def build_protocol() -> dict:
    """The pre-registered v3 protocol (deterministic; hash it before any OOS scoring)."""
    grids = {regime: _grid(regime) for regime in ("acute", "prolonged")}
    train = {regime: list(ids) for regime, ids in TRAIN_IDS.items()}
    train_all = [item for ids in train.values() for item in ids]
    return {
        "schema": PROTOCOL_VERSION,
        "protocol_id": "oil-v3-2026-11-16",
        "created": PROTOCOL_CREATED,
        "status": RESEARCH_ONLY,
        "research_only": True,
        "objective": "regime-specific calibration of the oil module on real EIA monthly Brent "
                     "averages, with a declared demand channel for 2020",
        "data": {"series_id": SERIES_ID, "series_name": SERIES_NAME,
                 "source": SERIES_SOURCE, "license": SERIES_LICENSE,
                 "frequency": "monthly average", "unit": "USD/bbl",
                 "file": "data/oil/brent_monthly_eia.csv"},
        "regime_rule": {
            "acute": f"supply episode with lost-supply duration <= {ACUTE_MAX_MONTHS} months",
            "prolonged": f"supply episode with lost-supply duration > {ACUTE_MAX_MONTHS} months",
            "demand": "episode with a declared demand_shift schedule (not fitted)",
        },
        "train_episodes": train,
        "oos_episodes": list(OOS_IDS),
        "train_oos_disjoint": set(train_all).isdisjoint(set(OOS_IDS)),
        "grid": {regime: {
            "elasticity_short_magnitude": list(PROTOCOL_VERSION_GRID[regime]
                                               ["elasticity_short_magnitude"]),
            "elasticity_long_multiplier": list(PROTOCOL_VERSION_GRID[regime]
                                               ["elasticity_long_multiplier"]),
            "size": len(grids[regime]),
        } for regime in grids},
        "declared_episode_premiums": {item.episode_id: item.risk_premium for item in EPISODES},
        "demand_params": dict(DEMAND_PARAMS),
        "selection_rule": SELECTION_RULE,
        "success_criteria": dict(SUCCESS_CRITERIA),
        "metrics": ["monthly_path_MAE", "monthly_path_RMSE", "peak_change_error",
                    "peak_change_ratio", "direction_ok", "order_ok",
                    "skill_vs_no_shock", "where_worse"],
        "order_band": list(ORDER_BAND),
        "contamination_note": ("1990/2019/2022 were exposed in earlier waves (docs/155, docs/158); "
                               "the v3 protocol is pre-registered within v3 but is not blind to "
                               "those episodes. 2020 is a new episode."),
        "reference_calibration": {"elasticity_short": -0.25, "elasticity_long": -0.55,
                                  "risk_premium": "episode reference_premium (v1 convention)"},
        "note": "research calibration, not an admission and not a forecast",
    }


def protocol_sha256(protocol: Mapping) -> str:
    text = json.dumps(dict(protocol), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------- scoring
def _params(regime: str, point: Mapping) -> dict:
    return {"regime": regime, "elasticity_short": float(point["elasticity_short"]),
            "elasticity_long": float(point["elasticity_long"])}


def build_case(target: EpisodeV3, params: Mapping, series: Mapping) -> tuple[MarketCalibration,
                                                                            Shock, dict]:
    """Calibration, monthly shock and the observed path of one episode."""
    observed = target.month_prices(series)
    base_price = observed["base_price"]
    horizon = target.horizon_months
    if params["elasticity_short"] >= 0.0 or params["elasticity_long"] >= 0.0:
        raise OilV3Error(f"{target.episode_id}: elasticities must be negative")
    if abs(params["elasticity_short"]) > abs(params["elasticity_long"]):
        raise OilV3Error(f"{target.episode_id}: short elasticity magnitude must not exceed long")
    lost = tuple(target.supply_loss if month < target.loss_months else 0.0
                 for month in range(horizon))
    if target.loss_profile:
        lost = tuple(float(target.loss_profile[month]) if month < len(target.loss_profile)
                     else lost[month] for month in range(horizon))
    added = tuple(round(a + b, 12) for a, b in zip(
        ramp(target.bypass_capacity, horizon, target.bypass_ramp_months),
        ramp(target.supply_response, horizon, target.supply_response_ramp_months)))
    release = _release_plan(target.spr_max_draw, target.spr_stock, horizon)
    shift = tuple(target.demand_shift) or tuple(0.0 for _ in range(horizon))
    calibration = MarketCalibration(
        commodity=target.name, base_price=base_price,
        base_demand=target.base_demand, baseline_supply=target.baseline_supply,
        elasticity_short=float(params["elasticity_short"]),
        elasticity_long=float(params["elasticity_long"]),
        elasticity_ramp_months=min(6, horizon),
        risk_premium=float(target.risk_premium),
        commercial_stock=target.commercial_stock,
        commercial_max_draw=target.commercial_max_draw,
        strategic_stock=target.spr_stock, strategic_max_draw=target.spr_max_draw)
    shock = Shock(lost_supply=lost, added_supply=added, strategic_release=release,
                  demand_shift=shift, label=target.episode_id)
    return calibration, shock, observed


def _release_plan(rate: float, stock: float, months: int) -> tuple[float, ...]:
    remaining = float(stock)
    plan = []
    for _month in range(int(months)):
        draw = min(float(rate), max(0.0, remaining) / 30.4375)
        plan.append(round(draw, 12))
        remaining -= draw * 30.4375
    return tuple(plan)


def _score_path(target: EpisodeV3, model_prices: Sequence[float], base_price: float,
                actual_prices: Sequence[float]) -> dict:
    if len(model_prices) != len(actual_prices):
        raise OilV3Error(f"{target.episode_id}: model/actual path length mismatch")
    model = [float(value) for value in model_prices]
    actual = [float(value) for value in actual_prices]
    errors = [m - a for m, a in zip(model, actual)]
    path_mae = math.fsum(abs(value) for value in errors) / len(errors)
    path_rmse = math.sqrt(math.fsum(value * value for value in errors) / len(errors))
    naive = [abs(a - base_price) for a in actual]
    naive_mae = math.fsum(naive) / len(naive)
    metric = "trough" if target.regime == "demand" else "peak"
    pick = min if metric == "trough" else max
    model_peak = pick(model)
    actual_peak = pick(actual)
    model_change = model_peak - base_price
    actual_change = actual_peak - base_price
    ratio = model_change / actual_change if abs(actual_change) > 1e-9 else None
    direction_ok = bool((model_change > 0.0) == (actual_change > 0.0))
    order_ok = bool(ratio is not None and ORDER_BAND[0] <= ratio <= ORDER_BAND[1])
    skill = 1.0 - path_mae / naive_mae if naive_mae else None
    return {
        "episode_id": target.episode_id, "regime": target.regime, "metric": metric,
        "base_price": round(base_price, 6),
        "model_peak_price": round(model_peak, 6),
        "actual_peak_price": round(actual_peak, 6),
        "model_peak_change": round(model_change, 6),
        "actual_peak_change": round(actual_change, 6),
        "peak_change_error": round(model_change - actual_change, 6),
        "peak_change_ratio": round(ratio, 6) if ratio is not None else None,
        "path_mae": round(path_mae, 6), "path_rmse": round(path_rmse, 6),
        "naive_mae": round(naive_mae, 6),
        "skill_vs_no_shock": round(skill, 6) if skill is not None else None,
        "direction_ok": direction_ok, "order_ok": order_ok,
        "model_prices": [round(value, 6) for value in model],
        "actual_prices": [round(value, 6) for value in actual],
    }


def score_episode(target: EpisodeV3, params: Mapping, series: Mapping) -> dict:
    """Run one episode and score the monthly path against the observed averages."""
    base_price, actual = _episode_actuals(target, series)
    model, _block = _model_prices(target, params, series)
    return _score_path(target, model, base_price, actual)


def _episode_actuals(target: EpisodeV3, series: Mapping) -> tuple[float, tuple]:
    block = target.month_prices(series)
    return block["base_price"], block["actual_prices"]


def _model_prices(target: EpisodeV3, params: Mapping, series: Mapping) -> tuple[tuple, dict]:
    calibration, shock, observed = build_case(target, params, series)
    path = simulate(calibration, shock)
    if not path.ok:
        raise OilV3Error(f"{target.episode_id}: engine checks failed {path.checks}")
    return path.price, observed


def _aggregate(rows: Sequence[dict]) -> dict:
    if not rows:
        raise OilV3Error("cannot aggregate an empty score list")
    return {
        "episodes": list(rows),
        "mean_path_mae": round(math.fsum(row["path_mae"] for row in rows) / len(rows), 6),
        "mean_path_rmse": round(math.fsum(row["path_rmse"] for row in rows) / len(rows), 6),
        "mean_naive_mae": round(math.fsum(row["naive_mae"] for row in rows) / len(rows), 6),
        "mean_skill": round(math.fsum(row["skill_vs_no_shock"] for row in rows
                                      if row["skill_vs_no_shock"] is not None) / len(rows), 6),
        "direction_hits": sum(1 for row in rows if row["direction_ok"]),
        "order_hits": sum(1 for row in rows if row["order_ok"]),
        "peak_change_mae": round(math.fsum(abs(row["peak_change_error"]) for row in rows)
                                 / len(rows), 6),
        "n": len(rows),
    }


@dataclass(frozen=True)
class FitResult:
    params: Mapping
    train: Mapping
    candidates: int
    selection_rule: str = SELECTION_RULE

    def as_dict(self) -> dict:
        return {"params": dict(self.params), "train": dict(self.train),
                "candidates_evaluated": self.candidates, "selection_rule": self.selection_rule}


def fit_regime(regime: str, series: Mapping,
               episodes: Sequence[EpisodeV3] | None = None) -> FitResult:
    """Grid-search one regime on its train episodes only (fail-closed on OOS overlap)."""
    if regime not in PROTOCOL_VERSION_GRID:
        raise OilV3Error(f"regime {regime!r} is not fitted")
    episodes = tuple(episodes if episodes is not None
                     else [episode(episode_id) for episode_id in TRAIN_IDS[regime]])
    if not episodes:
        raise OilV3Error("fit needs at least one train episode")
    ids = {item.episode_id for item in episodes}
    if ids & set(OOS_IDS):
        raise OilV3Error(f"train episodes overlap the held-out set: {sorted(ids & set(OOS_IDS))}")
    if any(item.regime != regime for item in episodes):
        raise OilV3Error(f"all train episodes must belong to regime {regime!r}")
    best = None
    evaluated = 0
    for point in _grid(regime):
        params = _params(regime, point)
        rows = [score_episode(target, params, series) for target in episodes]
        evaluated += 1
        mean_rmse = math.fsum(row["path_rmse"] for row in rows) / len(rows)
        key = (round(mean_rmse, 9), abs(params["elasticity_short"]),
               abs(params["elasticity_long"]))
        train = _aggregate(rows)
        if best is None or key < best[0]:
            best = (key, params, train)
    return FitResult(params=best[1], train=best[2], candidates=evaluated)


def reference_params(target: EpisodeV3) -> dict:
    """The v1 fixed calibration applied to the same monthly convention."""
    return {"regime": target.regime, "elasticity_short": -0.25, "elasticity_long": -0.55,
            "risk_premium": float(target.reference_premium)}


@dataclass(frozen=True)
class OilV3Report:
    protocol: dict
    protocol_sha256: str
    fits: Mapping
    oos: Mapping
    reference: Mapping
    verdict: str
    where_worse: tuple[str, ...]
    data: Mapping = field(default_factory=dict)
    status: str = RESEARCH_ONLY
    schema: str = REPORT_VERSION

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise OilV3Error("oil v3 reports are research-only")

    def as_dict(self) -> dict:
        comparison = {
            "mean_path_mae_calibrated": self.oos["mean_path_mae"],
            "mean_path_mae_reference": self.reference["mean_path_mae"],
            "mean_path_rmse_calibrated": self.oos["mean_path_rmse"],
            "mean_path_rmse_reference": self.reference["mean_path_rmse"],
            "peak_change_mae_calibrated": self.oos["peak_change_mae"],
            "peak_change_mae_reference": self.reference["peak_change_mae"],
            "direction_calibrated": self.oos["direction_hits"],
            "direction_reference": self.reference["direction_hits"],
            "order_calibrated": self.oos["order_hits"],
            "order_reference": self.reference["order_hits"],
        }
        return {
            "schema": self.schema, "status": self.status, "research_only": True,
            "note": "monthly-path regime calibration on real EIA Brent averages; not an admission "
                    "and not a forecast",
            "protocol": dict(self.protocol),
            "protocol_sha256": self.protocol_sha256,
            "data": dict(self.data),
            "fits": {regime: dict(fit) for regime, fit in self.fits.items()},
            "oos": dict(self.oos),
            "reference_v1": dict(self.reference),
            "comparison": comparison,
            "verdict": self.verdict,
            "where_worse": list(self.where_worse),
            "digest": self.digest(),
        }

    def digest(self) -> str:
        payload = {"protocol_sha256": self.protocol_sha256,
                   "fits": {regime: dict(fit) for regime, fit in self.fits.items()},
                   "oos": dict(self.oos), "reference": dict(self.reference),
                   "verdict": self.verdict, "where_worse": list(self.where_worse)}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_criteria(oos: Mapping, reference: Mapping, criteria: Mapping | None = None) -> str:
    """Apply the pre-registered success criteria (no post-hoc loosening)."""
    criteria = criteria or SUCCESS_CRITERIA
    n = oos["n"]
    if oos["direction_hits"] == n and oos["order_hits"] >= 3 and \
            oos["mean_path_rmse"] < reference["mean_path_rmse"] and \
            oos["mean_path_mae"] < oos["mean_naive_mae"]:
        return "OOS_PASS"
    if oos["direction_hits"] == n and (oos["order_hits"] >= 3 or
                                       oos["mean_path_rmse"] < reference["mean_path_rmse"] or
                                       oos["peak_change_mae"] < reference["peak_change_mae"]):
        return "OOS_PARTIAL"
    return "OOS_FAIL"


def calibrate(series: Mapping | None = None) -> OilV3Report:
    """Protocol -> per-regime fit on train -> OOS scoring with the frozen parameters."""
    series = series if series is not None else load_series()
    protocol = build_protocol()
    digest = protocol_sha256(protocol)
    fits = {}
    for regime in ("acute", "prolonged"):
        fitted = fit_regime(regime, series)
        fits[regime] = fitted.as_dict()
    oos_rows = []
    ref_rows = []
    for episode_id in OOS_IDS:
        target = episode(episode_id)
        params = _params("demand", DEMAND_PARAMS) if target.regime == "demand" \
            else fits[target.regime]["params"]
        oos_rows.append(score_episode(target, params, series))
        ref_rows.append(score_episode(target, reference_params(target), series))
    oos = _aggregate(oos_rows)
    reference = _aggregate(ref_rows)
    where_worse = tuple(row["episode_id"] for row, ref in zip(oos_rows, ref_rows)
                        if row["path_rmse"] >= ref["path_rmse"])
    fitted_params = {
        "acute": dict(fits["acute"]["params"]),
        "prolonged": dict(fits["prolonged"]["params"]),
        "demand": dict(DEMAND_PARAMS),
    }
    verdict_value = apply_criteria(oos, reference)
    return OilV3Report(
        protocol=protocol, protocol_sha256=digest,
        fits={"params": fitted_params, "acute": fits["acute"], "prolonged": fits["prolonged"]},
        oos=oos, reference=reference, verdict=verdict_value, where_worse=where_worse,
        data={key: series[key] for key in ("series_id", "path", "sha256", "observations",
                                           "first_month", "last_month", "source", "license",
                                           "unit", "frequency")})


def report_markdown(report: OilV3Report) -> str:
    """Human-readable v3 report (aggregates only)."""
    params = report.fits["params"]
    comparison = report.as_dict()["comparison"]
    lines = [
        "# Нефть v3: реальные месячные ряды, режимы и demand-канал (G4)",
        "",
        f"**Статус:** {report.status} — исследование, не допуск и не прогноз.",
        f"**Protocol SHA-256:** `{report.protocol_sha256}`",
        f"**Verdict:** `{report.verdict}`",
        "",
        "## Данные",
        f"- Источник: {report.data['source']}; лицензия: {report.data['license']}",
        f"- Ряд: {report.data['series_id']} ({report.data['first_month']} … "
        f"{report.data['last_month']}), наблюдений {report.data['observations']}, "
        f"частота: {report.data['frequency']}",
        f"- Файл: `{report.data['path']}` (SHA-256 `{report.data['sha256'][:16]}…`)",
        "",
        "## Параметры по режимам (обучено на train)",
    ]
    for regime in ("acute", "prolonged"):
        fit = report.fits[regime]
        row = fit["params"]
        lines.append(f"- **{regime}**: εshort={row['elasticity_short']}, "
                     f"εlong={row['elasticity_long']}; "
                     f"train RMSE {fit['train']['mean_path_rmse']:.2f}, "
                     f"направление {fit['train']['direction_hits']}/{fit['train']['n']}, "
                     f"порядок {fit['train']['order_hits']}/{fit['train']['n']} "
                     f"({fit['candidates_evaluated']} точек)")
    lines += [
        f"- **demand**: εshort={params['demand']['elasticity_short']}, "
        f"εlong={params['demand']['elasticity_long']} (не обучено, v1 defaults)",
        "- Объявленные премии (допущения): " + ", ".join(
            f"{episode_id}={premium:g}"
            for episode_id, premium in report.protocol["declared_episode_premiums"].items()),
        "",
        "## OOS: месячный путь против наблюдений",
        "",
        "| Эпизод | Режим | Факт Δ пика | Модель Δ | Ошибка | Ratio | MAE пути | RMSE пути | "
        "Skill |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report.oos["episodes"]:
        ratio = "—" if row["peak_change_ratio"] is None else f"{row['peak_change_ratio']:.2f}"
        lines.append(
            f"| {row['episode_id']} | {row['regime']} | {row['actual_peak_change']:+.2f} | "
            f"{row['model_peak_change']:+.2f} | {row['peak_change_error']:+.2f} | {ratio} | "
            f"{row['path_mae']:.2f} | {row['path_rmse']:.2f} | "
            f"{row['skill_vs_no_shock']:+.2f} |")
    lines += [
        "",
        f"- Средний MAE пути: **{report.oos['mean_path_mae']:.2f}** против "
        f"**{report.reference['mean_path_mae']:.2f}** у калибровки v1 и "
        f"{report.oos['mean_naive_mae']:.2f} у наивного «цена не изменится».",
        f"- Средний RMSE пути: **{report.oos['mean_path_rmse']:.2f}** против "
        f"{report.reference['mean_path_rmse']:.2f} (v1).",
        f"- Peak-change MAE: **{report.oos['peak_change_mae']:.2f}** против "
        f"{report.reference['peak_change_mae']:.2f} (v1) USD/bbl.",
        f"- Направление: {report.oos['direction_hits']}/{report.oos['n']} против "
        f"{report.reference['direction_hits']}/{report.reference['n']} (v1); порядок: "
        f"{report.oos['order_hits']}/{report.oos['n']} против "
        f"{report.reference['order_hits']}/{report.reference['n']}.",
        f"- Хуже v1 по RMSE пути: {', '.join(report.where_worse) or 'нигде'}.",
        "",
        "**Ограничения:** 4 train и 4 OOS эпизода; 1990/2019/2022 уже были в предыдущих волнах "
        "(протокол v3 предрегистрирован внутри v3, но не слеп к прошлым раундам); месячные "
        "средние сглаживают внутридневной пик (2019: интрадей ~72 против месячного 62.8); "
        "demand-шок — объявленное допущение (IEA/EIA оценки спроса), а не оценка; режимная "
        "эластичность — калибровочное утверждение, не поведенческая модель.",
        f"**Дайджест отчёта:** `{report.digest()}`",
        "",
    ]
    return "\n".join(lines)
