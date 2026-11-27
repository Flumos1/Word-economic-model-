#!/usr/bin/env python3
"""Fetch the open inputs of the external forecast scorecard (NY Fed Staff Nowcast archive).

The scorecard ([tools/external_forecast_scorecard.py](external_forecast_scorecard.py), docs/174)
scores external forecasts against first-publication facts.  The Philadelphia Fed SPF inputs are
retrieved by ``tools/fetch_spf.py``; this tool fetches the second OPEN source, the New York Fed Staff
Nowcast workbook, and converts it to the strict values-only CSV schema expected by the
``ny_fed_local`` adapter.

Data policy (the same as tools/fetch_data.py / tools/fetch_spf.py):

* the workbook and the derived CSV are **third-party data**: they are written only to the
  git-ignored ``data/raw/`` directory and are never committed;
* the committed report (``protocols/external_forecast_scorecard.json``) contains only aggregates,
  counts, metadata and SHA-256 hashes; the project policy
  (``REPORT_VALUES_POLICY`` in the scorecard) additionally keeps forecast/actual values out of git;
* this tool writes a metadata manifest next to the raw data (URL, retrieved_at, bytes, SHA-256 of the
  workbook, row counts, quarter range, SHA-256 of the derived CSV) plus an optional JSONL fetch log.
  Neither file is inside the tracked tree by default.

Source facts (checked 2026-11-26):

* workbook: ``new-york-fed-staff-nowcast_data_2002-present.xlsx`` on newyorkfed.org; the licence
  register entry ``NY_FED`` is OPEN (attribution, mark modifications, redistribute on the same terms);
* the workbook is values-only and covers 2002Q1-2021Q3 in the downloaded revision;
* 2002Q1-2015Q4 are **historical reconstructions**, 2016Q1 onward are **as published**; the adapter
  excludes reconstructed rows from real-time scoring.  Do not turn this into "PIT history is
  complete": the archive has no underlying series and no pre-2016 real-time publications.
* updates were suspended after September 2021 (licence register note), so 2021Q3 is the last quarter.

Usage::

    $env:AMFE_HTTP_TIMEOUT = "180"            # optional
    python tools/fetch_scorecard_data.py    # fetch + convert into data/raw/ny_fed/nowcast.csv
    python tools/fetch_scorecard_data.py --check
    python -m unittest tests.test_external_forecast_scorecard_fetch
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
URL = (
    "https://www.newyorkfed.org/medialibrary/media/research/policy/nowcast/"
    "new-york-fed-staff-nowcast_data_2002-present.xlsx"
)
DEFAULT_OUT = ROOT / "data" / "raw" / "ny_fed" / "nowcast.csv"
DEFAULT_META = ROOT / "data" / "raw" / "ny_fed" / "nowcast.meta.json"
DEFAULT_LOG = ROOT / "data" / "raw" / "scorecard_fetch_log.jsonl"
USER_AGENT = "AMFE-research/1.0 (scorecard fetch; contact: flumos@gmail.com)"
SCHEMA = "amfe.scorecard_fetch.nyfed_nowcast.v1"
SHEET = "Forecasts By Quarter"
HEADER_LABEL = "Forecast Date"
PUBLISHED_START_QUARTER = "2016Q1"
COLUMNS = ["date", "quarter", "nowcast", "provenance"]
LICENCE_ENTRY = "NY_FED"
ATTRIBUTION = (
    "Source: Federal Reserve Bank of New York, Staff Nowcast "
    "(https://www.newyorkfed.org/research/policy/nowcast); non-exclusive licence, attribution "
    "required, modifications marked"
)
LIMITATIONS = [
    "values-only workbook: no underlying series, so no re-estimation is possible",
    "2002Q1-2015Q4 are historical reconstructions, not real-time publications; the adapter excludes them",
    "as-published nowcasts exist only for 2016Q1 onward; the archive ends at 2021Q3 in this revision",
    "the archive is not a substitute for a PIT vintage store",
]

# --- Livingston Survey (third scorable source, added 2026-11-26; docs/254, docs/174) -----------
LIVINGSTON_URLS = {
    "medians": (
        "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/livingston-survey/"
        "historical-data/medians.xlsx?sc_lang=en&hash=CD0870EE8D175A251D5321627B6040C7"
    ),
    "release_dates": (
        "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/livingston-survey/"
        "livingston-release-dates.xlsx?la=en&sc_lang=en&hash=F1B872596626611C6F8C973747D3DA37"
    ),
}
LIVINGSTON_SCHEMA = "amfe.scorecard_fetch.livingston_t2h0.v1"
LIVINGSTON_COLUMNS = ["survey_date", "release_date", "release_basis", "origin",
                      "target_period", "horizon", "forecast"]
LIVINGSTON_STRICT_START = "2004-06-01"
LIVINGSTON_SHEET_MEDIANS = "RGDPX"
LIVINGSTON_SHEET_DATES = "Dates"
LIVINGSTON_DEFAULT_DIR = ROOT / "data" / "raw" / "livingston"
LIVINGSTON_DEFAULT_OUT = LIVINGSTON_DEFAULT_DIR / "livingston_t2_h0.csv"
LIVINGSTON_DEFAULT_META = LIVINGSTON_DEFAULT_DIR / "livingston.meta.json"
LIVINGSTON_LICENCE_ENTRY = "LIVINGSTON_SURVEY"
LIVINGSTON_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Philadelphia, Livingston Survey, median forecasts "
    "(https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-survey); "
    "research use, attribution to the Federal Reserve Bank of Philadelphia, values never published"
)
LIVINGSTON_LIMITATIONS = [
    "only T2_GDP_GROWTH at h=0 (survey-quarter q/q annualised) is derived; T1 and T2 h>0 are not mapped",
    "forecasts are published median levels (RGDPX_ZM, RGDPX_BP); 400*ln(ZM/BP) is a documented "
    "transformation, not a separately published median growth forecast",
    "strict subset starts in June 2004 because the survey documentation warns that base values "
    "X_BP before June 2004 are unreliable for variables other than CPI",
    "survey is semiannual (June/December): at most two PIT points per year; W1 coverage is only 2004-2006",
    "release dates are exact where the official file gives 'Exact'; otherwise 'Not After' is used as a "
    "conservative PIT-safe upper bound (never a reconstructed date)",
]



class FetchError(RuntimeError):
    """The workbook cannot be fetched or converted; the tool exits non-zero without writing."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def fetch_workbook(url: str = URL, *, timeout: int = 180, retries: int = 3) -> bytes:
    """Download the xlsx bytes (stdlib only, bounded retries, no redirects to non-http files)."""
    last_error: Exception | None = None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(max(1, int(retries))):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if int(response.status) != 200:
                    raise FetchError(f"HTTP {response.status} for {url}")
                data = response.read()
            if len(data) < 4 or data[:2] != b"PK":
                raise FetchError(f"the response is not an xlsx (zip) file: {len(data)} bytes")
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
            last_error = error
    raise FetchError(f"could not fetch {url}: {last_error!r}")


def _header_row(rows: Sequence[Sequence[Any]]) -> tuple[int, list[str]]:
    """Find the header row and the quarter columns; reject workbooks with the wrong layout."""
    for index, row in enumerate(rows):
        labels = [str(value).strip() if value is not None else "" for value in row]
        if HEADER_LABEL not in labels:
            continue
        header = [label.upper() for label in labels]
        if len([label for label in header if label]) < 2:
            continue
        return index, header
    raise FetchError("the workbook has no 'Forecast Date' header row")


def _quarter_label(value: Any) -> str | None:
    text = str(value).strip().upper() if value is not None else ""
    if len(text) != 6 or text[4] != "Q" or not text[:4].isdigit() or text[5] not in "1234":
        return None
    return text


def _date_label(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_workbook(data: bytes) -> dict:
    """Parse the NY Fed workbook into ``date, quarter, nowcast, provenance`` rows.

    The returned rows are values-less in logs/manifests: they are written only to the git-ignored
    CSV; callers must not publish them.  Provenance comes from the target quarter: the public
    workbook labels 2002Q1-2015Q4 as reconstructions and 2016Q1 onward as real-time publications.
    """
    try:
        import openpyxl  # noqa: PLC0415 - optional dependency only needed by the fetch step
    except ImportError as error:  # pragma: no cover - requirements ship openpyxl
        raise FetchError("openpyxl is required to read the xlsx workbook") from error
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 - openpyxl raises several format errors
        raise FetchError(f"cannot read the xlsx workbook: {error}") from error
    if SHEET not in workbook.sheetnames:
        raise FetchError(f"the workbook has no {SHEET!r} sheet; sheets={workbook.sheetnames}")
    sheet = workbook[SHEET]
    all_rows = list(sheet.iter_rows(values_only=True))
    header_index, header = _header_row(all_rows)
    quarter_columns: list[tuple[int, str]] = []
    for column, label in enumerate(header):
        quarter = _quarter_label(label)
        if quarter is not None:
            quarter_columns.append((column, quarter))
    if not quarter_columns:
        raise FetchError("the workbook has no quarter columns next to 'Forecast Date'")
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for raw in all_rows[header_index + 1:]:
        if not raw:
            continue
        published = _date_label(raw[0])
        if published is None:
            if any(value is not None for value in raw[1:]):
                skipped += 1
            continue
        for column, quarter in quarter_columns:
            if column >= len(raw) or raw[column] is None:
                continue
            try:
                value = float(raw[column])
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not math.isfinite(value):
                skipped += 1
                continue
            key = (published, quarter)
            if key in seen:
                raise FetchError(f"duplicate publication row {published}/{quarter}")
            seen.add(key)
            rows.append({
                "date": published,
                "quarter": quarter,
                "nowcast": value,
                "provenance": ("PUBLISHED" if quarter >= PUBLISHED_START_QUARTER
                               else "RECONSTRUCTED"),
            })
    if not rows:
        raise FetchError("the workbook has no usable nowcast rows")
    rows.sort(key=lambda item: (item["date"], item["quarter"]))
    quarters = sorted({item["quarter"] for item in rows})
    published_count = sum(1 for item in rows if item["provenance"] == "PUBLISHED")
    reconstructed_count = len(rows) - published_count
    return {
        "rows": rows,
        "skipped": skipped,
        "quarters": quarters,
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "rows_published": published_count,
        "rows_reconstructed": reconstructed_count,
        "published_quarters": sorted({item["quarter"] for item in rows
                                      if item["provenance"] == "PUBLISHED"}),
    }


def write_csv(rows: Iterable[Mapping[str, Any]], path: Path) -> dict:
    """Write the strict adapter CSV with LF newlines; return its size and SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = io.StringIO(newline="")
    writer = csv.DictWriter(payload, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow({key: row[key] for key in COLUMNS})
        count += 1
    text = payload.getvalue()
    path.write_bytes(text.encode("utf-8"))
    return {"path": _relative(path), "rows": count, "bytes": len(text.encode("utf-8")),
            "sha256": sha256_bytes(text.encode("utf-8"))}


def build_manifest(*, raw: bytes, parsed: Mapping[str, Any], csv_info: Mapping[str, Any],
                   url: str, retrieved_at: str) -> dict:
    return {
        "schema": SCHEMA,
        "tool": "tools/fetch_scorecard_data.py",
        "retrieved_at": retrieved_at,
        "source": {
            "name": "New York Fed Staff Nowcast (values-only workbook)",
            "url": url,
            "bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "sheet": SHEET,
            "header": HEADER_LABEL,
            "licence_entry": LICENCE_ENTRY,
            "attribution": ATTRIBUTION,
        },
        "parse": {
            "rows_total": len(parsed["rows"]),
            "rows_published": parsed["rows_published"],
            "rows_reconstructed": parsed["rows_reconstructed"],
            "skipped_rows": parsed["skipped"],
            "first_date": parsed["first_date"],
            "last_date": parsed["last_date"],
            "quarters": [parsed["quarters"][0], parsed["quarters"][-1]],
            "published_start_quarter": PUBLISHED_START_QUARTER,
            "published_quarters_count": len(parsed["published_quarters"]),
            "columns": COLUMNS,
        },
        "output": dict(csv_info),
        "limitations": list(LIMITATIONS),
        "raw_files_committed": False,
    }


def append_log(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def check_output(*, out: Path = DEFAULT_OUT, meta: Path = DEFAULT_META) -> dict:
    """Verify the local raw CSV against its manifest without touching the network."""
    result: dict[str, Any] = {"out": _relative(out), "meta": _relative(meta), "ok": False}
    if not out.is_file():
        result["reason"] = "RAW_CSV_NOT_FOUND"
        return result
    if not meta.is_file():
        result["reason"] = "MANIFEST_NOT_FOUND"
        return result
    try:
        manifest = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["reason"] = "MANIFEST_UNREADABLE"
        return result
    output = manifest.get("output") or {}
    expected_sha = output.get("sha256")
    actual_sha = sha256_file(out)
    result.update({"rows": output.get("rows"), "sha256": actual_sha,
                   "expected_sha256": expected_sha})
    if expected_sha and actual_sha != expected_sha:
        result["reason"] = "RAW_CSV_SHA256_MISMATCH"
        return result
    result["ok"] = True
    result["reason"] = None
    return result


# ----------------------------------------------------------------------------------------------
# Livingston Survey (third scorable source; docs/254, docs/174)
# ----------------------------------------------------------------------------------------------
def _cell_date(value: Any) -> date | None:
    """Read a date cell from the Livingston workbooks (datetime/date/ISO text)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip() if value is not None else ""
    if not text or text.upper() == "#N/A":
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cell_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "#N/A":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_xlsx_sheet(data: bytes, sheet_name: str, label: str) -> list[tuple]:
    try:
        import openpyxl  # noqa: PLC0415 - optional dependency only needed by the fetch step
    except ImportError as error:  # pragma: no cover - requirements ship openpyxl
        raise FetchError("openpyxl is required to read the xlsx workbooks") from error
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 - openpyxl raises several format errors
        raise FetchError(f"[{label}] cannot read the xlsx workbook: {error}") from error
    if sheet_name not in workbook.sheetnames:
        raise FetchError(f"[{label}] the workbook has no {sheet_name!r} sheet; "
                         f"sheets={workbook.sheetnames}")
    try:
        return list(workbook[sheet_name].iter_rows(values_only=True))
    finally:
        workbook.close()


def _header_index(rows: Sequence[Sequence[Any]], required: Sequence[str], label: str) -> tuple[int, dict[str, int]]:
    wanted = {name.strip().lower() for name in required}
    for index, row in enumerate(rows):
        labels = {str(value).strip().lower(): column for column, value in enumerate(row)
                  if value is not None and str(value).strip()}
        if wanted <= set(labels):
            return index, labels
    raise FetchError(f"[{label}] header row with {list(required)!r} is missing")


def parse_livingston(medians: bytes, release_dates: bytes) -> dict:
    """Derive the strict post-2004 Livingston T2 h=0 PIT rows.

    Source layout (documentation, checked 2026-11-26):
    * medians.xlsx, sheet ``RGDPX``: ``Date`` (survey date), ``RGDPX_BP`` (last historical
      quarterly level known at the survey), ``RGDPX_ZM`` (median forecast of the survey-quarter
      level); one row per survey (161 rows, 1946-06 .. 2026-06).
    * livingston-release-dates.xlsx, sheet ``Dates``: ``Survey``, ``Exact``, ``Not After``; the
      ``Survey`` year is encoded +100 before 2001, so rows are aligned by order with RGDPX and the
      authoritative survey date comes from RGDPX; ``Exact``/``Not After`` dates are correct.
    * strict subset starts 2004-06: before that the documentation warns that X_BP is unreliable for
      variables other than CPI.
    * forecast = 400*ln(RGDPX_ZM/RGDPX_BP) is the protocol T2 q/q annualised definition applied to
      published median levels (a documented transformation, no interpolation/reconstruction).
    """
    median_rows = _read_xlsx_sheet(medians, LIVINGSTON_SHEET_MEDIANS, "livingston medians")
    header_index, columns = _header_index(median_rows, ("Date", "RGDPX_BP", "RGDPX_ZM"),
                                          "livingston medians")
    medians_parsed: list[dict] = []
    for raw in median_rows[header_index + 1:]:
        survey = _cell_date(raw[columns["date"]]) if columns["date"] < len(raw) else None
        if survey is None:
            continue
        bp = _cell_float(raw[columns["rgdpx_bp"]]) if columns["rgdpx_bp"] < len(raw) else None
        zm = _cell_float(raw[columns["rgdpx_zm"]]) if columns["rgdpx_zm"] < len(raw) else None
        medians_parsed.append({"survey_date": survey, "bp": bp, "zm": zm})
    if not medians_parsed:
        raise FetchError("[livingston medians] no survey rows found")

    dates_rows = _read_xlsx_sheet(release_dates, LIVINGSTON_SHEET_DATES, "livingston release dates")
    # The published sheet is a three-row header: row 1 has 'Survey' / 'Livingston Survey Release
    # Date', row 3 has 'Exact' / 'Not After'; data starts below row 3. Locate both rows explicitly
    # instead of assuming a single header line.
    survey_column = None
    exact_column = None
    not_after_column = None
    header_row = None
    for index, row in enumerate(dates_rows):
        labels = {str(value).strip().lower(): column for column, value in enumerate(row)
                  if value is not None and str(value).strip()}
        if "survey" in labels:
            survey_column = labels["survey"]
        if "exact" in labels and "not after" in labels:
            exact_column = labels["exact"]
            not_after_column = labels["not after"]
            header_row = index
            break
    if survey_column is None or exact_column is None or not_after_column is None or header_row is None:
        raise FetchError("[livingston release dates] expected 'Survey' and 'Exact'/'Not After' headers")
    releases: list[tuple[date | None, date | None]] = []
    for raw in dates_rows[header_row + 1:]:
        survey_cell = raw[survey_column] if survey_column < len(raw) else None
        if _cell_date(survey_cell) is None:
            continue
        exact = _cell_date(raw[exact_column]) if exact_column < len(raw) else None
        not_after = _cell_date(raw[not_after_column]) if not_after_column < len(raw) else None
        releases.append((exact, not_after))
    if len(releases) != len(medians_parsed):
        raise FetchError(
            f"[livingston] row alignment failed: medians={len(medians_parsed)} releases={len(releases)}; "
            "the release-dates sheet must have one row per survey (docs/254 §4.4)")

    strict_start = date.fromisoformat(LIVINGSTON_STRICT_START)
    skipped: dict[str, int] = {}
    rows: list[dict] = []
    release_basis: dict[str, int] = {"exact": 0, "not_after": 0}
    for parsed, (exact, not_after) in zip(medians_parsed, releases):
        survey = parsed["survey_date"]
        if survey < strict_start:
            skipped["pre_2004_not_strict"] = skipped.get("pre_2004_not_strict", 0) + 1
            continue
        if exact is not None:
            publication, basis = exact, "exact"
        elif not_after is not None:
            publication, basis = not_after, "not_after"
        else:
            skipped["release_date_unknown"] = skipped.get("release_date_unknown", 0) + 1
            continue
        quarter_end = date(survey.year, 6, 30) if survey.month == 6 else date(survey.year, 12, 31)
        if survey.month not in (6, 12):
            skipped["survey_not_june_december"] = skipped.get("survey_not_june_december", 0) + 1
            continue
        if publication > quarter_end:
            skipped["release_after_quarter_end"] = skipped.get("release_after_quarter_end", 0) + 1
            continue
        bp, zm = parsed["bp"], parsed["zm"]
        if bp is None or zm is None or bp <= 0 or zm <= 0:
            skipped["missing_or_nonpositive_level"] = skipped.get("missing_or_nonpositive_level", 0) + 1
            continue
        forecast = 400.0 * math.log(zm / bp)
        if not math.isfinite(forecast):
            skipped["non_finite_forecast"] = skipped.get("non_finite_forecast", 0) + 1
            continue
        rows.append({
            "survey_date": survey.isoformat(),
            "release_date": publication.isoformat(),
            "release_basis": basis,
            "origin": quarter_end.isoformat(),
            "target_period": f"{survey.year}Q{2 if survey.month == 6 else 4}",
            "horizon": 0,
            "forecast": forecast,
        })
        release_basis[basis] += 1
    rows.sort(key=lambda item: item["survey_date"])
    if not rows:
        raise FetchError("[livingston] no strict post-2004 rows survived the PIT/level gates")
    return {
        "rows": rows,
        "skipped": skipped,
        "release_basis": release_basis,
        "medians_rows": len(medians_parsed),
        "release_rows": len(releases),
        "first_survey": rows[0]["survey_date"],
        "last_survey": rows[-1]["survey_date"],
        "first_origin": rows[0]["origin"],
        "last_origin": rows[-1]["origin"],
        "target_periods": [rows[0]["target_period"], rows[-1]["target_period"]],
        "strict_start": LIVINGSTON_STRICT_START,
    }


def write_livingston_csv(rows: Iterable[Mapping[str, Any]], path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = io.StringIO(newline="")
    writer = csv.DictWriter(payload, fieldnames=LIVINGSTON_COLUMNS, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow({key: row[key] for key in LIVINGSTON_COLUMNS})
        count += 1
    data = payload.getvalue().encode("utf-8")
    path.write_bytes(data)
    return {"path": _relative(path), "rows": count, "bytes": len(data),
            "sha256": sha256_bytes(data)}


def build_livingston_manifest(*, medians: bytes, release_dates: bytes, parsed: Mapping[str, Any],
                              csv_info: Mapping[str, Any], retrieved_at: str) -> dict:
    return {
        "schema": LIVINGSTON_SCHEMA,
        "tool": "tools/fetch_scorecard_data.py",
        "retrieved_at": retrieved_at,
        "sources": {
            "medians": {
                "url": LIVINGSTON_URLS["medians"],
                "bytes": len(medians),
                "sha256": sha256_bytes(medians),
                "sheet": LIVINGSTON_SHEET_MEDIANS,
                "columns": ["Date", "RGDPX_BP", "RGDPX_ZM"],
            },
            "release_dates": {
                "url": LIVINGSTON_URLS["release_dates"],
                "bytes": len(release_dates),
                "sha256": sha256_bytes(release_dates),
                "sheet": LIVINGSTON_SHEET_DATES,
                "columns": ["Survey", "Exact", "Not After"],
                "quirk": ("Survey year is encoded +100 before 2001; rows are aligned by order with "
                          "RGDPX and the survey date comes from RGDPX (docs/254 §4.4)"),
            },
        },
        "derivation": {
            "target": "T2_GDP_GROWTH",
            "horizon": 0,
            "formula": "400*ln(RGDPX_ZM/RGDPX_BP)",
            "origin_rule": "quarter end of the survey: 30 June / 31 December",
            "release_date_rule": "Exact if present, else Not After (PIT-safe upper bound)",
            "strict_start": LIVINGSTON_STRICT_START,
            "no_interpolation": True,
            "median_levels_transformation": (
                "the source publishes median levels; 400*ln(ZM/BP) is the protocol T2 q/q annualised "
                "definition applied to those levels, not a separately published median growth figure"),
        },
        "parse": {
            "rows_total": len(parsed["rows"]),
            "medians_rows": parsed["medians_rows"],
            "release_rows": parsed["release_rows"],
            "strict_start": parsed["strict_start"],
            "first_survey": parsed["first_survey"],
            "last_survey": parsed["last_survey"],
            "first_origin": parsed["first_origin"],
            "last_origin": parsed["last_origin"],
            "target_periods": parsed["target_periods"],
            "release_basis": dict(parsed["release_basis"]),
            "skipped": dict(sorted(parsed["skipped"].items())),
            "columns": LIVINGSTON_COLUMNS,
        },
        "output": dict(csv_info),
        "limitations": list(LIVINGSTON_LIMITATIONS),
        "raw_files_committed": False,
    }


def check_livingston(*, out: Path = LIVINGSTON_DEFAULT_OUT,
                     meta: Path = LIVINGSTON_DEFAULT_META) -> dict:
    result: dict[str, Any] = {"out": _relative(out), "meta": _relative(meta), "ok": False}
    if not out.is_file():
        result["reason"] = "RAW_CSV_NOT_FOUND"
        return result
    if not meta.is_file():
        result["reason"] = "MANIFEST_NOT_FOUND"
        return result
    try:
        manifest = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["reason"] = "MANIFEST_UNREADABLE"
        return result
    if manifest.get("schema") != LIVINGSTON_SCHEMA:
        result["reason"] = "MANIFEST_SCHEMA_MISMATCH"
        return result
    output = manifest.get("output") or {}
    expected_sha = output.get("sha256")
    actual_sha = sha256_file(out)
    result.update({"rows": output.get("rows"), "sha256": actual_sha,
                   "expected_sha256": expected_sha})
    if expected_sha and actual_sha != expected_sha:
        result["reason"] = "RAW_CSV_SHA256_MISMATCH"
        return result
    lines = out.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != ",".join(LIVINGSTON_COLUMNS):
        result["reason"] = "RAW_CSV_HEADER_MISMATCH"
        return result
    result["ok"] = True
    result["reason"] = None
    return result


def fetch_livingston(*, out: Path = LIVINGSTON_DEFAULT_OUT,
                     meta: Path = LIVINGSTON_DEFAULT_META,
                     log: Path | None = None, timeout: int = 180, retries: int = 3,
                     dry_run: bool = False) -> dict:
    medians = fetch_workbook(LIVINGSTON_URLS["medians"], timeout=timeout, retries=retries)
    release_dates = fetch_workbook(LIVINGSTON_URLS["release_dates"], timeout=timeout, retries=retries)
    parsed = parse_livingston(medians, release_dates)
    if dry_run:
        return {
            "status": "DRY_RUN",
            "sources": {
                "medians": {"bytes": len(medians), "sha256": sha256_bytes(medians)},
                "release_dates": {"bytes": len(release_dates), "sha256": sha256_bytes(release_dates)},
            },
            "rows_total": len(parsed["rows"]),
            "release_basis": dict(parsed["release_basis"]),
            "first_survey": parsed["first_survey"],
            "last_survey": parsed["last_survey"],
            "first_origin": parsed["first_origin"],
            "last_origin": parsed["last_origin"],
            "skipped": dict(sorted(parsed["skipped"].items())),
            "out": _relative(out),
        }
    csv_info = write_livingston_csv(parsed["rows"], out)
    manifest = build_livingston_manifest(medians=medians, release_dates=release_dates,
                                         parsed=parsed, csv_info=csv_info,
                                         retrieved_at=utc_now())
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    if log is not None:
        append_log(log, manifest)
    return {
        "status": "FETCHED",
        "sources": {
            "medians": {"bytes": len(medians), "sha256": sha256_bytes(medians)},
            "release_dates": {"bytes": len(release_dates), "sha256": sha256_bytes(release_dates)},
        },
        "rows_total": len(parsed["rows"]),
        "release_basis": dict(parsed["release_basis"]),
        "first_survey": parsed["first_survey"],
        "last_survey": parsed["last_survey"],
        "first_origin": parsed["first_origin"],
        "last_origin": parsed["last_origin"],
        "skipped": dict(sorted(parsed["skipped"].items())),
        "out": csv_info["path"],
        "out_sha256": csv_info["sha256"],
        "meta": _relative(meta),
    }


def _run_nyfed(args) -> dict:
    """Fetch/derive the NY Fed Staff Nowcast from the configured URL."""
    out, meta = Path(args.out), Path(args.meta)
    raw = fetch_workbook(args.url, timeout=args.timeout, retries=args.retries)
    digest = sha256_bytes(raw)
    if args.expect_sha256 and digest != args.expect_sha256:
        raise FetchError(f"workbook SHA-256 {digest} != expected {args.expect_sha256}")
    parsed = parse_workbook(raw)
    if args.dry_run:
        return {"status": "DRY_RUN", "url": args.url, "bytes": len(raw), "sha256": digest,
                "rows_total": len(parsed["rows"]),
                "rows_published": parsed["rows_published"],
                "rows_reconstructed": parsed["rows_reconstructed"],
                "first_date": parsed["first_date"], "last_date": parsed["last_date"],
                "out": _relative(out)}
    csv_info = write_csv(parsed["rows"], out)
    manifest = build_manifest(raw=raw, parsed=parsed, csv_info=csv_info,
                              url=args.url, retrieved_at=utc_now())
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    if not args.no_log:
        append_log(Path(args.log), manifest)
    return {
        "status": "FETCHED",
        "url": args.url,
        "bytes": len(raw),
        "sha256": digest,
        "rows_total": len(parsed["rows"]),
        "rows_published": parsed["rows_published"],
        "rows_reconstructed": parsed["rows_reconstructed"],
        "first_date": parsed["first_date"],
        "last_date": parsed["last_date"],
        "out": csv_info["path"], "out_sha256": csv_info["sha256"],
        "meta": _relative(meta),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch open scorecard inputs: NY Fed Staff Nowcast and Livingston Survey")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="git-ignored NY Fed CSV output")
    parser.add_argument("--meta", default=str(DEFAULT_META), help="git-ignored NY Fed manifest")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="git-ignored JSONL fetch log")
    parser.add_argument("--livingston-out", default=str(LIVINGSTON_DEFAULT_OUT),
                        help="git-ignored Livingston derived CSV")
    parser.add_argument("--livingston-meta", default=str(LIVINGSTON_DEFAULT_META),
                        help="git-ignored Livingston manifest")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--expect-sha256", default=None,
                        help="fail unless the NY Fed workbook SHA-256 matches this pin")
    parser.add_argument("--livingston", action="store_true",
                        help="fetch/derive the Livingston Survey inputs instead of the NY Fed archive")
    parser.add_argument("--all", action="store_true",
                        help="fetch/derive both open sources (NY Fed + Livingston)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and parse, print the summary, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="verify the already fetched raw CSV/manifest, no network")
    parser.add_argument("--no-log", action="store_true", help="do not append to the JSONL fetch log")
    args = parser.parse_args(argv)

    livingston_out = Path(args.livingston_out)
    livingston_meta = Path(args.livingston_meta)
    livingston_log = None if args.no_log else Path(args.log)
    if args.check:
        if args.all:
            nyfed = check_output(out=Path(args.out), meta=Path(args.meta))
            livingston = check_livingston(out=livingston_out, meta=livingston_meta)
            summary = {"ok": bool(nyfed["ok"] and livingston["ok"]),
                       "ny_fed": nyfed, "livingston": livingston}
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return 0 if summary["ok"] else 3
        if args.livingston:
            summary = check_livingston(out=livingston_out, meta=livingston_meta)
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return 0 if summary["ok"] else 3
        summary = check_output(out=Path(args.out), meta=Path(args.meta))
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary["ok"] else 3

    try:
        if args.all:
            nyfed = _run_nyfed(args)
            livingston = fetch_livingston(out=livingston_out, meta=livingston_meta,
                                          log=livingston_log, timeout=args.timeout,
                                          retries=args.retries, dry_run=args.dry_run)
            summary = {"status": "FETCHED" if not args.dry_run else "DRY_RUN",
                       "ny_fed": nyfed, "livingston": livingston}
        elif args.livingston:
            summary = fetch_livingston(out=livingston_out, meta=livingston_meta,
                                       log=livingston_log, timeout=args.timeout,
                                       retries=args.retries, dry_run=args.dry_run)
        else:
            summary = _run_nyfed(args)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    except FetchError as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False, indent=1))
        return 2



if __name__ == "__main__":
    raise SystemExit(main())
