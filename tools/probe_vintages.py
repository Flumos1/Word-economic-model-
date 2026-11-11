#!/usr/bin/env python3
"""Probe ALFRED/FRED endpoints for genuine vintage-file retrieval (R2 data gate).

The first vintage run showed that vintage_date on fredgraph.csv returned the latest series, so
this probe finds the working endpoint: it fetches the ALFRED series page, extracts links that
mention a vintage, and tries a list of candidate URL patterns, recording status, size and the
last observation of each response. The output is metadata only (no data is stored).
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

UA = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
SERIES = "CPIAUCSL"
VINTAGE = "1999-12-31"

CANDIDATES = [
    f"https://alfred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}&vintage_date={VINTAGE}",
    f"https://alfred.stlouisfed.org/graph/fredgraph.xls?id={SERIES}&vintage_date={VINTAGE}",
    f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}&vintage_date={VINTAGE}",
    f"https://alfred.stlouisfed.org/series?seid={SERIES}&vintage_date={VINTAGE}",
    f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}&vintage_date={VINTAGE}&revision_date={VINTAGE}",
    f"https://fred.stlouisfed.org/data/{SERIES}.txt",
    f"https://alfred.stlouisfed.org/data/{SERIES}.txt",
]

PAGES = [
    f"https://alfred.stlouisfed.org/series?seid={SERIES}",
    f"https://alfred.stlouisfed.org/series/downloaddata?seid={SERIES}",
]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            status = resp.status
    except Exception as exc:
        return {"url": url, "error": repr(exc)}
    out = {"url": url, "status": status, "content_type": ctype, "bytes": len(data)}
    text = data.decode("utf-8", "replace")
    out["head"] = text[:300].replace("\n", " | ")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and "," in lines[0]:
        dates = [ln.split(",")[0].strip() for ln in lines[1:] if ln.strip()]
        out.update({"csv": True, "rows": len(dates),
                    "first_observation": dates[0] if dates else None,
                    "last_observation": dates[-1] if dates else None})
    return out


def main() -> int:
    results = {"series": SERIES, "target_vintage": VINTAGE, "pages": [], "candidates": []}
    for page in PAGES:
        item = fetch(page)
        if "head" in item:
            html = urllib.request.urlopen(urllib.request.Request(page, headers={"User-Agent": UA}), timeout=60).read().decode("utf-8", "replace")
            links = sorted(set(re.findall(r'href="([^"]*(?:vintage|download|fredgraph)[^"]*)"', html)))
            item["links"] = links[:40]
            item["link_count"] = len(links)
        results["pages"].append(item)
    for url in CANDIDATES:
        results["candidates"].append(fetch(url))
    Path("probe_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pages": len(results["pages"]), "candidates": len(results["candidates"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
