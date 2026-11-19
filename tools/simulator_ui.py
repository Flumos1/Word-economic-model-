#!/usr/bin/env python3
"""Simulator UI MVP: local web demo over the **existing** AMFE models (stdlib only).

What it is
----------
A single-process demo server (``http.server``, vanilla HTML/JS, no external dependency, no CDN) with
one form: scenario (Hormuz base / Hormuz stress / gas shock / geopolitics), parameters (price, shock,
country) and a "Рассчитать" button.  ``/api/run`` calls the models that already exist in the project
and returns **aggregates only**:

* ``hormuz_base`` / ``hormuz_stress`` — ``amfe.markets.oil`` (``oil.brent``) price path translated by
  ``tools/hormuz_us_impact.py`` into US pump prices, CPI pp, household cost and an illustrative social
  risk index; sector cost multipliers come from ``OilModule.prepare_cost_shock``;
* ``gas_shock`` — ``amfe.markets.gas`` (``gas.lng_eu``): TTF/JKM paths, storage, LNG diversion;
* ``geopolitics`` — the MRIO geopolitics scenario report (``tools/geopolitics_scenarios.py``), read
  from ``build/geopolitics/report.json`` when it exists, otherwise recomputed on the declared
  synthetic fixture (``--geopolitics synthetic``).

Design rules (the same ones the rest of the project uses)
---------------------------------------------------------
* **Research only.**  Every document carries ``status=RESEARCH_ONLY``, ``admission=false`` and
  ``causal_claim=false`` plus a disclaimer: a demo scenario is not a forecast.
* **No raw data.**  Only declared assumptions, model aggregates and at most 12 path points are
  served; the payload is scanned (forbidden keys, filesystem paths, URLs, oversized lists) and the
  request is refused instead of leaking anything.
* **Fail-closed.**  An unknown scenario, an unknown/malformed parameter, a non-finite number, an
  out-of-range value, a bad country code or an unreadable body is a 4xx JSON error; a module failure
  is a 503.  Nothing is "fixed up" silently.
* **Deterministic.**  No clock, no randomness, no per-request caches keyed by time: two identical
  requests produce byte-identical JSON (and the same SHA-256 digest).

Usage
-----
    python tools/simulator_ui.py                     # http://127.0.0.1:8090
    python tools/simulator_ui.py --host 0.0.0.0 --port 8091
    python tools/simulator_ui.py --check             # print diagnostics and exit
    python tools/simulator_ui.py --geopolitics synthetic   # ignore the precomputed MRIO artifact

Tests: ``python tests/test_simulator_ui.py`` (see docs/192).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.markets import get_module  # noqa: E402
from amfe.markets.contract import MarketError  # noqa: E402
from amfe.markets.gas import build_report as build_gas_report  # noqa: E402

import hormuz_us_impact as hormuz_impact  # noqa: E402

SCHEMA = "amfe.simulator.ui.v1"
TOOL_VERSION = "1.0.0"
STATUS = "RESEARCH_ONLY"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_GEOPOLITICS_ARTIFACT = ROOT / "build" / "geopolitics" / "report.json"
DEFAULT_GEOPOLITICS_SCENARIO = "CHK-ORMUZ-2026"
MAX_BODY_BYTES = 64 * 1024
MAX_TARGET_CHARS = 2048
MAX_PATH_POINTS = 12
MAX_LIST_ITEMS = 128
MAX_STRING_CHARS = 4000
LOGGER = logging.getLogger("amfe.simulator_ui")

DISCLAIMER = (
    "Демонстрационный сценарий, а не прогноз: это сценарная арифметика существующих моделей AMFE "
    "на объявленных допущениях (status=RESEARCH_ONLY, admission=false, causal_claim=false). "
    "Диапазоны — оболочка вариантов чувствительности, а не доверительный интервал; числа не "
    "являются допуском, рекомендацией или оценкой вероятности."
)

#: Keys that must never appear in a served document (raw/third-party observations).
FORBIDDEN_KEYS = frozenset({
    "raw", "raw_data", "raw_values", "observations", "observed_values", "source_rows",
    "source_series", "series", "series_values", "third_party_data", "data_points",
    "vintage_dates", "used_origins", "actual_values", "forecast_values",
})
DRIVE_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_.\-])[A-Za-z]:[\\/]")
POSIX_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_.\-])/(?:mnt|home|Users|var|etc|opt|tmp|root)/")
EXTERNAL_URL_RE = re.compile(r"\b(?:https?|ftp|file)://", re.IGNORECASE)
COUNTRY_RE = re.compile(r"^[A-Z]{2,3}$")
SCENARIO_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,60}$")


# --------------------------------------------------------------------------------------------- errors
class SimulatorError(Exception):
    """A request cannot be served; carries an HTTP status, a stable code and safe details."""

    def __init__(self, status: int, code: str, message: str,
                 details: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.headers = dict(headers or {})


class BadRequest(SimulatorError):
    def __init__(self, code: str, message: str, details=None,
                 headers: Mapping[str, str] | None = None) -> None:
        declared = dict(headers or {})
        if code == "METHOD_NOT_ALLOWED":
            declared.setdefault("Allow", "GET, OPTIONS")
        super().__init__(400, code, message, details, headers=declared)


class NotFound(SimulatorError):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(404, code, message, details)


class PolicyViolation(SimulatorError):
    """The document cannot be served: raw data, a path or a URL would leak."""

    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(503, code, message, details)


class Unavailable(SimulatorError):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(503, code, message, details)


# ---------------------------------------------------------------------------------------- scenarios
SCENARIOS: tuple[dict, ...] = (
    {
        "id": "hormuz_base",
        "title": "Ормуз: закрытие 6 месяцев (base)",
        "family": "oil",
        "model": "oil.brent + hormuz_us_impact",
        "model_scenario": "hormuz_closed_6m",
        "stress": False,
        "summary": "Полное закрытие Ормуза на 6 месяцев: обходные трубопроводы, ответ предложения "
                   "и совместный выпуск стратегических/коммерческих запасов.",
        "params": ("price", "shock", "country"),
    },
    {
        "id": "hormuz_stress",
        "title": "Ормуз: закрытие + стресс-надбавки",
        "family": "oil",
        "model": "oil.brent + hormuz_us_impact (stress)",
        "model_scenario": "hormuz_closed_6m",
        "stress": True,
        "summary": "То же закрытие плюс объявленные стресс-факторы: сланцевый слэт, потеря "
                   "венесуэльской тяжёлой нефти, фрахт/страховка, обходной маршрут, крек дизеля и "
                   "авиакеросина.",
        "params": ("price", "shock", "country"),
    },
    {
        "id": "gas_shock",
        "title": "Газ: холодная зима + потеря трубопроводов",
        "family": "gas",
        "model": "gas.lng_eu",
        "model_scenario": "hard_winter_pipeline_shock_6m",
        "stress": False,
        "summary": "Спрос +10 % (холодная зима) и потеря трубопроводных поставок в ЕС; ПХГ "
                   "покрывают объявленную долю разрыва, остаток закрывается ценой и импортом СПГ.",
        "params": ("price", "shock", "country"),
    },
    {
        "id": "geopolitics",
        "title": "Геополитика: Ормуз 2026 (MRIO, страна×отрасль)",
        "family": "geopolitics",
        "model": "geopolitics_scenarios (FIGARO MRIO)",
        "model_scenario": DEFAULT_GEOPOLITICS_SCENARIO,
        "stress": False,
        "summary": "Сравнительная статика на MRIO: сокращение межстрановых коэффициентов на "
                   "объявленном направлении и пересчёт выпуска по странам и отраслям.",
        "params": ("country", "geo_scenario"),
    },
)
SCENARIO_BY_ID = {spec["id"]: spec for spec in SCENARIOS}

#: Numeric parameters per family: label, unit, range and default (all validated fail-closed).
PARAM_SPECS: dict[str, dict[str, dict]] = {
    "oil": {
        "price": {"label": "Brent, база", "unit": "USD/bbl", "min": 20.0, "max": 300.0,
                  "step": 1.0, "default": 75.0,
                  "help": "референсный уровень Brent на старте сценария (параметр модуля base_price)"},
        "shock": {"label": "Потеря потока Ормуза", "unit": "%", "min": 0.0, "max": 100.0,
                  "step": 5.0, "default": 100.0,
                  "help": "доля потока пролива, которая теряется (closure_share модуля нефти)"},
    },
    "gas": {
        "price": {"label": "TTF, база", "unit": "EUR/MWh", "min": 0.5, "max": 300.0,
                  "step": 0.5, "default": 34.0,
                  "help": "базовая цена TTF (ttf_price_eur_per_mwh модуля газа)"},
        "shock": {"label": "Потеря трубопроводов", "unit": "bcm/год", "min": 0.0, "max": 150.0,
                  "step": 5.0, "default": 45.0,
                  "help": "потеря трубопроводных поставок в ЕС (pipeline_loss_bcm_y)"},
    },
}
COUNTRY_DEFAULT = {"oil": "US", "gas": "DE", "geopolitics": "US"}
COUNTRY_NAMES = {
    "US": "США", "DE": "Германия", "FR": "Франция", "IT": "Италия", "ES": "Испания",
    "PL": "Польша", "NL": "Нидерланды", "BE": "Бельгия", "UK": "Великобритания",
    "NO": "Норвегия", "JP": "Япония", "CN": "Китай", "IN": "Индия", "KR": "Корея",
    "BR": "Бразилия", "CA": "Канада", "MX": "Мексика", "RU": "Россия", "SA": "Саудовская Аравия",
    "TR": "Турция", "AU": "Австралия", "ROW": "остальной мир",
}

OIL_LIMITATIONS = (
    "нефтяной модуль — RESEARCH_ONLY и не калиброван вне выборки; числа условные",
    "pass-through в цены топлива, веса CPI-U и чувствительность одобрения — объявленные допущения, "
    "а не оценки",
    "в ценовом пути нет замещения, запасов, ограничений переработки и реакции политики",
    "социальный риск — иллюстративный индекс на объявленных порогах, не вероятность беспорядков",
    "шаг — один месяц; пик CPI может отставать от пика нефти на 1–3 месяца",
)
GAS_LIMITATIONS = (
    "газовый модуль — исследовательский прототип на объявленных допущениях; данных, погоды и "
    "ПХГ по странам нет",
    "цены TTF/JKM/Henry Hub — сценарные, а не рыночные котировки",
    "отраслевого моста и страновой разбивки у газового модуля нет",
)
GEOPOLITICS_LIMITATIONS = (
    "сравнительная статика MRIO: нет цен, замещения, динамики и обратных связей; числа относятся к "
    "пику шока",
    "барьерные каналы реализованы как сокращение межстрановых коэффициентов; физические мощности, "
    "фрахт и запасы не моделируются",
    "направление — знак агрегированного изменения выпуска, а не оценка благосостояния",
)


# ----------------------------------------------------------------------------------------- utilities
def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _json_number(value: Any, digits: int = 4) -> float:
    """Coerce to a JSON-safe finite float (numpy scalars from the MRIO layer included)."""
    number = float(value)
    if not math.isfinite(number):
        raise Unavailable("NON_FINITE_VALUE", "модель вернула нечисловое значение")
    return _round(number, digits)


def _scan_aggregate(value: Any, where: str, depth: int = 0) -> None:
    """Fail-closed scan: no raw observations, no filesystem paths, no URLs, no giant blobs."""
    if depth > 12:
        raise PolicyViolation("NOT_AGGREGATE", f"{where}: слишком глубокая вложенность")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                raise PolicyViolation("RAW_DATA_BLOCKED", f"{where}: запрещённое поле {key!r}")
            _scan_aggregate(item, f"{where}.{key}", depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST_ITEMS:
            raise PolicyViolation("NOT_AGGREGATE",
                                  f"{where}: список из {len(value)} элементов не является агрегатом")
        for index, item in enumerate(value):
            _scan_aggregate(item, f"{where}[{index}]", depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise PolicyViolation("NOT_AGGREGATE", f"{where}: строка слишком длинная")
        if DRIVE_PATH_RE.search(value) or POSIX_PATH_RE.search(value):
            raise PolicyViolation("PATH_LEAK", f"{where}: локальный путь в документе")
        if EXTERNAL_URL_RE.search(value):
            raise PolicyViolation("URL_LEAK", f"{where}: внешняя ссылка в документе")
        return


def _coerce_number(name: str, raw: Any, spec: Mapping) -> float:
    if isinstance(raw, bool) or raw is None:
        raise BadRequest("INVALID_PARAM", f"параметр {name!r} должен быть числом")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip().replace(",", "."))
        except ValueError:
            raise BadRequest("INVALID_PARAM", f"параметр {name!r} не число: {raw!r}") from None
    else:
        raise BadRequest("INVALID_PARAM", f"параметр {name!r} должен быть числом")
    if not math.isfinite(value):
        raise BadRequest("INVALID_PARAM", f"параметр {name!r} должен быть конечным числом")
    if not (float(spec["min"]) <= value <= float(spec["max"])):
        raise BadRequest(
            "PARAM_OUT_OF_RANGE",
            f"параметр {name!r} вне диапазона {spec['min']}..{spec['max']} {spec['unit']}",
            {"param": name, "min": spec["min"], "max": spec["max"], "unit": spec["unit"]})
    return _round(value, 6)


def _coerce_country(raw: Any, default: str) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if not isinstance(raw, str):
        raise BadRequest("INVALID_COUNTRY", "страна должна быть кодом страны, например US")
    code = raw.strip().upper()
    if not COUNTRY_RE.fullmatch(code):
        raise BadRequest("INVALID_COUNTRY",
                         f"страна должна быть кодом из 2–3 латинских букв, получено {raw!r}",
                         {"example": "US"})
    return code


def _coerce_scenario_id(raw: Any, default: str) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if not isinstance(raw, str) or not SCENARIO_ID_RE.fullmatch(raw.strip()):
        raise BadRequest("INVALID_SCENARIO_ID", f"некорректный id сценария: {raw!r}")
    return raw.strip()


def _card(key: str, label: str, value: Any, unit: str = "", note: str = "",
          emphasis: bool = False) -> dict:
    card = {"key": key, "label": label, "value": value, "unit": unit, "note": note}
    if emphasis:
        card["emphasis"] = True
    return card


def _series(values: list, digits: int = 2) -> list:
    return [_json_number(value, digits) for value in values]


@dataclass(frozen=True)
class Response:
    """One HTTP response: a JSON payload, an HTML page or an empty body."""

    status: int
    payload: Any
    content_type: str = "application/json; charset=utf-8"
    headers: tuple = field(default=())


# ------------------------------------------------------------------------------------------- the app
class SimulatorApp:
    """Scenario dispatcher and document builder (stateless per request, deterministic)."""

    def __init__(self, geopolitics_artifact: Path | str = DEFAULT_GEOPOLITICS_ARTIFACT,
                 geopolitics_mode: str = "auto") -> None:
        self.geopolitics_artifact = Path(geopolitics_artifact)
        self.geopolitics_mode = str(geopolitics_mode)
        if self.geopolitics_mode not in ("auto", "artifact", "synthetic"):
            raise ValueError("geopolitics_mode must be auto, artifact or synthetic")
        self._lock = threading.Lock()
        self._artifact_state: dict | None = None
        self._synthetic_cache: dict[str, dict] = {}

    # -- public surface ---------------------------------------------------------------------
    def health(self) -> dict:
        artifact = self._artifact()
        return {
            "schema": SCHEMA,
            "kind": "health",
            "status": STATUS,
            "admission": False,
            "causal_claim": False,
            "version": TOOL_VERSION,
            "disclaimer": DISCLAIMER,
            "scenarios": [self._scenario_description(spec) for spec in SCENARIOS],
            "countries": [{"code": code, "name": COUNTRY_NAMES.get(code, code)}
                          for code in self._country_codes(artifact)],
            "geopolitics": {
                "mode": self.geopolitics_mode,
                "artifact": {
                    "available": bool(artifact),
                    "path": "build/geopolitics/report.json",
                    "sha256": artifact["sha256"] if artifact else None,
                },
                "scenarios": artifact["catalogue"] if artifact else [],
                "default_scenario": DEFAULT_GEOPOLITICS_SCENARIO,
            },
            "checks": {
                "oil_module": True,
                "gas_module": True,
                "geopolitics_artifact": bool(artifact),
                "aggregate_only": True,
                "deterministic": True,
            },
            "limits": {"max_body_bytes": MAX_BODY_BYTES, "max_path_points": MAX_PATH_POINTS},
            "limitations": list(OIL_LIMITATIONS[:2]) + list(GEOPOLITICS_LIMITATIONS[:1]),
        }

    def scenarios_document(self) -> dict:
        return {
            "schema": SCHEMA,
            "kind": "scenarios",
            "status": STATUS,
            "admission": False,
            "causal_claim": False,
            "scenarios": [self._scenario_description(spec) for spec in SCENARIOS],
        }

    def run(self, payload: Any) -> dict:
        """Validate a request and build one deterministic result document."""
        if not isinstance(payload, Mapping):
            raise BadRequest("INVALID_BODY", "тело запроса должно быть JSON-объектом")
        scenario_id = payload.get("scenario") or SCENARIOS[0]["id"]
        if not isinstance(scenario_id, str) or scenario_id not in SCENARIO_BY_ID:
            raise BadRequest("UNKNOWN_SCENARIO",
                             f"неизвестный сценарий {scenario_id!r}",
                             {"available": [spec["id"] for spec in SCENARIOS]})
        spec = SCENARIO_BY_ID[scenario_id]
        family = spec["family"]
        params = self._validated_params(spec, payload)
        if family == "oil":
            body = self._run_oil(spec, params)
        elif family == "gas":
            body = self._run_gas(spec, params)
        else:
            body = self._run_geopolitics(spec, params)
        return self._finalize(spec, params, body)

    def page(self) -> str:
        return PAGE_HTML.replace("__VERSION__", TOOL_VERSION)

    # -- parameter handling -----------------------------------------------------------------
    def _scenario_description(self, spec: Mapping) -> dict:
        family = spec["family"]
        numeric = PARAM_SPECS.get(family, {})
        defaults = {name: float(item["default"]) for name, item in numeric.items()}
        defaults.setdefault("country", COUNTRY_DEFAULT[family])
        if family == "geopolitics":
            defaults.setdefault("geo_scenario", spec["model_scenario"])
        return {
            "id": spec["id"],
            "title": spec["title"],
            "family": family,
            "model": spec["model"],
            "model_scenario": spec["model_scenario"],
            "summary": spec["summary"],
            "params": {name: dict(value) for name, value in numeric.items()},
            "defaults": defaults,
            "country_default": COUNTRY_DEFAULT[family],
        }

    def _validated_params(self, spec: Mapping, payload: Mapping) -> dict:
        family = spec["family"]
        numeric = PARAM_SPECS.get(family, {})
        params: dict[str, Any] = {"applied": {}, "notes": []}
        for name, param_spec in numeric.items():
            raw = payload.get(name, param_spec["default"])
            params[name] = _coerce_number(name, raw, param_spec)
        if family == "oil":
            params["applied"] = {"base_price": params["price"],
                                 "closure_share": _round(params["shock"] / 100.0, 6)}
        elif family == "gas":
            params["applied"] = {"ttf_price_eur_per_mwh": params["price"],
                                 "pipeline_loss_bcm_y": params["shock"]}
        params["country"] = _coerce_country(payload.get("country"), COUNTRY_DEFAULT[family])
        if family == "geopolitics":
            params["geo_scenario"] = _coerce_scenario_id(payload.get("geo_scenario"),
                                                         spec["model_scenario"])
        else:
            ignored = [name for name in ("geo_scenario",) if payload.get(name) not in (None, "")]
            if ignored:
                params["notes"].append("параметры вне сценария проигнорированы: "
                                       + ", ".join(ignored))
        return params

    def _finalize(self, spec: Mapping, params: Mapping, body: dict) -> dict:
        document = {
            "schema": SCHEMA,
            "kind": "result",
            "status": STATUS,
            "admission": False,
            "causal_claim": False,
            "disclaimer": DISCLAIMER,
            "scenario": {
                "id": spec["id"],
                "title": spec["title"],
                "family": spec["family"],
                "model": spec["model"],
                "model_scenario": body.pop("model_scenario", spec["model_scenario"]),
                "status": STATUS,
                "digest": body.pop("model_digest", None),
            },
            "parameters": {
                "scenario": spec["id"],
                "country": params["country"],
                "geo_scenario": params.get("geo_scenario"),
                "values": {name: params[name] for name in PARAM_SPECS.get(spec["family"], {})},
                "applied": params.get("applied", {}),
                "notes": params.get("notes", []),
            },
            **body,
        }
        document["digest"] = _digest(document)
        _scan_aggregate(document, "document")
        if document["schema"] != SCHEMA or document["status"] != STATUS:
            raise Unavailable("SELF_CHECK_FAILED", "документ не RESEARCH_ONLY")
        if document["admission"] is not False or document["causal_claim"] is not False:
            raise Unavailable("SELF_CHECK_FAILED", "документ не admission=false")
        return document

    # -- oil family -------------------------------------------------------------------------
    def _run_oil(self, spec: Mapping, params: Mapping) -> dict:
        module = get_module("oil.brent")
        anchor = float(params["price"])
        closure = float(params["applied"]["closure_share"])
        try:
            result = module.run(spec["model_scenario"], base_price=anchor, closure_share=closure)
        except MarketError as error:
            raise Unavailable("MODEL_FAILED", f"нефтяной модуль отказал: {error}") from None

        summary = result.summary
        peak_range = summary["brent_range_usd_per_bbl"]
        range_tuple = (float(peak_range["low"]), float(peak_range["central"]),
                       float(peak_range["high"]))
        assumptions = dict(hormuz_impact.ASSUMPTIONS)
        assumptions["baseline_brent_usd"] = {
            "value": anchor,
            "note": "референс пользователя (демо-параметр) вместо объявленных 75 USD/bbl",
        }
        rows = [hormuz_impact._consumer_block(float(row["price"]), range_tuple, assumptions)
                for row in summary["monthly_path"]]
        if not rows:
            raise Unavailable("MODEL_FAILED", "нефтяной модуль вернул пустой путь")
        peak = max(rows, key=lambda item: item["gasoline_usd_gal"])
        risk = hormuz_impact._risk_block(peak)
        stress = None
        if spec["stress"]:
            # _stress_block measures the delta from the declared 75 USD/bbl anchor; shifting the
            # input price keeps the user's reference price as the anchor (same formulas, no drift).
            shifted = dict(peak)
            shifted["brent_usd_bbl"] = _round(peak["brent_usd_bbl"] - (anchor - 75.0), 4)
            stress = hormuz_impact._stress_block({"peak": shifted})

        shock = module.prepare_cost_shock(result, pass_through=0.6)
        sector_rows = [
            {"bucket": name,
             "cost_increase_pct": _round(100.0 * (float(multiplier) - 1.0), 3),
             "multiplier": _round(multiplier, 6)}
            for name, multiplier in sorted(shock.sector_cost_multipliers.items())
        ]
        labels = [f"М{index}" for index in range(1, len(rows) + 1)]
        cards = [
            _card("brent_peak", "Brent, пик", peak["brent_usd_bbl"], "USD/bbl",
                  f"среднее {summary['price_mean']:.1f}; диапазон вариантов "
                  f"{range_tuple[0]:.1f}–{range_tuple[2]:.1f}", emphasis=True),
            _card("gasoline_peak", "Бензин, пик", peak["gasoline_usd_gal"], "USD/gal",
                  f"диапазон {peak['gasoline_range_usd_gal']['low']}–"
                  f"{peak['gasoline_range_usd_gal']['high']}", emphasis=True),
            _card("diesel_peak", "Дизель, пик", peak["diesel_usd_gal"], "USD/gal",
                  f"+{peak['diesel_delta_pct']} % к базе"),
            _card("cpi_headline", "CPI, п.п.", peak["cpi_headline_pp"], "п.п.",
                  "вклад в годовую инфляцию США", emphasis=True),
            _card("household", "Домохозяйство", peak["household_monthly_cost_usd"], "USD/мес",
                  f"год: {peak['household_annual_cost_usd']} USD"),
            _card("risk", "Соц. риск", risk["score_0_100"], "из 100", f"полоса: {risk['band']}",
                  emphasis=True),
        ]
        if stress:
            cards.extend([
                _card("stress_diesel", "Дизель (стресс)", stress["diesel_usd_gal"], "USD/gal",
                      f"+{stress['diesel_delta_pct']} %"),
                _card("stress_jet", "Авиакеросин (стресс)", stress["jet_usd_gal"], "USD/gal",
                      f"+{stress['jet_delta_pct']} %"),
                _card("stress_risk", "Риск (стресс)",
                      stress["social_political"]["score_0_100"], "из 100",
                      f"полоса: {stress['social_political']['band']}"),
            ])
        charts = [
            {"kind": "line", "title": "Brent по месяцам", "unit": "USD/bbl", "labels": labels,
             "lines": [{"label": "Brent", "values": _series([row["brent_usd_bbl"] for row in rows])}]},
            {"kind": "line", "title": "Моторное топливо по месяцам", "unit": "USD/gal",
             "labels": labels,
             "lines": [
                 {"label": "Бензин", "values": _series([row["gasoline_usd_gal"] for row in rows])},
                 {"label": "Дизель", "values": _series([row["diesel_usd_gal"] for row in rows])},
             ]},
        ]
        table = {
            "title": "Цены и потребительский эффект по месяцам",
            "columns": [
                {"key": "month", "label": "Месяц", "unit": "", "digits": 0},
                {"key": "brent", "label": "Brent", "unit": "USD/bbl", "digits": 2},
                {"key": "gasoline", "label": "Бензин", "unit": "USD/gal", "digits": 2},
                {"key": "diesel", "label": "Дизель", "unit": "USD/gal", "digits": 2},
                {"key": "cpi", "label": "CPI", "unit": "п.п.", "digits": 2},
                {"key": "household", "label": "Домохозяйство", "unit": "USD/мес", "digits": 2},
            ],
            "rows": [
                {"month": index, "brent": row["brent_usd_bbl"], "gasoline": row["gasoline_usd_gal"],
                 "diesel": row["diesel_usd_gal"], "cpi": row["cpi_headline_pp"],
                 "household": row["household_monthly_cost_usd"]}
                for index, row in enumerate(rows, start=1)
            ],
        }
        details: dict[str, Any] = {
            "oil_module": {
                "price_peak_usd_bbl": _round(summary["price_peak"], 2),
                "price_mean_usd_bbl": _round(summary["price_mean"], 2),
                "price_trough_usd_bbl": _round(summary["price_trough"], 2),
                "peak_month": summary["peak_month"],
                "closure_months": summary["closure_months"],
                "hormuz_transit_mb_per_day": summary["hormuz_transit_mb_per_day"],
                "bypass_capacity_mb_per_day": summary["bypass_capacity_mb_per_day"],
                "range_note": "диапазон — оболочка объявленных вариантов чувствительности",
            },
            "consumer": {
                "low_income_budget_shock_pct": peak["low_income_budget_shock_pct"],
                "median_budget_shock_pct": peak["median_budget_shock_pct"],
                "approval_pressure_pp": peak["approval_pressure_pp"],
            },
            "sector_basis": {"pass_through": 0.6,
                             "note": "доля изменения цены, доходящая до издержек (допущение моста)"},
            "policy_options": [
                "выпуск SPR (уже в центральном сценарии)",
                "послабления по топливу / гибкость смешивания",
                "дипломатическая деэскалация и конвои",
                "адресные трансферты домохозяйствам с низкими доходами",
                "меры спроса (общественный транспорт, ограничения скорости)",
            ],
            "monitoring": [
                "AAA US regular gasoline (недельно)",
                "запасы EIA и загрузка НПЗ",
                "CPI бензин/энергия и инфляционные ожидания",
                "потребительские настроения и одобрение",
                "фрахт/активность перевозчиков и региональные перебои",
            ],
        }
        if stress:
            details["stress"] = {
                "assumptions": stress["assumptions"],
                "effective_quality_premium_usd_bbl": stress["effective_quality_premium_usd_bbl"],
                "effective_freight_usd_bbl": stress["effective_freight_usd_bbl"],
                "delivered_crude_delta_usd_bbl": stress["delivered_crude_delta_usd_bbl"],
                "jet_usd_gal": stress["jet_usd_gal"],
                "cpi_headline_pp": stress["cpi_headline_pp"],
                "social_political": stress["social_political"],
                "checks": stress["checks"],
            }
        country = self._oil_country_block(params["country"], peak)
        checks = {**{key: bool(value) for key, value in result.checks.items()},
                  "path_months": len(rows) <= MAX_PATH_POINTS,
                  "range_ordered": range_tuple[0] <= range_tuple[1] <= range_tuple[2],
                  "prices_positive": all(row["brent_usd_bbl"] > 0 and row["gasoline_usd_gal"] > 0
                                         for row in rows),
                  "aggregate_only": True}
        return {
            "model_scenario": spec["model_scenario"],
            "model_digest": result.digest(),
            "cards": cards,
            "prices": {
                "available": True,
                "unit": "USD/bbl",
                "path": _series([row["brent_usd_bbl"] for row in rows]),
                "peak": _round(summary["price_peak"], 2),
                "peak_month": summary["peak_month"],
                "mean": _round(summary["price_mean"], 2),
                "trough": _round(summary["price_trough"], 2),
                "range": {"low": range_tuple[0], "central": range_tuple[1],
                          "high": range_tuple[2],
                          "note": "оболочка вариантов чувствительности, не интервал"},
            },
            "cpi": {
                "available": True,
                "basis": "US CPI-U: веса бензина/дизеля и pass-through — объявленные допущения "
                         "hormuz_us_impact",
                "headline_pp": peak["cpi_headline_pp"],
                "direct_pp": peak["cpi_direct_pp"],
                "core_pp": peak["cpi_core_round_pp"],
                "path": _series([row["cpi_headline_pp"] for row in rows]),
            },
            "risk": {
                "available": True,
                "score_0_100": risk["score_0_100"],
                "band": risk["band"],
                "reasons": list(risk["reasons"]),
                "note": risk["note"],
                "source": "hormuz_us_impact (индекс на объявленных порогах)",
            },
            "charts": charts,
            "table": table,
            "sectors": {
                "available": True,
                "basis": "OilModule.prepare_cost_shock: мультипликаторы издержек по корзинам "
                         "(допущение моста pass_through=0.6)",
                "columns": [
                    {"key": "bucket", "label": "Корзина", "unit": ""},
                    {"key": "multiplier", "label": "Мультипликатор", "unit": "×"},
                    {"key": "cost_increase_pct", "label": "Рост издержек", "unit": "%"},
                ],
                "rows": sector_rows,
                "note": "корзины — объявленный мост; отображение в отрасли MRIO делает мир-ядро",
            },
            "country": country,
            "details": details,
            "checks": checks,
            "assumptions": {
                "count": len(result.assumptions),
                "items": [{"name": item.name, "value": item.value, "unit": item.unit,
                           "source_class": item.source_class}
                          for item in result.assumptions[:20]],
            },
            "limitations": list(OIL_LIMITATIONS)
                             + (["стресс-надбавки — объявленные допущения, не оценки"]
                                if spec["stress"] else []),
            "provenance": {
                "models": ["amfe.markets.oil (oil.brent)", "tools/hormuz_us_impact.py"],
                "module_id": result.module_id,
                "identity": "base_price и closure_share — параметры модуля; потребительский "
                            "пересчёт — объявленные допущения hormuz_us_impact",
                "dataset": "declared assumptions only (no third-party data is read)",
            },
        }

    def _oil_country_block(self, code: str, peak: Mapping) -> dict:
        rows: list[dict] = []
        if code == "US":
            rows.extend([
                {"label": "Бензин, база", "value": 3.10, "unit": "USD/gal",
                 "source": "hormuz_us_impact (допущение)"},
                {"label": "Бензин, пик", "value": peak["gasoline_usd_gal"], "unit": "USD/gal",
                 "source": "hormuz_us_impact"},
                {"label": "Дизель, пик", "value": peak["diesel_usd_gal"], "unit": "USD/gal",
                 "source": "hormuz_us_impact"},
                {"label": "CPI, вклад", "value": peak["cpi_headline_pp"], "unit": "п.п.",
                 "source": "hormuz_us_impact"},
                {"label": "Бюджет низких доходов", "value": peak["low_income_budget_shock_pct"],
                 "unit": "%", "source": "hormuz_us_impact"},
            ])
        exposure = self._country_exposure(DEFAULT_GEOPOLITICS_SCENARIO, code)
        if exposure is not None:
            rows.extend([
                {"label": "Δ выпуска (MRIO)", "value": exposure["delta_output"],
                 "unit": "USD млн", "source": f"geopolitics_scenarios · {exposure['scenario_id']}"},
                {"label": "Место по |Δ| среди стран", "value": exposure["rank"], "unit": "",
                 "source": f"geopolitics_scenarios · {exposure['scenario_id']}"},
            ])
        note = ("потребительский пересчёт — только для США (hormuz_us_impact); для остальных стран "
                "показан выпуск страны из MRIO-сценария Ормуза")
        if not rows:
            note = ("для этого кода нет ни потребительского пересчёта, ни страны в MRIO-сценарии; "
                    "выберите другую страну")
        return {
            "available": bool(rows),
            "code": code,
            "name": COUNTRY_NAMES.get(code, code),
            "note": note,
            "rows": rows,
        }

    def _country_exposure(self, scenario_id: str, code: str) -> dict | None:
        artifact = self._artifact()
        if not artifact or scenario_id not in artifact["by_id"]:
            return None
        impacts = artifact["by_id"][scenario_id].get("impacts") or {}
        by_country = impacts.get("by_country") or {}
        if code not in by_country:
            return None
        ranking = sorted(by_country.items(), key=lambda item: (-abs(float(item[1])), item[0]))
        rank = next(index for index, item in enumerate(ranking, start=1) if item[0] == code)
        return {"scenario_id": scenario_id, "delta_output": _round(by_country[code], 3),
                "rank": rank}

    # -- gas family -------------------------------------------------------------------------
    def _run_gas(self, spec: Mapping, params: Mapping) -> dict:
        overrides = {"ttf_price_eur_per_mwh": float(params["price"]),
                     "pipeline_loss_bcm_y": float(params["shock"])}
        try:
            report = build_gas_report([spec["model_scenario"]], overrides)
        except MarketError as error:
            raise Unavailable("MODEL_FAILED", f"газовый модуль отказал: {error}") from None
        scenarios = report.get("scenarios") or []
        if not scenarios:
            raise Unavailable("MODEL_FAILED", "газовый модуль вернул пустой отчёт")
        scenario = scenarios[0]
        summary = scenario["summary"]
        ttf = _series(summary["ttf_usd_per_mmbtu_path"])
        jkm = _series(summary["jkm_usd_per_mmbtu_path"])
        eur = _series(summary["ttf_eur_per_mwh_path"])
        labels = [f"М{index}" for index in range(1, len(ttf) + 1)]
        storage = summary["storage"]
        flows = summary["lng_flows"]
        cards = [
            _card("ttf_peak", "TTF, пик", _round(summary["ttf_peak_usd_per_mmbtu"], 4),
                  "USD/MMBtu", f"{summary['ttf_peak_eur_per_mwh']:.1f} EUR/MWh, "
                  f"{summary['ttf_change_pct_vs_base']:+.1f} % к базе", emphasis=True),
            _card("ttf_mean", "TTF, среднее", _round(summary["ttf_mean_usd_per_mmbtu"], 4),
                  "USD/MMBtu", f"минимум {summary['ttf_trough_usd_per_mmbtu']:.2f}"),
            _card("jkm_peak", "JKM, пик", _round(summary["jkm_peak_usd_per_mmbtu"], 4),
                  "USD/MMBtu", f"{summary['jkm_change_pct_vs_base']:+.1f} % к базе", emphasis=True),
            _card("storage_end", "ПХГ на конец", _round(storage["end_bcm"], 3), "bcm",
                  f"{storage['end_share_of_capacity']:.1f} % ёмкости", emphasis=True),
            _card("diversion", "Диверсия СПГ",
                  _round(flows["diversion_bcm_y"], 3), "bcm/год",
                  f"{flows['diversion_share_of_global_pct']:.2f} % мировой торговли"),
            _card("spread", "JKM−TTF, конец",
                  _round(summary["jkm_minus_ttf_usd_per_mmbtu_path"][-1], 4), "USD/MMBtu",
                  f"режим арбитража: {summary['arbitrage']['diversion']['binding']}"),
        ]
        charts = [
            {"kind": "line", "title": "Цены хабов по месяцам", "unit": "USD/MMBtu",
             "labels": labels,
             "lines": [
                 {"label": "TTF", "values": ttf},
                 {"label": "JKM", "values": jkm},
             ]},
        ]
        table = {
            "title": "TTF/JKM и потоки по месяцам",
            "columns": [
                {"key": "month", "label": "Месяц", "unit": "", "digits": 0},
                {"key": "ttf", "label": "TTF", "unit": "USD/MMBtu", "digits": 2},
                {"key": "ttf_eur", "label": "TTF", "unit": "EUR/MWh", "digits": 1},
                {"key": "jkm", "label": "JKM", "unit": "USD/MMBtu", "digits": 2},
                {"key": "spread", "label": "JKM−TTF", "unit": "USD/MMBtu", "digits": 2},
            ],
            "rows": [
                {"month": index, "ttf": ttf[index - 1], "ttf_eur": eur[index - 1],
                 "jkm": jkm[index - 1],
                 "spread": _round(summary["jkm_minus_ttf_usd_per_mmbtu_path"][index - 1], 2)}
                for index in range(1, len(ttf) + 1)
            ],
        }
        assumptions = scenario.get("assumptions") or []
        details = {
            "gas_module": {
                "scenario_label": summary["scenario_label"],
                "base_price_usd_per_mmbtu": _round(summary["base_price_usd_per_mmbtu"], 4),
                "base_price_eur_per_mwh": _round(summary["base_price_eur_per_mwh"], 2),
                "henry_hub_reference_usd_per_mmbtu":
                    _round(summary["henry_hub_reference_usd_per_mmbtu"], 4),
                "ranges": summary["ranges"],
            },
            "storage": {key: (_round(value, 4) if isinstance(value, (int, float)) else value)
                        for key, value in storage.items()},
            "lng_flows": {key: (_round(value, 4) if isinstance(value, (int, float)) else value)
                          for key, value in flows.items()},
            "arbitrage": {
                "band_usd_per_mmbtu": _round(summary["arbitrage"]["band_usd_per_mmbtu"], 4),
                "diversion": summary["arbitrage"]["diversion"],
            },
        }
        return {
            "model_scenario": spec["model_scenario"],
            "model_digest": scenario.get("digest"),
            "cards": cards,
            "prices": {
                "available": True,
                "unit": "USD/MMBtu",
                "path": ttf,
                "peak": _round(summary["ttf_peak_usd_per_mmbtu"], 4),
                "peak_month": None,
                "mean": _round(summary["ttf_mean_usd_per_mmbtu"], 4),
                "trough": _round(summary["ttf_trough_usd_per_mmbtu"], 4),
                "range": {"low": _round(summary["ranges"]["ttf_peak"]["low"], 4),
                          "central": _round(summary["ranges"]["ttf_peak"]["central"], 4),
                          "high": _round(summary["ranges"]["ttf_peak"]["high"], 4),
                          "note": "оболочка вариантов чувствительности, не интервал"},
            },
            "cpi": {
                "available": False,
                "note": "газовый модуль считает цены хабов и балансы; пересчёта в CPI у него нет "
                        "(не подменяем его допущением)",
            },
            "risk": {
                "available": False,
                "note": "индекса риска у газового модуля нет; смотрите ПХГ, диверсию и проверки",
            },
            "charts": charts,
            "table": table,
            "sectors": {
                "available": False,
                "rows": [],
                "note": "отраслевого моста у газового модуля нет: он агрегирует хабы, а не отрасли",
            },
            "country": {
                "available": False,
                "code": params["country"],
                "name": COUNTRY_NAMES.get(params["country"], params["country"]),
                "note": "страновой разбивки нет: модель хаб-агрегат (TTF/JKM/Henry Hub)",
                "rows": [],
            },
            "details": details,
            "checks": {**{key: bool(value) for key, value in scenario["checks"].items()},
                       "path_months": len(ttf) <= MAX_PATH_POINTS,
                       "aggregate_only": True},
            "assumptions": {
                "count": len(assumptions),
                "items": [{"name": item.get("name"), "value": item.get("value"),
                           "unit": item.get("unit"), "source_class": item.get("source_class")}
                          for item in assumptions[:20]],
            },
            "limitations": list(GAS_LIMITATIONS),
            "provenance": {
                "models": ["amfe.markets.gas (gas.lng_eu)"],
                "module_id": report.get("module", {}).get("module_id", "gas.lng_eu"),
                "identity": "TTF/JKM/ПХГ — выход модуля газа на объявленных допущениях",
                "dataset": "declared assumptions only (no third-party data is read)",
            },
        }

    # -- geopolitics family -----------------------------------------------------------------
    def _run_geopolitics(self, spec: Mapping, params: Mapping) -> dict:
        scenario_id = params["geo_scenario"]
        source, report = self._geopolitics_report(scenario_id)
        declaration = report.get("scenario") or {}
        result = report.get("result") or {}
        impacts = report.get("impacts") or {}
        by_country = impacts.get("by_country") or {}
        by_industry = impacts.get("by_industry") or {}
        asymmetry = report.get("asymmetry") or {}
        if not by_country or not by_industry:
            raise Unavailable("MODEL_FAILED", "геополитический отчёт без агрегатов по странам")

        ranking = sorted(by_country.items(), key=lambda item: (-abs(float(item[1])), item[0]))
        total_abs = sum(abs(float(value)) for value in by_country.values()) or 1.0
        top_countries = [
            {"country": code, "name": COUNTRY_NAMES.get(code, code),
             "delta_output": _round(value, 3),
             "share_pct": _round(100.0 * abs(float(value)) / total_abs, 2)}
            for code, value in ranking[:10]
        ]
        industry_total = sum(abs(float(value)) for value in by_industry.values()) or 1.0
        top_industries = [
            {"industry": code, "delta_output": _round(value, 3),
             "share_pct": _round(100.0 * abs(float(value)) / industry_total, 2)}
            for code, value in sorted(by_industry.items(),
                                      key=lambda item: (-abs(float(item[1])), item[0]))[:10]
        ]
        relative = float(result.get("relative_delta_output") or 0.0)
        selected = by_country.get(params["country"])
        selected_rank = next((index for index, (code, _value) in enumerate(ranking, start=1)
                              if code == params["country"]), None)

        cards = [
            _card("delta_output", "Δ выпуска, всего", _round(result.get("total_delta_output"), 3),
                  "USD млн", f"{relative * 100:+.5f} % от базового выпуска", emphasis=True),
            _card("country_delta", f"Δ выпуска: {params['country']}",
                  _round(selected, 3) if selected is not None else None, "USD млн",
                  f"место {selected_rank} по |Δ|" if selected_rank else "страны нет в сценарии",
                  emphasis=True),
            _card("largest_country", "Крупнейший эффект",
                  ranking[0][0] if ranking else "—", "",
                  f"{_round(ranking[0][1], 1)} USD млн" if ranking else ""),
            _card("max_cell", "Макс. |Δ| ячейки",
                  _round(result.get("max_abs_delta_output"), 3), "USD млн",
                  "страна×отрасль"),
            _card("countries", "Стран затронуто", len(by_country), "",
                  f"отраслей: {len(by_industry)}"),
            _card("gain_loss", "Выгоды/ущерб",
                  f"{_round(asymmetry.get('gain_total', 0.0), 1)} / "
                  f"{_round(asymmetry.get('loss_total', 0.0), 1)}", "USD млн",
                  "считаются отдельно (P-008)"),
        ]
        labels = [row["country"] for row in top_countries]
        charts = [
            {"kind": "bar", "title": "Топ-10 стран по |Δ выпуска|", "unit": "USD млн",
             "labels": labels,
             "lines": [{"label": "Δ выпуска", "values": [row["delta_output"]
                                                         for row in top_countries]}]},
        ]
        table = {
            "title": "Страны: изменение выпуска (топ-10 по |Δ|)",
            "columns": [
                {"key": "country", "label": "Страна", "unit": ""},
                {"key": "name", "label": "Название", "unit": ""},
                {"key": "delta", "label": "Δ выпуска", "unit": "USD млн", "digits": 3},
                {"key": "share", "label": "Доля |Δ|", "unit": "%", "digits": 2},
            ],
            "rows": [{"country": row["country"], "name": row["name"],
                      "delta": row["delta_output"], "share": row["share_pct"]}
                     for row in top_countries],
        }
        country = {
            "available": selected is not None,
            "code": params["country"],
            "name": COUNTRY_NAMES.get(params["country"], params["country"]),
            "note": ("страна есть в сценарии; числа — сравнительная статика MRIO, USD млн"
                     if selected is not None else
                     "страны нет в этом сценарии (протокол объявляет другой набор актёров)"),
            "rows": ([
                {"label": "Δ выпуска", "value": _round(selected, 3), "unit": "USD млн",
                 "source": f"geopolitics_scenarios · {scenario_id}"},
                {"label": "Место по |Δ|", "value": selected_rank, "unit": "",
                 "source": f"geopolitics_scenarios · {scenario_id}"},
                {"label": "Доля |Δ| по стране", "value": _round(100.0 * abs(float(selected))
                                                                / total_abs, 2), "unit": "%",
                 "source": f"geopolitics_scenarios · {scenario_id}"},
            ] if selected is not None else []),
        }
        details = {
            "scenario_declaration": {
                "scenario_id": declaration.get("scenario_id"),
                "title_ru": declaration.get("title_ru"),
                "family": declaration.get("family"),
                "channel": declaration.get("channel"),
                "size": declaration.get("size"),
                "duration_months": declaration.get("duration"),
                "actors": declaration.get("actors"),
                "sectors": declaration.get("sectors"),
                "direction": declaration.get("direction"),
                "decay": report.get("decay"),
            },
            "baseline": report.get("baseline"),
            "asymmetry": asymmetry,
            "dataset": {
                "dataset_id": (report.get("dataset") or {}).get("dataset_id"),
                "countries": (report.get("dataset") or {}).get("countries"),
                "industries": (report.get("dataset") or {}).get("industries"),
                "sectors": (report.get("dataset") or {}).get("sectors"),
                "units": result.get("units"),
                "provenance_note": (report.get("dataset") or {}).get("provenance_note"),
            },
            "perturbation": report.get("perturbation"),
            "proxy": report.get("proxy"),
        }
        return {
            "model_scenario": scenario_id,
            "model_digest": report.get("digest"),
            "cards": cards,
            "prices": {
                "available": False,
                "note": "геополитический модуль — сравнительная статика выпуска; цен он не считает",
            },
            "cpi": {
                "available": False,
                "note": "CPI в MRIO-модуле не считается (нет цен); не подменяем допущением",
            },
            "risk": {
                "available": False,
                "note": "индекса риска нет; смотрите асимметрию выгод/ущерба и направление",
            },
            "charts": charts,
            "table": table,
            "sectors": {
                "available": True,
                "basis": "MRIO: изменение выпуска по отраслям, топ-10 по |Δ|",
                "columns": [
                    {"key": "industry", "label": "Отрасль", "unit": ""},
                    {"key": "delta", "label": "Δ выпуска", "unit": "USD млн", "digits": 3},
                    {"key": "share", "label": "Доля |Δ|", "unit": "%", "digits": 2},
                ],
                "rows": [{"industry": row["industry"], "delta": row["delta_output"],
                          "share": row["share_pct"]} for row in top_industries],
                "note": "отраслевые коды — CPA/NACE из датасета",
            },
            "country": country,
            "details": details,
            "checks": {**{key: bool(value)
                          for key, value in (report.get("checks") or {}).items()
                          if isinstance(value, (bool, int, float, str))},
                       "aggregate_only": True},
            "assumptions": {
                "count": 0,
                "items": [],
                "note": "допущения сценария объявлены в protocols/geopolitics_scenarios.json",
            },
            "limitations": list(GEOPOLITICS_LIMITATIONS)
                             + [item for item in (report.get("limitations") or [])[:3]],
            "provenance": {
                "models": ["tools/geopolitics_scenarios.py (FIGARO MRIO)"],
                "identity": (f"источник отчёта: {source['kind']} ({source['label']})"),
                "dataset": (report.get("dataset") or {}).get("dataset_id"),
                "artifact_sha256": source.get("sha256"),
            },
        }

    # -- geopolitics sources ----------------------------------------------------------------
    def _artifact(self) -> dict | None:
        """Load the precomputed geopolitics report once (thread-safe, deterministic)."""
        if self.geopolitics_mode == "synthetic":
            return None
        if self._artifact_state is not None:
            return self._artifact_state or None
        with self._lock:
            if self._artifact_state is not None:
                return self._artifact_state or None
            state = self._load_artifact()
            self._artifact_state = state or {}
        return self._artifact_state or None

    def _load_artifact(self) -> dict | None:
        path = self.geopolitics_artifact
        try:
            raw = path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            if self.geopolitics_mode == "artifact":
                raise Unavailable("ARTIFACT_MISSING",
                                  "предрасчитанный отчёт геополитики недоступен") from None
            LOGGER.warning("geopolitics artifact is unavailable: %s", path)
            return None
        scenarios = document.get("scenarios") if isinstance(document, dict) else None
        if not isinstance(scenarios, list):
            if self.geopolitics_mode == "artifact":
                raise Unavailable("ARTIFACT_INVALID", "отчёт геополитики без списка сценариев")
            return None
        by_id: dict[str, dict] = {}
        catalogue: list[dict] = []
        for item in scenarios:
            if not isinstance(item, dict):
                continue
            declaration = item.get("scenario") or {}
            scenario_id = declaration.get("scenario_id")
            if not isinstance(scenario_id, str):
                continue
            by_id[scenario_id] = item
            catalogue.append({
                "id": scenario_id,
                "title": declaration.get("title_ru") or scenario_id,
                "family": declaration.get("family"),
                "size": declaration.get("size"),
                "duration_months": declaration.get("duration"),
            })
        if not by_id:
            return None
        return {"by_id": by_id, "catalogue": catalogue,
                "sha256": hashlib.sha256(raw).hexdigest()}

    def _geopolitics_report(self, scenario_id: str) -> tuple[dict, dict]:
        artifact = self._artifact()
        if artifact:
            report = artifact["by_id"].get(scenario_id)
            if report is None:
                raise BadRequest("UNKNOWN_SCENARIO",
                                 f"сценария {scenario_id!r} нет в предрасчитанном отчёте",
                                 {"available": [item["id"] for item in artifact["catalogue"]]})
            return ({"kind": "artifact", "label": "build/geopolitics/report.json",
                     "sha256": artifact["sha256"]}, report)
        return ({"kind": "synthetic", "label": "синтетическая фикстура протокола",
                 "sha256": None}, self._synthetic_report(scenario_id))

    def _synthetic_report(self, scenario_id: str) -> dict:
        cached = self._synthetic_cache.get(scenario_id)
        if cached is not None:
            return cached
        try:
            import geopolitics_scenarios as geopolitics
        except Exception as error:  # pragma: no cover - import failure is environmental
            raise Unavailable("GEOPOLITICS_UNAVAILABLE",
                              f"модуль геополитики недоступен: {type(error).__name__}") from None
        try:
            document, protocol_sha = geopolitics.load_protocol(geopolitics.DEFAULT_PROTOCOL)
            summary = geopolitics.validate_protocol(document, geopolitics.load_source_registry())
            table = geopolitics.synthetic_table_for_protocol(document, summary)
            built = geopolitics.build_reports(table, document,
                                              geopolitics._synthetic_meta(table), protocol_sha,
                                              only=(scenario_id,), summary=summary)
        except Exception as error:
            raise Unavailable("GEOPOLITICS_UNAVAILABLE",
                              f"геополитический сценарий не собран: {type(error).__name__}: "
                              f"{error}") from None
        reports = built.get("reports") or []
        if not reports:
            raise Unavailable("GEOPOLITICS_UNAVAILABLE", "модуль не вернул отчёт сценария")
        with self._lock:
            self._synthetic_cache[scenario_id] = reports[0]
        return reports[0]

    # -- helpers ----------------------------------------------------------------------------
    def _country_codes(self, artifact: dict | None) -> list[str]:
        codes = ["US", "DE", "FR", "IT", "ES", "PL", "NL", "UK", "NO", "JP", "CN", "IN", "KR"]
        if artifact:
            report = next(iter(artifact["by_id"].values()))
            impacts = report.get("impacts") or {}
            codes.extend(sorted(impacts.get("by_country") or {}))
        return sorted(dict.fromkeys(codes))

    # -- HTTP -------------------------------------------------------------------------------
    def dispatch(self, method: str, target: str, body: bytes) -> Response:
        if len(target) > MAX_TARGET_CHARS:
            return self._error_response(BadRequest("TARGET_TOO_LONG",
                                                   "слишком длинный запрос"))
        path = (urlsplit(target).path or "/").rstrip("/") or "/"
        try:
            if method in ("GET", "HEAD"):
                return self._handle_get(path)
            if method == "OPTIONS":
                return Response(204, b"", "text/plain; charset=utf-8",
                                (("Allow", "GET, HEAD, POST, OPTIONS"),))
            if method == "POST":
                if path != "/api/run":
                    raise NotFound("NOT_FOUND", f"нет такого ресурса: {path}")
                return self._json(200, self.run(self._parse_body(body)))
            raise BadRequest("METHOD_NOT_ALLOWED", f"метод {method} не поддерживается")
        except SimulatorError as error:
            return self._error_response(error)

    def _handle_get(self, path: str) -> Response:
        if path in ("/", "/index.html"):
            return Response(200, self.page(), "text/html; charset=utf-8",
                            (("Content-Security-Policy",
                              "default-src 'none'; script-src 'unsafe-inline'; "
                              "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; "
                              "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),))
        if path == "/api/health":
            return self._json(200, self.health())
        if path == "/api/scenarios":
            return self._json(200, self.scenarios_document())
        if path == "/api/run":
            raise BadRequest("METHOD_NOT_ALLOWED", "/api/run принимает только POST",
                             headers={"Allow": "POST, OPTIONS"})
        if path == "/favicon.ico":
            return Response(204, b"", "image/x-icon")
        raise NotFound("NOT_FOUND", f"нет такого ресурса: {path}")

    def _parse_body(self, body: bytes) -> Any:
        if not body:
            raise BadRequest("EMPTY_BODY", "тело запроса пустое: нужен JSON-объект")
        if len(body) > MAX_BODY_BYTES:
            raise BadRequest("BODY_TOO_LARGE", f"тело больше {MAX_BODY_BYTES} байт")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("INVALID_JSON", "тело запроса не является JSON") from None
        if not isinstance(payload, dict):
            raise BadRequest("INVALID_BODY", "тело запроса должно быть JSON-объектом")
        return payload

    def _json(self, status: int, payload: Mapping) -> Response:
        return Response(status, payload)

    def _error_response(self, error: SimulatorError) -> Response:
        document = {
            "schema": SCHEMA,
            "kind": "error",
            "status": "ERROR",
            "research_status": STATUS,
            "code": error.code,
            "error": error.message,
            "admission": False,
            "causal_claim": False,
            "details": error.details,
        }
        return Response(error.status, document, "application/json; charset=utf-8",
                        tuple(error.headers.items()))


# -------------------------------------------------------------------------------------------- HTTP
class SimulatorHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter: JSON in, JSON out, one HTML page, no HTML error pages."""

    server_version = f"AMFE-Simulator/{TOOL_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        app: SimulatorApp = self.server.app  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(min(max(length, 0), MAX_BODY_BYTES + 1)) if length else b""
        response = app.dispatch(method, self.path, body)
        payload = response.payload
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        else:  # pragma: no cover - never reached by construction
            data = b""
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if method != "HEAD" and data:
            self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.debug("http %s - %s", self.address_string(), format % args)


class SimulatorServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the application instance."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: SimulatorApp,
                 handler_class: type[BaseHTTPRequestHandler] = SimulatorHandler) -> None:
        self.app = app
        super().__init__(address, handler_class)

    def server_bind(self) -> None:
        super().server_bind()
        self.server_name = "amfe-simulator-ui"


def create_server(app: SimulatorApp, host: str = DEFAULT_HOST,
                  port: int = DEFAULT_PORT) -> SimulatorServer:
    """Bind the app to ``host:port`` (``port=0`` picks a free port)."""
    return SimulatorServer((host, port), app)


# -------------------------------------------------------------------------------------------- page
PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMFE · Симулятор сценариев (RESEARCH_ONLY)</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#283040;--text:#e6edf3;--muted:#8b949e;
--accent:#5ee0b0;--warn:#f2b544;--bad:#ff7a7a;--blue:#7aa2ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;
gap:10px;align-items:center;justify-content:space-between;background:#10151d}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
.badge{font-size:11px;letter-spacing:.08em;text-transform:uppercase;border:1px solid var(--warn);
color:var(--warn);border-radius:999px;padding:3px 10px}
main{max-width:1140px;margin:0 auto;padding:18px 16px 56px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;
margin-bottom:14px}
.warn{border-color:#6b4d12;background:#1e1a0e;color:#f5d894;font-size:14px}
.warn b{color:#ffd479}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin-bottom:5px}
select,input{width:100%;background:#0b1017;color:var(--text);border:1px solid var(--line);
border-radius:8px;padding:9px 10px;font-size:14px;font-family:inherit}
input:focus,select:focus{outline:2px solid #1f6feb;outline-offset:1px}
.help{font-size:11.5px;color:#6e7781;margin-top:4px;min-height:16px}
button{background:var(--accent);color:#062018;border:0;border-radius:9px;padding:11px 20px;
font-weight:700;font-size:15px;cursor:pointer;font-family:inherit}
button:disabled{opacity:.55;cursor:progress}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--text);
font-weight:600;padding:9px 14px;font-size:13px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px}
.card{background:#0f1520;border:1px solid var(--line);border-radius:10px;padding:12px 13px}
.card.em{border-color:#2f6f5b}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card .v{font-size:24px;font-weight:700;margin:5px 0 1px;font-variant-numeric:tabular-nums}
.card .u{font-size:12px;color:var(--muted);margin-left:4px;font-weight:500}
.card .n{font-size:11.5px;color:#7d8590;margin-top:4px}
.risk{border-radius:10px;padding:12px 14px;border:1px solid var(--line);margin-top:12px}
.risk.low{border-color:#2f6f5b;background:#0f1c17}
.risk.elevated{border-color:#7a5c17;background:#1e1a0e}
.risk.high{border-color:#7a3b17;background:#1f130d}
.risk.severe{border-color:#7a1f1f;background:#210d0d}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 12px}
h3{font-size:13px;color:#a9b4c0;margin:0 0 6px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 9px;border-bottom:1px solid #1d2532;text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.05em;text-align:right}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:#121a26}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.chart{background:#0f1520;border:1px solid var(--line);border-radius:10px;padding:12px}
.chart svg{width:100%;height:auto;display:block}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;
border-bottom:1px solid #1b2330;font-size:13px}
.kv:last-child{border-bottom:0}
.kv span:last-child{font-variant-numeric:tabular-nums;color:#c9d1d9}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:11.5px;border:1px solid var(--line);border-radius:999px;padding:3px 9px;
color:var(--muted)}
.chip.ok{border-color:#2f6f5b;color:#7fd6b4}
.chip.bad{border-color:#7a1f1f;color:#ff9a9a}
.note{font-size:12.5px;color:#7d8590;margin:8px 0 0}
ul.lim{margin:6px 0 0;padding-left:18px;color:#98a1ab;font-size:12.5px}
.muted{color:var(--muted)}
.err{border-color:#7a1f1f;background:#210d0d;color:#ffb3b3}
footer{color:#5b636d;font-size:11.5px;text-align:center;margin-top:22px;line-height:1.7}
code{background:#0b1017;border:1px solid var(--line);border-radius:5px;padding:1px 5px;
font-size:12px}
@media(max-width:640px){.card .v{font-size:20px}main{padding:12px 10px 40px}}
</style>
</head>
<body>
<header>
  <h1>AMFE · Симулятор сценариев</h1>
  <div class="row">
    <span class="badge">RESEARCH_ONLY · admission=false</span>
    <span class="badge" style="border-color:#2f6f5b;color:#7fd6b4">v__VERSION__</span>
  </div>
</header>
<main>
  <section class="warn">
    <b>Это не прогноз.</b> Сценарная арифметика существующих моделей AMFE на объявленных допущениях:
    status RESEARCH_ONLY, admission=false, causal_claim=false. Диапазоны — оболочка вариантов
    чувствительности, а не доверительный интервал. Не допуск, не рекомендация и не вероятность.
  </section>

  <section>
    <h2>Сценарий и параметры</h2>
    <form id="form">
      <div class="grid">
        <div>
          <label for="scenario">Сценарий</label>
          <select id="scenario"></select>
          <div class="help" id="scenario-help"></div>
        </div>
        <div id="price-wrap">
          <label for="price" id="price-label">Цена</label>
          <input id="price" type="number" step="any" inputmode="decimal">
          <div class="help" id="price-help"></div>
        </div>
        <div id="shock-wrap">
          <label for="shock" id="shock-label">Шок</label>
          <input id="shock" type="number" step="any" inputmode="decimal">
          <div class="help" id="shock-help"></div>
        </div>
        <div>
          <label for="country">Страна</label>
          <select id="country"></select>
          <div class="help">для страновых эффектов; где модель их не считает — честная пометка</div>
        </div>
        <div id="geo-wrap" hidden>
          <label for="geo_scenario">Геополитический сценарий</label>
          <select id="geo_scenario"></select>
          <div class="help">сценарии протокола protocols/geopolitics_scenarios.json</div>
        </div>
      </div>
      <div class="row" style="margin-top:14px">
        <button id="run" type="submit">Рассчитать</button>
        <button id="download" class="ghost" type="button" disabled>Скачать JSON</button>
        <span class="muted" id="status">загрузка…</span>
      </div>
    </form>
  </section>

  <section id="result" hidden>
    <h2 id="result-title"></h2>
    <div class="cards" id="cards"></div>
    <div id="risk"></div>
    <div class="note" id="result-meta"></div>
  </section>

  <section id="charts-section" hidden>
    <h2>Графики</h2>
    <div class="charts" id="charts"></div>
  </section>

  <section id="table-section" hidden>
    <h2 id="table-title">Таблица</h2>
    <div style="overflow-x:auto"><table id="table"></table></div>
    <div class="note" id="table-note"></div>
  </section>

  <section id="sectors-section" hidden>
    <h2>Отраслевые эффекты</h2>
    <div style="overflow-x:auto"><table id="sectors"></table></div>
    <div class="note" id="sectors-note"></div>
  </section>

  <section id="country-section" hidden>
    <h2>Страна</h2>
    <div id="country-body"></div>
  </section>

  <section id="details-section" hidden>
    <h2>Детали и допущения</h2>
    <div id="details"></div>
  </section>

  <section id="checks-section" hidden>
    <h2>Проверки и ограничения</h2>
    <div class="chips" id="checks"></div>
    <ul class="lim" id="limitations"></ul>
    <div class="note" id="disclaimer"></div>
  </section>

  <footer>
    Демо без внешних зависимостей: stdlib <code>http.server</code> + ванильный HTML/JS, без CDN.<br>
    Модели: <span id="models">—</span><br>
    digest: <span id="digest">—</span>
  </footer>
</main>
<script>
"use strict";
const PALETTE = ["#5ee0b0", "#f2b544", "#7aa2ff", "#ff7a7a", "#c792ea"];
const state = {health: null, result: null, specs: {}};
const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function fmt(value, digits) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value !== "number") return String(value);
  const d = (digits === undefined) ? 2 : digits;
  return value.toLocaleString("ru-RU", {minimumFractionDigits: d, maximumFractionDigits: d});
}
function setStatus(text, isError) {
  const node = $("status");
  node.textContent = text;
  node.style.color = isError ? "#ff9a9a" : "";
}

async function loadHealth() {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error("health " + response.status);
  const health = await response.json();
  state.health = health;
  const select = $("scenario");
  select.innerHTML = "";
  health.scenarios.forEach((scenario) => {
    state.specs[scenario.id] = scenario;
    const option = el("option", null, scenario.title);
    option.value = scenario.id;
    select.appendChild(option);
  });
  const country = $("country");
  country.innerHTML = "";
  (health.countries || []).forEach((item) => {
    const option = el("option", null, item.code + " · " + item.name);
    option.value = item.code;
    country.appendChild(option);
  });
  const geo = $("geo_scenario");
  geo.innerHTML = "";
  ((health.geopolitics || {}).scenarios || []).forEach((item) => {
    const option = el("option", null, item.id + " · " + item.title);
    option.value = item.id;
    geo.appendChild(option);
  });
  if (!geo.options.length) {
    const option = el("option", null, health.geopolitics.default_scenario);
    option.value = health.geopolitics.default_scenario;
    geo.appendChild(option);
  }
  applyScenario();
}

function applyScenario() {
  const spec = state.specs[$("scenario").value];
  if (!spec) return;
  $("scenario-help").textContent = spec.model + " · " + spec.summary;
  const priceSpec = (spec.params || {}).price;
  const shockSpec = (spec.params || {}).shock;
  $("price-wrap").hidden = !priceSpec;
  $("shock-wrap").hidden = !shockSpec;
  if (priceSpec) {
    $("price").min = priceSpec.min; $("price").max = priceSpec.max;
    $("price").step = priceSpec.step; $("price").value = priceSpec.default;
    $("price-label").textContent = priceSpec.label + ", " + priceSpec.unit;
    $("price-help").textContent = priceSpec.help + " (" + priceSpec.min + "…" + priceSpec.max + ")";
  }
  if (shockSpec) {
    $("shock").min = shockSpec.min; $("shock").max = shockSpec.max;
    $("shock").step = shockSpec.step; $("shock").value = shockSpec.default;
    $("shock-label").textContent = shockSpec.label + ", " + shockSpec.unit;
    $("shock-help").textContent = shockSpec.help + " (" + shockSpec.min + "…" + shockSpec.max + ")";
  }
  $("geo-wrap").hidden = spec.family !== "geopolitics";
  const defaults = spec.defaults || {};
  if (defaults.country) $("country").value = defaults.country;
  if (defaults.geo_scenario) $("geo_scenario").value = defaults.geo_scenario;
}

async function runScenario(event) {
  if (event) event.preventDefault();
  const spec = state.specs[$("scenario").value];
  const payload = {scenario: spec.id, country: $("country").value};
  if ((spec.params || {}).price) payload.price = Number($("price").value);
  if ((spec.params || {}).shock) payload.shock = Number($("shock").value);
  if (spec.family === "geopolitics") payload.geo_scenario = $("geo_scenario").value;
  $("run").disabled = true;
  setStatus("считаю…", false);
  try {
    const response = await fetch("/api/run", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const document_ = await response.json();
    if (!response.ok) {
      throw new Error((document_.error || "ошибка") + " [" + document_.code + "]");
    }
    state.result = document_;
    render(document_);
    $("download").disabled = false;
    setStatus("готово · " + document_.scenario.model, false);
  } catch (error) {
    setStatus(String(error.message || error), true);
  } finally {
    $("run").disabled = false;
  }
}

function render(document_) {
  $("result").hidden = false;
  $("result-title").textContent = document_.scenario.title;
  const cards = $("cards");
  cards.innerHTML = "";
  (document_.cards || []).forEach((card) => {
    const node = el("div", "card" + (card.emphasis ? " em" : ""));
    node.appendChild(el("div", "k", card.label));
    const value = el("div", "v", fmt(card.value, typeof card.value === "number" ? 2 : 0));
    if (card.unit) value.appendChild(el("span", "u", card.unit));
    node.appendChild(value);
    node.appendChild(el("div", "n", card.note || ""));
    cards.appendChild(node);
  });
  renderRisk(document_.risk);
  $("result-meta").textContent = "модель: " + document_.scenario.model +
    (document_.scenario.model_scenario ? " · сценарий модели: " + document_.scenario.model_scenario : "") +
    (document_.scenario.digest ? " · digest модели: " + String(document_.scenario.digest).slice(0, 16) : "");
  renderCharts(document_.charts || []);
  renderTable(document_.table);
  renderSectors(document_.sectors);
  renderCountry(document_.country);
  renderDetails(document_);
  renderChecks(document_);
  $("models").textContent = ((document_.provenance || {}).models || []).join(", ") || "—";
  $("digest").textContent = document_.digest || "—";
}

function renderRisk(risk) {
  const box = $("risk");
  box.innerHTML = "";
  if (!risk || !risk.available) {
    box.appendChild(el("div", "note", (risk && risk.note) || "индекс риска для этого сценария не считается"));
    return;
  }
  const node = el("div", "risk " + (risk.band || ""));
  node.appendChild(el("b", null, "Социально-политический риск: " + risk.band + " (" +
    fmt(risk.score_0_100, 0) + "/100)"));
  node.appendChild(el("div", "note", (risk.reasons || []).join("; ") || "ни один порог не пройден"));
  node.appendChild(el("div", "note", risk.note || ""));
  box.appendChild(node);
}

function chartFrame(spec) {
  const wrap = el("div", "chart");
  wrap.appendChild(el("h3", null, spec.title + (spec.unit ? " · " + spec.unit : "")));
  return wrap;
}
function svgNode(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.keys(attrs || {}).forEach((key) => node.setAttribute(key, attrs[key]));
  return node;
}
function lineChart(spec) {
  const W = 520, H = 210, P = {l: 44, r: 12, t: 12, b: 24};
  const lines = spec.lines || [];
  const values = lines.reduce((all, line) => all.concat(line.values), []);
  let min = Math.min.apply(null, values), max = Math.max.apply(null, values);
  if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1; }
  if (max - min < 1e-9) { max = min + 1; }
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;
  const labels = spec.labels || [];
  const n = Math.max.apply(null, lines.map((line) => line.values.length));
  const x = (index) => P.l + (n <= 1 ? 0 : index * (W - P.l - P.r) / (n - 1));
  const y = (value) => H - P.b - (value - min) * (H - P.t - P.b) / (max - min);
  const svg = svgNode("svg", {viewBox: "0 0 " + W + " " + H, role: "img"});
  for (let i = 0; i <= 3; i += 1) {
    const value = min + (max - min) * i / 3;
    const yy = y(value);
    svg.appendChild(svgNode("line", {x1: P.l, x2: W - P.r, y1: yy, y2: yy,
      stroke: "#1f2836", "stroke-width": 1}));
    const text = svgNode("text", {x: P.l - 6, y: yy + 3, "text-anchor": "end",
      "font-size": 9, fill: "#6e7781"});
    text.textContent = fmt(value, Math.abs(max) < 10 ? 2 : 0);
    svg.appendChild(text);
  }
  lines.forEach((line, index) => {
    const color = PALETTE[index % PALETTE.length];
    const points = line.values.map((value, i) => x(i) + "," + y(value)).join(" ");
    svg.appendChild(svgNode("polyline", {points: points, fill: "none", stroke: color,
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}));
    line.values.forEach((value, i) => {
      svg.appendChild(svgNode("circle", {cx: x(i), cy: y(value), r: 2.6, fill: color}));
    });
  });
  labels.forEach((label, i) => {
    if (labels.length > 8 && i % 2 === 1) return;
    const text = svgNode("text", {x: x(i), y: H - 8, "text-anchor": "middle", "font-size": 9,
      fill: "#6e7781"});
    text.textContent = label;
    svg.appendChild(text);
  });
  return svg;
}
function barChart(spec) {
  const lines = spec.lines || [];
  if (!lines.length) return svgNode("svg", {viewBox: "0 0 10 10"});
  const values = lines[0].values;
  const labels = spec.labels || [];
  const rows = values.length;
  const W = 520, rowH = 24, H = Math.max(120, rows * rowH + 34), P = {l: 52, r: 60, t: 8, b: 18};
  let hi = Math.max.apply(null, values), lo = Math.min.apply(null, values);
  if (hi <= 0) hi = 1;
  if (lo >= 0) lo = 0;
  const span = hi - lo || 1;
  const zero = P.l + (0 - lo) * (W - P.l - P.r) / span;
  const svg = svgNode("svg", {viewBox: "0 0 " + W + " " + H, role: "img"});
  svg.appendChild(svgNode("line", {x1: zero, x2: zero, y1: P.t, y2: H - P.b,
    stroke: "#3a4556", "stroke-width": 1}));
  values.forEach((value, i) => {
    const yTop = P.t + i * rowH + 3;
    const width = Math.abs(value) * (W - P.l - P.r) / span;
    const xStart = value >= 0 ? zero : zero - width;
    const color = value >= 0 ? PALETTE[0] : PALETTE[3];
    svg.appendChild(svgNode("rect", {x: xStart, y: yTop, width: Math.max(width, 1),
      height: rowH - 8, rx: 3, fill: color, opacity: 0.85}));
    const left = svgNode("text", {x: zero - 6, y: yTop + rowH - 12, "text-anchor": "end",
      "font-size": 10, fill: "#a9b4c0"});
    left.textContent = (labels[i] !== undefined) ? labels[i] : String(i + 1);
    svg.appendChild(left);
    const right = svgNode("text", {x: value >= 0 ? zero + width + 5 : zero - width - 5,
      y: yTop + rowH - 12, "text-anchor": value >= 0 ? "start" : "end", "font-size": 9.5,
      fill: "#8b949e"});
    right.textContent = fmt(value, Math.abs(value) >= 100 ? 0 : 2);
    svg.appendChild(right);
  });
  return svg;
}
function renderCharts(charts) {
  const section = $("charts-section");
  const box = $("charts");
  box.innerHTML = "";
  if (!charts.length) { section.hidden = true; return; }
  section.hidden = false;
  charts.forEach((spec) => {
    const wrap = chartFrame(spec);
    wrap.appendChild(spec.kind === "bar" ? barChart(spec) : lineChart(spec));
    if (spec.lines && spec.lines.length > 1) {
      const legend = el("div", "legend");
      spec.lines.forEach((line, index) => {
        const item = el("span");
        const dot = el("i");
        dot.style.background = PALETTE[index % PALETTE.length];
        item.appendChild(dot);
        item.appendChild(document.createTextNode(line.label));
        legend.appendChild(item);
      });
      wrap.appendChild(legend);
    }
    box.appendChild(wrap);
  });
}
function buildTable(node, table) {
  node.innerHTML = "";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  (table.columns || []).forEach((column) => {
    const th = el("th", null, column.label + (column.unit ? ", " + column.unit : ""));
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  node.appendChild(head);
  const body = document.createElement("tbody");
  (table.rows || []).forEach((row) => {
    const tr = document.createElement("tr");
    (table.columns || []).forEach((column) => {
      const value = row[column.key];
      const digits = (column.digits === undefined) ? 2 : column.digits;
      tr.appendChild(el("td", null, typeof value === "number" ? fmt(value, digits) : value));
    });
    body.appendChild(tr);
  });
  node.appendChild(body);
}
function renderTable(table) {
  const section = $("table-section");
  if (!table || !(table.rows || []).length) { section.hidden = true; return; }
  section.hidden = false;
  $("table-title").textContent = table.title || "Таблица";
  buildTable($("table"), table);
  $("table-note").textContent = table.note || "";
}
function renderSectors(sectors) {
  const section = $("sectors-section");
  const body = $("sectors");
  body.innerHTML = "";
  if (!sectors || !sectors.available || !(sectors.rows || []).length) {
    section.hidden = !sectors;
    $("sectors-note").textContent = (sectors && sectors.note) || "";
    return;
  }
  section.hidden = false;
  buildTable(body, {columns: sectors.columns, rows: sectors.rows});
  $("sectors-note").textContent = [sectors.basis, sectors.note].filter(Boolean).join(" · ");
}
function renderCountry(country) {
  const section = $("country-section");
  const body = $("country-body");
  body.innerHTML = "";
  if (!country) { section.hidden = true; return; }
  section.hidden = false;
  const title = el("h3", null, (country.code || "—") + " · " + (country.name || ""));
  body.appendChild(title);
  (country.rows || []).forEach((row) => {
    const line = el("div", "kv");
    line.appendChild(el("span", null, row.label +
      (row.source ? "  (" + row.source + ")" : "")));
    line.appendChild(el("span", null, (typeof row.value === "number" ? fmt(row.value, 2) : row.value) +
      (row.unit ? " " + row.unit : "")));
    body.appendChild(line);
  });
  body.appendChild(el("div", "note", country.note || ""));
}
function renderDetails(document_) {
  const section = $("details-section");
  const box = $("details");
  box.innerHTML = "";
  const details = document_.details || {};
  const add = (label, value) => {
    const line = el("div", "kv");
    line.appendChild(el("span", null, label));
    line.appendChild(el("span", null, value));
    box.appendChild(line);
  };
  let printed = 0;
  if (details.oil_module) {
    add("Пик Brent", fmt(details.oil_module.price_peak_usd_bbl, 2) + " USD/bbl");
    add("Среднее / минимум", fmt(details.oil_module.price_mean_usd_bbl, 2) + " / " +
      fmt(details.oil_module.price_trough_usd_bbl, 2) + " USD/bbl");
    add("Месяц закрытия", details.oil_module.closure_months);
    add("Поток пролива", fmt(details.oil_module.hormuz_transit_mb_per_day, 1) + " mb/d; обход " +
      fmt(details.oil_module.bypass_capacity_mb_per_day, 1) + " mb/d");
    printed += 4;
  }
  if (details.gas_module) {
    add("Базовая TTF", fmt(details.gas_module.base_price_usd_per_mmbtu, 2) + " USD/MMBtu (" +
      fmt(details.gas_module.base_price_eur_per_mwh, 1) + " EUR/MWh)");
    add("Henry Hub (ориентир)", fmt(details.gas_module.henry_hub_reference_usd_per_mmbtu, 2) +
      " USD/MMBtu");
    printed += 2;
  }
  if (details.storage) {
    add("ПХГ: старт / конец", fmt(details.storage.start_bcm, 1) + " / " +
      fmt(details.storage.end_bcm, 1) + " bcm");
    printed += 1;
  }
  if (details.dataset) {
    add("Датасет", (details.dataset.dataset_id || "—") + " · " +
      (details.dataset.countries || "—") + " стран × " + (details.dataset.industries || "—") +
      " отраслей");
    printed += 1;
  }
  if (details.asymmetry) {
    add("Асимметрия выгод/ущерб", fmt(details.asymmetry.gain_total, 1) + " / " +
      fmt(details.asymmetry.loss_total, 1) + " USD млн");
    printed += 1;
  }
  if (document_.parameters) {
    const values = document_.parameters.values || {};
    Object.keys(values).forEach((key) => add("Параметр " + key, fmt(values[key], 3)));
  }
  if (!printed && !Object.keys(document_.parameters.values || {}).length) {
    box.appendChild(el("div", "note", "дополнительных деталей нет"));
  }
  const assumptions = document_.assumptions || {};
  if (assumptions.count) {
    box.appendChild(el("div", "note", "допущений в модели: " + assumptions.count +
      (assumptions.note ? " · " + assumptions.note : "")));
  }
  section.hidden = false;
}
function renderChecks(document_) {
  const section = $("checks-section");
  section.hidden = false;
  const box = $("checks");
  box.innerHTML = "";
  Object.keys(document_.checks || {}).forEach((key) => {
    const value = document_.checks[key];
    const chip = el("span", "chip " + (value === true ? "ok" : (value === false ? "bad" : "")));
    chip.textContent = key + ": " + String(value);
    box.appendChild(chip);
  });
  const list = $("limitations");
  list.innerHTML = "";
  (document_.limitations || []).forEach((item) => list.appendChild(el("li", null, item)));
  $("disclaimer").textContent = document_.disclaimer || "";
}
function download() {
  if (!state.result) return;
  const blob = new Blob([JSON.stringify(state.result, null, 2)],
    {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "amfe_" + state.result.scenario.id + "_" +
    (state.result.parameters || {}).country + ".json";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
$("form").addEventListener("submit", runScenario);
$("scenario").addEventListener("change", applyScenario);
$("download").addEventListener("click", download);
loadHealth().catch((error) => setStatus("не удалось загрузить /api/health: " + error, true));
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AMFE Simulator UI MVP (stdlib http.server, RESEARCH_ONLY, aggregate-only)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="TCP port (0 picks a free port)")
    parser.add_argument("--geopolitics", choices=("auto", "artifact", "synthetic"), default="auto",
                        help="source of the geopolitics scenario (default auto)")
    parser.add_argument("--geopolitics-artifact", default=str(DEFAULT_GEOPOLITICS_ARTIFACT),
                        help="precomputed geopolitics report (default build/geopolitics/report.json)")
    parser.add_argument("--check", action="store_true",
                        help="print diagnostics and exit without starting the server")
    parser.add_argument("--quiet", action="store_true", help="log warnings only")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"amfe.simulator_ui {TOOL_VERSION} ({SCHEMA})")
        return 0
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        app = SimulatorApp(geopolitics_artifact=Path(args.geopolitics_artifact),
                           geopolitics_mode=args.geopolitics)
        health = app.health()
    except (ValueError, Unavailable) as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, ensure_ascii=True),
              file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps(health, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    server = create_server(app, args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(json.dumps({"status": STATUS, "url": f"http://{host}:{port}", "schema": SCHEMA,
                      "version": TOOL_VERSION,
                      "geopolitics": args.geopolitics,
                      "artifact": health["geopolitics"]["artifact"]["available"]},
                     ensure_ascii=True), flush=True)
    if not health["geopolitics"]["artifact"]["available"]:
        LOGGER.warning("the geopolitics artifact is unavailable: the scenario is recomputed on the "
                       "declared synthetic fixture")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        LOGGER.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
