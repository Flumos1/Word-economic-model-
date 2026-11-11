#!/usr/bin/env python3
"""Probe the ALFRED download form for a series, including its initial-release (file_type=4) output.

Diagnostic tool for the R2 data gate (docs/68, docs/71). It answers one question: which parameters of
the ALFRED download-data form make the form return a zip instead of the validation page?

The form answers with the HTML page and the message "No observations were retrieved using the
specified options" when the requested combination has no data. That is exactly what happened for
GDPC1 (quarterly real GDP) with the year-end vintage dates in protocols/data_plan.json: file_type=4
returns a row only for a vintage date that is itself an initial-release date, and a year-end snapshot of
a quarterly series is a revision, not an initial release. This probe tests, in one run:

  * file_type=2 (control, by vintage date) at the retrieved vintage dates - must be a zip;
  * file_type=4 at the retrieved vintage dates - the documented failure;
  * file_type=4 at the *release-window* dates found on the ALFRED vintage calendar
    (the vintage dates that fall 20-50 days after a quarter end, i.e. the BEA advance-release window);
  * file_type=4 at the exact release dates of a few known quarters (hand-picked control);
  * file_type=1 (by real-time period) and file_type=3 (new and revised) at the same dates;
  * csv and xlsx formats for the working variant.

The vintage dates used for "retrieved vintage dates" are read from protocols/data_log.json (the actual
dates that tools/fetch_data.py retrieved for the series), so the probe reproduces the gate conditions.
Only metadata and the zips it receives are written (probe_results.json, probe_output_*.zip); the probe
never writes into protocols/, and the workflow uploads the output as a short-lived artifact.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.parse
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from verify_data_gate import (  # noqa: E402  (sibling tool, one implementation of the form logic)
    ROOT,
    ALFRED_FORM,
    available_vintage_dates,
    latest_range,
    parse_initial_release,
    post_form,
    release_date_candidates,
    request,
)
from datetime import date, timedelta  # noqa: E402

DATA_LOG = ROOT / "protocols" / "data_log.json"
DEFAULT_SERIES = "GDPC1"
RATE_SECONDS = 2.0


def load_log() -> dict:
    try:
        return json.loads(DATA_LOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def retrieved_vintage_dates(log: dict, series: str) -> list:
    for entry in log.get("entries", []):
        if entry.get("series") == series and entry.get("vintage") == "set":
            return sorted(entry.get("vintage_dates") or [])
    return []


def describe(label: str, raw: bytes, fields: dict) -> dict:
    item = {"label": label, "fields": fields, "bytes": len(raw), "is_zip": raw[:2] == b"PK"}
    if item["is_zip"]:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            item["files"] = zf.namelist()
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if csv_names:
                lines = zf.read(csv_names[0]).decode("utf-8-sig", "replace").splitlines()
                item["header"] = lines[0]
                item["row_count"] = len(lines) - 1
                item["sample"] = lines[1:3]
                if item["header"].lower().startswith("period_start_date"):
                    rows = parse_initial_release(raw)
                    item["initial_release_rows"] = [
                        {"period": p, "value": v, "release": r} for p, v, r in rows[:6]]
                    item["initial_release_count"] = len(rows)
    else:
        text = raw.decode("utf-8", "replace")
        import re
        item["messages"] = [m.strip() for m in re.findall(
            r'(?is)(no observations were retrieved[^<.]{0,120}|please [^<.]{0,120}|'
            r'invalid [^<.]{0,120}|error[^<.]{0,120})', text)][:6]
    return item


def post(series: str, fields: list, ua: str, label: str) -> dict:
    try:
        raw = request(ALFRED_FORM.format(seid=series), ua,
                      data=urllib.parse.urlencode(fields).encode())
    except Exception as exc:
        return {"label": label, "error": repr(exc)[:300]}
    return describe(label, raw, dict(fields))


def quarter_end_before(d: date) -> date:
    for year, month in ((d.year, 3), (d.year, 6), (d.year, 9), (d.year, 12),
                        (d.year - 1, 12), (d.year - 1, 9)):
        if month == 12:
            qe = date(year, 12, 31)
        else:
            qe = date(year, month + 1, 1) - timedelta(days=1)
        if qe <= d:
            return qe
    return date(d.year - 1, 9, 30)


def known_release_dates(available: list, limit: int = 6) -> list:
    """Hand-picked control: available vintage dates nearest to 30 days after a quarter end."""
    scored = []
    for tag in available:
        try:
            d = date.fromisoformat(tag)
        except ValueError:
            continue
        scored.append((abs((d - quarter_end_before(d)).days - 30), tag))
    scored.sort()
    return sorted({tag for _, tag in scored[:limit]})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe ALFRED download-form parameters for one series")
    ap.add_argument("--series", default=DEFAULT_SERIES)
    ap.add_argument("--out", default="probe_results.json")
    ap.add_argument("--zip-dir", default=".", help="where to keep the zips the form returns")
    args = ap.parse_args(argv)

    log = load_log()
    ua = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
    retrieved = retrieved_vintage_dates(log, args.series)
    available = []
    error = None
    try:
        first, last = latest_range(args.series, ua)
    except Exception as exc:
        first, last, error = "1900-01-01", "2100-01-01", repr(exc)[:200]
    try:
        available = available_vintage_dates(args.series, ua)
    except Exception as exc:
        error = (error or "") + f" vintage calendar failed: {exc!r}"[:200]
    if not available:
        available = retrieved[:]
    candidates = release_date_candidates(available, available[0] if available else first,
                                         available[-1] if available else last)
    candidates = candidates[-12:]
    control_dates = known_release_dates(available)
    out = {"series": args.series, "observation_range": [first, last],
           "retrieved_vintage_dates": retrieved,
           "available_vintage_dates_count": len(available),
           "release_window_candidates_found": len(candidates),
           "release_window_candidates": candidates,
           "control_release_dates": control_dates,
           "error": error, "attempts": []}

    def fields(dates: list, file_type: str, file_format: str = "csv") -> list:
        return [
            ("form[units]", "lin"),
            ("form[obs_start_date]", first),
            ("form[obs_end_date]", last),
            ("form[entered_vintage_dates]", " ".join(dates)),
            ("form[file_type]", file_type),
            ("form[file_format]", file_format),
            ("form[download_data]", ""),
        ]

    plan = [
        ("retrieved_ft2_control_csv", retrieved, "2", "csv"),
        ("retrieved_ft4_initial_csv", retrieved, "4", "csv"),
        ("release_window_ft4_initial_csv", candidates, "4", "csv"),
        ("control_release_ft4_initial_csv", control_dates, "4", "csv"),
        ("control_release_ft4_initial_xlsx", control_dates, "4", "xlsx"),
        ("control_release_ft1_realtime_csv", control_dates, "1", "csv"),
        ("control_release_ft3_newrevised_csv", control_dates, "3", "csv"),
    ]
    import time
    for label, dates, file_type, file_format in plan:
        if not dates:
            out["attempts"].append({"label": label, "skipped": "no dates for this variant"})
            continue
        item = post(args.series, fields(dates, file_type, file_format), ua, label)
        item["dates_count"] = len(dates)
        out["attempts"].append(item)
        time.sleep(RATE_SECONDS)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = [{k: a.get(k) for k in ("label", "is_zip", "row_count", "initial_release_count",
                                      "messages", "skipped", "error")}
               for a in out["attempts"]]
    print(json.dumps({"series": args.series, "range": [first, last],
                      "retrieved_vintage_dates": len(retrieved),
                      "attempts": summary}, ensure_ascii=False))
    working = [a["label"] for a in out["attempts"] if a.get("is_zip")]
    print(json.dumps({"working_variants": working}, ensure_ascii=False))
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
