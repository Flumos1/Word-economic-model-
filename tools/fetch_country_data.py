#!/usr/bin/env python3
"""Fetch real quarterly country series for the EA/CN/GB forecast layer (task-60).

The tool retrieves **official aggregates only** (no member-state aggregation), from
public endpoints that need no API key:

* ``EA`` GDP growth  - Eurostat REST, ``namq_10_gdp`` (EA20, B1GQ, CLV_PCH_SM, SCA);
* ``EA`` CPI         - Eurostat REST, ``prc_hicp_midx`` monthly index (EA20, CP00, I15)
                       averaged to quarters, then year-on-year percent;
* ``CN`` GDP growth  - OECD quarterly national accounts (G20 growth table) via DBnomics;
* ``CN`` CPI         - OECD prices (CPI, total, percent per annum, y/y) via DBnomics;
* ``GB`` GDP growth  - ONS ``QNA`` series ``IHYR.Q`` via DBnomics (y/y, CVM SA);
* ``GB`` CPI         - ONS ``MM23`` series ``D7G7.Q`` via DBnomics (CPI annual rate).

What is written
---------------
One JSON file per country/target/frequency in a **git-ignored** cache directory
(``data/raw/country/<CC>_<TARGET>_Q.json`` by default).  Each file contains the
normalised quarterly series (period, value), the exact query URL, the retrieval
timestamp, the licence string, the transform applied and a PIT declaration.
No raw response bodies are stored, no release dates are invented: every series is
declared ``FINAL_VINTAGE_NO_PIT`` because the public endpoints return the *latest*
vintage only.  If a provider ever exposes per-observation release dates, the cache
may carry ``release_dates`` and the adapter will treat it as ``REAL_PIT``.

The cache is read by :mod:`amfe.forecast.country` (provider ``COUNTRY_CACHE``).
Nothing here is committed: ``/data/raw/`` is in ``.gitignore``.

Usage
-----
    python tools/fetch_country_data.py                 # fetch all six series
    python tools/fetch_country_data.py --only EA:GDP   # one series
    python tools/fetch_country_data.py --dry-run       # fetch, print, do not write
    python tools/fetch_country_data.py --list          # show the catalogue

Exit code 0 if every requested series was fetched, 2 otherwise (each failure is
reported as ``NOT_AVAILABLE`` with the exact error; nothing is imputed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "data" / "raw" / "country"
SCHEMA = "amfe.country_cache.v1"
USER_AGENT = "AMFE-country-forecast/1.0 (research; local project)"

EUROSTAT_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
DBNOMICS_BASE = "https://api.db.nomics.world/v22/series"

LICENCES = {
    "EUROSTAT": "Eurostat open data (Commission Decision 2011/833/EU); attribution required",
    "OECD": "OECD data via DBnomics; attribution required; terms not re-audited in this task",
    "ONS": "Open Government Licence v3.0 (ONS); attribution required",
}

PIT_FINAL = "FINAL_VINTAGE_NO_PIT"


class FetchError(RuntimeError):
    """A provider request or payload was unusable."""


@dataclass(frozen=True)
class SeriesSpec:
    """One catalogue entry: what to fetch, where from and how it was transformed."""

    country: str
    target: str
    provider: str
    series_id: str
    title: str
    unit: str
    transform: str
    url: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def _eurostat_gdp_url() -> str:
    query = urllib.parse.urlencode(
        {
            "format": "JSON",
            "lang": "EN",
            "geo": "EA20",
            "na_item": "B1GQ",
            "unit": "CLV_PCH_SM",
            "s_adj": "SCA",
            "sinceTimePeriod": "1995-Q1",
        }
    )
    return f"{EUROSTAT_BASE}/namq_10_gdp?{query}"


def _eurostat_hicp_url() -> str:
    query = urllib.parse.urlencode(
        {
            "format": "JSON",
            "lang": "EN",
            "geo": "EA20",
            "coicop": "CP00",
            "unit": "I15",
            "sinceTimePeriod": "1996-01",
        }
    )
    return f"{EUROSTAT_BASE}/prc_hicp_midx?{query}"


def _dbnomics_url(provider: str, dataset: str, series_code: str) -> str:
    return f"{DBNOMICS_BASE}/{provider}/{dataset}/{series_code}?observations=1"


CATALOGUE: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        country="EA",
        target="GDP",
        provider="EUROSTAT",
        series_id="namq_10_gdp|EA20|B1GQ|CLV_PCH_SM|SCA",
        title="Euro area 20: GDP at market prices, chain-linked volumes, % change vs same quarter previous year",
        unit="percent, y/y",
        transform="as published (CLV_PCH_SM is already a y/y growth rate)",
        url=_eurostat_gdp_url(),
        notes=("official euro-area aggregate (EA20); member states are NOT aggregated here",),
    ),
    SeriesSpec(
        country="EA",
        target="CPI",
        provider="EUROSTAT",
        series_id="prc_hicp_midx|EA20|CP00|I15",
        title="Euro area 20: HICP all items, monthly index (2015=100), averaged to quarters, % y/y",
        unit="percent, y/y",
        transform="monthly index -> arithmetic mean inside each quarter -> year-on-year percent change",
        url=_eurostat_hicp_url(),
        notes=("HICP is published monthly; the quarterly value is the y/y change of the quarterly mean index",),
    ),
    SeriesSpec(
        country="CN",
        target="GDP",
        provider="OECD",
        series_id="OECD/DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_G20/Q.Y.CHN.S1.S1.B1GQ._Z._Z._Z.PC.L.GY.T0102",
        title="China: quarterly real GDP growth, percent, y/y (OECD QNA G20 growth table)",
        unit="percent, y/y",
        transform="as published (GY transformation = growth rate, y/y)",
        url=_dbnomics_url(
            "OECD",
            "DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_G20",
            "Q.Y.CHN.S1.S1.B1GQ._Z._Z._Z.PC.L.GY.T0102",
        ),
        notes=("OECD reproduces the national source; OECD is the channel, NBS is the origin",),
    ),
    SeriesSpec(
        country="CN",
        target="CPI",
        provider="OECD",
        series_id="OECD/DSD_PRICES@DF_PRICES_ALL/CHN.Q.N.CPI.PA._T.N.GY",
        title="China: consumer price index, total, percent per annum (y/y), quarterly",
        unit="percent, y/y",
        transform="as published (GY transformation = growth rate, y/y)",
        url=_dbnomics_url("OECD", "DSD_PRICES@DF_PRICES_ALL", "CHN.Q.N.CPI.PA._T.N.GY"),
        notes=("quarterly y/y already provided by the OECD prices dataset",),
    ),
    SeriesSpec(
        country="GB",
        target="GDP",
        provider="ONS",
        series_id="ONS/QNA/IHYR.Q",
        title="United Kingdom: GDP q-on-q4 (y/y) growth rate, CVM SA, percent",
        unit="percent, y/y",
        transform="as published (ONS IHYR, quarter on same quarter a year earlier)",
        url=_dbnomics_url("ONS", "QNA", "IHYR.Q"),
        notes=("ONS QNA is the official UK source; DBnomics is only the transport",),
    ),
    SeriesSpec(
        country="GB",
        target="CPI",
        provider="ONS",
        series_id="ONS/MM23/D7G7.Q",
        title="United Kingdom: CPI all items, annual rate (y/y), percent, quarterly",
        unit="percent, y/y",
        transform="as published (ONS D7G7, quarterly)",
        url=_dbnomics_url("ONS", "MM23", "D7G7.Q"),
        notes=("ONS D7G7 is the CPI annual rate; quarterly aggregation is the ONS quarterly series",),
    ),
)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
def http_json(url: str, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        raise FetchError(f"network error for {url}: {exc.reason}") from exc
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise FetchError(f"non-JSON payload from {url}") from exc


# --------------------------------------------------------------------------------------
# Period helpers (duplicated deliberately: tools must not import the model package)
# --------------------------------------------------------------------------------------
def quarter_index(year: int, quarter: int) -> int:
    return int(year) * 4 + (int(quarter) - 1)


def quarter_label(index: int) -> str:
    year, quarter = divmod(int(index), 4)
    return f"{year}Q{quarter + 1}"


def parse_quarter_label(text: str) -> int:
    raw = str(text).strip()
    for separator in ("-", " ", "_", "/"):
        raw = raw.replace(separator, "")
    if len(raw) == 6 and raw[4] in "Qq" and raw[5] in "1234":
        return quarter_index(int(raw[:4]), int(raw[5]))
    raise FetchError(f"cannot parse quarterly label {text!r}")


def parse_month_label(text: str) -> tuple[int, int]:
    raw = str(text).strip()
    if len(raw) == 7 and raw[4] == "-":
        year, month = int(raw[:4]), int(raw[5:])
        if 1 <= month <= 12:
            return year, month
    raise FetchError(f"cannot parse monthly label {text!r}")


def quarter_of_month(year: int, month: int) -> int:
    return quarter_index(year, (month - 1) // 3 + 1)


def aggregate_monthly_to_quarterly(periods: Sequence[str], values: Sequence[float]) -> tuple[list[str], list[float]]:
    """Average monthly observations inside each quarter (requires all three months)."""
    if len(periods) != len(values):
        raise FetchError("period/value length mismatch")
    buckets: dict[int, list[float]] = {}
    for period, value in zip(periods, values):
        year, month = parse_month_label(period)
        buckets.setdefault(quarter_of_month(year, month), []).append(float(value))
    out_periods: list[str] = []
    out_values: list[float] = []
    for index in sorted(buckets):
        bucket = buckets[index]
        if len(bucket) < 3:
            continue  # incomplete quarter: not a quarter observation
        out_periods.append(quarter_label(index))
        out_values.append(float(sum(bucket) / len(bucket)))
    return out_periods, out_values


def yoy_from_quarterly_index(periods: Sequence[str], values: Sequence[float]) -> tuple[list[str], list[float]]:
    """Year-on-year percent change of a quarterly index (requires t-4)."""
    if len(periods) != len(values):
        raise FetchError("period/value length mismatch")
    index = {parse_quarter_label(period): float(value) for period, value in zip(periods, values)}
    out_periods: list[str] = []
    out_values: list[float] = []
    for period in sorted(index):
        base = index.get(period - 4)
        if base is None or base <= 0:
            continue
        out_periods.append(quarter_label(period))
        out_values.append((index[period] / base - 1.0) * 100.0)
    return out_periods, out_values


# --------------------------------------------------------------------------------------
# Provider payload parsers
# --------------------------------------------------------------------------------------
def parse_eurostat_jsonstat(document: dict) -> tuple[list[str], list[float]]:
    """Read a single-series JSON-stat 2.0 response as (period label, value) pairs."""
    dimension = document.get("dimension") or {}
    time_dimension = dimension.get("time") or {}
    category = time_dimension.get("category") or {}
    positions: dict[int, str] = {int(pos): str(label) for label, pos in (category.get("index") or {}).items()}
    if not positions:
        raise FetchError("Eurostat payload has no time dimension")
    raw_values = document.get("value")
    if isinstance(raw_values, list):
        values = {index: raw_values[index] for index in range(len(raw_values))}
    elif isinstance(raw_values, dict):
        values = {int(key): value for key, value in raw_values.items()}
    else:
        raise FetchError("Eurostat payload has no values")
    periods: list[str] = []
    numbers: list[float] = []
    for position in sorted(positions):
        value = values.get(position)
        if value is None:
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        periods.append(positions[position])
        numbers.append(number)
    if not periods:
        raise FetchError("Eurostat payload has no usable observations")
    return periods, numbers


def parse_dbnomics_single_series(document: dict) -> tuple[list[str], list[float], dict]:
    """Read the single-series DBnomics response as (periods, values, metadata)."""
    docs = ((document.get("series") or {}).get("docs")) or []
    if len(docs) != 1:
        raise FetchError(f"DBnomics returned {len(docs)} series, expected exactly 1")
    doc = docs[0]
    periods = [str(item) for item in (doc.get("period") or [])]
    raw_values = doc.get("value") or []
    if len(periods) != len(raw_values):
        raise FetchError("DBnomics period/value length mismatch")
    out_periods: list[str] = []
    out_values: list[float] = []
    for period, value in zip(periods, raw_values):
        if value is None or (isinstance(value, str) and value.strip().upper() in ("NA", "")):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        out_periods.append(period)
        out_values.append(number)
    if not out_periods:
        raise FetchError("DBnomics payload has no usable observations")
    meta = {
        "dataset_name": doc.get("dataset_name"),
        "series_name": doc.get("series_name"),
        "provider_code": doc.get("provider_code"),
        "dataset_code": doc.get("dataset_code"),
        "series_code": doc.get("series_code"),
    }
    return out_periods, out_values, meta


# --------------------------------------------------------------------------------------
# Transform pipeline
# --------------------------------------------------------------------------------------
def normalise(spec: SeriesSpec, raw_periods: Sequence[str], raw_values: Sequence[float]) -> tuple[list[str], list[float]]:
    """Apply the catalogue transform and return canonical ``YYYYQn`` periods."""
    if spec.country == "EA" and spec.target == "CPI":
        quarterly_periods, quarterly_values = aggregate_monthly_to_quarterly(raw_periods, raw_values)
        periods, values = yoy_from_quarterly_index(quarterly_periods, quarterly_values)
    else:
        periods = [quarter_label(parse_quarter_label(item)) for item in raw_periods]
        values = [float(item) for item in raw_values]
    order = sorted(range(len(periods)), key=lambda index: parse_quarter_label(periods[index]))
    periods = [periods[index] for index in order]
    values = [values[index] for index in order]
    seen: set[str] = set()
    for period in periods:
        if period in seen:
            raise FetchError(f"{spec.country}/{spec.target}: duplicate period {period}")
        seen.add(period)
    if len(periods) < 24:
        raise FetchError(f"{spec.country}/{spec.target}: only {len(periods)} observations")
    return periods, values


def build_cache_payload(spec: SeriesSpec, periods: Sequence[str], values: Sequence[float],
                        retrieved_at: str) -> dict:
    body = {
        "schema": SCHEMA,
        "provider": spec.provider,
        "source_id": spec.provider,
        "country": spec.country,
        "target": spec.target,
        "frequency": "Q",
        "series_id": spec.series_id,
        "title": spec.title,
        "unit": spec.unit,
        "transform": spec.transform,
        "source_url": spec.url,
        "retrieved_at": retrieved_at,
        "licence": LICENCES[spec.provider],
        "pit": {
            "status": PIT_FINAL,
            "reason": (
                "public endpoint returns the latest vintage only; no per-observation release "
                "dates or vintage archive are exposed by this channel"
            ),
        },
        "release_dates": None,
        "notes": list(spec.notes),
        "periods": list(periods),
        "values": [round(float(value), 6) for value in values],
    }
    digest_source = json.dumps(
        {"periods": body["periods"], "values": body["values"]},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    body["payload_sha256"] = hashlib.sha256(digest_source).hexdigest()
    return body


def fetch_one(spec: SeriesSpec, timeout: float = 60.0) -> dict:
    document = http_json(spec.url, timeout=timeout)
    if spec.provider == "EUROSTAT":
        raw_periods, raw_values = parse_eurostat_jsonstat(document)
        meta = {"dataset_name": spec.series_id}
    else:
        raw_periods, raw_values, meta = parse_dbnomics_single_series(document)
    periods, values = normalise(spec, raw_periods, raw_values)
    payload = build_cache_payload(spec, periods, values, date.today().isoformat())
    payload["provider_metadata"] = {key: value for key, value in meta.items() if value}
    payload["raw_observations"] = len(raw_periods)
    return payload


def cache_path(root: Path, spec: SeriesSpec) -> Path:
    return Path(root) / f"{spec.country}_{spec.target}_Q.json"


def write_cache(payload: dict, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.write_bytes(text.encode("utf-8"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_specs(only: Iterable[str] | None) -> list[SeriesSpec]:
    if not only:
        return list(CATALOGUE)
    wanted = {item.strip().upper() for item in only if item and item.strip()}
    known = {f"{spec.country}:{spec.target}" for spec in CATALOGUE}
    unknown = wanted - known
    if unknown:
        raise SystemExit(f"unknown selector(s) {sorted(unknown)}; known: {sorted(known)}")
    return [spec for spec in CATALOGUE if f"{spec.country}:{spec.target}" in wanted]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch real quarterly EA/CN/GB series into a git-ignored cache")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="cache directory (default data/raw/country)")
    parser.add_argument("--only", action="append", default=None,
                        help="restrict to CC:TARGET (repeatable), e.g. --only EA:GDP")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, do not write files")
    parser.add_argument("--list", action="store_true", help="print the catalogue and exit")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout, seconds")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    specs = select_specs(args.only)
    if args.list:
        for spec in specs:
            print(f"{spec.country}:{spec.target}\t{spec.provider}\t{spec.series_id}\t{spec.url}")
        return 0
    root = Path(args.root)
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary: list[dict] = []
    failures = 0
    for spec in specs:
        entry = {"country": spec.country, "target": spec.target, "provider": spec.provider}
        try:
            payload = fetch_one(spec, timeout=args.timeout)
        except (FetchError, ValueError) as exc:
            failures += 1
            entry.update({"status": "NOT_AVAILABLE", "error": str(exc), "written": None})
            summary.append(entry)
            continue
        entry.update(
            {
                "status": "OK",
                "n_observations": len(payload["periods"]),
                "first_period": payload["periods"][0],
                "last_period": payload["periods"][-1],
                "pit_status": payload["pit"]["status"],
                "payload_sha256": payload["payload_sha256"],
                "written": None,
            }
        )
        if not args.dry_run:
            digest = write_cache(payload, cache_path(root, spec))
            entry["written"] = str(cache_path(root, spec))
            entry["file_sha256"] = digest
        summary.append(entry)
    print(json.dumps({"retrieved_at": retrieved_at, "root": str(root), "series": summary},
                     ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
