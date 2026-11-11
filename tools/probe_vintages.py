#!/usr/bin/env python3
"""Probe ALFRED initial-release/real-time outputs with the series' own observation range."""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"
ACTUAL = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-12-10"]


def fetch(url: str, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, data=data)
    with urllib.request.urlopen(req, timeout=240) as resp:
        return resp.read()


def series_range() -> tuple:
    text = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}").decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
    return lines[0].split(",")[0].strip(), lines[-1].split(",")[0].strip()


def post(file_type: str, file_format: str, label: str, first: str, last: str) -> dict:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first),
        ("form[obs_end_date]", last),
        ("form[entered_vintage_dates]", " ".join(ACTUAL)),
        ("form[file_type]", file_type),
        ("form[file_format]", file_format),
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    raw = fetch(PAGE, body)
    item = {"label": label, "file_type": file_type, "file_format": file_format,
            "bytes": len(raw), "is_zip": raw[:2] == b"PK"}
    if item["is_zip"]:
        Path(f"probe_output_{label}.zip").write_bytes(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            item["files"] = names
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            if csv_names:
                lines = zf.read(csv_names[0]).decode("utf-8-sig", "replace").splitlines()
                item["header"] = lines[0]
                item["sample"] = lines[1:3]
                item["row_count"] = len(lines) - 1
    else:
        snippet = re.sub(r"\s+", " ", raw.decode("utf-8", "replace"))
        item["messages"] = re.findall(r"(?i)(please[^.<]{0,120}|invalid[^.<]{0,120}|"
                                      r"error[^.<]{0,120}|must[^.<]{0,120})", snippet)[:8]
    return item


def main() -> int:
    first, last = series_range()
    out = {"series": SERIES, "range": [first, last], "attempts": []}
    for label, ft, ff in [
        ("ft2_control_csv", "2", "csv"),
        ("ft4_initial_csv", "4", "csv"),
        ("ft4_initial_xlsx", "4", "xlsx"),
        ("ft1_realtime_csv", "1", "csv"),
        ("ft3_newrevised_csv", "3", "csv"),
    ]:
        try:
            out["attempts"].append(post(ft, ff, label, first, last))
        except Exception as exc:
            out["attempts"].append({"label": label, "error": repr(exc)})
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "is_zip", "bytes", "header", "row_count", "files")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
