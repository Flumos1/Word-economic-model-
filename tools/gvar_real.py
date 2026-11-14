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
REPORT_PATH = CACHE_DIR / "gvar_report.json"
MRIO_CANDIDATES = (ROOT / "data" / "mrio" / "figaro" / "dataset.json",
                   ROOT / "data" / "mrio" / "dataset.json")
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


def mrio_weights(path: Path, countries) -> dict | None:
    """Read trade weights from an MRIO dataset if the file exists (read-only, never written)."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = document.get("trade_weights") if isinstance(document, dict) else None
    if not isinstance(raw, dict):
        return None
    known = set(countries)
    out = {}
    for code in countries:
        row = raw.get(code)
        if not isinstance(row, dict):
            return None
        clean = {str(other): float(value) for other, value in row.items()
                 if str(other) in known and str(other) != code and float(value) > 0.0}
        total = math.fsum(clean.values())
        if total <= 0.0:
            return None
        out[code] = {other: value / total for other, value in clean.items()}
    return out


def build_weights(panel: dict, countries, years, *, mrio_paths=MRIO_CANDIDATES) -> tuple[dict, dict]:
    for candidate in mrio_paths:
        weights = mrio_weights(candidate, countries)
        if weights is not None:
            return weights, {"method": "MRIO_TRADE_WEIGHTS", "source": str(candidate)}
    return gdp_weights(panel, countries, years)


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
        mrio_paths=MRIO_CANDIDATES) -> dict:
    panel = load_indicators(countries, cache_dir=cache_dir, refresh=refresh, start=start, end=end)
    sample = choose_sample(panel, countries, start=start, end=end)
    kept, years = sample["countries"], sample["years"]
    weights, weight_source = build_weights(panel, kept, years, mrio_paths=mrio_paths)
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
        "fits": fits,
        "irf": irf,
        "oos": oos,
        "limits": [
            "World Bank API is not bilateral: default weights are a GDP-share proxy, not trade",
            "annual WDI data, 30 observations at most; short samples weaken the VARX estimates",
            "no vintages / point-in-time data: GDP and CPI are revised",
            "the GVAR prototype is a reduced-form model; no identification beyond Cholesky",
            "aggregates are GDP-weighted means of member forecasts, not a separate model",
        ],
    }
    return report


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
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--out-summary", action="store_true", help="print only the compact summary")
    args = parser.parse_args(argv)
    lags_list = tuple(sorted({int(part) for part in args.lags.split(",") if part.strip()}))
    report = run(refresh=args.refresh, cache_dir=Path(args.cache_dir), lags_list=lags_list,
                 max_h=args.max_h, n_origins=args.origins)
    report["report_sha256"] = digest({k: v for k, v in report.items() if k != "report_sha256"})
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    summary = {"sample": report["sample"], "fits": report["fits"],
               "weights": report["weights"]["source"],
               "irf": report["irf"]["response"].get("WORLD", {}).get("g"),
               "metrics": _summary(report), "report": str(path),
               "report_sha256": report["report_sha256"]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
