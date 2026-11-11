#!/usr/bin/env python3
"""Philadelphia Fed Survey of Professional Forecasters (SPF) medians - metadata only in the repo.

Licence policy (protocols/evaluation_protocol_v4.json, licence_policy.raw_data / intended_use):
raw third-party archives are never committed and only derived statistics are published. The SPF median
forecasts are third-party data, so this tool writes the *values* only into the git-ignored data
directory (data/raw/spf/spf_median.json) and writes *metadata* into protocols/spf_metadata.json:

  * source URLs and the page the files were found on;
  * byte size and SHA-256 of each downloaded xlsx;
  * sheet and column names used, number of observations and survey quarters, first/last survey date;
  * SHA-256 of the extracted value lists per variable (provenance without publishing the values);
  * retrieval timestamp, extraction method, and the recorded limitations.

No median value is written to protocols/. The xlsx files themselves are downloaded in memory and are
never written to the repository either.

Source pages and files (licence register entry PHILADELPHIA_FED, status OPEN - research use):
  * median forecasts page:
    https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts
  * CPI/10-year inflation medians:
    .../survey-of-professional-forecasters/historical-data/Inflation.xlsx
    sheet INFLATION, columns YEAR, QUARTER, INFPGDP1YR, INFCPI1YR, INFCPI10YR
  * real GDP growth medians:
    .../survey-of-professional-forecasters/historical-data/medianGrowth.xlsx
    sheet RGDP, columns YEAR, QUARTER, drgdp2 .. drgdp6 (the published forecast-horizon codes)

Survey dates: the quarter in the YEAR/QUARTER columns is the survey quarter. SPF deadlines fall in the
middle month of that quarter (documented in the licence register and the SPF documentation), so an
anchor survey_date = first day of the middle month is recorded; the exact deadline day is published in
the SPF release calendar and is not machine-parsed here (limitation).

--check-rtdsm additionally probes the Philadelphia Fed Real-Time Data Set for Macroeconomists (RTDSM)
xlsx files and prints their metadata (size, SHA-256, sheet dimensions) without writing anything and
without storing the files - RTDSM is likewise metadata-only in this project.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ("https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
        "survey-of-professional-forecasters/historical-data/")
RTDSM_BASE = ("https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
              "real-time-data/data-files/xlsx/")
SOURCE_PAGE = ("https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/"
               "median-forecasts")
FILES = {
    "inflation": {"name": "Inflation.xlsx", "url": BASE + "Inflation.xlsx",
                  "sheet": "INFLATION", "variables": ["INFCPI1YR", "INFCPI10YR"]},
    "growth": {"name": "medianGrowth.xlsx", "url": BASE + "medianGrowth.xlsx",
               "sheet": "RGDP", "variables": ["drgdp2", "drgdp3", "drgdp4", "drgdp5", "drgdp6"]},
}
RTDSM_FILES = {
    "ROUTPUTQvQd": {"url": RTDSM_BASE + "ROUTPUTQvQd.xlsx",
                    "content": "real output (GDP/GDI), quarterly vintages"},
    "cpiQvMd": {"url": RTDSM_BASE + "cpiQvMd.xlsx",
                "content": "CPI, quarterly and monthly vintages"},
}
USER_AGENT = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
VALUES_DEFAULT = ROOT / "data" / "raw" / "spf" / "spf_median.json"
METADATA_DEFAULT = ROOT / "protocols" / "spf_metadata.json"
HORIZONS = {
    "INFCPI1YR": {"variable": "CPI inflation", "horizon": "1 year ahead",
                  "units": "percent, as published"},
    "INFCPI10YR": {"variable": "CPI inflation", "horizon": "10 years ahead",
                   "units": "percent per year"},
    "drgdp2": {"variable": "Real GDP growth", "horizon": "h2 (published SPF horizon code)"},
    "drgdp3": {"variable": "Real GDP growth", "horizon": "h3 (published SPF horizon code)"},
    "drgdp4": {"variable": "Real GDP growth", "horizon": "h4 (published SPF horizon code)"},
    "drgdp5": {"variable": "Real GDP growth", "horizon": "h5 (published SPF horizon code)"},
    "drgdp6": {"variable": "Real GDP growth", "horizon": "h6 (published SPF horizon code)"},
}
MIDDLE_MONTH = {1: 2, 2: 5, 3: 8, 4: 11}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=240) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url}: HTTP {resp.status}")
        return resp.read()


def xlsx_compat(raw: bytes) -> bytes:
    """Normalise malformed docProps/core.xml timestamps that make openpyxl 3.x fail.

    Some Philadelphia Fed workbooks write W3CDTF values such as "2026-10-30T 9:58:09-04:00"
    (a space after T, a single-digit hour), which openpyxl cannot parse. The timestamp is rewritten to
    "<date>T00:00:00Z"; cell data is not touched.
    """
    zin = zipfile.ZipFile(io.BytesIO(raw))
    if "docProps/core.xml" not in zin.namelist():
        return raw
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/core.xml":
                text = data.decode("utf-8")
                text = re.sub(
                    r"(<dcterms:(?:created|modified)[^>]*>)([^<]*)(</dcterms)",
                    lambda m: (m.group(1) + (re.search(r"\d{4}-\d{2}-\d{2}", m.group(2)).group(0)
                                            if re.search(r"\d{4}-\d{2}-\d{2}", m.group(2))
                                            else "1970-01-01") + "T00:00:00Z" + m.group(3)),
                    text)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    return out.getvalue()


def load_workbook(raw: bytes):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - CI installs openpyxl
        raise SystemExit("openpyxl is required: python -m pip install openpyxl") from exc
    try:
        return openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except TypeError:
        return openpyxl.load_workbook(io.BytesIO(xlsx_compat(raw)), read_only=True, data_only=True)


def numeric(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def survey_anchor(year: int, quarter: int) -> dict:
    month = MIDDLE_MONTH[quarter]
    return {"survey_quarter": f"{year}Q{quarter}", "survey_date": f"{year}-{month:02d}-01"}


def parse_medians(raw: bytes, sheet: str, variables: list, min_year: int = 1990) -> tuple:
    """Return (median rows, header names) for one workbook sheet."""
    wb = load_workbook(raw)
    if sheet not in wb.sheetnames:
        raise RuntimeError(f"sheet {sheet!r} absent; sheets={wb.sheetnames}")
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    index = {name: i for i, name in enumerate(header)}
    for required in ["YEAR", "QUARTER", *variables]:
        if required not in index:
            raise RuntimeError(f"column {required!r} absent in sheet {sheet!r}; header={header}")
    out = []
    for row in rows:
        year, quarter = numeric(row[index["YEAR"]]), numeric(row[index["QUARTER"]])
        if year is None or quarter is None or year < min_year:
            continue
        anchor = survey_anchor(int(year), int(quarter))
        for variable in variables:
            value = numeric(row[index[variable]])
            if value is None:
                continue
            out.append({"variable": HORIZONS[variable]["variable"], "code": variable,
                        "horizon": HORIZONS[variable]["horizon"],
                        **anchor, "value": value})
    wb.close()
    return out, header


def value_list_sha(rows: list) -> str:
    blob = json.dumps([[r["code"], r["survey_quarter"], r["value"]] for r in rows],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check_rtdsm() -> dict:
    """Probe the RTDSM xlsx files: availability, size, SHA-256, sheet dimensions (nothing stored)."""
    report = {"dataset": "Philadelphia Fed Real-Time Data Set for Macroeconomists (RTDSM)",
              "source_url": "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/"
                            "real-time-data-set-for-macroeconomists",
              "retrieved_at": utc_now(), "raw_files_stored": False, "files": [],
              "automatable": True,
              "publication_rule": "metadata only: the RTDSM workbooks are third-party data and are "
                                  "never committed; values stay outside the repository"}
    for key, meta in RTDSM_FILES.items():
        entry = {"key": key, "url": meta["url"], "content": meta["content"]}
        try:
            raw = fetch(meta["url"])
            entry.update({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                          "is_zip": raw[:2] == b"PK"})
            if entry["is_zip"]:
                wb = load_workbook(raw)
                entry["sheets"] = wb.sheetnames
                ws = wb[wb.sheetnames[0]]
                first = next(ws.iter_rows(values_only=True), ())
                entry["first_sheet_dims"] = f"{ws.max_row}x{ws.max_column}"
                entry["header_sample"] = [str(v)[:24] for v in first[:6]]
                wb.close()
        except Exception as exc:
            entry["error"] = repr(exc)[:300]
            report["automatable"] = False
        report["files"].append(entry)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SPF medians (values outside git) and RTDSM probe")
    ap.add_argument("--values-out", default=str(VALUES_DEFAULT),
                    help="git-ignored path for the extracted median values")
    ap.add_argument("--out", default=str(METADATA_DEFAULT),
                    help="metadata file committed to the repository (no values)")
    ap.add_argument("--min-year", type=int, default=1990)
    ap.add_argument("--check-rtdsm", action="store_true",
                    help="probe RTDSM xlsx availability and print metadata; writes nothing")
    args = ap.parse_args(argv)

    if args.check_rtdsm:
        print(json.dumps(check_rtdsm(), ensure_ascii=False, indent=1))
        return 0

    series = []
    file_meta = []
    for key, spec in FILES.items():
        raw = fetch(spec["url"])
        rows, header = parse_medians(raw, spec["sheet"], spec["variables"], args.min_year)
        series.extend(rows)
        file_meta.append({"key": key, "name": spec["name"], "url": spec["url"],
                          "sheet": spec["sheet"], "columns_used": spec["variables"],
                          "sheet_header": header,
                          "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                          "rows_used": len(rows)})
    series.sort(key=lambda r: (r["survey_date"], r["code"]))
    quarters = sorted({r["survey_quarter"] for r in series})
    # values -> git-ignored data directory only
    values_path = Path(args.values_out)
    values_path.parent.mkdir(parents=True, exist_ok=True)
    values_abs = values_path.resolve() if values_path.is_absolute() else (Path.cwd() / values_path).resolve()
    if values_abs.is_relative_to(ROOT):
        values_location = str(values_abs.relative_to(ROOT)).replace("\\", "/")
    else:
        values_location = "outside the repository (git-ignored by policy)"
    values_path.write_text(json.dumps({"dataset": "SPF median forecasts (third-party values; not "
                                                  "committed, not published)",
                                       "source_url": SOURCE_PAGE,
                                       "retrieved_at": utc_now(), "series": series},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    by_code = {}
    for row in series:
        by_code.setdefault(row["code"], []).append(row)
    variables = []
    for code in [*FILES["inflation"]["variables"], *FILES["growth"]["variables"]]:
        rows = by_code.get(code, [])
        variables.append({"code": code, "variable": HORIZONS[code]["variable"],
                          "horizon": HORIZONS[code]["horizon"],
                          "observations": len(rows),
                          "first_survey_quarter": rows[0]["survey_quarter"] if rows else None,
                          "last_survey_quarter": rows[-1]["survey_quarter"] if rows else None,
                          "values_sha256": value_list_sha(rows) if rows else None})
    doc = {
        "dataset": "Survey of Professional Forecasters - median forecasts (metadata only)",
        "tool": "tools/fetch_spf.py",
        "source_url": SOURCE_PAGE,
        "retrieved_at": utc_now(),
        "licence_register_entry": "PHILADELPHIA_FED",
        "licence_reading": "research use only, no use of the Bank's names or logos as endorsement "
                           "(see protocols/licence_register.json; not a legal opinion)",
        "attribution": "Source: Federal Reserve Bank of Philadelphia, Survey of Professional "
                       "Forecasters, https://www.philadelphiafed.org/surveys-and-data/"
                       "real-time-data-research/survey-of-professional-forecasters",
        "publication_rule": "the SPF median values are third-party data: they are written only to the "
                            "git-ignored data directory and are never committed or published; only "
                            "this metadata file (URLs, hashes, shapes, date ranges) is committed",
        "raw_files_committed": False,
        "values_location": values_location,
        "files": file_meta,
        "variables": variables,
        "coverage": {"first_survey_quarter": quarters[0] if quarters else None,
                     "last_survey_quarter": quarters[-1] if quarters else None,
                     "survey_quarters": len(quarters), "observations": len(series),
                     "first_survey_date": min((r["survey_date"] for r in series), default=None),
                     "last_survey_date": max((r["survey_date"] for r in series), default=None)},
        "limitations": [
            "survey_date is an anchor (first day of the middle month of the survey quarter); the exact "
            "SPF deadline day comes from the release calendar and is not machine-parsed here",
            "real GDP growth horizons keep the published SPF horizon codes drgdp2..drgdp6; the mapping "
            "from code to quarter offset must be read from the SPF documentation before scoring",
            "CPI inflation is published by the SPF as INFPGDP1YR/INFCPI1YR/INFCPI10YR only - there is "
            "no monthly year-over-year CPI series in this file (see the licence register note)",
            "the median values themselves are not committed; reproduce them by running this tool "
            "(workflow .github/workflows/r2-metadata.yml) and reading the git-ignored output",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metadata": str(out), "values": str(values_path), **doc["coverage"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
