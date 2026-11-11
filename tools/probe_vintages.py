#!/usr/bin/env python3
"""Probe the ALFRED download form POST variants (R2 data gate).

The form posts to /series/downloaddata?seid=... with form[...] fields and a submit button
named form[download_data]. This probe tries several request shapes and reports which one
returns a zip file. Only metadata about the responses is kept; if a zip is returned it is
saved as an artifact (that is the data-gate evidence).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
# real ALFRED vintage (release) dates of the series, from the download page
VINTAGES = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-04-10"]
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"


def fetch(url: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
            return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                    "bytes": len(raw), "raw": raw}
    except Exception as exc:
        return {"error": repr(exc)}


def post(fields: list, label: str) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    r = fetch(PAGE, data=body)
    item = {"label": label, "fields": {k: v for k, v in fields}}
    if "raw" in r:
        raw = r.pop("raw")
        item.update(r)
        item["is_zip"] = raw[:2] == b"PK"
        text = raw.decode("utf-8", "replace")
        item["head"] = text[:200].replace("\n", " | ")
        if not item["is_zip"]:
            errors = re.findall(r"(?i)(error[^<\n]{0,120}|invalid[^<\n]{0,120}|required[^<\n]{0,80})", text)
            item["errors"] = errors[:10]
        return item
    item.update(r)
    return item


def base_fields(file_type: str, file_format: str, entered: str) -> list:
    return [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", "2026-10-01"),
        ("form[entered_vintage_dates]", entered),
        ("form[file_type]", file_type),
        ("form[file_format]", file_format),
    ]


def main() -> int:
    entered = " ".join(VINTAGES)
    variants = []
    f = base_fields("2", "csv", entered) + [("form[download_data]", "")]
    variants.append(("entered+csv+button", f))
    f = base_fields("2", "csv", entered)
    f += [("form[selected_vintage_dates][]", v) for v in VINTAGES]
    f += [("form[download_data]", "Download data")]
    variants.append(("entered+selected+csv+button", f))
    f = base_fields("2", "xlsx", entered) + [("form[download_data]", "")]
    variants.append(("entered+xlsx+button", f))
    out = {"series": SERIES, "vintages": VINTAGES, "attempts": []}
    for label, fields in variants:
        item = post(fields, label)
        if item.get("is_zip"):
            Path(f"vintages_{label.split('+')[0]}_{label.split('+')[1]}.zip").write_bytes(item.pop("raw"))
        out["attempts"].append(item)
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "status", "content_type", "is_zip", "bytes")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
