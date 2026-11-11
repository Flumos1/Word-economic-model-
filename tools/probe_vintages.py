#!/usr/bin/env python3
"""Probe ALFRED initial-release/real-time outputs and surface validation messages."""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"
ACTUAL = ["1996-12-12", "1999-12-14", "2004-12-17", "2007-12-14", "2013-12-17", "2020-12-10"]


def post(file_type: str, file_format: str, label: str, use_selected: bool = False) -> dict:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
    ]
    if use_selected:
        fields += [("form[selected_vintage_dates][]", d) for d in ACTUAL]
    else:
        fields.append(("form[entered_vintage_dates]", " ".join(ACTUAL)))
    fields += [("form[file_type]", file_type), ("form[file_format]", file_format),
               ("form[download_data]", "")]
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(PAGE, headers={"User-Agent": UA}, data=body)
    with urllib.request.urlopen(req, timeout=240) as resp:
        raw = resp.read()
    item = {"label": label, "file_type": file_type, "file_format": file_format,
            "selected": use_selected, "bytes": len(raw), "is_zip": raw[:2] == b"PK"}
    if item["is_zip"]:
        Path(f"probe_initial_{label}.zip").write_bytes(raw)
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
        text = raw.decode("utf-8", "replace")
        snippet = re.sub(r"\s+", " ", text)
        errors = re.findall(r"(?i)(please[^.<]{0,160}|invalid[^.<]{0,160}|error[^.<]{0,160}|"
                            r"must[^.<]{0,160}|cannot[^.<]{0,160}|not a vintage[^.<]{0,120})", snippet)
        item["messages"] = errors[:12]
        item["head"] = snippet[:160]
    return item


def main() -> int:
    out = {"actual_dates": ACTUAL, "attempts": []}
    attempts = [
        ("ft2_entered_csv", "2", "csv", False),
        ("ft1_entered_csv", "1", "csv", False),
        ("ft1_entered_xlsx", "1", "xlsx", False),
        ("ft4_entered_csv", "4", "csv", False),
        ("ft4_entered_xlsx", "4", "xlsx", False),
        ("ft4_selected_csv", "4", "csv", True),
        ("ft1_selected_csv", "1", "csv", True),
    ]
    for label, ft, ff, sel in attempts:
        try:
            out["attempts"].append(post(ft, ff, label, sel))
        except Exception as exc:
            out["attempts"].append({"label": label, "error": repr(exc)})
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: a.get(k) for k in ("label", "is_zip", "bytes", "header", "row_count", "messages")}
                      for a in out["attempts"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
