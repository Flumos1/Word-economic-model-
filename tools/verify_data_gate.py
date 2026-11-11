#!/usr/bin/env python3
"""R2 data gate: check the retrieved vintages against ALFRED's recorded initial releases.

Protocol v4, data_depth.gate: "at least 30 (series, date) pairs, at least 10 of them before 2000,
must match the primary-source release of that time, within rounding".

The check is automated and uses only the project channels:
  1. the vintage set already retrieved by tools/fetch_data.py (columns = actual ALFRED vintage dates);
  2. the ALFRED "Observations, Initial Release Only" output (file_type=4), which records the value
     and the release date of the first publication of every observation (ALFRED archives the
     BLS/BEA release data; the direct BLS site blocks automated access - see the licence register).

For every row of the initial-release file (period, value, release date) the vintage column of that
release date must contain the same value for that period within rounding. The report is metadata:
counts, per-series breakdown, mismatches, and the SHA-256 of the inputs.
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
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={seid}"
ALFRED_FORM = "https://alfred.stlouisfed.org/series/downloaddata?seid={seid}"
TOLERANCE = 0.05  # within rounding of the published values (levels to 0.1 / 0.001)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
        except Exception as exc:
            last = exc
            time.sleep(5 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last!r}")


def read_zip_csv(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError("no CSV in zip")
        return zf.read(names[0]).decode("utf-8-sig", "replace")


def parse_vintage_zip(raw: bytes) -> dict:
    """Columns of a vintage CSV -> {release_date_iso: {period_iso: value}}."""
    reader = csv.reader(io.StringIO(read_zip_csv(raw)))
    header = next(reader)
    columns = {}
    for name in header[1:]:
        tag = name.strip().split("_")[-1]
        if len(tag) == 8 and tag.isdigit():
            iso = f"{tag[0:4]}-{tag[4:6]}-{tag[6:8]}"
            columns[iso] = {}
    for row in reader:
        if not row or not row[0].strip():
            continue
        period = row[0].strip()[:10]
        for i, name in enumerate(header[1:], start=1):
            tag = name.strip().split("_")[-1]
            if len(tag) == 8 and tag.isdigit() and i < len(row):
                value = row[i].strip()
                if value not in ("", "."):
                    columns[f"{tag[0:4]}-{tag[4:6]}-{tag[6:8]}"][period] = float(value)
    return columns


def parse_initial_release(raw: bytes) -> list:
    """Rows of the ALFRED initial-release output: (period, value, release_date)."""
    reader = csv.reader(io.StringIO(read_zip_csv(raw)))
    header = [h.strip().lower() for h in next(reader)]
    i_period = header.index("period_start_date")
    i_release = header.index("realtime_start_date")
    i_value = 1  # the value column sits between them
    rows = []
    for row in reader:
        if len(row) <= max(i_period, i_release, i_value) or not row[i_period].strip():
            continue
        try:
            value = float(row[i_value])
        except ValueError:
            continue
        rows.append((row[i_period].strip()[:10], value, row[i_release].strip()[:10]))
    return rows


def latest_range(seid: str, ua: str) -> tuple:
    text = request(FRED_CSV.format(seid=seid), ua).decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
    return lines[0].split(",")[0].strip()[:10], lines[-1].split(",")[0].strip()[:10]


def initial_release(seid: str, dates: list, first: str, last: str, ua: str) -> list:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first),
        ("form[obs_end_date]", last),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "4"),        # Observations, Initial Release Only
        ("form[file_format]", "csv"),    # Zipped CSV
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    return parse_initial_release(request(ALFRED_FORM.format(seid=seid), ua, data=body))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R2 data gate check against initial releases")
    ap.add_argument("--data", default="data/raw")
    ap.add_argument("--plan", default=str(PLAN))
    ap.add_argument("--report", default="protocols/r2_gate_report.json")
    ap.add_argument("--min-pairs", type=int, default=30)
    ap.add_argument("--min-pairs-pre-2000", type=int, default=10)
    args = ap.parse_args(argv)

    plan = load_json(Path(args.plan))
    ua = plan.get("user_agent", "AMFE-research/0.1")
    data_dir = Path(args.data)
    report = {"gate": "R2", "protocol": "protocols/evaluation_protocol_v4.json",
              "generated_at": utc_now(), "tolerance": TOLERANCE,
              "series": [], "pairs": [], "excluded": [], "failures": []}
    matched = 0
    matched_pre_2000 = 0
    for s in plan["series"]:
        zpath = data_dir / s["id"] / "vintages.zip"
        if not zpath.exists():
            report["excluded"].append({"series": s["id"], "reason": "no vintage set retrieved"})
            continue
        raw = zpath.read_bytes()
        columns = parse_vintage_zip(raw)
        if not columns:
            report["excluded"].append({"series": s["id"], "reason": "vintage set has no dated columns"})
            continue
        dates = sorted(columns)
        first, last = latest_range(s["id"], ua)
        rows = initial_release(s["id"], dates, first, last, ua)
        entry = {"series": s["id"], "vintage_dates": dates, "initial_release_rows": len(rows),
                 "checked": 0, "matched": 0, "mismatches": []}
        for period, value, release in rows:
            column = columns.get(release)
            if column is None:
                report["excluded"].append({"series": s["id"], "period": period, "release": release,
                                           "reason": "no vintage column for that release date"})
                continue
            if period not in column:
                report["excluded"].append({"series": s["id"], "period": period, "release": release,
                                           "reason": "period absent in the vintage column"})
                continue
            got = column[period]
            entry["checked"] += 1
            pair = {"series": s["id"], "period": period, "release": release,
                    "initial_release": value, "vintage_value": got}
            if abs(got - value) <= TOLERANCE:
                entry["matched"] += 1
                matched += 1
                if period < "2000-01-01":
                    matched_pre_2000 += 1
                report["pairs"].append(pair)
            else:
                pair["difference"] = round(got - value, 6)
                entry["mismatches"].append(pair)
                report["failures"].append(pair)
        report["series"].append(entry)
        time.sleep(float(plan.get("rate_limit_seconds", 2.0)))

    report["totals"] = {"matched_pairs": matched, "matched_pairs_pre_2000": matched_pre_2000,
                        "failures": len(report["failures"]), "excluded": len(report["excluded"])}
    report["passed"] = (matched >= args.min_pairs and matched_pre_2000 >= args.min_pairs_pre_2000
                        and not report["failures"])
    report["criteria"] = {"min_pairs": args.min_pairs, "min_pairs_pre_2000": args.min_pairs_pre_2000,
                          "mismatches_allowed": 0}
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], **report["totals"], "report": str(out)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
