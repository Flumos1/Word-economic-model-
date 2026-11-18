#!/usr/bin/env python3
"""Sector exposure: macro shock -> ISIC/NACE/SIC industry -> relative output / cost proxy (task-61).

The tool closes the last mile between the **forecast -> world bridge** (``tools/forecast_world_bridge.py``,
docs/178) and a **sector/customer language**: a user asks "what does a macro shock mean for my
industry?" and receives a transparent table of mapped industries, relative output effects and a
relative cost proxy, plus two conditional company illustrations.

It reads only already published aggregates:

* ``build/forecast_world/<dataset>/forecast_world.json`` -- scenario blocks from the forecast bridge
  (country margins, industry margins, combined relative output and its 5–95 interval);
* ``build/market_bridge/bridge_report.json`` -- the price bridge, used as a **sectoral incidence
  profile** of cost pass-through (per-industry relative price change / aggregate price change);
* the static crosswalk in this file (ISIC Rev.4 / NACE Rev.2 / US SIC -> OECD ICIO 50 / FIGARO 64).

No MRIO matrix, no raw third-party series, no per-sector dataset values are read: only aggregates,
paths and SHA-256. Every report carries ``status = RESEARCH_ONLY`` and ``admission = false``.

Formulas (all declared, none fitted):

* output of a bucket ``b``: ``ΔX_b = Σ_{i in b} ΔX_i`` (sum of the bridge industry margins);
* relative output: ``ΔX_b / ΣX`` (change relative to **world** output, not to the industry's own
  baseline: the bridge aggregates do not publish a per-industry baseline output);
* share of the scenario: ``ΔX_b / ΔX_total`` and ``|ΔX_b| / Σ_i |ΔX_i|``;
* country cells inside a bucket (separable, same device as the bridge):
  ``ΔX[c,b] = ΔX_c · ΔX_b / ΔX_total``;
* cost proxy: ``Δcost_b / cost_b = incidence_b · Δp_scenario``, where
  ``incidence_b = mean_i(Δp_i^PB / Δp^PB_aggregate)`` from the price bridge (missing code -> 1.0,
  flagged), and ``Δp_scenario`` is the scenario's declared relative price shock (1 pp = 1%).

Fail-closed: missing/invalid bridge artefact, an unknown dataset or scenario, a blocked scenario,
a mapping code absent from the bridge margins, a non-research status, an inconsistent margin total
or a raw-data marker all stop the run **before** any file is written (CLI exit code 2).

Determinism: fixed mapping order, sorted keys, deterministic top lists, no wall-clock in the payload
when ``--generated-at`` is supplied; two runs are byte-identical.

Usage
-----
    python tools/sector_exposure.py --dataset oecd_2025 --scenario inflation_plus_1pp \
        --generated-at 2026-11-18T00:00:00Z
    python tools/sector_exposure.py --demo --generated-at 2026-11-18T00:00:00Z
    python -m unittest tests.test_sector_exposure -v
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "amfe.sector_exposure.v1"
STATUS = "RESEARCH_ONLY"
ADMISSION = False
CROSSWALK_VERSION = "1.0.0"

FORECAST_WORLD_SCHEMA = "amfe.forecast_world_bridge.v1"
PRICE_BRIDGE_SCHEMA = "amfe.markets.mrio_bridge.v1"

DEFAULT_PRICE_BRIDGE = ROOT / "build" / "market_bridge" / "bridge_report.json"
DEFAULT_OUT = ROOT / "build" / "sector_exposure"
DATASETS = {
    "oecd_2025": {
        "label": "OECD ICIO 2025, 2019 (81 страна x 50 отраслей)",
        "forecast_world": ROOT / "build" / "forecast_world" / "oecd_2025" / "forecast_world.json",
    },
    "figaro": {
        "label": "FIGARO 2019 (50 стран x 64 отрасли)",
        "forecast_world": ROOT / "build" / "forecast_world" / "figaro" / "forecast_world.json",
    },
}
DEFAULT_SCENARIO = "inflation_plus_1pp"

# ----------------------------------------------------------------------------------- crosswalk
# One row per exposure bucket.  Ranges are declared crosswalk approximations, not a claim that a
# national classifier maps one-to-one; the MRIO columns are the exact codes used in the artefacts.
SECTOR_CROSSWALK = (
    {
        "id": "agriculture_forestry_fishing",
        "name_ru": "Сельское, лесное хозяйство и рыболовство",
        "isic": "A01-A03",
        "nace": "01-03",
        "sic": "0111-0291, 0711-0999",
        "mrio": {"oecd_2025": ("A01", "A02", "A03"),
                 "figaro": ("CPA_A01", "CPA_A02", "CPA_A03")},
        "note": "Секция A ISIC целиком; в SIC — сельское хозяйство, лесоводство, рыболовство.",
    },
    {
        "id": "mining_extraction",
        "name_ru": "Добыча полезных ископаемых (включая нефть и газ)",
        "isic": "B05-B09",
        "nace": "05-09",
        "sic": "1011-1499, 1381-1389",
        "mrio": {"oecd_2025": ("B05", "B06", "B07", "B08", "B09"),
                 "figaro": ("CPA_B",)},
        "note": ("FIGARO объединяет B05–B09 в CPA_B; разбивка по видам добычи в этом наборе "
                 "недоступна."),
    },
    {
        "id": "food_beverage_tobacco",
        "name_ru": "Пищевая промышленность, напитки, табак",
        "isic": "C10-C12",
        "nace": "10-12",
        "sic": "2011-2141",
        "mrio": {"oecd_2025": ("C10T12",), "figaro": ("CPA_C10-12",)},
        "note": "Группа C10–C12 OECD/FIGARO уже агрегирована.",
    },
    {
        "id": "coke_refined_petroleum",
        "name_ru": "Кокс и нефтепродукты",
        "isic": "C19",
        "nace": "19",
        "sic": "2911, 2951-2999",
        "mrio": {"oecd_2025": ("C19",), "figaro": ("CPA_C19",)},
        "note": "Нефтепереработка и коксование.",
    },
    {
        "id": "chemicals_pharma",
        "name_ru": "Химия и фармацевтика",
        "isic": "C20-C21",
        "nace": "20-21",
        "sic": "2812-2899, 2834",
        "mrio": {"oecd_2025": ("C20", "C21"), "figaro": ("CPA_C20", "CPA_C21")},
        "note": "Химические вещества (C20) и фармацевтика (C21) разделены в MRIO.",
    },
    {
        "id": "metals",
        "name_ru": "Металлургия и металлообработка",
        "isic": "C24",
        "nace": "24",
        "sic": "3312-3399",
        "mrio": {"oecd_2025": ("C24A", "C24B"), "figaro": ("CPA_C24",)},
        "note": "OECD делит C24 на C24A/C24B; FIGARO — один CPA_C24.",
    },
    {
        "id": "machinery_equipment",
        "name_ru": "Машины и оборудование",
        "isic": "C28",
        "nace": "28",
        "sic": "3511-3599",
        "mrio": {"oecd_2025": ("C28",), "figaro": ("CPA_C28",)},
        "note": "Машиностроение, кроме электроники и транспорта.",
    },
    {
        "id": "electronics_electrical",
        "name_ru": "Электроника и электрооборудование",
        "isic": "C26-C27",
        "nace": "26-27",
        "sic": "3612-3699, 3812-3829",
        "mrio": {"oecd_2025": ("C26", "C27"), "figaro": ("CPA_C26", "CPA_C27")},
        "note": "Компьютеры/электроника (C26) и электрооборудование (C27).",
    },
    {
        "id": "electricity_gas_water",
        "name_ru": "Электро-, газо- и водоснабжение",
        "isic": "D",
        "nace": "35",
        "sic": "4911-4971",
        "mrio": {"oecd_2025": ("D",), "figaro": ("CPA_D35",)},
        "note": "Секция D ISIC (энергетика и коммунальные услуги).",
    },
    {
        "id": "construction",
        "name_ru": "Строительство",
        "isic": "F",
        "nace": "41-43",
        "sic": "1521-1799",
        "mrio": {"oecd_2025": ("F",), "figaro": ("CPA_F",)},
        "note": "Секция F ISIC.",
    },
    {
        "id": "trade_repair",
        "name_ru": "Оптовая и розничная торговля, ремонт",
        "isic": "G",
        "nace": "45-47",
        "sic": "5013-5999",
        "mrio": {"oecd_2025": ("G",),
                 "figaro": ("CPA_G45", "CPA_G46", "CPA_G47")},
        "note": "FIGARO делит G на G45/G46/G47; OECD — один G.",
    },
    {
        "id": "transport_logistics",
        "name_ru": "Транспорт и логистика",
        "isic": "H49-H53",
        "nace": "49-53",
        "sic": "4011-4789",
        "mrio": {"oecd_2025": ("H49", "H50", "H51", "H52", "H53"),
                 "figaro": ("CPA_H49", "CPA_H50", "CPA_H51", "CPA_H52", "CPA_H53")},
        "note": "Наземный, водный, воздушный, складской транспорт и связь/почта (H53).",
    },
    {
        "id": "finance_insurance",
        "name_ru": "Финансы и страхование",
        "isic": "K",
        "nace": "64-66",
        "sic": "6011-6411",
        "mrio": {"oecd_2025": ("K",),
                 "figaro": ("CPA_K64", "CPA_K65", "CPA_K66")},
        "note": "Финансовые услуги, страхование, вспомогательная деятельность.",
    },
    {
        "id": "professional_services",
        "name_ru": "Профессиональные и деловые услуги",
        "isic": "M",
        "nace": "69-75",
        "sic": "7311-7389, 8111-8999",
        "mrio": {"oecd_2025": ("M",),
                 "figaro": ("CPA_M69_70", "CPA_M71", "CPA_M72", "CPA_M73", "CPA_M74_75")},
        "note": "Юридические, аудиторские, инженерные, рекламные, научные услуги.",
    },
    {
        "id": "public_admin_education_health",
        "name_ru": "Госуправление, образование, здравоохранение",
        "isic": "O-Q",
        "nace": "84-88",
        "sic": "8011-8399, 9111-9799",
        "mrio": {"oecd_2025": ("O", "P", "Q"),
                 "figaro": ("CPA_O84", "CPA_P85", "CPA_Q86", "CPA_Q87_88")},
        "note": "Секции O/P/Q ISIC; в FIGARO — четыре кода.",
    },
)

DEMO_INDUSTRIES = ("mining_extraction", "electricity_gas_water", "trade_repair")

DEFAULT_COMPANIES = (
    {
        "id": "conditional_energy_intensive",
        "label_ru": "Условная компания A — энергоёмкое производство",
        "weights": {"mining_extraction": 0.35, "electricity_gas_water": 0.45,
                    "trade_repair": 0.20},
        "note": ("Веса — условная структура выручки/затрат для демонстрации, не данные "
                 "компании."),
    },
    {
        "id": "conditional_trade_logistics",
        "label_ru": "Условная компания B — торгово-логистическая компания",
        "weights": {"trade_repair": 0.55, "electricity_gas_water": 0.30,
                    "mining_extraction": 0.15},
        "note": ("Веса — условная структура выручки/затрат для демонстрации, не данные "
                 "компании."),
    },
)

COST_MODES = ("price_bridge", "uniform")

# Values that must never appear in the aggregate payload (raw data / raw dataset paths).
FORBIDDEN_KEYS = {"Z", "Z_sparse", "x", "y", "va", "sector_rows", "sector_cols"}
FORBIDDEN_SUBSTRINGS = ("data/mrio", "data\\mrio", "dataset.json", "\\data\\", "F:\\", "F:/")


class SectorExposureError(RuntimeError):
    """The tool refuses to produce a report."""


class MissingInput(SectorExposureError):
    """A required artefact / scenario / mapping entry is absent (fail-closed)."""


class MappingError(SectorExposureError):
    """The crosswalk is inconsistent with itself or with the source margins."""


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
        raise SectorExposureError(
            f"{what}: невалидный JSON ({display_path(path)}): {error}") from error
    if not isinstance(document, dict):
        raise SectorExposureError(f"{what}: ожидался JSON-объект ({display_path(path)})")
    return document


def number(value, *, what: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise SectorExposureError(f"{what}: ожидалось число, получено {value!r}") from error
    if result != result or result in (float("inf"), float("-inf")):
        raise SectorExposureError(f"{what}: не конечное число ({value!r})")
    return result


# -------------------------------------------------------------------------------------- mapping
def crosswalk_entry(bucket_id: str) -> dict:
    for entry in SECTOR_CROSSWALK:
        if entry["id"] == bucket_id:
            return entry
    raise MappingError(f"неизвестная отрасль {bucket_id!r}; известны "
                       f"{[item['id'] for item in SECTOR_CROSSWALK]}")


def code_map(dataset: str) -> dict:
    """MRIO code -> bucket id; refuses duplicate codes and unknown datasets."""
    if dataset not in DATASETS:
        raise SectorExposureError(f"неизвестный набор данных {dataset!r}; известны "
                                 f"{sorted(DATASETS)}")
    mapping: dict = {}
    for entry in SECTOR_CROSSWALK:
        codes = entry["mrio"].get(dataset)
        if not codes:
            raise MappingError(f"у отрасли {entry['id']!r} нет кодов для {dataset!r}")
        for code in codes:
            if code in mapping:
                raise MappingError(f"код {code!r} встречается в двух отраслях: "
                                   f"{mapping[code]!r} и {entry['id']!r}")
            mapping[code] = entry["id"]
    return mapping


def bucket_for_code(code: str, dataset: str) -> str | None:
    return code_map(dataset).get(code)


def mapped_codes(dataset: str) -> tuple:
    return tuple(code_map(dataset))


def mapping_table(dataset: str) -> list:
    """JSON-ready crosswalk with the MRIO codes of the requested dataset."""
    if dataset not in DATASETS:
        raise SectorExposureError(f"неизвестный набор данных {dataset!r}")
    table = []
    for entry in SECTOR_CROSSWALK:
        table.append({
            "id": entry["id"],
            "name_ru": entry["name_ru"],
            "isic": entry["isic"],
            "nace": entry["nace"],
            "sic": entry["sic"],
            "mrio_codes": list(entry["mrio"][dataset]),
            "note": entry["note"],
        })
    return table


# --------------------------------------------------------------------------------------- inputs
def load_forecast_world(path: Path) -> dict:
    document = read_json(path, what="forecast -> world bridge report")
    if document.get("schema") != FORECAST_WORLD_SCHEMA:
        raise SectorExposureError(f"мост прогноз->мир: неожиданная схема "
                                 f"{document.get('schema')!r}")
    if document.get("status") not in ("RESEARCH_ONLY", "PARTIAL_INPUTS"):
        raise SectorExposureError(f"мост прогноз->мир: статус {document.get('status')!r} "
                                 "не является исследовательским")
    if document.get("admission") is not False:
        raise SectorExposureError("мост прогноз->мир: admission должен быть false")
    checks = document.get("checks") or {}
    if not isinstance(checks, dict) or checks.get("no_raw_data") is not True:
        raise SectorExposureError("мост прогноз->мир: no_raw_data != true; источник не чист от "
                                 "сырых данных")
    model = document.get("model") or {}
    if not isinstance(model, dict) or not model.get("dataset"):
        raise SectorExposureError("мост прогноз->мир: нет model.dataset")
    if not isinstance(document.get("scenarios"), list) or not document["scenarios"]:
        raise SectorExposureError("мост прогноз->мир: нет сценариев")
    return {
        "document": document,
        "path": display_path(path),
        "sha256": sha256_file(path),
        "status": document.get("status"),
        "dataset": model.get("dataset"),
        "generated_at": document.get("generated_at"),
    }


def select_scenario(document: dict, scenario_id: str) -> dict:
    scenarios = document.get("scenarios") or []
    found = next((item for item in scenarios if item.get("id") == scenario_id), None)
    if found is None:
        raise MissingInput(f"сценарий {scenario_id!r} отсутствует; в источнике "
                           f"{[item.get('id') for item in scenarios]}")
    if found.get("status") != "OK":
        reason = found.get("reason") or found.get("status")
        raise MissingInput(f"сценарий {scenario_id!r} заблокирован: {reason}")

    combined = found.get("combined") or {}
    base_output = number(combined.get("base_output_usd_mn"),
                         what=f"{scenario_id}: combined.base_output_usd_mn")
    if base_output <= 0.0:
        raise SectorExposureError(f"{scenario_id}: неположительный базовый выпуск")
    delta = number(combined.get("delta_output_usd_mn"),
                   what=f"{scenario_id}: combined.delta_output_usd_mn")
    relative = number(combined.get("relative_output"),
                      what=f"{scenario_id}: combined.relative_output")
    if abs(delta / base_output - relative) > 1e-9:
        raise SectorExposureError(
            f"{scenario_id}: combined.relative_output не согласован с Δ/базой "
            f"({relative!r} vs {delta / base_output!r})")
    interval = combined.get("interval") or {}
    low = number(interval.get("delta_low"), what=f"{scenario_id}: interval.delta_low")
    high = number(interval.get("delta_high"), what=f"{scenario_id}: interval.delta_high")
    if high < low:
        raise SectorExposureError(f"{scenario_id}: интервал перевёрнут ({low!r} > {high!r})")

    industries = found.get("industries") or []
    countries = found.get("countries") or []
    industry_map: dict = {}
    for item in industries:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise SectorExposureError(f"{scenario_id}: отраслевой маргинал без имени")
        if name in industry_map:
            raise SectorExposureError(f"{scenario_id}: дубль отрасли {name!r}")
        industry_map[name] = number(item.get("delta_output"),
                                    what=f"{scenario_id}: industry {name}.delta_output")
    country_map: dict = {}
    for item in countries:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise SectorExposureError(f"{scenario_id}: страновой маргинал без имени")
        if name in country_map:
            raise SectorExposureError(f"{scenario_id}: дубль страны {name!r}")
        country_map[name] = number(item.get("delta_output"),
                                   what=f"{scenario_id}: country {name}.delta_output")

    if delta != 0.0:
        if not industry_map:
            raise SectorExposureError(f"{scenario_id}: нет отраслевых маргиналов при ненулевом "
                                     "шуме")
        if not country_map:
            raise SectorExposureError(f"{scenario_id}: нет страновых маргиналов при ненулевом "
                                     "шуме")
        tolerance = 1e-6 * max(len(industry_map), abs(delta))
        industry_total = sum(industry_map.values())
        if abs(industry_total - delta) > tolerance:
            raise SectorExposureError(
                f"{scenario_id}: отраслевой маргинал не сходится с агрегатом "
                f"({industry_total!r} vs {delta!r})")
        tolerance = 1e-6 * max(len(country_map), abs(delta))
        country_total = sum(country_map.values())
        if abs(country_total - delta) > tolerance:
            raise SectorExposureError(
                f"{scenario_id}: страновой маргинал не сходится с агрегатом "
                f"({country_total!r} vs {delta!r})")
    elif any(abs(value) > 1e-12 for value in industry_map.values()) or any(
            abs(value) > 1e-12 for value in country_map.values()):
        raise SectorExposureError(f"{scenario_id}: нулевой агрегат при ненулевых маргиналах")

    return {
        "id": scenario_id,
        "label": found.get("label") or scenario_id,
        "shifts": found.get("shifts") or {},
        "base_output_usd_mn": base_output,
        "delta_output_usd_mn": delta,
        "relative_output": relative,
        "interval": {"delta_low": low, "delta_high": high},
        "price_channel": found.get("price_channel") or {},
        "growth_channel": found.get("growth_channel") or {},
        "industry_map": industry_map,
        "country_map": country_map,
    }


def pick_variant(variants: dict, pass_through: float) -> tuple:
    if not isinstance(variants, dict) or not variants:
        raise MissingInput("price bridge: нет вариантов pass-through")
    target = f"{float(pass_through):g}"
    if target in variants:
        return target, variants[target]
    closest = min(variants, key=lambda item: abs(float(item) - float(pass_through)))
    return closest, variants[closest]


def load_price_bridge(path: Path, dataset: str, pass_through: float) -> dict:
    document = read_json(path, what="price bridge report")
    if document.get("schema") != PRICE_BRIDGE_SCHEMA:
        raise SectorExposureError(f"price bridge: неожиданная схема "
                                 f"{document.get('schema')!r}")
    if document.get("status") != STATUS:
        raise SectorExposureError(f"price bridge: статус {document.get('status')!r} "
                                 "не RESEARCH_ONLY")
    if document.get("admission") is not False:
        raise SectorExposureError("price bridge: admission должен быть false")
    results = document.get("price_results") or {}
    entry = results.get(dataset)
    if not isinstance(entry, dict):
        raise MissingInput(f"price bridge: нет набора {dataset!r}; есть {sorted(results)}")
    if entry.get("status") != "OK":
        raise MissingInput(f"price bridge: набор {dataset!r} в статусе {entry.get('status')!r}")
    variant_key, variant = pick_variant(entry.get("variants") or {}, pass_through)
    if not isinstance(variant, dict):
        raise SectorExposureError(f"price bridge: вариант {variant_key!r} — не объект")
    result = variant.get("results") or {}
    if not isinstance(result, dict):
        raise SectorExposureError("price bridge: results — не объект")
    aggregate_pct = number(result.get("aggregate_price_index_pct"),
                           what="price bridge: aggregate_price_index_pct")
    if aggregate_pct <= 0.0:
        raise SectorExposureError("price bridge: неположительный агрегатный ценовой индекс")
    aggregate_relative = aggregate_pct / 100.0
    incidence: dict = {}
    for item in result.get("top_industries") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise SectorExposureError("price bridge: top_industries без имени отрасли")
        value = number(item.get("delta_price_relative"),
                       what=f"price bridge: {name}.delta_price_relative")
        if value < 0.0:
            raise SectorExposureError(f"price bridge: отрицательное изменение цены {name!r}")
        incidence[name] = value / aggregate_relative
    return {
        "path": display_path(path),
        "sha256": sha256_file(path),
        "dataset": dataset,
        "variant": variant_key,
        "pass_through": number(variant.get("pass_through", pass_through),
                               what="price bridge: pass_through"),
        "aggregate_price_index_pct": aggregate_pct,
        "aggregate_relative": aggregate_relative,
        "incidence": incidence,
        "top_industries": len(incidence),
    }


# ----------------------------------------------------------------------------------- exposure
def cost_profile(codes: tuple, price: dict, scenario: dict, cost_mode: str) -> dict:
    price_channel = scenario.get("price_channel") or {}
    if price_channel.get("status") != "OK":
        return {
            "status": "NOT_APPLIED",
            "reason": "сценарий не содержит ценового шока (price channel NOT_APPLIED)",
            "incidence": None,
            "relative_change": None,
            "interval": None,
            "matched_codes": [],
            "fallback_codes": [],
            "formula": "Δcost/cost = incidence · Δp; ценовой шок в сценарии отсутствует",
        }
    shift = number(price_channel.get("relative_price_shock"),
                   what=f"{scenario['id']}: relative_price_shock")
    if shift == 0.0:
        return {
            "status": "NOT_APPLIED",
            "reason": "нулевой ценовой шок",
            "incidence": None,
            "relative_change": None,
            "interval": None,
            "matched_codes": [],
            "fallback_codes": [],
            "formula": "Δcost/cost = incidence · Δp; Δp = 0",
        }
    band = price_channel.get("relative_price_shock_interval") or [shift, shift]
    if not isinstance(band, list) or len(band) != 2:
        raise SectorExposureError(f"{scenario['id']}: ценовой интервал должен быть парой")
    band_low = number(band[0], what="price channel interval low")
    band_high = number(band[1], what="price channel interval high")
    if band_high < band_low:
        raise SectorExposureError("ценовой интервал перевёрнут")

    matched: list = []
    fallback: list = []
    values: list = []
    if cost_mode == "uniform":
        values = [1.0 for _ in codes]
        incidence = 1.0
        status = "UNIFORM"
        basis = "полный перенос ценового шока во все отрасли (допущение, без профиля)"
    elif cost_mode == "price_bridge":
        for code in codes:
            if code in price["incidence"]:
                matched.append(code)
                values.append(price["incidence"][code])
            else:
                fallback.append(code)
                values.append(1.0)
        incidence = sum(values) / len(values)
        if not fallback:
            status = "OK"
            basis = "профиль инцидентности price bridge (все коды найдены)"
        elif matched:
            status = "PARTIAL_FALLBACK"
            basis = ("профиль price bridge; для кодов вне top-10 взята агрегатная "
                     "инцидентность 1.0")
        else:
            status = "AGGREGATE_FALLBACK"
            basis = "агрегатная инцидентность 1.0: ни один код не найден в price bridge"
    else:
        raise SectorExposureError(f"неизвестный режим затрат {cost_mode!r}; известны "
                                 f"{COST_MODES}")

    return {
        "status": status,
        "basis": basis,
        "incidence": round(incidence, 9),
        "relative_change": round(incidence * shift, 12),
        "interval": [round(incidence * band_low, 12), round(incidence * band_high, 12)],
        "matched_codes": matched,
        "fallback_codes": fallback,
        "formula": ("Δcost/cost = incidence · Δp; incidence = mean_i(Δp_i^price_bridge / "
                    "Δp^price_bridge_aggregate), missing code -> 1.0"),
    }


def output_profile(entry: dict, scenario: dict, dataset: str, top_n: int) -> dict:
    codes = tuple(entry["mrio"][dataset])
    industry_map = scenario["industry_map"]
    total = scenario["delta_output_usd_mn"]
    if total == 0.0:
        # A genuine zero-shock scenario (e.g. the bridge ``base`` block) carries no margins:
        # every bucket is zero by definition, and the cost side is NOT_APPLIED.
        delta = 0.0
        missing = []
    else:
        missing = [code for code in codes if code not in industry_map]
        if missing:
            raise MappingError(f"отрасль {entry['id']!r}: коды {missing} отсутствуют в "
                               f"отраслевом маргинале сценария {scenario['id']!r}")
        delta = sum(industry_map[code] for code in codes)
    base = scenario["base_output_usd_mn"]
    relative = delta / base
    share = delta / total if total else 0.0
    abs_total = sum(abs(value) for value in industry_map.values())
    share_abs = abs(delta) / abs_total if abs_total else 0.0
    factor = delta / total if total else 0.0
    interval_low = factor * scenario["interval"]["delta_low"]
    interval_high = factor * scenario["interval"]["delta_high"]

    cells = []
    if total and delta:
        for country, country_delta in scenario["country_map"].items():
            value = delta * country_delta / total
            cells.append({"country": country, "delta_output_usd_mn": value,
                          "share_of_bucket": value / delta if delta else 0.0})
        cells.sort(key=lambda item: (-abs(item["delta_output_usd_mn"]), item["country"]))
    top_countries = [{
        "country": item["country"],
        "delta_output_usd_mn": round(item["delta_output_usd_mn"], 6),
        "share_of_bucket": round(item["share_of_bucket"], 9),
    } for item in cells[:top_n]]

    return {
        "delta_output_usd_mn": round(delta, 6),
        "relative_to_world_output": round(relative, 12),
        "share_of_scenario_change": round(share, 12) if total else 0.0,
        "share_of_abs_scenario_change": round(share_abs, 12),
        "interval_usd_mn": [round(min(interval_low, interval_high), 6),
                            round(max(interval_low, interval_high), 6)],
        "top_countries": top_countries,
    }


def company_profile(company: dict, buckets: dict) -> dict:
    weights = company.get("weights") or {}
    if not isinstance(weights, dict) or not weights:
        raise MappingError(f"компания {company.get('id')!r}: нет весов")
    total_weight = 0.0
    for bucket_id, weight in weights.items():
        if bucket_id not in buckets:
            raise MappingError(f"компания {company.get('id')!r}: отрасль {bucket_id!r} "
                               "отсутствует в отчёте (demo/фокус не совпадает)")
        total_weight += number(weight, what=f"компания {company['id']}: вес {bucket_id}")
    if abs(total_weight - 1.0) > 1e-9:
        raise MappingError(f"компания {company.get('id')!r}: сумма весов {total_weight!r} != 1")
    weighted_output = 0.0
    weighted_share = 0.0
    weighted_cost = 0.0
    weighted_delta = 0.0
    cost_available = True
    contributions = []
    for bucket_id, weight in weights.items():
        bucket = buckets[bucket_id]
        weight = float(weight)
        weighted_output += weight * bucket["relative_to_world_output"]
        weighted_share += weight * bucket["share_of_scenario_change"]
        weighted_delta += weight * bucket["delta_output_usd_mn"]
        contributions.append((abs(weight * bucket["delta_output_usd_mn"]), bucket_id))
        if bucket["cost"]["relative_change"] is None:
            cost_available = False
        else:
            weighted_cost += weight * bucket["cost"]["relative_change"]
    contributions.sort(key=lambda item: (-item[0], item[1]))
    return {
        "id": company["id"],
        "label_ru": company["label_ru"],
        "status": "CONDITIONAL",
        "weights": {key: float(value) for key, value in weights.items()},
        "weight_sum": round(total_weight, 12),
        "weighted_output_relative_to_world_output": round(weighted_output, 12),
        "weighted_share_of_scenario_change": round(weighted_share, 12),
        "weighted_delta_output_usd_mn": round(weighted_delta, 6),
        "weighted_cost_relative": round(weighted_cost, 12) if cost_available else None,
        "dominant_bucket": contributions[0][1],
        "note": company.get("note") or "",
    }


# --------------------------------------------------------------------------------------- checks
def scan_for_raw(payload) -> list:
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


# ---------------------------------------------------------------------------------------- report
def build_report(*, forecast_world: Path, price_bridge: Path, dataset: str = "oecd_2025",
                 scenario_id: str = DEFAULT_SCENARIO, pass_through: float = 0.6,
                 cost_mode: str = "price_bridge", top_n: int = 5, demo: bool = False,
                 out_dir: Path | None = None, generated_at: str | None = None) -> dict:
    if dataset not in DATASETS:
        raise SectorExposureError(f"неизвестный набор данных {dataset!r}; известны "
                                 f"{sorted(DATASETS)}")
    if not 0.0 <= float(pass_through) <= 1.0:
        raise SectorExposureError("pass-through должен быть в [0, 1]")
    if int(top_n) <= 0:
        raise SectorExposureError("--top должен быть положительным")
    if cost_mode not in COST_MODES:
        raise SectorExposureError(f"неизвестный режим затрат {cost_mode!r}")

    source = load_forecast_world(Path(forecast_world))
    if source["dataset"] != dataset:
        raise SectorExposureError(
            f"набор {dataset!r} не совпадает с model.dataset в мосте ({source['dataset']!r})")
    scenario = select_scenario(source["document"], scenario_id)
    price = load_price_bridge(Path(price_bridge), dataset, float(pass_through))

    mapping = code_map(dataset)
    valid_ids = {entry["id"] for entry in SECTOR_CROSSWALK}
    focus_ids = DEMO_INDUSTRIES if demo else tuple(entry["id"] for entry in SECTOR_CROSSWALK)
    for bucket_id in focus_ids:
        if bucket_id not in valid_ids:
            raise MappingError(f"неизвестная отрасль {bucket_id!r}")

    buckets: dict = {}
    for entry in SECTOR_CROSSWALK:
        if entry["id"] not in focus_ids:
            continue
        profile = output_profile(entry, scenario, dataset, int(top_n))
        profile["id"] = entry["id"]
        profile["name_ru"] = entry["name_ru"]
        profile["isic"] = entry["isic"]
        profile["nace"] = entry["nace"]
        profile["sic"] = entry["sic"]
        profile["mrio_codes"] = list(entry["mrio"][dataset])
        profile["cost"] = cost_profile(tuple(entry["mrio"][dataset]), price, scenario, cost_mode)
        profile["note"] = entry["note"]
        buckets[entry["id"]] = profile

    companies = []
    for company in DEFAULT_COMPANIES:
        companies.append(company_profile(company, buckets))

    source_codes = set(scenario["industry_map"])
    mapped_source = source_codes & set(mapping)
    unmapped_codes = sorted(source_codes - set(mapping))
    focus_codes = {code for bucket_id in focus_ids
                   for code in crosswalk_entry(bucket_id)["mrio"][dataset]}
    focus_margin = sum(scenario["industry_map"][code] for code in focus_codes
                       if code in scenario["industry_map"])
    bucket_delta = sum(item["delta_output_usd_mn"] for item in buckets.values())
    bucket_check = abs(bucket_delta - focus_margin) <= 1e-6 * max(1.0, abs(focus_margin))

    coverage = (len(mapped_source) / len(source_codes)) if source_codes else 0.0
    report = {
        "schema": SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "generated_at": generated_at or now(),
        "statement": (
            "Отраслевая экспозиция макро-шока: агрегаты моста «прогноз → мир» перекладываются "
            "в язык отраслей ISIC/NACE/SIC и в условные компании. Это исследовательская "
            "сравнительная статика с объявленными допущениями, НЕ прогноз, НЕ допуск и НЕ "
            "инвестиционная рекомендация."
        ),
        "dataset": dataset,
        "dataset_label": DATASETS[dataset]["label"],
        "mode": "demo" if demo else "full",
        "scenario": {
            "id": scenario["id"],
            "label": scenario["label"],
            "shifts": scenario["shifts"],
            "base_output_usd_mn": scenario["base_output_usd_mn"],
            "delta_output_usd_mn": scenario["delta_output_usd_mn"],
            "relative_output": scenario["relative_output"],
            "interval_usd_mn": [scenario["interval"]["delta_low"],
                                scenario["interval"]["delta_high"]],
            "price_channel": {
                "status": scenario["price_channel"].get("status"),
                "relative_price_shock": scenario["price_channel"].get("relative_price_shock"),
                "relative_price_shock_interval":
                    scenario["price_channel"].get("relative_price_shock_interval"),
            },
            "growth_channel": {
                "status": scenario["growth_channel"].get("status"),
                "relative_demand_shock":
                    scenario["growth_channel"].get("relative_demand_shock"),
            },
        },
        "inputs": {
            "forecast_world": {
                "path": source["path"],
                "sha256": source["sha256"],
                "status": source["status"],
                "generated_at": source["generated_at"],
            },
            "price_bridge": {
                "path": price["path"],
                "sha256": price["sha256"],
                "dataset": price["dataset"],
                "variant": price["variant"],
                "pass_through": price["pass_through"],
                "aggregate_price_index_pct": price["aggregate_price_index_pct"],
                "top_industries": price["top_industries"],
            },
            "mapping_version": CROSSWALK_VERSION,
            "cost_mode": cost_mode,
        },
        "mapping": {
            "version": CROSSWALK_VERSION,
            "classification": "ISIC Rev.4 / NACE Rev.2 / US SIC (1987) -> MRIO",
            "industries": len(SECTOR_CROSSWALK),
            "coverage": {
                "source_codes": len(source_codes),
                "mapped_codes": len(mapped_source),
                "mapped_share": round(coverage, 9),
                "unmapped_codes": unmapped_codes,
            },
            "table": mapping_table(dataset),
        },
        "industries": list(buckets.values()),
        "companies": companies,
        "checks": {
            "mode": "demo" if demo else "full",
            "scenario_ok": True,
            "industry_margins_match_total": True,
            "country_margins_match_total": True,
            "relative_matches_combined": True,
            "bucket_deltas_match_margins": bucket_check,
            "focus_industries": list(focus_ids),
            "cost_mode": cost_mode,
            "cost_fallback_codes": sorted({code for item in buckets.values()
                                           for code in item["cost"]["fallback_codes"]}),
            "no_raw_data": True,
            "research_only": True,
            "admission": False,
            "deterministic": True,
            "fail_closed": True,
        },
        "limitations": [
            "только агрегаты: сырые MRIO-матрицы, per-sector значения и сторонние ряды не "
            "читаются и не публикуются;",
            "относительный эффект отрасли = ΔX_b / ΣX (к мировому выпуску), а не к выпуску "
            "самой отрасли: базового выпуска отрасли в агрегатах моста нет;",
            "страны внутри отрасли распределены сепарабельно ΔX[c,b] = Δ_c · Δ_b / Δ_total — "
            "это устройство распределения, а не модель ячейки;",
            "ценовая сторона использует профиль инцидентности price bridge (нефтяной шок) и "
            "масштабируется объявленным ценовым шоком сценария; CPI→PPI не моделируется, для "
            "кодов вне top-10 принимается агрегатная инцидентность 1.0;",
            "сравнительная статика: нет замещения, динамики, обратных связей и поведения;",
            "условные компании — иллюстрация с объявленными весами, не P&L и не данные "
            "компании;",
            "не допуск: admission=false, статус RESEARCH_ONLY; заявлений о точности нет.",
        ],
    }

    problems = scan_for_raw(report)
    if problems:
        raise SectorExposureError("в отчёте обнаружены сырые маркеры: "
                                  + "; ".join(problems[:5]))
    report["checks"]["no_raw_data"] = not problems

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "sector_exposure.json"
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        # Canonical write (task-69): Path.write_text() translates "\n" to CRLF on Windows, so the
        # file digest stopped matching the digest of the in-memory payload.  Bytes are written
        # as-is, in UTF-8, with LF line endings on every platform.
        report_path.write_bytes(payload.encode("utf-8"))
        summary_path = out_dir / "sector_exposure.md"
        summary_path.write_bytes(render_markdown(report).encode("utf-8"))
        return {
            "report": report,
            "report_path": str(report_path),
            "summary_path": str(summary_path),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": report_path.stat().st_size,
        }
    return {"report": report, "report_path": None, "summary_path": None,
            "sha256": None, "bytes": None}


# -------------------------------------------------------------------------------------- markdown
def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def _pct(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    return f"{100 * value:+.{digits}f}%"


def render_markdown(report: dict) -> str:
    scenario = report["scenario"]
    lines = [
        "# Отраслевая экспозиция макро-шока (task-61)",
        "",
        f"Статус: **{report['status']}**, admission=false, дата {report['generated_at']}.",
        "",
        "> Сравнительная статика на агрегатах моста «прогноз → мир» и price bridge: "
        "не прогноз, не допуск, не инвестиционная рекомендация.",
        "",
        f"Набор: **{report['dataset_label']}**; сценарий: **{scenario['label']}** "
        f"(`{scenario['id']}`); режим: `{report['mode']}`.",
        "",
        "## Сценарий на входе",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Базовый выпуск, USD млн | {_fmt(scenario['base_output_usd_mn'])} |",
        f"| Δ выпуска, USD млн | {_fmt(scenario['delta_output_usd_mn'])} |",
        f"| Δ выпуска, % мира | {_pct(scenario['relative_output'])} |",
        f"| Интервал 5–95, USD млн | {_fmt(scenario['interval_usd_mn'][0])} … "
        f"{_fmt(scenario['interval_usd_mn'][1])} |",
        f"| Ценовой шок | {_pct(scenario['price_channel']['relative_price_shock'])} |",
        "",
        "## Отрасли",
        "",
        "| Отрасль | MRIO | Δ выпуска, USD млн | Δ / мир | Доля шока (abs) | Δ затрат |",
        "|---|---|---|---|---|---|",
    ]
    for item in report["industries"]:
        cost = item["cost"]
        lines.append(
            f"| {item['name_ru']} | {', '.join(item['mrio_codes'])} | "
            f"{_fmt(item['delta_output_usd_mn'])} | {_pct(item['relative_to_world_output'], 6)} | "
            f"{100 * item['share_of_abs_scenario_change']:.2f}% | "
            f"{_pct(cost['relative_change']) if cost['relative_change'] is not None else '—'} |")
    lines.append("")
    lines += ["### Топ стран по отраслям", "",
              "| Отрасль | Страна | Δ, USD млн | Доля отрасли |", "|---|---|---|---|"]
    for item in report["industries"]:
        for country in item["top_countries"][:3]:
            lines.append(f"| {item['name_ru']} | {country['country']} | "
                         f"{_fmt(country['delta_output_usd_mn'])} | "
                         f"{_pct(country['share_of_bucket'], 2)} |")
    lines += ["", "## Условные компании", "",
              "| Компания | Веса | Δ / мир | Доля шока (abs) | Δ затрат | Ведущая отрасль |",
              "|---|---|---|---|---|---|"]
    for company in report["companies"]:
        weights = ", ".join(f"{key} {value:g}" for key, value in company["weights"].items())
        cost = (f"{100 * company['weighted_cost_relative']:+.4f}%"
                if company["weighted_cost_relative"] is not None else "—")
        lines.append(f"| {company['label_ru']} | {weights} | "
                     f"{_pct(company['weighted_output_relative_to_world_output'], 6)} | "
                     f"{100 * abs(company['weighted_share_of_scenario_change']):.2f}% | {cost} | "
                     f"{company['dominant_bucket']} |")
    lines += ["", "## Маппинг ISIC / NACE / SIC → MRIO", "",
              "| id | Отрасль | ISIC | NACE | SIC | MRIO |", "|---|---|---|---|---|---|"]
    for item in report["mapping"]["table"]:
        lines.append(f"| `{item['id']}` | {item['name_ru']} | {item['isic']} | "
                     f"{item['nace']} | {item['sic']} | {', '.join(item['mrio_codes'])} |")
    coverage = report["mapping"]["coverage"]
    lines += ["", f"Покрытие: {coverage['mapped_codes']} из {coverage['source_codes']} кодов "
                  f"источника ({100 * coverage['mapped_share']:.1f}%); непокрытые коды: "
                  f"{', '.join(coverage['unmapped_codes']) or '—'}.", "",
              "## Проверки", "",
              f"- сценарий: `{scenario['id']}`, режим затрат: `{report['inputs']['cost_mode']}`;",
              f"- баланс отраслей: {'PASS' if report['checks']['bucket_deltas_match_margins'] else 'FAIL'}; "
              f"no_raw_data: {report['checks']['no_raw_data']}; "
              f"fail_closed: {report['checks']['fail_closed']};",
              f"- коды без профиля price bridge: "
              f"{', '.join(report['checks']['cost_fallback_codes']) or '—'}.", "",
              "## Ограничения", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", "## Команды", "",
              "```powershell",
              "python tools/sector_exposure.py --dataset oecd_2025 "
              "--scenario inflation_plus_1pp --generated-at 2026-11-18T00:00:00Z",
              "python tools/sector_exposure.py --demo --generated-at 2026-11-18T00:00:00Z",
              "python -m unittest tests.test_sector_exposure -v",
              "```", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Macro shock -> mapped industries -> relative output / cost proxy")
    parser.add_argument("--forecast-world", default=None,
                        help="forecast_world.json (по умолчанию build/forecast_world/<dataset>/)")
    parser.add_argument("--price-bridge", default=str(DEFAULT_PRICE_BRIDGE))
    parser.add_argument("--dataset", default="oecd_2025", choices=tuple(DATASETS))
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--pass-through", type=float, default=0.6)
    parser.add_argument("--cost-mode", default="price_bridge", choices=COST_MODES)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    forecast_world = (Path(args.forecast_world) if args.forecast_world
                      else DATASETS[args.dataset]["forecast_world"])
    out_dir = (Path(args.out) if args.out
               else DEFAULT_OUT / f"{args.dataset}_{args.scenario}"
               / ("demo" if args.demo else "full"))
    try:
        outcome = build_report(
            forecast_world=forecast_world, price_bridge=Path(args.price_bridge),
            dataset=args.dataset, scenario_id=args.scenario,
            pass_through=float(args.pass_through), cost_mode=args.cost_mode,
            top_n=int(args.top), demo=bool(args.demo), out_dir=out_dir,
            generated_at=args.generated_at)
    except SectorExposureError as error:
        print(json.dumps({"status": "BLOCKED_INPUT", "error": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report = outcome["report"]
    summary = {
        "status": report["status"],
        "schema": report["schema"],
        "mode": report["mode"],
        "report": outcome["report_path"],
        "summary": outcome["summary_path"],
        "sha256": outcome["sha256"],
        "bytes": outcome["bytes"],
        "dataset": report["dataset"],
        "scenario": report["scenario"]["id"],
        "industries": len(report["industries"]),
        "companies": len(report["companies"]),
        "checks": {"bucket_deltas_match_margins":
                   report["checks"]["bucket_deltas_match_margins"],
                   "no_raw_data": report["checks"]["no_raw_data"],
                   "fail_closed": report["checks"]["fail_closed"]},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
