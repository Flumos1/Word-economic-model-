#!/usr/bin/env python3
"""Automated data retrieval for AMFE (protocol v4, owner decision D-132).

No human download step: the tool fetches the registered OPEN series from the public
FRED/ALFRED download endpoints, one series per request, rate-limited, with a project
User-Agent and no API key or API cache. Raw files are written outside the repository
(data/ is git-ignored); only URL, retrieval date and SHA-256 go into the data log.

The FRED API service is not used. This is the project's reading of the terms in
protocols/licence_register.json (FRED_ALFRED_SERVICES); it is not legal advice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "protocols" / "data_plan.json"
REGISTER = ROOT / "protocols" / "licence_register.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def register_statuses() -> dict:
    reg = load_json(REGISTER)
    return {e["id"]: e["status"] for e in reg["entries"]}


def fetch(url: str, user_agent: str, timeout: int = 60, retries: int = 3) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Automated retrieval of registered OPEN series")
    ap.add_argument("--out", default="data/raw", help="ignored output directory for raw files")
    ap.add_argument("--log", default="data_log.json", help="data log path (metadata only)")
    ap.add_argument("--plan", default=str(PLAN))
    ap.add_argument("--include-vintages", action="store_true",
                    help="also fetch the vintage dates listed in the plan")
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
    series = plan["series"]
    for s in series:
        if statuses.get(s["register"]) != "OPEN":
            failures.append({"id": s["id"], "reason": f"register status {statuses.get(s['register'])}"})
            continue
        targets = [("latest", s["url"])]
        if args.include_vintages:
            for date in s.get("vintages", []):
                targets.append((date, f"{s['url']}&vintage_date={date}"))
        for label, url in targets:
            print(f"[{s['id']}] {label}: {url}", flush=True)
            if args.dry_run:
                entries.append({"series": s["id"], "vintage": label, "url": url, "dry_run": True})
                continue
            try:
                data = fetch(url, ua)
            except RuntimeError as exc:
                failures.append({"id": s["id"], "vintage": label, "reason": str(exc)})
                continue
            folder = out / s["id"]
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{label}.csv"
            path.write_bytes(data)
            entries.append({
                "series": s["id"],
                "register": s["register"],
                "vintage": label,
                "url": url,
                "retrieved_at": utc_now(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "file": str(path).replace("\\", "/"),
            })
            time.sleep(rate)

    log = {
        "log_id": "AMFE-DATA-LOG",
        "tool": "tools/fetch_data.py",
        "protocol": "protocols/evaluation_protocol_v4.json",
        "generated_at": utc_now(),
        "no_human_download": True,
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
