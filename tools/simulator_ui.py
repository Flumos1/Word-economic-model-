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
  synthetic fixture (``--geopolitics synthetic``);
* ``institutions`` — block 15 (``amfe.world.institutions``): the declared
  STATUS-QUO/REFORM/DETERIORATION/POLARIZATION overlay of six governance dimensions on an
  illustrative ``EXAMPLE-*`` profile, giving a 0-100 composite, a risk premium in bps and the
  investment/growth channels.  There is no country table: a profile is a geometric example;
* ``technology`` — block 17 (``amfe.world.technology``): logistic adoption → growth accounting
  (TFP, capital, labour) → output and energy over five aggregate sectors, with the declared
  baseline/ai_acceleration/ai_slowdown/energy_transition scenarios and a 10-15 year horizon.

Both block scenarios compute **only** arithmetic on declared assumptions: they are scenario
deformations, not forecasts, probabilities or causal estimates.

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
* **Optional auth, off by default.**  The local demo stays open unless ``--require-auth`` is set;
  then ``/`` and ``/api/health`` remain public, every other endpoint needs ``X-API-Key`` or
  ``Authorization: Bearer``, plans gate ``POST /api/run`` (403) and a per-key hourly quota returns
  429 with ``Retry-After``/``X-RateLimit-*``.  Keys are stored as SHA-256 digests and never logged.

Usage
-----
    python tools/simulator_ui.py                     # http://127.0.0.1:8090 (open local demo)
    python tools/simulator_ui.py --host 0.0.0.0 --port 8091
    python tools/simulator_ui.py --check             # print diagnostics and exit
    python tools/simulator_ui.py --geopolitics synthetic   # ignore the precomputed MRIO artifact

    # demo behind keys and quotas (keys may also come from AMFE_SIM_KEYS)
    python tools/simulator_ui.py --require-auth \
        --api-keys "demo_free_key_01:free,demo_pro_key_01:pro" --quota-per-hour 60

Tests: ``python tests/test_simulator_ui.py`` (see docs/192).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
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
TOOL_VERSION = "1.2.0"
STATUS = "RESEARCH_ONLY"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_QUOTA_PER_HOUR = 60
RATE_WINDOW_SECONDS = 3600
MIN_API_KEY_CHARS = 8
API_KEY_HEADERS = ("X-API-Key", "Api-Key")
PUBLIC_PATHS = frozenset({"/", "/index.html", "/api/health", "/favicon.ico"})
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


class Unauthorized(SimulatorError):
    """No key or an unknown key: 401 with a ``WWW-Authenticate`` challenge."""

    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(401, code, message, details,
                         headers={"WWW-Authenticate": 'ApiKey realm="amfe-simulator"'})


class Forbidden(SimulatorError):
    """The key is valid but its plan does not allow this operation (403)."""

    def __init__(self, code: str, message: str, details=None,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(403, code, message, details, headers=headers)


class RateLimited(SimulatorError):
    """The key exceeded its hourly quota (429); carries the retry/quota headers."""

    def __init__(self, code: str, message: str, retry_after: int, details=None,
                 headers: Mapping[str, str] | None = None) -> None:
        merged = dict(headers or {})
        merged.setdefault("Retry-After", str(max(1, int(retry_after))))
        super().__init__(429, code, message, details, headers=merged)


# ---------------------------------------------------------------------------------------------- auth
@dataclass(frozen=True)
class Plan:
    """A named access level.  ``free`` is read-only; ``pro``/``internal`` may run scenarios."""

    name: str
    can_run: bool
    description: str = ""


PLANS: dict[str, Plan] = {
    "free": Plan("free", False, "чтение каталога сценариев; POST /api/run запрещён"),
    "pro": Plan("pro", True, "полный доступ к сценариям"),
    "internal": Plan("internal", True, "полный доступ (внутренний контур)"),
}


@dataclass(frozen=True)
class Client:
    """An authenticated caller; the plaintext key is never retained or logged."""

    key_sha256: str
    client_id: str
    plan: Plan


class AuthConfigError(RuntimeError):
    """Invalid auth configuration: the CLI refuses to start (fail-closed)."""


def parse_keys(spec: str, plans: Mapping[str, Plan] | None = None) -> dict[str, Client]:
    """Parse ``key[:plan],...`` into ``{sha256(key): Client}`` (digests only).

    Empty items are ignored; a key shorter than :data:`MIN_API_KEY_CHARS`, an unknown plan or a
    duplicate key raises :class:`AuthConfigError` instead of silently weakening the demo.
    """
    plans = dict(plans or PLANS)
    clients: dict[str, Client] = {}
    for item in re.split(r"[\s,;]+", (spec or "").strip()):
        if not item:
            continue
        key, _, plan_name = item.partition(":")
        key = key.strip()
        plan_name = plan_name.strip().lower() or "free"
        if len(key) < MIN_API_KEY_CHARS:
            raise AuthConfigError(
                f"API key is shorter than {MIN_API_KEY_CHARS} characters; "
                "short keys are rejected as trivially guessable")
        if plan_name not in plans:
            raise AuthConfigError(
                f"unknown plan {plan_name!r}; available plans: {', '.join(sorted(plans))}")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if digest in clients:
            raise AuthConfigError("the same API key is listed more than once")
        clients[digest] = Client(key_sha256=digest, client_id=digest[:8], plan=plans[plan_name])
    return clients


@dataclass(frozen=True)
class AuthSettings:
    """Immutable auth configuration (disabled by default; the clock is injectable for tests)."""

    enabled: bool = False
    clients: Mapping[str, Client] = field(default_factory=dict)
    quota_per_hour: int = DEFAULT_QUOTA_PER_HOUR
    clock: Callable[[], float] = time.time

    @classmethod
    def disabled(cls) -> "AuthSettings":
        return cls(False, {}, DEFAULT_QUOTA_PER_HOUR, time.time)


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    window_seconds: int = RATE_WINDOW_SECONDS


class HourlyQuota:
    """In-memory fixed-window hourly quota per key (single process; documented limitation)."""

    def __init__(self, quota_per_hour: int, clock: Callable[[], float] = time.time) -> None:
        self.limit = max(0, int(quota_per_hour))
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def _bucket(self) -> tuple[int, int]:
        now = float(self._clock())
        return int(now // RATE_WINDOW_SECONDS), max(
            1, int(RATE_WINDOW_SECONDS - (now % RATE_WINDOW_SECONDS)))

    def peek(self, client: Client) -> QuotaDecision:
        bucket, reset = self._bucket()
        with self._lock:
            used = self._counts.get((client.client_id, bucket), 0)
        return QuotaDecision(used < self.limit, self.limit, max(0, self.limit - used),
                             reset)

    def consume(self, client: Client) -> QuotaDecision:
        bucket, reset = self._bucket()
        with self._lock:
            if len(self._counts) > 4096:
                self._counts = {key: value for key, value in self._counts.items()
                                if key[1] >= bucket}
            key = (client.client_id, bucket)
            used = self._counts.get(key, 0)
            if used >= self.limit:
                return QuotaDecision(False, self.limit, 0, reset)
            used += 1
            self._counts[key] = used
        return QuotaDecision(True, self.limit, max(0, self.limit - used), reset)


def _quota_headers(decision: QuotaDecision, plan_name: str) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_seconds),
        "X-RateLimit-Window": str(decision.window_seconds),
        "X-Plan": plan_name,
    }


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup that works with ``dict`` and ``HTTPMessage``."""
    if not headers:
        return None
    try:
        value = headers.get(name)  # type: ignore[union-attr]
    except AttributeError:  # pragma: no cover - defensive
        value = None
    if value:
        return str(value)
    lowered = name.lower()
    for key, item in headers.items():
        if str(key).lower() == lowered and item:
            return str(item)
    return None


def _extract_api_key(headers: Mapping[str, str] | None) -> str | None:
    """Read ``X-API-Key``/``Api-Key`` or ``Authorization: Bearer``; never the query string."""
    for name in API_KEY_HEADERS:
        value = _header(headers, name)
        if value:
            return value.strip()
    authorization = _header(headers, "Authorization")
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.strip().lower() != "bearer" or not token.strip():
            raise Unauthorized("INVALID_AUTH_SCHEME",
                               "заголовок Authorization должен быть 'Bearer <key>'")
        return token.strip()
    return None


def resolve_auth_settings(args: argparse.Namespace,
                          environ: Mapping[str, str] | None = None) -> AuthSettings:
    """Build the auth settings from CLI/env; raise :class:`AuthConfigError` (never open silently)."""
    environ = os.environ if environ is None else environ
    explicit = getattr(args, "api_keys", None)
    env_spec = str(environ.get("AMFE_SIM_KEYS") or "")
    spec = (explicit if explicit is not None else env_spec).strip()
    enabled = bool(getattr(args, "require_auth", False))
    quota = int(getattr(args, "quota_per_hour", DEFAULT_QUOTA_PER_HOUR))
    if not enabled:
        if explicit is not None and explicit.strip():
            raise AuthConfigError(
                "--api-keys is set but --require-auth is off; pass --require-auth or drop the keys")
        if env_spec.strip():
            raise AuthConfigError(
                "AMFE_SIM_KEYS is set but --require-auth is off; pass --require-auth or unset it")
        return AuthSettings.disabled()
    if quota < 1:
        raise AuthConfigError("--quota-per-hour must be a positive integer")
    if not spec:
        raise AuthConfigError("--require-auth needs --api-keys or AMFE_SIM_KEYS; refusing to "
                              "serve without keys")
    return AuthSettings(True, parse_keys(spec), quota)



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
    {
        "id": "institutions",
        "title": "Институты (блок 15): реформа, ухудшение, поляризация",
        "family": "institutions",
        "model": "amfe.world.institutions (M15, RESEARCH_ONLY)",
        "model_scenario": "REFORM",
        "stress": False,
        "summary": "Оверлей объявленного сдвига шести измерений качества управления на "
                   "иллюстративный профиль EXAMPLE-*: композит 0–100, премия за риск в б.п. "
                   "(уровень и разброс), Δ инвестиций и Δ потенциального роста. Профили — "
                   "геометрические примеры, а не страновые значения.",
        "params": (),
    },
    {
        "id": "technology",
        "title": "Технологии и ИИ (блок 17): диффузия → TFP → выпуск и энергия",
        "family": "technology",
        "model": "amfe.world.technology (block 17, RESEARCH_ONLY)",
        "model_scenario": "ai_acceleration",
        "stress": False,
        "summary": "Логистическая диффузия внедрения по пяти агрегированным отраслям, growth "
                   "accounting (TFP, капитал, труд) и энергия; сценарии — объявленные деформации "
                   "скорости, потолка, передачи в TFP и энергоэффективности, а не вероятности.",
        "params": (),
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
    "institutions": {
        "magnitude": {"label": "Масштаб сдвига", "unit": "×", "min": 0.25, "max": 3.0,
                      "step": 0.25, "default": 1.0,
                      "help": "множитель объявленных сдвигов сценария (magnitude модуля 15)"},
        "horizon": {"label": "Горизонт", "unit": "лет", "min": 1, "max": 10, "step": 1,
                    "default": 5, "integer": True,
                    "help": "годы оверлея: рампа сценария разворачивает эффект по годам"},
    },
    "technology": {
        "horizon": {"label": "Горизонт", "unit": "лет", "min": 10, "max": 15, "step": 1,
                    "default": 15, "integer": True,
                    "help": "годы диффузии и growth accounting (модуль допускает 1–50 лет)"},
    },
}

#: Illustrative institution profiles and the four declared institution scenarios (block 15).
INSTITUTION_PROFILES = ("EXAMPLE-LOW", "EXAMPLE-MID", "EXAMPLE-HIGH", "EXAMPLE-POLARIZED")
INSTITUTION_SCENARIOS = ("STATUS-QUO", "REFORM", "DETERIORATION", "POLARIZATION")
#: The four declared technology scenarios (block 17).
TECHNOLOGY_SCENARIOS = ("baseline", "ai_acceleration", "ai_slowdown", "energy_transition")

#: Enumerated parameters per family: fixed option lists validated fail-closed.
CHOICE_SPECS: dict[str, dict[str, dict]] = {
    "institutions": {
        "profile": {"label": "Профиль институтов", "options": INSTITUTION_PROFILES,
                    "default": "EXAMPLE-MID",
                    "help": "EXAMPLE-* — иллюстративные профили, не страны и не измерения"},
        "inst_scenario": {"label": "Сценарий институтов", "options": INSTITUTION_SCENARIOS,
                          "default": "REFORM",
                          "help": "объявленный сдвиг шести измерений; не прогноз и не вероятность"},
    },
    "technology": {
        "tech_scenario": {"label": "Технологический сценарий",
                          "options": TECHNOLOGY_SCENARIOS, "default": "ai_acceleration",
                          "help": "объявленные деформации параметров диффузии, а не вероятности"},
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
INSTITUTIONS_LIMITATIONS = (
    "блок 15 — RESEARCH_ONLY и не калиброван: шесть индикаторов, веса, сдвиги и эластичности — "
    "объявленные допущения (A1–A10), а не данные",
    "страновой таблицы нет: профили EXAMPLE-* иллюстративны; число не является рейтингом страны",
    "композит — взвешенная сумма с равными весами; смена весов или эластичностей меняет уровень, "
    "но не знаки (устойчивость не проверялась)",
    "премия за риск — условная величина в б.п., а не суверенный спред и не доходность; "
    "разброс между индикаторами — гипотеза A7, а не измеренная несогласованность правил",
    "сценарий — «что если», а не прогноз: вероятности не оцениваются, причинность не "
    "утверждается (gating=false, causal_claim=false)",
)
TECHNOLOGY_LIMITATIONS = (
    "блок 17 — RESEARCH_ONLY и не калиброван: потолок, скорость, эластичность TFP и "
    "энергопараметры — объявленные допущения, а не оценки",
    "передача во TFP — гипотеза H-005 с постоянной эластичностью; числа не являются «вкладом ИИ» "
    "и не переносятся на реальную экономику",
    "рост капитала и труда экзогенны; инвестиционного решения, износа, рынка труда и обратной "
    "связи спроса нет",
    "агрегат — взвешенное геометрическое среднее по пяти отраслям с фиксированными весами; "
    "отраслевой точности и отраслевого моста в MRIO нет",
    "энергия — арифметика поверх выпуска: нет энергобаланса, структуры топлива, цен и физических "
    "пределов; сценарий не прогноз и не вероятность",
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
    if spec.get("integer"):
        rounded = int(round(value))
        if abs(rounded - value) > 1e-9:
            raise BadRequest("INVALID_PARAM", f"параметр {name!r} должен быть целым числом")
        return rounded
    return _round(value, 6)


def _coerce_choice(name: str, raw: Any, spec: Mapping) -> str:
    """Validate an enumerated parameter against its declared option list (fail-closed)."""
    options = tuple(spec["options"])
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return str(spec["default"])
    if not isinstance(raw, str):
        raise BadRequest("INVALID_CHOICE",
                         f"параметр {name!r} должен быть строкой из {list(options)}",
                         {"param": name, "allowed": list(options)})
    value = raw.strip()
    for option in options:
        if option.lower() == value.lower():
            return option
    raise BadRequest("INVALID_CHOICE",
                     f"неизвестное значение {raw!r} для параметра {name!r}",
                     {"param": name, "allowed": list(options)})


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


def _thin_indices(length: int, limit: int = MAX_PATH_POINTS) -> list[int]:
    """Evenly spaced indices (first and last kept) so a path never exceeds ``limit`` points."""
    if length <= limit:
        return list(range(length))
    step = (length - 1) / float(limit - 1)
    return sorted({int(round(index * step)) for index in range(limit)})


def _module_present(name: str) -> bool:
    """Import a block module without raising: health must stay readable when it is absent."""
    try:
        importlib.import_module(name)
        return True
    except Exception:  # pragma: no cover - environmental failure only
        return False


def _require_module(name: str, label: str):
    """Import a block module fail-closed: a missing module is a 503, never a silent empty block."""
    try:
        return importlib.import_module(name)
    except Exception as error:  # pragma: no cover - environmental failure only
        raise Unavailable("MODULE_UNAVAILABLE",
                          f"{label}: модуль недоступен ({type(error).__name__})") from None


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
                 geopolitics_mode: str = "auto",
                 auth: AuthSettings | None = None) -> None:
        self.geopolitics_artifact = Path(geopolitics_artifact)
        self.geopolitics_mode = str(geopolitics_mode)
        if self.geopolitics_mode not in ("auto", "artifact", "synthetic"):
            raise ValueError("geopolitics_mode must be auto, artifact or synthetic")
        self.auth = auth or AuthSettings.disabled()
        self.quota = (HourlyQuota(self.auth.quota_per_hour, self.auth.clock)
                      if self.auth.enabled else None)
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
            "auth": self._auth_summary(),
            "checks": {
                "oil_module": True,
                "gas_module": True,
                "institutions_module": _module_present("amfe.world.institutions"),
                "technology_module": _module_present("amfe.world.technology"),
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
        elif family == "institutions":
            body = self._run_institutions(spec, params)
        elif family == "technology":
            body = self._run_technology(spec, params)
        else:
            body = self._run_geopolitics(spec, params)
        return self._finalize(spec, params, body)

    def page(self) -> str:
        return PAGE_HTML.replace("__VERSION__", TOOL_VERSION)

    def _auth_summary(self) -> dict:
        """Public auth metadata: no keys, no digests, only modes and limits."""
        if not self.auth.enabled:
            return {
                "required": False,
                "header": "X-API-Key",
                "alternative": "Authorization: Bearer <key>",
                "plans": [],
                "quota_per_hour": None,
                "public_paths": ["/", "/api/health"],
                "note": "локальное демо без аутентификации; для пилота используйте docs/183",
            }
        plans = [{"name": plan.name, "can_run": plan.can_run, "description": plan.description}
                 for plan in PLANS.values() if any(client.plan.name == plan.name
                                                   for client in self.auth.clients.values())]
        return {
            "required": True,
            "header": "X-API-Key",
            "alternative": "Authorization: Bearer <key>",
            "plans": plans,
            "quota_per_hour": self.auth.quota_per_hour,
            "public_paths": ["/", "/api/health"],
            "note": ("ключи хранятся только как SHA-256; квоты — фиксированное окно в памяти "
                     "процесса; это не замена docs/183"),
        }

    # -- parameter handling -----------------------------------------------------------------
    def _scenario_description(self, spec: Mapping) -> dict:
        family = spec["family"]
        numeric = PARAM_SPECS.get(family, {})
        choices = CHOICE_SPECS.get(family, {})
        defaults = {name: float(item["default"]) for name, item in numeric.items()}
        for name, item in choices.items():
            defaults[name] = item["default"]
        country_default = COUNTRY_DEFAULT.get(family)
        if country_default:
            defaults.setdefault("country", country_default)
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
            "choices": {name: {"label": item["label"], "options": list(item["options"]),
                               "default": item["default"], "help": item["help"]}
                        for name, item in choices.items()},
            "defaults": defaults,
            "country_default": country_default,
        }

    def _validated_params(self, spec: Mapping, payload: Mapping) -> dict:
        family = spec["family"]
        numeric = PARAM_SPECS.get(family, {})
        choices = CHOICE_SPECS.get(family, {})
        params: dict[str, Any] = {"applied": {}, "notes": []}
        for name, param_spec in numeric.items():
            raw = payload.get(name, param_spec["default"])
            params[name] = _coerce_number(name, raw, param_spec)
        for name, choice_spec in choices.items():
            params[name] = _coerce_choice(name, payload.get(name), choice_spec)
        if family == "oil":
            params["applied"] = {"base_price": params["price"],
                                 "closure_share": _round(params["shock"] / 100.0, 6)}
        elif family == "gas":
            params["applied"] = {"ttf_price_eur_per_mwh": params["price"],
                                 "pipeline_loss_bcm_y": params["shock"]}
        elif family == "institutions":
            params["applied"] = {"magnitude": float(params["magnitude"]),
                                 "horizon_years": int(params["horizon"]),
                                 "profile": params["profile"],
                                 "scenario_id": params["inst_scenario"]}
        elif family == "technology":
            params["applied"] = {"horizon_years": int(params["horizon"]),
                                 "scenario_id": params["tech_scenario"]}
        country_default = COUNTRY_DEFAULT.get(family)
        if country_default:
            params["country"] = _coerce_country(payload.get("country"), country_default)
        else:
            ignored_country = payload.get("country")
            if ignored_country not in (None, ""):
                params["notes"].append(
                    "параметр country неприменим к этому блоку и проигнорирован: страновых "
                    "значений в нём нет")
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
                "country": params.get("country"),
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

    # -- institutions family (block 15) -----------------------------------------------------
    def _run_institutions(self, spec: Mapping, params: Mapping) -> dict:
        module = _require_module("amfe.world.institutions", "блок 15")
        horizon = int(params["horizon"])
        magnitude = float(params["magnitude"])
        try:
            profile = module.find_example_profile(params["profile"])
            declared = {item.scenario_id: item for item in module.default_scenarios()}
            base_scenario = declared[params["inst_scenario"]]
            scenario = module.InstitutionScenario(
                scenario_id=base_scenario.scenario_id,
                title_ru=base_scenario.title_ru,
                kind=base_scenario.kind,
                deltas=dict(base_scenario.deltas),
                magnitude=magnitude,
                ramp_years=base_scenario.ramp_years,
                notes=base_scenario.notes)
            parameters = module.OverlayParameters(horizon_years=horizon)
            report = module.scenario_overlay(profile, scenario, parameters)
            modifier = module.as_scenario_modifier(report)
        except module.InstitutionError as error:
            raise BadRequest("MODEL_INPUT", f"блок 15 отклонил вход: {error}") from None
        except Exception as error:  # pragma: no cover - defensive
            raise Unavailable("MODEL_FAILED",
                              f"блок 15 отказал: {type(error).__name__}") from None

        profile_block = report["profile"]
        overlay = report["overlay"]
        base = overlay["base"]
        full = overlay["full_effect"]
        path = list(overlay["path"])
        premium = full["risk_premium"]
        indicator_labels = [item["key"] for item in profile_block["indicator_metadata"]]
        indicator_values = [profile_block["indicators"][key] for key in indicator_labels]
        labels = [f"Г{row['year']}" for row in path]
        cards = [
            _card("score_base", "Композит, база", _round(profile_block["score"], 2), "из 100",
                  f"разброс {profile_block['dispersion']:.2f}; профиль "
                  f"{profile_block['name']}", emphasis=True),
            _card("delta_score", "Δ композита", _round(full["delta_score_points"], 2), "пункты",
                  f"запрошено {full['requested_delta_points']:.2f}; рампа "
                  f"{overlay['ramp_years']} лет", emphasis=True),
            _card("premium_delta", "Δ премии за риск", _round(premium["delta_bps"], 1), "б.п.",
                  f"уровень {premium['score_component_bps']:.1f} + разброс "
                  f"{premium['dispersion_component_bps']:.1f}", emphasis=True),
            _card("premium_level", "Премия, уровень", _round(premium["level_bps"], 1), "б.п.",
                  f"база {premium['base_bps']:.0f}; потолок {premium['cap_bps']:.0f}"),
            _card("investment_delta", "Δ инвестиций", _round(full["investment"]["delta_pp"], 3),
                  "п.п. ВВП", f"эластичность {full['investment']['elasticity_pp_per_point']:.2f} "
                             "п.п. на пункт", emphasis=True),
            _card("growth_delta", "Δ роста", _round(full["growth"]["delta_pp"], 3), "п.п.",
                  "потенциального роста, а не факта"),
        ]
        charts = [
            {"kind": "line", "title": "Премия за риск по годам", "unit": "б.п.",
             "labels": labels,
             "lines": [{"label": "Премия",
                        "values": _series([row["risk_premium_bps"] for row in path], 2)}]},
            {"kind": "bar", "title": "Шесть измерений профиля", "unit": "0–100",
             "labels": indicator_labels,
             "lines": [{"label": "Профиль", "values": _series(indicator_values, 2)}]},
        ]
        table = {
            "title": "Оверлей по годам (профиль × сценарий)",
            "columns": [
                {"key": "year", "label": "Год", "unit": "", "digits": 0},
                {"key": "ramp", "label": "Рампа", "unit": "", "digits": 2},
                {"key": "score", "label": "Композит", "unit": "0–100", "digits": 2},
                {"key": "premium", "label": "Премия", "unit": "б.п.", "digits": 1},
                {"key": "investment", "label": "Инвестиции", "unit": "доля ВВП", "digits": 4},
                {"key": "growth", "label": "Δ роста", "unit": "п.п.", "digits": 3},
            ],
            "rows": [
                {"year": row["year"], "ramp": _round(row["ramp"], 2),
                 "score": _round(row["score"], 2),
                 "premium": _round(row["risk_premium_bps"], 1),
                 "investment": _round(row["investment_ratio"], 4),
                 "growth": _round(row["delta_growth_pp"], 3)}
                for row in path
            ],
            "note": ("рампа — r_t=min(1,t/ramp_years); полный эффект — последняя строка; "
                     "числа — арифметика на объявленных допущениях"),
        }
        details = {
            "institutions_module": {
                "module": "amfe.world.institutions",
                "profile": profile_block["name"],
                "profile_source": profile_block["source"],
                "profile_note": profile_block["note"],
                "score": _round(profile_block["score"], 4),
                "dispersion": _round(profile_block["dispersion"], 4),
                "dimensions": {name: _round(value, 4)
                               for name, value in profile_block["dimensions"].items()},
                "parameters": {
                    "base_risk_premium_bps": _round(report["parameters"]["base_risk_premium_bps"], 2),
                    "beta_bps_per_point": _round(report["parameters"]["beta_bps_per_point"], 2),
                    "gamma_bps_per_point_dispersion":
                        _round(report["parameters"]["gamma_bps_per_point_dispersion"], 2),
                    "premium_cap_bps": _round(report["parameters"]["premium_cap_bps"], 2),
                    "base_investment_ratio":
                        _round(report["parameters"]["base_investment_ratio"], 4),
                    "elasticity_investment_pp_per_point":
                        _round(report["parameters"]["elasticity_investment_pp_per_point"], 4),
                    "elasticity_growth_pp_per_point":
                        _round(report["parameters"]["elasticity_growth_pp_per_point"], 4),
                    "horizon_years": report["parameters"]["horizon_years"],
                },
                "modifier": {
                    "risk_premium_bps": _round(modifier["risk_premium_bps"], 2),
                    "investment_delta_pp": _round(modifier["investment_delta_pp"], 4),
                    "growth_delta_pp": _round(modifier["growth_delta_pp"], 4),
                    "applies_to": list(modifier["applies_to"]),
                    "gating": modifier["gating"],
                },
                "data_status": report["data_status"],
                "not_calibrated": report["not_calibrated"],
                "ramp_years": overlay["ramp_years"],
                "converged": overlay["converged"],
            },
            "overlay_note": ("институты — модификатор сценариев, а не модель страны: три канала "
                             "(премия, инвестиции, рост) при допущениях A1–A10"),
        }
        checks = {
            "score_bounds": bool(report["checks"]["score_bounds"]),
            "indicator_bounds": bool(report["checks"]["indicator_bounds"]),
            "weights_normalised": bool(report["checks"]["weights_normalised"]),
            "premium_non_negative": bool(report["checks"]["premium_non_negative"]),
            "investment_bounds": bool(report["checks"]["investment_bounds"]),
            "path_monotone_ramp": bool(report["checks"]["path_monotone_ramp"]),
            "no_raw_data": bool(report["checks"]["no_raw_data"]),
            "path_years": len(path) <= MAX_PATH_POINTS,
            "aggregate_only": True,
        }
        return {
            "model_scenario": report["scenario"]["scenario_id"],
            "model_digest": report["digest"],
            "cards": cards,
            "prices": {
                "available": False,
                "note": "блок 15 не считает цены: только композит, премия и макро-каналы",
            },
            "cpi": {
                "available": False,
                "note": "CPI в блоке 15 не считается; премия — не инфляция",
            },
            "risk": {
                "available": True,
                "score_0_100": _round(min(100.0, max(0.0, premium["level_bps"] / 20.0)), 2),
                "band": ("low" if premium["level_bps"] < 200.0 else
                         "elevated" if premium["level_bps"] < 600.0 else
                         "high" if premium["level_bps"] < 1200.0 else "severe"),
                "reasons": [f"Δ премии {premium['delta_bps']:+.1f} б.п.",
                            f"Δ разброса {full['delta_dispersion']:+.2f} пункта"],
                "note": ("условная шкала для отображения: премия/20, а не вероятность и не "
                         "суверенный спред"),
                "source": "amfe.world.institutions (масштабирование для UI)",
            },
            "charts": charts,
            "table": table,
            "sectors": {
                "available": False,
                "rows": [],
                "note": "отраслевого разреза в блоке 15 нет: три макро-канала, а не отрасли",
            },
            "country": {
                "available": False,
                "code": None,
                "name": "—",
                "note": ("страновой таблицы в блоке 15 нет: профили EXAMPLE-* иллюстративны; "
                         "блок не отвечает «какие институты в стране X»"),
                "rows": [],
            },
            "details": details,
            "checks": checks,
            "assumptions": {
                "count": len(report["assumptions"]),
                "items": [{"name": item["id"], "value": item["statement"],
                           "unit": item["kind"], "source_class": item["kind"]}
                          for item in report["assumptions"][:20]],
            },
            "limitations": list(INSTITUTIONS_LIMITATIONS),
            "provenance": {
                "models": ["amfe.world.institutions (block 15, M15)"],
                "module_id": "world.institutions",
                "identity": ("профиль EXAMPLE-* + объявленный сдвиг шести измерений; "
                             "не страновые значения и не результат измерения"),
                "dataset": "declared assumptions only (no third-party data is read)",
            },
        }

    # -- technology family (block 17) -------------------------------------------------------
    def _run_technology(self, spec: Mapping, params: Mapping) -> dict:
        module = _require_module("amfe.world.technology", "блок 17")
        horizon = int(params["horizon"])
        scenario_id = params["tech_scenario"]
        try:
            report = module.build_report([scenario_id], None, horizon_years=horizon)
        except module.TechnologyError as error:
            raise BadRequest("MODEL_INPUT", f"блок 17 отклонил вход: {error}") from None
        except Exception as error:  # pragma: no cover - defensive
            raise Unavailable("MODEL_FAILED",
                              f"блок 17 отказал: {type(error).__name__}") from None
        scenario = (report.get("scenarios") or [None])[0]
        if not scenario or not scenario.get("aggregate"):
            raise Unavailable("MODEL_FAILED", "блок 17 вернул пустой агрегат")
        aggregate = scenario["aggregate"]
        horizon_block = aggregate["horizon"]
        contributions = horizon_block["contributions_log"]
        paths = aggregate["paths"]
        indices = _thin_indices(len(paths["adoption"]))
        labels = [f"Г{index}" for index in indices]
        thinned = {name: [paths[name][index] for index in indices] for name in paths}
        sector_order = [sector for sector in report.get("sectors") or []]
        sectors = scenario.get("sectors") or {}
        row_order = [sector for sector in sector_order if sector in sectors]
        cards = [
            _card("adoption", "Внедрение, конец", _round(horizon_block["adoption"], 3), "доля",
                  f"{scenario['title_ru']}; горизонт {horizon} лет", emphasis=True),
            _card("tfp", "TFP, прирост", _round(horizon_block["tfp_gain_pct"], 2), "%",
                  "логарифмическое приближение, не оценка"),
            _card("output", "Выпуск, прирост", _round(horizon_block["output_gain_pct"], 2), "%",
                  "при постоянной отдаче и экзогенных K, L", emphasis=True),
            _card("energy", "Энергия, изменение",
                  _round(horizon_block["energy_change_pct"], 2), "%",
                  "арифметика поверх выпуска"),
            _card("energy_intensity", "Энергоёмкость",
                  _round(horizon_block["energy_intensity_change_pct"], 2), "%",
                  "энергия / выпуск", emphasis=True),
            _card("tfp_share", "Вклад TFP", _round(contributions["tfp"], 4), "лог-п.",
                  f"капитал {contributions['capital']:.4f} · труд "
                  f"{contributions['labour']:.4f}"),
        ]
        charts = [
            {"kind": "line", "title": "Внедрение, TFP, выпуск, энергия (× к базе)",
             "unit": "× к базе", "labels": labels,
             "lines": [
                 {"label": "Внедрение",
                  "values": _series(thinned["adoption"], 3)},
                 {"label": "TFP", "values": _series(thinned["tfp_ratio"], 3)},
                 {"label": "Выпуск", "values": _series(thinned["output_ratio"], 3)},
                 {"label": "Энергия", "values": _series(thinned["energy_ratio"], 3)},
             ]},
            {"kind": "line", "title": "Энергоёмкость (× к базе)", "unit": "× к базе",
             "labels": labels,
             "lines": [{"label": "Энергоёмкость",
                        "values": _series(thinned["energy_intensity_ratio"], 3)}]},
        ]
        table = {
            "title": "Агрегат по годам (× к базе)",
            "columns": [
                {"key": "year", "label": "Год", "unit": "", "digits": 0},
                {"key": "adoption", "label": "Внедрение", "unit": "доля", "digits": 3},
                {"key": "tfp", "label": "TFP", "unit": "×", "digits": 3},
                {"key": "output", "label": "Выпуск", "unit": "×", "digits": 3},
                {"key": "energy", "label": "Энергия", "unit": "×", "digits": 3},
            ],
            "rows": [
                {"year": index,
                 "adoption": _round(paths["adoption"][index], 3),
                 "tfp": _round(paths["tfp_ratio"][index], 3),
                 "output": _round(paths["output_ratio"][index], 3),
                 "energy": _round(paths["energy_ratio"][index], 3)}
                for index in indices
            ],
            "note": ("путь прорежен до 12 точек (первый и последний сохранены); полный путь — "
                     "в отчёте модуля, а не в демо"),
        }
        sector_rows = []
        for sector in row_order:
            item = sectors[sector]
            block = item["horizon"]
            sector_rows.append({
                "sector": item["title_ru"],
                "adoption": _round(block["adoption"], 3),
                "tfp": _round(block["tfp_gain_pct"], 2),
                "output": _round(block["output_gain_pct"], 2),
                "energy": _round(block["energy_change_pct"], 2),
            })
        details = {
            "technology_module": {
                "module": report["module"]["module_id"],
                "version": report["module"]["version"],
                "block": report["block"],
                "data_class": report["data_class"],
                "calibration": report["calibration"],
                "horizon_years": report["horizon_years"],
                "growth": report["growth"],
                "sectors": list(report.get("sectors") or []),
                "scenario_parameters": scenario["parameters"],
                "aggregate": {
                    "adoption": _round(horizon_block["adoption"], 4),
                    "delta_adoption": _round(horizon_block["delta_adoption"], 4),
                    "tfp_gain_pct": _round(horizon_block["tfp_gain_pct"], 3),
                    "output_gain_pct": _round(horizon_block["output_gain_pct"], 3),
                    "energy_change_pct": _round(horizon_block["energy_change_pct"], 3),
                    "energy_intensity_change_pct":
                        _round(horizon_block["energy_intensity_change_pct"], 3),
                    "contributions_log": {key: _round(value, 6)
                                          for key, value in contributions.items()},
                    "identity_residual": horizon_block["identity_residual"],
                    "weights_renormalised": aggregate["weights_renormalised"],
                },
                "module_note": "логистика → growth accounting; сценарий — деформация, не вероятность",
            },
            "aggregate_note": aggregate["note"],
        }
        checks = {key: bool(value) for key, value in report["checks"].items()
                  if isinstance(value, bool)}
        checks.update({
            "path_points": len(labels) <= MAX_PATH_POINTS,
            "horizon_in_ui_range": 10 <= horizon <= 15,
            "no_raw_series": bool(report["checks"].get("no_raw_series", True)),
            "aggregate_only": True,
        })
        return {
            "model_scenario": scenario_id,
            "model_digest": report["digest"],
            "cards": cards,
            "prices": {
                "available": False,
                "note": "блок 17 не считает цены: диффузия → TFP → выпуск/энергия",
            },
            "cpi": {
                "available": False,
                "note": "CPI в блоке 17 не считается; это не прогноз инфляции",
            },
            "risk": {
                "available": False,
                "note": ("индекса риска в блоке 17 нет; смотрите тождество growth accounting "
                         "и разложение вкладов"),
            },
            "charts": charts,
            "table": table,
            "sectors": {
                "available": bool(sector_rows),
                "basis": "пять агрегированных отраслей; рост внедрения → TFP → выпуск/энергия",
                "columns": [
                    {"key": "sector", "label": "Отрасль", "unit": ""},
                    {"key": "adoption", "label": "Внедрение", "unit": "доля", "digits": 3},
                    {"key": "tfp", "label": "TFP", "unit": "%", "digits": 2},
                    {"key": "output", "label": "Выпуск", "unit": "%", "digits": 2},
                    {"key": "energy", "label": "Энергия", "unit": "%", "digits": 2},
                ],
                "rows": sector_rows,
                "note": ("отраслевые числа — те же допущения, что и агрегат; отраслевого моста "
                         "в MRIO нет"),
            },
            "country": {
                "available": False,
                "code": None,
                "name": "—",
                "note": "странового разреза в блоке 17 нет: агрегированные отрасли, не страны",
                "rows": [],
            },
            "details": details,
            "checks": checks,
            "assumptions": {
                "count": len(report["assumptions"]),
                "items": [{"name": item.get("name"), "value": item.get("value"),
                           "unit": item.get("unit"), "source_class": item.get("source_class")}
                          for item in report["assumptions"][:20]],
            },
            "limitations": list(TECHNOLOGY_LIMITATIONS),
            "provenance": {
                "models": ["amfe.world.technology (block 17)"],
                "module_id": report["module"]["module_id"],
                "identity": ("логистическая диффузия → growth accounting → выпуск/энергия на "
                             "объявленных допущениях; не оценка эффекта ИИ"),
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
    def dispatch(self, method: str, target: str, body: bytes,
                 headers: Mapping[str, str] | None = None) -> Response:
        if len(target) > MAX_TARGET_CHARS:
            return self._error_response(BadRequest("TARGET_TOO_LONG",
                                                   "слишком длинный запрос"))
        path = (urlsplit(target).path or "/").rstrip("/") or "/"
        try:
            if method == "OPTIONS":
                return Response(204, b"", "text/plain; charset=utf-8",
                                (("Allow", "GET, HEAD, POST, OPTIONS"),))
            auth_headers: dict[str, str] = {}
            if self.auth.enabled and path not in PUBLIC_PATHS:
                auth_headers = self._authorize(method, path, headers)
            if method in ("GET", "HEAD"):
                response = self._handle_get(path)
            elif method == "POST":
                if path != "/api/run":
                    raise NotFound("NOT_FOUND", f"нет такого ресурса: {path}")
                response = self._json(200, self.run(self._parse_body(body)))
            else:
                raise BadRequest("METHOD_NOT_ALLOWED", f"метод {method} не поддерживается")
            return self._with_headers(response, auth_headers)
        except SimulatorError as error:
            return self._error_response(error)

    def _authorize(self, method: str, path: str,
                   headers: Mapping[str, str] | None) -> dict[str, str]:
        """Authenticate, check the plan and consume one quota unit (headers on success)."""
        client = self._authenticate(headers)
        if path == "/api/run" and method == "POST" and not client.plan.can_run:
            decision = self.quota.peek(client)  # type: ignore[union-attr]
            quota_headers = _quota_headers(decision, client.plan.name)
            raise Forbidden(
                "PLAN_FORBIDDEN",
                f"план {client.plan.name!r} не позволяет запускать сценарии",
                {"plan": client.plan.name, "required_scope": "run",
                 "allowed_plans": [plan.name for plan in PLANS.values() if plan.can_run]},
                headers=quota_headers)
        decision = self.quota.consume(client)  # type: ignore[union-attr]
        quota_headers = _quota_headers(decision, client.plan.name)
        if not decision.allowed:
            raise RateLimited(
                "RATE_LIMITED",
                f"квота {decision.limit} запросов в час для этого ключа исчерпана",
                decision.reset_seconds,
                {"plan": client.plan.name, "quota_per_hour": decision.limit,
                 "retry_after_seconds": decision.reset_seconds,
                 "window_seconds": decision.window_seconds},
                headers=quota_headers)
        LOGGER.info("auth ok client=%s plan=%s remaining=%s", client.client_id,
                    client.plan.name, decision.remaining)
        return quota_headers

    def _authenticate(self, headers: Mapping[str, str] | None) -> Client:
        token = _extract_api_key(headers)
        if not token:
            LOGGER.warning("auth rejected: missing API key")
            raise Unauthorized("MISSING_API_KEY",
                               "нужен ключ в X-API-Key или Authorization: Bearer")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found: Client | None = None
        for known, client in self.auth.clients.items():
            if hmac.compare_digest(digest, known):
                found = client
        if found is None:
            LOGGER.warning("auth rejected: invalid API key")
            raise Unauthorized("INVALID_API_KEY", "ключ недействителен")
        return found

    @staticmethod
    def _with_headers(response: Response, extra: Mapping[str, str]) -> Response:
        if not extra:
            return response
        return Response(response.status, response.payload, response.content_type,
                        tuple(response.headers) + tuple(extra.items()))

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
        response = app.dispatch(method, self.path, body, headers=self.headers)
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
        <div id="country-wrap">
          <label for="country">Страна</label>
          <select id="country"></select>
          <div class="help">для страновых эффектов; где модель их не считает — честная пометка</div>
        </div>
        <div id="auth-wrap" hidden>
          <label for="api-key">API-ключ (режим --require-auth)</label>
          <input id="api-key" type="password" autocomplete="off" spellcheck="false">
          <div class="help">уходит только в заголовке X-API-Key, не сохраняется</div>
        </div>
        <div id="geo-wrap" hidden>
          <label for="geo_scenario">Геополитический сценарий</label>
          <select id="geo_scenario"></select>
          <div class="help">сценарии протокола protocols/geopolitics_scenarios.json</div>
        </div>
      </div>
      <div class="grid" id="numbers" style="margin-top:12px"></div>
      <div class="grid" id="choices" style="margin-top:12px"></div>
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
  $("auth-wrap").hidden = !(health.auth && health.auth.required);
  applyScenario();
}

function applyScenario() {
  const spec = state.specs[$("scenario").value];
  if (!spec) return;
  $("scenario-help").textContent = spec.model + " · " + spec.summary;
  const numbers = $("numbers");
  numbers.innerHTML = "";
  Object.keys(spec.params || {}).forEach((name) => {
    const param = spec.params[name];
    const wrap = el("div");
    const label = el("label", null, param.label + (param.unit ? ", " + param.unit : ""));
    label.setAttribute("for", "num-" + name);
    const input = document.createElement("input");
    input.type = "number";
    input.id = "num-" + name;
    input.dataset.name = name;
    input.step = (param.integer ? 1 : "any");
    input.min = param.min;
    input.max = param.max;
    input.value = param.default;
    wrap.appendChild(label);
    wrap.appendChild(input);
    wrap.appendChild(el("div", "help",
      (param.help || "") + " (" + param.min + "…" + param.max + ")"));
    numbers.appendChild(wrap);
  });
  const choices = $("choices");
  choices.innerHTML = "";
  Object.keys(spec.choices || {}).forEach((name) => {
    const item = spec.choices[name];
    const wrap = el("div");
    const label = el("label", null, item.label);
    label.setAttribute("for", "choice-" + name);
    const select = document.createElement("select");
    select.id = "choice-" + name;
    select.dataset.name = name;
    (item.options || []).forEach((option) => {
      const node = el("option", null, option);
      node.value = option;
      select.appendChild(node);
    });
    select.value = (spec.defaults || {})[name] || item.default;
    wrap.appendChild(label);
    wrap.appendChild(select);
    wrap.appendChild(el("div", "help", item.help || ""));
    choices.appendChild(wrap);
  });
  const defaults = spec.defaults || {};
  $("country-wrap").hidden = !spec.country_default;
  $("geo-wrap").hidden = spec.family !== "geopolitics";
  if (defaults.country) $("country").value = defaults.country;
  if (defaults.geo_scenario) $("geo_scenario").value = defaults.geo_scenario;
}

function buildPayload() {
  const spec = state.specs[$("scenario").value];
  const payload = {scenario: spec.id};
  if (spec.country_default) payload.country = $("country").value;
  Object.keys(spec.params || {}).forEach((name) => {
    const node = $("num-" + name);
    if (node && node.value !== "") payload[name] = Number(node.value);
  });
  Object.keys(spec.choices || {}).forEach((name) => {
    const node = $("choice-" + name);
    if (node) payload[name] = node.value;
  });
  if (spec.family === "geopolitics") payload.geo_scenario = $("geo_scenario").value;
  return payload;
}

async function runScenario(event) {
  if (event) event.preventDefault();
  const payload = buildPayload();
  const headers = {"Content-Type": "application/json"};
  const apiKey = ($("api-key").value || "").trim();
  if (apiKey) headers["X-API-Key"] = apiKey;
  $("run").disabled = true;
  setStatus("считаю…", false);
  try {
    const response = await fetch("/api/run", {
      method: "POST", headers: headers,
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
    parser.add_argument("--require-auth", action="store_true",
                        help="require an API key for everything except / and /api/health")
    parser.add_argument("--api-keys", default=None,
                        help="key[:plan],... (free|pro|internal); env AMFE_SIM_KEYS also works")
    parser.add_argument("--quota-per-hour", type=int, default=DEFAULT_QUOTA_PER_HOUR,
                        help=f"per-key hourly quota in --require-auth mode "
                             f"(default {DEFAULT_QUOTA_PER_HOUR})")
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
        auth = resolve_auth_settings(args)
    except AuthConfigError as error:
        print(json.dumps({"status": "BLOCKED_CONFIG", "error": str(error)}, ensure_ascii=True),
              file=sys.stderr)
        return 2
    try:
        app = SimulatorApp(geopolitics_artifact=Path(args.geopolitics_artifact),
                           geopolitics_mode=args.geopolitics, auth=auth)
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
                      "auth": auth.enabled,
                      "quota_per_hour": auth.quota_per_hour if auth.enabled else None,
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
