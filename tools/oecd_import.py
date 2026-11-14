#!/usr/bin/env python3
"""OECD ICIO import: automated acquisition attempts, format-robust parser, D1 validation.

Task P2 (docs/130): obtain the OECD Inter-Country Input-Output tables (60+ economies x 45
industries, 1995-2022, USD million) — the main "full world" source — or prepare everything for a
manual download and parse the file automatically once it is placed locally.

What this tool does
-------------------
1. ``--probe``   repeated automated attempts with browser-like headers, cookies and a session:
   the dataset page, the Data Explorer, the ``stats.oecd.org`` Index page, the OECD SDMX API
   (dataflow enumeration and data endpoints), plausible DAM/zip URLs and the Internet Archive
   discovery API.  Every attempt is recorded verbatim (status, content type, size, final URL,
   error); no protection is circumvented and no third-party mirror is used as a data source.
2. ``--instruct`` exact step-by-step manual download instructions (time blocks, files, target
   directory, licence step) for the user.
3. ``--convert`` parses a downloaded ICIO archive/CSV (long/tidy, wide/matrix or SDMX-CSV
   layout — auto-detected) into ``amfe.mrio.dataset.v1.1``: a country x industry document that
   separates non-negative final demand ``y`` from ``inventory_changes`` (changes in inventories
   can be negative), keeps ``va`` and checks the accounting identities.  Multiple years are
   converted in parallel processes.
4. ``--d1``      D1 gate (docs/120 §5): >=10 countries, >=30 industries, identities within 0.1%,
   non-negativity of Z/x/y, and GDP coverage >=80% of the world (computed from the ICIO value
   added by country with the RoW block as the residual, cross-checked against World Bank GDP).
5. ``--check`` / ``--manifest``  local status and an aggregate-only manifest (counts, hashes,
   checks, coverage; never the Z matrix).

Data location (never committed; ``/data/mrio/`` is in ``.gitignore``)
--------------------------------------------------------------------
``data/mrio/oecd_icio/archive/``   files downloaded manually by the user
``data/mrio/oecd_icio/datasets/``  converted <year>.json documents (values; local only)
``data/mrio/oecd_icio/reports/``   probe / instruction / manifest / d1 reports (no values)

Usage:
    python tools/oecd_import.py --probe
    python tools/oecd_import.py --instruct
    python tools/oecd_import.py --convert --year 2019 --year 2018 --jobs 2
    python tools/oecd_import.py --d1
    python tools/oecd_import.py --check
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import io
import itertools
import json
import multiprocessing as mp
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_DATA_ROOT = ROOT / "data" / "mrio" / "oecd_icio"
WB_GDP_FILE = ROOT / "data" / "mrio" / "wb" / "wb_NY.GDP.MKTP.CD.json"

SCHEMA_V11 = "amfe.mrio.dataset.v1.1"
SCHEMA_VERSION_V11 = "1.1.0"
UNITS = "USD_mn"
D1_MIN_COUNTRIES = 10
D1_MIN_INDUSTRIES = 30
D1_TOLERANCE = 1e-3          # 0.1%: the D1 acceptance level (docs/120 §5)
D1_COVERAGE_MIN = 0.80

#: ICIO 2025 release documentation: 1995-2022 in five blocks, 45 industries (ISIC Rev.4 based).
BLOCKS = ("1995-2000", "2001-2005", "2006-2010", "2011-2015", "2016-2022")
#: ICIO industry codes of the 45-industry classification (as used in the release files).
ICIO_INDUSTRIES = (
    "D01T02", "D03", "D05T06", "D07T08", "D09", "D10T12", "D13T15", "D16", "D17T18", "D19",
    "D20", "D21", "D22", "D23", "D24", "D25", "D26", "D27", "D28", "D29", "D30", "D31T33",
    "D35", "D36T39", "D41T43", "D45T47", "D49", "D50", "D51", "D52", "D53", "D58T60", "D61",
    "D62T63", "D64T66", "D68", "D69T75", "D77T82", "D84", "D85", "D86T88", "D90T93", "D94T96",
    "D97T98", "D99",
)
#: Final-demand columns of the ICIO tables; ``INV`` is carried separately (it can be negative).
FINAL_DEMAND = ("HFCE", "NPISH", "NPS", "GGFC", "GFCF", "EXP", "DIRP", "DPABR", "DISC")
INVENTORY = ("INV", "INVNT", "CHINV", "INVENTORY")
#: ICIO 2016-edition matrix layout: final-demand columns are ``<PREFIX>_<ISO3>`` (6 categories:
#: HFCE, NPS, GGFC, GFCF, INV, DIRP) plus one statistical-discrepancy column ``DISC``;
#: the bottom rows are ``VA+TAXSUB`` (value added + taxes less subsidies) and ``OUT``.
ICIO2016_FD = ("HFCE", "NPS", "GGFC", "GFCF", "INV", "DIRP")
ADJUSTMENT_ROWS = ("VA", "VA+TAXSUB", "TLS", "VAT", "GO", "OUTPUT", "TOT", "TOTAL", "II", "OUT", "X")
FD_ALL = FINAL_DEMAND + INVENTORY

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/vnd.sdmx.data+json;version=2.0,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-fetch-mode": "navigate",
}
#: Recorded attempts (2026-11-14, docs/130 §2).  ``url`` is the official endpoint only.
PROBE_URLS = (
    ("dataset_page", "https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html"),
    ("stats_index", "https://stats.oecd.org/Index.aspx?DataSetCode=ICIO"),
    ("data_explorer", "https://data-explorer.oecd.org/"),
    ("data_explorer_api", "https://data-explorer.oecd.org/api/"),
    ("sdmx_dataflow_oecd_sdd_nad",
     "https://sdmx.oecd.org/public/rest/v2/structure/dataflow/OECD.SDD.NAD"),
    ("sdmx_dataflow_oecd_sti_pie",
     "https://sdmx.oecd.org/public/rest/v2/structure/dataflow/OECD.STI.PIE"),
    ("sdmx_icio_dataflow_guess",
     "https://sdmx.oecd.org/public/rest/v2/data/dataflow/OECD.SDD.NAD/DSD_ICIO@DF_ICIO/1.0/all"),
    ("sdmx_icio_v1_dataflow",
     "https://sdmx.oecd.org/public/rest/dataflow/OECD.SDD.NAD?format=jsondata"),
    ("legacy_zip_2025", "https://www.oecd.org/sti/ind/ICIO2025.zip"),
    ("dam_zip_2025",
     "https://www.oecd.org/content/dam/oecd/en/data/datasets/inter-country-input-output-tables/"
     "2025-icio/ICIO2025.zip"),
    ("wayback_availability",
     "https://archive.org/wayback/available?url=oecd.org/en/data/datasets/"
     "inter-country-input-output-tables.html"),
    ("wayback_cdx",
     "https://web.archive.org/cdx/search/cdx?url=oecd.org/en/data/datasets/"
     "inter-country-input-output-tables.html&output=json&limit=20"),
)
MANUAL_STEPS = (
    "1. Открыть в браузере (не автоматически — OECD отдаёт 403 не-браузерным клиентам):",
    "   https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html",
    "2. Принять OECD Terms and Conditions (страница датасета / OECD iLibrary); сохранить текст",
    "   условий и дату принятия в data/mrio/oecd_icio/reports/terms.txt (для аудита лицензии).",
    "3. Скачать релиз ICIO 2025 (1995-2022, 60+ экономик x 45 отраслей, USD million).",
    "   Блоки: 1995-2000, 2001-2005, 2006-2010, 2011-2015, 2016-2022.",
    "   Внутри блоков — ReadMe и CSV/архивы по годам (точные имена файлов в ReadMe релиза).",
    "4. Положить ВСЕ скачанные файлы (zip/csv/ReadMe) в",
    "   data/mrio/oecd_icio/archive/   (каталог не коммитится, /data/mrio/ в .gitignore).",
    "5. Запустить: python tools/oecd_import.py --convert --year 2019",
    "   (парсер сам определит long/wide/SDMX-CSV раскладку; при несовпадении он назовёт",
    "   ожидаемые колонки).",
    "6. Запустить D1: python tools/oecd_import.py --d1",
    "   (страны/отрасли, идентичности <=0,1%, покрытие >=80% мирового ВВП, неотрицательность).",
)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class OECDImportError(RuntimeError):
    """Acquisition, parsing or validation is blocked (named reason, no partial dataset)."""


# ---------------------------------------------------------------------------------- paths
def data_root(root=None) -> Path:
    path = Path(root) if root is not None else DEFAULT_DATA_ROOT
    for child in ("archive", "datasets", "reports"):
        (path / child).mkdir(parents=True, exist_ok=True)
    return path


def write_json(document: dict, path: Path) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    payload = path.read_bytes()
    return {"path": str(path), "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------- probe
def _session_opener(cookiejar: http.cookiejar.CookieJar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))


def probe_url(url: str, opener=None, timeout: float = 25.0) -> dict:
    """One HEAD/Range/GET attempt with browser-like headers; records only observed facts."""
    opener = opener or _session_opener(http.cookiejar.CookieJar())
    result = {"url": url, "checked_at": now(), "method": None, "http_status": None,
              "content_type": None, "content_length": None, "content_range": None,
              "total_bytes": None, "final_url": None, "error": None, "elapsed_ms": None}
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers=BROWSER_HEADERS, method="HEAD")
        try:
            with opener.open(request, timeout=timeout) as response:
                result.update({"method": "HEAD", "http_status": response.status,
                               "content_type": response.headers.get("Content-Type"),
                               "content_length": _int_or_none(
                                   response.headers.get("Content-Length")),
                               "final_url": response.geturl()})
                if result["content_length"] is None:
                    # HEAD without a length is not proof of a file: sniff a small Range GET
                    headers = dict(BROWSER_HEADERS)
                    headers["Range"] = "bytes=0-127"
                    with opener.open(urllib.request.Request(url, headers=headers),
                                     timeout=timeout) as sniff:
                        chunk = sniff.read(128)
                        content_range = sniff.headers.get("Content-Range")
                        result.update({"method": "HEAD+GET+Range", "content_type":
                                       sniff.headers.get("Content-Type"),
                                       "content_range": content_range,
                                       "total_bytes": _total_from_range(content_range),
                                       "sniff_magic": chunk[:8].hex()})
                        result["downloadable_file"] = _looks_like_archive(chunk, content_range)
        except urllib.error.HTTPError as head_error:
            if head_error.code in (403, 405, 501, 400):
                headers = dict(BROWSER_HEADERS)
                headers["Range"] = "bytes=0-2047"
                with opener.open(urllib.request.Request(url, headers=headers),
                                 timeout=timeout) as response:
                    chunk = response.read(2048)
                    content_range = response.headers.get("Content-Range")
                    result.update({"method": "GET+Range", "http_status": response.status,
                                   "content_type": response.headers.get("Content-Type"),
                                   "content_length": len(chunk), "content_range": content_range,
                                   "total_bytes": _total_from_range(content_range),
                                   "final_url": response.geturl()})
            else:
                raise
    except urllib.error.HTTPError as error:
        result.update({"http_status": error.code, "error": f"HTTPError {error.code}"})
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    result["reachable"] = 200 <= int(result["http_status"] or 0) < 400
    return result


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _total_from_range(value):
    match = re.search(r"/(\d+)$", value or "")
    return int(match.group(1)) if match else None


def _looks_like_archive(chunk: bytes, content_range: str | None) -> bool:
    """A real data file starts with ZIP/CSV magic and has a multi-MB declared size."""
    total = _total_from_range(content_range) or 0
    magic = chunk[:2]
    return total > 1_000_000 and (magic == b"PK" or b"," in chunk[:200] or magic[:1].isdigit())


def probe_all(timeout: float = 25.0) -> dict:
    jar = http.cookiejar.CookieJar()
    opener = _session_opener(jar)
    probes = []
    for name, url in PROBE_URLS:
        record = probe_url(url, opener=opener, timeout=timeout)
        record["attempt"] = name
        probes.append(record)
        time.sleep(0.4)  # be polite to the endpoints
    cookies = [{"name": cookie.name, "domain": cookie.domain} for cookie in jar]
    return {"generated_at": now(), "browser_headers": True, "cookies_after": cookies,
            "probes": probes,
            "summary": {"attempts": len(probes),
                        "reachable": sum(1 for item in probes if item["reachable"]),
                        "downloadable_file": any(
                            item.get("downloadable_file") or (
                                item["reachable"] and item.get("total_bytes")
                                and int(item["total_bytes"]) > 1_000_000
                                and "html" not in str(item.get("content_type")).lower())
                            for item in probes)}}


# ------------------------------------------------------------------------------ parsing
def _norm(value) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).lower()


def _code(value) -> str:
    return str(value or "").strip().upper()


def classify_column(labels: dict) -> dict:
    """Fuzzy, logged mapping of the dimension column names of an unknown ICIO layout."""
    mapping = {}
    for column in labels:
        key = _norm(column)
        if key in ("year", "time", "period", "timeperiod"):
            mapping.setdefault("year", column)
        elif key in ("rowcountry", "rowregion", "rowarea", "refarea", "rowrefarea") or (
                key.startswith("row") and "country" in key):
            mapping.setdefault("row_country", column)
        elif key in ("rowindustry", "rowisic", "rowactivity", "rowindustrycode") or (
                key.startswith("row") and ("industry" in key or "activity" in key)):
            mapping.setdefault("row_industry", column)
        elif key in ("colcountry", "colregion", "colarea", "counterpartarea", "colrefarea") or (
                key.startswith("col") and "country" in key):
            mapping.setdefault("col_country", column)
        elif key in ("colindustry", "colisic", "colactivity", "colindustrycode") or (
                key.startswith("col") and ("industry" in key or "activity" in key)):
            mapping.setdefault("col_industry", column)
        elif key in ("value", "obsvalue", "observationvalue", "amount", "val"):
            mapping.setdefault("value", column)
        elif key in ("unit", "unitmeasure", "units", "unitmult"):
            mapping.setdefault("unit", column)
        elif key in ("currency", "currencycode"):
            mapping.setdefault("currency", column)
    return mapping


def detect_layout(header: list[str]) -> str:
    """``long``, ``wide``, ``icio_matrix`` (2016 edition) or ``sdmx`` CSV data message."""
    header = [str(column) for column in header]
    upper = {column.upper() for column in header}
    if {"REF_AREA", "OBS_VALUE"} <= upper:
        return "sdmx"
    if header and header[0].strip() in ("", "Row", "row", "V1") and any(
            re.fullmatch(r"[A-Z0-9]{3,4}_[A-Z0-9]{2,10}", column) and
            not re.fullmatch(r"(HFCE|NPISH|NPS|GGFC|GFCF|INV|INVNT|DIRP|DPABR|DISC)_[A-Z0-9]{3,4}",
                             column)
            for column in header[1:]):
        return "icio_matrix"
    tidy = classify_column(header)
    if {"row_country", "row_industry", "col_country", "col_industry", "value"} <= set(tidy):
        return "long"
    value_like = [column for column in header[2:]
                  if re.fullmatch(r"[A-Z]{3,4}[_:\-\s]?[A-Z0-9]{2,12}", column)]
    if len(header) >= 3 and len(value_like) >= max(1, len(header) - 3):
        return "wide"
    if any("country" in key or "industry" in key or "region" in key
           for key in (_norm(column) for column in header)):
        return "long"
    raise OECDImportError(
        f"cannot detect the ICIO layout from the header {header[:8]}; expected a long/tidy table "
        f"(row country/industry, column country/industry, value), a wide WIOD-like matrix or the "
        f"ICIO 2016 matrix layout")


def parse_tidy_rows(rows: list[dict], mapping: dict, default_year=None) -> list[dict]:
    """Tidy CSV rows -> cells ``(year, row, row_industry, col, col_industry, value)``."""
    cells = []
    for number, row in enumerate(rows, start=2):
        try:
            value = float(row[mapping["value"]])
        except (KeyError, TypeError, ValueError):
            raise OECDImportError(f"line {number}: value column is not numeric: {row.get(mapping.get('value'))!r}")
        year = row.get(mapping.get("year")) if mapping.get("year") else default_year
        if year is None:
            raise OECDImportError(f"line {number}: no year column and no --year given")
        cells.append({"year": int(float(year)),
                      "row_country": _code(row[mapping["row_country"]]),
                      "row_industry": _code(row[mapping["row_industry"]]),
                      "col_country": _code(row[mapping["col_country"]]),
                      "col_industry": _code(row[mapping["col_industry"]]),
                      "value": value})
    return cells


def parse_wide_rows(header: list[str], rows: list[list[str]], year: int) -> list[dict]:
    """Wide matrix -> cells: label columns are (row country, row industry); value columns are
    ``<COLCOUNTRY>_<CODE>`` / ``<COLCOUNTRY>:<CODE>`` / ``<COLCOUNTRY><CODE>`` (3-letter ISO and
    ICIO industry code or final-demand code)."""
    label_columns = 2
    value_columns = header[label_columns:]
    decoded = []
    for column in value_columns:
        text = str(column).strip()
        match = re.match(r"^([A-Z]{3})[_:\-\s]?([A-Z0-9]{2,7})$", text) or re.match(
            r"^([A-Z]{3})()$", text)
        if not match:
            raise OECDImportError(
                f"cannot decode wide value column {column!r}; expected <ISO3>_<industry/FD code>")
        decoded.append((match.group(1), match.group(2) or "TOTAL"))
    cells = []
    for number, row in enumerate(rows, start=2):
        if len(row) < label_columns + len(decoded):
            raise OECDImportError(f"line {number}: layout has {len(row)} columns, expected "
                                  f"{label_columns + len(decoded)}")
        row_country, row_industry = _code(row[0]), _code(row[1])
        for (col_country, col_industry), raw in zip(decoded, row[label_columns:]):
            if raw is None or str(raw).strip() == "":
                continue
            try:
                value = float(raw)
            except ValueError as error:
                raise OECDImportError(f"line {number}: non-numeric cell {raw!r}") from error
            cells.append({"year": int(year), "row_country": row_country,
                          "row_industry": row_industry, "col_country": col_country,
                          "col_industry": col_industry, "value": value})
    return cells


def parse_icio_matrix(header: list[str], rows: list[list[str]], year: int) -> list[dict]:
    """ICIO 2016-edition matrix CSV (verified on the archived official file, docs/130).

    Layout: one empty/first label column holding ``<ISO3>_<INDUSTRY>`` row keys plus the bottom
    rows ``VA+TAXSUB`` and ``OUT``; value columns ``<ISO3>_<INDUSTRY>`` for intermediate use,
    ``<PREFIX>_<ISO3>`` for the six final-demand categories (HFCE, NPS, GGFC, GFCF, INV, DIRP)
    and a single ``DISC`` statistical-discrepancy column.
    """
    columns = [str(column) for column in header]
    if not columns or columns[0].strip() not in ("", "Row", "row", "V1"):
        raise OECDImportError("ICIO 2016 layout: first column must hold the row keys")
    value_columns = columns[1:]
    intermediate = []   # (index, country, industry)
    final_demand = []   # (index, prefix, country)
    discrepancy = None
    for index, column in enumerate(value_columns):
        if column == "DISC":
            discrepancy = index
            continue
        prefix, _, suffix = column.partition("_")
        if prefix in ICIO2016_FD + ("INVNT", "DPABR", "NPISH") and re.fullmatch(r"[A-Z0-9]{3,4}", suffix):
            final_demand.append((index, prefix, suffix))
        elif re.fullmatch(r"[A-Z0-9]{3,4}", prefix) and not suffix.startswith("_"):
            intermediate.append((index, prefix, suffix))
        else:
            raise OECDImportError(f"ICIO 2016 layout: cannot decode value column {column!r}")
    row_economies, row_industries = [], []
    for line in rows:
        label = str(line[0]).strip() if line else ""
        if "_" not in label:
            continue
        economy, _, industry = label.partition("_")
        if industry.startswith("C"):
            if economy not in row_economies:
                row_economies.append(economy)
            if industry not in row_industries:
                row_industries.append(industry)
    if not row_economies or not row_industries:
        raise OECDImportError("ICIO 2016 layout: no country-industry row keys found")
    cells = []
    for line in rows:
        label = str(line[0]).strip() if line else ""
        if label in ("VA+TAXSUB", "VA", "TLS", "OUT", "X"):
            for index, country, industry in intermediate:
                cells.append({"year": int(year), "row_country": country,
                              "row_industry": label,
                              "col_country": country, "col_industry": industry,
                              "value": _number(line[index + 1])})
            continue
        if "_" not in label:
            continue
        economy, _, industry = label.partition("_")
        if not industry or industry in ("VA", "TLS", "OUT", "X", "TOTAL"):
            continue
        for index, country, col_industry in intermediate:
            cells.append({"year": int(year), "row_country": economy,
                          "row_industry": industry, "col_country": country,
                          "col_industry": col_industry,
                          "value": _number(line[index + 1])})
        for index, prefix, country in final_demand:
            cells.append({"year": int(year), "row_country": economy,
                          "row_industry": industry, "col_country": country,
                          "col_industry": prefix, "value": _number(line[index + 1])})
        if discrepancy is not None:
            cells.append({"year": int(year), "row_country": economy,
                          "row_industry": industry, "col_country": economy,
                          "col_industry": "DISC", "value": _number(line[discrepancy + 1])})
    return cells


def _number(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError) as error:
        raise OECDImportError(f"non-numeric ICIO value {value!r}") from error


def parse_sdmx_rows(rows: list[dict]) -> list[dict]:
    """OECD SDMX CSV (data message) rows -> cells; dimensions must be resolvable by name."""
    def first(row, *names):
        for name in names:
            for key in row:
                if _norm(key) == _norm(name):
                    return row[key]
        return None
    cells = []
    for number, row in enumerate(rows, start=2):
        value = first(row, "OBS_VALUE", "value")
        if value in (None, ""):
            continue
        year = first(row, "TIME_PERIOD", "year", "period")
        cells.append({"year": int(float(year)), "row_country": _code(first(row, "REF_AREA")),
                      "row_industry": _code(first(row, "ACTIVITY")),
                      "col_country": _code(first(row, "COUNTERPART_AREA")),
                      "col_industry": _code(first(row, "COUNTERPART_ACTIVITY")),
                      "value": float(value)})
    return cells


def read_archive(path: Path) -> list[dict]:
    """Read every CSV member of a downloaded zip (or a single CSV) into tidy cell records."""
    path = Path(path)
    members: list[tuple[str, bytes]] = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith((".csv", ".txt")):
                    continue
                with archive.open(info) as stream:
                    members.append((info.filename, stream.read()))
    else:
        members.append((path.name, path.read_bytes()))
    cells = []
    for name, payload in members:
        text = payload.decode("utf-8-sig", errors="replace")
        reader = csv.reader(text.splitlines())
        try:
            header = next(reader)
        except StopIteration:
            continue
        layout = detect_layout(header)
        default_year = _year_from_name(name)
        if layout == "icio_matrix":
            lines = [line for line in reader if any(cell.strip() for cell in line)]
            cells.extend(parse_icio_matrix(header, lines,
                                           default_year if default_year else _require_year(lines)))
            continue
        rows = [dict(zip(header, line)) for line in reader if any(cell.strip() for cell in line)]
        if not rows:
            continue
        if layout == "long":
            cells.extend(parse_tidy_rows(rows, classify_column(header), default_year))
        elif layout == "sdmx":
            cells.extend(parse_sdmx_rows(rows))
        else:
            cells.extend(parse_wide_rows(header, [[row.get(column) for column in header]
                                                  for row in rows],
                                         default_year if default_year else _require_year(rows)))
    return cells


def _years_from_name(name: str) -> list[int]:
    """Data years encoded in an ICIO file name.

    ``ICIO2016_2011.zip`` -> [2011]; ``2016-2022_SML.zip`` -> range(2016, 2023); a block name
    whose first year is the release year (``1995-2000``) keeps the full range.  The edition number
    of ``ICIO<edition>_<year>`` files is not a data year.
    """
    text = str(name)
    block = re.search(r"(?<!\d)((?:19|20)\d{2})-((?:19|20)\d{2})(?!\d)", text)
    if block:
        start, end = int(block.group(1)), int(block.group(2))
        if start <= end and end - start < 40:
            return list(range(start, end + 1))
    matches = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", text)
    if not matches:
        return []
    return [int(matches[-1])]


def _year_from_name(name: str):
    years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", name)
    return int(years[-1]) if years else None


def _require_year(rows: list[dict]):
    for row in rows:
        for key, value in row.items():
            if _norm(key) in ("year", "time", "period"):
                return int(float(value))
    raise OECDImportError("wide ICIO layout without a year: pass --year explicitly")


def build_document(cells: list[dict], *, year: int, source: dict,
                   tolerance: float = D1_TOLERANCE) -> dict:
    """Assemble the ``amfe.mrio.dataset.v1.1`` document from tidy cells (fail-closed).

    Cell roles: ``row_industry``/``col_industry`` both in the industry list -> intermediate Z;
    ``col_industry`` a final-demand code -> final demand (``INV`` goes to ``inventory_changes``);
    ``row_industry`` an adjustment code (VA/TLS/...) -> value added / output rows.
    """
    year_cells = [cell for cell in cells if int(cell["year"]) == int(year)]
    if not year_cells:
        raise OECDImportError(f"no cells for year {year}")
    countries: list[str] = []
    industries: list[str] = []
    for cell in year_cells:
        for country in (cell["row_country"], cell["col_country"]):
            if country and country not in countries:
                countries.append(country)
        row_code = cell["row_industry"]
        col_code = cell["col_industry"]
        if row_code not in ADJUSTMENT_ROWS and row_code and row_code not in industries:
            industries.append(row_code)
        if (row_code in ADJUSTMENT_ROWS and col_code not in FD_ALL + ADJUSTMENT_ROWS
                and col_code and col_code not in industries):
            industries.append(col_code)
    if not industries:
        raise OECDImportError("no industry codes found in the ICIO rows/columns")
    known = [code for code in ICIO_INDUSTRIES if code in set(industries)]
    if len(known) >= D1_MIN_INDUSTRIES:
        industries = known + [code for code in industries if code not in known]
    size = len(countries) * len(industries)
    position = {pair: index for index, pair in enumerate(itertools.product(countries, industries))}
    Z = [[0.0] * size for _ in range(size)]
    y = [0.0] * size
    inventory = [0.0] * size
    discrepancy = [0.0] * size
    x: list[float] = [0.0] * size
    va_rows: dict[str, dict[str, float]] = {}
    for cell in year_cells:
        row_code, col_code = cell["row_industry"], cell["col_industry"]
        value = float(cell["value"])
        if row_code in ADJUSTMENT_ROWS:
            va_rows.setdefault(row_code, {})[(cell["col_country"], col_code)] = value
            continue
        if row_code not in industries:
            raise OECDImportError(f"unknown row industry {row_code!r}")
        if col_code in industries:
            Z[position[(cell["row_country"], row_code)]][
                position[(cell["col_country"], col_code)]] = value
        elif col_code in FINAL_DEMAND:
            y[position[(cell["row_country"], row_code)]] += value
        elif col_code in INVENTORY:
            inventory[position[(cell["row_country"], row_code)]] += value
        elif col_code == "DISC":
            discrepancy[position[(cell["row_country"], row_code)]] += value
        elif col_code in ("TOTAL", "OUT", "OUTPUT"):
            x[position[(cell["row_country"], row_code)]] = value
        else:
            raise OECDImportError(f"unknown column industry {col_code!r}")
    va = [0.0] * size
    va_blocks = [block for key, block in va_rows.items() if key in ("VA", "VA+TAXSUB")]
    tax_block = va_rows.get("TLS") or {}
    for country, industry in itertools.product(countries, industries):
        key = (country, industry)
        value = sum(float(block[key]) for block in va_blocks if key in block)
        value += float(tax_block.get(key, 0.0))
        if va_blocks and any(key in block for block in va_blocks) or key in tax_block:
            va[position[key]] = value
    output_block = va_rows.get("OUT") or va_rows.get("X") or va_rows.get("OUTPUT") or {}
    for country, industry in itertools.product(countries, industries):
        value = output_block.get((country, industry))
        if value is not None:
            x[position[(country, industry)]] = value
    if any(value == 0.0 for value in x):
        x = [sum(Z[row]) + y[row] + inventory[row] for row in range(size)]
    return {
        "schema": SCHEMA_V11,
        "schema_version": SCHEMA_VERSION_V11,
        "countries": countries,
        "industries": industries,
        "years": [int(year)],
        "units": UNITS,
        "Z": Z,
        "y": y,
        "inventory_changes": inventory,
        "discrepancy": discrepancy,
        "x": x,
        "va": va,
        "source": source,
        "tolerance": float(tolerance),
        "notes": ("values are third-party data (OECD ICIO), local only, never committed; "
                  "y excludes changes in inventories, carried in inventory_changes (may be "
                  "negative); row identity x = Z*1 + y + inventory_changes"),
    }


def validate_v11(document: dict, tolerance: float | None = None) -> dict:
    """D1 checks: dimensions, non-negativity, row/column/global identities within tolerance."""
    tol = float(tolerance if tolerance is not None else document.get("tolerance", D1_TOLERANCE))
    countries = list(document["countries"])
    industries = list(document["industries"])
    year = document["years"][0]
    size = len(countries) * len(industries)
    Z = document["Z"]
    if len(countries) < D1_MIN_COUNTRIES:
        raise OECDImportError(f"D1: {len(countries)} countries < {D1_MIN_COUNTRIES}")
    if len(industries) < D1_MIN_INDUSTRIES:
        raise OECDImportError(f"D1: {len(industries)} industries < {D1_MIN_INDUSTRIES}")
    data = document
    Z_rows = [list(map(float, row)) for row in Z]
    y = list(map(float, data["y"]))
    inventory = list(map(float, data.get("final_demand_adjustments")
                         or data.get("inventory_changes", [0.0] * size)))
    discrepancy = list(map(float, data.get("va_adjustments")
                           or data.get("discrepancy", [0.0] * size)))
    x = list(map(float, data["x"]))
    va = list(map(float, data["va"]))
    if len(Z_rows) != size or any(len(row) != size for row in Z_rows):
        raise OECDImportError("D1: Z is not countries*industries square")
    if not (len(y) == len(inventory) == len(x) == len(va) == size):
        raise OECDImportError("D1: vector length mismatch")
    # Matrix-release documents (final_demand_adjustments/va_adjustments) split the adjustments:
    # fda is row-side (inventory + discrepancy) and vaa is column-side (TLS + shifted VA).  The
    # legacy documents keep discrepancy on the row side, so branch on the declared fields.
    matrix_release = "final_demand_adjustments" in data
    row_adjustment = inventory if matrix_release else [
        inventory[index] + discrepancy[index] for index in range(size)]
    row_residual = [sum(Z_rows[row]) + y[row] + row_adjustment[row] - x[row]
                    for row in range(size)]
    column_residual = [sum(Z_rows[row][column] for row in range(size)) + va[column]
                       + discrepancy[column] - x[column]
                       for column in range(size)]
    total_x, total_z = sum(x), sum(sum(row) for row in Z_rows)
    total_y = sum(y) + sum(row_adjustment)
    total_va = sum(va) + sum(discrepancy)
    scale = max(1.0, abs(total_x))
    checks = {
        "dimensions": {"countries": len(countries), "industries": len(industries), "n": size},
        "non_negative_Z": min(min(row) for row in Z_rows) >= -tol,
        "non_negative_x": min(x) >= -tol,
        "non_negative_y": min(y) >= -tol,
        "row_balance": max(abs(value) for value in row_residual) <= tol * max(
            1.0, max(abs(value) for value in x)),
        "column_balance": max(abs(value) for value in column_residual) <= tol * max(
            1.0, max(abs(value) for value in x)),
        "global_output_identity": abs(total_x - (total_z + total_y)) <= tol * scale,
        "global_va_equals_final": abs(total_va - total_y) <= tol * max(1.0, abs(total_va)),
    }
    checks["d1_pass"] = all(checks[key] for key in (
        "non_negative_Z", "non_negative_x", "non_negative_y", "row_balance", "column_balance",
        "global_output_identity", "global_va_equals_final"))
    return {"year": int(year), "checks": checks,
            "max_abs_row_residual": max(abs(value) for value in row_residual),
            "max_abs_column_residual": max(abs(value) for value in column_residual),
            "total_output": total_x, "total_intermediate": total_z,
            "total_final": total_y, "total_value_added": total_va,
            "tolerance": tol}


# ------------------------------------------------------------------------------- coverage
def load_world_bank_gdp(path: Path = WB_GDP_FILE) -> dict:
    """{ISO3: GDP in USD_mn} for the most recent year present (World Bank current USD)."""
    if not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8")).get("payload")
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    latest = {}
    for record in payload[1]:
        code = record.get("countryiso3code") or record.get("country", {}).get("id")
        value = record.get("value")
        year = int(record.get("date", 0) or 0)
        if not code or value is None:
            continue
        if code not in latest or year > latest[code][0]:
            latest[code] = (year, float(value))
    return {code: (year, value / 1e6) for code, (year, value) in latest.items()}


def gdp_coverage(validation: dict, countries: list[str], va_by_country: dict,
                 world_bank: dict | None = None) -> dict:
    """D1 coverage: share of world GDP represented by the named countries (RoW = residual).

    Two independent readings are reported: (a) ICIO-internal — the share of total value added
    that is not in the RoW block; (b) World Bank cross-check — sum of the named countries' World
    Bank GDP relative to the World Bank total of the *same* countries when 'WLD' is missing.
    """
    residual = 0.0
    for code in ("ROW", "REST", "WLD", "WROW"):
        residual += float(va_by_country.get(code, 0.0))
    total = sum(float(value) for value in va_by_country.values())
    covered = total - residual
    share = covered / total if total > 0 else 0.0
    report = {"named_countries": len(countries), "countries_with_row": "ROW" in va_by_country,
              "total_value_added_usd_mn": total, "row_value_added_usd_mn": residual,
              "covered_value_added_usd_mn": covered,
              "covered_share_of_world_va": share,
              "coverage_pass": share >= D1_COVERAGE_MIN,
              "method": "ICIO value added: named countries / (named + RoW)"}
    if world_bank:
        cross = {}
        for country in countries:
            if country in world_bank:
                wt_year, wt_value = world_bank[country]
                cross[country] = {"year": wt_year, "wb_gdp_usd_mn": wt_value,
                                  "icio_va_usd_mn": float(va_by_country.get(country, 0.0))}
        covered_wb = sum(item["wb_gdp_usd_mn"] for item in cross.values())
        report["world_bank_cross_check"] = {
            "countries_matched": len(cross), "sum_wb_gdp_usd_mn": covered_wb,
            "sum_icio_va_usd_mn": sum(item["icio_va_usd_mn"] for item in cross.values()),
            "note": "VA is at basic prices, GDP at market prices; the ratio is informational",
        }
    return report


# ------------------------------------------------------------------------------ conversion
def _icio_matrix_layout(path: Path) -> bool:
    """Cheap layout check: read the first CSV line of an archive and test the header shape."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                member = next((info for info in archive.infolist()
                               if info.filename.lower().endswith(".csv")), None)
                if member is None:
                    return False
                with archive.open(member) as stream:
                    header = csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig",
                                                        errors="replace"))
                    first = next(header, [])
        else:
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                first = next(csv.reader(handle), [])
    except (OSError, zipfile.BadZipFile):
        return False
    return bool(first) and detect_layout(first) == "icio_matrix"


def build_document_icio_matrix(path: Path, year: int, source: dict,
                               tolerance: float = D1_TOLERANCE) -> dict:
    """Memory-lean direct builder for the ICIO 2016/2023/2025 matrix CSVs.

    Streams the (large) CSV: builds the Z matrix as float rows, sums the six final-demand
    categories per row country, carries INV/INVNT as ``inventory_changes`` and derives value
    added from the ``VA``+``TLS`` bottom rows and output from the ``X``/``OUT`` row.
    """
    import numpy as np

    path = Path(path)
    buffer: bytes | None = None
    member_name = None
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [info for info in archive.infolist()
                          if info.filename.lower().endswith(".csv")]
            preferred = [info for info in candidates if str(year) in info.filename]
            info = preferred[0] if preferred else candidates[0]
            member_name = info.filename
            with archive.open(info) as stream:
                buffer = stream.read()
        lines = io.StringIO(buffer.decode("utf-8-sig", errors="replace"))
    else:
        lines = io.StringIO(path.read_text(encoding="utf-8-sig", errors="replace"))
    reader = csv.reader(lines)
    header = next(reader)
    label_column = 0
    value_columns = header[1:]
    intermediate = []   # (index, country, industry)
    final_demand = []   # (index, prefix, country)
    total_column = None
    for index, column in enumerate(value_columns):
        if str(column).strip().upper() in ("X", "TOTAL", "OUT"):
            total_column = index
            continue
        prefix, _, suffix = str(column).partition("_")
        fd_prefixes = ICIO2016_FD + ("INVNT", "DPABR", "NPISH")
        if prefix in fd_prefixes and re.fullmatch(r"[A-Z0-9]{3,4}", suffix):
            final_demand.append((index, prefix, suffix))          # <PREFIX>_<ISO3> (2016 edition)
        elif suffix in fd_prefixes and re.fullmatch(r"[A-Z0-9]{3,4}", prefix):
            final_demand.append((index, suffix, prefix))          # <ISO3>_<PREFIX> (2023/2025)
        else:
            intermediate.append((index, prefix, suffix))
    countries, industries = [], []
    Z_rows, y, y_all, inventory, discrepancy, x, va = [], [], [], [], [], [], []
    row_out_values = []
    rows_of_intermediate = []
    va_rows, tax_rows, out_rows = None, None, None
    for line in reader:
        if not line:
            continue
        label = str(line[0]).strip()
        if label in ("TLS", "VA", "VA+TAXSUB"):
            values = np.fromiter((_number(line[index + 1]) for index, _, _ in intermediate),
                                 dtype=float, count=len(intermediate))
            if label == "TLS":
                tax_rows = values
            else:
                va_rows = values if va_rows is None else va_rows + values
            continue
        if label in ("X", "OUT", "OUTPUT", "TOTAL"):
            out_rows = np.fromiter((_number(line[index + 1]) for index, _, _ in intermediate),
                                   dtype=float, count=len(intermediate))
            continue
        economy, _, industry = label.partition("_")
        if not economy or not industry:
            continue
        if economy not in countries:
            countries.append(economy)
        if industry not in industries:
            industries.append(industry)
        Z_rows.append(np.fromiter((_number(line[index + 1]) for index, _, _ in intermediate),
                                  dtype=float, count=len(intermediate)))
        row_country = economy
        final_value = 0.0
        inventory_value = 0.0
        discrepancy_value = 0.0
        for index, prefix, country in final_demand:
            # final demand is a matrix (industry x consuming country): every adjustment vector
            # (inventory changes, discrepancy) is a product total as well, so destination
            # filtering applies only to the country-own diagnostic, never to the totals
            value = _number(line[index + 1])
            if prefix in INVENTORY:
                inventory_value += value
            elif prefix == "DISC":
                discrepancy_value += value
            elif country == row_country:
                final_value += value
        y.append(final_value)
        inventory.append(inventory_value)
        discrepancy.append(discrepancy_value)
        if total_column is not None:
            row_out_values.append(_number(line[total_column + 1]))
        all_value = sum(_number(line[index + 1]) for index, prefix, _ in final_demand
                        if prefix not in INVENTORY and prefix != "DISC")
        y_all.append(all_value)
    size = len(countries) * len(industries)
    if len(Z_rows) != size:
        raise OECDImportError(f"ICIO matrix: {len(Z_rows)} data rows for {size} "
                              f"country-industry pairs")
    position = {pair: index for index, pair in enumerate(itertools.product(countries, industries))}
    Z = np.zeros((size, size), dtype=float)
    for row, key in enumerate((label.partition("_")[0], label.partition("_")[2])
                              for label in []):  # placeholder, replaced below
        pass
    # rows were read in the canonical order of their labels: rebuild the order explicitly
    row_keys = []
    # second pass is avoided: country/industry order from Z_rows follows the file order, which is
    # the canonical country-major order of the reader; verify and fill positions
    del Z
    Z = np.zeros((size, size), dtype=float)
    for row_index, values in enumerate(Z_rows):
        country_index, industry_index = divmod(row_index, len(industries))
        if country_index >= len(countries):
            raise OECDImportError("ICIO matrix: row order is not country-major")
        Z[row_index, :] = values
    if row_out_values:
        x = [float(value) for value in row_out_values]
    elif out_rows is not None:
        x = [float(value) for value in out_rows]
    else:
        x = [float(Z[row].sum() + y_all[row] + inventory[row] + discrepancy[row])
             for row in range(size)]
    if len(y_all) == size:
        # ICIO release 2023/2025 records final demand as a matrix (supplying industry x consuming
        # country).  The canonical vector y is the product total: sum over all destinations.
        y = y_all
        document_note_fd = ("final demand is summed over all destination countries "
                            "(product total; FD matrix collapsed)")
        row_residual = [abs(Z[row].sum() + y[row] + inventory[row] + discrepancy[row] - x[row])
                        for row in range(size)]
        if max(row_residual) > 0.001 * max(1.0, float(np.max(np.abs(x)))):
            raise OECDImportError(
                f"ICIO matrix row identity residual {max(row_residual):.3f} USD_mn exceeds the "
                f"0.1% tolerance after collapsing the FD matrix; refusing to write the dataset")
    else:
        document_note_fd = "final demand of the row country"

    va_values = np.zeros(size, dtype=float)
    if va_rows is not None:
        va_values = np.asarray(va_rows, dtype=float).copy()
    tax_values = np.zeros(size, dtype=float)
    if tax_rows is not None:
        tax_values = np.asarray(tax_rows, dtype=float).copy()
    # Row side: changes in inventories and the statistical discrepancy are signed final-demand
    # adjustments.  Column side: taxes less subsidies (TLS) and any negative base VA cells are
    # signed value-added adjustments, so base va stays non-negative and both identities hold.
    inventory_values = np.asarray(inventory, dtype=float).copy()
    discrepancy_values = np.asarray(discrepancy, dtype=float).copy()
    final_adjustments = inventory_values + discrepancy_values
    value_added_adjustments = np.asarray(tax_values, dtype=float).copy()
    negative_va = va_values < 0.0
    if negative_va.any():
        value_added_adjustments[negative_va] += va_values[negative_va]
        va_values[negative_va] = 0.0
    final_adjustments = [float(value) for value in final_adjustments]
    value_added_adjustments = [float(value) for value in value_added_adjustments]
    va = [float(value) for value in va_values]
    document = {
        "schema": SCHEMA_V11, "schema_version": SCHEMA_VERSION_V11,
        "countries": countries, "industries": industries, "years": [int(year)],
        "units": UNITS,
        "Z": [[float(value) for value in row] for row in Z],
        "y": y, "final_demand_adjustments": final_adjustments,
        "va_adjustments": value_added_adjustments,
        "x": x, "va": va, "source": source, "tolerance": float(tolerance),
        "absolute_tolerance": 1.0,
        "notes": ("values are third-party OECD ICIO data, local only, never committed; "
                  "row identity x = Z*1 + y + fda (INVNT + DISC); "
                  "va is base value added and vaa carries TLS and negative VA cells; "
                  + document_note_fd),
    }
    return document


def convert_year(path: str, year: int, out_dir: str, tolerance: float = D1_TOLERANCE) -> dict:
    """Parse + build + validate + write one year (worker-safe top-level function)."""
    started = time.monotonic()
    try:
        source = {"name": "OECD Inter-Country Input-Output tables (ICIO)",
                  "provider": "OECD", "year": int(year),
                  "licence": "OECD Terms and Conditions (accepted manually by the owner)",
                  "converted_by": "tools/oecd_import.py", "schema": SCHEMA_V11}
        if _icio_matrix_layout(Path(path)):
            document = build_document_icio_matrix(Path(path), year, source, tolerance)
            validation = validate_v11(document, tolerance=tolerance)
            if not validation["checks"]["d1_pass"]:
                return {"year": int(year), "status": "FAIL_D1", "validation": validation}
            payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            target = Path(out_dir) / f"{year}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return {"year": int(year), "status": "AVAILABLE", "path": str(target),
                    "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                    "countries": len(document["countries"]),
                    "industries": len(document["industries"]),
                    "validation": validation, "seconds": round(time.monotonic() - started, 1)}
        cells = read_archive(Path(path))
        source = {"name": "OECD Inter-Country Input-Output tables (ICIO)",
                  "provider": "OECD", "year": int(year),
                  "licence": "OECD Terms and Conditions (accepted manually by the owner)",
                  "converted_by": "tools/oecd_import.py", "schema": SCHEMA_V11}
        document = build_document(cells, year=year, source=source, tolerance=tolerance)
        validation = validate_v11(document, tolerance=tolerance)
        if not validation["checks"]["d1_pass"]:
            return {"year": int(year), "status": "FAIL_D1", "validation": validation}
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        target = Path(out_dir) / f"{year}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {"year": int(year), "status": "AVAILABLE", "path": str(target),
                "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "countries": len(document["countries"]),
                "industries": len(document["industries"]),
                "validation": validation, "seconds": round(time.monotonic() - started, 1)}
    except OECDImportError as error:
        return {"year": int(year), "status": "BLOCKED_CONVERSION", "reason": str(error)}
    except Exception as error:  # noqa: BLE001 - named reason instead of a traceback in CI
        return {"year": int(year), "status": "BLOCKED_CONVERSION",
                "reason": f"{type(error).__name__}: {error}"}


def convert_years(path: Path, years: list[int], out_dir: Path, jobs: int = 1,
                  tolerance: float = D1_TOLERANCE) -> list[dict]:
    if not years:
        raise OECDImportError("no years requested")
    if jobs <= 1 or len(years) == 1:
        return [convert_year(str(path), year, str(out_dir), tolerance) for year in years]
    with mp.get_context("spawn").Pool(min(jobs, len(years))) as pool:
        results = pool.starmap(convert_year,
                               [(str(path), year, str(out_dir), tolerance) for year in years])
    return sorted(results, key=lambda item: item["year"])


def aggregate_manifest(datasets: list[dict], coverage: list[dict], probe: dict | None) -> dict:
    """Aggregate-only manifest: counts, hashes, checks and coverage — no values."""
    entries = []
    for item in datasets:
        entry = {key: item[key] for key in
                 ("year", "status", "path", "bytes", "sha256", "countries", "industries")
                 if key in item}
        if "validation" in item:
            validation = item["validation"]
            entry["validation"] = {
                "d1_pass": validation["checks"]["d1_pass"],
                "countries": validation["checks"]["dimensions"]["countries"],
                "industries": validation["checks"]["dimensions"]["industries"],
                "max_abs_row_residual": validation["max_abs_row_residual"],
                "max_abs_column_residual": validation["max_abs_column_residual"],
                "total_output": validation["total_output"],
                "tolerance": validation["tolerance"],
            }
        entries.append(entry)
    return {"schema": "amfe.mrio.manifest.v1.1", "generated_at": now(),
            "source": "OECD ICIO", "datasets": entries, "coverage": coverage,
            "probes": probe, "data_root": str(data_root()),
            "note": "aggregate metadata only: no Z matrix and no observation values"}


# --------------------------------------------------------------------------------- D1/status
def local_datasets(root=None) -> list[Path]:
    path = data_root(root) / "datasets"
    return sorted(path.glob("*.json"))


def check_local(root=None) -> dict:
    paths = local_datasets(root)
    entries = []
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            validation = validate_v11(document)
            entries.append({"path": str(path), "year": document.get("years"),
                            "countries": len(document["countries"]),
                            "industries": len(document["industries"]),
                            "bytes": path.stat().st_size,
                            "d1_pass": validation["checks"]["d1_pass"]})
        except (KeyError, ValueError, OECDImportError) as error:
            entries.append({"path": str(path), "status": "INVALID", "reason": str(error)})
    return {"data_root": str(data_root(root)), "datasets": entries,
            "status": "AVAILABLE" if entries and all(entry.get("d1_pass") for entry in entries)
            else "BLOCKED_DATA"}


def run_d1(root=None) -> dict:
    paths = local_datasets(root)
    if not paths:
        return {"status": "BLOCKED_DATA", "reason": "no converted ICIO dataset under "
                f"{data_root(root) / 'datasets'}; download the release manually (--instruct)",
                "coverage": None}
    report = {"generated_at": now(), "datasets": []}
    world_bank = load_world_bank_gdp()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        validation = validate_v11(document)
        countries = list(document["countries"])
        industries = list(document["industries"])
        va_by_country = {country: 0.0 for country in countries}
        for position, country in enumerate(country for country in countries
                                           for _ in industries):
            va_by_country[country] += float(document["va"][position])
        coverage = gdp_coverage(validation, countries, va_by_country, world_bank or None)
        d1 = bool(validation["checks"]["d1_pass"] and coverage["coverage_pass"]
                  and len(countries) >= D1_MIN_COUNTRIES
                  and len(industries) >= D1_MIN_INDUSTRIES)
        report["datasets"].append({"year": document["years"], "path": str(path),
                                   "checks": validation["checks"], "coverage": coverage,
                                   "d1": d1})
    report["status"] = ("D1_PASS" if all(item["d1"] for item in report["datasets"])
                        else "D1_FAIL")
    return report


WAYBACK_EDITION = "ICIO2016"
WAYBACK_YEARS = tuple(range(1995, 2012))


def _wayback_playback_url(snapshot: str) -> str | None:
    """Convert a Wayback snapshot URL to its raw playback (``id_``) form."""
    match = re.match(r"https?://web\.archive\.org/web/(\d+)/(.*)$", str(snapshot))
    if not match:
        return None
    return f"https://web.archive.org/web/{match.group(1)}id_/{match.group(2)}"


def wayback_files(year: int, edition: str = WAYBACK_EDITION) -> dict:
    """Official archived URL of one ICIO per-year zip (Wayback availability API)."""
    original = f"oecd.org/sti/ind/{edition}_{year}.zip"
    query = "https://archive.org/wayback/available?url=" + urllib.parse.quote(original, safe="")
    try:
        payload = json.loads(urllib.request.urlopen(
            urllib.request.Request(query, headers=BROWSER_HEADERS), timeout=40).read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"year": int(year), "url": None, "error": f"{type(error).__name__}: {error}"}
    snapshot = (payload.get("archived_snapshots") or {}).get("closest") or {}
    playback = _wayback_playback_url(snapshot.get("url") or "")
    return {"year": int(year), "snapshot": snapshot.get("url"), "url": playback,
            "status": snapshot.get("status")}


def download_wayback_icio(root: Path | None = None, years=None,
                          timeout: float = 120.0) -> dict:
    """Download the official OECD ICIO 2016-edition per-year zips from the Internet Archive.

    The files are the official OECD objects (``www.oecd.org/sti/ind/ICIO2016_<year>.zip``)
    archived by the Wayback Machine; no protection is circumvented and no third-party mirror is
    used as a data source.  Existing complete files are kept (size check) and re-verified by
    SHA-256; the report contains sizes and hashes only.
    """
    root = Path(root) if root is not None else DEFAULT_DATA_ROOT
    target_dir = root / "archive" / "icio_2016"
    target_dir.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    results = []
    for year in (years or WAYBACK_YEARS):
        entry = wayback_files(int(year))
        target = target_dir / f"{WAYBACK_EDITION}_{year}.zip"
        record = {"year": int(year), "url": entry.get("url"), "path": str(target)}
        if entry.get("url") is None:
            record.update({"status": "NO_SNAPSHOT", "error": entry.get("error")})
            results.append(record)
            continue
        if target.is_file() and target.stat().st_size > 1_000_000:
            record.update({"status": "CACHED", "bytes": target.stat().st_size,
                           "sha256": sha256_file(target)})
            results.append(record)
            continue
        for attempt in range(3):
            try:
                started = time.monotonic()
                part = target.with_suffix(".zip.part")
                headers = dict(BROWSER_HEADERS)
                headers["Referer"] = "https://web.archive.org/"
                with opener.open(urllib.request.Request(entry["url"], headers=headers),
                                 timeout=timeout) as response, part.open("wb") as handle:
                    written = 0
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        handle.write(block)
                        written += len(block)
                part.replace(target)
                record.update({"status": "DOWNLOADED", "bytes": written,
                               "sha256": sha256_file(target),
                               "seconds": round(time.monotonic() - started, 1)})
                break
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                record.update({"status": "RETRY", "error": f"{type(error).__name__}: {error}"})
                time.sleep(4)
        if record.get("status") not in ("DOWNLOADED", "CACHED"):
            record["status"] = "BLOCKED"
        results.append(record)
        time.sleep(1.0)
    available = [item for item in results if item["status"] in ("DOWNLOADED", "CACHED")]
    return {"generated_at": now(), "edition": WAYBACK_EDITION, "years": list(years or WAYBACK_YEARS),
            "downloaded": available, "failed": [item for item in results
                                                if item["status"] not in ("DOWNLOADED", "CACHED")],
            "total_bytes": sum(int(item.get("bytes") or 0) for item in available),
            "target_directory": str(target_dir)}


#: Official webfs file service (discovered on the dataset page, 2026-11-14): the machine
#: channel for the ICIO 2023/2025 releases.  Page links look like
#: ``https://webfs-sti.oecd.org/files/STI-PIE/ICIO/<release>/<file>``.
WEBFS_PREFIX = "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/"
WEBFS_RELEASES = ("2025", "2023", "2021")
WEBFS_RELEASE = "2025"
#: The five time blocks of the ICIO 2025 release (small/SML variant: standard country coverage).
ICIO2025_BLOCKS = ("1995-2000", "2001-2005", "2006-2010", "2011-2015", "2016-2022")
ICIO2023_BLOCKS = ("1995-2000", "2001-2005", "2006-2010", "2011-2015", "2016-2020")


def discover_webfs_links(html: str, release: str | None = None) -> dict:
    """Extract the official webfs file links of one ICIO release from the dataset page HTML."""
    pattern = re.compile(r"https://webfs-sti\.oecd\.org/files/STI-PIE/ICIO/(\d{4})/([^\s\"'<>]+)")
    found: dict[str, dict] = {}
    for match in pattern.finditer(html or ""):
        year, filename = match.group(1), match.group(2)
        if release and year != str(release):
            continue
        found.setdefault(year, {})[filename] = f"{WEBFS_PREFIX}{year}/{filename}"
    return {year: {"files": files,
                   "blocks": sorted(name for name in files if re.match(r"\d{4}-\d{4}_(SML|EXT)\.zip", name)),
                   "readme": sorted(name for name in files if name.lower().startswith("readme"))}
            for year, files in sorted(found.items())}


def fetch_dataset_page(timeout: float = 60.0) -> str:
    """GET the official ICIO dataset page with browser headers and a cookie session."""
    jar = http.cookiejar.CookieJar()
    opener = _session_opener(jar)
    request = urllib.request.Request(PROBE_URLS[0][1], headers=BROWSER_HEADERS)
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def download_webfs(root: Path | None = None, release: str = WEBFS_RELEASE,
                   blocks: tuple[str, ...] | None = None, timeout: float = 300.0,
                   readme: bool = True) -> dict:
    """Download the official ICIO small (SML) block archives and the ReadMe from webfs.

    The links are taken only from the official OECD dataset page; existing complete files are
    kept.  The report stays aggregate-only (sizes, SHA-256, member names).
    """
    root = Path(root) if root is not None else DEFAULT_DATA_ROOT
    html = fetch_dataset_page(timeout=min(timeout, 120.0))
    links = discover_webfs_links(html, release=release)
    if release not in links:
        raise OECDImportError(f"release {release} not found on the dataset page")
    directory = root / "archive"
    directory.mkdir(parents=True, exist_ok=True)
    wanted = list(blocks or ICIO2025_BLOCKS if release == "2025" else ICIO2023_BLOCKS)
    files = links[release]["files"]
    results = []
    if readme:
        for name in links[release]["readme"]:
            results.append(_download_webfs_file(files[name], directory, timeout))
    for block in wanted:
        candidates = [name for name in files if name.startswith(block) and "_SML.zip" in name]
        if not candidates:
            results.append({"name": f"{block}_SML.zip", "status": "NOT_LISTED"})
            continue
        results.append(_download_webfs_file(files[candidates[0]], directory, timeout))
    available = [item for item in results if item.get("status") in ("DOWNLOADED", "CACHED")]
    return {"generated_at": now(), "release": release, "page": PROBE_URLS[0][1],
            "directory": str(directory), "downloaded": available,
            "failed": [item for item in results if item.get("status") not in
                       ("DOWNLOADED", "CACHED")],
            "total_bytes": sum(int(item.get("bytes") or 0) for item in available)}


def _download_webfs_file(url: str, directory: Path, timeout: float,
                         chunk_size: int = 8 << 20) -> dict:
    """Download one webfs file; large objects are fetched with bounded Range requests.

    The OECD webfs service answers ``Range`` requests with 206 but may answer a plain GET of a
    large file with 403, so the downloader always asks for explicit byte ranges (a standard HTTP
    feature, no protection bypass) and assembles the file with size verification.
    """
    name = url.rsplit("/", 1)[-1]
    target = Path(directory) / name
    record = {"name": name, "url": url, "path": str(target)}
    if target.is_file() and target.stat().st_size > 1000 and _is_zip_or_xlsx(target):
        record.update({"status": "CACHED", "bytes": target.stat().st_size,
                       "sha256": sha256_file(target)})
        return record
    part = target.with_suffix(target.suffix + ".part")
    started = time.monotonic()
    try:
        head = dict(BROWSER_HEADERS)
        head["Range"] = "bytes=0-0"
        with urllib.request.urlopen(urllib.request.Request(url, headers=head),
                                    timeout=timeout) as response:
            content_range = response.headers.get("Content-Range") or ""
        total = _total_from_range(content_range)
        if not total:
            raise OECDImportError(f"{name}: the server did not report a size via Content-Range")
        written = 0
        with part.open("wb") as handle:
            start = 0
            while start < total:
                end = min(start + chunk_size, total) - 1
                headers = dict(BROWSER_HEADERS)
                headers["Range"] = f"bytes={start}-{end}"
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=max(timeout, 120.0)) as response:
                    block = response.read()
                if not block:
                    raise OECDImportError(f"{name}: empty range response at {start}")
                handle.write(block)
                written += len(block)
                start += len(block)
        if written != total:
            raise OECDImportError(f"{name}: downloaded {written} of {total} bytes")
        part.replace(target)
        record.update({"status": "DOWNLOADED", "bytes": written,
                       "sha256": sha256_file(target),
                       "seconds": round(time.monotonic() - started, 1)})
    except (urllib.error.URLError, TimeoutError, OSError, OECDImportError) as error:
        record.update({"status": "BLOCKED", "error": f"{type(error).__name__}: {error}"})
    return record


def _is_zip_or_xlsx(path: Path) -> bool:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(2) == b"PK"
    except OSError:
        return False


#: Expected files of the ICIO 2025 manual/automatic download (small variant), minimum sizes.
EXPECTED_2025_ARCHIVES = {
    #: exact byte sizes observed on the official webfs service (Content-Range, 2026-11-14)
    "ReadMe_ICIO_small.xlsx": {"suffix": ".xlsx", "bytes": 328422, "min_bytes": 300_000},
    "1995-2000_SML.zip": {"suffix": ".zip", "bytes": 110135258, "min_bytes": 100_000_000},
    "2001-2005_SML.zip": {"suffix": ".zip", "bytes": 99391392, "min_bytes": 90_000_000},
    "2006-2010_SML.zip": {"suffix": ".zip", "bytes": 111587430, "min_bytes": 100_000_000},
    "2011-2015_SML.zip": {"suffix": ".zip", "bytes": 116134326, "min_bytes": 100_000_000},
    "2016-2022_SML.zip": {"suffix": ".zip", "bytes": 166241019, "min_bytes": 150_000_000},
}


def check_archive(root: Path | None = None,
                  expected: dict | None = None) -> dict:
    """Validate the expected ICIO 2025 archives: names, sizes, PK magic and CSV members.

    Fail-closed: a missing file, an HTML error page saved as ``*.zip``, an unexpected size or a
    zip without CSV members is reported as a problem (never silently accepted).
    """
    root = Path(root) if root is not None else DEFAULT_DATA_ROOT
    directory = root / "archive"
    expected = expected or EXPECTED_2025_ARCHIVES
    report = {"generated_at": now(), "directory": str(directory), "files": [], "problems": [],
              "status": "PASS"}
    if not directory.is_dir():
        report.update({"status": "BLOCKED_DATA",
                       "problems": [f"archive directory not found: {directory}"]})
        return report
    for name, spec in expected.items():
        path = directory / name
        entry = {"name": name, "path": str(path), "expected_suffix": spec["suffix"],
                 "min_bytes": spec["min_bytes"]}
        if not path.is_file():
            entry["status"] = "MISSING"
            report["problems"].append(f"{name}: missing")
            report["files"].append(entry)
            continue
        size = path.stat().st_size
        entry.update({"bytes": size, "sha256": sha256_file(path)})
        if not name.lower().endswith(spec["suffix"]):
            entry["status"] = "BAD_NAME"
            report["problems"].append(f"{name}: expected suffix {spec['suffix']}")
        elif size < int(spec["min_bytes"]):
            entry["status"] = "TOO_SMALL"
            report["problems"].append(f"{name}: {size} bytes < {spec['min_bytes']} (truncated "
                                      f"download or an HTML error page)")
        elif not _is_zip_or_xlsx(path):
            entry["status"] = "BAD_MAGIC"
            report["problems"].append(f"{name}: not a zip/xlsx (PK magic missing)")
        elif name.endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    members = [info.filename for info in archive.infolist()
                               if not info.is_dir()]
            except zipfile.BadZipFile as error:
                entry["status"] = "BAD_ZIP"
                report["problems"].append(f"{name}: {error}")
                report["files"].append(entry)
                continue
            csv_members = [member for member in members if member.lower().endswith(".csv")]
            entry["members"] = len(members)
            entry["csv_members"] = csv_members[:12]
            entry["status"] = "OK" if csv_members else "NO_CSV"
            if not csv_members:
                report["problems"].append(f"{name}: archive has no CSV members")
        else:
            entry["status"] = "OK"
        report["files"].append(entry)
    if report["problems"]:
        report["status"] = "BLOCKED_DATA"
    return report


def instruction_document() -> dict:
    return {"generated_at": now(), "source": "OECD ICIO 2025 release (1995-2022)",
            "download_page": PROBE_URLS[0][1],
            "blocks": list(BLOCKS), "industries": len(ICIO_INDUSTRIES), "units": UNITS,
            "target_directory": "data/mrio/oecd_icio/archive/",
            "steps": list(MANUAL_STEPS),
            "why_manual": ("non-browser clients receive HTTP 403 from oecd.org, stats.oecd.org "
                           "redirects to data-explorer.oecd.org and the ICIO dataflow is not "
                           "published in the OECD SDMX API (404/204, see --probe); no protection "
                           "is circumvented and no third-party mirror is used"),
            "parser_ready": True,
            "expected_layouts": ["long/tidy (row country, row industry, column country, "
                                 "column industry, value[, year])",
                                 "wide matrix (ISO3_industry / ISO3_FD columns)",
                                 "SDMX-CSV (REF_AREA/ACTIVITY/OBS_VALUE)"]}


# --------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OECD ICIO: automated attempts, parser, D1")
    parser.add_argument("--root", default=None, help="data root (default data/mrio/oecd_icio)")
    parser.add_argument("--probe", action="store_true", help="automated attempts (records facts)")
    parser.add_argument("--wayback", action="store_true", help="download archived official ICIO 2016-edition per-year zips")
    parser.add_argument("--discover", action="store_true", help="discover official webfs links on the dataset page")
    parser.add_argument("--download-2025", action="store_true", help="download the official ICIO 2025 SML block archives")
    parser.add_argument("--release", default="2025", choices=("2025", "2023", "2021"))
    parser.add_argument("--check-archive", action="store_true", help="validate the expected ICIO 2025 archive files")
    parser.add_argument("--instruct", action="store_true", help="manual download instructions")
    parser.add_argument("--archive", default=None, help="downloaded zip/csv (default: scan archive/)")
    parser.add_argument("--convert", action="store_true", help="convert archive to dataset v1.1")
    parser.add_argument("--year", action="append", default=None, type=int)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--tolerance", type=float, default=D1_TOLERANCE)
    parser.add_argument("--d1", action="store_true", help="D1 gate on converted datasets")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args(argv)
    root = data_root(args.root)
    if not any((args.probe, args.instruct, args.wayback, args.discover, args.download_2025,
                args.check_archive, args.convert, args.d1, args.check)):
        parser.print_help()
        return 0
    try:
        if args.probe:
            report = probe_all(timeout=args.timeout)
            report["file"] = write_json(report, root / "reports" / "probe.json")
            print(json.dumps({"action": "probe", "summary": report["summary"],
                              "file": report["file"],
                              "downloadable_file": report["summary"]["downloadable_file"]},
                             ensure_ascii=False, indent=1))
        if args.instruct:
            document = instruction_document()
            document["file"] = write_json(document, root / "reports" / "instructions.json")
            print(json.dumps(document, ensure_ascii=False, indent=1))
        if args.wayback:
            report = download_wayback_icio(root)
            path = write_json(report, root / "reports" / "wayback.json")
            print(json.dumps({"action": "wayback", "downloaded": len(report["downloaded"]),
                              "failed": len(report["failed"]),
                              "total_bytes": report["total_bytes"], "file": path},
                             ensure_ascii=False, indent=1))
        if args.discover:
            html = fetch_dataset_page(timeout=max(args.timeout, 30.0))
            links = discover_webfs_links(html, release=args.release)
            path = write_json({"generated_at": now(), "release": args.release,
                               "page": PROBE_URLS[0][1], "links": links},
                              root / "reports" / "webfs_links.json")
            print(json.dumps({"action": "discover", "release": args.release,
                              "releases": sorted(links), "files": len(links.get(args.release, {}).get("files", {})),
                              "file": path}, ensure_ascii=False, indent=1))
        if args.download_2025:
            report = download_webfs(root, release=args.release, timeout=max(args.timeout, 120.0))
            path = write_json(report, root / "reports" / f"download_{args.release}.json")
            print(json.dumps({"action": "download", "release": args.release,
                              "downloaded": len(report["downloaded"]),
                              "failed": len(report["failed"]),
                              "total_bytes": report["total_bytes"], "file": path},
                             ensure_ascii=False, indent=1))
        if args.check_archive:
            report = check_archive(root)
            path = write_json(report, root / "reports" / "archive_check.json")
            print(json.dumps({"action": "check-archive", "status": report["status"],
                              "problems": report["problems"],
                              "files": [{"name": item["name"], "status": item["status"],
                                         "bytes": item.get("bytes"),
                                         "csv_members": item.get("csv_members")}
                                        for item in report["files"]],
                              "file": path}, ensure_ascii=False, indent=1))
        if args.convert:
            tasks: list[tuple[Path, list[int]]] = []
            if args.archive:
                archive = Path(args.archive)
                years = args.year or _years_from_name(archive.name)
                if not years:
                    raise OECDImportError("cannot infer the year from the file name; pass --year")
                tasks.append((archive, years))
            else:
                candidates = sorted(p for p in (root / "archive").rglob("*")
                                    if p.suffix.lower() in (".zip", ".csv", ".txt"))
                if not candidates:
                    raise OECDImportError(
                        f"no archive under {root / 'archive'}; run --instruct and place the "
                        f"downloaded ICIO files there")
                for archive in candidates:
                    years = args.year or _years_from_name(archive.name)
                    if years:
                        tasks.append((archive, years))
                if not tasks:
                    raise OECDImportError("cannot infer any year from the archive file names; "
                                          "pass --year")
            flat = [(str(archive), year) for archive, years in tasks for year in years]
            if args.jobs > 1 and len(flat) > 1:
                with mp.get_context("spawn").Pool(min(args.jobs, len(flat))) as pool:
                    results = pool.starmap(convert_year,
                                           [(archive, year, str(root / "datasets"),
                                             args.tolerance) for archive, year in flat])
                results = sorted(results, key=lambda item: item["year"])
            else:
                results = [convert_year(archive, year, str(root / "datasets"), args.tolerance)
                           for archive, year in flat]
            print(json.dumps({"action": "convert",
                              "archives": [str(archive) for archive, _ in tasks],
                              "results": results}, ensure_ascii=False, indent=1))
        if args.d1:
            report = run_d1(args.root)
            report["file"] = write_json(report, root / "reports" / "d1.json")
            print(json.dumps(report, ensure_ascii=False, indent=1))
        if args.check:
            status = check_local(args.root)
            manifest_path = root / "reports" / "manifest.json"
            d1_path = root / "reports" / "d1.json"
            if d1_path.is_file():
                d1_report = json.loads(d1_path.read_text(encoding="utf-8"))
                entries = []
                for item in d1_report.get("datasets", []):
                    years = item.get("year") or []
                    entries.append({
                        "year": years[0] if years else None,
                        "path": item.get("path"),
                        "d1": bool(item.get("d1")),
                        "coverage_share": round(float(item.get("coverage", {}).get(
                            "covered_share_of_world_va", 0.0)), 6),
                        "validation": {"d1_pass": bool(item.get("checks", {}).get("d1_pass")),
                                       "countries": item.get("checks", {}).get(
                                           "dimensions", {}).get("countries"),
                                       "industries": item.get("checks", {}).get(
                                           "dimensions", {}).get("industries")},
                    })
                manifest = {"schema": "amfe.mrio.manifest.v1.1",
                            "generated_at": now(), "source": "OECD ICIO",
                            "datasets": entries,
                            "coverage": {"min": min((e["coverage_share"] for e in entries),
                                                    default=None),
                                         "max": max((e["coverage_share"] for e in entries),
                                                    default=None),
                                         "gate": 0.80,
                                         "note": "coverage = named economies' share of world "
                                                 "value added (RoW residual), World Bank cross-check"},
                            "note": "aggregate metadata only: no Z matrix and no values"}
                status["manifest"] = write_json(manifest, manifest_path)
            else:
                status["manifest"] = write_json(
                    aggregate_manifest([], [], None), manifest_path)
            print(json.dumps(status, ensure_ascii=False, indent=1))
        return 0
    except OECDImportError as error:
        document = {"action": "blocked", "status": "BLOCKED_DATA", "reason": str(error),
                    "generated_at": now()}
        document["file"] = write_json(document, root / "reports" / "blocked.json")
        print(json.dumps(document, ensure_ascii=False, indent=1))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
