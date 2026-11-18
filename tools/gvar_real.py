#!/usr/bin/env python3
"""GVAR on real cross-country data (World Bank API, no key): download, panel, weights, OOS.

What this tool does
-------------------
1. Downloads five World Bank WDI indicators for a fixed list of ISO3 countries, 1995-2024, with an
   on-disk cache under ``data/mrio/wb/`` (never committed).  World Bank WDI is used under its open
   terms (CC BY 4.0); the raw payload is cached verbatim with its source URL and retrieval time.
2. Builds a balanced annual panel of two variables per country: real GDP growth (annual %, WDI code
   ``NY.GDP.MKTP.KD.ZG``) and CPI inflation (annual %, ``FP.CPI.TOTL.ZG``).  Countries without a
   complete window are dropped; fewer than ``MIN_COUNTRIES`` complete countries is a hard failure.
3. Trade weights: the World Bank API is not bilateral.  The delivered default is a GDP-share proxy
   ``w_ij = GDP_j / sum_{k != i} GDP_k`` (documented approximation); if
   ``data/mrio/figaro/dataset.json`` (or another MRIO dataset passed with ``--mrio``) contains
   ``trade_weights``, those are read instead (read-only) and the source is recorded.
4. Fits the existing ``amfe.world.gvar.GVAR`` prototype (``src/amfe/world/gvar.py``, not modified) for
   p = 1 and p = 2, reports stability, builds an IRF to a US growth shock and runs a rolling
   out-of-sample comparison h = 1..4 against univariate AR(1) and a random walk on the aggregates
   US, EA, CN and the world (RMSE / MAE).
5. Writes the report to ``data/mrio/wb/gvar_report.json`` (derived numbers only, no raw WB payload)
   and prints a compact summary.

Fail-closed: a missing indicator, too few complete countries, too short a sample, a non-unit weight
row or a non-finite value raise :class:`GVARRealError` instead of silently shrinking the evaluation.

Usage:
    python tools/gvar_real.py                      # use the cache, download if absent
    python tools/gvar_real.py --refresh            # re-download all indicators
    python tools/gvar_real.py --lags 1,2 --max-h 4 --origins 15
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.world.gvar import GVAR, GVARError  # noqa: E402

MRIO_ROOT = Path(os.environ.get("AMFE_MRIO_ROOT") or (ROOT / "data" / "mrio"))
CACHE_DIR = MRIO_ROOT / "wb"
FIGARO_DATASET = MRIO_ROOT / "figaro" / "figaro_2019" / "dataset.json"
REPORT_PATH = CACHE_DIR / "gvar_report.json"
WEIGHTS_CACHE = MRIO_ROOT / "cache" / "gvar_real"
YBY_REPORT_PATH = WEIGHTS_CACHE / "gvar_v2_report.json"
WEIGHTS_MODES = ("auto", "figaro", "gdp", "yby")
SELECT_MODES = ("none", "bic", "aic")
WB_API = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
START_YEAR, END_YEAR = 1995, 2024
MIN_COUNTRIES = 40
MIN_YEARS = 15
MIN_TRAIN = 12

INDICATORS = {
    "gdp_current": "NY.GDP.MKTP.CD",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "inflation": "FP.CPI.TOTL.ZG",
    "trade_gdp": "NE.TRD.GNFS.ZS",
    "population": "SP.POP.TOTL",
}

# 55 economies; the complete-window selection keeps those with full WDI coverage (>= 40 expected).
COUNTRIES = (
    "USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN", "RUS", "KOR", "AUS",
    "ESP", "MEX", "IDN", "TUR", "NLD", "SAU", "CHE", "POL", "ARG", "SWE", "BEL", "THA", "IRN",
    "AUT", "NOR", "ARE", "ISR", "SGP", "MYS", "ZAF", "DNK", "PHL", "COL", "FIN", "CHL", "ROU",
    "CZE", "PRT", "NZL", "PER", "GRC", "HUN", "IRL", "SVK", "SVN", "LUX", "EST", "LVA", "LTU",
    "BGR", "HRV", "EGY",
)

EURO_AREA = ("DEU", "FRA", "ITA", "ESP", "NLD", "BEL", "AUT", "PRT", "IRL", "FIN", "GRC", "SVK",
             "SVN", "LUX", "EST", "LVA", "LTU", "HRV")


class GVARRealError(RuntimeError):
    """Missing data, too short a sample or an invalid dataset: the evaluation must not proceed."""


# ------------------------------------------------------------------------------------- download
def wb_url(indicator: str, countries, start: int = START_YEAR, end: int = END_YEAR) -> str:
    codes = ";".join(countries)
    return (WB_API.format(countries=codes, indicator=indicator)
            + f"?format=json&per_page=20000&date={start}:{end}")


def _download(url: str, *, timeout: float = 60.0, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "amfe-gvar-real/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list) or len(payload) != 2:
                raise GVARRealError("unexpected World Bank payload shape (expected [meta, rows])")
            return payload
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            time.sleep(min(2.0 * attempt, 6.0))
    raise GVARRealError(f"World Bank download failed after {retries} attempts: {url} ({last!r})")


def cache_path(indicator: str) -> Path:
    return CACHE_DIR / f"wb_{indicator}.json"


def fetch_indicator(indicator: str, countries, *, cache_dir: Path = CACHE_DIR, refresh: bool = False,
                    start: int = START_YEAR, end: int = END_YEAR, download=_download) -> dict:
    """Return ``{"source", "downloaded_at", "payload"}`` for one indicator, cached on disk."""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"wb_{indicator}.json"
    if path.is_file() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict) or "payload" not in cached:
            raise GVARRealError(f"corrupt cache file {path}")
        return cached
    url = wb_url(indicator, countries, start, end)
    payload = download(url)
    document = {"source": url, "indicator": indicator, "retrieved_at":
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "payload": payload}
    path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")
    return document


def parse_payload(payload, *, countries=None) -> dict:
    """Parse a WDI indicator payload into ``{iso3: {year: value|None}}``."""
    if not isinstance(payload, list) or len(payload) != 2:
        raise GVARRealError("payload must be the [meta, rows] pair of the World Bank API")
    rows = payload[1]
    if rows is None:
        raise GVARRealError("World Bank returned no rows for the requested indicator")
    allowed = None if countries is None else {str(code) for code in countries}
    out: dict = {}
    for row in rows:
        code = str(row.get("countryiso3code") or row.get("country", {}).get("id") or "").upper()
        if not code:
            continue
        if allowed is not None and code not in allowed:
            continue
        date = row.get("date")
        try:
            year = int(str(date)[:4])
        except (TypeError, ValueError):
            continue
        value = row.get("value")
        out.setdefault(code, {})
        out[code][year] = None if value is None else float(value)
    return out


def load_indicators(countries, *, cache_dir: Path = CACHE_DIR, refresh: bool = False,
                    start: int = START_YEAR, end: int = END_YEAR, download=_download) -> dict:
    """Download (or read cached) five indicators and return ``{name: {iso3: {year: value}}}``."""
    panel: dict = {}
    for name, code in INDICATORS.items():
        document = fetch_indicator(code, countries, cache_dir=cache_dir, refresh=refresh,
                                   start=start, end=end, download=download)
        panel[name] = parse_payload(document["payload"], countries=countries)
    return panel


# --------------------------------------------------------------------------------------- sample
def choose_sample(panel: dict, countries, *, start: int = START_YEAR, end: int = END_YEAR,
                  min_countries: int = MIN_COUNTRIES, min_years: int = MIN_YEARS) -> dict:
    """Largest contiguous year window with at least ``min_countries`` complete countries.

    Complete means both growth and inflation are present in every year of the window (the GVAR
    prototype needs a balanced panel).
    """
    for needed in ("gdp_growth", "inflation"):
        if needed not in panel:
            raise GVARRealError(f"indicator {needed!r} is missing from the panel")
    years_all = list(range(int(start), int(end) + 1))
    # try progressively shorter windows, keeping the longest (latest end is preserved)
    windows = []
    for first in range(start, end - min_years + 2):
        for last in range(end, first + min_years - 2, -1):
            windows.append((first, last))
    windows.sort(key=lambda window: (-(window[1] - window[0]), window[0]))
    for first, last in windows:
        years = [year for year in years_all if first <= year <= last]
        kept = [code for code in countries
                if all(panel["gdp_growth"].get(code, {}).get(year) is not None
                       and panel["inflation"].get(code, {}).get(year) is not None
                       for year in years)]
        if len(kept) >= min_countries:
            return {"countries": kept, "years": years, "first_year": first, "last_year": last,
                    "n_years": len(years), "n_countries": len(kept)}
    raise GVARRealError(
        f"no window between {start} and {end} has {min_countries}+ countries with complete growth "
        f"and inflation; the GVAR evaluation is refused")


# -------------------------------------------------------------------------------------- weights
def gdp_weights(panel: dict, countries, years) -> tuple[dict, dict]:
    """GDP-share proxy weights: ``w_ij = gdp_j / sum_{k != i} gdp_k`` (mean GDP over the sample)."""
    mean_gdp = {}
    for code in countries:
        values = [panel["gdp_current"].get(code, {}).get(year) for year in years]
        usable = [value for value in values if value is not None and value > 0]
        if not usable:
            raise GVARRealError(f"no positive GDP level for {code}; cannot build weights")
        mean_gdp[code] = float(np.mean(usable))
    weights = {}
    for code in countries:
        others = {other: mean_gdp[other] for other in countries if other != code}
        total = math.fsum(others.values())
        if total <= 0.0:
            raise GVARRealError(f"weights for {code} have no positive entries")
        weights[code] = {other: value / total for other, value in others.items()}
    return weights, {"method": "GDP_SHARE_PROXY", "gdp_levels": mean_gdp}


# ISO2 codes used by the FIGARO/Eurostat table -> ISO3 codes used by the World Bank panel.
ISO2_TO_ISO3 = {
    "AL": "ALB", "AR": "ARG", "AT": "AUT", "AU": "AUS", "BE": "BEL", "BG": "BGR", "BR": "BRA",
    "CA": "CAN", "CH": "CHE", "CN": "CHN", "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DK": "DNK",
    "EE": "EST", "EL": "GRC", "ES": "ESP", "FI": "FIN", "FR": "FRA", "HR": "HRV", "HU": "HUN",
    "ID": "IDN", "IE": "IRL", "IN": "IND", "IT": "ITA", "JP": "JPN", "KR": "KOR", "LT": "LTU",
    "LU": "LUX", "LV": "LVA", "ME": "MNE", "MK": "MKD", "MT": "MLT", "MX": "MEX", "NL": "NLD",
    "NO": "NOR", "PL": "POL", "PT": "PRT", "RO": "ROU", "RS": "SRB", "RU": "RUS", "SA": "SAU",
    "SE": "SWE", "SI": "SVN", "SK": "SVK", "TR": "TUR", "UK": "GBR", "US": "USA", "ZA": "ZAF",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _year_from_path(path: Path):
    """Vintage year from a ``figaro_2019`` / ``wiod_2000`` style directory (fallback only)."""
    import re
    for part in reversed(Path(path).parts):
        match = re.search(r"(19|20)\d{2}", part)
        if match:
            return int(match.group(0))
    return None


def figaro_export_matrix(dataset_path: Path) -> dict:
    """Bilateral export matrix from a FIGARO MRIO ``Z`` table (read-only, block sums).

    ``Z`` is a ``(n_countries*n_industries) x (n_countries*n_industries)`` matrix in country-major
    order; ``exports[i][j] = sum over industries of Z[block i, block j]`` is the value of products
    of country ``i`` used by country ``j`` (USD mn, the dataset's units).  The rest-of-world row and
    column are kept in the matrix but never used as a named GVAR weight (see
    :func:`figaro_trade_weights`).
    """
    path = Path(dataset_path)
    if not path.is_file():
        raise GVARRealError(f"FIGARO dataset not found: {path}")
    manifest = path.parent / "manifest.json"
    if manifest.is_file():
        try:
            expected_sha = json.loads(manifest.read_text(encoding="utf-8")).get(
                "dataset", {}).get("sha256")
        except (OSError, ValueError):
            expected_sha = None
        if expected_sha:
            current_sha = _sha256_file(path)
            if current_sha != expected_sha:
                raise GVARRealError(
                    f"FIGARO dataset {path} is incomplete: sha256 {current_sha[:16]} does not "
                    f"match manifest {expected_sha[:16]}; the file may be under a concurrent "
                    "rebuild (retry after it finishes)")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GVARRealError(f"cannot read FIGARO dataset {path}: {exc!r}") from exc
    countries = document.get("countries")
    industries = document.get("industries")
    Z = document.get("Z")
    Z_sparse = document.get("Z_sparse")
    if not isinstance(countries, list) or not isinstance(industries, list):
        raise GVARRealError("FIGARO dataset must provide countries and industries")
    if Z is None and Z_sparse is None:
        raise GVARRealError("FIGARO dataset must provide Z (dense) or Z_sparse (schema v1.1)")
    n_countries, n_industries = len(countries), len(industries)
    expected = n_countries * n_industries
    if Z is not None:
        try:
            matrix = np.asarray(Z, dtype=float)
        except (TypeError, ValueError) as exc:
            raise GVARRealError("FIGARO Z is not a numeric matrix") from exc
        if matrix.shape != (expected, expected):
            raise GVARRealError(f"FIGARO Z must be {expected} x {expected}, got {matrix.shape}")
        if not np.all(np.isfinite(matrix)):
            raise GVARRealError("FIGARO Z contains non-finite values")
        if np.any(matrix < 0.0):
            raise GVARRealError("FIGARO Z contains negative flows")
        blocks = matrix.reshape(n_countries, n_industries, n_countries, n_industries)
        exports = blocks.sum(axis=(1, 3))
        total_intermediate = float(matrix.sum())
        storage = "dense_Z"
    else:
        if not isinstance(Z_sparse, dict):
            raise GVARRealError("FIGARO Z_sparse must be a mapping rows/cols/values")
        rows = np.asarray(Z_sparse.get("rows", []), dtype=np.int64)
        cols = np.asarray(Z_sparse.get("cols", []), dtype=np.int64)
        values = np.asarray(Z_sparse.get("values", []), dtype=float)
        if not (rows.size == cols.size == values.size) or rows.size == 0:
            raise GVARRealError("FIGARO Z_sparse rows/cols/values must have equal non-zero length")
        if rows.min() < 0 or cols.min() < 0 or rows.max() >= expected or cols.max() >= expected:
            raise GVARRealError("FIGARO Z_sparse indices are outside the block matrix")
        if not np.all(np.isfinite(values)):
            raise GVARRealError("FIGARO Z_sparse contains non-finite values")
        if np.any(values < 0.0):
            raise GVARRealError("FIGARO Z_sparse contains negative flows")
        cell = (rows // n_industries) * n_countries + (cols // n_industries)
        exports = np.bincount(cell, weights=values, minlength=n_countries * n_countries
                              ).reshape(n_countries, n_countries)
        total_intermediate = float(values.sum())
        storage = "sparse_Z"
    countries = [str(code) for code in countries]
    return {"countries": countries,
            "industries": n_industries,
            "units": document.get("units", "USD_mn"),
            "year": int(document["years"][0]) if document.get("years") else _year_from_path(path),
            "exports": exports.tolist(),
            "total_intermediate_usd_mn": total_intermediate,
            "storage": storage,
            "sha256": _sha256_file(path),
            "schema": document.get("schema")}


def figaro_trade_weights(target_countries, *, dataset_path: Path = FIGARO_DATASET,
                         cache_dir: Path = CACHE_DIR, refresh: bool = False,
                         load=figaro_export_matrix) -> tuple[dict, dict]:
    """Trade weights ``w_ij`` from FIGARO block exports for the countries covered by the table.

    ``w_ij = export_ij / sum_{k != i} export_ik`` over the named partner countries of the target
    panel (``w_ii = 0``); the rest-of-world block is excluded both as a row and as a partner, so
    every returned row sums to 1.  Countries of the target panel that the table does not name are
    *not* in the returned mapping (the caller decides the fallback).  The export matrix is cached
    under ``data/mrio/wb/weights_figaro.json`` keyed on the dataset SHA-256.
    """
    path = Path(dataset_path)
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / "weights_figaro.json"
    sha = _sha256_file(path) if path.is_file() else None
    export_document = None
    if cache.is_file() and not refresh:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("dataset_sha256") == sha:
            export_document = cached
    if export_document is None:
        export_document = load(path)
        export_document["dataset_sha256"] = export_document.get("sha256", sha)
        export_document["source"] = str(path)
        cache.write_text(json.dumps(export_document) + "\n", encoding="utf-8", newline="\n")
    table_countries = [str(code) for code in export_document["countries"]]
    exports = np.asarray(export_document["exports"], dtype=float)
    if exports.shape != (len(table_countries), len(table_countries)):
        raise GVARRealError("cached FIGARO export matrix does not match its country list")
    index = {code: position for position, code in enumerate(table_countries)}
    named = [code for code in table_countries if code != "ROW"]
    iso3_of_table = {code: ISO2_TO_ISO3.get(code) for code in named}
    covered = sorted({iso3 for iso3 in iso3_of_table.values() if iso3 in set(target_countries)})
    if len(covered) < 2:
        raise GVARRealError(
            "FIGARO covers fewer than two countries of the panel; trade weights are undefined")
    weights = {}
    export_totals = {}
    for iso3 in covered:
        table_code = next(code for code, mapped in iso3_of_table.items() if mapped == iso3)
        row = exports[index[table_code]]
        partners = {}
        for other in named:
            other_table = other
            other_iso3 = iso3_of_table.get(other_table)
            if other_iso3 is None or other_iso3 not in covered or other_iso3 == iso3:
                continue
            value = float(row[index[other_table]])
            if value > 0.0:
                partners[other_iso3] = value
        total = math.fsum(partners.values())
        if total <= 0.0:
            continue
        weights[iso3] = {other: value / total for other, value in partners.items()}
        export_totals[iso3] = total
    if len(weights) < 2:
        raise GVARRealError("FIGARO export rows are empty for the covered countries")
    row_share = float(np.max(np.abs(exports.sum(axis=1))))
    coverage = {
        "table_countries": len(table_countries),
        "named_table_countries": len(named),
        "covered_iso3": covered,
        "uncovered_iso3": sorted(set(target_countries) - set(covered)),
        "exports_usd_mn": {code: round(total, 6) for code, total in export_totals.items()},
        "row_convention": "exports[i][j] = sum_industries Z[block i, block j]; ROW excluded",
        "dataset_sha256": export_document.get("dataset_sha256") or export_document.get("sha256"),
        "dataset_path": str(path),
        "units": export_document.get("units", "USD_mn"),
        "max_abs_row_total_usd_mn": round(row_share, 6),
    }
    return weights, {"method": "FIGARO_TRADE_WEIGHTS_2019", "detail": coverage}


def build_weights(panel: dict, countries, years, *, mode: str = "auto",
                  figaro_path: Path = FIGARO_DATASET, cache_dir: Path = CACHE_DIR,
                  refresh: bool = False,
                  load=figaro_export_matrix) -> tuple[dict, dict]:
    """Weights for the GVAR panel: FIGARO trade weights with a GDP fallback, or the GDP proxy.

    ``mode="auto"`` (default): FIGARO if the dataset exists, else the GDP proxy.
    ``mode="figaro"``: FIGARO rows for covered countries, GDP-proxy rows for the countries the
    table does not name (the mixed fallback is reported); a missing dataset is a hard failure.
    ``mode="gdp"``: GDP-share proxy for every country.
    """
    if mode not in ("auto", "figaro", "gdp"):
        raise GVARRealError(f"unknown weights mode {mode!r}")
    if mode == "gdp":
        weights, source = gdp_weights(panel, countries, years)
        source = dict(source, mode="gdp")
        return weights, source
    dataset_available = Path(figaro_path).is_file()
    if not dataset_available:
        if mode == "figaro":
            raise GVARRealError(f"FIGARO dataset not found: {figaro_path}")
        weights, source = gdp_weights(panel, countries, years)
        return weights, dict(source, mode="gdp", fallback_reason="FIGARO dataset missing")
    figaro_rows, figaro_source = figaro_trade_weights(
        countries, dataset_path=figaro_path, cache_dir=cache_dir, refresh=refresh, load=load)
    gdp_rows, gdp_source = gdp_weights(panel, countries, years)
    weights = {}
    for code in countries:
        if code in figaro_rows:
            weights[code] = figaro_rows[code]
        else:
            weights[code] = gdp_rows[code]
    detail = dict(figaro_source["detail"])
    detail.update({"mode": mode, "mrio_rows": len(figaro_rows),
                   "gdp_fallback_rows": sorted(set(countries) - set(figaro_rows)),
                   "gdp_levels": gdp_source["gdp_levels"]})
    return weights, {"method": figaro_source["method"], "detail": detail}


# -------------------------------------------------------------------------------------- dataset
def build_dataset(panel: dict, countries, years, weights) -> dict:
    """Balanced two-variable GVAR dataset: ``g`` (real GDP growth) and ``pi`` (CPI inflation)."""
    for name in ("gdp_growth", "inflation"):
        if name not in panel:
            raise GVARRealError(f"indicator {name!r} is missing")
    series = {}
    for code in countries:
        growth = [panel["gdp_growth"].get(code, {}).get(year) for year in years]
        inflation = [panel["inflation"].get(code, {}).get(year) for year in years]
        if any(value is None for value in growth + inflation):
            raise GVARRealError(f"incomplete series for {code}; the panel is not balanced")
        series[code] = {"g": [float(value) for value in growth],
                        "pi": [float(value) for value in inflation]}
    dataset = {"countries": list(countries), "series": series, "weights": weights}
    return dataset


def _checked_dataset(dataset: dict, lags: int) -> GVAR:
    try:
        return GVAR(dataset, lags=lags)
    except GVARError as exc:
        raise GVARRealError(f"GVAR estimation refused: {exc}") from exc


# ------------------------------------------------------------------------------------ aggregates
def group_weights(countries, years, panel: dict, groups: dict) -> dict:
    """GDP-share weights inside each reported group (US, EA, CN, world)."""
    gdp = {}
    for code in countries:
        values = [panel["gdp_current"].get(code, {}).get(year) for year in years]
        usable = [value for value in values if value is not None and value > 0]
        gdp[code] = float(np.mean(usable)) if usable else 0.0
    out = {}
    for name, members in groups.items():
        selected = [code for code in members if code in countries]
        total = math.fsum(gdp[code] for code in selected)
        if not selected or total <= 0.0:
            raise GVARRealError(f"aggregate {name!r} has no countries with GDP in the sample")
        out[name] = {code: gdp[code] / total for code in selected}
    return out


def aggregate_series(series: dict, weights: dict) -> np.ndarray:
    values = np.zeros_like(np.asarray(series[list(weights)[0]], dtype=float))
    for code, weight in weights.items():
        values = values + weight * np.asarray(series[code], dtype=float)
    return values


def _rmse(errors) -> float:
    array = np.asarray(errors, dtype=float)
    return float(np.sqrt(np.mean(array ** 2))) if array.size else float("nan")


def _mae(errors) -> float:
    array = np.asarray(errors, dtype=float)
    return float(np.mean(np.abs(array))) if array.size else float("nan")


# ----------------------------------------------------------------------------------- OOS engine
def ar1_forecast(values, steps: int) -> list[float]:
    """Univariate AR(1) with intercept fitted on ``values``, recursive h-step forecasts."""
    y = np.asarray(values, dtype=float)
    if y.size < 3:
        raise GVARRealError("AR(1) needs at least three observations")
    x = np.column_stack([np.ones(y.size - 1), y[:-1]])
    target = y[1:]
    beta, *_ = np.linalg.lstsq(x, target, rcond=None)
    path = []
    last = float(y[-1])
    for _ in range(steps):
        last = float(beta[0] + beta[1] * last)
        path.append(last)
    return path


def rolling_oos(panel: dict, countries, years, weights, groups: dict, *, lags_list=(1, 2),
                max_h: int = 4, n_origins: int = 15) -> dict:
    """Rolling (expanding) out-of-sample comparison of GVAR(p), AR(1) and a random walk."""
    series = {code: {"g": np.asarray([panel["gdp_growth"][code][year] for year in years]),
                     "pi": np.asarray([panel["inflation"][code][year] for year in years])}
              for code in countries}
    shares = group_weights(countries, years, panel, groups)
    total = len(years)
    if total < MIN_TRAIN + max_h:
        raise GVARRealError(f"sample of {total} years is too short for OOS h<= {max_h}")
    origins = list(range(max(MIN_TRAIN, total - int(n_origins)), total - 1))
    models = [f"GVAR_p{lags}" for lags in lags_list] + ["AR1", "RW"]
    errors: dict = {model: {group: {variable: {h: [] for h in range(1, max_h + 1)}
                                    for variable in ("g", "pi")} for group in groups}
                    for model in models}
    diagnostics: dict = {f"GVAR_p{lags}": {"fits": 0, "skipped": 0, "stable": 0}
                         for lags in lags_list}
    for origin in origins:
        train_slice = slice(0, origin + 1)
        dataset = {"countries": list(countries),
                   "series": {code: {"g": series[code]["g"][train_slice].tolist(),
                                     "pi": series[code]["pi"][train_slice].tolist()}
                              for code in countries},
                   "weights": weights}
        forecasts: dict = {}
        for lags in lags_list:
            name = f"GVAR_p{lags}"
            try:
                model = _checked_dataset(dataset, lags)
            except GVARRealError:
                diagnostics[name]["skipped"] += 1
                continue
            diagnostics[name]["fits"] += 1
            diagnostics[name]["stable"] += int(model.is_stable())
            path = model.forecast(steps=max_h)["path"]
            forecasts[name] = {variable: {code: path[code][variable] for code in countries}
                               for variable in ("g", "pi")}
        forecasts["AR1"] = {}
        forecasts["RW"] = {}
        for variable in ("g", "pi"):
            forecasts["AR1"][variable] = {}
            forecasts["RW"][variable] = {}
            for code in countries:
                values = series[code][variable][train_slice]
                forecasts["AR1"][variable][code] = ar1_forecast(values, max_h)
                forecasts["RW"][variable][code] = [float(values[-1])] * max_h
        for h in range(1, max_h + 1):
            if origin + h >= total:
                continue
            for group, share in shares.items():
                for variable in ("g", "pi"):
                    actual = math.fsum(weight * float(series[code][variable][origin + h])
                                       for code, weight in share.items())
                    for model in models:
                        if model not in forecasts:
                            continue
                        forecast = math.fsum(weight * float(forecasts[model][variable][code][h - 1])
                                             for code, weight in share.items())
                        errors[model][group][variable][h].append(forecast - actual)
    metrics = {model: {group: {variable: {h: {"rmse": _rmse(errors[model][group][variable][h]),
                                              "mae": _mae(errors[model][group][variable][h]),
                                              "n": len(errors[model][group][variable][h])}
                                       for h in range(1, max_h + 1)}
                                for variable in ("g", "pi")} for group in groups}
               for model in models}
    per_country = {}
    for model in models:
        per_country[model] = {}
        for code in countries:
            per_country[model][code] = {}
            for variable in ("g", "pi"):
                per_country[model][code][variable] = {}
                for h in range(1, max_h + 1):
                    model_errors = []
                    for origin in origins:
                        if origin + h >= total:
                            continue
                        forecast = forecasts.get(model, {}).get(variable, {}).get(code)
                        if forecast is None:
                            continue
                        actual = float(series[code][variable][origin + h])
                        model_errors.append(float(forecast[h - 1]) - actual)
                    per_country[model][code][variable][h] = {"rmse": _rmse(model_errors),
                                                             "mae": _mae(model_errors),
                                                             "n": len(model_errors)}
    return {"origins": [int(years[index]) for index in origins], "origins_index": origins,
            "metrics": metrics, "per_country": per_country, "diagnostics": diagnostics,
            "groups": {name: sorted(share) for name, share in shares.items()},
            "group_weights": {name: {code: round(weight, 12) for code, weight in share.items()}
                              for name, share in shares.items()}}


# ------------------------------------------------------------------------------------------- IRF
def irf_report(model: GVAR, groups: dict, *, horizons: int = 8, shock_country: str = "USA",
               shock_variable: str = "g", shock_size: float = 1.0) -> dict:
    response = model.irf(shock_country, shock_variable, horizons=horizons, shock_size=shock_size)
    out: dict = {}
    for name, members in groups.items():
        selected = [code for code in members if code in model.countries]
        if not selected:
            continue
        out[name] = {}
        for variable in model.variables:
            aggregated = []
            for step in range(horizons + 1):
                aggregated.append(float(np.mean([response["response"][code][variable][step]
                                                 for code in selected])))
            out[name][variable] = [round(value, 6) for value in aggregated]
    return {"method": response["method"], "shock_country": shock_country,
            "shock_variable": shock_variable, "shock_size": shock_size,
            "horizons": list(range(horizons + 1)), "response": out,
            "note": "group response = simple mean of member-country responses (same order as the "
                    "aggregate weights are not used here)"}


# ------------------------------------------------------------------------------------------ run
def run(*, refresh: bool = False, cache_dir: Path = CACHE_DIR, lags_list=(1, 2), max_h: int = 4,
        n_origins: int = 15, countries=COUNTRIES, start: int = START_YEAR, end: int = END_YEAR,
        weights_mode: str = "auto", figaro_path: Path = FIGARO_DATASET,
        load_figaro=figaro_export_matrix) -> dict:
    panel = load_indicators(countries, cache_dir=cache_dir, refresh=refresh, start=start, end=end)
    sample = choose_sample(panel, countries, start=start, end=end)
    kept, years = sample["countries"], sample["years"]
    weights, weight_source = build_weights(panel, kept, years, mode=weights_mode,
                                           figaro_path=figaro_path, cache_dir=cache_dir,
                                           refresh=refresh, load=load_figaro)
    dataset = build_dataset(panel, kept, years, weights)
    fits = {}
    for lags in lags_list:
        model = _checked_dataset(dataset, lags)
        fits[f"p{lags}"] = {"stable": model.is_stable(),
                            "max_modulus": model.stability()["max_modulus"],
                            "observations": model.n, "variables": list(model.variables)}
    groups = {"US": ["USA"], "EA": [code for code in EURO_AREA if code in kept],
              "CN": ["CHN"], "WORLD": kept}
    oos = rolling_oos(panel, kept, years, weights, groups, lags_list=lags_list, max_h=max_h,
                      n_origins=n_origins)
    full = _checked_dataset(dataset, max(lags_list))
    irf = irf_report(full, groups, horizons=8)
    report = {
        "report_id": "AMFE-GVAR-REAL-WB",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "World Bank WDI via api.worldbank.org (no key); open terms CC BY 4.0",
        "indicators": dict(INDICATORS),
        "sample": {"first_year": sample["first_year"], "last_year": sample["last_year"],
                   "years": sample["n_years"], "countries": sample["n_countries"],
                   "country_codes": kept},
        "weights": {"source": weight_source["method"], "detail": weight_source},
        "weights_summary": weights_summary(weights, kept),
        "fits": fits,
        "irf": irf,
        "oos": oos,
        "limits": [
            "annual WDI data, 30 observations at most; short samples weaken the VARX estimates",
            "no vintages / point-in-time data: GDP and CPI are revised",
            "the GVAR prototype is a reduced-form model; no identification beyond Cholesky",
            "aggregates are GDP-weighted means of member forecasts, not a separate model",
        ],
    }
    return report


def weights_summary(weights: dict, countries, *, top: int = 5) -> dict:
    """Top trade partners and row checks for a few reference countries (report metadata)."""
    selected = [code for code in ("USA", "DEU", "CHN", "JPN") if code in weights]
    out = {"top_partners": {}, "row_sums": {}, "zero_self": True}
    for code in selected:
        row = weights[code]
        ordered = sorted(row.items(), key=lambda item: (-item[1], item[0]))
        out["top_partners"][code] = [[partner, round(value, 6)] for partner, value in ordered[:top]]
        out["row_sums"][code] = round(math.fsum(row.values()), 12)
        out["zero_self"] = out["zero_self"] and code not in row
    out["max_row_gap"] = round(max(abs(math.fsum(row.values()) - 1.0)
                                   for row in weights.values()), 12)
    return out


def compare_runs(figaro: dict, gdp: dict, *, label_a: str = "FIGARO",
                 label_b: str = "GDP_PROXY") -> dict:
    """Paired OOS comparison of two runs that differ only in the GVAR weights."""
    a, b = figaro["oos"]["metrics"], gdp["oos"]["metrics"]
    groups = tuple(name for name in ("US", "EA", "CN", "WORLD") if name in a["GVAR_p1"])
    aggregate = {}
    horizons = sorted({int(key) for key in a["GVAR_p1"][groups[0]]["g"]})
    for group in groups:
        aggregate[group] = {}
        for variable in ("g", "pi"):
            aggregate[group][variable] = {}
            for h in horizons:
                ea = a["GVAR_p1"][group][variable][h]
                eb = b["GVAR_p1"][group][variable][h]
                delta = ea["rmse"] - eb["rmse"]
                aggregate[group][variable][str(h)] = {
                    "rmse_a": round(ea["rmse"], 6), "rmse_b": round(eb["rmse"], 6),
                    "mae_a": round(ea["mae"], 6), "mae_b": round(eb["mae"], 6),
                    "delta_rmse": round(delta, 6), "better": (label_a if delta < -1e-12
                                                              else label_b if delta > 1e-12
                                                              else "TIE")}
    per_country = {}
    pa, pb = figaro["oos"]["per_country"]["GVAR_p1"], gdp["oos"]["per_country"]["GVAR_p1"]
    countries = sorted(pa)
    for variable in ("g", "pi"):
        per_country[variable] = {}
        for h in horizons:
            better_a = better_b = ties = 0
            deltas = []
            for code in countries:
                ea = pa[code][variable][h]["rmse"]
                eb = pb[code][variable][h]["rmse"]
                deltas.append(ea - eb)
                if ea < eb - 1e-12:
                    better_a += 1
                elif eb < ea - 1e-12:
                    better_b += 1
                else:
                    ties += 1
            deltas.sort()
            median = float(np.median(deltas)) if deltas else float("nan")
            per_country[variable][str(h)] = {"better_a": better_a, "better_b": better_b,
                                             "ties": ties, "median_delta_rmse": round(median, 6)}
    baseline_identical = json.dumps(a.get("AR1"), sort_keys=True) == json.dumps(
        b.get("AR1"), sort_keys=True) and json.dumps(a.get("RW"), sort_keys=True) == json.dumps(
        b.get("RW"), sort_keys=True)
    return {"label_a": label_a, "label_b": label_b,
            "primary_model": "GVAR_p1", "aggregate": aggregate,
            "per_country": per_country,
            "baseline_identical": baseline_identical,
            "note": ("delta_rmse = RMSE(a) - RMSE(b); negative favours the FIGARO weights. "
                     "AR(1) and RW do not depend on the weights and are identical by "
                     "construction.")}


def digest(report: dict) -> str:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _summary(report: dict) -> dict:
    metrics = report["oos"]["metrics"]
    lines = {}
    for group in ("US", "EA", "CN", "WORLD"):
        lines[group] = {}
        for model in metrics:
            entry = {}
            for variable in ("g", "pi"):
                for h in (1, 4):
                    value = metrics[model][group][variable].get(h, {})
                    entry[f"{variable}_h{h}"] = {"rmse": round(value.get("rmse", float("nan")), 6)
                                                 if value.get("rmse") == value.get("rmse") else None,
                                                 "mae": round(value.get("mae", float("nan")), 6)
                                                 if value.get("mae") == value.get("mae") else None}
            lines[group][model] = entry
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GVAR on World Bank data with rolling OOS")
    parser.add_argument("--refresh", action="store_true", help="re-download the WB payloads")
    parser.add_argument("--lags", default="1,2", help="GVAR lag orders to compare (default 1,2)")
    parser.add_argument("--max-h", type=int, default=4, help="OOS horizon (default 4)")
    parser.add_argument("--origins", type=int, default=15, help="number of rolling OOS origins")
    parser.add_argument("--weights", choices=WEIGHTS_MODES, default="auto",
                        help="auto: FIGARO if the dataset exists else GDP; figaro: require FIGARO "
                             "(GDP fallback rows for uncovered countries); gdp: proxy only; "
                             "yby: v2 pipeline with the nearest MRIO vintage per year and BIC/AIC "
                             "lag selection in the rolling OOS")
    parser.add_argument("--figaro", default=str(FIGARO_DATASET), help="FIGARO dataset.json path")
    parser.add_argument("--cache-dir", default=None,
                        help="World Bank payload cache (default: <mrio root>/wb)")
    parser.add_argument("--report", default=None,
                        help="report path (default: data/mrio/wb/gvar_report.json for legacy modes, "
                             "data/mrio/cache/gvar_real/gvar_v2_report.json for --weights yby)")
    parser.add_argument("--data-root", default=None,
                        help="MRIO data root (default: AMFE_MRIO_ROOT or data/mrio)")
    parser.add_argument("--weights-cache", default=None,
                        help="export-matrix cache for --weights yby (default: <data root>/cache/gvar_real)")
    parser.add_argument("--select-lag", choices=SELECT_MODES, default="bic",
                        help="lag selection inside the rolling OOS for --weights yby (default bic)")
    args = parser.parse_args(argv)
    if args.weights == "yby":
        return yby_cli(args)
    lags_list = tuple(sorted({int(part) for part in args.lags.split(",") if part.strip()}))
    figaro_path = Path(args.figaro)
    figaro_available = figaro_path.is_file()
    if args.weights == "figaro" and not figaro_available:
        raise SystemExit(f"FIGARO dataset not found: {figaro_path}")
    modes = ["gdp"] if args.weights == "gdp" else (
        ["figaro", "gdp"] if figaro_available else ["gdp"])
    runs = {}
    legacy_cache = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    for mode in modes:
        runs[mode] = run(refresh=args.refresh, cache_dir=legacy_cache,
                         lags_list=lags_list, max_h=args.max_h, n_origins=args.origins,
                         weights_mode=mode, figaro_path=figaro_path)
    primary = runs["figaro"] if "figaro" in runs else runs["gdp"]
    report = {
        "report_id": "AMFE-GVAR-REAL-WB",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "World Bank WDI via api.worldbank.org (no key); FIGARO 2019 via Eurostat "
                  "re-use policy; open data, attribution required",
        "indicators": dict(INDICATORS),
        "weights_mode": primary["weights"]["source"],
        "sample": primary["sample"],
        "weights": primary["weights"],
        "weights_summary": primary["weights_summary"],
        "runs": runs,
        "comparison": (compare_runs(runs["figaro"], runs["gdp"])
                       if "figaro" in runs and "gdp" in runs else None),
        "limits": primary["limits"] + [
            "FIGARO weights are a single 2019 table applied to the whole 1995-2024 sample",
            "trade weights are an input, not estimated; time-invariant by construction",
            "the FIGARO table covers 42 of the 53 World Bank countries; the rest use the GDP "
            "proxy rows (reported in weights.detail.gdp_fallback_rows)",
        ],
    }
    report["report_sha256"] = digest({k: v for k, v in report.items() if k != "report_sha256"})
    path = Path(args.report) if args.report else REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                    newline="\n")
    summary = {
        "weights_mode": report["weights_mode"],
        "weights_detail": {key: value for key, value in report["weights"]["detail"].get("detail", {}).items()
                           if key in ("mode", "mrio_rows", "gdp_fallback_rows",
                                      "dataset_sha256", "covered_iso3")},
        "weights_summary": report["weights_summary"],
        "sample": report["sample"],
        "fits": {mode: run_report["fits"] for mode, run_report in runs.items()},
        "irf": {mode: run_report["irf"]["response"].get("WORLD", {}).get("g")
                for mode, run_report in runs.items()},
        "metrics": {mode: _summary(run_report) for mode, run_report in runs.items()},
        "comparison": report["comparison"],
        "report": str(path), "report_sha256": report["report_sha256"]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


# ============================================================================================
# v2 pipeline: nearest-vintage MRIO weights, BIC/AIC lag selection in the rolling OOS,
# five-model comparison (GVAR year weights, GVAR fixed 2019, GVAR GDP proxy, AR(1), RW).
# Everything below is additive: the legacy modes (auto/figaro/gdp) and their report are untouched.
# ============================================================================================
ROW_CODES = ("ROW", "RoW", "REST_OF_WORLD", "WLD")


def _mrio_root(data_root=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    return Path(os.environ.get("AMFE_MRIO_ROOT") or MRIO_ROOT)


def wiod_export_matrix(dataset_path: Path) -> dict:
    """Bilateral export matrix from a dense WIOD-style ``Z`` table (block sums by country)."""
    path = Path(dataset_path)
    if not path.is_file():
        raise GVARRealError(f"WIOD dataset not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GVARRealError(f"cannot read WIOD dataset {path}: {exc!r}") from exc
    countries = document.get("countries")
    industries = document.get("industries")
    Z = document.get("Z")
    if not isinstance(countries, list) or not isinstance(industries, list):
        raise GVARRealError("WIOD dataset must provide countries and industries")
    if Z is None:
        raise GVARRealError("WIOD dataset must provide a dense Z matrix")
    n_countries, n_industries = len(countries), len(industries)
    expected = n_countries * n_industries
    try:
        matrix = np.asarray(Z, dtype=float)
    except (TypeError, ValueError) as exc:
        raise GVARRealError("WIOD Z is not a numeric matrix") from exc
    if matrix.shape != (expected, expected):
        raise GVARRealError(f"WIOD Z must be {expected} x {expected}, got {matrix.shape}")
    if not np.all(np.isfinite(matrix)):
        raise GVARRealError("WIOD Z contains non-finite values")
    if np.any(matrix < 0.0):
        raise GVARRealError("WIOD Z contains negative flows")
    blocks = matrix.reshape(n_countries, n_industries, n_countries, n_industries)
    exports = blocks.sum(axis=(1, 3))
    return {"countries": [str(code) for code in countries],
            "industries": n_industries,
            "units": document.get("units", "USD_mn"),
            "year": int(document["years"][0]) if document.get("years") else _year_from_path(path),
            "exports": exports.tolist(),
            "total_intermediate_usd_mn": float(matrix.sum()),
            "storage": "dense_Z",
            "sha256": _sha256_file(path),
            "schema": document.get("schema")}


def load_export_matrix(path, *, kind: str, cache_dir, refresh: bool = False) -> dict:
    """Load a MRIO vintage's export matrix, cached on disk by the dataset SHA-256."""
    path = Path(path)
    if kind not in ("figaro", "wiod"):
        raise GVARRealError(f"unknown MRIO kind {kind!r}")
    if not path.is_file():
        raise GVARRealError(f"MRIO dataset not found: {path}")
    sha = _sha256_file(path)
    directory = Path(cache_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GVARRealError(f"cannot create the MRIO export cache {directory}: {exc}") from exc
    cache_file = directory / f"{kind}_{sha[:16]}.json"
    if cache_file.is_file() and not refresh:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if (isinstance(cached, dict) and cached.get("sha256") == sha
                and cached.get("year") is not None):
            cached.setdefault("kind", kind)
            cached.setdefault("dataset_path", str(path))
            return cached
    loader = figaro_export_matrix if kind == "figaro" else wiod_export_matrix
    document = loader(path)
    if not isinstance(document, dict):
        raise GVARRealError(f"MRIO loader for {path} returned {type(document).__name__}")
    document["kind"] = kind
    document["sha256"] = document.get("sha256") or sha
    document["dataset_path"] = str(path)
    document.setdefault("source", str(path))
    if document.get("year") is None:
        raise GVARRealError(f"cannot determine the vintage year of {path}")
    try:
        cache_file.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8",
                              newline="\n")
    except OSError as exc:
        raise GVARRealError(f"cannot write the MRIO export cache {cache_file}: {exc}") from exc
    return document


def discover_mrio_vintages(data_root=None, *, cache_dir=None, refresh: bool = False) -> list:
    """All available MRIO vintages under the data root, sorted by year (fail-closed when none)."""
    root = _mrio_root(data_root)
    cache = Path(cache_dir) if cache_dir is not None else root / "cache" / "gvar_real"
    paths = []
    figaro_dir = root / "figaro"
    if figaro_dir.is_dir():
        paths.extend((path, "figaro") for path in sorted(figaro_dir.glob("figaro_*/dataset.json")))
    wiod_dir = root / "wiod_2016"
    if wiod_dir.is_dir():
        paths.extend((path, "wiod") for path in sorted(wiod_dir.glob("wiod_*/dataset.json")))
    if not paths:
        raise GVARRealError(
            f"no MRIO dataset under {root}: expected figaro/figaro_*/dataset.json or "
            "wiod_2016/wiod_*/dataset.json; set AMFE_MRIO_ROOT to the MRIO data root")
    documents = [load_export_matrix(path, kind=kind, cache_dir=cache, refresh=refresh)
                 for path, kind in paths]
    years = [int(document["year"]) for document in documents]
    if len(set(years)) != len(years):
        raise GVARRealError(f"duplicate MRIO vintage years are not allowed: {sorted(years)}")
    documents.sort(key=lambda document: int(document["year"]))
    return documents


def choose_vintage(year: int, vintages) -> tuple:
    """Nearest available MRIO vintage; ties resolve to the earlier vintage.

    Years before the first vintage use the first one (e.g. 1995-1999 -> 2000); years after the last
    use the last one (e.g. 2020+ -> 2019 FIGARO).  The rule is deterministic and is recorded.
    """
    if not vintages:
        raise GVARRealError("no MRIO vintages available to choose from")
    best = min(vintages, key=lambda document: (abs(int(document["year"]) - int(year)),
                                               int(document["year"])))
    distance = abs(int(best["year"]) - int(year))
    return best, {"target_year": int(year), "vintage_year": int(best["year"]),
                  "distance": int(distance),
                  "rule": "nearest MRIO vintage; ties -> earlier vintage; "
                          "before the first vintage -> first; after the last -> last"}


def weights_from_exports(document: dict, target_countries) -> tuple:
    """Trade weights from a vintage export matrix, over the named partners of the target panel."""
    countries = [str(code) for code in document.get("countries") or []]
    try:
        exports = np.asarray(document.get("exports"), dtype=float)
    except (TypeError, ValueError) as exc:
        raise GVARRealError("MRIO export matrix is not numeric") from exc
    if not countries or exports.ndim != 2 or exports.shape != (len(countries), len(countries)):
        raise GVARRealError("MRIO export matrix does not match its country list")
    if not np.all(np.isfinite(exports)) or np.any(exports < 0.0):
        raise GVARRealError("MRIO export matrix contains non-finite or negative values")
    kind = str(document.get("kind") or "")
    named = [code for code in countries if code not in ROW_CODES]
    if kind == "figaro":
        iso3_of = {code: ISO2_TO_ISO3.get(code) for code in named}
    else:
        iso3_of = {code: (code if len(code) >= 3 and code not in ROW_CODES else None)
                   for code in named}
    iso3_of = {code: mapped for code, mapped in iso3_of.items() if mapped}
    if len(set(iso3_of.values())) != len(iso3_of):
        raise GVARRealError("MRIO country codes are not unique after ISO mapping")
    target = {str(code) for code in target_countries}
    covered = sorted({mapped for mapped in iso3_of.values() if mapped in target})
    if len(covered) < 2:
        raise GVARRealError(
            f"MRIO vintage {document.get('year')} ({kind}) covers fewer than two panel countries")
    index = {code: position for position, code in enumerate(countries)}
    reverse = {mapped: code for code, mapped in iso3_of.items()}
    weights: dict = {}
    totals: dict = {}
    for iso3 in covered:
        table_code = reverse[iso3]
        row = exports[index[table_code]]
        partners = {}
        for other in named:
            mapped = iso3_of.get(other)
            if mapped is None or mapped == iso3 or mapped not in covered:
                continue
            value = float(row[index[other]])
            if value > 0.0:
                partners[mapped] = value
        total = math.fsum(partners.values())
        if total <= 0.0:
            continue
        weights[iso3] = {other: value / total for other, value in partners.items()}
        totals[iso3] = total
    if len(weights) < 2:
        raise GVARRealError(f"MRIO vintage {document.get('year')} has no usable export rows")
    for code, row in weights.items():
        gap = abs(math.fsum(row.values()) - 1.0)
        if gap > 1e-9 or code in row:
            raise GVARRealError(f"MRIO weights for {code} are not unit rows without self weight")
    coverage = {"vintage_year": document.get("year"), "kind": kind,
                "table_countries": len(countries), "named_table_countries": len(named),
                "covered_iso3": covered,
                "uncovered_iso3": sorted(target - set(covered)),
                "exports_usd_mn": {code: round(value, 6) for code, value in totals.items()},
                "dataset_sha256": document.get("sha256"),
                "dataset_path": document.get("dataset_path"),
                "units": document.get("units", "USD_mn")}
    return weights, coverage


def _with_gdp_fallback(trade_rows: dict, gdp_rows: dict, countries) -> tuple:
    """Merge trade rows with the GDP-proxy rows for panel countries the vintage does not cover."""
    merged: dict = {}
    fallback = []
    for code in countries:
        if code in trade_rows:
            merged[code] = trade_rows[code]
            continue
        row = gdp_rows.get(code) or {}
        if not row:
            raise GVARRealError(f"no trade and no GDP-proxy weights for country {code}")
        merged[code] = row
        fallback.append(code)
    for code in countries:
        row = merged[code]
        if abs(math.fsum(row.values()) - 1.0) > 1e-9:
            raise GVARRealError(f"weights for {code} do not sum to one")
        if code in row:
            raise GVARRealError(f"weights for {code} contain a self weight")
    return merged, sorted(fallback)


def build_weights_by_year(panel: dict, countries, years, *, data_root=None, cache_dir=None,
                          refresh: bool = False, vintages=None) -> tuple:
    """Weights per target year from the nearest available MRIO vintage (deterministic rule)."""
    root = _mrio_root(data_root)
    cache = Path(cache_dir) if cache_dir is not None else root / "cache" / "gvar_real"
    vintages = (list(vintages) if vintages is not None
                else discover_mrio_vintages(root, cache_dir=cache, refresh=refresh))
    if not vintages:
        raise GVARRealError("no MRIO vintages available: the year-by-year weights are undefined")
    gdp_rows, gdp_source = gdp_weights(panel, countries, years)
    by_year: dict = {}
    per_year: dict = {}
    for year in years:
        vintage, choice = choose_vintage(int(year), vintages)
        trade_rows, coverage = weights_from_exports(vintage, countries)
        merged, fallback = _with_gdp_fallback(trade_rows, gdp_rows, countries)
        by_year[int(year)] = merged
        per_year[int(year)] = {"vintage_year": choice["vintage_year"],
                               "kind": coverage["kind"],
                               "distance": choice["distance"],
                               "covered_iso3": len(coverage["covered_iso3"]),
                               "fallback_rows": fallback,
                               "dataset_path": coverage["dataset_path"],
                               "dataset_sha256": coverage["dataset_sha256"]}
    source = {"method": "NEAREST_MRIO_VINTAGE",
              "rule": ("nearest MRIO vintage: for each target year use the vintage with the "
                       "smallest absolute distance |target_year - vintage_year|; ties -> earlier "
                       "vintage; years before the first vintage use the first; after the last use "
                       "the last; rows for panel countries absent from the vintage use the "
                       "GDP-share proxy"),
              "vintages": [{"year": int(document["year"]), "kind": document.get("kind"),
                            "dataset_path": document.get("dataset_path"),
                            "sha256": document.get("sha256"),
                            "countries": len(document.get("countries") or [])}
                           for document in vintages],
              "per_year": per_year,
              "fallback_total": int(sum(len(entry["fallback_rows"]) for entry in per_year.values())),
              "gdp_proxy_method": gdp_source["method"],
              "cache_dir": str(cache)}
    return by_year, source


def fixed_2019_weights(panel: dict, countries, years, *, vintages) -> tuple:
    """One fixed 2019 MRIO table for every year of the sample (the v1 baseline)."""
    candidates = [document for document in vintages if int(document["year"]) == 2019]
    if not candidates:
        raise GVARRealError("no 2019 MRIO vintage: the fixed-2019 weights are undefined "
                            "(fail-closed)")
    vintage = sorted(candidates, key=lambda document: (str(document.get("kind")),
                                                       str(document.get("dataset_path"))))[0]
    trade_rows, coverage = weights_from_exports(vintage, countries)
    gdp_rows, _ = gdp_weights(panel, countries, years)
    merged, fallback = _with_gdp_fallback(trade_rows, gdp_rows, countries)
    source = {"method": "FIXED_MRIO_2019", "vintage_year": 2019, "kind": coverage["kind"],
              "dataset_path": coverage["dataset_path"],
              "dataset_sha256": coverage["dataset_sha256"],
              "covered_iso3": coverage["covered_iso3"], "fallback_rows": fallback}
    return merged, source


def information_criteria(dataset: dict, lags: int) -> dict:
    """Equation-by-equation AIC/BIC of a GVAR(p) fit on a training window (Gaussian residuals).

    The joint residual covariance is singular for a panel with more series than training rows
    (53 countries x 2 variables > 27 rows), so the criterion is summed over the country equations:
    ``BIC = sum_eq [T*ln(sigma2_eq) + n_params_eq*ln(T)]`` (and AIC with ``2*n_params_eq``).
    """
    model = _checked_dataset(dataset, lags)
    observations = model.n - model.lags
    k = model.k
    loglikelihood = 0.0
    parameters = 0
    for country in model.countries:
        residual = np.asarray(model.residuals[country], dtype=float)
        variance = np.mean(residual ** 2, axis=0)
        if variance.size != k or np.any(variance <= 0.0) or not np.all(np.isfinite(variance)):
            raise GVARRealError(
                f"degenerate residuals for {country} at lag {lags}: cannot compute AIC/BIC")
        loglikelihood += (-0.5 * observations * k * (1.0 + math.log(2.0 * math.pi))
                          - 0.5 * observations * float(np.sum(np.log(variance))))
        parameters += k * (1 + 2 * k * int(lags))
    aic = -2.0 * loglikelihood + 2.0 * parameters
    bic = -2.0 * loglikelihood + parameters * math.log(observations)
    stability = model.stability()
    return {"lags": int(lags), "loglik": round(float(loglikelihood), 9),
            "aic": round(float(aic), 9), "bic": round(float(bic), 9),
            "params": int(parameters), "observations": int(observations),
            "stable": bool(stability["stable"]),
            "max_modulus": round(float(stability["max_modulus"]), 9)}


def select_lag(dataset: dict, lags=(1, 2), criterion: str = "bic") -> dict:
    """Choose p in ``lags`` by the information criterion on the training window (tie -> smaller p)."""
    if criterion not in ("aic", "bic"):
        raise GVARRealError(f"unknown lag-selection criterion {criterion!r}")
    requested = tuple(sorted({int(lag) for lag in lags}))
    if not requested:
        raise GVARRealError("lag candidates must be a non-empty subset of (1, 2)")
    table: dict = {}
    for lag in requested:
        try:
            table[lag] = information_criteria(dataset, lag)
        except GVARRealError as exc:
            table[lag] = {"lags": lag, "skipped": str(exc)}
    feasible = [lag for lag in requested if "bic" in table[lag]]
    if not feasible:
        raise GVARRealError(f"no feasible lag among {requested}: {table}")
    selected = min(feasible, key=lambda lag: (table[lag][criterion], lag))
    return {"criterion": criterion, "selected": int(selected),
            "criteria": {str(lag): value for lag, value in table.items()},
            "rule": "min information criterion over the feasible p; ties -> smaller p"}


def rolling_oos_v2(panel: dict, countries, years, *, weights_by_year: dict, weights_2019: dict,
                   weights_gdp: dict, groups: dict, lags=(1, 2), criterion: str = "bic",
                   max_h: int = 4, n_origins: int = 15) -> dict:
    """Rolling OOS h=1..4 with per-origin BIC/AIC lag selection and three weight variants.

    Selection is made on the training window only.  If the selected lag is unstable, the smallest
    stable lag is used instead (recorded as a fallback); if no lag is stable the origin is skipped.
    """
    if criterion not in ("aic", "bic"):
        raise GVARRealError(f"unknown lag-selection criterion {criterion!r}")
    lags = tuple(sorted({int(lag) for lag in lags}))
    series = {code: {"g": np.asarray([panel["gdp_growth"][code][year] for year in years]),
                     "pi": np.asarray([panel["inflation"][code][year] for year in years])}
              for code in countries}
    shares = group_weights(countries, years, panel, groups)
    total = len(years)
    if total < MIN_TRAIN + max_h:
        raise GVARRealError(f"sample of {total} years is too short for OOS h<= {max_h}")
    origins = list(range(max(MIN_TRAIN, total - int(n_origins)), total - 1))
    variants = {"GVAR_YEAR": lambda target_year: weights_by_year[int(target_year)],
                "GVAR_2019": lambda target_year: weights_2019,
                "GVAR_GDP": lambda target_year: weights_gdp}
    models = list(variants) + ["AR1", "RW"]
    errors = {model: {group: {variable: {h: [] for h in range(1, max_h + 1)}
                              for variable in ("g", "pi")} for group in groups}
              for model in models}
    country_errors = {model: {code: {variable: {h: [] for h in range(1, max_h + 1)}
                                     for variable in ("g", "pi")} for code in countries}
                      for model in models}
    diagnostics = {name: {"fits": 0, "skipped": 0, "unstable_selected": 0, "projected": 0,
                          "selected_counts": {}, "used_counts": {}, "per_origin": []}
                   for name in variants}
    for origin in origins:
        train_slice = slice(0, origin + 1)
        train_years = list(years[:origin + 1])
        forecast_year = int(years[origin])
        forecasts: dict = {}
        for name, getter in variants.items():
            diagnostics_entry = diagnostics[name]
            dataset = build_dataset(panel, countries, train_years, getter(forecast_year))
            try:
                selection = select_lag(dataset, lags=lags, criterion=criterion)
            except GVARRealError as exc:
                diagnostics_entry["skipped"] += 1
                diagnostics_entry["per_origin"].append({"origin_year": forecast_year,
                                                        "skipped": str(exc)})
                continue
            selected = int(selection["selected"])
            try:
                model = _checked_dataset(dataset, selected)
            except GVARRealError as exc:
                diagnostics_entry["skipped"] += 1
                diagnostics_entry["per_origin"].append({"origin_year": forecast_year,
                                                        "skipped": str(exc)})
                continue
            unstable = not model.is_stable()
            if unstable:
                diagnostics_entry["unstable_selected"] += 1
            projection = project_to_stability(model)
            if projection["applied"]:
                diagnostics_entry["projected"] += 1
            diagnostics_entry["fits"] += 1
            key = str(selected)
            diagnostics_entry["selected_counts"][key] = (
                diagnostics_entry["selected_counts"].get(key, 0) + 1)
            diagnostics_entry["used_counts"][key] = (
                diagnostics_entry["used_counts"].get(key, 0) + 1)
            diagnostics_entry["per_origin"].append({
                "origin_year": forecast_year, "selected": selected, "used": selected,
                "stable_before_projection": bool(not unstable),
                "stable": bool(model.is_stable()),
                "projection": projection,
                "criteria": {str(lag): ({"aic": value["aic"], "bic": value["bic"],
                                         "stable": value["stable"], "params": value["params"]}
                                        if "aic" in value else value)
                             for lag, value in selection["criteria"].items()}})
            path = model.forecast(steps=max_h)["path"]
            forecasts[name] = {variable: {code: path[code][variable] for code in countries}
                               for variable in ("g", "pi")}
        forecasts["AR1"] = {}
        forecasts["RW"] = {}
        for variable in ("g", "pi"):
            forecasts["AR1"][variable] = {}
            forecasts["RW"][variable] = {}
            for code in countries:
                values = series[code][variable][train_slice]
                forecasts["AR1"][variable][code] = ar1_forecast(values, max_h)
                forecasts["RW"][variable][code] = [float(values[-1])] * max_h
        for h in range(1, max_h + 1):
            if origin + h >= total:
                continue
            for group, share in shares.items():
                for variable in ("g", "pi"):
                    actual = math.fsum(weight * float(series[code][variable][origin + h])
                                       for code, weight in share.items())
                    for model in models:
                        if model not in forecasts:
                            continue
                        forecast = math.fsum(
                            weight * float(forecasts[model][variable][code][h - 1])
                            for code, weight in share.items())
                        errors[model][group][variable][h].append(forecast - actual)
                        for code in countries:
                            country_actual = float(series[code][variable][origin + h])
                            country_forecast = float(forecasts[model][variable][code][h - 1])
                            country_errors[model][code][variable][h].append(
                                country_forecast - country_actual)
    metrics = {model: {group: {variable: {h: {"rmse": _rmse(errors[model][group][variable][h]),
                                              "mae": _mae(errors[model][group][variable][h]),
                                              "n": len(errors[model][group][variable][h])}
                                       for h in range(1, max_h + 1)}
                                for variable in ("g", "pi")} for group in groups}
               for model in models}
    per_country = {model: {code: {variable: {
        h: {"rmse": _rmse(country_errors[model][code][variable][h]),
            "mae": _mae(country_errors[model][code][variable][h]),
            "n": len(country_errors[model][code][variable][h])}
        for h in range(1, max_h + 1)} for variable in ("g", "pi")}
        for code in countries} for model in models}
    rolling = {}
    for name, entry in diagnostics.items():
        rolling[name] = {"fits": entry["fits"], "skipped": entry["skipped"],
                         "unstable_selected": entry["unstable_selected"],
                         "projected": entry["projected"],
                         "selected_counts": dict(sorted(entry["selected_counts"].items())),
                         "used_counts": dict(sorted(entry["used_counts"].items())),
                         "per_origin": entry["per_origin"]}
    return {"origins": [int(years[index]) for index in origins], "origins_index": origins,
            "models": models, "criterion": criterion, "lags": list(lags),
            "metrics": metrics, "per_country": per_country, "rolling": rolling,
            "groups": {name: sorted(share) for name, share in shares.items()},
            "group_weights": {name: {code: round(weight, 12) for code, weight in share.items()}
                              for name, share in shares.items()},
            "timeline": {"min_train": MIN_TRAIN, "max_h": int(max_h),
                         "n_origins": len(origins),
                         "first_origin": int(years[origins[0]]) if origins else None,
                         "last_origin": int(years[origins[-1]]) if origins else None}}


def project_to_stability(model: GVAR, *, target_modulus: float = 0.98) -> dict:
    """Pre-registered stability projection of an estimated GVAR.

    If the estimated companion matrix has ``max|lambda| > target_modulus``, all lag coefficients
    (own and foreign, not the intercepts) are uniformly shrunk by one factor ``s`` in [0, 1], found
    by deterministic bisection so that ``max|lambda| <= target_modulus``.  The same treatment is
    applied to every model variant and every origin, and the factor is recorded.  Without it the
    annual 53-country estimates are often explosive on short rolling windows (a finding of the v2
    run) and the OOS comparison would silently drop most origins.
    """
    if not 0.0 < target_modulus < 1.0:
        raise GVARRealError("target_modulus must lie in (0, 1)")
    if not hasattr(model, "_global_companion") or not hasattr(model, "_global"):
        raise GVARRealError("the installed GVAR prototype does not expose the companion internals "
                            "needed for the stability projection")
    initial = float(model.stability()["max_modulus"])
    if initial <= target_modulus:
        return {"applied": False, "scale": 1.0,
                "max_modulus_before": round(initial, 6),
                "max_modulus_after": round(initial, 6),
                "target_modulus": target_modulus}
    original = {country: np.array(model.coefficients[country], dtype=float, copy=True)
                for country in model.countries}

    def apply(scale: float) -> float:
        for country in model.countries:
            beta = np.array(original[country], copy=True)
            beta[:, 1:] = original[country][:, 1:] * scale
            model.coefficients[country] = beta
        model._global = model._global_companion()
        return float(np.abs(np.linalg.eigvals(model._global)).max())

    low, high = 0.0, 1.0
    for _ in range(40):
        middle = 0.5 * (low + high)
        if apply(middle) <= target_modulus:
            low = middle
        else:
            high = middle
    maximum = apply(low)
    model._eigenvalues = np.linalg.eigvals(model._global)
    return {"applied": True, "scale": round(low, 12),
            "max_modulus_before": round(initial, 6),
            "max_modulus_after": round(float(maximum), 6),
            "target_modulus": target_modulus}


def _delta_label(delta: float, primary: str, other: str) -> str:
    if delta < -1e-12:
        return primary
    if delta > 1e-12:
        return other
    return "TIE"


def compare_v2(oos: dict, primary: str = "GVAR_YEAR",
               others=("GVAR_2019", "GVAR_GDP", "AR1", "RW")) -> dict:
    """Paired RMSE/MAE comparison of the year-weights model against each alternative."""
    metrics = oos["metrics"]
    per_country = oos["per_country"]
    groups = [group for group in ("US", "EA", "CN", "WORLD") if group in metrics[primary]]
    if not groups:
        raise GVARRealError("no aggregate group in the OOS metrics")
    horizons = sorted({int(h) for h in metrics[primary][groups[0]]["g"]})
    out = {"primary": primary,
           "note": ("delta_rmse = RMSE(primary) - RMSE(other); negative favours the primary "
                    "(year weights + lag selection)")}
    for other in others:
        if other not in metrics:
            continue
        cells = []
        better = worse = tie = 0
        delta_sum = 0.0
        for group in groups:
            for variable in ("g", "pi"):
                for h in horizons:
                    first = float(metrics[primary][group][variable][h]["rmse"])
                    second = float(metrics[other][group][variable][h]["rmse"])
                    delta = first - second
                    label = _delta_label(delta, primary, other)
                    better += int(label == primary)
                    worse += int(label == other)
                    tie += int(label == "TIE")
                    delta_sum += delta
                    cells.append({"group": group, "variable": variable, "h": h,
                                  "rmse_primary": round(first, 6),
                                  "rmse_other": round(second, 6),
                                  "delta_rmse": round(delta, 6), "better": label})
        per_variable = {}
        for variable in ("g", "pi"):
            for h in horizons:
                wins = losses = ties = 0
                deltas = []
                for code in sorted(per_country[primary]):
                    first = float(per_country[primary][code][variable][h]["rmse"])
                    second = float(per_country[other][code][variable][h]["rmse"])
                    if not (math.isfinite(first) and math.isfinite(second)):
                        continue
                    delta = first - second
                    deltas.append(delta)
                    if delta < -1e-12:
                        wins += 1
                    elif delta > 1e-12:
                        losses += 1
                    else:
                        ties += 1
                per_variable[f"{variable}_h{h}"] = {
                    "primary_better": wins, "other_better": losses, "ties": ties,
                    "median_delta_rmse": round(float(np.median(deltas)), 6) if deltas else None}
        out[other] = {"aggregate": {"primary_better_cells": better, "other_better_cells": worse,
                                    "ties": tie, "cells": len(cells),
                                    "mean_delta_rmse": round(delta_sum / len(cells), 6),
                                    "cells_detail": cells},
                      "per_country": per_variable}
    return out


def vs_best_baseline(oos: dict, primary: str = "GVAR_YEAR",
                     baselines=("AR1", "RW")) -> dict:
    """Does the primary model beat the best of the univariate baselines per aggregate cell?"""
    metrics = oos["metrics"]
    groups = [group for group in ("US", "EA", "CN", "WORLD") if group in metrics[primary]]
    horizons = sorted({int(h) for h in metrics[primary][groups[0]]["g"]})
    cells = []
    better = worse = tie = 0
    for group in groups:
        for variable in ("g", "pi"):
            for h in horizons:
                candidates = {name: float(metrics[name][group][variable][h]["rmse"])
                              for name in baselines if name in metrics}
                best_name = min(candidates, key=candidates.get)
                best_value = candidates[best_name]
                first = float(metrics[primary][group][variable][h]["rmse"])
                delta = first - best_value
                label = _delta_label(delta, primary, best_name)
                better += int(label == primary)
                worse += int(label == best_name)
                tie += int(label == "TIE")
                cells.append({"group": group, "variable": variable, "h": h,
                              "rmse_primary": round(first, 6),
                              "best_baseline": best_name,
                              "rmse_best_baseline": round(best_value, 6),
                              "delta_rmse": round(delta, 6), "better": label})
    return {"baselines": list(baselines), "primary_better_cells": better,
            "baseline_better_cells": worse, "ties": tie, "cells": len(cells), "detail": cells}


def _weight_checks(weights_by_year: dict, countries) -> dict:
    max_gap = 0.0
    zero_self = True
    for weights in weights_by_year.values():
        for code in countries:
            row = weights[code]
            max_gap = max(max_gap, abs(math.fsum(row.values()) - 1.0))
            zero_self = zero_self and code not in row
    return {"years": len(weights_by_year), "max_row_gap": round(max_gap, 12),
            "zero_self": bool(zero_self)}


def run_yby(*, data_root=None, wb_cache=None, weights_cache=None, refresh: bool = False,
            lags=(1, 2), criterion: str = "bic", max_h: int = 4, n_origins: int = 15,
            countries=COUNTRIES, start: int = START_YEAR, end: int = END_YEAR) -> dict:
    """Full v2 evaluation: year weights, lag selection, five-model rolling OOS."""
    root = _mrio_root(data_root)
    wb_directory = Path(wb_cache) if wb_cache is not None else root / "wb"
    cache = Path(weights_cache) if weights_cache is not None else root / "cache" / "gvar_real"
    panel = load_indicators(countries, cache_dir=wb_directory, refresh=refresh,
                            start=start, end=end)
    sample = choose_sample(panel, countries, start=start, end=end)
    kept, years = sample["countries"], sample["years"]
    vintages = discover_mrio_vintages(root, cache_dir=cache, refresh=refresh)
    by_year, by_year_source = build_weights_by_year(
        panel, kept, years, data_root=root, cache_dir=cache, refresh=refresh, vintages=vintages)
    fixed_weights, fixed_source = fixed_2019_weights(panel, kept, years, vintages=vintages)
    gdp_rows, gdp_source = gdp_weights(panel, kept, years)
    groups = {"US": ["USA"], "EA": [code for code in EURO_AREA if code in kept],
              "CN": ["CHN"], "WORLD": kept}
    oos = rolling_oos_v2(panel, kept, years, weights_by_year=by_year, weights_2019=fixed_weights,
                         weights_gdp=gdp_rows, groups=groups, lags=lags, criterion=criterion,
                         max_h=max_h, n_origins=n_origins)
    comparisons = compare_v2(oos)
    against_baseline = vs_best_baseline(oos)
    full_sample = {}
    for name, weights in (("GVAR_YEAR", by_year[int(years[-1])]), ("GVAR_2019", fixed_weights),
                          ("GVAR_GDP", gdp_rows)):
        dataset = build_dataset(panel, kept, years, weights)
        selection = select_lag(dataset, lags=lags, criterion=criterion)
        selected = int(selection["selected"])
        model = _checked_dataset(dataset, selected)
        unstable = not model.is_stable()
        projection = project_to_stability(model)
        full_sample[name] = {
            "selected_lag": selected,
            "stable_before_projection": bool(not unstable),
            "stable": bool(model.is_stable()),
            "projection": projection,
            "max_modulus": round(float(model.stability()["max_modulus"]), 6),
            "criteria": {str(lag): ({"aic": value["aic"], "bic": value["bic"],
                                     "params": value["params"], "observations": value["observations"],
                                     "stable": value["stable"], "max_modulus": value["max_modulus"]}
                                    if "aic" in value else value)
                         for lag, value in selection["criteria"].items()}}
    dataset_year = build_dataset(panel, kept, years, by_year[int(years[-1])])
    model_year = _checked_dataset(dataset_year, full_sample["GVAR_YEAR"]["selected_lag"])
    project_to_stability(model_year)
    irf = irf_report(model_year, groups, horizons=8)
    rolling_summary = {}
    for name, entry in oos["rolling"].items():
        rolling_summary[name] = {key: entry[key] for key in
                                 ("fits", "skipped", "unstable_selected", "projected",
                                  "selected_counts", "used_counts")}
    report = {
        "report_id": "AMFE-GVAR-REAL-YBY",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": ("World Bank WDI (open, CC BY 4.0); FIGARO 2019 (Eurostat re-use policy) and "
                   "WIOD 2016 (2000/2007/2014) trade tables, read-only, derived block sums; "
                   "data are never committed"),
        "indicators": dict(INDICATORS),
        "data_root": str(root),
        "sample": {"first_year": sample["first_year"], "last_year": sample["last_year"],
                   "years": sample["n_years"], "countries": sample["n_countries"],
                   "country_codes": kept},
        "vintages": [{"year": int(document["year"]), "kind": document.get("kind"),
                      "dataset_path": document.get("dataset_path"),
                      "sha256": document.get("sha256"),
                      "countries": len(document.get("countries") or [])}
                     for document in vintages],
        "weights": {"by_year": by_year_source, "fixed_2019": fixed_source,
                    "gdp_proxy": gdp_source},
        "weights_checks": _weight_checks(by_year, kept),
        "weights_summary": weights_summary(by_year[int(years[-1])], kept),
        "lag_selection": {"criterion": criterion, "lags": list(lags),
                          "full_sample": full_sample, "rolling": rolling_summary},
        "oos": oos,
        "comparisons": comparisons,
        "vs_best_baseline": against_baseline,
        "irf": irf,
        "limits": [
            "annual WDI data, at most 30 observations; rolling estimates use 16-27 years only",
            "the nearest-vintage rule can pick a FUTURE MRIO vintage for a given target year "
            "(e.g. 2005 -> 2007, 2010 -> 2014, 2015-2018 -> 2019): structural look-ahead in the "
            "weights is disclosed but not corrected",
            "trade weights are an input, not estimated; FIGARO 2019 and WIOD 2000-2014 are the only "
            "available vintages (2000-2014 are yearly WIOD tables)",
            "panel countries absent from a vintage use the GDP-share proxy (rows reported per year)",
            "no vintages / point-in-time data: GDP and CPI are revised, which flatters all models",
            "the GVAR prototype is reduced-form; identification is at most a Cholesky ordering",
            "aggregates are GDP-weighted means of member forecasts, not separate equations",
            "lag selection uses equation-by-equation BIC/AIC; the estimated companion is often "
            "explosive on 16-27 annual observations, so a pre-registered stability projection "
            "(uniform shrink of the lag coefficients to max|lambda| = 0.98) is applied and its "
            "scale is recorded per origin: OOS numbers are conditional on that projection",
            "research OOS run only: not connected to admission, registry or candidates",
        ],
    }
    report["report_sha256"] = digest({key: value for key, value in report.items()
                                      if key != "report_sha256"})
    return report


def _yby_summary(report: dict) -> dict:
    metrics = report["oos"]["metrics"]
    compact = {}
    for model in metrics:
        compact[model] = {}
        for group in ("US", "EA", "CN", "WORLD"):
            entry = {}
            for variable in ("g", "pi"):
                for h in (1, 4):
                    value = metrics[model][group][variable].get(h)
                    entry[f"{variable}_h{h}"] = ({"rmse": round(value["rmse"], 6),
                                                  "mae": round(value["mae"], 6)}
                                                 if value and math.isfinite(value["rmse"]) else None)
            compact[model][group] = entry
    comparison = {other: {"primary_better_cells": value["aggregate"]["primary_better_cells"],
                          "other_better_cells": value["aggregate"]["other_better_cells"],
                          "ties": value["aggregate"]["ties"],
                          "mean_delta_rmse": value["aggregate"]["mean_delta_rmse"]}
                  for other, value in report["comparisons"].items() if isinstance(value, dict)}
    return {"report_id": report["report_id"], "sample": report["sample"],
            "vintages": [{key: vintage[key] for key in ("year", "kind")}
                         for vintage in report["vintages"]],
            "weight_rule": report["weights"]["by_year"]["rule"],
            "lag_selection": {"criterion": report["lag_selection"]["criterion"],
                              "full_sample": {name: {"selected_lag": entry["selected_lag"],
                                                     "stable_before_projection":
                                                         entry["stable_before_projection"],
                                                     "stable": entry["stable"],
                                                     "projection": entry["projection"]}
                                               for name, entry in
                                               report["lag_selection"]["full_sample"].items()},
                              "rolling": report["lag_selection"]["rolling"]},
            "metrics": compact,
            "comparisons": comparison,
            "vs_best_baseline": {key: report["vs_best_baseline"][key]
                                 for key in ("primary_better_cells", "baseline_better_cells",
                                             "ties", "cells")},
            "report_sha256": report["report_sha256"]}


def yby_cli(args) -> int:
    """Entry point for ``--weights yby`` (v2 pipeline); keeps the legacy CLI untouched."""
    lags_list = tuple(sorted({int(part) for part in args.lags.split(",") if part.strip()}))
    if not lags_list or any(lag not in (1, 2) for lag in lags_list):
        raise SystemExit("--lags for --weights yby must be a non-empty subset of '1,2'")
    root = Path(args.data_root) if args.data_root else Path(
        os.environ.get("AMFE_MRIO_ROOT") or MRIO_ROOT)
    wb_cache = Path(args.cache_dir) if args.cache_dir else root / "wb"
    weights_cache = Path(args.weights_cache) if args.weights_cache else (
        root / "cache" / "gvar_real")
    report = run_yby(data_root=root, wb_cache=wb_cache, weights_cache=weights_cache,
                     refresh=args.refresh, lags=lags_list, criterion=args.select_lag,
                     max_h=args.max_h, n_origins=args.origins)
    path = Path(args.report) if args.report else weights_cache / "gvar_v2_report.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8", newline="\n")
    except OSError as exc:
        raise GVARRealError(f"cannot write the report {path}: {exc}") from exc
    summary = _yby_summary(report)
    summary["report"] = str(path)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
