"""Historical backtest of the oil market module: 1990 Gulf, 2019 Abqaiq, 2022 Russia (docs/155).

What this is (and is not)
-------------------------
It is a **research backtest**: three historical supply-shock episodes are encoded as explicit
inputs (supply loss, duration, bypass capacity, strategic stocks, elasticities) plus the observed
Brent response (public approximations, all ``source_class="assumption"``).  The oil engine is run
deterministically, its peak Brent change is compared to the historical change with direction/order/
MAE metrics and with the naive "no shock" baseline, and the sensitivity variants show how wide the
answer is.

It is **not** a forecast, not an admission and not a validation of the module: three episodes with
four or five free parameters each are enough to expose errors, not enough to establish skill.
The verdict is computed mechanically and reported even when it is unflattering.

Data policy
-----------
No third-party series is read and no timestamp/randomness enters the result: every input is a
declared assumption with its public source hint, so the backtest is reproducible offline and can be
re-run after the assumptions are replaced with sourced observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .contract import Assumption, MarketError, RESEARCH_ONLY, ScenarioError
from .engine import MarketCalibration, Shock, simulate

DAYS_PER_MONTH = 30.4375
RESULT_SCHEMA = "amfe.markets.backtest.v1"
#: Ratio band for the "right order of magnitude" check.
ORDER_LOW, ORDER_HIGH = 0.5, 2.0


class BacktestError(MarketError):
    """Fail-closed backtest error (unknown episode, invalid inputs, empty selection)."""


@dataclass(frozen=True)
class Episode:
    """One historical supply-shock episode with explicit inputs and the observed Brent change.

    ``actual_delta_usd`` is a public approximation of the peak Brent change, not a loaded series:
    it is a scenario input for the comparison and is declared as an assumption.
    """

    episode_id: str
    name: str
    date: str
    base_price: float
    supply_loss: float          # mb/d
    loss_months: int
    demand: float               # mb/d
    supply: float               # mb/d
    bypass_capacity: float      # mb/d
    bypass_ramp_months: int
    supply_response: float      # mb/d
    supply_response_ramp_months: int
    spr_stock: float            # mb
    spr_max_draw: float         # mb/d
    commercial_stock: float     # mb
    commercial_max_draw: float  # mb/d
    elasticity_short: float
    elasticity_long: float
    risk_premium: float         # USD/bbl
    horizon_months: int
    actual_peak_price: float    # USD/bbl, public approximation
    actual_delta_usd: float     # USD/bbl, public approximation
    source_note: str
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.episode_id or not self.name:
            raise BacktestError("episode_id and name must be non-empty")
        for name in ("base_price", "demand", "supply", "horizon_months", "actual_peak_price"):
            if float(getattr(self, name)) <= 0.0:
                raise BacktestError(f"{self.episode_id}: {name} must be positive")
        for name in ("supply_loss", "bypass_capacity", "supply_response", "spr_stock",
                     "spr_max_draw", "commercial_stock", "commercial_max_draw"):
            if float(getattr(self, name)) < 0.0:
                raise BacktestError(f"{self.episode_id}: {name} must be non-negative")
        if int(self.loss_months) <= 0 or int(self.loss_months) > int(self.horizon_months):
            raise BacktestError(f"{self.episode_id}: loss_months must be in 1..horizon_months")
        if int(self.actual_delta_usd) == 0 and float(self.actual_delta_usd) == 0.0:
            raise BacktestError(f"{self.episode_id}: actual_delta_usd must be non-zero")
        if self.elasticity_short >= 0.0 or self.elasticity_long >= 0.0:
            raise BacktestError(f"{self.episode_id}: elasticities must be negative")
        object.__setattr__(self, "loss_months", int(self.loss_months))
        object.__setattr__(self, "horizon_months", int(self.horizon_months))
        object.__setattr__(self, "sources", tuple(str(item) for item in self.sources))

    def inputs(self) -> tuple:
        """The episode inputs as declared assumptions (no observed series are claimed)."""
        specs = (
            ("base_price", self.base_price, "USD/bbl",
             "Brent level just before the episode; replace with a dated front-month quote"),
            ("supply_loss", self.supply_loss, "mb/d",
             "peak lost supply; replace with EIA/IEA episode estimates"),
            ("loss_months", self.loss_months, "months", "how long the loss persisted"),
            ("world_demand", self.demand, "mb/d", "world oil demand in that year (IEA/EIA)"),
            ("world_supply", self.supply, "mb/d", "world oil supply in that year (IEA/EIA)"),
            ("bypass_capacity", self.bypass_capacity, "mb/d",
             "alternative routes/spare capacity usable in the episode"),
            ("supply_response", self.supply_response, "mb/d",
             "non-Gulf supply response in the episode"),
            ("spr_stock", self.spr_stock, "mb", "strategic stock actually released"),
            ("spr_max_draw", self.spr_max_draw, "mb/d", "release rate in the episode"),
            ("elasticity_short", self.elasticity_short, "elasticity",
             "effective short-run demand elasticity used by the module"),
            ("elasticity_long", self.elasticity_long, "elasticity",
             "effective long-run demand elasticity used by the module"),
            ("risk_premium", self.risk_premium, "USD/bbl",
             "declared risk premium added by the module"),
            ("actual_delta_usd", self.actual_delta_usd, "USD/bbl",
             "observed peak Brent change (public approximation)"),
        )
        return tuple(
            Assumption(name=f"{self.episode_id}.{name}", value=value, unit=unit,
                       source_class="assumption", note=note,
                       source=self.sources[0] if self.sources else self.source_note)
            for name, value, unit, note in specs)


#: The three historical episodes (all numbers below are declared public approximations).
EPISODES: tuple[Episode, ...] = (
    Episode(
        episode_id="1990_gulf",
        name="1990 Gulf: Iraq invades Kuwait (August 1990)",
        date="1990-08",
        base_price=17.0, supply_loss=4.3, loss_months=3,
        demand=65.0, supply=65.0,
        bypass_capacity=0.5, bypass_ramp_months=2,
        supply_response=0.5, supply_response_ramp_months=3,
        spr_stock=50.0, spr_max_draw=0.5,
        commercial_stock=300.0, commercial_max_draw=1.5,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=0.0, horizon_months=6,
        actual_peak_price=41.0, actual_delta_usd=24.0,
        source_note="Brent ~17 USD/bbl in July 1990, peak ~41 in October 1990; Iraq+Kuwait "
                    "supply ~4.3 mb/d lost; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
    Episode(
        episode_id="2019_abqaiq",
        name="2019 Abqaiq/Khurais attack (14 September 2019)",
        date="2019-09",
        base_price=60.0, supply_loss=5.7, loss_months=1,
        demand=100.0, supply=100.0,
        bypass_capacity=0.0, bypass_ramp_months=1,
        supply_response=0.3, supply_response_ramp_months=1,
        spr_stock=0.0, spr_max_draw=0.0,
        commercial_stock=0.0, commercial_max_draw=0.0,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=0.0, horizon_months=3,
        actual_peak_price=69.0, actual_delta_usd=9.0,
        source_note="Brent ~60 USD/bbl before the attack, close ~69 (intraday ~72) on the next "
                    "trading day; ~5.7 mb/d Saudi output lost for days; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
    Episode(
        episode_id="2022_russia",
        name="2022 Russia/Ukraine: feared export loss (February-March 2022)",
        date="2022-02",
        base_price=97.0, supply_loss=3.0, loss_months=3,
        demand=100.0, supply=100.0,
        bypass_capacity=0.3, bypass_ramp_months=2,
        supply_response=0.5, supply_response_ramp_months=3,
        spr_stock=150.0, spr_max_draw=1.5,
        commercial_stock=200.0, commercial_max_draw=1.0,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=12.0, horizon_months=6,
        actual_peak_price=127.0, actual_delta_usd=30.0,
        source_note="Brent ~97 USD/bbl in late February 2022, peak ~127 in March 2022; the feared "
                    "loss was 3+ mb/d, the realised loss smaller; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
)
EPISODES_BY_ID = {episode.episode_id: episode for episode in EPISODES}


def episode(episode_id: str) -> Episode:
    if episode_id not in EPISODES_BY_ID:
        raise ScenarioError(f"unknown episode {episode_id!r}; available {sorted(EPISODES_BY_ID)}")
    return EPISODES_BY_ID[episode_id]


def _release_plan(rate: float, stock: float, months: int) -> tuple[float, ...]:
    """Monthly strategic release rates capped by the declared stock (mb vs mb/d)."""
    remaining = float(stock)
    plan = []
    for _month in range(int(months)):
        draw = min(float(rate), max(0.0, remaining) / DAYS_PER_MONTH)
        plan.append(round(draw, 12))
        remaining -= draw * DAYS_PER_MONTH
    return tuple(plan)


def build_case(target: Episode, **overrides) -> tuple[MarketCalibration, Shock]:
    """Calibration and monthly shock of one episode (``overrides`` change calibration inputs)."""
    params = {
        "base_price": target.base_price, "demand": target.demand, "supply": target.supply,
        "supply_loss": target.supply_loss, "loss_months": target.loss_months,
        "bypass_capacity": target.bypass_capacity,
        "bypass_ramp_months": target.bypass_ramp_months,
        "supply_response": target.supply_response,
        "supply_response_ramp_months": target.supply_response_ramp_months,
        "spr_stock": target.spr_stock, "spr_max_draw": target.spr_max_draw,
        "commercial_stock": target.commercial_stock,
        "commercial_max_draw": target.commercial_max_draw,
        "elasticity_short": target.elasticity_short, "elasticity_long": target.elasticity_long,
        "risk_premium": target.risk_premium,
    }
    unknown = [key for key in overrides if key not in params]
    if unknown:
        raise BacktestError(f"unknown overrides {unknown}; allowed {sorted(params)}")
    params.update(overrides)
    horizon = target.horizon_months
    loss_months = min(int(params["loss_months"]), horizon)
    lost = tuple(float(params["supply_loss"]) if month < loss_months else 0.0
                 for month in range(horizon))
    added = []
    for month in range(horizon):
        bypass = float(params["bypass_capacity"]) * min(
            1.0, float(month + 1) / float(max(1, target.bypass_ramp_months)))
        response = float(params["supply_response"]) * min(
            1.0, float(month + 1) / float(max(1, target.supply_response_ramp_months)))
        added.append(round(bypass + response, 12))
    calibration = MarketCalibration(
        commodity=target.name, base_price=float(params["base_price"]),
        base_demand=float(params["demand"]), baseline_supply=float(params["supply"]),
        elasticity_short=float(params["elasticity_short"]),
        elasticity_long=float(params["elasticity_long"]),
        elasticity_ramp_months=min(6, horizon),
        risk_premium=float(params["risk_premium"]),
        commercial_stock=float(params["commercial_stock"]),
        commercial_max_draw=float(params["commercial_max_draw"]),
        strategic_stock=float(params["spr_stock"]),
        strategic_max_draw=float(params["spr_max_draw"]),
    )
    shock = Shock(lost_supply=lost, added_supply=tuple(added),
                  strategic_release=_release_plan(float(params["spr_max_draw"]),
                                                  float(params["spr_stock"]), horizon),
                  label=target.episode_id)
    return calibration, shock


@dataclass(frozen=True)
class EpisodeResult:
    """Model vs history for one episode (all metrics in USD/bbl)."""

    episode_id: str
    name: str
    date: str
    base_price: float
    actual_delta_usd: float
    actual_peak_price: float
    model_delta_usd: float
    model_peak_price: float
    model_peak_month: int
    error_usd: float
    abs_error_usd: float
    rel_error: float | None
    ratio: float | None
    direction_ok: bool
    order_ok: bool
    baseline_abs_error_usd: float
    beats_baseline: bool
    sensitivity: tuple[dict, ...] = ()
    sensitivity_range: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "episode_id": self.episode_id, "name": self.name, "date": self.date,
            "base_price": self.base_price, "actual_peak_price": self.actual_peak_price,
            "actual_delta_usd": self.actual_delta_usd,
            "model_peak_price": self.model_peak_price, "model_delta_usd": self.model_delta_usd,
            "model_peak_month": self.model_peak_month,
            "error_usd": self.error_usd, "abs_error_usd": self.abs_error_usd,
            "rel_error": self.rel_error, "ratio_model_to_actual": self.ratio,
            "direction_ok": self.direction_ok, "order_ok": self.order_ok,
            "baseline_abs_error_usd": self.baseline_abs_error_usd,
            "beats_baseline": self.beats_baseline,
            "sensitivity": [dict(item) for item in self.sensitivity],
            "sensitivity_range": dict(self.sensitivity_range),
            "checks": dict(self.checks),
        }


def _sensitivity(target: Episode, base_delta: float) -> tuple[tuple[dict, ...], dict]:
    variants = (
        ("elasticity_less_elastic", {"elasticity_short": target.elasticity_short * 0.6,
                                     "elasticity_long": target.elasticity_long * 0.6}),
        ("elasticity_more_elastic", {"elasticity_short": target.elasticity_short * 1.5,
                                     "elasticity_long": target.elasticity_long * 1.4}),
        ("no_strategic_release", {"spr_stock": 0.0, "spr_max_draw": 0.0}),
        ("bypass_plus_1mbd", {"bypass_capacity": target.bypass_capacity + 1.0}),
        ("no_risk_premium", {"risk_premium": 0.0}),
        ("premium_doubled", {"risk_premium": target.risk_premium * 2.0}),
        # literature-range elasticities (barely price-elastic short run): the episodes are
        # scored again with a much lower elasticity to show how far the model can move
        ("elasticity_literature_low", {"elasticity_short": target.elasticity_short * 0.35,
                                       "elasticity_long": target.elasticity_long * 0.4}),
        ("elasticity_literature_high", {"elasticity_short": target.elasticity_short * 2.0,
                                        "elasticity_long": target.elasticity_long * 1.8}),
    )
    rows = []
    for name, overrides in variants:
        try:
            calibration, shock = build_case(target, **overrides)
            path = simulate(calibration, shock)
        except MarketError as error:  # fail-closed: a broken variant is reported, not hidden
            rows.append({"variant": name, "overrides": dict(overrides),
                         "error": f"{type(error).__name__}: {error}"})
            continue
        peak = max(path.price)
        delta = peak - calibration.base_price
        rows.append({"variant": name, "overrides": dict(overrides),
                     "model_peak_price": round(peak, 6), "model_delta_usd": round(delta, 6),
                     "delta_vs_central": round(delta - base_delta, 6)})
    values = [row["model_delta_usd"] for row in rows if "model_delta_usd" in row]
    sensitivity_range = {
        "low": round(min(values), 6) if values else None,
        "high": round(max(values), 6) if values else None,
        "actual_within_range": (min(values) - 1e-9 <= target.actual_delta_usd
                                <= max(values) + 1e-9) if values else False,
        "variants": len(rows),
    }
    rows.sort(key=lambda row: (row.get("model_delta_usd", -1e9), row["variant"]))
    return tuple(rows), sensitivity_range


def run_episode(target: Episode, **overrides) -> EpisodeResult:
    """Run one episode through the oil engine and score it against the historical change."""
    calibration, shock = build_case(target, **overrides)
    path = simulate(calibration, shock)
    if not path.ok:
        raise BacktestError(f"{target.episode_id}: engine checks failed {path.checks}")
    peak = max(path.price)
    model_delta = peak - calibration.base_price
    actual_delta = float(target.actual_delta_usd)
    error = model_delta - actual_delta
    ratio = model_delta / actual_delta if actual_delta != 0.0 else None
    sensitivity, sensitivity_range = _sensitivity(target, model_delta)
    checks = {
        "engine_ok": path.ok,
        "same_sign": (model_delta > 0) == (actual_delta > 0) and model_delta != 0.0,
        "finite_metrics": all(math.isfinite(value) for value in
                              (peak, model_delta, error, target.actual_peak_price)),
        "sensitivity_declared": sensitivity_range["variants"] == 8,
        "deterministic_inputs": True,
    }
    return EpisodeResult(
        episode_id=target.episode_id, name=target.name, date=target.date,
        base_price=calibration.base_price, actual_delta_usd=actual_delta,
        actual_peak_price=float(target.actual_peak_price),
        model_delta_usd=round(model_delta, 6), model_peak_price=round(peak, 6),
        model_peak_month=int(path.summary()["price_peak_month"]),
        error_usd=round(error, 6), abs_error_usd=round(abs(error), 6),
        rel_error=round(error / actual_delta, 6) if actual_delta else None,
        ratio=round(ratio, 6) if ratio is not None else None,
        direction_ok=checks["same_sign"],
        order_ok=bool(ratio is not None and ORDER_LOW <= ratio <= ORDER_HIGH),
        baseline_abs_error_usd=round(abs(actual_delta), 6),
        beats_baseline=abs(error) < abs(actual_delta),
        sensitivity=sensitivity, sensitivity_range=sensitivity_range, checks=checks)


@dataclass(frozen=True)
class BacktestReport:
    """Aggregate backtest report (research-only; honest verdict, no admission claims)."""

    episodes: tuple[EpisodeResult, ...]
    metrics: dict
    verdict: str
    where_worse: tuple[str, ...]
    schema: str = RESULT_SCHEMA
    status: str = RESEARCH_ONLY

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise BacktestError("backtest reports are research-only")
        if not self.episodes:
            raise BacktestError("backtest report must contain at least one episode")

    def as_dict(self) -> dict:
        return {
            "schema": self.schema, "status": self.status, "research_only": True,
            "note": "research backtest on declared public approximations; not a forecast, not a "
                    "validation and not an admission",
            "verdict": self.verdict, "where_worse": list(self.where_worse),
            "metrics": dict(self.metrics),
            "episodes": [item.as_dict() for item in self.episodes],
            "assumptions": [assumption.as_dict() for episode_result in self.episodes
                            for assumption in _episode_assumptions(episode_result.episode_id)],
            "digest": self.digest(),
        }

    def digest(self) -> str:
        payload = {"verdict": self.verdict, "metrics": self.metrics,
                   "episodes": [item.as_dict() for item in self.episodes]}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str,
                          separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _episode_assumptions(episode_id: str) -> tuple:
    return episode(episode_id).inputs()


def _verdict(results: Sequence[EpisodeResult], metrics: Mapping) -> str:
    direction_hits = sum(1 for item in results if item.direction_ok)
    order_hits = sum(1 for item in results if item.order_ok)
    if direction_hits == len(results) and order_hits == len(results) \
            and metrics["mae_model_usd"] < metrics["mae_baseline_usd"]:
        return "PASS_DIRECTION_AND_ORDER"
    if direction_hits == len(results):
        return "DIRECTION_OK_MAGNITUDE_OFF"
    return "FAIL"


def backtest(episode_ids: Sequence[str] | None = None, **overrides) -> BacktestReport:
    """Run the declared episodes (or a subset) and compute aggregate error metrics."""
    ids = ([item.episode_id for item in EPISODES] if episode_ids is None
           else list(episode_ids))
    if not ids:
        raise BacktestError("no episodes selected")
    results = []
    for episode_id in ids:
        target = episode(episode_id) if not isinstance(episode_id, Episode) else episode_id
        results.append(run_episode(target, **overrides))
    errors = [item.abs_error_usd for item in results]
    baseline = [item.baseline_abs_error_usd for item in results]
    mae_model = math.fsum(errors) / len(errors)
    mae_baseline = math.fsum(baseline) / len(baseline)
    mean_actual = math.fsum(abs(item.actual_delta_usd) for item in results) / len(results)
    metrics = {
        "episodes": len(results),
        "direction_hits": sum(1 for item in results if item.direction_ok),
        "order_hits": sum(1 for item in results if item.order_ok),
        "mae_model_usd": round(mae_model, 6),
        "mae_baseline_usd": round(mae_baseline, 6),
        "rmse_model_usd": round(math.sqrt(math.fsum(error * error for error in
                                                   [item.error_usd for item in results])
                                          / len(results)), 6),
        "mean_actual_delta_usd": round(mean_actual, 6),
        "mean_model_delta_usd": round(math.fsum(item.model_delta_usd for item in results)
                                      / len(results), 6),
        "skill_score_vs_no_shock": round(1.0 - mae_model / mae_baseline, 6)
        if mae_baseline else None,
        "mean_ratio": round(math.fsum(item.ratio for item in results
                                      if item.ratio is not None) / len(results), 6),
        "sensitivity_covers_actual": sum(1 for item in results
                                         if item.sensitivity_range.get("actual_within_range")),
        "order_band": [ORDER_LOW, ORDER_HIGH],
    }
    where_worse = tuple(item.episode_id for item in results if not item.beats_baseline)
    report = BacktestReport(episodes=tuple(results), metrics=metrics,
                            verdict=_verdict(results, metrics), where_worse=where_worse)
    return report


def report_markdown(report: BacktestReport) -> str:
    """A human-readable table of the backtest (aggregates only)."""
    lines = [
        "# Бэктест нефтяного модуля (1990 / 2019 / 2022)",
        "",
        f"**Статус:** {report.status} — исследовательский бэктест, не прогноз и не допуск.",
        f"**Verdict:** `{report.verdict}`",
        f"**Ошибки:** MAE модели {report.metrics['mae_model_usd']:.2f} USD/bbl против "
        f"MAE наивного «без шока» {report.metrics['mae_baseline_usd']:.2f}; "
        f"skill {report.metrics['skill_score_vs_no_shock']:+.3f}; "
        f"направление верно в {report.metrics['direction_hits']}/{report.metrics['episodes']}, "
        f"порядок (0.5–2x) — в {report.metrics['order_hits']}/{report.metrics['episodes']}.",
        "",
        "| Эпизод | Brent до | Факт Δ | Модель Δ | Ошибка, $ | Отн. ошибка | Ratio | Направление | Порядок |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report.episodes:
        rel = "—" if item.rel_error is None else f"{item.rel_error:+.2f}"
        ratio = "—" if item.ratio is None else f"{item.ratio:.2f}"
        lines.append(
            f"| {item.episode_id} ({item.date}) | {item.base_price:.1f} | "
            f"{item.actual_delta_usd:+.1f} | {item.model_delta_usd:+.2f} | "
            f"{item.error_usd:+.2f} | {rel} | {ratio} | "
            f"{'OK' if item.direction_ok else 'FAIL'} | {'OK' if item.order_ok else 'FAIL'} |")
    lines += [
        "",
        "| Эпизод | Чувствительность: Δ модели (low … high) | Факт в диапазоне |",
        "|---|---|---|",
    ]
    for item in report.episodes:
        low = item.sensitivity_range.get("low")
        high = item.sensitivity_range.get("high")
        lines.append(f"| {item.episode_id} | {low:+.2f} … {high:+.2f} | "
                     f"{'да' if item.sensitivity_range.get('actual_within_range') else 'нет'} |")
    lines += [
        "",
        f"**Хуже наивного «без шока»:** {', '.join(report.where_worse) or 'нет'}.",
        "",
        "**Ограничения:** три эпизода; входы и фактические Δ Brent — публичные приближения "
        "(`source_class=\"assumption\"`), не PIT-ряды; нет внутридневных данных и торговых "
        "маршрутов; параметры не калиброваны. Выводы — только о направлении/порядке, не о точности.",
        f"**Дайджест:** `{report.digest()}`",
        "",
    ]
    return "\n".join(lines)
