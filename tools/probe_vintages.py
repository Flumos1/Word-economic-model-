#!/usr/bin/env python3
"""Probe the ALFRED 'Initial Release Only' output with actual vintage dates (R2 gate)."""
from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"
ACTUAL = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-12-10"]


def post(file_type: str, file_format: str, dates: list, label: str) -> dict:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", file_type),
        ("form[file_format]", file_format),
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(PAGE, headers={"User-Agent": UA}, data=body)
    with urllib.request.urlopen(req, timeout=240) as resp:
        raw = resp.read()
    item = {"label": label, "bytes": len(raw), "is_zip": raw[:2] == b"PK"}
    if item["is_zip"]:
        Path(f"probe_initial_{label}.zip").write_bytes(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            item["files"] = names
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            if csv_names:
                text = zf.read(csv_names[0]).decode("utf-8-sig", "replace")
                lines = text.splitlines()
                item["header"] = lines[0]
                item["sample"] = lines[1:4]
                item["row_count"] = len(lines) - 1
    else:
        item["head"] = raw[:140].decode("utf-8", "replace").replace("\n", " | ")
    return item


def main() -> int:
    out = {"series": SERIES, "actual_dates": ACTUAL, "attempts": []}
    for label, ft, ff, dates in [
        ("initial_csv_actual", "4", "csv", ACTUAL),
        ("initial_csv_nodates", "4", "csv", []),
        ("realtime_csv_actual", "1", "csv", ACTUAL),
    ]:
        try:
            out["attempts"].append(post(ft, ff, dates, label))
        except Exception as exc:
            out["attempts"].append({"label": label, "error": repr(exc)})
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "is_zip", "bytes", "header", "row_count", "head")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
