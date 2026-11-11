#!/usr/bin/env python3
"""Probe the ALFRED 'Initial Release Only' output (R2 data gate evidence)."""
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


def post(file_type: str, file_format: str, label: str) -> dict:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        ("form[entered_vintage_dates]", "1996-12-31 2000-12-31 2010-12-31 2020-12-31"),
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
    return item


def main() -> int:
    out = {"series": SERIES, "attempts": []}
    for label, ft, ff in [
        ("initial_csv", "4", "csv"),
        ("initial_xlsx", "4", "xlsx"),
        ("realtime_period_csv", "1", "csv"),
    ]:
        try:
            out["attempts"].append(post(ft, ff, label))
        except Exception as exc:
            out["attempts"].append({"label": label, "error": repr(exc)})
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "is_zip", "bytes", "header", "row_count")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
