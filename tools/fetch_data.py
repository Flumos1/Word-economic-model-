#!/usr/bin/env python3
"""Automated data retrieval for AMFE (protocol v4, owner decisions D-131/D-132).

No human download step. Two modes, both rate-limited and logged:
  * latest files: GET the public FRED CSV endpoint (fredgraph.csv?id=...);
  * vintage sets: POST the ALFRED download-data form once per series with the vintage dates from
    protocols/data_plan.json (file_type "Observations by Vintage Date, All Observations", file
    format Zipped CSV). The response is a zip with one CSV whose columns are the vintages; the
    tool checks that no vintage column contains an observation later than its vintage date.

Raw files are written outside the repository (data/ is git-ignored); only metadata goes into the
data log (URL, date, bytes, SHA-256, rows, last observation, vintage check).

The FRED API service is not used. This is the project's reading of the terms in
protocols/licence_register.json (FRED_ALFRED_SERVICES); it is not legal advice.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
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
    """Row count and last observation date of a plain FRED CSV file."""
    try:
        lines = data.decode("utf-8", "replace").strip().splitlines()
    except Exception:
        return {"rows": None, "last_observation": None, "csv": False}
    if not lines or "," not in lines[0]:
        return {"rows": None, "last_observation": None, "csv": False}
    dates = [row.split(",")[0].strip() for row in lines[1:] if row.strip()]
    return {
        "rows": len(dates),
        "first_observation": dates[0] if dates else None,
        "last_observation": dates[-1] if dates else None,
        "csv": True,
    }


def fetch_latest(url: str, ua: str) -> bytes:
    return request(url, ua)


def fetch_vintages(seid: str, dates: list, ua: str) -> bytes:
    """One POST to the ALFRED download-data form -> zip with a vintage CSV."""
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", "1900-01-01"),
        ("form[obs_end_date]", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "2"),        # Observations by Vintage Date, All Observations
        ("form[file_format]", "csv"),    # Zipped CSV
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    raw = request(ALFRED_FORM.format(seid=seid), ua, data=body)
    if raw[:2] != b"PK":
        raise RuntimeError("ALFRED download form did not return a zip; check the form fields")
    return raw


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
        # latest file
        print(f"[{s['id']}] latest: {s['url']}", flush=True)
        if args.dry_run:
            entries.append({"series": s["id"], "vintage": "latest", "url": s["url"], "dry_run": True})
        else:
            try:
                data = fetch_latest(s["url"], ua)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "latest.csv"
                path.write_bytes(data)
                meta = csv_meta(data)
                entries.append({
                    "series": s["id"], "register": s["register"], "vintage": "latest",
                    "url": s["url"], "retrieved_at": utc_now(), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "file": str(path).replace("\\", "/"),
                    "rows": meta["rows"], "last_observation": meta["last_observation"],
                    "first_observation": meta["first_observation"], "is_csv": meta["csv"],
                })
            except RuntimeError as exc:
                failures.append({"id": s["id"], "vintage": "latest", "reason": str(exc)})
            time.sleep(rate)
        # vintage set
        dates = s.get("vintages", [])
        if args.include_vintages and dates:
            url = ALFRED_FORM.format(seid=s["id"])
            print(f"[{s['id']}] vintages {len(dates)}: {dates[0]} .. {dates[-1]}", flush=True)
            if args.dry_run:
                entries.append({"series": s["id"], "vintage": "set", "url": url,
                                "vintage_dates": dates, "dry_run": True})
                continue
            try:
                raw = fetch_vintages(s["id"], dates, ua)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "vintages.zip"
                path.write_bytes(raw)
                meta = vintage_meta(raw)
                entries.append({
                    "series": s["id"], "register": s["register"], "vintage": "set",
                    "url": url, "retrieved_at": utc_now(), "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "file": str(path).replace("\\", "/"),
                    "vintage_dates": dates, "vintage_count": meta["vintage_count"],
                    "rows": meta["rows"], "vintage_last_observation": meta["vintage_last_observation"],
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
