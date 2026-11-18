#!/usr/bin/env python3
"""Forecast -> world bridge: T1/T2/T3 forecast (point + interval) -> MRIO scenarios (task-55).

The bridge is the missing link between the **product feed** (``build/product/forecast_latest.json``,
docs/113) and the **world scenario artefacts** (``build/world``, ``build/world_oecd_2025``,
docs/131/146), plus the existing price/quantity channel (``build/quantity_bridge``, docs/159).

It consumes only aggregates that already exist in the repository:

* point and interval of the product forecast (T1_CPI_YOY, T2_GDP_GROWTH, T3_RECESSION);
* the Leontief price-pass-through / demand-response calibration measured by
  ``tools/quantity_bridge.py`` (``aggregate_relative`` price shock and the ``final_demand`` /
  ``intermediate_input`` / ``combined`` relative output responses per pass-through level);
* the baseline totals and the country/industry margins of the existing comparative-static
  reports (``reports/<scenario>.json``) and their index (``report_index.json``).

No MRIO matrix, no raw third-party series and no new exogenous data are read: ``Z``, ``x``, ``y``,
per-sector values and dataset files stay out of the pipeline (same rule as docs/121/127/131).

Three scenarios (``base``, ``inflation_plus_1pp``, ``growth_minus_1pp``) are computed as linear,
declared-elasticity responses:

* price channel: ``Δx/x = S_price · Δp``, ``S_price = combined.long.relative_delta_output /
  price_stage.aggregate_relative`` (exact reuse of the quantity-bridge calibration);
* growth channel: ``Δx/x = M · Δg``, ``M = baseline.total_output / baseline.total_final_demand``
  (aggregate Leontief demand multiplier of the same report);
* both channels are also re-scaled through the baseline output to USD mn and allocated to a
  country x industry table with a **separable (outer-product) allocation** built from the two
  existing margins: ``Δx[c,i] = Δ_c · Δ_i / Δ_total``. That reproduces both margins exactly; it is
  a declared allocation device, not a cell-level model.

Fail-closed: the bridge refuses to write numbers when a required forecast target is missing
(strict mode, exit 2), when the feed is not valid JSON, when the interval is inconsistent with the
point, when a template/report/index artefact is missing, or when the balance checks fail. The
``--partial`` mode is the only way to get a report with a blocked scenario; it is marked
``PARTIAL_INPUTS``, the blocked scenario carries no numbers and the exit code is 3.

Determinism: fixed scenario order, sorted keys, no wall-clock in the payload when
``--generated-at`` is supplied (same convention as ``tools/world_reports.py`` and
``tools/quantity_bridge.py``). Two runs on the same inputs are byte-identical.

Research-only: every artefact carries ``status = RESEARCH_ONLY`` and ``admission = false``; the
numbers are comparative statics with declared elasticities, not a forecast/admission/advice.

Usage
-----
    python tools/forecast_world_bridge.py --dataset figaro --generated-at 2026-11-18T00:00:00Z
    python tools/forecast_world_bridge.py --feed build/product/forecast_latest.json --partial
    python -m unittest tests.test_forecast_world_bridge -v
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "amfe.forecast_world_bridge.v1"
STATUS = "RESEARCH_ONLY"
ADMISSION = False

DEFAULT_FEED = ROOT / "build" / "product" / "forecast_latest.json"
DEFAULT_OUT = ROOT / "build" / "forecast_world"
QUANTITY_REPORT = ROOT / "build" / "quantity_bridge" / "quantity_report.json"

TARGETS = (
    ("T1", "T1_CPI_YOY", "CPI г/г, %"),
    ("T2", "T2_GDP_GROWTH", "рост ВВП, %"),
    ("T3", "T3_RECESSION", "вероятность рецессии, доля"),
)
TARGET_IDS = {key: ident for key, ident, _ in TARGETS}
IDENT_TO_KEY = {ident: key for key, ident, _ in TARGETS}

DIAGNOSTIC_QUANTILES = "quantiles_5_to_95_step_5"
QUANTILE_STEP = 19  # 5..95 with step 5

DATASETS = {
    "figaro": {
        "label": "FIGARO 2019 (50 стран x 64 отрасли)",
        "index": ROOT / "build" / "world" / "report_index.json",
        "reports": ROOT / "build" / "world" / "reports",
        "quantity_key": "figaro",
    },
    "oecd_2025": {
        "label": "OECD ICIO 2025, 2019 (81 страна x 50 отраслей)",
        "index": ROOT / "build" / "world_oecd_2025" / "report_index.json",
        "reports": ROOT / "build" / "world_oecd_2025" / "reports",
        "quantity_key": "oecd_2025",
    },
}

# Scenario shifts are declared in percentage points (pp) relative to the forecast point.
SCENARIOS = (
    {
        "id": "base",
        "label": "Базовый (прогноз как есть)",
        "cpi_shift_pp": 0.0,
        "growth_shift_pp": 0.0,
        "needs": (),
        "description": ("мир без дополнительного шока: каналы равны нулю, в отчёт попадают точечный "
                        "прогноз и интервал продуктового фида"),
    },
    {
        "id": "inflation_plus_1pp",
        "label": "Инфляция +1 пп",
        "cpi_shift_pp": 1.0,
        "growth_shift_pp": 0.0,
        "needs": ("T1",),
        "description": ("CPI на 1 пп выше прогноза: ценовой канал quantity_bridge (эластичности -> "
                        "спрос -> выпуск Леонтьева)"),
    },
    {
        "id": "growth_minus_1pp",
        "label": "Рост −1 пп",
        "cpi_shift_pp": 0.0,
        "growth_shift_pp": -1.0,
        "needs": ("T2",),
        "description": ("рост ВВП на 1 пп ниже прогноза: спросовый канал с агрегатным множителем "
                        "выпуска из базовой линии мирового отчёта"),
    },
)

# Existing comparative-static reports used as **shape templates** for the allocation.
DEFAULT_PRICE_TEMPLATE = "import_plus20"   # cost-push / import-cost shock: closest inflation analogue
DEFAULT_GROWTH_TEMPLATE = "energy_minus5"  # negative final-demand shock: closest contraction analogue

SHORT_LONG = ("short", "long")
CHANNEL_NAMES = ("final_demand", "intermediate_input", "combined")

# Values that must never appear in the aggregate payload (raw data / raw dataset paths).
FORBIDDEN_KEYS = {"Z", "Z_sparse", "x", "y", "va", "sector_rows", "sector_cols"}
FORBIDDEN_SUBSTRINGS = ("data/mrio", "data\\mrio", "dataset.json", "\\data\\", "F:\\", "F:/")


class ForecastWorldError(RuntimeError):
    """The bridge refuses to produce a number."""


class MissingInput(ForecastWorldError):
    """A required forecast target / artefact is absent (fail-closed)."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    """Repository-relative POSIX path; only the basename for paths outside the repository."""
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.name


def read_json(path: Path, *, what: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise MissingInput(f"{what}: файл не найден ({display_path(path)})")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ForecastWorldError(f"{what}: невалидный JSON ({display_path(path)}): {error}") from error
    if not isinstance(document, dict):
        raise ForecastWorldError(f"{what}: ожидался JSON-объект ({display_path(path)})")
    return document


def number(value, *, what: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ForecastWorldError(f"{what}: ожидалось число, получено {value!r}") from error
    if result != result or result in (float("inf"), float("-inf")):
        raise ForecastWorldError(f"{what}: не конечное число ({value!r})")
    return result


# --------------------------------------------------------------------------------- feed loading
def parse_feed(document: dict, path: Path) -> dict:
    """Validate one product-feed document and normalise point / interval / quantiles."""
    label = display_path(path)
    ident = document.get("target")
    if ident not in IDENT_TO_KEY:
        raise ForecastWorldError(f"{label}: неизвестная цель {ident!r}; ожидается "
                                 f"{sorted(IDENT_TO_KEY)}")
    key = IDENT_TO_KEY[ident]
    if "point" not in document:
        raise ForecastWorldError(f"{label}: нет поля point (точечный прогноз)")
    point = number(document.get("point"), what=f"{label}: point")

    quantiles = document.get(DIAGNOSTIC_QUANTILES)
    interval = document.get("interval")
    low = high = median = None
    if quantiles is not None:
        if not isinstance(quantiles, list) or len(quantiles) != QUANTILE_STEP:
            raise ForecastWorldError(f"{label}: {DIAGNOSTIC_QUANTILES} должен содержать "
                                     f"{QUANTILE_STEP} значений")
        values = [number(item, what=f"{label}: quantile") for item in quantiles]
        if any(right < left for left, right in zip(values, values[1:])):
            raise ForecastWorldError(f"{label}: квантили не монотонны")
        low, median, high = values[0], values[len(values) // 2], values[-1]
    elif isinstance(interval, dict) and "low" in interval and "high" in interval:
        low = number(interval["low"], what=f"{label}: interval.low")
        high = number(interval["high"], what=f"{label}: interval.high")
        median = number(interval.get("median", point), what=f"{label}: interval.median")
    else:
        raise ForecastWorldError(f"{label}: нет интервала ({DIAGNOSTIC_QUANTILES} или interval)")

    if high < low:
        raise ForecastWorldError(f"{label}: интервал перевёрнут (low={low!r} > high={high!r})")
    tolerance = 1e-9 * max(1.0, abs(point), abs(low), abs(high))
    if point < low - tolerance or point > high + tolerance:
        raise ForecastWorldError(f"{label}: точка {point!r} вне интервала [{low!r}, {high!r}]")
    if key == "T3" and not (0.0 <= low and high <= 1.0):
        raise ForecastWorldError(f"{label}: вероятность рецессии вне [0, 1]")

    horizon = document.get("horizon")
    return {
        "target": ident,
        "key": key,
        "point": point,
        "low": low,
        "median": median if median is not None else point,
        "high": high,
        "origin": document.get("origin"),
        "horizon": horizon if isinstance(horizon, int) else None,
        "status": document.get("status"),
        "candidate": document.get("candidate"),
        "data_class": document.get("data_class"),
        "path": display_path(path),
        "sha256": sha256_file(path),
    }


def load_feeds(paths) -> dict:
    """Load every feed file and merge them by target id (duplicates are refused)."""
    feeds: dict = {}
    for path in paths:
        document = read_json(path, what="product feed")
        feed = parse_feed(document, Path(path))
        ident = feed["target"]
        if ident in feeds:
            raise ForecastWorldError(f"цель {ident} встречается в двух фидах: "
                                     f"{feeds[ident]['path']} и {feed['path']}")
        feeds[ident] = feed
    return feeds


# ------------------------------------------------------------------------------- world context
def load_world_context(dataset: str, *, price_template: str, growth_template: str) -> dict:
    if dataset not in DATASETS:
        raise ForecastWorldError(f"неизвестный набор данных {dataset!r}; известны "
                                 f"{sorted(DATASETS)}")
    config = DATASETS[dataset]
    index = read_json(config["index"], what="world report index")
    index_checks = index.get("checks") or {}
    if index.get("status") != "RESEARCH_ONLY":
        raise ForecastWorldError(f"индекс мировых отчётов не RESEARCH_ONLY: {index.get('status')!r}")

    def load_template(scenario_id: str, role: str) -> dict:
        path = Path(config["reports"]) / f"{scenario_id}.json"
        report = read_json(path, what=f"{role} template {scenario_id}")
        if report.get("schema") != "amfe.world.scenario_report.v1":
            raise ForecastWorldError(f"шаблон {scenario_id}: неожиданная схема "
                                     f"{report.get('schema')!r}")
        if report.get("scenario", {}).get("id") != scenario_id:
            raise ForecastWorldError(f"шаблон {scenario_id}: id внутри отчёта "
                                     f"{report.get('scenario', {}).get('id')!r}")
        margins = report.get("impacts") or {}
        by_country = margins.get("by_country") or {}
        by_industry = margins.get("by_industry") or {}
        if not by_country or not by_industry:
            raise ForecastWorldError(f"шаблон {scenario_id}: нет страновых/отраслевых агрегатов")
        total = number(report.get("result", {}).get("total_delta_output"),
                       what=f"шаблон {scenario_id}: total_delta_output")
        return {
            "scenario_id": scenario_id,
            "role": role,
            "path": display_path(path),
            "sha256": sha256_file(path),
            "total_delta_output": total,
            "relative_delta_output": number(report["result"].get("relative_delta_output"),
                                            what=f"шаблон {scenario_id}: relative_delta_output"),
            "by_country": {str(name): float(value) for name, value in by_country.items()},
            "by_industry": {str(name): float(value) for name, value in by_industry.items()},
            "baseline": {
                "total_output": number(report.get("baseline", {}).get("total_output"),
                                       what=f"шаблон {scenario_id}: baseline.total_output"),
                "total_final_demand": number(
                    report.get("baseline", {}).get("total_final_demand"),
                    what=f"шаблон {scenario_id}: baseline.total_final_demand"),
            },
            "checks": report.get("checks") or {},
        }

    price_ctx = load_template(price_template, "price")
    growth_ctx = load_template(growth_template, "growth")

    index_scenarios = {str(item.get("id")): item for item in index.get("scenarios") or []}
    for ctx in (price_ctx, growth_ctx):
        entry = index_scenarios.get(ctx["scenario_id"])
        if not entry:
            raise ForecastWorldError(f"сценарий {ctx['scenario_id']} отсутствует в индексе "
                                     f"{display_path(config['index'])}")
        headline_total = number(entry.get("headline", {}).get("total_delta_output"),
                                what=f"индекс: {ctx['scenario_id']}.total_delta_output")
        if abs(headline_total - ctx["total_delta_output"]) > 1e-3:
            raise ForecastWorldError(
                f"индекс и отчёт расходятся по {ctx['scenario_id']}: "
                f"{headline_total!r} vs {ctx['total_delta_output']!r}")

    dataset_index = index.get("dataset") or {}
    sfc = index.get("sfc") or {}
    sfc_checks = sfc.get("checks") or {}

    quantity = read_json(QUANTITY_REPORT, what="quantity bridge report")
    entry = (quantity.get("datasets") or {}).get(config["quantity_key"])
    if not entry or entry.get("status") != "OK":
        raise MissingInput(f"в quantity_report нет пригодного набора {config['quantity_key']!r}")
    quantity_dataset = entry.get("dataset") or {}
    if quantity_dataset.get("sha256") != dataset_index.get("sha256"):
        raise ForecastWorldError("quantity_report и world index относятся к разным датасетам "
                                 "(sha256 не совпадает)")
    demand_model = quantity.get("demand_model") or {}
    return {
        "dataset": dataset,
        "label": config["label"],
        "index": {
            "path": display_path(config["index"]),
            "sha256": sha256_file(config["index"]),
            "status": index.get("status"),
            "checks": {
                "all_feasible": bool(index_checks.get("all_feasible")),
                "all_sfc_pass": bool(index_checks.get("all_sfc_pass")),
                "all_validations_pass": bool(index_checks.get("all_validations_pass")),
                "max_abs_material_residual": index_checks.get("max_abs_material_residual"),
                "scenario_count": index_checks.get("scenario_count"),
            },
            "sfc": {
                "pass": bool(sfc_checks.get("sfc_pass")),
                "saving_investment_identity_max_abs":
                    sfc_checks.get("saving_investment_identity_max_abs"),
                "global_current_account_sum": sfc_checks.get("global_current_account_sum"),
                "countries": sfc.get("countries"),
                "units": sfc.get("units"),
            },
        },
        "dataset_info": {
            "dataset_id": dataset_index.get("dataset_id"),
            "source": dataset_index.get("source"),
            "countries": dataset_index.get("countries"),
            "industries": dataset_index.get("industries"),
            "sectors": dataset_index.get("sectors"),
            "years": dataset_index.get("years"),
            "units": dataset_index.get("units"),
            "sha256": dataset_index.get("sha256"),
        },
        "quantity": {
            "path": display_path(QUANTITY_REPORT),
            "sha256": sha256_file(QUANTITY_REPORT),
            "schema": quantity.get("schema"),
            "status": quantity.get("status"),
            "dataset_key": config["quantity_key"],
            "dataset_sha256": quantity_dataset.get("sha256"),
            "groups": ((entry.get("variants") or {}).get("0.6") or {}).get("channels", {})
                      .get("groups"),
            "demand_model": {
                "schema": demand_model.get("schema"),
                "formula": demand_model.get("formula"),
                "horizon_months": demand_model.get("horizon_months"),
                "ramp_months": demand_model.get("ramp_months"),
                "elasticity_bounds": demand_model.get("elasticity_bounds"),
            },
        },
        "price_template": price_ctx,
        "growth_template": growth_ctx,
    }


# --------------------------------------------------------------------------------- coefficients
def pick_pass_through(variants: dict, pass_through: float) -> dict:
    keys = sorted(variants, key=lambda item: float(item))
    if not keys:
        raise ForecastWorldError("в quantity_report нет вариантов pass-through")
    target = f"{float(pass_through):g}"
    if target in variants:
        return variants[target]
    closest = min(keys, key=lambda item: abs(float(item) - float(pass_through)))
    return variants[closest]


def price_sensitivity(context: dict, *, pass_through: float, channel: str, horizon: str) -> dict:
    """Inflation -> output sensitivity from the quantity bridge calibration (linear in Δp)."""
    quantity = read_json(QUANTITY_REPORT, what="quantity bridge report")
    entry = quantity["datasets"][context["quantity"]["dataset_key"]]
    variant = pick_pass_through(entry.get("variants") or {}, pass_through)
    price_relative = number(variant.get("price_stage", {}).get("aggregate_relative"),
                            what="quantity_report: price_stage.aggregate_relative")
    if price_relative == 0.0:
        raise ForecastWorldError("quantity_report: нулевой агрегатный ценовой шок")
    channels = variant.get("channels") or {}
    if channel not in channels:
        raise ForecastWorldError(f"неизвестный канал {channel!r}; известны {sorted(channels)}")
    if horizon not in SHORT_LONG:
        raise ForecastWorldError(f"неизвестный горизонт {horizon!r}; известны {SHORT_LONG}")
    if channel == "combined":
        effect = channels["combined"].get(horizon) or {}
    else:
        effect = (channels[channel].get(horizon) or {})
    relative_output = number(effect.get("relative_delta_output"),
                             what=f"quantity_report: {channel}.{horizon}.relative_delta_output")
    sensitivity = relative_output / price_relative
    if sensitivity > 0.0:
        raise ForecastWorldError("quantity_report: положительная чувствительность выпуска к цене — "
                                 "калибровка несовместима со знаком канала")
    return {
        "channel": channel,
        "horizon": horizon,
        "pass_through": float(variant.get("pass_through", pass_through)),
        "relative_price_shock": price_relative,
        "relative_output": relative_output,
        "sensitivity": sensitivity,
        "formula": "Δx/x = S_price · Δp; S_price = relative_output / aggregate_relative",
        "source": f"quantity_bridge/{context['quantity']['dataset_key']}",
    }


def demand_multiplier(context: dict) -> dict:
    """Aggregate Leontief demand multiplier of the baseline (Σx / Σy) from an existing report."""
    baseline = context["growth_template"]["baseline"]
    if baseline["total_final_demand"] <= 0.0:
        raise ForecastWorldError("базовая линия: неположительный конечный спрос")
    multiplier = baseline["total_output"] / baseline["total_final_demand"]
    if multiplier < 1.0:
        raise ForecastWorldError(f"агрегатный множитель выпуска < 1 ({multiplier!r})")
    return {
        "value": multiplier,
        "base_output": baseline["total_output"],
        "base_final_demand": baseline["total_final_demand"],
        "formula": "Δx/x = M · Δy/y; M = baseline.total_output / baseline.total_final_demand",
        "source": f"reports/{context['growth_template']['scenario_id']}.json baseline",
    }


# ----------------------------------------------------------------------------------- allocation
def allocation(total: float, by_country: dict, by_industry: dict) -> dict:
    """Separable allocation Δx[c,i] = Δ_c·Δ_i/Δ_total; reproduces both margins exactly."""
    country_total = sum(by_country.values())
    industry_total = sum(by_industry.values())
    if total == 0.0:
        raise ForecastWorldError("нулевой агрегат: аллокация не определена")
    # The source reports round every margin to 1e-3 USD mn, so the sums carry a rounding tail of
    # roughly half a unit per margin; anything beyond that is a real inconsistency.
    tolerance = max(1e-9 * abs(total), 1e-3 * max(len(by_country), len(by_industry)))
    if abs(country_total - total) > tolerance or abs(industry_total - total) > tolerance:
        raise ForecastWorldError(
            f"маргиналы шаблона не сходятся с агрегатом: страна {country_total!r}, "
            f"отрасль {industry_total!r}, агрегат {total!r} (допуск {tolerance!r})")
    cells = {}
    for country, delta_country in by_country.items():
        for industry, delta_industry in by_industry.items():
            cells[(country, industry)] = delta_country * delta_industry / total
    return {"cells": cells, "total": total}


def ranked_cells(cells: dict, top_n: int) -> list:
    """Top cells by |Δx| with a deterministic tie-break (country, industry)."""
    ranked = sorted(cells.items(), key=lambda item: (-abs(item[1]), item[0][0], item[0][1]))
    share_base = sum(abs(value) for value in cells.values())
    output = []
    for (country, industry), value in ranked[:top_n]:
        output.append({
            "country": country,
            "industry": industry,
            "delta_output": round(value, 6),
            "share_of_abs": round(abs(value) / share_base, 9) if share_base else 0.0,
        })
    return output


def ranked_margin(mapping: dict) -> list:
    share_base = sum(abs(value) for value in mapping.values())
    items = [{"name": name, "delta_output": round(value, 6),
              "share_of_abs": round(abs(value) / share_base, 9) if share_base else 0.0}
             for name, value in mapping.items()]
    items.sort(key=lambda item: (-abs(item["delta_output"]), item["name"]))
    return items


# ------------------------------------------------------------------------------------ scenarios
def scenario_block(scenario: dict, feeds: dict, context: dict, *, sensitivity: dict,
                   multiplier: dict, top_n: int, full_cross: bool,
                   partial: bool) -> dict:
    missing = [key for key in scenario["needs"] if TARGET_IDS[key] not in feeds]
    block = {
        "id": scenario["id"],
        "label": scenario["label"],
        "description": scenario["description"],
        "shifts": {"cpi_pp": scenario["cpi_shift_pp"], "growth_pp": scenario["growth_shift_pp"]},
        "requires": [TARGET_IDS[key] for key in scenario["needs"]],
    }
    if missing:
        if not partial:
            raise MissingInput(f"сценарий {scenario['id']}: нет прогноза для "
                               f"{[TARGET_IDS[key] for key in missing]}")
        block.update({"status": "BLOCKED_INPUT",
                      "reason": f"нет прогноза для {[TARGET_IDS[key] for key in missing]}",
                      "price_channel": {"status": "BLOCKED_INPUT"},
                      "growth_channel": {"status": "BLOCKED_INPUT"},
                      "combined": {"status": "BLOCKED_INPUT", "delta_output_usd_mn": None,
                                   "relative_output": None,
                                   "note": "числа не публикуются: вход отсутствует (fail-closed)"},
                      "countries": [], "industries": [], "top_country_industry": [],
                      "checks": {"computed": False, "fail_closed": True}})
        return block

    price_channel = {"status": "NOT_APPLIED", "relative_price_shock": 0.0,
                     "relative_output": 0.0, "delta_output_usd_mn": 0.0,
                     "interval": {"relative_low": 0.0, "relative_high": 0.0,
                                  "delta_low": 0.0, "delta_high": 0.0}}
    if scenario["cpi_shift_pp"]:
        t1 = feeds[TARGET_IDS["T1"]]
        shift = scenario["cpi_shift_pp"] / 100.0          # 1 pp = 1% relative price
        band = ((t1["low"] - t1["point"]) / 100.0, (t1["high"] - t1["point"]) / 100.0)
        sens = sensitivity["sensitivity"]
        price_channel = {
            "status": "OK",
            "target": t1["target"],
            "forecast_point": t1["point"],
            "forecast_interval": [t1["low"], t1["high"]],
            "relative_price_shock": shift,
            "relative_price_shock_interval": [band[0], band[1]],
            "sensitivity": sens,
            "relative_output": sens * shift,
            "delta_output_usd_mn": sens * shift * multiplier["base_output"],
            "interval": {
                "relative_low": min(sens * band[0], sens * band[1]),
                "relative_high": max(sens * band[0], sens * band[1]),
                "delta_low": min(sens * band[0], sens * band[1]) * multiplier["base_output"],
                "delta_high": max(sens * band[0], sens * band[1]) * multiplier["base_output"],
            },
            "calibration": sensitivity,
        }

    growth_channel = {"status": "NOT_APPLIED", "relative_demand_shock": 0.0,
                      "relative_output": 0.0, "delta_output_usd_mn": 0.0,
                      "interval": {"relative_low": 0.0, "relative_high": 0.0,
                                   "delta_low": 0.0, "delta_high": 0.0}}
    if scenario["growth_shift_pp"]:
        t2 = feeds[TARGET_IDS["T2"]]
        shift = scenario["growth_shift_pp"] / 100.0
        band = ((t2["low"] - t2["point"]) / 100.0, (t2["high"] - t2["point"]) / 100.0)
        value = multiplier["value"]
        growth_channel = {
            "status": "OK",
            "target": t2["target"],
            "forecast_point": t2["point"],
            "forecast_interval": [t2["low"], t2["high"]],
            "relative_demand_shock": shift,
            "relative_demand_shock_interval": [band[0], band[1]],
            "multiplier": value,
            "relative_output": value * shift,
            "delta_output_usd_mn": value * shift * multiplier["base_output"],
            "interval": {
                "relative_low": min(value * band[0], value * band[1]),
                "relative_high": max(value * band[0], value * band[1]),
                "delta_low": min(value * band[0], value * band[1]) * multiplier["base_output"],
                "delta_high": max(value * band[0], value * band[1]) * multiplier["base_output"],
            },
            "calibration": multiplier,
        }

    relative_output = price_channel["relative_output"] + growth_channel["relative_output"]
    total_delta = (price_channel["delta_output_usd_mn"] + growth_channel["delta_output_usd_mn"])
    relative_low = (price_channel["interval"]["relative_low"]
                    + growth_channel["interval"]["relative_low"])
    relative_high = (price_channel["interval"]["relative_high"]
                     + growth_channel["interval"]["relative_high"])

    block.update({
        "status": "OK",
        "price_channel": price_channel,
        "growth_channel": growth_channel,
        "combined": {
            "relative_output": relative_output,
            "delta_output_usd_mn": total_delta,
            "base_output_usd_mn": multiplier["base_output"],
            "interval": {
                "relative_low": min(relative_low, relative_high),
                "relative_high": max(relative_low, relative_high),
                "delta_low": min(relative_low, relative_high) * multiplier["base_output"],
                "delta_high": max(relative_low, relative_high) * multiplier["base_output"],
            },
        },
    })

    checks = {"computed": True, "fail_closed": False}
    if total_delta == 0.0:
        block.update({"countries": [], "industries": [], "top_country_industry": [],
                      "allocation": {"status": "NOT_APPLIED",
                                     "reason": "нулевой шок: аллокация страна×отрасль не нужна"},
                      "checks": checks})
        return block

    template = context["price_template"] if scenario["cpi_shift_pp"] else context["growth_template"]
    if scenario["cpi_shift_pp"] and scenario["growth_shift_pp"]:
        raise ForecastWorldError("комбинированный шок не поддержан: один сценарий — один шаблон")
    scale = total_delta / template["total_delta_output"]
    scaled_country = {name: value * scale for name, value in template["by_country"].items()}
    scaled_industry = {name: value * scale for name, value in template["by_industry"].items()}
    shape = allocation(total_delta, scaled_country, scaled_industry)
    country_margin = {name: 0.0 for name in template["by_country"]}
    industry_margin = {name: 0.0 for name in template["by_industry"]}
    for (country, industry), value in shape["cells"].items():
        country_margin[country] += value
        industry_margin[industry] += value

    margin_country_ok = all(
        abs(country_margin[name] - scale * template["by_country"][name]) <=
        1e-6 * max(1.0, abs(scale * template["by_country"][name]))
        for name in template["by_country"])
    margin_industry_ok = all(
        abs(industry_margin[name] - scale * template["by_industry"][name]) <=
        1e-6 * max(1.0, abs(scale * template["by_industry"][name]))
        for name in template["by_industry"])
    cell_total = sum(shape["cells"].values())
    conservation_ok = abs(cell_total - total_delta) <= 1e-6 * max(1.0, abs(total_delta))
    relative_ok = abs(total_delta / multiplier["base_output"] - relative_output) <= 1e-9
    checks.update({
        "template": template["scenario_id"],
        "template_role": template["role"],
        "cells": len(shape["cells"]),
        "conservation": conservation_ok,
        "country_margins": margin_country_ok,
        "industry_margins": margin_industry_ok,
        "relative_matches_channel": relative_ok,
        "pair_sum": round(cell_total, 6),
    })
    block.update({
        "countries": ranked_margin(country_margin),
        "industries": ranked_margin(industry_margin),
        "top_country_industry": ranked_cells(shape["cells"], top_n),
        "allocation": {
            "status": "OK",
            "formula": "Δx[c,i] = Δ_c · Δ_i / Δ_total",
            "template": template["scenario_id"],
            "scale_vs_template": round(scale, 9),
            "cells": len(shape["cells"]),
            "note": ("сепарабельная (внешним произведением) аллокация по существующим маргиналам: "
                     "воспроизводит оба маргинала точно, кросс-взаимодействие не моделируется"),
        },
        "checks": checks,
    })
    if full_cross:
        block["country_industry_matrix"] = {
            "countries": sorted(template["by_country"]),
            "industries": sorted(template["by_industry"]),
            "values": [[round(shape["cells"][(country, industry)], 6)
                        for industry in sorted(template["by_industry"])]
                       for country in sorted(template["by_country"])],
            "units": "USD_mn",
        }
    return block


# --------------------------------------------------------------------------------------- report
def scan_for_raw(payload) -> list:
    """Return the list of raw-data markers found in the payload (must be empty)."""
    problems = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key in FORBIDDEN_KEYS:
                    problems.append(f"{path}.{key}: raw key")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for position, value in enumerate(node):
                walk(value, f"{path}[{position}]")
        elif isinstance(node, str):
            for marker in FORBIDDEN_SUBSTRINGS:
                if marker in node:
                    problems.append(f"{path}: raw path marker {marker!r}")

    walk(payload, "$")
    return problems


def build_report(*, feeds, dataset: str, pass_through: float, channel: str, horizon: str,
                 price_template: str, growth_template: str, top_n: int = 15,
                 full_cross: bool = False, partial: bool = False,
                 out_dir: Path | None = None, generated_at: str | None = None) -> dict:
    feed_paths = [Path(path) for path in feeds] if not isinstance(feeds, dict) else None
    loaded = load_feeds(feed_paths) if feed_paths is not None else dict(feeds)

    strict_missing = [TARGET_IDS[key] for key, ident, _ in TARGETS if ident not in loaded]
    if strict_missing and not partial:
        raise MissingInput("в продуктовом фиде нет обязательных целей: "
                           f"{strict_missing} (fail-closed; --partial разрешает частичный отчёт)")

    context = load_world_context(dataset, price_template=price_template,
                                 growth_template=growth_template)
    sensitivity = price_sensitivity(context, pass_through=pass_through, channel=channel,
                                    horizon=horizon)
    multiplier = demand_multiplier(context)

    scenarios = [scenario_block(scenario, loaded, context, sensitivity=sensitivity,
                                multiplier=multiplier, top_n=top_n, full_cross=full_cross,
                                partial=partial)
                 for scenario in SCENARIOS]

    blocked_scenarios = [item["id"] for item in scenarios if item.get("status") != "OK"]
    failed_checks = [item["id"] for item in scenarios
                     if item.get("status") == "OK"
                     and not all(value for key, value in item["checks"].items()
                                 if key in ("conservation", "country_margins",
                                            "industry_margins", "relative_matches_channel"))]
    risk = loaded.get("T3_RECESSION")
    risk_overlay = {
        "target": "T3_RECESSION",
        "status": "AVAILABLE" if risk else "NOT_AVAILABLE",
        "probability": risk["point"] if risk else None,
        "interval": [risk["low"], risk["high"]] if risk else None,
        "threshold": 0.5,
        "flag": ("RECESSION_OVERLAY_ARMED" if risk and risk["point"] >= 0.5
                 else "NOT_ARMED" if risk else "NOT_AVAILABLE"),
        "effect_on_numbers": ("нет: T3 — риск-контекст, в арифметику сценариев не входит"),
        "note": "вероятность рецессии не масштабирует выпуск: каналы остаются раздельными",
    }

    forecast_block = {}
    for key, ident, description in TARGETS:
        feed = loaded.get(ident)
        forecast_block[key] = ({
            "target": ident,
            "description": description,
            "point": feed["point"],
            "median": feed["median"],
            "interval": [feed["low"], feed["high"]],
            "interval_width": feed["high"] - feed["low"],
            "origin": feed["origin"],
            "horizon": feed["horizon"],
            "status": feed["status"],
            "candidate": feed["candidate"],
            "data_class": feed["data_class"],
            "path": feed["path"],
            "sha256": feed["sha256"],
        } if feed else {"target": ident, "description": description,
                        "status": "NOT_AVAILABLE", "point": None, "interval": None})

    report = {
        "schema": SCHEMA,
        "status": "PARTIAL_INPUTS" if (blocked_scenarios or strict_missing) else STATUS,
        "admission": ADMISSION,
        "generated_at": generated_at or now(),
        "statement": ("Мост «прогноз → мир»: точечный прогноз и интервал T1/T2/T3 переводятся в "
                      "сценарные агрегаты мира через ценовой/количественный канал "
                      "quantity_bridge и существующие сравнительно-статические отчёты. Это "
                      "исследовательская сравнительная статика с объявленными эластичностями, "
                      "НЕ прогноз мира, НЕ допуск и НЕ инвестиционная рекомендация."),
        "forecast": forecast_block,
        "model": {
            "dataset": dataset,
            "dataset_label": context["label"],
            "dataset_info": context["dataset_info"],
            "price_channel": {
                **sensitivity,
                "calibration_note": ("калибровка взята из build/quantity_bridge как есть: тот же "
                                     "ценовой этап (Leontief pass-through), те же эластичности "
                                     "спроса; шаг 1 пп инфляции = 1% относительного уровня цен"),
            },
            "growth_channel": {
                **multiplier,
                "calibration_note": ("агрегатный спросовый множитель Леонтьева из базовой линии "
                                     "существующего мирового отчёта; шаг 1 пп роста = 1% "
                                     "относительного конечного спроса"),
            },
            "templates": {
                "price": {"scenario_id": context["price_template"]["scenario_id"],
                          "path": context["price_template"]["path"],
                          "sha256": context["price_template"]["sha256"],
                          "total_delta_output": context["price_template"]["total_delta_output"],
                          "relative_delta_output":
                              context["price_template"]["relative_delta_output"],
                          "reason": "cost-push/импортные издержки: ближайший аналог инфляции"},
                "growth": {"scenario_id": context["growth_template"]["scenario_id"],
                           "path": context["growth_template"]["path"],
                           "sha256": context["growth_template"]["sha256"],
                           "total_delta_output": context["growth_template"]["total_delta_output"],
                           "relative_delta_output":
                               context["growth_template"]["relative_delta_output"],
                           "reason": "отрицательный спросовый шок: ближайший аналог −1 пп роста"},
            },
            "channels": list(CHANNEL_NAMES),
            "channel_used": channel,
            "horizon": horizon,
            "pass_through": sensitivity["pass_through"],
            "assumptions": [
                "линеаризация первого порядка: эффект пропорционален шоку (ошибка растёт с шоком)",
                "1 пп инфляции = +1% относительного уровня цен; 1 пп роста = ±1% конечного спроса",
                "оба канала переиспользуют уже опубликованные калибровки, новых эластичностей нет",
                "аллокация страна×отрасль сепарабельная (внешнее произведение маргиналов)",
                "шаблоны — существующие сценарии: цена = import_plus20, рост = energy_minus5",
                "T3 — риск-контекст и не масштабирует числа",
            ],
        },
        "inputs": {
            "feeds": [{"path": feed["path"], "sha256": feed["sha256"], "target": feed["target"]}
                      for _, feed in sorted(loaded.items())],
            "world_index": context["index"],
            "quantity_report": context["quantity"],
            "missing_targets": strict_missing,
        },
        "scenarios": scenarios,
        "risk_overlay": risk_overlay,
        "checks": {
            "mode": "partial" if partial else "strict",
            "targets_required": [ident for _, ident, _ in TARGETS],
            "targets_present": sorted(loaded),
            "missing_targets": strict_missing,
            "scenarios": [item["id"] for item in scenarios],
            "scenarios_ok": [item["id"] for item in scenarios if item.get("status") == "OK"],
            "scenarios_blocked": blocked_scenarios,
            "world_checks": context["index"]["checks"],
            "sfc_pass": context["index"]["sfc"]["pass"],
            "quantity_channel_pass": True,
            "dataset_sha_matches_quantity_report": True,
            "template_totals_match_index": True,
            "no_raw_data": True,
            "no_point_forecast": True,
            "research_only": True,
            "admission": False,
            "deterministic": True,
            "fail_closed": not partial,
            "balances": {
                "scenario_blocks_conservation": not failed_checks,
                "failed_scenarios": failed_checks,
            },
        },
        "limitations": [
            "сравнительная статика: нет цен, замещения, динамики и обратных связей;",
            "мост линеен по шоку: 1 пп и калибровка quantity_bridge — не поведенческая модель;",
            "эластичности спроса — допущения порядка величины, не оценки по данным;",
            "аллокация страна×отрасль сепарабельная: кросс-взаимодействие не моделируется, "
            "знак отдельной ячейки — следствие маргиналов;",
            "шаблоны фиксированы (цена = import_plus20, рост = energy_minus5) и меняются только "
            "через CLI; это выбор аналогии, а не оценка;",
            "T3 не масштабирует числа: вероятность рецессии приводится как риск-контекст;",
            "сырые MRIO и сторонние ряды не читаются и не публикуются: только агрегаты и SHA-256;",
            "не допуск: admission=false, статус RESEARCH_ONLY; заявлений о превосходстве нет.",
        ],
    }
    problems = scan_for_raw(report)
    if problems:
        raise ForecastWorldError("в отчёте обнаружены сырые маркеры: " + "; ".join(problems[:5]))
    report["checks"]["no_raw_data"] = not problems

    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "forecast_world.json"
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    # Canonical write (task-69): Path.write_text() translates "\n" to CRLF on Windows, so the file
    # digest stopped matching the digest of the in-memory payload.  Bytes are written as-is, in
    # UTF-8, with LF line endings on every platform.
    report_path.write_bytes(payload.encode("utf-8"))
    summary_path = out_dir / "forecast_world.md"
    summary_path.write_bytes(render_markdown(report).encode("utf-8"))
    return {"report": report, "report_path": str(report_path), "summary_path": str(summary_path),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": report_path.stat().st_size}


# ----------------------------------------------------------------------------------- markdown
def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def render_markdown(report: dict) -> str:
    model = report["model"]
    forecast = report["forecast"]
    lines = [
        "# Мост «прогноз → мир» (Сценарии T1/T2/T3 → страна×отрасль)",
        "",
        f"Статус: **{report['status']}**, admission=false, дата {report['generated_at']}.",
        "",
        "> Сравнительная статика с объявленными эластичностями: не прогноз мира, не допуск.",
        "",
        f"Набор: **{model['dataset_label']}**, канал `{model['channel_used']}` "
        f"(горизонт `{model['horizon']}`, pass-through {model['pass_through']}).",
        "",
        "## Прогноз на входе",
        "",
        "| Цель | Точка | Интервал | Ширина | Статус |",
        "|---|---|---|---|---|",
    ]
    for key in ("T1", "T2", "T3"):
        item = forecast.get(key) or {}
        if item.get("point") is None:
            lines.append(f"| {item.get('target', key)} | — | — | — | NOT_AVAILABLE |")
            continue
        low, high = item["interval"]
        lines.append(f"| {item['target']} | {_fmt(item['point'], 3)} | "
                     f"{_fmt(low, 3)} … {_fmt(high, 3)} | {_fmt(item['interval_width'], 3)} | "
                     f"{item.get('status') or '—'} |")
    lines += ["", "## Сценарии", "",
              "| Сценарий | Δ выпуска, USD млн | % выпуска | Интервал 5–95, USD млн | Статус |",
              "|---|---|---|---|---|"]
    for scenario in report["scenarios"]:
        if scenario.get("status") != "OK":
            lines.append(f"| {scenario['label']} | — | — | — | {scenario['status']} |")
            continue
        combined = scenario["combined"]
        interval = combined["interval"]
        lines.append(
            f"| {scenario['label']} | {_fmt(combined['delta_output_usd_mn'])} | "
            f"{100 * combined['relative_output']:+.4f}% | "
            f"{_fmt(interval['delta_low'])} … {_fmt(interval['delta_high'])} | OK |")
    lines.append("")
    for scenario in report["scenarios"]:
        if scenario.get("status") != "OK" or not scenario.get("top_country_industry"):
            continue
        lines += [f"### {scenario['label']}: топ страна×отрасль", "",
                  "| Страна | Отрасль | Δ выпуска, USD млн | Доля |", "|---|---|---|---|"]
        lines += [f"| {cell['country']} | {cell['industry']} | "
                  f"{_fmt(cell['delta_output'])} | {100 * cell['share_of_abs']:.2f}% |"
                  for cell in scenario["top_country_industry"]]
        lines.append("")
        countries = scenario["countries"][:10]
        lines += ["**Страновой маргинал (топ-10):**", "",
                  "| Страна | Δ выпуска, USD млн | Доля |", "|---|---|---|"]
        lines += [f"| {item['name']} | {_fmt(item['delta_output'])} | "
                  f"{100 * item['share_of_abs']:.2f}% |" for item in countries]
        lines.append("")
    risk = report["risk_overlay"]
    lines += ["## Риск-контекст (T3)", "",
              f"- Статус: **{risk['status']}**, вероятность: "
              f"{_fmt(risk['probability'], 3) if risk['probability'] is not None else '—'}, "
              f"флаг: `{risk['flag']}`",
              f"- Влияние на числа: {risk['effect_on_numbers']}",
              "",
              "## Проверки", "",
              f"- сценариев: {len(report['checks']['scenarios'])}, "
              f"выполнено: {len(report['checks']['scenarios_ok'])}, "
              f"заблокировано: {len(report['checks']['scenarios_blocked'])}",
              f"- SFC: {'PASS' if report['checks']['sfc_pass'] else 'FAIL'}, "
              f"fail_closed: {report['checks']['fail_closed']}, "
              f"no_raw_data: {report['checks']['no_raw_data']}",
              "",
              "## Ограничения", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Forecast (T1/T2/T3 point+interval) -> MRIO scenarios -> country x industry")
    parser.add_argument("--feed", action="append", default=None,
                        help=f"product feed JSON (по умолчанию {display_path(DEFAULT_FEED)})")
    parser.add_argument("--dataset", default="oecd_2025", choices=tuple(DATASETS))
    parser.add_argument("--pass-through", type=float, default=0.6)
    parser.add_argument("--channel", default="combined", choices=CHANNEL_NAMES)
    parser.add_argument("--horizon", default="long", choices=SHORT_LONG)
    parser.add_argument("--price-template", default=DEFAULT_PRICE_TEMPLATE)
    parser.add_argument("--growth-template", default=DEFAULT_GROWTH_TEMPLATE)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--full-cross", action="store_true")
    parser.add_argument("--partial", action="store_true",
                        help="разрешить отчёт с заблокированными сценариями (exit 3)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    feed_paths = args.feed or [str(DEFAULT_FEED)]
    try:
        if not 0.0 <= float(args.pass_through) <= 1.0:
            raise ForecastWorldError("pass-through должен быть в [0, 1]")
        if int(args.top) <= 0:
            raise ForecastWorldError("--top должен быть положительным")
        outcome = build_report(
            feeds=[Path(path) for path in feed_paths], dataset=args.dataset,
            pass_through=float(args.pass_through), channel=args.channel, horizon=args.horizon,
            price_template=args.price_template, growth_template=args.growth_template,
            top_n=int(args.top), full_cross=bool(args.full_cross), partial=bool(args.partial),
            out_dir=Path(args.out), generated_at=args.generated_at)
    except (ForecastWorldError, MissingInput) as error:
        print(json.dumps({"status": "BLOCKED_INPUT", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    report = outcome["report"]
    summary = {
        "status": report["status"],
        "schema": report["schema"],
        "report": outcome["report_path"],
        "summary": outcome["summary_path"],
        "sha256": outcome["sha256"],
        "bytes": outcome["bytes"],
        "scenarios": {item["id"]: item.get("status") for item in report["scenarios"]},
        "missing_targets": report["checks"]["missing_targets"],
        "checks": {"sfc_pass": report["checks"]["sfc_pass"],
                   "no_raw_data": report["checks"]["no_raw_data"],
                   "fail_closed": report["checks"]["fail_closed"]},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 3 if report["checks"]["missing_targets"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
