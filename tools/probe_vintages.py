#!/usr/bin/env python3
"""Probe the ALFRED download form POST variants (R2 data gate).

The form posts to /series/downloaddata?seid=... with form[...] fields and a submit button
named form[download_data]. This probe tries several request shapes and reports which one
returns a zip file. Metadata goes into probe_results.json; the zip itself is uploaded as an
artifact (it is the data-gate evidence).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
# real ALFRED vintage (release) dates of the series, taken from the download page
VINTAGES = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-04-10"]
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"


def fetch(url: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                    "raw": resp.read()}
    except Exception as exc:
        return {"error": repr(exc)}


def post(fields: list, label: str) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    r = fetch(PAGE, data=body)
    item = {"label": label, "fields": dict(fields)}
    if "raw" not in r:
        item.update(r)
        return item
    raw = r["raw"]
    item.update({"status": r["status"], "content_type": r["content_type"], "bytes": len(raw)})
    item["is_zip"] = raw[:2] == b"PK"
    if item["is_zip"]:
        Path(f"vintages_{label.replace('+', '_')}.zip").write_bytes(raw)
    else:
        text = raw.decode("utf-8", "replace")
        item["head"] = text[:200].replace("\n", " | ")
        item["errors"] = re.findall(
            r"(?i)(error[^<\n]{0,120}|invalid[^<\n]{0,120}|required[^<\n]{0,80})", text)[:10]
    return item


def base_fields(file_open: str, file_format: str, entered: str) -> list:
    return [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", "2026-10-01"),
        ("form[entered_vintage_dates]", entered),
        ("form[file_type]", file_open),
        ("form[file_format]", file_format),
    ]


def main() -> int:
    entered = " ".join(VINTAGES)
    variants = [
        ("entered+csv+button", base_fields("2", "csv", entered) + [("form[download_data]", "")]),
        ("entered+selected+csv+button",
         base_fields("2", "csv", entered)
         + [("form[selected_vintage_dates][]", v) for v in VINTAGES]
         + [("form[download_data]", "Download data")]),
        ("entered+xlsx+button", base_fields("2", "xlsx", entered) + [("form[download_data]", "")]),
    ]
    out = {"series": SERIES, "vintages": VINTAGES, "attempts": []}
    for label, fields in variants:
        out["attempts"].append(post(fields, label))
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "status", "content_type", "is_zip", "bytes")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
