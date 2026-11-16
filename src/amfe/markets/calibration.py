"""PIT-style calibration and out-of-sample check of the oil module (docs/158).

Discipline (the whole point of this module)
-------------------------------------------
1. **Pre-registration first.** :func:`build_protocol` returns a deterministic protocol document
   (parameter grid, selection rule, success criteria, train and OOS episode ids) and
   :func:`protocol_sha256` hashes it.  The protocol is created *before* any OOS evaluation and its
   digest is carried into the report; ``tools/oil_calibration.py --protocol-only`` prints it.
2. **Train only.** :func:`fit` accepts an explicit list of train episodes and evaluates the grid on
   them only.  It never imports or reads the OOS episode deltas; the OOS list is a separate constant
   and a test changes the OOS actuals to prove the fitted parameters do not move.
3. **OOS is scored, not tuned.** The calibrated parameters are fixed once (best train RMSE) and then
   applied unchanged to 1990/2019/2022.  The verdict compares them with the previous fixed
   calibration from :mod:`amfe.markets.backtest` (docs/155) using the pre-registered criteria.

All inputs are public approximations marked ``source_class="assumption"``: this is a research
calibration, not an admission and not a forecast.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .backtest import Episode, build_case, run_episode
from .contract import MarketError, RESEARCH_ONLY
from .engine import simulate

PROTOCOL_VERSION = "amfe.markets.calibration.protocol.v1"
REPORT_VERSION = "amfe.markets.calibration.report.v1"
PROTOCOL_CREATED = "2026-11-16"  # protocol date (deliberately not a runtime timestamp)
#: Episodes used for the grid search -- three supply-shock episodes, all pre-2019.
TRAIN_EPISODES: tuple[Episode, ...] = (
    Episode(
        episode_id="2003_iraq",
        name="2003 Iraq war (March 2003)",
        date="2003-03",
        base_price=25.0, supply_loss=2.3, loss_months=3,
        demand=78.0, supply=78.0,
        bypass_capacity=0.3, bypass_ramp_months=3,
        supply_response=0.8, supply_response_ramp_months=3,
        spr_stock=0.0, spr_max_draw=0.0,
        commercial_stock=200.0, commercial_max_draw=1.0,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=0.0, horizon_months=6,
        actual_peak_price=33.0, actual_delta_usd=8.0,
        source_note="Brent ~25 USD/bbl in January 2003, peak ~33 in March 2003; Iraqi output "
                    "~2.3 mb/d lost; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
    Episode(
        episode_id="2011_libya",
        name="2011 Libya civil war (February 2011)",
        date="2011-02",
        base_price=95.0, supply_loss=1.5, loss_months=6,
        demand=89.0, supply=89.0,
        bypass_capacity=0.2, bypass_ramp_months=2,
        supply_response=1.2, supply_response_ramp_months=4,
        spr_stock=60.0, spr_max_draw=1.0,
        commercial_stock=200.0, commercial_max_draw=1.0,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=0.0, horizon_months=6,
        actual_peak_price=127.0, actual_delta_usd=32.0,
        source_note="Brent ~95 USD/bbl in January 2011, peak ~127 in April 2011; Libyan output "
                    "~1.5 mb/d lost; IEA released ~60 mb; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
    Episode(
        episode_id="2018_iran",
        name="2018 Iran sanctions and Venezuela decline (March-October 2018)",
        date="2018-03",
        base_price=66.0, supply_loss=2.0, loss_months=6,
        demand=100.0, supply=100.0,
        bypass_capacity=0.2, bypass_ramp_months=2,
        supply_response=1.5, supply_response_ramp_months=4,
        spr_stock=0.0, spr_max_draw=0.0,
        commercial_stock=200.0, commercial_max_draw=1.0,
        elasticity_short=-0.25, elasticity_long=-0.55,
        risk_premium=0.0, horizon_months=6,
        actual_peak_price=86.0, actual_delta_usd=20.0,
        source_note="Brent ~66 USD/bbl in March 2018, peak ~86 in October 2018; Iran export loss "
                    "plus Venezuela decline ~2 mb/d; public approximations",
        sources=("public oil-market history (EIA/IEA episode summaries)",),
    ),
)
TRAIN_IDS = tuple(item.episode_id for item in TRAIN_EPISODES)

#: The held-out episodes (docs/155): never used by :func:`fit`.
OOS_IDS: tuple[str, ...] = ("1990_gulf", "2019_abqaiq", "2022_russia")

#: Pre-registered grid (magnitudes; the module uses negative elasticities).
GRID_ELASTICITY_SHORT: tuple[float, ...] = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25)
GRID_LONG_MULTIPLIER: tuple[float, ...] = (1.6, 2.0, 2.4)
GRID_RISK_PREMIUM: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0)

#: Pre-registered selection and success rules (hash-protected via the protocol digest).
SELECTION_RULE = ("minimize the root-mean-square error of the peak Brent change on the train "
                  "episodes; ties broken by (risk premium, |elasticity_short|, |elasticity_long|)")
SUCCESS_CRITERIA = {
    "OOS_PASS": "direction hits = 3/3 AND order hits >= 2/3 AND MAE_calibrated < MAE_fixed",
    "OOS_PARTIAL": "direction hits = 3/3 AND (order hits >= 2/3 OR MAE_calibrated < MAE_fixed)",
    "OOS_FAIL": "otherwise",
}
ORDER_BAND = (0.5, 2.0)


class CalibrationError(MarketError):
    """Fail-closed calibration error (bad grid, train/OOS overlap, empty selection)."""


def _grid_size() -> int:
    return len(GRID_ELASTICITY_SHORT) * len(GRID_LONG_MULTIPLIER) * len(GRID_RISK_PREMIUM)


def build_protocol() -> dict:
    """The pre-registered protocol document (deterministic; hash it before running OOS)."""
    protocol = {
        "schema": PROTOCOL_VERSION,
        "protocol_id": "oil-pit-2026-11-16",
        "created": PROTOCOL_CREATED,
        "status": RESEARCH_ONLY,
        "research_only": True,
        "objective": "calibrate oil-module effective elasticity and risk premium on train "
                     "episodes, then score the unchanged parameters on held-out episodes",
        "train_episodes": list(TRAIN_IDS),
        "oos_episodes": list(OOS_IDS),
        "train_oos_disjoint": set(TRAIN_IDS).isdisjoint(OOS_IDS),
        "grid": {
            "elasticity_short_magnitude": list(GRID_ELASTICITY_SHORT),
            "elasticity_long_multiplier": list(GRID_LONG_MULTIPLIER),
            "risk_premium_usd_per_bbl": list(GRID_RISK_PREMIUM),
            "size": _grid_size(),
            "note": "elasticities are applied with a negative sign; long magnitude must not be "
                    "below the short one (engine validates this)",
        },
        "selection_rule": SELECTION_RULE,
        "success_criteria": dict(SUCCESS_CRITERIA),
        "metrics": ["direction_ok", "order_ok", "abs_error_usd", "rel_error", "ratio",
                    "MAE", "RMSE", "skill_score_vs_no_shock", "where_worse"],
        "order_band": list(ORDER_BAND),
        "reference_calibration": {
            "source": "docs/155 fixed calibration (oil module defaults)",
            "elasticity_short": -0.25, "elasticity_long": -0.55,
            "risk_premium": "episode-declared (0/0/12 USD/bbl for 1990/2019/2022)",
        },
        "data_policy": "all inputs are public approximations declared as assumptions; no PIT "
                       "series are loaded and no OOS information enters the fit",
        "note": "research calibration, not an admission and not a forecast",
    }
    return protocol


def protocol_sha256(protocol: Mapping) -> str:
    text = json.dumps(dict(protocol), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CalibrationParams:
    """One point of the pre-registered grid."""

    elasticity_short: float
    elasticity_long: float
    risk_premium: float

    def __post_init__(self) -> None:
        if not -1.0 < self.elasticity_short < 0.0:
            raise CalibrationError("elasticity_short must be in (-1, 0)")
        if not -1.0 < self.elasticity_long < 0.0:
            raise CalibrationError("elasticity_long must be in (-1, 0)")
        if abs(self.elasticity_short) > abs(self.elasticity_long):
            raise CalibrationError("elasticity_short magnitude must not exceed elasticity_long")
        if self.risk_premium < 0.0:
            raise CalibrationError("risk_premium must be non-negative")

    def as_dict(self) -> dict:
        return {"elasticity_short": self.elasticity_short,
                "elasticity_long": self.elasticity_long,
                "risk_premium": self.risk_premium}


def grid() -> tuple[CalibrationParams, ...]:
    """The full pre-registered grid in deterministic order."""
    points = []
    for short in GRID_ELASTICITY_SHORT:
        for multiplier in GRID_LONG_MULTIPLIER:
            for premium in GRID_RISK_PREMIUM:
                points.append(CalibrationParams(elasticity_short=-short,
                                                elasticity_long=-(short * multiplier),
                                                risk_premium=premium))
    return tuple(points)


def _score_episode(target: Episode, params: CalibrationParams) -> dict:
    """Run one episode with fixed parameters and return its score (no OOS/train distinction)."""
    calibration, shock = build_case(
        target, elasticity_short=params.elasticity_short,
        elasticity_long=params.elasticity_long, risk_premium=params.risk_premium)
    path = simulate(calibration, shock)
    if not path.ok:
        raise CalibrationError(f"{target.episode_id}: engine checks failed {path.checks}")
    peak = max(path.price)
    model_delta = peak - calibration.base_price
    actual = float(target.actual_delta_usd)
    error = model_delta - actual
    ratio = model_delta / actual
    return {
        "episode_id": target.episode_id, "base_price": calibration.base_price,
        "actual_delta_usd": actual, "model_delta_usd": round(model_delta, 6),
        "model_peak_price": round(peak, 6), "error_usd": round(error, 6),
        "abs_error_usd": round(abs(error), 6),
        "rel_error": round(error / actual, 6), "ratio": round(ratio, 6),
        "direction_ok": bool((model_delta > 0) == (actual > 0)),
        "order_ok": bool(ORDER_BAND[0] <= ratio <= ORDER_BAND[1]),
    }


def train_score(params: CalibrationParams,
                episodes: Sequence[Episode] = TRAIN_EPISODES) -> dict:
    """Score the parameters on the train episodes only (same numbers regardless of OOS)."""
    rows = [_score_episode(target, params) for target in episodes]
    squared = [row["error_usd"] ** 2 for row in rows]
    errors = [row["abs_error_usd"] for row in rows]
    return {
        "params": params.as_dict(),
        "episodes": rows,
        "mae_usd": round(math.fsum(errors) / len(errors), 6),
        "rmse_usd": round(math.sqrt(math.fsum(squared) / len(squared)), 6),
        "direction_hits": sum(1 for row in rows if row["direction_ok"]),
        "order_hits": sum(1 for row in rows if row["order_ok"]),
    }


@dataclass(frozen=True)
class FitResult:
    params: CalibrationParams
    train: dict
    candidates: int
    selection_rule: str = SELECTION_RULE

    def as_dict(self) -> dict:
        return {"params": self.params.as_dict(), "train": self.train,
                "candidates_evaluated": self.candidates,
                "selection_rule": self.selection_rule}


def fit(episodes: Sequence[Episode] = TRAIN_EPISODES) -> FitResult:
    """Grid-search the pre-registered parameters on the given train episodes only."""
    episodes = tuple(episodes)
    if not episodes:
        raise CalibrationError("fit needs at least one train episode")
    ids = tuple(item.episode_id for item in episodes)
    if set(ids) & set(OOS_IDS):
        raise CalibrationError(f"train episodes overlap the held-out set: {sorted(set(ids) & set(OOS_IDS))}")
    best = None
    evaluated = 0
    for params in grid():
        score = train_score(params, episodes)
        evaluated += 1
        key = (score["rmse_usd"], params.risk_premium, abs(params.elasticity_short),
               abs(params.elasticity_long))
        if best is None or key < best[0]:
            best = (key, params, score)
    return FitResult(params=best[1], train=best[2], candidates=evaluated)


def oos_fixed() -> dict:
    """Score the held-out episodes with the previous fixed calibration (docs/155)."""
    from .backtest import episode as _episode

    rows = []
    for episode_id in OOS_IDS:
        result = run_episode(_episode(episode_id))
        row = result.as_dict()
        rows.append({
            "episode_id": row["episode_id"], "base_price": row["base_price"],
            "actual_delta_usd": row["actual_delta_usd"],
            "model_delta_usd": row["model_delta_usd"],
            "model_peak_price": row["model_peak_price"],
            "error_usd": row["error_usd"], "abs_error_usd": row["abs_error_usd"],
            "rel_error": row["rel_error"], "ratio": row["ratio_model_to_actual"],
            "direction_ok": row["direction_ok"], "order_ok": row["order_ok"],
        })
    return _aggregate(rows)


def oos_calibrated(params: CalibrationParams) -> dict:
    """Score the held-out episodes with the fixed calibrated parameters (no re-tuning)."""
    from .backtest import episode as _episode

    rows = [_score_episode(_episode(episode_id), params) for episode_id in OOS_IDS]
    return _aggregate(rows)


def _aggregate(rows: Sequence[dict]) -> dict:
    errors = [row["abs_error_usd"] for row in rows]
    squared = [row["error_usd"] ** 2 for row in rows]
    mae = math.fsum(errors) / len(errors)
    baseline = math.fsum(abs(row["actual_delta_usd"]) for row in rows) / len(rows)
    return {
        "episodes": [dict(row) for row in rows],
        "mae_usd": round(mae, 6),
        "rmse_usd": round(math.sqrt(math.fsum(squared) / len(squared)), 6),
        "mae_baseline_usd": round(baseline, 6),
        "skill_score_vs_no_shock": round(1.0 - mae / baseline, 6) if baseline else None,
        "direction_hits": sum(1 for row in rows if row["direction_ok"]),
        "order_hits": sum(1 for row in rows if row["order_ok"]),
        "n": len(rows),
    }


def verdict(calibrated: Mapping, fixed: Mapping, criteria: Mapping | None = None) -> str:
    """Apply the pre-registered success criteria (no post-hoc loosening)."""
    criteria = criteria or SUCCESS_CRITERIA
    direction_hits = calibrated["direction_hits"]
    order_hits = calibrated["order_hits"]
    better_mae = calibrated["mae_usd"] < fixed["mae_usd"]
    if direction_hits == calibrated["n"] and order_hits >= 2 and better_mae:
        return "OOS_PASS"
    if direction_hits == calibrated["n"] and (order_hits >= 2 or better_mae):
        return "OOS_PARTIAL"
    return "OOS_FAIL"


@dataclass(frozen=True)
class CalibrationReport:
    """Protocol + fitted parameters + OOS comparison (research-only)."""

    protocol: dict
    protocol_sha256: str
    fit_result: FitResult
    oos_calibrated: dict
    oos_fixed: dict
    verdict: str
    where_worse: tuple[str, ...]
    status: str = RESEARCH_ONLY
    schema: str = REPORT_VERSION

    def __post_init__(self) -> None:
        if self.status != RESEARCH_ONLY:
            raise CalibrationError("calibration reports are research-only")

    def as_dict(self) -> dict:
        return {
            "schema": self.schema, "status": self.status, "research_only": True,
            "note": "PIT-style calibration on declared public approximations; not an admission "
                    "and not a forecast",
            "protocol": dict(self.protocol),
            "protocol_sha256": self.protocol_sha256,
            "fit": self.fit_result.as_dict(),
            "oos_calibrated": dict(self.oos_calibrated),
            "oos_fixed_reference": dict(self.oos_fixed),
            "verdict": self.verdict,
            "where_worse": list(self.where_worse),
            "comparison": {
                "mae_calibrated_usd": self.oos_calibrated["mae_usd"],
                "mae_fixed_usd": self.oos_fixed["mae_usd"],
                "mae_delta_usd": round(self.oos_calibrated["mae_usd"]
                                       - self.oos_fixed["mae_usd"], 6),
                "direction_calibrated": self.oos_calibrated["direction_hits"],
                "direction_fixed": self.oos_fixed["direction_hits"],
                "order_calibrated": self.oos_calibrated["order_hits"],
                "order_fixed": self.oos_fixed["order_hits"],
            },
            "digest": self.digest(),
        }

    def digest(self) -> str:
        payload = {"protocol_sha256": self.protocol_sha256, "fit": self.fit_result.as_dict(),
                   "oos_calibrated": self.oos_calibrated, "oos_fixed": self.oos_fixed,
                   "verdict": self.verdict, "where_worse": list(self.where_worse)}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def calibrate() -> CalibrationReport:
    """Protocol -> fit on train -> OOS scoring.  OOS information never feeds the fit."""
    protocol = build_protocol()
    digest = protocol_sha256(protocol)
    fitted = fit(TRAIN_EPISODES)
    calibrated = oos_calibrated(fitted.params)
    fixed = oos_fixed()
    where_worse = tuple(
        row["episode_id"]
        for row, reference in zip(calibrated["episodes"], fixed["episodes"])
        if row["abs_error_usd"] >= reference["abs_error_usd"])
    return CalibrationReport(
        protocol=protocol, protocol_sha256=digest, fit_result=fitted,
        oos_calibrated=calibrated, oos_fixed=fixed,
        verdict=verdict(calibrated, fixed), where_worse=where_worse)


def report_markdown(report: CalibrationReport) -> str:
    """Human-readable calibration report (aggregates only)."""
    fit_result = report.fit_result
    params = fit_result.params
    comparison = report.as_dict()["comparison"]
    lines = [
        "# PIT-калибровка и OOS нефтяного модуля (1990 / 2019 / 2022)",
        "",
        f"**Статус:** {report.status} — исследование, не допуск и не прогноз.",
        f"**Protocol SHA-256:** `{report.protocol_sha256}`",
        f"**Verdict:** `{report.verdict}`",
        "",
        "## Протокол (до OOS)",
        f"- Train: {', '.join(report.protocol['train_episodes'])}",
        f"- OOS: {', '.join(report.protocol['oos_episodes'])}",
        f"- Сетка: {report.protocol['grid']['size']} точек "
        f"(εshort {report.protocol['grid']['elasticity_short_magnitude']}, "
        f"×{report.protocol['grid']['elasticity_long_multiplier']}, "
        f"premium {report.protocol['grid']['risk_premium_usd_per_bbl']})",
        f"- Правило отбора: {report.protocol['selection_rule']}",
        "- Критерии: " + "; ".join(f"{key}: {value}"
                                   for key, value in report.protocol['success_criteria'].items()),
        "",
        "## Обучение (train)",
        f"- Выбрано: εshort={params.elasticity_short}, εlong={params.elasticity_long}, "
        f"risk premium={params.risk_premium} USD/bbl (перебрано {fit_result.candidates} точек)",
        f"- Train MAE {fit_result.train['mae_usd']:.2f}, RMSE {fit_result.train['rmse_usd']:.2f}; "
        f"направление {fit_result.train['direction_hits']}/{len(fit_result.train['episodes'])}, "
        f"порядок {fit_result.train['order_hits']}/{len(fit_result.train['episodes'])}",
        "",
        "## OOS: калиброванные параметры против прежней фиксированной калибровки",
        "",
        "| Эпизод | Факт Δ | Ошибка калибр. | Ошибка фикс. | Ratio калибр. | Направление |",
        "|---|---|---|---|---|---|",
    ]
    for row, reference in zip(report.oos_calibrated["episodes"], report.oos_fixed["episodes"]):
        lines.append(
            f"| {row['episode_id']} | {row['actual_delta_usd']:+.1f} | "
            f"{row['error_usd']:+.2f} | {reference['error_usd']:+.2f} | "
            f"{row['ratio']:.3f} | {'OK' if row['direction_ok'] else 'FAIL'} |")
    lines += [
        "",
        f"- MAE: калиброванная **{comparison['mae_calibrated_usd']:.2f}** против фиксированной "
        f"**{comparison['mae_fixed_usd']:.2f}** (Δ {comparison['mae_delta_usd']:+.2f} USD/bbl)",
        f"- Направление: {comparison['direction_calibrated']}/3 против "
        f"{comparison['direction_fixed']}/3; порядок: {comparison['order_calibrated']}/3 против "
        f"{comparison['order_fixed']}/3",
        f"- Хуже фиксированной калибровки: {', '.join(report.where_worse) or 'нигде'}",
        "",
        "**Ограничения:** 3 train и 3 OOS эпизода; входы и фактические Δ Brent — публичные "
        "приближения, не PIT-ряды; сетка и критерии предрегистрированы, но данных слишком мало "
        "для статистической значимости; 2019/1990 структурно несовместимы с одной глобальной "
        "эластичностью — главный вывод. Выводы — только о направлении/порядке.",
        f"**Дайджест отчёта:** `{report.digest()}`",
        "",
    ]
    return "\n".join(lines)
