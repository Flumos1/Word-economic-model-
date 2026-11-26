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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the NY Fed Staff Nowcast archive for the scorecard")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="git-ignored CSV output")
    parser.add_argument("--meta", default=str(DEFAULT_META), help="git-ignored manifest next to the raw data")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="git-ignored JSONL fetch log")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--expect-sha256", default=None,
                        help="fail unless the workbook SHA-256 matches this pin")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and parse, print the summary, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="verify the already fetched raw CSV/manifest, no network")
    parser.add_argument("--no-log", action="store_true", help="do not append to the JSONL fetch log")
    args = parser.parse_args(argv)
    out, meta = Path(args.out), Path(args.meta)
    if args.check:
        summary = check_output(out=out, meta=meta)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary["ok"] else 3
    try:
        raw = fetch_workbook(args.url, timeout=args.timeout, retries=args.retries)
        digest = sha256_bytes(raw)
        if args.expect_sha256 and digest != args.expect_sha256:
            raise FetchError(f"workbook SHA-256 {digest} != expected {args.expect_sha256}")
        parsed = parse_workbook(raw)
        if args.dry_run:
            summary = {"status": "DRY_RUN", "url": args.url, "bytes": len(raw), "sha256": digest,
                       "rows_total": len(parsed["rows"]),
                       "rows_published": parsed["rows_published"],
                       "rows_reconstructed": parsed["rows_reconstructed"],
                       "first_date": parsed["first_date"], "last_date": parsed["last_date"],
                       "out": _relative(out)}
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return 0
        csv_info = write_csv(parsed["rows"], out)
        manifest = build_manifest(raw=raw, parsed=parsed, csv_info=csv_info,
                                  url=args.url, retrieved_at=utc_now())
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8", newline="\n")
        if not args.no_log:
            append_log(Path(args.log), manifest)
        summary = {
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
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    except FetchError as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False, indent=1))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
