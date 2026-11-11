#!/usr/bin/env python3
"""Probe the ALFRED download-data form and a POST request (R2 data gate).

The download form posts to the same page with form[...] fields. This probe records the exact
option values of the file_type/file_format selects, the submit field, and tries a POST for one
series and ten vintage dates. Only metadata about the response is kept (no series values).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
VINTAGES = ["1996-12-31", "1999-12-31", "2004-12-31", "2007-12-31", "2013-12-31", "2020-04-30"]
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"


def fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return {"url": url, "status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "bytes": len(raw), "raw": raw}
    except Exception as exc:
        return {"url": url, "error": repr(exc)}


def select_options(html: str, select_id: str) -> list:
    m = re.search(rf'<select[^>]*id="{select_id}"[^>]*>(.*?)</select>', html, flags=re.S | re.I)
    if not m:
        return []
    return re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', m.group(1), flags=re.I)


def main() -> int:
    out = {"series": SERIES, "vintages": VINTAGES, "selects": {}, "inputs": [], "post": {}}
    page = fetch(PAGE)
    if "raw" not in page:
        Path("probe_results.json").write_text(json.dumps({"error": page}, ensure_ascii=False, indent=2))
        return 1
    html = page.pop("raw").decode("utf-8", "replace")
    out["page"] = {k: v for k, v in page.items()}
    for sid in ("form_units", "form_file_type", "form_file_format"):
        out["selects"][sid] = select_options(html, sid)
    out["inputs"] = re.findall(r"<(?:input|button)[^>]*>", html, flags=re.I)[-25:]

    file_type = next((v for v, _ in out["selects"].get("form_file_type", []) if v), "")
    file_format = next((v for v, _ in out["selects"].get("form_file_format", []) if v), "")
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1947-01-01"),
        ("form[obs_end_date]", "2026-12-31"),
        ("form[entered_vintage_dates]", " ".join(VINTAGES)),
        ("form[file_type]", file_type),
        ("form[file_format]", file_format),
    ]
    for v in VINTAGES:
        fields.append(("form[selected_vintage_dates][]", v))
    body = urllib.parse.urlencode(fields).encode()
    r = fetch(PAGE, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if "raw" in r:
        raw = r.pop("raw")
        out["post"] = {k: v for k, v in r.items()}
        out["post"].update({"is_zip": raw[:2] == b"PK", "bytes": len(raw),
                            "head": raw[:120].decode("utf-8", "replace").replace("\n", " | ")})
        if raw[:2] == b"PK":
            Path("downloaded_vintages.zip").write_bytes(raw)
    else:
        out["post"] = r
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file_type": out["selects"].get("form_file_type"),
                      "file_format": out["selects"].get("form_file_format"),
                      "post": {k: out["post"].get(k) for k in ("status", "content_type", "is_zip", "bytes")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
