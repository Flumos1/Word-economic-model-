#!/usr/bin/env python3
"""Automated data retrieval for AMFE (protocol v4, owner decisions D-131/D-132).

No human download step. Two modes, both rate-limited and logged:
  * latest file: GET the public FRED CSV endpoint (fredgraph.csv?id=...);
  * vintage set: POST the ALFRED download-data form once per series. The planned year-end dates
    in protocols/data_plan.json are mapped to the latest ACTUAL ALFRED vintage date in the same
    year (the form rejects arbitrary dates), and the observation range is taken from the series'
    own first/last observation (the form rejects dates outside it). The response is a zip with one
    CSV whose columns are the vintages; the tool checks that no vintage column has an observation
    later than its vintage date.

Raw files are written outside the repository (data/ is git-ignored); only metadata goes into the
data log. The FRED API service is not used. This is the project's reading of the terms in
protocols/licence_register.json (FRED_ALFRED_SERVICES); it is not legal advice.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "protocols" / "data_plan.json"
REGISTER = ROOT / "protocols" / "licence_register.json"
ALFRED_FORM = "https://alfred.stlouisfed.org/series/downloaddata?seid={seid}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def register_statuses() -> dict:
    reg = load_json(REGISTER)
    return {e["id"]: e["status"] for e in reg["entries"]}


def request(url: str, user_agent: str, data: bytes | None = None, timeout: int = 240,
            retries: int = 3) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise urllib.error.HTTPError(url, resp.status, "non-200", resp.headers, None)
                return resp.read()
        except Exception as exc:  # network errors and HTTP errors
            last = exc
            wait = 5 * attempt
            print(f"  retry {attempt}/{retries} after {exc!r}; sleeping {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"could not fetch {url}: {last!r}")


def csv_meta(data: bytes) -> dict:
    """Row count and first/last observation of a plain FRED CSV file."""
    try:
        lines = data.decode("utf-8", "replace").strip().splitlines()
    except Exception:
        return {"rows": None, "last_observation": None, "csv": False}
    if not lines or "," not in lines[0]:
        return {"rows": None, "last_observation": None, "csv": False}
    dates = [row.split(",")[0].strip() for row in lines[1:] if row.strip()]
    return {"rows": len(dates), "first_observation": dates[0] if dates else None,
            "last_observation": dates[-1] if dates else None, "csv": True}


def page_vintage_dates(html: str) -> list:
    """All actual vintage dates offered on the ALFRED download-data page."""
    m = re.search(r'<select[^>]*id="form_selected_vintage_dates"[^>]*>(.*?)</select>', html, re.S | re.I)
    if not m:
        return []
    return sorted(set(re.findall(r'value="(\d{4}-\d{2}-\d{2})"', m.group(1))))


def map_planned_dates(planned: list, available: list) -> list:
    """For each planned date take the latest actual vintage date in the same year."""
    out = set()
    for planned_date in planned:
        year = planned_date[:4]
        same_year = [d for d in available if d.startswith(year)]
        if same_year:
            out.add(max(same_year))
    return sorted(out)


def fetch_vintages(seid: str, planned: list, first_obs: str, last_obs: str, ua: str) -> tuple:
    page_url = ALFRED_FORM.format(seid=seid)
    html = request(page_url, ua).decode("utf-8", "replace")
    available = page_vintage_dates(html)
    actual = map_planned_dates(planned, available)
    if not actual:
        actual = planned
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first_obs),
        ("form[obs_end_date]", last_obs),
        ("form[entered_vintage_dates]", " ".join(actual)),
        ("form[file_type]", "2"),        # Observations by Vintage Date, All Observations
        ("form[file_format]", "csv"),    # Zipped CSV
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    raw = request(page_url, ua, data=body)
    if raw[:2] != b"PK":
        head = raw[:200].decode("utf-8", "replace").replace("\n", " | ")
        raise RuntimeError(f"ALFRED download form did not return a zip; head={head!r}")
    return raw, actual, available


def vintage_meta(zip_bytes: bytes) -> dict:
    """Parse the vintage CSV inside the zip: per-vintage last observation and a real-time check."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv") and "readme" not in n.lower()]
        if not names:
            raise RuntimeError("no CSV inside the vintage zip")
        text = zf.read(names[0]).decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    vintages = [h.strip() for h in header[1:]]
    last_obs = {v: None for v in vintages}
    rows = 0
    for row in reader:
        if not row or not row[0].strip():
            continue
        rows += 1
        date = row[0].strip()
        for i, v in enumerate(vintages, start=1):
            if i < len(row) and row[i].strip() not in ("", "."):
                last_obs[v] = date
    check = all((last_obs[v] or "0000-00-00") <= v for v in vintages)
    return {"vintages": vintages, "vintage_count": len(vintages), "rows": rows,
            "vintage_last_observation": last_obs, "vintage_check": check}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Automated retrieval of registered OPEN series")
    ap.add_argument("--out", default="data/raw", help="ignored output directory for raw files")
    ap.add_argument("--log", default="protocols/data_log.json", help="data log path (metadata only)")
    ap.add_argument("--plan", default=str(PLAN))
    ap.add_argument("--include-vintages", action="store_true",
                    help="also fetch the vintage sets listed in the plan (ALFRED download form)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    plan = load_json(Path(args.plan))
    statuses = register_statuses()
    out = Path(args.out)
    log_path = Path(args.log)
    rate = float(plan.get("rate_limit_seconds", 2.0))
    ua = plan.get("user_agent", "AMFE-research/0.1")

    entries = []
    failures = []
    for s in plan["series"]:
        if statuses.get(s["register"]) != "OPEN":
            failures.append({"id": s["id"], "reason": f"register status {statuses.get(s['register'])}"})
            continue
        folder = out / s["id"]
        latest_meta = {}
        print(f"[{s['id']}] latest: {s['url']}", flush=True)
        if args.dry_run:
            entries.append({"series": s["id"], "vintage": "latest", "url": s["url"], "dry_run": True})
        else:
            try:
                data = request(s["url"], ua)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "latest.csv"
                path.write_bytes(data)
                latest_meta = csv_meta(data)
                entries.append({
                    "series": s["id"], "register": s["register"], "vintage": "latest",
                    "url": s["url"], "retrieved_at": utc_now(), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "file": str(path).replace("\\", "/"),
                    "rows": latest_meta["rows"], "last_observation": latest_meta["last_observation"],
                    "first_observation": latest_meta["first_observation"], "is_csv": latest_meta["csv"],
                })
            except RuntimeError as exc:
                failures.append({"id": s["id"], "vintage": "latest", "reason": str(exc)})
            time.sleep(rate)
        planned = s.get("vintages", [])
        if args.include_vintages and planned:
            if args.dry_run:
                entries.append({"series": s["id"], "vintage": "set",
                                "url": ALFRED_FORM.format(seid=s["id"]),
                                "planned_vintages": planned, "dry_run": True})
                continue
            if not latest_meta.get("first_observation"):
                failures.append({"id": s["id"], "vintage": "set",
                                 "reason": "latest file is needed to set the observation range"})
                continue
            print(f"[{s['id']}] vintages: planned {len(planned)} years", flush=True)
            try:
                raw, actual, available = fetch_vintages(
                    s["id"], planned, latest_meta["first_observation"],
                    latest_meta["last_observation"], ua)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "vintages.zip"
                path.write_bytes(raw)
                meta = vintage_meta(raw)
                entries.append({
                    "series": s["id"], "register": s["register"], "vintage": "set",
                    "url": ALFRED_FORM.format(seid=s["id"]), "retrieved_at": utc_now(),
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "file": str(path).replace("\\", "/"),
                    "planned_vintages": planned, "vintage_dates": actual,
                    "available_vintage_dates": available,
                    "vintage_count": meta["vintage_count"], "rows": meta["rows"],
                    "vintage_last_observation": meta["vintage_last_observation"],
                    "vintage_check": meta["vintage_check"], "is_csv": True,
                })
            except RuntimeError as exc:
                failures.append({"id": s["id"], "vintage": "set", "reason": str(exc)})
            time.sleep(rate)

    log = {
        "log_id": "AMFE-DATA-LOG",
        "tool": "tools/fetch_data.py",
        "protocol": "protocols/evaluation_protocol_v4.json",
        "generated_at": utc_now(),
        "no_human_download": True,
        "include_vintages": args.include_vintages,
        "rate_limit_seconds": rate,
        "plan": str(args.plan).replace("\\", "/"),
        "entries": entries,
        "failures": failures,
    }
    if not args.dry_run:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"fetched": len(entries), "failed": len(failures), "log": str(log_path)}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
