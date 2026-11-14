#!/usr/bin/env python3
"""Full-world MRIO acquisition: direct-URL probe, resumable download, WIOD 2016 converter.

Task P2 (docs/126): try to obtain a *full-world* multi-regional input-output table without
inventing data.  The tool never fabricates values: every fact it writes is either an HTTP status
observed by this process, a file size/hash of a downloaded file, a count, or a value parsed from
the downloaded third-party archive and written to the git-ignored local data root.

Commands
--------
    python tools/alt_mrio_import.py --probe                    # HEAD/Range on every known URL
    python tools/alt_mrio_import.py --download wiod_2016       # resumable GET + sha256/md5
    python tools/alt_mrio_import.py --inspect wiod_2016        # archive members/sizes (no values)
    python tools/alt_mrio_import.py --convert wiod_2016 --years 2014 [--years 2000 --jobs 2]
    python tools/alt_mrio_import.py --check                    # local dataset + manifest status
    python tools/alt_mrio_import.py --all                      # probe -> download -> convert -> manifest
    python tools/alt_mrio_import.py --report                   # BLOCKED/readiness report (JSON)

Outputs (never committed; ``data/mrio/`` is local)
-------------------------------------------------
``data/mrio/<source>/archive/``   downloaded archives (size + SHA-256 recorded in the report)
``data/mrio/<source>/dataset_<year>.json``  canonical ``amfe.mrio.dataset.v1`` (values; local only)
``data/mrio/<source>/manifest.json``        aggregate-only manifest (counts, hashes, checks)
``data/mrio/reports/probe.json``, ``blocked.json``  network facts / BLOCKED report (no values)

The converter is deliberately fail-closed: a missing archive, an unknown table layout, a unit
mismatch, a negative intermediate flow or a violated accounting identity stops the conversion
with a named reason instead of writing a silently truncated dataset.

Pandas is required only for parsing the WIOD archives (Stata/Excel); the probe/download/report
paths use the standard library only.  On this machine the bundled runtime provides pandas 3.x
(see docs/126 §5); ``AMFE_PYTHON`` may point at an interpreter that has it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_DATA_ROOT = ROOT / "data" / "mrio"

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
#: Verified on 2026-11-14 (docs/126): the WIOD 2016 release page links these Dataverse files.
SOURCES: dict[str, dict] = {
    "wiod_2016": {
        "source_id": "wiod_2016",
        "name": "WIOD 2016 release, World Input-Output Tables (current prices, USD million)",
        "provider": "University of Groningen / GGDC, hosted on DataverseNL",
        "page": "https://www.rug.nl/ggdc/valuechain/wiod/wiod-2016-release",
        "doi": "10.34894/PJ2M1C",
        "licence": "CC BY 4.0",
        "licence_verified": True,
        "countries": "43 (28 EU + 15 other major) + RoW",
        "industries": "56 (ISIC Rev. 4, 2016 release)",
        "years": "2000-2014",
        "units": "USD_mn",
        "status": "AVAILABLE_MANUAL",
        "archive": "https://dataverse.nl/api/access/datafile/199103",
        "archive_name": "WIOTS_in_STATA.zip",
        "archive_bytes": 638963496,
        "archive_md5": "cde6085d4233fec2356877aedf21d950de912b78",
        "notes": "Stata archive is the smallest of the three WIOT formats; Excel is 919 MB. "
                 "No API: files are direct Dataverse access links (HTTP 200/206 verified).",
    },
    "oecd_icio": {
        "source_id": "oecd_icio",
        "name": "OECD Inter-Country Input-Output tables (ICIO), 2025 release",
        "provider": "OECD",
        "page": "https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html",
        "doi": None,
        "licence": "OECD Terms and Conditions (not verified automatically)",
        "licence_verified": False,
        "countries": "60+ economies + RoW (per release documentation)",
        "industries": "45 (per release documentation)",
        "years": "1995-2022",
        "units": "USD_mn (current prices)",
        "status": "BLOCKED_AUTOMATED",
        "archive": None,
        "archive_name": None,
        "archive_bytes": None,
        "archive_md5": None,
        "urls": [
            "https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html",
            "https://stats.oecd.org/Index.aspx?DataSetCode=ICIO",
            "https://sdmx.oecd.org/public/rest/dataflow/OECD.SDD.NAD?format=jsondata",
            "https://sdmx.oecd.org/public/rest/data/OECD.SDD.NAD,DSD_ICIO@DF_ICIO,1.0/all?format=csvfile",
        ],
        "notes": "Anti-bot 403 for non-browser clients; stats.oecd.org redirects to the Data "
                 "Explorer; the ICIO dataflow is not exposed in the OECD SDMX API. Manual browser "
                 "download only (docs/126 §3).",
    },
    "eora_mrio": {
        "source_id": "eora_mrio",
        "name": "Eora global MRIO database",
        "provider": "Eora / University of Sydney",
        "page": "https://www.worldmrio.com/",
        "doi": None,
        "licence": "free academic licence (registration); commercial licence by purchase",
        "licence_verified": True,
        "countries": "190",
        "industries": "15 909 sectors (aggregate)",
        "years": "1990-2022",
        "units": "USD (unit base to be confirmed from the released documentation)",
        "status": "BLOCKED_LICENCE",
        "archive": None,
        "archive_name": None,
        "archive_bytes": None,
        "archive_md5": None,
        "urls": ["https://www.worldmrio.com/", "https://www.worldmrio.com/simplified/"],
        "notes": "No direct file links on the public pages (HTTP 200, pages do not expose "
                 "downloads); download requires an academic e-mail registration.",
    },
    "un_comtrade": {
        "source_id": "un_comtrade",
        "name": "UN Comtrade public preview API (trade values, not an MRIO)",
        "provider": "United Nations Statistics Division",
        "page": "https://comtradeapi.un.org/",
        "doi": None,
        "licence": "UN Comtrade terms (verify before use)",
        "licence_verified": False,
        "countries": "all reporters (bilateral goods trade)",
        "industries": "none (HS commodity codes; needs an HS->industry bridge)",
        "years": "varies by reporter",
        "units": "USD -> USD_mn",
        "status": "AVAILABLE_API",
        "archive": None,
        "archive_name": None,
        "archive_bytes": None,
        "archive_md5": None,
        "urls": ["https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=842"
                 "&period=2022&cmdCode=TOTAL&flowCode=M"],
        "notes": "Not an MRIO source: only a trade-layer input for later assembly; never a "
                 "substitute for Z/VA/final demand.",
    },
}

WIOD_COUNTRIES = (
    "AUS", "AUT", "BEL", "BGR", "BRA", "CAN", "CHE", "CHN", "CYP", "CZE", "DEU", "DNK", "ESP",
    "EST", "FIN", "FRA", "GBR", "GRC", "HRV", "HUN", "IDN", "IND", "IRL", "ITA", "JPN", "KOR",
    "LTU", "LUX", "LVA", "MEX", "MLT", "NLD", "NOR", "POL", "PRT", "ROU", "RUS", "SVK", "SVN",
    "SWE", "TUR", "TWN", "USA", "ROW",
)
#: WIOD 2016 final-demand columns (current prices WIOT) and the total-output column.
WIOD_FD_COLUMNS = ("CONS_hh", "CONS_np", "CONS_gov", "GFCF", "INV", "EXP")
WIOD_TOT_COLUMN = "TOT"
SECTOR_CODE = re.compile(r"^[A-Z]{3}[A-Z0-9]{1,3}$")


class ImportBlocked(RuntimeError):
    """Acquisition or conversion is blocked; the message is the exact BLOCKED reason."""


# --------------------------------------------------------------------------- hashing / probes
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_url(url: str, timeout: float = 20.0, range_bytes: int = 2048) -> dict:
    """HEAD first, then a bounded Range GET; record the observable facts (no invention)."""
    result = {"url": url, "checked_at": utc_now(), "browser_ua": True, "method": None,
              "http_status": None, "reachable": False, "content_type": None,
              "content_length": None, "content_range": None, "total_bytes": None,
              "final_url": None, "error": None, "elapsed_ms": None}
    started = time.monotonic()
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    try:
        request = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result.update({"method": "HEAD", "http_status": response.status,
                               "content_type": response.headers.get("Content-Type"),
                               "content_length": _int_or_none(response.headers.get(
                                   "Content-Length")),
                               "final_url": response.geturl()})
        except urllib.error.HTTPError as head_error:
            if head_error.code not in (403, 405, 501):
                raise
            range_headers = dict(headers)
            range_headers["Range"] = f"bytes=0-{range_bytes - 1}"
            request = urllib.request.Request(url, headers=range_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                chunk = response.read(range_bytes)
                content_range = response.headers.get("Content-Range")
                result.update({"method": "GET+Range", "http_status": response.status,
                               "content_type": response.headers.get("Content-Type"),
                               "content_length": len(chunk), "content_range": content_range,
                               "total_bytes": _total_from_range(content_range),
                               "final_url": response.geturl()})
        result["reachable"] = 200 <= int(result["http_status"] or 0) < 400
    except urllib.error.HTTPError as error:
        result.update({"http_status": error.code, "error": f"HTTPError {error.code}"})
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return result


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _total_from_range(content_range: str | None):
    match = re.search(r"/(\d+)$", content_range or "")
    return int(match.group(1)) if match else None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def probe_sources(sources=None, timeout: float = 20.0) -> list[dict]:
    """Probe every known direct URL of the requested sources (deterministic order)."""
    out = []
    for source_id, entry in SOURCES.items():
        if sources and source_id not in sources:
            continue
        urls = [entry["page"]]
        urls.extend(entry.get("urls") or [])
        if entry.get("archive"):
            urls.append(entry["archive"])
        seen = set()
        probes = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            probes.append(probe_url(url, timeout=timeout))
        out.append({"source_id": source_id, "status_recorded": entry["status"],
                    "probes": probes})
    return out


# ------------------------------------------------------------------------------- download
def download(url: str, target: Path, expected_bytes: int | None = None,
             expected_md5: str | None = None, timeout: float = 120.0,
             chunk_size: int = 1 << 20, progress=None) -> dict:
    """Resumable download to ``target`` (``.part`` + atomic rename), then verify size/md5."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    resumed_from = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if resumed_from:
        headers["Range"] = f"bytes={resumed_from}-"
    started = time.monotonic()
    written = resumed_from
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=timeout) as response:
        mode = "ab" if resumed_from and response.status == 206 else "wb"
        if mode == "wb":
            resumed_from = 0
            written = 0
        with part.open(mode) as handle:
            while True:
                block = response.read(chunk_size)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if progress is not None:
                    progress(written)
    part.replace(target)
    size = target.stat().st_size
    if expected_bytes is not None and size != int(expected_bytes):
        raise ImportBlocked(f"size mismatch for {target.name}: {size} != {expected_bytes}")
    digest = sha256_file(target)
    checksum = md5_file(target)
    if expected_md5 is not None and checksum.lower() != str(expected_md5).lower():
        raise ImportBlocked(f"md5 mismatch for {target.name}: {checksum} != {expected_md5}")
    return {"path": str(target), "bytes": size, "sha256": digest, "md5": checksum,
            "resumed_from": resumed_from,
            "elapsed_s": round(time.monotonic() - started, 1), "url": url}


def archive_path(source_id: str, root: Path | None = None) -> Path:
    entry = SOURCES[source_id]
    if not entry.get("archive"):
        raise ImportBlocked(f"{source_id}: no direct archive URL (status {entry['status']})")
    return mrio_data_root(root) / source_id / "archive" / entry["archive_name"]


def mrio_data_root(root=None) -> Path:
    path = Path(root) if root is not None else Path(os.environ.get("AMFE_MRIO_ROOT",
                                                                  DEFAULT_DATA_ROOT))
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------------- archive inspect
def inspect_archive(path: Path) -> dict:
    """List members/sizes of a zip archive (metadata only, no member content)."""
    path = Path(path)
    if not path.is_file():
        raise ImportBlocked(f"archive not found: {path}")
    members = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            members.append({"name": info.filename, "bytes": info.file_size,
                            "compressed_bytes": info.compress_size,
                            "is_dir": info.is_dir()})
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path),
            "members": members}


def select_member(members: list[dict], year: int) -> str:
    """Pick the archive member that holds one year of the WIOT (explicit, no guessing)."""
    candidates = [m for m in members if not m["is_dir"] and m["name"].lower().endswith(
        (".dta", ".csv", ".xlsx"))]
    if not candidates:
        raise ImportBlocked("archive contains no .dta/.csv/.xlsx member")
    with_year = [m for m in candidates if str(year) in Path(m["name"]).stem]
    if len(with_year) == 1:
        return with_year[0]["name"]
    if len(candidates) == 1:
        return candidates[0]["name"]
    names = ", ".join(Path(m["name"]).name for m in candidates)
    raise ImportBlocked(f"cannot choose the member for {year} unambiguously: {names}")


# ------------------------------------------------------------------------------ parsing
def _need_pandas():
    try:
        import pandas  # noqa: F401
    except ImportError as error:  # pragma: no cover - environment dependent
        raise ImportBlocked(
            "pandas is required to parse the WIOD Stata/Excel archives; run the converter with an "
            "interpreter that has pandas (the bundled DSH runtime does) or install pandas") from error


def _numeric_like(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def find_label_columns(frame) -> dict:
    """Locate the row-label columns and the value columns of a WIOT-shaped frame.

    The 2016 Stata WIOT uses ``Country`` and ``IndustryCode`` label columns, ``v<ISO3><n>``
    value columns with ``n = 1..56`` (sectors) and ``n = 57..61`` (final-demand categories), and
    a ``TOT`` total-output column.  The detector is layout-explicit and raises instead of guessing.
    """
    columns = [str(name) for name in frame.columns]
    for required in ("Country", "IndustryCode", "Year", "TOT"):
        if required not in columns:
            raise ImportBlocked(f"WIOT label column {required!r} not found among {columns[:8]}")
    return {"columns": columns, "country_column": "Country", "industry_column": "IndustryCode",
            "year_column": "Year", "tot_column": "TOT"}


def wiod_layout(frame, countries: tuple[str, ...] | None = None) -> dict:
    """The WIOT sector/final-demand columns in file order (raises if the layout differs)."""
    columns = [str(name) for name in frame.columns]
    value_columns = [name for name in columns if name.startswith("v") and re.fullmatch(
        r"v[A-Z]{3}\d{1,2}", name)]
    country_codes = []
    for name in value_columns:
        code = name[1:4]
        if code not in country_codes:
            country_codes.append(code)
    if countries is not None and tuple(country_codes) != tuple(countries):
        raise ImportBlocked(f"WIOD country order differs from the expected list: {country_codes}")
    sectors_per_country = {}
    for name in value_columns:
        code, number = name[1:4], int(name[4:])
        sectors_per_country.setdefault(code, []).append(number)
    counts = {len(numbers) for numbers in sectors_per_country.values()}
    if len(counts) != 1:
        raise ImportBlocked("WIOT value columns are not complete per country")
    per_country = counts.pop()
    if per_country < 7:
        raise ImportBlocked(f"WIOT layout with {per_country} value columns per country is too "
                            "small (sectors + 5 final-demand categories expected)")
    sector_count = per_country - 5
    sector_columns = [f"v{code}{n}" for code in country_codes
                      for n in range(1, sector_count + 1)]
    fd_columns = [f"v{code}{n}" for code in country_codes
                  for n in range(sector_count + 1, per_country + 1)]
    missing = [name for name in sector_columns + fd_columns if name not in columns]
    if missing:
        raise ImportBlocked(f"WIOT value columns missing: {missing[:5]}")
    return {"country_codes": country_codes, "sector_columns": sector_columns,
            "fd_columns": fd_columns, "tot_column": "TOT"}


def row_keys(frame, label_columns: list) -> tuple[list[str], list[str]]:
    """(country, industry) keys of the WIOT data rows, excluding the total block."""
    countries, industries = [], []
    for _, row in frame.iterrows():
        country = str(row[label_columns[0]]).strip()
        industry = str(row[label_columns[1]]).strip()
        if country.upper() in ("TOT", "TOTAL") or industry.upper() in ("TOT", "TOTAL", "VA", "GO"):
            continue
        countries.append(country)
        industries.append(industry)
    return countries, industries


def _split_signed(values):
    """Split a raw vector into ``(non-negative base, signed adjustment)``.

    ``base = max(value, 0)`` and ``adjustment = min(value, 0)`` so that
    ``base + adjustment == value`` exactly.  Schema 1.1 keeps ``y``/``va`` non-negative and moves
    the negative part (WIOD inventory changes, negative value added) into the signed adjustments.
    """
    base = [value if value > 0.0 else 0.0 for value in values]
    adjustment = [value if value < 0.0 else 0.0 for value in values]
    return base, adjustment


def build_dataset(table: dict, *, year: int, source: dict, tolerance: float = 1e-6):
    """Map a parsed WIOD table dict to the canonical ``amfe.mrio.dataset.v1.1`` dataset.

    ``table`` carries plain lists: ``row_keys`` [(country, industry)...], ``sector_columns``,
    ``Z`` (rows x sector columns), ``y`` (final demand sum per row), ``x`` (TOT column),
    ``va`` (value-added row over sector columns) and ``go`` (gross-output row, cross-check).

    Negative final demand / value added are not discarded and not clipped silently: they become
    signed adjustments (``final_demand_adjustments``, ``va_adjustments``), so the accounting
    identities stay exact:

        x = Z 1 + y + fda,   x = 1' Z + va + vaa,   sum(va) + sum(vaa) = sum(y) + sum(fda)

    The WIOD value-added row does not close every column exactly (the release keeps valuation /
    transport-margin adjustment rows outside ``VA``); that column residual is absorbed into
    ``va_adjustments`` as well and reported in ``source['adjustments']``, so no residual is hidden.
    """
    from amfe.mrio.schema import MRIODataset, SCHEMA_VERSION

    keys = table["row_keys"]
    if not keys:
        raise ImportBlocked("parsed WIOD table has no data rows")
    countries = []
    for country, _ in keys:
        if country not in countries:
            countries.append(country)
    industries = []
    for _, industry in keys:
        if industry not in industries:
            industries.append(industry)
    if len(countries) * len(industries) != len(keys):
        raise ImportBlocked("WIOD rows are not a complete country x industry grid")
    if len(keys) != len(table["Z"]):
        raise ImportBlocked("Z row count differs from the label count")
    if len(table["sector_columns"]) != len(keys):
        raise ImportBlocked("sector column count differs from the label count")
    size = len(keys)
    Z = [[float(value) for value in row] for row in table["Z"]]
    if any(len(row) != size for row in Z):
        raise ImportBlocked("Z is not square")
    y_raw = [float(value) for value in table["y"]]
    va_raw = [float(value) for value in table["va"]]
    x = [float(value) for value in table["x"]]
    if not (len(y_raw) == len(va_raw) == len(x) == size):
        raise ImportBlocked("y/va/x length differs from the label count")

    y, fda = _split_signed(y_raw)
    va, vaa = _split_signed(va_raw)
    column_residual = []
    for column in range(size):
        intermediate = sum(Z[row][column] for row in range(size))
        residual = x[column] - intermediate - va_raw[column]
        column_residual.append(residual)
        vaa[column] += residual
    row_residual = [sum(Z[row]) + y_raw[row] - x[row] for row in range(size)]

    dataset = MRIODataset(
        countries=tuple(countries), industries=tuple(industries), years=(int(year),),
        Z=tuple(tuple(row) for row in Z),
        y=tuple(y), x=tuple(x), va=tuple(va),
        units="USD_mn",
        source={"name": source["name"], "provider": source["provider"],
                "page": source["page"], "doi": source.get("doi"),
                "licence": source["licence"], "archive": source.get("archive_name"),
                "year": int(year), "converted_by": "tools/alt_mrio_import.py",
                "schema_version": SCHEMA_VERSION,
                "adjustments": {
                    "final_demand_cells": sum(1 for value in fda if value != 0.0),
                    "value_added_cells": sum(1 for value in vaa if value != 0.0),
                    "negative_final_demand_cells": sum(1 for value in y_raw if value < 0.0),
                    "min_final_demand": round(min(y_raw), 6) if y_raw else 0.0,
                    "negative_value_added_cells": sum(1 for value in va_raw if value < 0.0),
                    "min_value_added": round(min(va_raw), 6) if va_raw else 0.0,
                    "value_added_column_residual_absorbed": round(
                        sum(1 for value in column_residual if value != 0.0), 0),
                    "max_abs_value_added_column_residual": round(
                        max((abs(value) for value in column_residual), default=0.0), 6),
                    "max_abs_row_residual_before": round(
                        max((abs(value) for value in row_residual), default=0.0), 9),
                }},
        tolerance=float(tolerance),
        final_demand_adjustments=tuple(fda),
        va_adjustments=tuple(vaa),
        schema_version=SCHEMA_VERSION,
    )
    return dataset


def _chunks(sequence, size):
    for start in range(0, len(sequence), size):
        yield sequence[start:start + size]


def parse_wiod_frames(frame, year: int) -> dict:
    """Turn a pandas frame (one WIOT year) into the plain ``table`` dict of :func:`build_dataset`.

    WIOT 2016 layout (verified on the downloaded Stata archive, 2026-11-14): label columns
    ``IndustryCode``/``IndustryDescription``/``Country``/``RNr``/``Year``; ``44 x 61`` value
    columns ``v<ISO3><n>`` with ``n = 1..56`` sectors and ``n = 57..61`` final-demand categories;
    a ``TOT`` total-output column; data rows for the 44 country blocks (43 countries + RoW) and a
    ``TOT`` block of adjustment rows (II_fob, TXSP, EXP_adj, PURR, PURNR, VA, IntTTM, GO).
    Values are current-price USD million (the release states this explicitly).
    """
    import numpy as np

    layout = find_label_columns(frame)
    if layout["year_column"] in frame.columns:
        mask = frame[layout["year_column"]].astype(int) == int(year)
        if not mask.any():
            raise ImportBlocked(f"no rows for {year}")
        frame = frame[mask].reset_index(drop=True)
    wiod = wiod_layout(frame)
    country_column, industry_column = layout["country_column"], layout["industry_column"]
    country_series = frame[country_column].astype(str).str.strip()
    industry_series = frame[industry_column].astype(str).str.strip()
    data_mask = country_series.str.upper() != "TOT"
    data = frame[data_mask].reset_index(drop=True)
    countries = list(dict.fromkeys(data[country_column].astype(str).str.strip()))
    industries = list(dict.fromkeys(data[industry_column].astype(str).str.strip()))
    if len(countries) * len(industries) != len(data):
        raise ImportBlocked(f"WIOD data rows are not a complete grid: {len(data)} rows vs "
                            f"{len(countries)} countries x {len(industries)} sectors")
    if tuple(countries) != tuple(wiod["country_codes"]):
        raise ImportBlocked("WIOD row country order differs from the value-column order")
    grid = data.set_index([country_column, industry_column])
    expected = [(country, industry) for country in countries for industry in industries]
    try:
        grid = grid.loc[expected]
    except KeyError as error:
        raise ImportBlocked(f"WIOD rows do not cover the full grid: {error}") from error
    Z = grid[wiod["sector_columns"]].to_numpy(dtype=float)
    y = grid[wiod["fd_columns"]].to_numpy(dtype=float).sum(axis=1)
    x = grid[wiod["tot_column"]].to_numpy(dtype=float)
    totals = frame[~data_mask].set_index(industry_column)  # the TOT block by row code
    needed = [code for code in ("VA", "TXSP", "IntTTM", "GO") if code in totals.index]
    if "VA" not in needed or "GO" not in needed:
        raise ImportBlocked("WIOT adjustment rows VA/GO not found")
    va_rows = [code for code in ("VA", "TXSP", "IntTTM") if code in totals.index]
    va = totals.loc[va_rows][wiod["sector_columns"]].to_numpy(dtype=float).sum(axis=0)
    go = totals.loc["GO"][wiod["sector_columns"]].to_numpy(dtype=float)
    if np.max(np.abs(x - go)) > 1e-6 * max(1.0, float(np.max(np.abs(x)))):
        raise ImportBlocked("WIOT row total (TOT) and column total (GO) differ")
    return {"row_keys": expected, "sector_columns": wiod["sector_columns"], "Z": Z, "y": y,
            "x": x, "va": va, "go": go, "year": int(year),
            "adjustment_rows": {"present": sorted(str(code) for code in va_rows),
                                "column_residual_max_abs": None}}


def read_wiod_year(archive: Path, year: int) -> tuple:
    """Parse one WIOD year from the downloaded zip; returns (table, member, frame)."""
    _need_pandas()
    inspected = inspect_archive(archive)
    member = select_member(inspected["members"], year)
    with zipfile.ZipFile(Path(archive)) as handle, handle.open(member) as stream:
        name = member.lower()
        if name.endswith(".dta"):
            import pandas as pd
            import io
            frame = pd.read_stata(io.BytesIO(stream.read()), convert_categoricals=False)
        elif name.endswith(".csv"):
            import pandas as pd
            import io
            frame = pd.read_csv(io.BytesIO(stream.read()))
        elif name.endswith(".xlsx"):
            import pandas as pd
            frame = pd.read_excel(io.BytesIO(stream.read()), sheet_name=str(year))
        else:  # pragma: no cover - guarded by select_member
            raise ImportBlocked(f"unsupported WIOD member: {member}")
    if "year" in [str(column).lower() for column in frame.columns]:
        year_column = next(column for column in frame.columns if str(column).lower() == "year")
        mask = frame[year_column].astype(int) == int(year)
        if not mask.any():
            raise ImportBlocked(f"no rows for {year} in {member}")
        frame = frame[mask].reset_index(drop=True)
    table = parse_wiod_frames(frame, year)
    return table, member, frame


#: World Bank GDP (current US$) cache written by the FIGARO track (data/mrio/wb/).
WB_GDP_FILENAME = "wb/wb_NY.GDP.MKTP.CD.json"

#: Standard ISO-3 -> ISO-2 map for the 43 named WIOD 2016 countries (the World Bank API cache
#: downloaded for the FIGARO track stores ISO-2 ids).  ``ROW`` is a WIOD aggregate, not a country.
ISO3_TO_ISO2 = {
    "AUS": "AU", "AUT": "AT", "BEL": "BE", "BGR": "BG", "BRA": "BR", "CAN": "CA", "CHE": "CH",
    "CHN": "CN", "CYP": "CY", "CZE": "CZ", "DEU": "DE", "DNK": "DK", "ESP": "ES", "EST": "EE",
    "FIN": "FI", "FRA": "FR", "GBR": "GB", "GRC": "GR", "HRV": "HR", "HUN": "HU", "IDN": "ID",
    "IND": "IN", "IRL": "IE", "ITA": "IT", "JPN": "JP", "KOR": "KR", "LTU": "LT", "LUX": "LU",
    "LVA": "LV", "MEX": "MX", "MLT": "MT", "NLD": "NL", "NOR": "NO", "POL": "PL", "PRT": "PT",
    "ROU": "RO", "RUS": "RU", "SVK": "SK", "SVN": "SI", "SWE": "SE", "TUR": "TR", "TWN": "TW",
    "USA": "US",
}

#: Coverage cache written by ``--coverage`` inside the converted source directory.
WB_COVERAGE_FILENAME = "wb_gdp_coverage.json"
WB_API = "https://api.worldbank.org/v2/country/{codes}/indicator/NY.GDP.MKTP.CD?format=json&per_page=20000&date={years}"


def _http_get(url: str, timeout: float = 45.0) -> bytes:
    """GET a URL with a browser-like User-Agent; raises ``URLError``/``HTTPError`` on failure."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _wb_payload(document):
    """The ``[meta, data]`` list of a World Bank API v2 response (plain or wrapped cache)."""
    if isinstance(document, dict) and "payload" in document:
        document = document["payload"]
    if isinstance(document, list) and len(document) > 1 and isinstance(document[1], list):
        return document[1]
    return None


def _wb_values(rows, year: int) -> dict[str, float]:
    """``{country_iso: value}`` for one year from World Bank data rows."""
    values: dict[str, float] = {}
    for item in rows or []:
        if not isinstance(item, dict) or str(item.get("date")) != str(int(year)):
            continue
        value = item.get("value")
        if value is None:
            continue
        code = str((item.get("country") or {}).get("id", "")).upper()
        if code:
            values[code] = float(value)
    return values


def _coverage_cache_path(root: Path | None = None) -> Path:
    return mrio_data_root(root) / "wiod_2016" / WB_COVERAGE_FILENAME


def fetch_gdp_coverage(years, country_codes, root: Path | None = None,
                       timeout: float = 45.0) -> dict:
    """Fetch world and named-country GDP (current US$) from the World Bank API and cache it.

    Two requests: the ``WLD`` aggregate and the named ISO-3 countries (``ROW`` is excluded).  The
    result is written to ``data/mrio/wiod_2016/wb_gdp_coverage.json`` (git-ignored local data, only
    GDP totals -- no MRIO values).  A network/HTTP failure returns ``status: UNAVAILABLE`` with the
    reason instead of inventing a number.
    """
    years = sorted({int(year) for year in years})
    codes = sorted({str(code).upper() for code in country_codes
                    if str(code).upper() not in ("ROW", "TOT", "WLD")})
    if not years or not codes:
        raise ImportBlocked("coverage needs at least one year and one country code")
    date = ";".join(str(year) for year in years)
    document: dict = {"source": WB_API.format(codes="WLD;" + ";".join(codes), years=date),
                      "indicator": "NY.GDP.MKTP.CD (current US$)",
                      "retrieved_at": utc_now(), "years": years, "countries": codes,
                      "world": {}, "named": {}, "status": "AVAILABLE"}
    try:
        world_rows = _wb_payload(json.loads(_http_get(
            WB_API.format(codes="WLD", years=date), timeout=timeout)))
        named_rows = _wb_payload(json.loads(_http_get(
            WB_API.format(codes=";".join(codes), years=date), timeout=timeout)))
    except Exception as error:  # network/HTTP: report the fact, never fabricate a share
        document.update({"status": "UNAVAILABLE", "reason": f"{type(error).__name__}: {error}"})
        write_json(document, _coverage_cache_path(root))
        return document
    if world_rows is None or named_rows is None:
        document.update({"status": "UNAVAILABLE",
                         "reason": "World Bank response has no data array"})
        write_json(document, _coverage_cache_path(root))
        return document
    for year in years:
        world = _wb_values(world_rows, year).get("WLD")
        named = _wb_values(named_rows, year)
        if world is None or world <= 0:
            document["world"][str(year)] = None
            document["named"][str(year)] = named
            continue
        document["world"][str(year)] = round(world / 1e6, 3)   # USD million
        document["named"][str(year)] = {
            "total_usd_mn": round(sum(named.values()) / 1e6, 3),
            "share_percent": round(100.0 * sum(named.values()) / world, 4),
            "countries_found": len(named),
            "missing": sorted(set(codes) - set(named)),
        }
    write_json(document, _coverage_cache_path(root))
    return document


def gdp_coverage(year: int, country_codes, root: Path | None = None) -> dict:
    """Share of the named countries in world GDP at market prices (World Bank, current US$).

    First the coverage cache written by :func:`fetch_gdp_coverage`; then the raw
    ``data/mrio/wb/`` World Bank cache (ISO codes must match -- the FIGARO cache stores ISO-2, so
    this path is a fallback for synthetic tests).  When neither has the year, coverage is
    ``UNAVAILABLE`` with the reason: it is never estimated or invented.
    """
    root_path = mrio_data_root(root)
    codes = {str(code).upper() for code in country_codes
             if str(code).upper() not in ("ROW", "TOT")}
    path = root_path / "wiod_2016" / WB_COVERAGE_FILENAME
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return {"status": "UNAVAILABLE", "year": int(year),
                    "reason": f"coverage cache unreadable: {error}"}
        if document.get("status") == "AVAILABLE":
            world = (document.get("world") or {}).get(str(int(year)))
            named = (document.get("named") or {}).get(str(int(year)))
            if world and isinstance(named, dict) and named.get("share_percent") is not None:
                return {
                    "status": "AVAILABLE",
                    "year": int(year),
                    "world_gdp_usd_mn": world,
                    "named_gdp_usd_mn": named.get("total_usd_mn"),
                    "named_share_percent": named["share_percent"],
                    "row_share_percent": round(100.0 - named["share_percent"], 4),
                    "named_countries": named.get("countries_found"),
                    "missing_from_wb": named.get("missing", []),
                    "source": "World Bank NY.GDP.MKTP.CD (current US$); "
                              "cache data/mrio/wiod_2016/" + WB_COVERAGE_FILENAME,
                }
        # the coverage cache exists but is not usable -> fall through to the raw cache
    raw = root_path / WB_GDP_FILENAME
    if not raw.is_file():
        return {"status": "UNAVAILABLE", "year": int(year),
                "reason": f"no World Bank cache: run --coverage to fetch it ({raw})"}
    try:
        rows = _wb_payload(json.loads(raw.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        return {"status": "UNAVAILABLE", "year": int(year),
                "reason": f"World Bank GDP cache unreadable: {error}"}
    if rows is None:
        return {"status": "UNAVAILABLE", "year": int(year),
                "reason": "World Bank JSON has no data array"}
    values = _wb_values(rows, year)
    world = values.get("WLD")
    named = {}
    missing = []
    for code in sorted(codes):
        value = values.get(code) or values.get(ISO3_TO_ISO2.get(code, ""))
        if value is None:
            missing.append(code)
        else:
            named[code] = value
    if world is None or world <= 0:
        panel_total = sum(named.values())
        if not named:
            return {"status": "UNAVAILABLE", "year": int(year),
                    "reason": f"World Bank WORLD aggregate is missing for {year}",
                    "named_countries": 0, "missing_from_wb": missing}
        return {
            "status": "PARTIAL_PROXY",
            "year": int(year),
            "panel_gdp_usd_mn": round(panel_total / 1e6, 3),
            "panel_share_percent": None,
            "named_countries": len(named),
            "missing_from_wb": missing,
            "denominator_note": "the local World Bank cache has no WORLD aggregate and the live "
                                "API was unreachable, so the share of world GDP is not computed; "
                                "only the named-country GDP total is reported",
            "reason": f"World Bank WORLD aggregate is missing for {year}",
            "source": "World Bank NY.GDP.MKTP.CD (current US$), cached data/mrio/wb/",
        }
    named_total = sum(named.values())
    share = 100.0 * named_total / world
    return {
        "status": "AVAILABLE",
        "year": int(year),
        "world_gdp_usd_mn": round(world / 1e6, 3),
        "named_gdp_usd_mn": round(named_total / 1e6, 3),
        "named_share_percent": round(share, 4),
        "row_share_percent": round(100.0 - share, 4),
        "named_countries": len(named),
        "missing_from_wb": missing,
        "source": "World Bank NY.GDP.MKTP.CD (current US$), cached data/mrio/wb/",
    }


def dataset_id_for(year: int, source_id: str = "wiod_2016") -> str:
    """Canonical local dataset id: ``wiod_2016`` -> ``wiod_<year>`` (loadable by AMFE_MRIO_ROOT)."""
    prefix = "wiod" if source_id.startswith("wiod") else source_id.split("_")[0]
    return f"{prefix}_{int(year)}"


def convert_year(archive: Path, year: int, out_dir: Path, tolerance: float = 1e-6,
                 source_id: str = "wiod_2016", dataset_id: str | None = None) -> dict:
    """Parse + map + validate + write one year; returns aggregate metadata (no values).

    Output (inside the git-ignored ``data/mrio`` root):

    ``<out_dir>/<dataset_id>/dataset.json``   canonical ``amfe.mrio.dataset.v1.1`` (values)
    ``<out_dir>/<dataset_id>/manifest.json``  aggregate-only manifest (counts, hashes, residuals,
                                              signed-adjustment statistics, GDP coverage)

    With ``AMFE_MRIO_ROOT=<root>/<source_id>`` the production loader picks ``<dataset_id>`` up as a
    normal MRIO dataset (``python tools/mrio_data.py --validate``).
    """
    from amfe.mrio.loaders import write_dataset
    from amfe.mrio.schema import SCHEMA_VERSION, validate_dataset

    started = time.monotonic()
    table, member, frame = read_wiod_year(archive, year)
    del frame
    dataset_id = dataset_id or dataset_id_for(year, source_id)
    try:
        dataset = build_dataset(table, year=year, source=SOURCES[source_id],
                                tolerance=tolerance)
    except Exception as error:  # schema gate before validation: report the exact blockers
        diagnostics = {
            "schema_error": f"{type(error).__name__}: {error}",
            "negative_final_demand_cells": sum(1 for value in table["y"] if value < -1e-6),
            "min_final_demand": round(min(table["y"]), 6) if table["y"] else 0.0,
            "negative_value_added_cells": sum(1 for value in table["va"] if value < -1e-6),
            "min_value_added": round(min(table["va"]), 6) if table["va"] else 0.0,
            "adjustment_rows": table.get("adjustment_rows"),
        }
        return {"year": int(year), "status": "BLOCKED_CONVERSION", "member": member,
                "dataset_id": dataset_id, "diagnostics": diagnostics,
                "seconds": round(time.monotonic() - started, 1),
                "countries": len({key[0] for key in table["row_keys"]}),
                "industries": len({key[1] for key in table["row_keys"]})}
    report = validate_dataset(dataset, enforce_p2_scale=True)
    if not report.ok:
        return {"year": int(year), "status": "BLOCKED_CONVERSION", "member": member,
                "dataset_id": dataset_id, "validation": report.as_dict(),
                "diagnostics": {"adjustments": dataset.source.get("adjustments")},
                "seconds": round(time.monotonic() - started, 1)}
    directory = Path(out_dir) / dataset_id
    target = directory / "dataset.json"
    file_sha = write_dataset(dataset, target)
    description = dataset.describe()
    coverage = gdp_coverage(int(year), dataset.countries)
    manifest = {
        "schema": "amfe.mrio.manifest.v1",
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "generated_at": utc_now(),
        "status": "AVAILABLE",
        "source": {"source_id": source_id, "name": SOURCES[source_id]["name"],
                   "provider": SOURCES[source_id]["provider"],
                   "page": SOURCES[source_id]["page"],
                   "licence": SOURCES[source_id]["licence"],
                   "archive": SOURCES[source_id].get("archive_name"),
                   "member": member,
                   "converted_by": "tools/alt_mrio_import.py"},
        "dataset": {
            "path": str(target), "bytes": target.stat().st_size, "file_sha256": file_sha,
            "dataset_digest": description["digest"],
            "countries": description["countries"], "industries": description["industries"],
            "years": description["years"], "units": description["units"],
            "schema": description["schema"], "schema_version": description["schema_version"],
            "cells_Z": description["cells_Z"],
            "adjustments": dataset.source.get("adjustments"),
            "has_adjustments": description["has_adjustments"],
        },
        "validation": report.as_dict(),
        "coverage": coverage,
        "p2_gate": {"min_countries": 10, "min_industries": 30,
                    "passed": report.checks.get("p2_scale", False)},
    }
    manifest_path = directory / "manifest.json"
    manifest_sha = write_json(manifest, manifest_path)["sha256"]
    summary = {
        "year": int(year), "status": "AVAILABLE", "member": member, "dataset_id": dataset_id,
        "path": str(target), "bytes": target.stat().st_size, "file_sha256": file_sha,
        "manifest_path": str(manifest_path), "manifest_sha256": manifest_sha,
        "dataset_digest": description["digest"], "countries": description["countries"],
        "industries": description["industries"], "years": description["years"],
        "schema_version": description["schema_version"],
        "checks": report.checks, "validation_ok": report.ok,
        "max_abs_row_residual": report.metrics["max_abs_row_residual"],
        "max_abs_column_residual": report.metrics["max_abs_column_residual"],
        "global_difference_va_minus_y": report.metrics["global_difference_va_minus_y"],
        "total_final_demand_adjustments": report.metrics["total_final_demand_adjustments"],
        "total_value_added_adjustments": report.metrics["total_value_added_adjustments"],
        "adjustment_cells": report.metrics["adjustment_cells"],
        "coverage": coverage,
        "seconds": round(time.monotonic() - started, 1),
    }
    return summary


def _convert_worker(payload: dict) -> dict:
    """Process-pool entry point (top level so it can be pickled)."""
    try:
        return convert_year(Path(payload["archive"]), int(payload["year"]),
                            Path(payload["out_dir"]), float(payload["tolerance"]),
                            payload.get("source_id", "wiod_2016"))
    except Exception as error:  # the parent reports the named reason, never a partial dataset
        return {"year": int(payload["year"]), "status": "BLOCKED",
                "reason": f"{type(error).__name__}: {error}"}


def convert_years(archive: Path, years, out_dir: Path, jobs: int = 1,
                  tolerance: float = 1e-6, source_id: str = "wiod_2016") -> list[dict]:
    """Convert several years, in parallel when ``jobs > 1`` (one process per year)."""
    years = [int(year) for year in years]
    if not years:
        raise ImportBlocked("no years requested")
    if jobs <= 1 or len(years) == 1:
        return [_convert_worker({"archive": str(archive), "year": year, "out_dir": str(out_dir),
                                 "tolerance": tolerance, "source_id": source_id})
                for year in years]
    results = []
    with ProcessPoolExecutor(max_workers=min(int(jobs), len(years))) as pool:
        futures = {pool.submit(_convert_worker, {"archive": str(archive), "year": year,
                                                 "out_dir": str(out_dir),
                                                 "tolerance": tolerance,
                                                 "source_id": source_id}): year
                   for year in years}
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: item["year"])


# ---------------------------------------------------------------------------- reports
def blocked_report(root: Path | None = None, probes=None) -> dict:
    """Exact BLOCKED report: what was tried, what the user must do (no values, no invention)."""
    sources = []
    for source_id, entry in SOURCES.items():
        sources.append({
            "source_id": source_id, "name": entry["name"], "status": entry["status"],
            "countries": entry["countries"], "industries": entry["industries"],
            "years": entry["years"], "licence": entry["licence"],
            "page": entry["page"], "archive": entry.get("archive"),
            "manual_steps": _manual_steps(entry),
        })
    local = check_local(root) if root is not None else None
    return {"generated_at": utc_now(), "data_root": str(mrio_data_root(root)),
            "sources": sources, "local": local, "probes": probes,
            "note": "facts observed by tools/alt_mrio_import.py; no values are stored here"}


def _manual_steps(entry: dict) -> list[str]:
    if entry["status"] == "AVAILABLE_MANUAL" and entry.get("archive"):
        return ["run: python tools/alt_mrio_import.py --download " + entry["source_id"],
                "then: python tools/alt_mrio_import.py --convert " + entry["source_id"]
                + " --years 2014"]
    if entry["source_id"] == "oecd_icio":
        return ["open the dataset page in a browser (non-browser clients get HTTP 403)",
                "accept the OECD terms, download the 2025-release ICIO archive (1995-2022)",
                "place it under data/mrio/oecd_icio/archive/ and re-run --inspect/--convert"]
    if entry["source_id"] == "eora_mrio":
        return ["register at worldmrio.com with an academic e-mail address",
                "accept the academic licence, download the country/aggregate MRIO archive",
                "place it under data/mrio/eora_mrio/archive/ (a bespoke parser is required for "
                "the Eora text layout; not implemented in this wave)"]
    return ["record the licence and a manual download path in docs/126 before use"]


def check_local(root: Path | None = None) -> dict:
    """Status of the local converted datasets (schema/accounting validated; aggregates only)."""
    from amfe.mrio.loaders import available_datasets, load_dataset
    from amfe.mrio.schema import MRIOError

    root_path = mrio_data_root(root)
    status = {"data_root": str(root_path), "datasets": [], "status": "BLOCKED_DATA"}
    for source_id in SOURCES:
        source_dir = root_path / source_id
        candidates = sorted(source_dir.glob("*/dataset.json")) + sorted(
            source_dir.glob("dataset_*.json"))
        for path in candidates:
            entry = {"source_id": source_id, "path": str(path), "bytes": path.stat().st_size}
            try:
                loaded = load_dataset(str(path), root=root_path, enforce_p2_scale=True)
            except MRIOError as error:
                entry.update({"status": "INVALID", "reason": str(error)})
            else:
                entry.update({"status": "AVAILABLE", "sha256": loaded.sha256,
                              "dataset": loaded.dataset.describe(),
                              "validation_ok": loaded.report.ok,
                              "checks": loaded.report.checks})
                status["status"] = "AVAILABLE"
            status["datasets"].append(entry)
    return status


def write_json(document: dict, path: Path) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def update_coverage(root: Path | None = None, fetch_missing: bool = True,
                    timeout: float = 45.0) -> dict:
    """Recompute the GDP coverage of every converted dataset and update its manifest.

    Reads each ``<source>/<dataset_id>/manifest.json``, takes the dataset year and the source
    country list, asks :func:`gdp_coverage` (fetching the World Bank totals once when the cache is
    absent) and rewrites the manifest with the coverage block.  Only GDP aggregates are stored.
    """
    root_path = mrio_data_root(root)
    results = []
    for source_id in SOURCES:
        for manifest_path in sorted((root_path / source_id).glob("*/manifest.json")):
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                results.append({"manifest": str(manifest_path), "status": "UNREADABLE",
                                "reason": str(error)})
                continue
            dataset = document.get("dataset") or {}
            years = dataset.get("years") or []
            if not years:
                results.append({"manifest": str(manifest_path), "status": "SKIPPED",
                                "reason": "manifest has no dataset year"})
                continue
            year = int(years[0])
            codes = WIOD_COUNTRIES if source_id.startswith("wiod") else []
            coverage = gdp_coverage(year, codes, root_path) if codes else {
                "status": "UNAVAILABLE", "year": year,
                "reason": "no country-code list for this source"}
            if coverage["status"] != "AVAILABLE" and codes and fetch_missing:
                fetch_gdp_coverage([year], codes, root_path, timeout=timeout)
                coverage = gdp_coverage(year, codes, root_path)
            document["coverage"] = coverage
            written = write_json(document, manifest_path)
            results.append({"source_id": source_id, "manifest": str(manifest_path),
                            "dataset_id": document.get("dataset_id"), "year": year,
                            "coverage": coverage, "manifest_sha256": written["sha256"]})
    return {"action": "coverage", "generated_at": utc_now(), "data_root": str(root_path),
            "datasets": results}


# --------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Full-world MRIO acquisition and WIOD converter")
    parser.add_argument("--root", default=None, help="local data root (default data/mrio)")
    parser.add_argument("--probe", action="store_true", help="probe every known direct URL")
    parser.add_argument("--sources", default=None, help="comma-separated source ids for --probe")
    parser.add_argument("--download", default=None, metavar="SOURCE")
    parser.add_argument("--force", action="store_true", help="re-download an existing archive")
    parser.add_argument("--inspect", default=None, metavar="SOURCE")
    parser.add_argument("--convert", default=None, metavar="SOURCE")
    parser.add_argument("--years", action="append", default=None, help="year to convert (repeatable)")
    parser.add_argument("--jobs", type=int, default=1, help="conversion processes")
    parser.add_argument("--tolerance", type=float, default=1e-6,
                        help="relative tolerance of the accounting identities")
    parser.add_argument("--coverage", action="store_true",
                        help="recompute World Bank GDP coverage of the converted datasets")
    parser.add_argument("--no-fetch", action="store_true",
                        help="with --coverage: do not call the World Bank API")
    parser.add_argument("--check", action="store_true", help="local dataset status")
    parser.add_argument("--all", action="store_true", help="probe, download, convert, report")
    parser.add_argument("--report", action="store_true", help="write the BLOCKED/readiness report")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    root = mrio_data_root(args.root)
    report_dir = root / "reports"
    output: dict = {"action": None, "data_root": str(root), "generated_at": utc_now()}

    def fail(reason: str, action: str) -> int:
        document = {"action": action, "status": "BLOCKED_DATA", "reason": reason,
                    "generated_at": utc_now(), "data_root": str(root)}
        write_json(document, report_dir / "blocked.json")
        print(json.dumps(document, ensure_ascii=False, indent=1))
        return 2

    try:
        if args.probe or args.all:
            sources = [item.strip() for item in (args.sources or "").split(",") if item.strip()]
            probes = probe_sources(sources or None, timeout=args.timeout)
            output["action"] = "probe"
            output["probes"] = probes
            output["probe_file"] = write_json({"generated_at": utc_now(), "probes": probes},
                                              report_dir / "probe.json")
        if args.inspect or args.all:
            source_id = args.inspect or "wiod_2016"
            archive = archive_path(source_id, args.root)
            output["action"] = "inspect"
            output["inspect"] = inspect_archive(archive)
        if args.download or args.all:
            source_id = args.download or "wiod_2016"
            archive = archive_path(source_id, args.root)
            entry = SOURCES[source_id]
            if archive.is_file() and not args.force:
                output["download"] = {"path": str(archive), "bytes": archive.stat().st_size,
                                      "sha256": sha256_file(archive), "md5": md5_file(archive),
                                      "skipped": "already downloaded"}
            else:
                output["download"] = download(entry["archive"], archive,
                                              expected_bytes=entry["archive_bytes"],
                                              expected_md5=entry["archive_md5"],
                                              timeout=max(args.timeout, 60.0))
            output["action"] = "download"
        if args.convert or args.all:
            source_id = args.convert or "wiod_2016"
            archive = archive_path(source_id, args.root)
            if not archive.is_file():
                raise ImportBlocked(f"archive not downloaded: {archive}; run --download first")
            years = [int(year) for year in (args.years or ["2014"])]
            out_dir = root / source_id
            results = convert_years(archive, years, out_dir, jobs=args.jobs,
                                    tolerance=args.tolerance, source_id=source_id)
            output["action"] = "convert"
            output["conversion"] = results
            if all(item["status"] == "AVAILABLE" for item in results):
                output["status"] = "AVAILABLE"
            else:
                output["status"] = "BLOCKED_DATA"
        if args.coverage:
            output["action"] = "coverage"
            output["coverage"] = update_coverage(args.root, fetch_missing=not args.no_fetch,
                                                timeout=args.timeout)
        if args.check:
            output["action"] = "check"
            output["local"] = check_local(args.root)
        if args.report or args.all:
            document = blocked_report(args.root)
            document["generated_at"] = utc_now()
            output["report_file"] = write_json(document, report_dir / "blocked.json")
            output["action"] = output.get("action") or "report"
        if not any((args.probe, args.download, args.inspect, args.convert, args.check,
                    args.coverage, args.report, args.all)):
            parser.print_help()
            return 0
        output.setdefault("status", "OK")
        print(json.dumps(output, ensure_ascii=False, indent=1, default=str))
        return 0 if output.get("status") != "BLOCKED_DATA" else 2
    except ImportBlocked as error:
        return fail(str(error), "blocked")


if __name__ == "__main__":
    raise SystemExit(main())
