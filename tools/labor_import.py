"""Labor / household data import for the AMFE meso-micro layer (docs/136).

Primary source: **WIOD Socio Economic Accounts (SEA) 2016** -- compensation of employees, labour
compensation, persons engaged, employees, hours worked, gross output and value added by
country x industry x year (43 countries, 56 industries, 2000-2014, local currency).  The Excel
workbook is downloaded from the official GGDC Dataverse file linked on the WIOD 2016 release page
(https://www.rug.nl/ggdc/valuechain/wiod/wiod-2016-release), cached locally with SHA-256 and the
licence recorded (WIOD 2016 release: CC BY 4.0).

Fallback: **World Bank WDI** country indicators (household consumption share of GDP, employment to
population, participation, GDP per capita) -- used only when SEA is unavailable; every WDI number is
a fetched third-party value with its retrieval metadata, never an assumption.

Outputs (git-ignored local data root, ``data/mrio/labor``)
----------------------------------------------------------
``archive/Socio_Economic_Accounts.xlsx``  raw third-party workbook (never committed)
``sea_normalized.json``                   normalized ``years -> countries -> industries -> values``
``manifest.json``                         SHA-256, bytes, licence, units, coverage, counts
``wdi_<indicator>_<year>.json``           fallback WDI responses (with metadata)

Commands
--------
    python tools/labor_import.py --probe
    python tools/labor_import.py --download
    python tools/labor_import.py --parse
    python tools/labor_import.py --fallback --year 2014
    python tools/labor_import.py --manifest
    python tools/labor_import.py --check

Fail-closed: a missing archive, an unreadable workbook, an empty variable or a missing WDI value
stops the action with ``BLOCKED_DATA`` (exit 2) instead of writing a partial dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DATA_ROOT_ENV = "AMFE_MRIO_ROOT"
DEFAULT_DATA_ROOT = ROOT / "data" / "mrio"
LABOR_DIRNAME = "labor"
STATUS_AVAILABLE = "AVAILABLE"
STATUS_BLOCKED = "BLOCKED_DATA"

#: Official GGDC Dataverse file of the WIOD 2016 SEA workbook (linked on the release page).
SEA_URL = "https://dataverse.nl/api/access/datafile/199095"
SEA_FILENAME = "Socio_Economic_Accounts.xlsx"
SEA_LICENCE = "CC BY 4.0 (WIOD 2016 release, Groningen Growth and Development Centre)"
SEA_CITATION = ("WIOD Socio Economic Accounts, November 2016 release (published February 2018); "
                "https://www.rug.nl/ggdc/valuechain/wiod/wiod-2016-release")
SEA_SHA256 = "8cf5ed3d1b7d7ddaE93037cc5e1c1d0a2721b9a7679dcc076b85ac5220c576ce"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
#: SEA variables used by the labor layer (names as in the workbook).
SEA_VARIABLES = ("COMP", "LAB", "VA", "GO", "EMP", "EMPE", "H_EMPE")
#: Documented units of the SEA variables (Notes sheet of the workbook).
SEA_UNITS = {
    "COMP": "millions of national currency, current prices",
    "LAB": "millions of national currency, current prices",
    "VA": "millions of national currency, current prices",
    "GO": "millions of national currency, current prices",
    "EMP": "thousands of persons engaged",
    "EMPE": "thousands of employees",
    "H_EMPE": "millions of hours worked by employees",
}
#: World Bank fallback indicators (only used when SEA is unavailable).
WDI_INDICATORS = {
    "NE.CON.PRVT.ZS": "household final consumption expenditure (% of GDP)",
    "SL.EMP.TOTL.SP.ZS": "employment to population ratio, 15+, total (%)",
    "SL.TLF.CACT.ZS": "labor force participation rate, total (% of population 15+)",
    "NY.GDP.PCAP.CD": "GDP per capita (current US$)",
}
WDI_API = ("https://api.worldbank.org/v2/country/all/indicator/{indicator}"
           "?format=json&per_page=20000&date={year}")
WDI_LICENCE = "World Bank Open Data (CC BY 4.0), indicator NY.GDP.MKTP.CD family"


class LaborImportError(RuntimeError):
    """Fail-closed error of the labor data import."""


def utc_now() -> str:
    override = os.environ.get("AMFE_GENERATED_AT", "").strip()
    return override or datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_root(root=None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(env) if env else DEFAULT_DATA_ROOT


def labor_dir(root=None) -> Path:
    return data_root(root) / LABOR_DIRNAME


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(document, path: Path) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True)
               + "\n").encode("utf-8")
    path.write_bytes(payload)
    return {"path": str(path), "bytes": len(payload), "sha256": _sha256_bytes(payload)}


# ---------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------
def probe(url: str = SEA_URL, timeout: float = 30.0) -> dict:
    """HEAD/GET probe: records the observed status, size and content type (no invention)."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "url": url, "reachable": True, "http_status": getattr(response, "status", None),
                "bytes": response.headers.get("Content-Length"),
                "content_type": response.headers.get("Content-Type"),
                "location": response.headers.get("Location"),
            }
    except urllib.error.HTTPError as error:
        return {"url": url, "reachable": False, "http_status": error.code,
                "error": f"HTTPError {error.code}: {error.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"url": url, "reachable": False, "http_status": None,
                "error": f"{type(error).__name__}: {error}"}


def download(url: str = SEA_URL, target: Path | None = None,
             expected_sha256: str | None = None, timeout: float = 600.0) -> dict:
    """Stream the workbook to ``target``, verify SHA-256 and return the file facts."""
    target = Path(target) if target is not None else labor_dir() / "archive" / SEA_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": BROWSER_UA})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                target.open("wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise LaborImportError(f"{STATUS_BLOCKED}: download failed: "
                               f"{type(error).__name__}: {error}") from error
    actual = digest.hexdigest()
    if expected_sha256 and actual != str(expected_sha256).lower():
        target.unlink(missing_ok=True)
        raise LaborImportError(f"{STATUS_BLOCKED}: SHA-256 mismatch: expected "
                               f"{expected_sha256}, got {actual}")
    return {"path": str(target), "bytes": total, "sha256": actual, "url": url}


# ---------------------------------------------------------------------------------------
# SEA parsing / normalisation
# ---------------------------------------------------------------------------------------
def _year_columns(columns) -> list[int]:
    years = []
    for column in columns:
        text = str(column).strip()
        if text.isdigit() and 1900 <= int(text) <= 2100:
            years.append(int(text))
    return sorted(years)


def normalize_sea_rows(rows, variables=SEA_VARIABLES) -> dict:
    """Turn long ``(country, variable, code, year, value)`` rows into the normalized structure.

    ``rows`` is an iterable of mappings with keys ``country``, ``variable``, ``code`` and one key
    per year (``int`` or ``str``) holding the value.  Pure function: no pandas, no I/O -- the
    workbook reader only prepares the rows, so the normalisation is unit-testable.
    """
    wanted = set(variables)
    document: dict = {"years": [], "countries": [], "industries": [], "values": {}}
    countries, industries, years = set(), set(), set()
    for row in rows:
        variable = str(row.get("variable", "")).strip()
        if variable not in wanted:
            continue
        country = str(row.get("country", "")).strip()
        code = str(row.get("code", "")).strip()
        if not country or not code:
            raise LaborImportError(f"SEA row without country/code: {row!r}")
        countries.add(country)
        industries.add(code)
        for key, value in row.items():
            if key in ("country", "variable", "code", "description"):
                continue
            if not str(key).strip().isdigit():
                continue
            year = int(key)
            number = _number(value)
            if number is None:
                continue
            years.add(year)
            cell = document["values"].setdefault(str(year), {}).setdefault(
                country, {}).setdefault(code, {})
            if variable in cell:
                raise LaborImportError(
                    f"duplicate SEA cell {country}/{code}/{year}/{variable}")
            cell[variable] = number
    if not document["values"]:
        raise LaborImportError(f"{STATUS_BLOCKED}: SEA workbook has no values for {sorted(wanted)}")
    document["years"] = sorted(years)
    document["countries"] = sorted(countries)
    document["industries"] = sorted(industries)
    document["units"] = dict(SEA_UNITS)
    document["variables"] = sorted(wanted)
    return document


def _number(value):
    """Float value or ``None`` for empty/NaN cells (SEA uses empty cells for missing)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_sea_workbook(path: Path, variables=SEA_VARIABLES) -> dict:
    """Read the SEA workbook (sheet ``DATA``) and normalise it; requires pandas."""
    path = Path(path)
    if not path.is_file():
        raise LaborImportError(f"{STATUS_BLOCKED}: SEA workbook not found: {path}")
    try:
        import pandas as pd
    except ImportError as error:  # pragma: no cover - the bundled runtime has pandas
        raise LaborImportError(
            f"{STATUS_BLOCKED}: pandas is required to parse the SEA workbook; use the bundled "
            f"runtime or install pandas ({error})") from error
    frame = pd.read_excel(path, sheet_name="DATA")
    frame.columns = [int(c) if str(c).strip().isdigit() else c for c in frame.columns]
    years = _year_columns(frame.columns)
    if not years:
        raise LaborImportError(f"{STATUS_BLOCKED}: no year columns in {path}")
    keep = frame[frame["variable"].isin(variables)]
    if keep.empty:
        raise LaborImportError(f"{STATUS_BLOCKED}: no SEA variables {sorted(variables)} in {path}")
    rows = keep.to_dict(orient="records")
    document = normalize_sea_rows(rows, variables=variables)
    document["source"] = {
        "name": "WIOD Socio Economic Accounts 2016",
        "citation": SEA_CITATION,
        "url": SEA_URL,
        "licence": SEA_LICENCE,
        "licence_verified": False,
        "retrieved_at": utc_now(),
        "file": str(path),
        "file_bytes": path.stat().st_size,
        "file_sha256": sha256_file(path),
        "note": "values are third-party data in millions of national currency: ratios (labour "
                "share) are currency-free, but values per USD output need the IO table (USD_mn)",
    }
    return document


def build_manifest(document: dict) -> dict:
    """Aggregate-only manifest: calendar coverage, country/industry counts, hashes, units."""
    values = document["values"]
    cells = sum(len(industries) for year in values.values()
                for industries in year.values())
    variables = sum(len(industry_values)
                    for year in values.values()
                    for country in year.values()
                    for industry_values in country.values())
    return {
        "schema": "amfe.layouts.labor.manifest.v1",
        "generated_at": utc_now(),
        "status": STATUS_AVAILABLE,
        "source": document.get("source", {}),
        "years": document["years"],
        "countries": len(document["countries"]),
        "industries": len(document["industries"]),
        "variables": document["variables"],
        "units": document["units"],
        "country_codes": document["countries"],
        "industry_codes": document["industries"],
        "cells": {"country_industry_year": cells, "variable_cells": variables},
        "normalized": {"sha256": _sha256_bytes(
            (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))},
        "licence": SEA_LICENCE,
        "notes": "aggregate-only manifest; no raw values are copied here",
    }


# ---------------------------------------------------------------------------------------
# World Bank WDI fallback
# ---------------------------------------------------------------------------------------
def fetch_wdi(indicator: str, year: int, timeout: float = 180.0) -> dict:
    """Fetch one WDI indicator for one year; returns a cached document (values + metadata)."""
    if indicator not in WDI_INDICATORS:
        raise LaborImportError(f"unsupported WDI indicator {indicator!r}")
    url = WDI_API.format(indicator=indicator, year=int(year))
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise LaborImportError(f"{STATUS_BLOCKED}: WDI {indicator} {year} fetch failed: "
                               f"{type(error).__name__}: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LaborImportError(f"{STATUS_BLOCKED}: WDI response is not JSON: {error}") from error
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise LaborImportError(f"{STATUS_BLOCKED}: unexpected WDI response for {indicator}")
    values = {}
    for item in payload[1]:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        code = str((item.get("country") or {}).get("id", "")).upper()
        if code:
            values[code] = float(item["value"])
    if not values:
        raise LaborImportError(f"{STATUS_BLOCKED}: WDI {indicator} {year} returned no values")
    return {
        "indicator": indicator,
        "description": WDI_INDICATORS[indicator],
        "year": int(year),
        "url": url,
        "licence": WDI_LICENCE,
        "retrieved_at": utc_now(),
        "values": values,
    }


def fetch_wdi_fallback(year: int, root=None, indicators=None) -> dict:
    """Fetch every fallback indicator for one year and write one document per indicator."""
    directory = labor_dir(root) / "wdi"
    documents = {}
    for indicator in (indicators or WDI_INDICATORS):
        document = fetch_wdi(indicator, year)
        written = write_json(document, directory / f"wdi_{indicator.replace('.', '_')}_{year}.json")
        documents[indicator] = {"path": written["path"], "sha256": written["sha256"],
                                "countries": len(document["values"])}
    return {"action": "fallback", "year": int(year), "indicators": documents,
            "status": STATUS_AVAILABLE}


# ---------------------------------------------------------------------------------------
# Status / CLI
# ---------------------------------------------------------------------------------------
def check(root=None) -> dict:
    """Local status: SEA archive/normalized/manifest facts (hashes), WDI fallback files."""
    directory = labor_dir(root)
    status = {"action": "check", "labor_dir": str(directory), "status": STATUS_BLOCKED,
              "reason": None, "sea": None, "wdi": [], "manifest": None}
    archive = directory / "archive" / SEA_FILENAME
    if archive.is_file():
        status["sea"] = {"path": str(archive), "bytes": archive.stat().st_size,
                         "sha256": sha256_file(archive)}
    normalized = directory / "sea_normalized.json"
    if normalized.is_file():
        try:
            document = json.loads(normalized.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            status["reason"] = f"sea_normalized.json unreadable: {error}"
        else:
            status["status"] = STATUS_AVAILABLE
            status["normalized"] = {
                "path": str(normalized), "bytes": normalized.stat().st_size,
                "sha256": sha256_file(normalized),
                "years": document.get("years"), "countries": len(document.get("countries", [])),
                "industries": len(document.get("industries", [])),
                "variables": document.get("variables"),
            }
    if status["status"] != STATUS_AVAILABLE and status["reason"] is None:
        status["reason"] = (f"{STATUS_BLOCKED}: run --download and --parse first "
                            f"({directory / 'sea_normalized.json'})")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        try:
            status["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status["manifest"] = None
    wdi_dir = directory / "wdi"
    if wdi_dir.is_dir():
        for path in sorted(wdi_dir.glob("wdi_*.json")):
            status["wdi"].append({"path": str(path), "bytes": path.stat().st_size,
                                  "sha256": sha256_file(path)})
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Labor data import (WIOD SEA 2016 / WDI fallback)")
    parser.add_argument("--root", default=None, help="MRIO data root (default $AMFE_MRIO_ROOT)")
    parser.add_argument("--probe", action="store_true", help="HEAD probe of the SEA file")
    parser.add_argument("--download", action="store_true", help="download the SEA workbook")
    parser.add_argument("--parse", action="store_true", help="parse SEA into sea_normalized.json")
    parser.add_argument("--fallback", action="store_true", help="World Bank WDI fallback")
    parser.add_argument("--year", type=int, default=2014, help="fallback year (default 2014)")
    parser.add_argument("--manifest", action="store_true", help="write the aggregate manifest")
    parser.add_argument("--check", action="store_true", help="local status (hashes, coverage)")
    parser.add_argument("--force", action="store_true", help="re-download an existing archive")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    directory = labor_dir(args.root)
    output: dict = {"action": None, "labor_dir": str(directory), "generated_at": utc_now()}
    try:
        if args.probe:
            output["action"] = "probe"
            output["probe"] = probe()
        if args.download:
            archive = directory / "archive" / SEA_FILENAME
            if archive.is_file() and not args.force:
                output["download"] = {"path": str(archive), "bytes": archive.stat().st_size,
                                      "sha256": sha256_file(archive), "skipped": "already present"}
            else:
                output["download"] = download(target=archive, expected_sha256=SEA_SHA256)
            output["action"] = "download"
        if args.parse:
            document = read_sea_workbook(directory / "archive" / SEA_FILENAME)
            written = write_json(document, directory / "sea_normalized.json")
            output["action"] = "parse"
            output["parsed"] = {**written, "years": document["years"],
                                "countries": len(document["countries"]),
                                "industries": len(document["industries"])}
            output["status"] = STATUS_AVAILABLE
        if args.fallback:
            output["fallback"] = fetch_wdi_fallback(args.year, args.root)
            output["action"] = "fallback"
        if args.manifest or args.parse:
            path = directory / "sea_normalized.json"
            if not path.is_file():
                raise LaborImportError(f"{STATUS_BLOCKED}: nothing to manifest: {path} not found")
            document = json.loads(path.read_text(encoding="utf-8"))
            manifest = build_manifest(document)
            written = write_json(manifest, directory / "manifest.json")
            output["manifest"] = written
            if output.get("action") is None:
                output["action"] = "manifest"
        if args.check:
            output["action"] = "check"
            output["check"] = check(args.root)
        if output["action"] is None:
            build_parser().print_help()
            return 0
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=1, sort_keys=True))
        else:
            _print_summary(output)
        return 0
    except LaborImportError as error:
        blocked = {"action": output.get("action") or "blocked", "status": STATUS_BLOCKED,
                   "reason": str(error), "labor_dir": str(directory),
                   "generated_at": utc_now()}
        write_json(blocked, directory / "blocked.json")
        print(json.dumps(blocked, ensure_ascii=False, indent=1) if args.json else str(error))
        return 2


def _print_summary(output: dict) -> None:
    print(f"labor import: {output.get('action')}  dir={output.get('labor_dir')}")
    for key in ("probe", "download", "parsed", "manifest", "fallback"):
        if output.get(key):
            print(f"  {key}: {json.dumps(output[key], ensure_ascii=False, sort_keys=True)[:300]}")
    status = output.get("check")
    if status:
        print(f"  status={status.get('status')} reason={status.get('reason')}")
        if status.get("sea"):
            print(f"  SEA archive: {status['sea']['bytes']} bytes sha256 {status['sea']['sha256']}")
        if status.get("normalized"):
            info = status["normalized"]
            print(f"  normalized: years={info['years']} countries={info['countries']} "
                  f"industries={info['industries']}")
        print(f"  WDI files: {len(status.get('wdi') or [])}")


if __name__ == "__main__":
    raise SystemExit(main())
