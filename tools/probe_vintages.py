#!/usr/bin/env python3
"""Probe which ALFRED download-form fields break the zip response (R2 data gate).

Two differences between the working probe request and the failing tool request are tested:
the observation start date (1947-01-01 vs 1900-01-01) and the vintage dates (actual release
dates vs arbitrary year-end dates). Only metadata is stored.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
ACTUAL = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-04-10"]
ARBITRARY = ["1996-12-31", "1997-12-31", "1998-12-31", "1999-12-31", "2001-12-31", "2004-12-31",
             "2007-12-31", "2010-12-31", "2013-12-31", "2020-04-30"]
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"


def post(start: str, end: str, dates: list, label: str) -> dict:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", start),
        ("form[obs_end_date]", end),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "2"),
        ("form[file_format]", "csv"),
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(PAGE, headers={"User-Agent": UA}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except Exception as exc:
        return {"label": label, "error": repr(exc)}
    item = {"label": label, "status": 200, "content_type": ctype, "bytes": len(raw),
            "is_zip": raw[:2] == b"PK"}
    if item["is_zip"]:
        Path(f"probe_{label}.zip").write_bytes(raw)
    else:
        text = raw.decode("utf-8", "replace")
        item["errors"] = re.findall(
            r"(?i)(error[^<\n]{0,160}|invalid[^<\n]{0,160}|too [^<\n]{0,100}|required[^<\n]{0,100})", text)[:12]
        item["head"] = text[:160].replace("\n", " | ")
    return item


def main() -> int:
    variants = [
        ("A_actual_1947_1001", "1947-01-01", "2026-10-01", ACTUAL),
        ("B_arbitrary_1947_today", "1947-01-01", TODAY, ARBITRARY),
        ("C_actual_1900_today", "1900-01-01", TODAY, ACTUAL),
        ("D_arbitrary_1900_1001", "1900-01-01", "2026-10-01", ARBITRARY),
        ("E_arbitrary_1900_today", "1900-01-01", TODAY, ARBITRARY),
    ]
    out = {"today": TODAY, "attempts": []}
    for label, start, end, dates in variants:
        out["attempts"].append(post(start, end, dates, label))
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "is_zip", "bytes", "errors")} for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
