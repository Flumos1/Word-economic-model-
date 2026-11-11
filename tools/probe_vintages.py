#!/usr/bin/env python3
"""Probe the ALFRED download-data form (R2 data gate).

The public help page describes the form but not its URL parameters. This probe extracts the
form action, field names and option values from the raw HTML of the download page and tries a
few likely request shapes. Only metadata is stored; no series values are recorded.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
VINTAGE = "1999-12-31"
PAGE = f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}"


def fetch(url: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return {"url": url, "status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "bytes": len(raw), "raw": raw}
    except Exception as exc:
        return {"url": url, "error": repr(exc)}


def describe(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    forms = re.findall(r"<form[^>]*>", text, flags=re.I)
    inputs = re.findall(r"<(?:input|select|textarea)[^>]*>", text, flags=re.I)[:120]
    options = re.findall(r"<option[^>]*>[^<]*</option>", text, flags=re.I)[:40]
    return {"forms": forms, "inputs": inputs, "options": options}


def main() -> int:
    out = {"series": SERIES, "vintage": VINTAGE, "page": {}, "attempts": []}
    page = fetch(PAGE)
    if "raw" in page:
        raw = page.pop("raw")
        out["page"] = {k: v for k, v in page.items() if k != "raw"}
        out["page"].update(describe(raw))
        # snippet around the first form
        text = raw.decode("utf-8", "replace")
        i = text.lower().find("<form")
        out["page"]["snippet"] = text[max(0, i - 200): i + 4000] if i >= 0 else text[:2000]
    attempts = [
        ("GET", f"{PAGE}&vintage_date={VINTAGE}&file_format=csv"),
        ("GET", f"{PAGE}&vintage_dates={VINTAGE}&file_format=csv"),
        ("GET", f"{PAGE}&vintage_date={VINTAGE}&file_format=text&output_format=1"),
        ("GET", f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}&vintage_dates={VINTAGE}&output_format=1&file_format=2"),
    ]
    for method, url in attempts:
        r = fetch(url)
        item = {"method": method, "url": url}
        if "raw" in r:
            raw = r.pop("raw")
            item.update({k: v for k, v in r.items() if k != "raw"})
            item["is_zip"] = raw[:2] == b"PK"
            item["head"] = raw[:120].decode("utf-8", "replace").replace("\n", " | ")
        else:
            item.update(r)
        out["attempts"].append(item)
    Path("probe_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"forms": len(out["page"].get("forms", [])),
                      "inputs": len(out["page"].get("inputs", [])),
                      "attempts": len(out["attempts"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
