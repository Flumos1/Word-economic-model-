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

CACHE_DIR = ROOT / "data" / "mrio" / "wb"
FIGARO_DATASET = ROOT / "data" / "mrio" / "figaro" / "figaro_2019" / "dataset.json"
REPORT_PATH = CACHE_DIR / "gvar_report.json"
WEIGHTS_MODES = ("auto", "figaro", "gdp")
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
    path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")
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
        cache.write_text(json.dumps(export_document) + "\n", encoding="utf-8")
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
                             "(GDP fallback rows for uncovered countries); gdp: proxy only")
    parser.add_argument("--figaro", default=str(FIGARO_DATASET), help="FIGARO dataset.json path")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    lags_list = tuple(sorted({int(part) for part in args.lags.split(",") if part.strip()}))
    figaro_path = Path(args.figaro)
    figaro_available = figaro_path.is_file()
    if args.weights == "figaro" and not figaro_available:
        raise SystemExit(f"FIGARO dataset not found: {figaro_path}")
    modes = ["gdp"] if args.weights == "gdp" else (
        ["figaro", "gdp"] if figaro_available else ["gdp"])
    runs = {}
    for mode in modes:
        runs[mode] = run(refresh=args.refresh, cache_dir=Path(args.cache_dir),
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
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
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


if __name__ == "__main__":
    raise SystemExit(main())
