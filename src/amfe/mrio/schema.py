"""Canonical MRIO dataset schema for the AMFE P2 world model (docs/121).

Scope
-----
This module defines the *interface* that the rest of the project consumes: a multi-regional
input-output (MRIO) table at country x industry level with one consistent value base.  It is
deliberately data-free and dependency-free (standard library only, like
:mod:`amfe.resources.io`), so the CI runner needs no numpy and no third-party downloads.

Canonical accounting identities
-------------------------------
Let ``n = len(countries) * len(industries)`` and index ``(country, industry)`` pairs in a fixed
order.  All values are flows in ``USD_mn`` for one reference year.

* row balance (use side):      ``x[i] = sum_j Z[i][j] + y[i]``
* column balance (cost side):  ``x[j] = sum_i Z[i][j] + va[j]``
* global identity:             ``sum(va) = sum(y)`` (for a closed world: country blocks plus a
  rest-of-world block; net trade imbalances stay *inside* the final-demand vector)

The identities are checked, not assumed (EQ-IO-001 in docs/53: ``x = A x + y`` is
SPECIFIED_ONLY, its empirical validity is an open question).  The validator reports the largest
scaled residual and the country totals; the loader is fail-closed: a dataset that does not
satisfy the schema or the identities is refused instead of being silently truncated.

P2 scale gate
-------------
``MIN_COUNTRIES = 10`` and ``MIN_INDUSTRIES = 30`` are the P2 requirements (10+ countries x 30+
industries).  They are enforced by :func:`check_p2_scale`, not by the dataset constructor, so
small synthetic fixtures remain usable in tests.

Public API
----------
``MRIODataset``        immutable, normalised dataset (construct through :func:`dataset_from_document`).
``ValidationReport``   checks, metrics and errors for one dataset.
``validate_dataset``   full validation (structure + scale + identities).
``SourceEntry``        one row of the source/licence matrix (docs/121).
``SOURCE_MATRIX``      curated, dated facts about the candidate MRIO sources.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: Schema identifier written into every dataset document and manifest.
SCHEMA = "amfe.mrio.dataset.v1"
SCHEMA_VERSION = "1.0.0"
#: The only accepted value base: millions of US dollars, current prices (producer values).
CANONICAL_UNITS = "USD_mn"
ALLOWED_UNITS = (CANONICAL_UNITS,)
#: P2 gate: at least 10 countries and at least 30 industries.
MIN_COUNTRIES = 10
MIN_INDUSTRIES = 30
#: Default relative tolerance for the accounting identities.
TOLERANCE = 1e-6
#: Allowed statuses of a local dataset / source in the manifest.
STATUS_AVAILABLE = "AVAILABLE"
STATUS_BLOCKED = "BLOCKED_DATA"
DATASET_STATUSES = (STATUS_AVAILABLE, STATUS_BLOCKED)
#: Source access statuses (a source may be usable but require a manual step or a key).
SOURCE_STATUSES = (
    "AVAILABLE_API",        # programmatic API, no key needed (verified)
    "AVAILABLE_MANUAL",    # files via a web page / archive, no API (verified)
    "AVAILABLE",           # reachable transport (not a data source itself)
    "BLOCKED_KEY",         # requires a registered API key
    "BLOCKED_LICENCE",     # requires registration / licence agreement / purchase
    "UNVERIFIED",          # could not be verified automatically in this wave
)
_DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class MRIOError(ValueError):
    """Base class for MRIO data errors."""


class SchemaError(MRIOError):
    """The document does not satisfy the canonical schema (shape, labels, units)."""


class ValidationError(MRIOError):
    """The dataset is structurally sound but violates an accounting identity or the P2 gate."""


class DataUnavailable(MRIOError):
    """Fail-closed: the requested local dataset is absent, unreadable or not trustworthy."""


# --------------------------------------------------------------------------------------
# Source / licence matrix (docs/121; facts verified 2026-11-14, see ``checked_at``)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceEntry:
    """One candidate MRIO source: coverage, licence, access and verification status."""

    source_id: str
    name: str
    provider: str
    countries: str
    industries: str
    years: str
    units: str
    licence: str
    licence_verified: bool
    access: str
    status: str
    url: str
    checked_at: str
    check: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise SchemaError(f"{self.source_id}: unknown source status {self.status!r}")
        if not self.source_id or not self.url:
            raise SchemaError("source_id and url must be non-empty")

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "provider": self.provider,
            "countries": self.countries,
            "industries": self.industries,
            "years": self.years,
            "units": self.units,
            "licence": self.licence,
            "licence_verified": self.licence_verified,
            "access": self.access,
            "status": self.status,
            "url": self.url,
            "checked_at": self.checked_at,
            "check": self.check,
            "notes": self.notes,
        }


#: Verified facts recorded 2026-11-14 by real HTTP probes (docs/121 §1).  ``None`` means "not
#: established by this probe" -- never guessed; the note says where to look it up.
SOURCE_MATRIX: tuple[SourceEntry, ...] = (
    SourceEntry(
        source_id="oecd_icio",
        name="OECD Inter-Country Input-Output tables (ICIO) and extended ICIO",
        provider="OECD",
        countries="не подтверждено этой проверкой (по документации OECD — матрица экономик, точное "
                  "число в ReadMe релиза)",
        industries="не подтверждено этой проверкой (см. ReadMe релиза)",
        years="1995-2020 (2023 release); 1995-2022 (2025 release, блоки 1995-2000 / 2001-2005 / "
              "2006-2010 / 2011-2015 / 2016-2022) — подтверждено страницей датасета",
        units="USD млн (current prices; проверить в ReadMe)",
        licence="OECD Terms and Conditions — проверить перед загрузкой",
        licence_verified=False,
        access="файлы через OECD Data Explorer / OECD iLibrary (ReadMe + csv/Rdata по блокам), "
               "API-ключа нет; автоматической выгрузки в этой волне не было",
        status="AVAILABLE_MANUAL",
        url="https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html",
        checked_at="2026-11-14",
        check="браузерный клиент (HEAD/GET): HTTP 200, на странице найдены блоки годов и ReadMe; "
              "urllib с UA 'AMFE-MRIO-data-check/1.0': HTTP 403 (анти-бот защита) — скачивание "
              "только вручную из браузера; ICIO НЕ найден как dataflow в OECD SDMX (36 МБ список "
              "dataflow, 200 — строки 'ICIO' нет)",
        notes="основной кандидат на 10+ стран x 30+ отраслей; ключ не нужен, но нужен ручной шаг "
              "скачивания и проверка лицензии",
    ),
    SourceEntry(
        source_id="wiod_2016",
        name="World Input-Output Database (WIOD) 2016 release",
        provider="University of Groningen, GGDC",
        countries="43 (28 стран ЕС + 15 других крупных стран) + RoW — подтверждено страницей релиза",
        industries="не подтверждено этой проверкой (в релизе отраслевая разбивка; точное число в "
                   "документации)", 
        years="2000-2014 — подтверждено страницей релиза",
        units="USD млн (current prices; там же таблицы в предыдущих ценах)",
        licence="Creative Commons Attribution 4.0 International (CC BY 4.0) — подтверждено "
                "страницей релиза",
        licence_verified=True,
        access="файлы по ссылкам со страницы релиза (и через Wayback Machine); API нет",
        status="AVAILABLE_MANUAL",
        url="https://www.rug.nl/ggdc/valuechain/wiod/wiod-2016-release",
        checked_at="2026-11-14",
        check="HTTP 200 GET; страница содержит формулировку CC BY 4.0, годы 2000-2014 и состав "
              "28+15 стран",
        notes="самая простая лицензия из крупных MRIO; годы заканчиваются 2014",
    ),
    SourceEntry(
        source_id="eurostat_figaro",
        name="Eurostat FIGARO (EU inter-country supply, use and input-output tables)",
        provider="Eurostat",
        countries="ЕС + основные торговые партнёры (точный состав набора — в метаданных Eurostat; "
                  "этой проверкой не подтверждён)",
        industries="не подтверждено этой проверкой (в наборе NACE-разбивка)",
        years="не подтверждено этой проверкой (в API-ответе набор отдаётся по годам)",
        units="EUR млн (проверить в метаданных; конвертация в USD_mn обязательна)",
        licence="Eurostat re-use policy (открытые данные с атрибуцией) — проверить перед загрузкой",
        licence_verified=False,
        access="Eurostat dissemination API без ключа: dataset code NAIO_10_CP1700, "
               "GET .../statistics/1.0/data/NAIO_10_CP1700?format=JSON — подтверждено",
        status="AVAILABLE_API",
        url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/NAIO_10_CP1700?format=JSON&lang=EN",
        checked_at="2026-11-14",
        check="HTTP 200, 134 877 537 байт JSON (набор NAIO_10_CP1700 отдаётся без ключа); "
              "страницы /web/figaro и /web/input-output-tables/database отдали 404",
        notes="единственный источник, который в этой проверке отдал данные без ключа и без "
              "ручной формы; валюта EUR — нужен FX-слой",
    ),
    SourceEntry(
        source_id="un_comtrade",
        name="UN Comtrade (товарная торговля по HS, не отраслевая таблица)",
        provider="United Nations Statistics Division",
        countries="все страны-репортёры (двусторонние потоки товаров)",
        industries="нет отраслей MRIO: товарные коды HS (нужен мост HS→отрасль)",
        years="в preview-проверке отдан 2022 год (USA, cmdCode=TOTAL)",
        units="USD (преобразование в USD_mn обязательно)",
        licence="UN Comtrade terms of use — проверить перед загрузкой",
        licence_verified=False,
        access="public preview API без ключа: "
               "GET https://comtradeapi.un.org/public/v1/preview/C/A/HS?... — подтверждено "
               "(count=224); полный/массовый доступ обычно требует ключа подписки — условия "
               "проверить на сайте",
        status="AVAILABLE_API",
        url="https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=842&period=2022&cmdCode=TOTAL&flowCode=M",
        checked_at="2026-11-14",
        check="HTTP 200 GET preview без ключа; ответ JSON с данными (elapsedTime, count, data)",
        notes="не MRIO-источник: используется только как слои торговли/весов, требует моста "
              "HS→отрасль и отдельной валидации",
    ),
    SourceEntry(
        source_id="bea_io",
        name="US Bureau of Economic Analysis input-output / GDP-by-industry",
        provider="BEA (US Department of Commerce)",
        countries="США (одна страна; для MRIO — только национальный блок)",
        industries="отрасли BEA (детализация в наборе; этой проверкой не подтверждена)",
        years="не подтверждено этой проверкой",
        units="USD (преобразование в USD_mn обязательно)",
        licence="US federal government data (public domain); условия API — на сайте BEA",
        licence_verified=False,
        access="API требует зарегистрированный UserID: запрос с UserID=DEMO отдал 200 с ошибкой "
               "APIErrorCode=4 'This UserId is not active'",
        status="BLOCKED_KEY",
        url="https://apps.bea.gov/api/data/",
        checked_at="2026-11-14",
        check="HTTP 200, тело — ошибка BEA: 'This UserId is not active. Please activate it'; "
              "значит нужен действующий ключ",
        notes="национальный блок США; не даёт 10+ стран",
    ),
    SourceEntry(
        source_id="eora_mrio",
        name="Eora Global Multi-Region Input-Output Database",
        provider="Eora / University of Sydney (N. Pinto)",
        countries="190 — подтверждено страницей",
        industries="15 909 секторов (aggregate) — подтверждено страницей (заявляемый охват)",
        years="1990-2022 — подтверждено страницей",
        units="USD (проверить в документации; преобразование в USD_mn обязательно)",
        licence="бесплатная академическая лицензия для degree-granting учреждений; "
                "коммерческое использование — покупка лицензии (подтверждено страницей)",
        licence_verified=True,
        access="скачивание после регистрации академической почты; API нет",
        status="BLOCKED_LICENCE",
        url="https://www.worldmrio.com/",
        checked_at="2026-11-14",
        check="HTTP 200 GET; страница содержит 190 стран, 15 909 секторов, 1990-2022 и условия "
              "лицензии/регистрации",
        notes="максимальный охват, но вход по регистрации; лицензию фиксировать письменно",
    ),
    SourceEntry(
        source_id="github_api",
        name="GitHub API (транспорт/зеркало артефактов, не источник данных)",
        provider="GitHub",
        countries="н/д",
        industries="н/д",
        years="н/д",
        units="н/д",
        licence="зависит от репозитория-зеркала",
        licence_verified=False,
        access="https://api.github.com без ключа (лимит 60 запросов/час на IP)",
        status="AVAILABLE",
        url="https://api.github.com/rate_limit",
        checked_at="2026-11-14",
        check="HTTP 200, rate_limit JSON (core limit 60, remaining 58)",
        notes="используется только как канал доставки/хеширования артефактов в CI; сырые третьи "
              "данные в git не попадают никогда",
    ),
)

SOURCE_BY_ID = {entry.source_id: entry for entry in SOURCE_MATRIX}


def source_matrix_document() -> list[dict]:
    """The source matrix as JSON-ready dictionaries (deterministic order)."""
    return [entry.as_dict() for entry in SOURCE_MATRIX]


# --------------------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------------------
def _floats(values: Iterable[Any], name: str) -> tuple[float, ...]:
    out = []
    for index, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"{name}[{index}] is not a number: {value!r}") from exc
        if not math.isfinite(number):
            raise SchemaError(f"{name}[{index}] must be finite, got {number!r}")
        out.append(number)
    return tuple(out)


@dataclass(frozen=True)
class MRIODataset:
    """One MRIO snapshot: countries x industries, one year, one value base (``USD_mn``).

    ``Z`` is the inter-industry/inter-country use matrix (``n x n``), ``y`` final demand by
    ``(country, industry)`` row, ``x`` gross output, ``va`` value added by column.  Optional
    ``trade_weights`` (bilateral country weights), ``fx`` (national currency per USD) and
    ``prices`` (deflators/indexes) are carried as metadata; they are never used to change the
    accounting identities.
    """

    countries: tuple[str, ...]
    industries: tuple[str, ...]
    years: tuple[int, ...]
    Z: tuple[tuple[float, ...], ...]
    y: tuple[float, ...]
    x: tuple[float, ...]
    va: tuple[float, ...]
    units: str = CANONICAL_UNITS
    source: dict = field(default_factory=dict)
    trade_weights: dict = field(default_factory=dict)
    fx: dict = field(default_factory=dict)
    prices: dict = field(default_factory=dict)
    tolerance: float = TOLERANCE

    # -- normalisation / structural checks ---------------------------------------------
    def __post_init__(self) -> None:
        countries = tuple(str(item).strip() for item in self.countries)
        industries = tuple(str(item).strip() for item in self.industries)
        if not countries:
            raise SchemaError("countries must not be empty")
        if not industries:
            raise SchemaError("industries must not be empty")
        if any(not label for label in countries + industries):
            raise SchemaError("country/industry labels must be non-empty")
        if len(set(countries)) != len(countries):
            raise SchemaError("countries must be unique")
        if len(set(industries)) != len(industries):
            raise SchemaError("industries must be unique")
        try:
            years = tuple(int(year) for year in self.years)
        except (TypeError, ValueError) as exc:
            raise SchemaError("years must be integers") from exc
        if not years:
            raise SchemaError("years must not be empty")
        if len(set(years)) != len(years) or list(years) != sorted(years):
            raise SchemaError("years must be unique and ascending")
        units = str(self.units)
        if units not in ALLOWED_UNITS:
            raise SchemaError(
                f"unsupported units {units!r}: the canonical value base is {CANONICAL_UNITS}"
            )
        if self.tolerance <= 0:
            raise SchemaError("tolerance must be positive")
        size = len(countries) * len(industries)

        rows = tuple(tuple(row) for row in self.Z)
        if len(rows) != size:
            raise SchemaError(f"Z must have {size} rows (countries x industries), got {len(rows)}")
        cleaned_rows = []
        for index, row in enumerate(rows):
            if len(row) != size:
                raise SchemaError(f"Z row {index} must have {size} entries, got {len(row)}")
            cleaned_rows.append(_floats(row, f"Z[{index}]"))
        for name, vector in (("y", self.y), ("x", self.x), ("va", self.va)):
            if len(vector) != size:
                raise SchemaError(f"{name} must have {size} entries, got {len(vector)}")
        y = _floats(self.y, "y")
        x = _floats(self.x, "x")
        va = _floats(self.va, "va")

        limit = -float(self.tolerance)
        for index, row in enumerate(cleaned_rows):
            for column, value in enumerate(row):
                if value < limit:
                    raise SchemaError(f"Z[{index}][{column}] is negative ({value}); Z >= 0 required")
        for name, vector in (("y", y), ("x", x), ("va", va)):
            for index, value in enumerate(vector):
                if value < limit:
                    raise SchemaError(f"{name}[{index}] is negative ({value}); >= 0 required")
        if not isinstance(self.source, Mapping):
            raise SchemaError("source must be a mapping")
        for name, mapping in (("trade_weights", self.trade_weights), ("fx", self.fx),
                              ("prices", self.prices)):
            if not isinstance(mapping, Mapping):
                raise SchemaError(f"{name} must be a mapping")

        object.__setattr__(self, "countries", countries)
        object.__setattr__(self, "industries", industries)
        object.__setattr__(self, "years", years)
        object.__setattr__(self, "Z", tuple(cleaned_rows))
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "va", va)
        object.__setattr__(self, "units", str(units))
        object.__setattr__(self, "source", dict(self.source))
        object.__setattr__(self, "trade_weights", dict(self.trade_weights))
        object.__setattr__(self, "fx", dict(self.fx))
        object.__setattr__(self, "prices", dict(self.prices))
        object.__setattr__(self, "tolerance", float(self.tolerance))

    # -- accessors --------------------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.countries) * len(self.industries)

    @property
    def labels(self) -> tuple[str, ...]:
        """Row/column labels in the canonical order ``country|industry``."""
        return tuple(f"{country}|{industry}"
                     for country in self.countries for industry in self.industries)

    def country_of(self, index: int) -> str:
        return self.countries[index // len(self.industries)]

    def industry_of(self, index: int) -> str:
        return self.industries[index % len(self.industries)]

    def index_of(self, country: str, industry: str) -> int:
        if country not in self.countries or industry not in self.industries:
            raise SchemaError(f"unknown (country, industry) pair: ({country!r}, {industry!r})")
        return self.countries.index(country) * len(self.industries) + self.industries.index(industry)

    def country_index(self, country: str) -> range:
        if country not in self.countries:
            raise SchemaError(f"unknown country {country!r}")
        start = self.countries.index(country) * len(self.industries)
        return range(start, start + len(self.industries))

    # -- canonical serialisation -------------------------------------------------------
    def to_document(self) -> dict:
        """The canonical JSON document (plain types, deterministic key order on write)."""
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "countries": list(self.countries),
            "industries": list(self.industries),
            "years": list(self.years),
            "units": self.units,
            "Z": [list(row) for row in self.Z],
            "y": list(self.y),
            "x": list(self.x),
            "va": list(self.va),
            "source": dict(self.source),
            "trade_weights": dict(self.trade_weights),
            "fx": dict(self.fx),
            "prices": dict(self.prices),
            "notes": "canonical AMFE MRIO schema (docs/121); values are third-party data and are "
                     "never committed to the repository",
        }

    def digest(self) -> str:
        """SHA-256 of the canonical document (floats rounded to 9 decimals for portability)."""
        return hashlib.sha256(canonical_bytes(self.to_document())).hexdigest()

    def describe(self) -> dict:
        """Metadata-only summary safe to print/log (no observation values)."""
        return {
            "schema": SCHEMA,
            "countries": len(self.countries),
            "industries": len(self.industries),
            "years": list(self.years),
            "units": self.units,
            "cells_Z": self.n * self.n,
            "digest": self.digest(),
            "source": self.source.get("name") if isinstance(self.source, Mapping) else None,
        }


def _round(value: float, digits: int = 9) -> float:
    return float(f"{value:.{digits}f}") if math.isfinite(value) else value


def _canonicalise(node: Any) -> Any:
    if isinstance(node, float):
        return _round(node)
    if isinstance(node, Mapping):
        return {str(key): _canonicalise(node[key]) for key in sorted(node)}
    if isinstance(node, (list, tuple)):
        return [_canonicalise(item) for item in node]
    return node


def canonical_bytes(document: Mapping) -> bytes:
    """Deterministic UTF-8 JSON bytes for hashing (sorted keys, no whitespace, 9 decimals)."""
    payload = _canonicalise(document)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def dataset_from_document(document: Mapping) -> MRIODataset:
    """Build a dataset from a JSON document; every schema violation raises :class:`SchemaError`."""
    if not isinstance(document, Mapping):
        raise SchemaError("dataset document must be a JSON object")
    schema = document.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise SchemaError(f"unsupported schema {schema!r}: expected {SCHEMA!r}")
    required = ("countries", "industries", "years", "Z", "y", "x", "va")
    missing = [key for key in required if key not in document]
    if missing:
        raise SchemaError(f"dataset document misses required fields: {missing}")
    try:
        return MRIODataset(
            countries=tuple(document["countries"]),
            industries=tuple(document["industries"]),
            years=tuple(document["years"]),
            Z=tuple(tuple(row) for row in document["Z"]),
            y=tuple(document["y"]),
            x=tuple(document["x"]),
            va=tuple(document["va"]),
            units=str(document.get("units", CANONICAL_UNITS)),
            source=document.get("source") or {},
            trade_weights=document.get("trade_weights") or {},
            fx=document.get("fx") or {},
            prices=document.get("prices") or {},
            tolerance=float(document.get("tolerance", TOLERANCE)),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, MRIOError):
            raise
        raise SchemaError(f"malformed dataset document: {exc}") from exc


def dataset_to_bytes(dataset: MRIODataset) -> bytes:
    """Deterministic UTF-8 JSON file bytes (readable, sorted keys, trailing newline)."""
    payload = _canonicalise(dataset.to_document())
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    return text.encode("utf-8")


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ValidationReport:
    """Result of :func:`validate_dataset`: named checks, metrics and human-readable errors."""

    ok: bool
    checks: dict
    metrics: dict
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "metrics": dict(self.metrics),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def check_p2_scale(dataset: MRIODataset,
                   min_countries: int = MIN_COUNTRIES,
                   min_industries: int = MIN_INDUSTRIES) -> tuple[bool, str]:
    """P2 gate: at least ``min_countries`` countries and ``min_industries`` industries."""
    if len(dataset.countries) < min_countries:
        return False, (f"P2 scale gate: {len(dataset.countries)} countries < {min_countries} "
                       f"(the world model needs {min_countries}+ x {min_industries}+)")
    if len(dataset.industries) < min_industries:
        return False, (f"P2 scale gate: {len(dataset.industries)} industries < {min_industries} "
                       f"(the world model needs {min_countries}+ x {min_industries}+)")
    return True, ""


def validate_dataset(dataset: MRIODataset, tolerance: float | None = None,
                     enforce_p2_scale: bool = True,
                     min_countries: int = MIN_COUNTRIES,
                     min_industries: int = MIN_INDUSTRIES) -> ValidationReport:
    """Check the accounting identities and (optionally) the P2 scale gate.

    Row balance ``x = Z 1 + y`` and column balance ``x = 1' Z + va`` are checked with a scaled
    tolerance: ``|residual_i| <= tol * max(1, |x_i|)``.  Country totals are reported for x, y and
    va so a caller can reconcile them against published aggregates.
    """
    tol = float(dataset.tolerance if tolerance is None else tolerance)
    if tol <= 0:
        raise SchemaError("tolerance must be positive")
    errors: list[str] = []
    warnings: list[str] = []

    size = dataset.n
    row_residual = []
    column_residual = []
    for row in range(size):
        intermediate = math.fsum(dataset.Z[row])
        residual = intermediate + dataset.y[row] - dataset.x[row]
        row_residual.append(residual)
        scale = max(1.0, abs(dataset.x[row]))
        if abs(residual) > tol * scale:
            errors.append(f"row balance violated at {dataset.labels[row]}: "
                          f"sum(Z)+y-x = {residual:.6e} (|.| > {tol:.1e}*{scale:.3g})")
    for column in range(size):
        intermediate = math.fsum(dataset.Z[row][column] for row in range(size))
        residual = intermediate + dataset.va[column] - dataset.x[column]
        column_residual.append(residual)
        scale = max(1.0, abs(dataset.x[column]))
        if abs(residual) > tol * scale:
            errors.append(f"column balance violated at {dataset.labels[column]}: "
                          f"sum(Z)+va-x = {residual:.6e} (|.| > {tol:.1e}*{scale:.3g})")

    total_va = math.fsum(dataset.va)
    total_final = math.fsum(dataset.y)
    total_output = math.fsum(dataset.x)
    total_intermediate = math.fsum(math.fsum(row) for row in dataset.Z)
    va_scale = max(1.0, abs(total_va), abs(total_final))
    global_ok = abs(total_va - total_final) <= tol * va_scale
    if not global_ok:
        errors.append(f"global identity violated: sum(va) = {total_va:.6f} != sum(y) = "
                      f"{total_final:.6f} (difference {total_va - total_final:.6e})")
    aggregate_residual = total_output - (total_intermediate + total_final)
    aggregate_scale = max(1.0, abs(total_output))
    aggregate_ok = abs(aggregate_residual) <= tol * aggregate_scale
    if not aggregate_ok:
        errors.append(f"aggregate output identity violated: sum(x) = {total_output:.6f} != "
                      f"sum(Z)+sum(y) = {total_intermediate + total_final:.6f} "
                      f"(difference {aggregate_residual:.6e})")

    countries = dataset.countries
    x_by_country = {country: math.fsum(dataset.x[i] for i in dataset.country_index(country))
                    for country in countries}
    y_by_country = {country: math.fsum(dataset.y[i] for i in dataset.country_index(country))
                    for country in countries}
    va_by_country = {country: math.fsum(dataset.va[i] for i in dataset.country_index(country))
                     for country in countries}
    # cross-border intermediate flows: exports leave a country block, imports enter it
    exports_by_country = {}
    imports_by_country = {}
    for country in countries:
        block = set(dataset.country_index(country))
        exports = math.fsum(dataset.Z[row][column] for row in block
                            for column in range(size) if column not in block)
        imports = math.fsum(dataset.Z[row][column] for row in range(size)
                            for column in block if row not in block)
        exports_by_country[country] = round(exports, 6)
        imports_by_country[country] = round(imports, 6)
    net_exports_by_country = {
        country: round(exports_by_country[country] - imports_by_country[country], 6)
        for country in countries
    }
    net_exports_sum = round(math.fsum(net_exports_by_country.values()), 6)

    min_Z = min((value for row in dataset.Z for value in row), default=0.0)
    min_y = min(dataset.y, default=0.0)

    scale_ok, scale_error = check_p2_scale(dataset, min_countries, min_industries)
    if not scale_ok:
        if enforce_p2_scale:
            errors.append(scale_error)
        else:
            warnings.append(scale_error)

    checks = {
        "schema": True,
        "dimensions": True,
        "labels_unique": True,
        "non_negative": min_Z >= -tol and min_y >= -tol,
        "row_balance": not any("row balance" in error for error in errors),
        "column_balance": not any("column balance" in error for error in errors),
        "global_value_added_equals_final_demand": global_ok,
        "aggregate_output_identity": aggregate_ok,
        "p2_scale": scale_ok,
    }
    metrics = {
        "countries": len(dataset.countries),
        "industries": len(dataset.industries),
        "n": size,
        "years": list(dataset.years),
        "units": dataset.units,
        "max_abs_row_residual": round(max((abs(value) for value in row_residual), default=0.0), 9),
        "max_abs_column_residual": round(max((abs(value) for value in column_residual),
                                             default=0.0), 9),
        "max_rel_row_residual": round(max(
            (abs(residual) / max(1.0, abs(dataset.x[row]))
             for row, residual in enumerate(row_residual)), default=0.0), 9),
        "total_output": round(total_output, 6),
        "total_intermediate": round(total_intermediate, 6),
        "total_final_demand": round(total_final, 6),
        "total_value_added": round(total_va, 6),
        "global_difference_va_minus_y": round(total_va - total_final, 9),
        "global_difference_output": round(aggregate_residual, 9),
        "x_by_country": {country: round(value, 6) for country, value in x_by_country.items()},
        "y_by_country": {country: round(value, 6) for country, value in y_by_country.items()},
        "va_by_country": {country: round(value, 6) for country, value in va_by_country.items()},
        "exports_by_country": exports_by_country,
        "imports_by_country": imports_by_country,
        "net_exports_by_country": net_exports_by_country,
        "net_exports_sum": net_exports_sum,
        "tolerance": tol,
        "digest": dataset.digest(),
    }
    return ValidationReport(ok=not errors, checks=checks, metrics=metrics,
                            errors=tuple(errors), warnings=tuple(warnings))


def require_valid(dataset: MRIODataset, enforce_p2_scale: bool = True) -> ValidationReport:
    """Validate and raise :class:`ValidationError` unless every check passes."""
    report = validate_dataset(dataset, enforce_p2_scale=enforce_p2_scale)
    if not report.ok:
        raise ValidationError("; ".join(report.errors[:5]) or "dataset validation failed")
    return report
