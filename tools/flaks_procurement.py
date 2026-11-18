#!/usr/bin/env python3
"""FLAKS procurement intelligence MVP: supplier scoring, TCO, budget and contract indexation.

Task ``task-66`` (UC-13/14/16, docs/174). The tool answers three questions of a CFO/procurement
lead on a **demo (synthetic)** supplier set:

* UC-13 -- which supplier is better: transparent multi-criteria score (price, TCO, risk, quality,
  delivery) with declared weights and an explicit contribution of every criterion;
* UC-14 -- what the procurement budget will be: base spend re-priced through declared macro
  scenarios (CPI from the product feed, oil from the price bridge, freight and FX);
* UC-16 -- how much contract indexation is due: per-contract clause (CPI / PPI / FX shares,
  cap, zero floor) evaluated on the same scenario drivers.

Model (all coefficients declared, nothing fitted):

* ``TCO/unit = price + freight + duty + risk + quality + late + carrying`` with
  ``duty = price * country duty``, ``risk = price * composite_risk * RISK_COST_RATE``,
  ``quality = price * defect_ppm/1e6 * REWORK_MULTIPLIER``,
  ``late = price * (1 - otif) * LATE_PENALTY_RATE``,
  ``carrying = price * lead_days/365 * CARRYING_RATE``;
* ``composite_risk = 0.45*country + 0.30*financial + 0.15*supply + 0.10*compliance``;
* score components are **best-in-class ratios** inside each category (``best/own`` for
  lower-is-better, ``own/best`` for higher-is-better), quality is a declared blend
  ``0.5*min(defect)/defect + 0.3*otif/max(otif) + 0.2*audit/max(audit)``;
* ``score = Σ w_k · component_k`` with declared default weights (price .20, TCO .30, risk .20,
  quality .20, delivery .10); the report publishes every ``w_k · component_k`` so the score is
  reproducible by hand;
* scenario drivers: ``cpi_pp`` (feed anchor + explicit shift + declared oil sensitivity),
  ``oil_price_pct = oil_delta_pct * OIL_PRICE_ELASTICITY_PCT_PER_100/100`` (linearised price
  bridge aggregate), ``freight_price_pct = freight_delta_pct * FREIGHT_TO_PRICE_ELASTICITY``;
* category price index: ``Σ incidence_k · driver_k`` (CPI, oil, freight incidences per category);
* contract indexation: ``raw = cpi_share*cpi + ppi_share*(oil_inc + freight_inc) + fx_share*fx``,
  then ``applied = clamp(raw, floor 0 if declared, cap)`` and
  ``forecast = base·(1+applied) · (1+volume_delta)``.

Data policy: **no real data is read or committed**. The built-in demo set is synthetic
(``source_class = "synthetic_demo"``); a user file must declare the same class, otherwise the run
is refused. Only aggregates of already published artefacts are used as anchors
(``build/product/forecast_latest.json`` point/interval of T1, ``build/market_bridge/bridge_report.json``
market shock and aggregate price index) and only when ``--verify-anchors`` is requested.

Fail-closed: an unknown scenario, a broken/unexpected schema, a missing field, a non-positive
price/volume/lead time, a duplicated supplier id, an unknown country or category, a category with
fewer than two suppliers, indexation shares summing above 1, weights not summing to 1, or a
real-data marker all stop the run **before** any file is written (CLI exit code 2, JSON error on
stderr).

Determinism: category order, ranking (score desc, then id), sorted keys, fixed rounding; with
``--generated-at`` the payload carries no wall-clock and two runs are byte-identical.

Research-only: every artefact carries ``status = RESEARCH_ONLY`` and ``admission = false``.

Usage
-----
    python tools/flaks_procurement.py --scenario oil_plus_20 --report build/flaks_procurement
    python tools/flaks_procurement.py --suppliers demo --scenario base --generated-at 2026-11-18T00:00:00Z
    python tools/flaks_procurement.py --dump-suppliers > demo_suppliers.json
    python tools/flaks_procurement.py --verify-anchors --scenario hormuz_6m
    python -m unittest tests.test_flaks_procurement -v
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "amfe.flaks.procurement.v1"
DATASET_SCHEMA = "amfe.flaks.procurement_demo.v1"
MACRO_SCHEMA = "amfe.flaks.macro_inputs.v1"
STATUS = "RESEARCH_ONLY"
ADMISSION = False
MODEL_VERSION = "1.0.0"

DEFAULT_OUT = ROOT / "build" / "flaks_procurement"
DEFAULT_FEED = ROOT / "build" / "product" / "forecast_latest.json"
DEFAULT_PRICE_BRIDGE = ROOT / "build" / "market_bridge" / "bridge_report.json"

WEIGHT_KEYS = ("price", "tco", "risk", "quality", "delivery")
DEFAULT_WEIGHTS = {"price": 0.20, "tco": 0.30, "risk": 0.20, "quality": 0.20, "delivery": 0.10}
WEIGHT_RATIONALE = {
    "price": "цена за единицу: то, что видно в счёте, и стартовая точка переговоров",
    "tco": "полная стоимость владения: логистика, пошлина, риск, качество, замороженный капитал",
    "risk": "риск поставщика и страны: перебои, комплаенс, финансовая устойчивость",
    "quality": "качество: дефекты (ppm), OTIF и результат аудита",
    "delivery": "срок поставки: длинный лид-тайм связывает оборотный капитал и снижает гибкость",
}
NORMALIZATION = {
    "price": "best/own, best = минимальная цена за единицу в категории",
    "tco": "best/own, best = минимальный TCO за единицу в категории",
    "risk": "(1 − composite_risk) / max(1 − composite_risk) в категории",
    "quality": "0.5·min(defect)/defect + 0.3·otif/max(otif) + 0.2·audit/max(audit)",
    "delivery": "min(lead_time)/lead_time",
}
RISK_WEIGHTS = {"country": 0.45, "financial": 0.30, "supply": 0.15, "compliance": 0.10}
QUALITY_WEIGHTS = {"defect": 0.5, "otif": 0.3, "audit": 0.2}
TCO_KEYS = ("price", "logistics", "duty", "risk", "quality", "late", "carrying")

RISK_COST_RATE = 0.12
REWORK_MULTIPLIER = 3.0
LATE_PENALTY_RATE = 0.35
CARRYING_RATE = 0.09

OIL_TO_CPI_PP_PER_PCT = 0.015
OIL_PRICE_ELASTICITY_PCT_PER_100 = 2.364
FREIGHT_TO_PRICE_ELASTICITY = 0.35

BANDS = (("A", 0.92, "предпочтительный"), ("B", 0.82, "допустимый"), ("C", 0.0, "рискованный"))
MIN_SUPPLIERS_PER_CATEGORY = 2
MIN_ACTIVE_PER_CATEGORY = 1

SOURCE_CLASSES = ("synthetic_demo", "anonymized_fixture")
BASE_CURRENCY = "EUR"
CURRENCIES = ("EUR", "USD", "CNY")
CONTRACT_STATUSES = ("active", "candidate")

FORBIDDEN_KEYS = {"Z", "Z_sparse", "x", "y", "va", "sector_rows", "sector_cols"}
FORBIDDEN_SUBSTRINGS = ("data/mrio", "data\\mrio", "dataset.json", "\\data\\", "F:\\", "F:/")

CATEGORIES = (
    {
        "id": "metal_rolled",
        "name_ru": "Металлопрокат (плоский прокат)",
        "unit": "кг",
        "cpi_incidence": 0.35,
        "oil_incidence": 1.55,
        "freight_incidence": 0.12,
        "note": "энергоёмкое производство; индекс цен металлов обгоняет CPI",
    },
    {
        "id": "polymer_granulate",
        "name_ru": "Полимерное сырьё (гранулят)",
        "unit": "кг",
        "cpi_incidence": 0.25,
        "oil_incidence": 2.10,
        "freight_incidence": 0.10,
        "note": "нефтехимия: цена почти целиком следует за нефтью",
    },
    {
        "id": "packaging_film",
        "name_ru": "Упаковочная плёнка",
        "unit": "кг",
        "cpi_incidence": 0.30,
        "oil_incidence": 1.80,
        "freight_incidence": 0.08,
        "note": "полимерная основа плюс переработка; высокая доля нефтяной компоненты",
    },
    {
        "id": "contract_logistics",
        "name_ru": "Контрактная логистика (фрахт)",
        "unit": "отправка",
        "cpi_incidence": 0.45,
        "oil_incidence": 1.20,
        "freight_incidence": 1.00,
        "note": "топливная составляющая и ставки фрахта; дефект = претензии на млн отправок",
    },
)
CATEGORY_BY_ID = {entry["id"]: entry for entry in CATEGORIES}
CATEGORY_ORDER = tuple(entry["id"] for entry in CATEGORIES)

COUNTRIES = {
    "PL": {"name": "Польша", "risk": 0.22, "duty_rate": 0.0, "region": "EU"},
    "DE": {"name": "Германия", "risk": 0.14, "duty_rate": 0.0, "region": "EU"},
    "CZ": {"name": "Чехия", "risk": 0.20, "duty_rate": 0.0, "region": "EU"},
    "IT": {"name": "Италия", "risk": 0.24, "duty_rate": 0.0, "region": "EU"},
    "RO": {"name": "Румыния", "risk": 0.28, "duty_rate": 0.0, "region": "EU"},
    "TR": {"name": "Турция", "risk": 0.47, "duty_rate": 0.045, "region": "non-EU"},
    "CN": {"name": "Китай", "risk": 0.42, "duty_rate": 0.065, "region": "non-EU"},
    "VN": {"name": "Вьетнам", "risk": 0.51, "duty_rate": 0.080, "region": "non-EU"},
    "IN": {"name": "Индия", "risk": 0.49, "duty_rate": 0.090, "region": "non-EU"},
    "UA": {"name": "Украина", "risk": 0.62, "duty_rate": 0.0, "region": "DCFTA"},
}

SCENARIOS = (
    {
        "id": "base",
        "label": "базовый (без шока)",
        "cpi_delta_pp": 0.0,
        "oil_delta_pct": 0.0,
        "freight_delta_pct": 0.0,
        "fx_delta_pct": 0.0,
        "volume_delta_pct": 0.0,
        "note": "точка прогноза CPI без сдвига; бюджет равен сумме контрактов",
    },
    {
        "id": "inflation_plus_1pp",
        "label": "инфляция +1 пп",
        "cpi_delta_pp": 1.0,
        "oil_delta_pct": 0.0,
        "freight_delta_pct": 0.0,
        "fx_delta_pct": 0.0,
        "volume_delta_pct": 0.0,
        "note": "чистый CPI-канал: индексация по CPI-доле контрактов",
    },
    {
        "id": "oil_plus_20",
        "label": "нефть +20 %",
        "cpi_delta_pp": 0.0,
        "oil_delta_pct": 20.0,
        "freight_delta_pct": 6.0,
        "fx_delta_pct": 0.0,
        "volume_delta_pct": 0.0,
        "note": "нефтяной канал + CPI-отклик 0,015 пп на 1 % нефти",
    },
    {
        "id": "hormuz_6m",
        "label": "Ормуз закрыт на 6 месяцев (мост цены)",
        "cpi_delta_pp": 1.5,
        "oil_delta_pct": 125.14112,
        "freight_delta_pct": 35.0,
        "fx_delta_pct": 0.0,
        "volume_delta_pct": -2.0,
        "note": "сдвиг нефти из bridge_report.market_shock (Ормуз, 6 мес.)",
    },
    {
        "id": "stress_combo",
        "label": "комбинированный стресс",
        "cpi_delta_pp": 2.0,
        "oil_delta_pct": 40.0,
        "freight_delta_pct": 15.0,
        "fx_delta_pct": 5.0,
        "volume_delta_pct": -5.0,
        "note": "CPI + нефть + фрахт + ослабление EUR к валютам закупок",
    },
)
SCENARIO_BY_ID = {entry["id"]: entry for entry in SCENARIOS}
SCENARIO_ORDER = tuple(entry["id"] for entry in SCENARIOS)
DEFAULT_SCENARIO = "base"

MACRO_ANCHORS = (
    {
        "name": "cpi_yoy_pct",
        "value": 3.6257264569782937,
        "unit": "% г/г",
        "source": "build/product/forecast_latest.json: point T1_CPI_YOY (2026-11-14)",
        "source_class": "published_aggregate",
    },
    {
        "name": "cpi_interval_pct",
        "value": [2.45788896937228, 4.797070272153592],
        "unit": "% г/г (5–95)",
        "source": "build/product/forecast_latest.json: quantile endpoints",
        "source_class": "published_aggregate",
    },
    {
        "name": "brent_base_usd_bbl",
        "value": 75.0,
        "unit": "USD/bbl",
        "source": "build/market_bridge/bridge_report.json: market_shock.base_price_usd_bbl",
        "source_class": "published_aggregate",
    },
    {
        "name": "oil_price_elasticity_pct_per_100",
        "value": OIL_PRICE_ELASTICITY_PCT_PER_100,
        "unit": "% индекса цен на +100 % Brent",
        "source": ("build/market_bridge/bridge_report.json: aggregate_price_index_pct / "
                   "brent_delta_pct (Hormuz 6m), линеаризация"),
        "source_class": "published_aggregate",
    },
    {
        "name": "oil_to_cpi_pp_per_pct",
        "value": OIL_TO_CPI_PP_PER_PCT,
        "unit": "пп CPI на 1 % нефти",
        "source": "допущение порядка величины (docs/178 §8)",
        "source_class": "assumption",
    },
    {
        "name": "freight_to_price_elasticity",
        "value": FREIGHT_TO_PRICE_ELASTICITY,
        "unit": "% цены на +1 % ставки фрахта",
        "source": "допущение порядка величины (доля транспорта в цене)",
        "source_class": "assumption",
    },
)


class ProcurementError(RuntimeError):
    """Any condition that must stop the run before a file is written."""


class DatasetError(ProcurementError):
    """The supplier/contract dataset is broken or violates the data policy."""


class ScenarioError(ProcurementError):
    """Unknown or inconsistent macro scenario."""


class WeightsError(ProcurementError):
    """Scoring weights are invalid."""


# ------------------------------------------------------------------------------------ utilities
def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def read_json(path: Path, *, what: str) -> dict:
    if not Path(path).exists():
        raise ProcurementError(f"нет файла {what}: {display_path(Path(path))}")
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProcurementError(f"{what}: битый JSON ({error})") from error
    if not isinstance(document, dict):
        raise ProcurementError(f"{what}: ожидался объект JSON")
    return document


def number(value, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetError(f"{what}: ожидалось число, получено {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise DatasetError(f"{what}: не конечное число")
    return result


def r(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def band_for(score: float) -> dict:
    for letter, threshold, label_ru in BANDS:
        if score >= threshold:
            return {"letter": letter, "label_ru": label_ru, "threshold": threshold}
    return {"letter": "C", "label_ru": "рискованный", "threshold": 0.0}


def _check_keys(payload: dict, allowed: tuple, where: str) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise DatasetError(f"{where}: неизвестные поля {unknown}")


def _require(payload: dict, fields: tuple, where: str) -> None:
    missing = [name for name in fields if name not in payload]
    if missing:
        raise DatasetError(f"{where}: отсутствуют поля {missing}")


# ---------------------------------------------------------------------------------- demo dataset
def demo_dataset() -> dict:
    """Synthetic supplier/contract set: no real company, price or contract data."""
    return {
        "schema": DATASET_SCHEMA,
        "version": "1.0.0",
        "source_class": "synthetic_demo",
        "currency": BASE_CURRENCY,
        "period": "2026",
        "note": ("Демонстрационный набор: поставщики, цены и контракты вымышлены. "
                 "Реальные данные клиента в репозиторий не попадают."),
        "suppliers": [
            {
                "id": "sup-pl-stal", "name": "Stal-Pol (демо)", "country": "PL",
                "category": "metal_rolled", "currency": "EUR", "unit": "кг",
                "annual_volume": 900000, "unit_price": 0.94, "freight_per_unit": 0.028,
                "lead_time_days": 12, "otif": 0.965, "defect_ppm": 320, "audit_score": 0.80,
                "risk": {"financial": 0.18, "supply": 0.22, "compliance": 0.15},
                "contract": {"id": "CT-MET-01", "status": "active", "cpi_share": 0.40,
                             "ppi_share": 0.40, "fx_share": 0.0, "cap_pct": 6.0,
                             "floor_zero": True, "payment_days": 45},
            },
            {
                "id": "sup-de-stahl", "name": "Stahlwerk Nord (демо)", "country": "DE",
                "category": "metal_rolled", "currency": "EUR", "unit": "кг",
                "annual_volume": 750000, "unit_price": 1.02, "freight_per_unit": 0.019,
                "lead_time_days": 8, "otif": 0.985, "defect_ppm": 140, "audit_score": 0.90,
                "risk": {"financial": 0.12, "supply": 0.10, "compliance": 0.08},
                "contract": {"id": "CT-MET-02", "status": "active", "cpi_share": 0.60,
                             "ppi_share": 0.20, "fx_share": 0.0, "cap_pct": 4.0,
                             "floor_zero": True, "payment_days": 30},
            },
            {
                "id": "sup-cn-metal", "name": "East Metal (демо)", "country": "CN",
                "category": "metal_rolled", "currency": "USD", "unit": "кг",
                "annual_volume": 750000, "unit_price": 0.79, "freight_per_unit": 0.061,
                "lead_time_days": 42, "otif": 0.915, "defect_ppm": 780, "audit_score": 0.66,
                "risk": {"financial": 0.30, "supply": 0.35, "compliance": 0.25},
                "contract": {"id": "CT-MET-03", "status": "candidate", "cpi_share": 0.0,
                             "ppi_share": 0.0, "fx_share": 0.80, "cap_pct": 10.0,
                             "floor_zero": True, "payment_days": 60},
            },
            {
                "id": "sup-pl-polymer", "name": "Polimer-PL (демо)", "country": "PL",
                "category": "polymer_granulate", "currency": "EUR", "unit": "кг",
                "annual_volume": 220000, "unit_price": 1.24, "freight_per_unit": 0.033,
                "lead_time_days": 10, "otif": 0.975, "defect_ppm": 220, "audit_score": 0.82,
                "risk": {"financial": 0.20, "supply": 0.25, "compliance": 0.18},
                "contract": {"id": "CT-POL-01", "status": "active", "cpi_share": 0.50,
                             "ppi_share": 0.40, "fx_share": 0.0, "cap_pct": 8.0,
                             "floor_zero": True, "payment_days": 45},
            },
            {
                "id": "sup-de-polymer", "name": "Rheinpolymer (демо)", "country": "DE",
                "category": "polymer_granulate", "currency": "EUR", "unit": "кг",
                "annual_volume": 130000, "unit_price": 1.38, "freight_per_unit": 0.021,
                "lead_time_days": 7, "otif": 0.990, "defect_ppm": 90, "audit_score": 0.92,
                "risk": {"financial": 0.10, "supply": 0.08, "compliance": 0.06},
                "contract": {"id": "CT-POL-02", "status": "candidate", "cpi_share": 0.70,
                             "ppi_share": 0.10, "fx_share": 0.0, "cap_pct": 3.5,
                             "floor_zero": True, "payment_days": 30},
            },
            {
                "id": "sup-tr-polymer", "name": "Anadolu Kimya (демо)", "country": "TR",
                "category": "polymer_granulate", "currency": "USD", "unit": "кг",
                "annual_volume": 100000, "unit_price": 1.16, "freight_per_unit": 0.052,
                "lead_time_days": 21, "otif": 0.940, "defect_ppm": 480, "audit_score": 0.70,
                "risk": {"financial": 0.38, "supply": 0.30, "compliance": 0.28},
                "contract": {"id": "CT-POL-03", "status": "active", "cpi_share": 0.30,
                             "ppi_share": 0.30, "fx_share": 0.40, "cap_pct": 9.0,
                             "floor_zero": True, "payment_days": 60},
            },
            {
                "id": "sup-cz-film", "name": "Folie Czech (демо)", "country": "CZ",
                "category": "packaging_film", "currency": "EUR", "unit": "кг",
                "annual_volume": 55000, "unit_price": 2.12, "freight_per_unit": 0.030,
                "lead_time_days": 9, "otif": 0.970, "defect_ppm": 300, "audit_score": 0.78,
                "risk": {"financial": 0.22, "supply": 0.18, "compliance": 0.14},
                "contract": {"id": "CT-FLM-01", "status": "active", "cpi_share": 0.50,
                             "ppi_share": 0.30, "fx_share": 0.0, "cap_pct": 6.0,
                             "floor_zero": True, "payment_days": 45},
            },
            {
                "id": "sup-it-film", "name": "Film Italia (демо)", "country": "IT",
                "category": "packaging_film", "currency": "EUR", "unit": "кг",
                "annual_volume": 40000, "unit_price": 2.35, "freight_per_unit": 0.024,
                "lead_time_days": 6, "otif": 0.985, "defect_ppm": 150, "audit_score": 0.88,
                "risk": {"financial": 0.16, "supply": 0.12, "compliance": 0.10},
                "contract": {"id": "CT-FLM-02", "status": "active", "cpi_share": 0.60,
                             "ppi_share": 0.20, "fx_share": 0.0, "cap_pct": 4.0,
                             "floor_zero": True, "payment_days": 30},
            },
            {
                "id": "sup-vn-film", "name": "Mekong Pack (демо)", "country": "VN",
                "category": "packaging_film", "currency": "USD", "unit": "кг",
                "annual_volume": 25000, "unit_price": 1.86, "freight_per_unit": 0.075,
                "lead_time_days": 38, "otif": 0.900, "defect_ppm": 950, "audit_score": 0.62,
                "risk": {"financial": 0.42, "supply": 0.40, "compliance": 0.35},
                "contract": {"id": "CT-FLM-03", "status": "candidate", "cpi_share": 0.0,
                             "ppi_share": 0.0, "fx_share": 0.90, "cap_pct": 12.0,
                             "floor_zero": True, "payment_days": 90},
            },
            {
                "id": "sup-pl-log", "name": "Trans-Pol (демо)", "country": "PL",
                "category": "contract_logistics", "currency": "EUR", "unit": "отправка",
                "annual_volume": 900, "unit_price": 820.0, "freight_per_unit": 0.0,
                "lead_time_days": 3, "otif": 0.960, "defect_ppm": 500, "audit_score": 0.80,
                "risk": {"financial": 0.20, "supply": 0.22, "compliance": 0.16},
                "contract": {"id": "CT-LOG-01", "status": "active", "cpi_share": 0.40,
                             "ppi_share": 0.30, "fx_share": 0.0, "cap_pct": 5.0,
                             "floor_zero": True, "payment_days": 30},
            },
            {
                "id": "sup-ro-log", "name": "Dunarea Cargo (демо)", "country": "RO",
                "category": "contract_logistics", "currency": "EUR", "unit": "отправка",
                "annual_volume": 600, "unit_price": 760.0, "freight_per_unit": 0.0,
                "lead_time_days": 5, "otif": 0.945, "defect_ppm": 650, "audit_score": 0.72,
                "risk": {"financial": 0.28, "supply": 0.30, "compliance": 0.22},
                "contract": {"id": "CT-LOG-02", "status": "active", "cpi_share": 0.35,
                             "ppi_share": 0.35, "fx_share": 0.0, "cap_pct": 7.0,
                             "floor_zero": True, "payment_days": 45},
            },
            {
                "id": "sup-de-log", "name": "Nordfreight (демо)", "country": "DE",
                "category": "contract_logistics", "currency": "EUR", "unit": "отправка",
                "annual_volume": 300, "unit_price": 910.0, "freight_per_unit": 0.0,
                "lead_time_days": 2, "otif": 0.985, "defect_ppm": 200, "audit_score": 0.90,
                "risk": {"financial": 0.12, "supply": 0.10, "compliance": 0.08},
                "contract": {"id": "CT-LOG-03", "status": "candidate", "cpi_share": 0.50,
                             "ppi_share": 0.10, "fx_share": 0.0, "cap_pct": 3.0,
                             "floor_zero": True, "payment_days": 30},
            },
        ],
    }


# ------------------------------------------------------------------------------------- validation
def validate_weights(weights: dict | None) -> dict:
    values = dict(DEFAULT_WEIGHTS if weights is None else weights)
    unknown = sorted(set(values) - set(WEIGHT_KEYS))
    if unknown:
        raise WeightsError(f"неизвестные веса {unknown}; ожидались {list(WEIGHT_KEYS)}")
    missing = sorted(set(WEIGHT_KEYS) - set(values))
    if missing:
        raise WeightsError(f"не заданы веса {missing}")
    clean = {}
    for key in WEIGHT_KEYS:
        value = number(values[key], what=f"вес {key}")
        if value <= 0.0:
            raise WeightsError(f"вес {key} должен быть положительным")
        clean[key] = value
    total = sum(clean.values())
    if abs(total - 1.0) > 1e-9:
        raise WeightsError(f"сумма весов должна быть 1.0, получено {total!r}")
    return {"values": clean, "sum": total}


def normalize_dataset(document: dict, *, origin: str) -> dict:
    if document.get("schema") != DATASET_SCHEMA:
        raise DatasetError(f"набор {origin}: схема {document.get('schema')!r}, "
                           f"ожидалась {DATASET_SCHEMA!r}")
    source_class = document.get("source_class")
    if source_class not in SOURCE_CLASSES:
        raise DatasetError(
            f"набор {origin}: source_class {source_class!r} не разрешён "
            f"({list(SOURCE_CLASSES)}): реальные данные клиента в репозиторий не коммитятся")
    if document.get("currency") != BASE_CURRENCY:
        raise DatasetError(f"набор {origin}: базовая валюта должна быть {BASE_CURRENCY}")
    raw = document.get("suppliers")
    if not isinstance(raw, list) or not raw:
        raise DatasetError(f"набор {origin}: пустой список поставщиков")

    records = []
    seen = set()
    for index, entry in enumerate(raw):
        where = f"поставщик #{index + 1}"
        if not isinstance(entry, dict):
            raise DatasetError(f"{where}: ожидался объект")
        _check_keys(entry, ("id", "name", "country", "category", "currency", "unit",
                            "annual_volume", "unit_price", "freight_per_unit", "lead_time_days",
                            "otif", "defect_ppm", "audit_score", "risk", "contract"), where)
        _require(entry, ("id", "name", "country", "category", "currency", "unit",
                         "annual_volume", "unit_price", "freight_per_unit", "lead_time_days",
                         "otif", "defect_ppm", "audit_score", "risk", "contract"), where)
        supplier_id = entry["id"]
        if not isinstance(supplier_id, str) or not supplier_id.strip():
            raise DatasetError(f"{where}: пустой id")
        if supplier_id in seen:
            raise DatasetError(f"{where}: повторяющийся id {supplier_id!r}")
        seen.add(supplier_id)
        where = f"поставщик {supplier_id}"
        country_code = entry["country"]
        if country_code not in COUNTRIES:
            raise DatasetError(f"{where}: неизвестная страна {country_code!r}")
        category = entry["category"]
        if category not in CATEGORY_BY_ID:
            raise DatasetError(f"{where}: неизвестная категория {category!r}")
        if entry["currency"] not in CURRENCIES:
            raise DatasetError(f"{where}: неизвестная валюта {entry['currency']!r}")

        volume = number(entry["annual_volume"], what=f"{where}.annual_volume")
        price = number(entry["unit_price"], what=f"{where}.unit_price")
        freight = number(entry["freight_per_unit"], what=f"{where}.freight_per_unit")
        lead = number(entry["lead_time_days"], what=f"{where}.lead_time_days")
        otif = number(entry["otif"], what=f"{where}.otif")
        defect = number(entry["defect_ppm"], what=f"{where}.defect_ppm")
        audit = number(entry["audit_score"], what=f"{where}.audit_score")
        if volume <= 0:
            raise DatasetError(f"{where}: annual_volume должен быть > 0")
        if price <= 0:
            raise DatasetError(f"{where}: unit_price должен быть > 0")
        if freight < 0:
            raise DatasetError(f"{where}: freight_per_unit не может быть отрицательным")
        if not 0 < lead <= 365:
            raise DatasetError(f"{where}: lead_time_days должен быть в (0, 365]")
        if not 0 < otif <= 1:
            raise DatasetError(f"{where}: otif должен быть в (0, 1]")
        if not 0 < defect <= 1_000_000:
            raise DatasetError(f"{where}: defect_ppm должен быть в (0, 1e6]")
        if not 0 < audit <= 1:
            raise DatasetError(f"{where}: audit_score должен быть в (0, 1]")

        risk_raw = entry["risk"]
        if not isinstance(risk_raw, dict):
            raise DatasetError(f"{where}.risk: ожидался объект")
        _check_keys(risk_raw, ("financial", "supply", "compliance"), f"{where}.risk")
        _require(risk_raw, ("financial", "supply", "compliance"), f"{where}.risk")
        parts = {}
        for key in ("financial", "supply", "compliance"):
            value = number(risk_raw[key], what=f"{where}.risk.{key}")
            if not 0.0 <= value <= 1.0:
                raise DatasetError(f"{where}.risk.{key} должен быть в [0, 1]")
            parts[key] = value

        contract = entry["contract"]
        if not isinstance(contract, dict):
            raise DatasetError(f"{where}.contract: ожидался объект")
        _check_keys(contract, ("id", "status", "cpi_share", "ppi_share", "fx_share",
                               "cap_pct", "floor_zero", "payment_days"), f"{where}.contract")
        _require(contract, ("id", "status", "cpi_share", "ppi_share", "fx_share",
                            "cap_pct", "floor_zero", "payment_days"), f"{where}.contract")
        if contract["status"] not in CONTRACT_STATUSES:
            raise DatasetError(f"{where}.contract.status: ожидалось одно из "
                               f"{list(CONTRACT_STATUSES)}")
        shares = {}
        for key in ("cpi_share", "ppi_share", "fx_share"):
            value = number(contract[key], what=f"{where}.contract.{key}")
            if not 0.0 <= value <= 1.0:
                raise DatasetError(f"{where}.contract.{key} должен быть в [0, 1]")
            shares[key] = value
        if sum(shares.values()) > 1.0 + 1e-9:
            raise DatasetError(f"{where}.contract: доли индексации в сумме > 1 "
                               f"({sum(shares.values())!r})")
        if shares["fx_share"] > 0 and entry["currency"] == BASE_CURRENCY:
            raise DatasetError(f"{where}.contract: fx_share > 0 при базовой валюте "
                               f"{BASE_CURRENCY}")
        cap = number(contract["cap_pct"], what=f"{where}.contract.cap_pct")
        if not 0.0 <= cap <= 25.0:
            raise DatasetError(f"{where}.contract.cap_pct должен быть в [0, 25]")
        if not isinstance(contract["floor_zero"], bool):
            raise DatasetError(f"{where}.contract.floor_zero должен быть bool")
        payment_days = number(contract["payment_days"], what=f"{where}.contract.payment_days")
        if not 0 <= payment_days <= 365:
            raise DatasetError(f"{where}.contract.payment_days должен быть в [0, 365]")

        country = COUNTRIES[country_code]
        composite = (RISK_WEIGHTS["country"] * country["risk"]
                     + RISK_WEIGHTS["financial"] * parts["financial"]
                     + RISK_WEIGHTS["supply"] * parts["supply"]
                     + RISK_WEIGHTS["compliance"] * parts["compliance"])
        tco = supplier_tco(price=price, freight=freight, composite_risk=composite,
                           defect_ppm=defect, otif=otif, lead_days=lead,
                           duty_rate=country["duty_rate"])
        record = {
            "id": supplier_id,
            "name": entry["name"] if isinstance(entry["name"], str) else supplier_id,
            "country": country_code,
            "country_name": country["name"],
            "region": country["region"],
            "duty_rate": country["duty_rate"],
            "category": category,
            "currency": entry["currency"],
            "unit": (entry["unit"] if isinstance(entry["unit"], str)
                     else CATEGORY_BY_ID[category]["unit"]),
            "annual_volume": volume,
            "unit_price": price,
            "freight_per_unit": freight,
            "lead_time_days": lead,
            "otif": otif,
            "defect_ppm": defect,
            "audit_score": audit,
            "risk_parts": {"country": country["risk"], **parts},
            "risk_composite": composite,
            "tco": tco,
            "base_amount_eur": volume * price,
            "contract": {
                "id": contract["id"],
                "status": contract["status"],
                "cpi_share": shares["cpi_share"],
                "ppi_share": shares["ppi_share"],
                "fx_share": shares["fx_share"],
                "cap_pct": cap,
                "floor_zero": bool(contract["floor_zero"]),
                "payment_days": payment_days,
            },
        }
        records.append(record)

    by_category: dict = {}
    for category in CATEGORY_ORDER:
        entries = [record for record in records if record["category"] == category]
        if entries and len(entries) < MIN_SUPPLIERS_PER_CATEGORY:
            raise DatasetError(f"категория {category!r}: нужно минимум "
                               f"{MIN_SUPPLIERS_PER_CATEGORY} поставщика для сравнения, "
                               f"есть {len(entries)}")
        active = [record for record in entries if record["contract"]["status"] == "active"]
        if entries and len(active) < MIN_ACTIVE_PER_CATEGORY:
            raise DatasetError(f"категория {category!r}: нет ни одного действующего контракта")
        if entries:
            by_category[category] = {
                "suppliers": len(entries),
                "active": len(active),
                "volume": sum(record["annual_volume"] for record in active),
                "spend_eur": sum(record["base_amount_eur"] for record in active),
            }
    if not by_category:
        raise DatasetError(f"набор {origin}: ни один поставщик не попал в объявленные категории")

    return {
        "schema": DATASET_SCHEMA,
        "version": document.get("version"),
        "source_class": source_class,
        "currency": BASE_CURRENCY,
        "period": document.get("period"),
        "note": document.get("note"),
        "suppliers": records,
        "categories": by_category,
        "counts": {
            "suppliers": len(records),
            "categories": len(by_category),
            "active": sum(1 for record in records
                          if record["contract"]["status"] == "active"),
            "candidates": sum(1 for record in records
                              if record["contract"]["status"] == "candidate"),
        },
    }


def supplier_tco(*, price: float, freight: float, composite_risk: float, defect_ppm: float,
                 otif: float, lead_days: float, duty_rate: float) -> dict:
    components = {
        "price": price,
        "logistics": freight,
        "duty": price * duty_rate,
        "risk": price * composite_risk * RISK_COST_RATE,
        "quality": price * (defect_ppm / 1e6) * REWORK_MULTIPLIER,
        "late": price * (1.0 - otif) * LATE_PENALTY_RATE,
        "carrying": price * lead_days / 365.0 * CARRYING_RATE,
    }
    components["total"] = sum(components[key] for key in TCO_KEYS)
    return components


# ---------------------------------------------------------------------------------------- scoring
def score_category(category: str, records: list, weights: dict) -> dict:
    info = CATEGORY_BY_ID[category]
    entries = [record for record in records if record["category"] == category]
    if len(entries) < MIN_SUPPLIERS_PER_CATEGORY:
        raise DatasetError(f"категория {category!r}: слишком мало поставщиков для скоринга")

    min_price = min(entry["unit_price"] for entry in entries)
    min_tco = min(entry["tco"]["total"] for entry in entries)
    min_lead = min(entry["lead_time_days"] for entry in entries)
    min_defect = min(entry["defect_ppm"] for entry in entries)
    max_otif = max(entry["otif"] for entry in entries)
    max_audit = max(entry["audit_score"] for entry in entries)
    max_benefit = max(1.0 - entry["risk_composite"] for entry in entries)
    if max_benefit <= 0:
        raise DatasetError(f"категория {category!r}: риск всех поставщиков равен 1.0")

    scored = []
    for entry in entries:
        quality = (QUALITY_WEIGHTS["defect"] * min_defect / entry["defect_ppm"]
                   + QUALITY_WEIGHTS["otif"] * entry["otif"] / max_otif
                   + QUALITY_WEIGHTS["audit"] * entry["audit_score"] / max_audit)
        components = {
            "price": min_price / entry["unit_price"],
            "tco": min_tco / entry["tco"]["total"],
            "risk": (1.0 - entry["risk_composite"]) / max_benefit,
            "quality": quality,
            "delivery": min_lead / entry["lead_time_days"],
        }
        components = {key: r(components[key], 12) for key in WEIGHT_KEYS}
        contributions = {key: r(weights[key] * components[key], 12) for key in WEIGHT_KEYS}
        score = r(sum(contributions.values()), 12)
        scored.append({**entry, "components": components,
                       "contributions": contributions, "score": score})

    scored.sort(key=lambda item: (-item["score"], item["id"]))
    best_score = scored[0]["score"]
    for position, entry in enumerate(scored, start=1):
        entry["rank"] = position
        entry["band"] = band_for(entry["score"])
        entry["delta_to_best"] = r(entry["score"] - best_score, 12)
        entry["tco_vs_best_pct"] = r(
            (entry["tco"]["total"] / min_tco - 1.0) * 100.0, 6)
        entry["price_vs_best_pct"] = r(
            (entry["unit_price"] / min_price - 1.0) * 100.0, 6)

    active_volume = sum(entry["annual_volume"] for entry in scored
                        if entry["contract"]["status"] == "active")
    active_tco = sum(entry["annual_volume"] * entry["tco"]["total"] for entry in scored
                     if entry["contract"]["status"] == "active")
    best_single = min_tco * active_volume
    return {
        "id": category,
        "name_ru": info["name_ru"],
        "unit": info["unit"],
        "note": info["note"],
        "suppliers": scored,
        "recommended": {"supplier_id": scored[0]["id"], "score": scored[0]["score"],
                        "band": scored[0]["band"]["letter"]},
        "best_price_supplier": min(entries, key=lambda item: (item["unit_price"],
                                                              item["id"]))["id"],
        "best_tco_supplier": min(entries, key=lambda item: (item["tco"]["total"],
                                                            item["id"]))["id"],
        "savings": {
            "volume_active": active_volume,
            "current_tco_eur": r(active_tco, 6),
            "best_single_source_tco_eur": r(best_single, 6),
            "saving_eur": r(active_tco - best_single, 6),
            "saving_pct": r((active_tco - best_single) / active_tco * 100.0, 6)
            if active_tco else 0.0,
            "note": ("иллюстрация «весь объём категории — одному поставщику с минимальным TCO»; "
                     "концентрация риска не учтена"),
        },
    }


# -------------------------------------------------------------------------------- macro scenarios
def scenario_drivers(scenario_id: str, *, cpi_override=None) -> dict:
    scenario = SCENARIO_BY_ID.get(scenario_id)
    if scenario is None:
        raise ScenarioError(f"неизвестный сценарий {scenario_id!r}; известны "
                            f"{list(SCENARIO_ORDER)}")
    cpi_from_oil = scenario["oil_delta_pct"] * OIL_TO_CPI_PP_PER_PCT
    cpi_pp = (scenario["cpi_delta_pp"] + cpi_from_oil) if cpi_override is None \
        else float(cpi_override)
    return {
        "id": scenario["id"],
        "label": scenario["label"],
        "note": scenario["note"],
        "cpi_pp": cpi_pp,
        "cpi_explicit_pp": scenario["cpi_delta_pp"],
        "cpi_from_oil_pp": r(cpi_from_oil, 9),
        "oil_delta_pct": scenario["oil_delta_pct"],
        "oil_price_pct": r(scenario["oil_delta_pct"] * OIL_PRICE_ELASTICITY_PCT_PER_100 / 100.0, 9),
        "freight_delta_pct": scenario["freight_delta_pct"],
        "freight_price_pct": r(scenario["freight_delta_pct"] * FREIGHT_TO_PRICE_ELASTICITY, 9),
        "fx_pct": scenario["fx_delta_pct"],
        "volume_delta_pct": scenario["volume_delta_pct"],
    }


def category_price_change(category: str, drivers: dict) -> dict:
    info = CATEGORY_BY_ID[category]
    components = {
        "cpi": info["cpi_incidence"] * drivers["cpi_pp"],
        "oil": info["oil_incidence"] * drivers["oil_price_pct"],
        "freight": info["freight_incidence"] * drivers["freight_price_pct"],
    }
    total = sum(components.values())
    return {
        "id": category,
        "name_ru": info["name_ru"],
        "incidences": {"cpi": info["cpi_incidence"], "oil": info["oil_incidence"],
                       "freight": info["freight_incidence"]},
        "components_pct": {key: r(value, 9) for key, value in components.items()},
        "price_change_pct": r(total, 9),
    }


def contract_indexation(record: dict, drivers: dict) -> dict:
    contract = record["contract"]
    info = CATEGORY_BY_ID[record["category"]]
    ppi_driver = (info["oil_incidence"] * drivers["oil_price_pct"]
                  + info["freight_incidence"] * drivers["freight_price_pct"])
    parts = {
        "cpi": contract["cpi_share"] * drivers["cpi_pp"],
        "ppi": contract["ppi_share"] * ppi_driver,
        "fx": contract["fx_share"] * drivers["fx_pct"],
    }
    raw = sum(parts.values())
    applied = raw
    flags = []
    if applied > contract["cap_pct"]:
        applied = contract["cap_pct"]
        flags.append("CAP_BINDING")
    if contract["floor_zero"] and applied < 0.0:
        applied = 0.0
        flags.append("FLOOR_BINDING")
    base = record["base_amount_eur"]
    indexation_eur = base * applied / 100.0
    volume_effect_eur = (base + indexation_eur) * drivers["volume_delta_pct"] / 100.0
    fixed_share = max(0.0, 1.0 - contract["cpi_share"] - contract["ppi_share"]
                      - contract["fx_share"])
    return {
        "contract_id": contract["id"],
        "supplier_id": record["id"],
        "category": record["category"],
        "status": contract["status"],
        "shares": {"cpi": contract["cpi_share"], "ppi": contract["ppi_share"],
                   "fx": contract["fx_share"], "fixed": r(fixed_share, 12)},
        "driver_parts_pct": {key: r(value, 9) for key, value in parts.items()},
        "raw_pct": r(raw, 9),
        "cap_pct": contract["cap_pct"],
        "floor_zero": contract["floor_zero"],
        "applied_pct": r(applied, 9),
        "clamped": bool(flags),
        "flags": flags,
        "base_eur": r(base, 6),
        "indexation_eur": r(indexation_eur, 6),
        "volume_effect_eur": r(volume_effect_eur, 6),
        "forecast_eur": r(base + indexation_eur, 6),
        "forecast_with_volume_eur": r(base + indexation_eur + volume_effect_eur, 6),
        "payment_days": contract["payment_days"],
    }


def budget_block(dataset: dict, drivers: dict) -> dict:
    records = dataset["suppliers"]
    contracts = []
    by_category = []
    for category in CATEGORY_ORDER:
        if category not in dataset["categories"]:
            continue
        category_base = 0.0
        category_forecast = 0.0
        category_with_volume = 0.0
        active_contracts = 0
        for record in records:
            if record["category"] != category:
                continue
            if record["contract"]["status"] != "active":
                continue
            item = contract_indexation(record, drivers)
            contracts.append(item)
            category_base += item["base_eur"]
            category_forecast += item["forecast_eur"]
            category_with_volume += item["forecast_with_volume_eur"]
            active_contracts += 1
        price = category_price_change(category, drivers)
        by_category.append({
            "id": category,
            "name_ru": CATEGORY_BY_ID[category]["name_ru"],
            "base_eur": r(category_base, 6),
            "forecast_eur": r(category_forecast, 6),
            "forecast_with_volume_eur": r(category_with_volume, 6),
            "delta_eur": r(category_forecast - category_base, 6),
            "delta_pct": r((category_forecast - category_base) / category_base * 100.0, 9)
            if category_base else 0.0,
            "price_change_pct": price["price_change_pct"],
            "active_contracts": active_contracts,
        })
    contracts.sort(key=lambda item: (item["category"], item["supplier_id"]))
    base_total = sum(item["base_eur"] for item in contracts)
    total = sum(item["forecast_eur"] for item in contracts)
    total_with_volume = sum(item["forecast_with_volume_eur"] for item in contracts)
    return {
        "contracts": contracts,
        "by_category": by_category,
        "base_total_eur": r(base_total, 6),
        "total_eur": r(total, 6),
        "total_with_volume_eur": r(total_with_volume, 6),
        "volume_effect_eur": r(total_with_volume - total, 6),
        "delta_eur": r(total - base_total, 6),
        "delta_pct": r((total - base_total) / base_total * 100.0, 9) if base_total else 0.0,
    }


def budget_interval(dataset: dict, drivers: dict) -> dict:
    anchors = {entry["name"]: entry["value"] for entry in MACRO_ANCHORS}
    point = float(anchors["cpi_yoy_pct"])
    low, high = anchors["cpi_interval_pct"]
    low_delta = float(low) - point
    high_delta = float(high) - point
    low_block = budget_block(dataset, {**drivers, "cpi_pp": drivers["cpi_pp"] + low_delta})
    high_block = budget_block(dataset, {**drivers, "cpi_pp": drivers["cpi_pp"] + high_delta})
    centre = budget_block(dataset, drivers)
    floor_bound = sum(1 for item in low_block["contracts"] if "FLOOR_BINDING" in item["flags"])
    cap_bound = sum(1 for item in high_block["contracts"] if "CAP_BINDING" in item["flags"])
    return {
        "low_eur": low_block["total_eur"],
        "point_eur": centre["total_eur"],
        "high_eur": high_block["total_eur"],
        "width_eur": r(high_block["total_eur"] - low_block["total_eur"], 6),
        "cpi_low_pp": r(drivers["cpi_pp"] + low_delta, 9),
        "cpi_high_pp": r(drivers["cpi_pp"] + high_delta, 9),
        "floor_bound_contracts": floor_bound,
        "cap_bound_contracts": cap_bound,
        "note": ("интервал 5–95 прогноза CPI, сдвинутый на шаг сценария (конвенция docs/178 §4); "
                 "на нижней границе срабатывает floor_zero, на верхней — cap; неопределённость "
                 "нефти/фрахта/FX не публикуется"),
    }


# --------------------------------------------------------------------------------------- anchors
def verify_anchors(feed: Path = DEFAULT_FEED, price_bridge: Path = DEFAULT_PRICE_BRIDGE) -> dict:
    anchors = {entry["name"]: entry["value"] for entry in MACRO_ANCHORS}
    feed_document = read_json(Path(feed), what="продуктовый фид")
    target = feed_document.get("target")
    if target != "T1_CPI_YOY":
        raise ProcurementError(f"фид: цель {target!r}, ожидалась T1_CPI_YOY")
    point = number(feed_document.get("point"), what="фид.point")
    quantiles = feed_document.get("quantiles_5_to_95_step_5")
    if not isinstance(quantiles, list) or len(quantiles) != 19:
        raise ProcurementError("фид: ожидался вектор из 19 квантилей 5–95")
    low, high = number(quantiles[0], what="фид.q05"), number(quantiles[-1], what="фид.q95")
    if abs(point - anchors["cpi_yoy_pct"]) > 1e-6:
        raise ProcurementError(f"якорь CPI {anchors['cpi_yoy_pct']!r} не совпадает с фидом "
                               f"{point!r}")
    if abs(low - anchors["cpi_interval_pct"][0]) > 1e-6 or \
            abs(high - anchors["cpi_interval_pct"][1]) > 1e-6:
        raise ProcurementError("якорь интервала CPI не совпадает с квантилями фида")

    bridge = read_json(Path(price_bridge), what="мост цены")
    if bridge.get("schema") != "amfe.markets.mrio_bridge.v1":
        raise ProcurementError(f"мост цены: схема {bridge.get('schema')!r}")
    if bridge.get("status") != STATUS:
        raise ProcurementError(f"мост цены: статус {bridge.get('status')!r}, ожидался {STATUS}")
    shock = bridge.get("market_shock") or {}
    base_price = number(shock.get("base_price_usd_bbl"), what="мост.base_price_usd_bbl")
    delta_pct = number(shock.get("brent_delta_pct"), what="мост.brent_delta_pct")
    if abs(base_price - anchors["brent_base_usd_bbl"]) > 1e-9:
        raise ProcurementError("якорь базовой цены Brent не совпадает с мостом цены")
    variants = (bridge.get("price_results") or {}).get("figaro", {}).get("variants") or {}
    if not variants:
        raise ProcurementError("мост цены: нет вариантов price_results.figaro.variants")
    variant_key = sorted(variants)[0]
    aggregate = number(variants[variant_key].get("results", {}).get("aggregate_price_index_pct"),
                       what="мост.aggregate_price_index_pct")
    elasticity = aggregate / delta_pct  # delta_pct is a fraction (1.2514 = +125,14 %)
    if abs(elasticity - anchors["oil_price_elasticity_pct_per_100"]) > 1e-3:
        raise ProcurementError(f"якорь нефтяной эластичности {elasticity!r} не совпадает с "
                               f"{anchors['oil_price_elasticity_pct_per_100']!r}")
    return {
        "verified": True,
        "feed": {"path": display_path(Path(feed)), "sha256": sha256_file(Path(feed)),
                 "target": target, "point": point, "interval": [low, high]},
        "price_bridge": {"path": display_path(Path(price_bridge)),
                         "sha256": sha256_file(Path(price_bridge)),
                         "scenario": shock.get("scenario"),
                         "brent_delta_pct": delta_pct,
                         "aggregate_price_index_pct": aggregate,
                         "oil_price_elasticity_pct_per_100": r(elasticity, 9),
                         "variant": variant_key},
    }


# ---------------------------------------------------------------------------------------- payload
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


def load_dataset(source) -> dict:
    if source is None or (isinstance(source, str) and source.lower() == "demo"):
        document = demo_dataset()
        return {"dataset": normalize_dataset(document, origin="встроенный демо-набор"),
                "path": None, "sha256": None}
    path = Path(source)
    document = read_json(path, what="набор поставщиков")
    return {"dataset": normalize_dataset(document, origin=display_path(path)),
            "path": display_path(path), "sha256": sha256_file(path)}


def build_report(*, suppliers=None, scenario_id: str = DEFAULT_SCENARIO,
                 weights: dict | None = None, out_dir=None, generated_at: str | None = None,
                 anchors: dict | None = None) -> dict:
    weight_block = validate_weights(weights)
    weights_values = weight_block["values"]
    loaded = load_dataset(suppliers)
    dataset = loaded["dataset"]
    records = dataset["suppliers"]

    if scenario_id not in SCENARIO_BY_ID:
        raise ScenarioError(f"неизвестный сценарий {scenario_id!r}; известны "
                            f"{list(SCENARIO_ORDER)}")
    drivers = scenario_drivers(scenario_id)

    categories = [score_category(category, records, weights_values)
                  for category in CATEGORY_ORDER if category in dataset["categories"]]

    ranking = sorted(
        [{"supplier_id": entry["id"], "name": entry["name"], "category": entry["category"],
          "score": entry["score"], "rank": entry["rank"], "band": entry["band"]["letter"]}
         for category in categories for entry in category["suppliers"]],
        key=lambda item: (-item["score"], item["category"], item["supplier_id"]))

    focus = budget_block(dataset, drivers)
    focus_interval = budget_interval(dataset, drivers)
    scenarios = []
    for ident in SCENARIO_ORDER:
        item_drivers = scenario_drivers(ident)
        block = budget_block(dataset, item_drivers)
        interval = budget_interval(dataset, item_drivers)
        scenarios.append({
            "id": ident,
            "label": item_drivers["label"],
            "drivers": {key: item_drivers[key] for key in
                        ("cpi_pp", "cpi_explicit_pp", "cpi_from_oil_pp", "oil_delta_pct",
                         "oil_price_pct", "freight_delta_pct", "freight_price_pct", "fx_pct",
                         "volume_delta_pct")},
            "base_total_eur": block["base_total_eur"],
            "total_eur": block["total_eur"],
            "total_with_volume_eur": block["total_with_volume_eur"],
            "delta_eur": block["delta_eur"],
            "delta_pct": block["delta_pct"],
            "interval_eur": {"low": interval["low_eur"], "point": interval["point_eur"],
                             "high": interval["high_eur"]},
            "by_category": block["by_category"],
            "focus": ident == scenario_id,
        })

    base_scenario_total = next(item["total_eur"] for item in scenarios if item["id"] == "base")
    base_contract_sum = sum(record["base_amount_eur"] for record in records
                            if record["contract"]["status"] == "active")
    interval_ordered = (focus_interval["low_eur"] <= focus_interval["point_eur"]
                        <= focus_interval["high_eur"])

    weights_ok = all(abs(entry["score"] - sum(entry["contributions"].values())) <= 1e-9
                     for category in categories for entry in category["suppliers"])
    tco_ok = all(
        abs(entry["tco"]["total"] - sum(entry["tco"][key] for key in TCO_KEYS)) <= 1e-9
        for category in categories for entry in category["suppliers"])
    components_ok = all(
        0.0 < entry["components"][key] <= 1.0 + 1e-12
        for category in categories for entry in category["suppliers"]
        for key in WEIGHT_KEYS)

    report = {
        "schema": SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "model_version": MODEL_VERSION,
        "generated_at": generated_at or now(),
        "statement": (
            "Закупочная аналитика FLAKS (UC-13/14/16) на демонстрационном наборе: скоринг "
            "поставщиков с прозрачными весами, TCO, прогноз бюджета и индексации контрактов по "
            "объявленным сценариям. Исследовательский MVP с допущениями, НЕ прогноз, НЕ допуск, "
            "НЕ инвестиционная и НЕ закупочная рекомендация."
        ),
        "dataset": {
            "path": loaded["path"],
            "sha256": loaded["sha256"],
            "schema": dataset["schema"],
            "version": dataset["version"],
            "source_class": dataset["source_class"],
            "currency": dataset["currency"],
            "period": dataset["period"],
            "note": dataset["note"],
            "counts": dataset["counts"],
            "categories": [
                {"id": category, **info} for category, info in dataset["categories"].items()
            ],
            "real_data_allowed": False,
        },
        "weights": {
            "values": weights_values,
            "sum": weight_block["sum"],
            "normalization": NORMALIZATION,
            "rationale": WEIGHT_RATIONALE,
            "override": "--weights price=0.20,tco=0.30,risk=0.20,quality=0.20,delivery=0.10",
        },
        "macro": {
            "schema": MACRO_SCHEMA,
            "anchors": list(MACRO_ANCHORS),
            "derived": {
                "oil_to_cpi_pp_per_pct": OIL_TO_CPI_PP_PER_PCT,
                "oil_price_elasticity_pct_per_100": OIL_PRICE_ELASTICITY_PCT_PER_100,
                "freight_to_price_elasticity": FREIGHT_TO_PRICE_ELASTICITY,
            },
            "risk_cost_rate": RISK_COST_RATE,
            "rework_multiplier": REWORK_MULTIPLIER,
            "late_penalty_rate": LATE_PENALTY_RATE,
            "carrying_rate": CARRYING_RATE,
            "risk_weights": RISK_WEIGHTS,
            "quality_weights": QUALITY_WEIGHTS,
            "anchors_verified": (anchors or {}).get("verified", False),
            "verification": anchors,
        },
        "scenario": {
            "id": drivers["id"],
            "label": drivers["label"],
            "note": drivers["note"],
            "drivers": {key: drivers[key] for key in
                        ("cpi_pp", "cpi_explicit_pp", "cpi_from_oil_pp", "oil_delta_pct",
                         "oil_price_pct", "freight_delta_pct", "freight_price_pct", "fx_pct",
                         "volume_delta_pct")},
            "category_price_changes": [category_price_change(category, drivers)
                                       for category in CATEGORY_ORDER
                                       if category in dataset["categories"]],
        },
        "scoring": {
            "categories": categories,
            "ranking": ranking,
            "bands": [{"letter": letter, "threshold": threshold, "label_ru": label}
                      for letter, threshold, label in BANDS],
        },
        "budget": {
            "focus_scenario": scenario_id,
            "base_total_eur": focus["base_total_eur"],
            "focus_total_eur": focus["total_eur"],
            "focus_total_with_volume_eur": focus["total_with_volume_eur"],
            "focus_volume_effect_eur": focus["volume_effect_eur"],
            "focus_delta_eur": focus["delta_eur"],
            "focus_delta_pct": focus["delta_pct"],
            "interval_eur": focus_interval,
            "scenarios": scenarios,
            "contracts": focus["contracts"],
            "by_category": focus["by_category"],
        },
        "checks": {
            "weights_sum_to_one": abs(weight_block["sum"] - 1.0) <= 1e-9,
            "score_equals_contributions": weights_ok,
            "tco_components_sum": tco_ok,
            "components_in_unit_interval": components_ok,
            "budget_base_equals_contract_sum": abs(base_scenario_total - base_contract_sum)
            <= 1e-6 * max(1.0, base_contract_sum),
            "interval_ordered": interval_ordered,
            "categories_scored": len(categories),
            "suppliers_scored": sum(len(category["suppliers"]) for category in categories),
            "no_raw_data": True,
            "real_data_allowed": False,
            "deterministic": True,
            "fail_closed": True,
            "research_only": True,
            "admission": False,
        },
        "limitations": [
            "набор демонстрационный (source_class=synthetic_demo): поставщики, цены и контракты "
            "вымышлены, реальные данные клиента в репозиторий не коммитятся;",
            "веса скоринга — управленческое допущение, не оценка предпочтений; при их смене "
            "меняется порядок поставщиков (веса публикуются и проверяются на сумму 1);",
            "нормализация «best-in-class ratio» относительна внутри категории: смена состава "
            "участников меняет компоненты, а не абсолютное качество поставщика;",
            "TCO — объявленная формула (логистика, пошлина, риск-премия, качество, поздняя "
            "поставка, замороженный капитал); это не бухгалтерский учёт и не аудит счетов;",
            "риск страны и пошлины — демонстрационные таблицы-допущения, не данные рейтинговых "
            "агентств и не таможенный тариф;",
            "сценарии линейны: CPI→цена 1 пп = 1 %, нефть→цена линеаризована по агрегату моста "
            "цены (+2,364 % на +100 % Brent), большие шаги выходят за линеаризацию;",
            "интервал бюджета — перенос 5–95 прогноза CPI; неопределённость нефти, фрахта и FX "
            "не публикуется и в интервал не входит;",
            "индексация не учитывает переговоры, взаимозачёт, штрафы, оспаривание и юридическую "
            "проверку формулировок договора; cap/floor применяются как объявлено в контракте;",
            "«экономия» считается как перевод всего объёма категории на поставщика с минимальным "
            "TCO и не учитывает концентрацию риска, мощность и квалификацию;",
            "не допуск: admission=false, статус RESEARCH_ONLY; заявлений о точности нет.",
        ],
    }

    problems = scan_for_raw(report)
    if problems:
        raise ProcurementError("в отчёте обнаружены сырые маркеры: " + "; ".join(problems[:5]))
    report["checks"]["no_raw_data"] = not problems

    paths = {"json": None, "markdown": None, "sha256": None, "bytes": None}
    if out_dir is not None:
        target = Path(out_dir) / scenario_id
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / "flaks_procurement.json"
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        json_path.write_bytes(payload.encode("utf-8"))
        md_path = target / "flaks_procurement.md"
        md_path.write_bytes(render_markdown(report).encode("utf-8"))
        paths = {"json": str(json_path), "markdown": str(md_path),
                 "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                 "bytes": json_path.stat().st_size}
    return {"report": report, **paths}


# -------------------------------------------------------------------------------------- markdown
def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def _pct(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f} %".replace(",", " ")


def _table(headers: tuple, rows: list) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def render_markdown(report: dict) -> str:
    scenario = report["scenario"]
    budget = report["budget"]
    categories = report["scoring"]["categories"]
    lines = []
    lines.append("# FLAKS Procurement Intelligence — MVP (UC-13/14/16)")
    lines.append("")
    lines.append(f"Статус: **{report['status']}**, admission=**false**, "
                 f"модель {report['model_version']}, сценарий **{scenario['id']}** "
                 f"({scenario['label']}). generated_at: {report['generated_at']}.")
    lines.append("")
    lines.append("> " + report["statement"])
    lines.append("")
    lines.append("## 1. Резюме для CFO")
    lines.append("")
    top = report["scoring"]["ranking"][0] if report["scoring"]["ranking"] else None
    saving_total = sum(category["savings"]["saving_eur"] for category in categories)
    lines.append(f"- Базовый бюджет действующих контрактов: "
                 f"**{_fmt(budget['base_total_eur'])} EUR**.")
    lines.append(f"- Прогноз при сценарии `{scenario['id']}` (только цены/индексация): "
                 f"**{_fmt(budget['focus_total_eur'])} EUR** ({_pct(budget['focus_delta_pct'])}), "
                 f"интервал 5–95: {_fmt(budget['interval_eur']['low_eur'])} … "
                 f"{_fmt(budget['interval_eur']['high_eur'])} EUR.")
    lines.append(f"- С учётом объёмного эффекта "
                 f"({scenario['drivers']['volume_delta_pct']:+.1f} %): "
                 f"**{_fmt(budget['focus_total_with_volume_eur'])} EUR**.")
    if top:
        lines.append(f"- Лидер общего скоринга: **{top['name']}** (`{top['category']}`), "
                     f"балл {top['score']:.4f}, класс {top['band']}.")
    lines.append(f"- Потенциал «весь объём — поставщику с минимальным TCO»: "
                 f"**{_fmt(saving_total)} EUR** до концентрации риска "
                 f"(иллюстрация, не рекомендация).")
    drivers = scenario["drivers"]
    lines.append(f"- Драйверы сценария: CPI {drivers['cpi_pp']:+.4f} пп, "
                 f"нефть {drivers['oil_delta_pct']:+.1f} %, "
                 f"фрахт {drivers['freight_delta_pct']:+.1f} %, "
                 f"FX {drivers['fx_pct']:+.1f} %, "
                 f"объём {drivers['volume_delta_pct']:+.1f} %.")
    lines.append("")
    lines.append("## 2. Скоринг поставщиков")
    lines.append("")
    lines.append("Веса: " + ", ".join(f"`{key}`={value:.2f}"
                                     for key, value in report["weights"]["values"].items())
                 + " (сумма 1.0). Нормализация — best-in-class внутри категории.")
    lines.append("")
    for category in categories:
        lines.append(f"### {category['name_ru']} (`{category['id']}`)")
        lines.append("")
        rows = []
        for entry in category["suppliers"]:
            rows.append((entry["rank"], entry["name"], entry["country"],
                         f"{entry['score']:.4f}", entry["band"]["letter"],
                         _fmt(entry["unit_price"], 2), _fmt(entry["tco"]["total"], 4),
                         f"{entry['risk_composite']:.3f}", f"{entry['otif']:.3f}",
                         f"{entry['lead_time_days']:.0f}",
                         entry["contract"]["status"]))
        lines.extend(_table(("#", "Поставщик", "Страна", "Балл", "Класс", "Цена/ед.",
                             "TCO/ед.", "Риск", "OTIF", "Лид, дн.", "Контракт"), rows))
        lines.append("")
        rows = []
        for entry in category["suppliers"]:
            rows.append((entry["name"],
                         f"{entry['components']['price']:.3f}",
                         f"{entry['components']['tco']:.3f}",
                         f"{entry['components']['risk']:.3f}",
                         f"{entry['components']['quality']:.3f}",
                         f"{entry['components']['delivery']:.3f}",
                         " + ".join(f"{entry['contributions'][key]:.4f}"
                                    for key in WEIGHT_KEYS),
                         f"{entry['score']:.4f}"))
        lines.extend(_table(("Поставщик", "Цена", "TCO", "Риск", "Кач-во", "Срок",
                             "Вклад (пп: цена+TCO+риск+кач-во+срок)", "Итог"), rows))
        lines.append("")
        lines.append(f"Минимальная цена: `{category['best_price_supplier']}`; минимальный TCO: "
                     f"`{category['best_tco_supplier']}`; рекомендация скоринга: "
                     f"`{category['recommended']['supplier_id']}`. "
                     f"Экономия при переводе объёма на min-TCO: "
                     f"{_fmt(category['savings']['saving_eur'])} EUR "
                     f"({category['savings']['saving_pct']:.2f} %) — без учёта концентрации риска.")
        lines.append("")

    lines.append("## 3. TCO: из чего складывается")
    lines.append("")
    lines.append("`TCO/ед. = цена + логистика + пошлина + риск + качество + поздняя поставка + "
                 "замороженный капитал`.")
    lines.append("")
    for category in categories:
        lines.append(f"### {category['name_ru']}")
        lines.append("")
        rows = []
        for entry in category["suppliers"]:
            tco = entry["tco"]
            rows.append((entry["name"], _fmt(tco["price"], 4), _fmt(tco["logistics"], 4),
                         _fmt(tco["duty"], 4), _fmt(tco["risk"], 4), _fmt(tco["quality"], 4),
                         _fmt(tco["late"], 4), _fmt(tco["carrying"], 4),
                         _fmt(tco["total"], 4), _fmt(entry["annual_volume"], 0)))
        lines.extend(_table(("Поставщик", "Цена", "Логистика", "Пошлина", "Риск", "Качество",
                             "Поздняя", "Капитал", "TCO/ед.", "Объём"), rows))
        lines.append("")

    lines.append("## 4. Бюджет закупок по сценариям")
    lines.append("")
    rows = []
    for item in budget["scenarios"]:
        marker = "◀" if item["focus"] else ""
        rows.append((item["id"] + (" " + marker if marker else ""),
                     f"{item['drivers']['cpi_pp']:+.3f}", f"{item['drivers']['oil_delta_pct']:+.1f}",
                     f"{item['drivers']['freight_delta_pct']:+.1f}",
                     _fmt(item["total_eur"]), _fmt(item["delta_eur"]),
                     _pct(item["delta_pct"]), _fmt(item["total_with_volume_eur"]),
                     f"{_fmt(item['interval_eur']['low'])} … {_fmt(item['interval_eur']['high'])}"))
    lines.extend(_table(("Сценарий", "ΔCPI, пп", "Δнефть, %", "Δфрахт, %", "Бюджет (цены), EUR",
                         "Δ, EUR", "Δ, %", "С объёмом, EUR", "Интервал 5–95, EUR"), rows))
    lines.append("")
    lines.append(f"Объёмный эффект считается отдельно: `(база + индексация) · (1 + Δобъём)`, "
                 f"чтобы ценовой шок и изменение закупки не смешивались.")
    lines.append("")
    lines.append("### Цена категории под сценарием `" + scenario["id"] + "`")
    lines.append("")
    rows = [(item["name_ru"], f"{item['incidences']['cpi']:.2f}", f"{item['incidences']['oil']:.2f}",
             f"{item['incidences']['freight']:.2f}",
             f"{item['components_pct']['cpi']:+.3f}", f"{item['components_pct']['oil']:+.3f}",
             f"{item['components_pct']['freight']:+.3f}", f"{item['price_change_pct']:+.3f}")
            for item in scenario["category_price_changes"]]
    lines.extend(_table(("Категория", "Инцидентность CPI", "нефть", "фрахт", "Вклад CPI, %",
                         "Вклад нефти, %", "Вклад фрахта, %", "Цена, %"), rows))
    lines.append("")

    lines.append("## 5. Индексация контрактов (сценарий `" + scenario["id"] + "`)")
    lines.append("")
    rows = []
    for item in budget["contracts"]:
        flags = ",".join(item["flags"]) if item["flags"] else "—"
        rows.append((item["contract_id"], item["supplier_id"], item["category"],
                     f"{item['shares']['cpi']:.2f}/{item['shares']['ppi']:.2f}/"
                     f"{item['shares']['fx']:.2f}/{item['shares']['fixed']:.2f}",
                     _fmt(item["base_eur"]), f"{item['raw_pct']:+.3f}",
                     f"{item['cap_pct']:.1f}", f"{item['applied_pct']:+.3f}",
                     _fmt(item["indexation_eur"]), _fmt(item["forecast_eur"]),
                     _fmt(item["forecast_with_volume_eur"]), flags))
    lines.extend(_table(("Договор", "Поставщик", "Категория", "CPI/PPI/FX/фикс",
                         "База, EUR", "Сырой %", "Cap, %", "Применён %", "Индексация, EUR",
                         "Прогноз (цены), EUR", "С объёмом, EUR", "Флаги"), rows))
    lines.append("")
    lines.append(f"Итого: база {_fmt(budget['base_total_eur'])} EUR, прогноз по ценам "
                 f"{_fmt(budget['focus_total_eur'])} EUR, изменение "
                 f"{_fmt(budget['focus_delta_eur'])} EUR ({_pct(budget['focus_delta_pct'])}); "
                 f"с объёмным эффектом — {_fmt(budget['focus_total_with_volume_eur'])} EUR.")
    lines.append("")

    lines.append("## 6. Проверки")
    lines.append("")
    policy_keys = {"real_data_allowed", "admission", "research_only", "deterministic",
                   "fail_closed", "no_raw_data"}
    rows = [(key, "OK" if value else "FAIL")
            for key, value in report["checks"].items()
            if isinstance(value, bool) and key not in policy_keys]
    lines.extend(_table(("Проверка", "Результат"), rows))
    lines.append("")
    lines.append(f"Политика: статус {report['status']}, admission=false, реальные данные "
                 f"запрещены (source_class=`{report['dataset']['source_class']}`), "
                 f"fail-closed=OK, детерминизм=OK.")
    lines.append("")
    lines.append(f"Оценено: категорий — {report['checks']['categories_scored']}, "
                 f"поставщиков — {report['checks']['suppliers_scored']}.")
    lines.append("")
    lines.append("## 7. Ограничения")
    lines.append("")
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 8. Воспроизводимость")
    lines.append("")
    lines.append("```powershell")
    lines.append("python tools/flaks_procurement.py --scenario " + scenario["id"]
                 + " --report build/flaks_procurement --generated-at <UTC>")
    lines.append("python -m unittest tests.test_flaks_procurement -v")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- CLI
def parse_weights(text: str | None) -> dict | None:
    if not text:
        return None
    values = {}
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        if "=" not in chunk:
            raise WeightsError(f"вес {chunk!r}: ожидался формат key=value")
        key, _, raw = chunk.partition("=")
        key = key.strip()
        try:
            values[key] = float(raw)
        except ValueError as error:
            raise WeightsError(f"вес {key!r}: не число ({raw!r})") from error
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FLAKS procurement MVP: supplier scoring, TCO, budget, contract indexation")
    parser.add_argument("--suppliers", default="demo",
                        help="путь к JSON-набору поставщиков или 'demo' (встроенный синтетический)")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO,
                        help=f"сценарий из {list(SCENARIO_ORDER)}")
    parser.add_argument("--weights", default=None,
                        help="веса скоринга, например price=0.2,tco=0.3,risk=0.2,quality=0.2,"
                             "delivery=0.1")
    parser.add_argument("--report", default=None,
                        help="каталог отчётов (по умолчанию build/flaks_procurement)")
    parser.add_argument("--dry-run", action="store_true", help="не писать файлы")
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--verify-anchors", action="store_true",
                        help="сверить якоря CPI/нефти с build/-артефактами (fail-closed)")
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--price-bridge", default=str(DEFAULT_PRICE_BRIDGE))
    parser.add_argument("--dump-suppliers", action="store_true",
                        help="напечатать демо-набор и выйти")
    parser.add_argument("--json", action="store_true", help="напечатать полный отчёт")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.dump_suppliers:
        print(json.dumps(demo_dataset(), ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    out_dir = None
    if not args.dry_run:
        out_dir = Path(args.report) if args.report else DEFAULT_OUT
    try:
        anchors = None
        if args.verify_anchors:
            anchors = verify_anchors(Path(args.feed), Path(args.price_bridge))
        outcome = build_report(
            suppliers=args.suppliers, scenario_id=args.scenario,
            weights=parse_weights(args.weights), out_dir=out_dir,
            generated_at=args.generated_at, anchors=anchors)
    except ProcurementError as error:
        print(json.dumps({"status": "BLOCKED_INPUT", "error": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report = outcome["report"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    summary = {
        "status": report["status"],
        "schema": report["schema"],
        "scenario": report["scenario"]["id"],
        "report": outcome["json"],
        "markdown": outcome["markdown"],
        "sha256": outcome["sha256"],
        "bytes": outcome["bytes"],
        "suppliers": report["dataset"]["counts"]["suppliers"],
        "categories": report["dataset"]["counts"]["categories"],
        "base_total_eur": report["budget"]["base_total_eur"],
        "forecast_total_eur": report["budget"]["focus_total_eur"],
        "delta_pct": report["budget"]["focus_delta_pct"],
        "checks": {"no_raw_data": report["checks"]["no_raw_data"],
                   "fail_closed": report["checks"]["fail_closed"],
                   "budget_base_equals_contract_sum":
                       report["checks"]["budget_base_equals_contract_sum"]},
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
