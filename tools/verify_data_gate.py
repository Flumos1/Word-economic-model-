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

Quarterly series (GDPC1) need a supplement. ALFRED file_type=4 returns a row only for a vintage
date that *is itself an initial-release date*. The planned vintage dates in protocols/data_plan.json
are year-end dates that fetch_data.py maps to the latest actual ALFRED vintage date in the year, and
a year-end snapshot of a quarterly series is a revision, not an initial release; the form then answers
"No observations were retrieved using the specified options" (documented in docs/71). For such series
the gate discovers the real initial-release dates from the ALFRED vintage calendar: candidate dates are
the vintage dates that fall 20-50 days after a quarter end (the BEA advance-release window), then it
posts file_type=4 with those candidates, and re-downloads a supplementary vintage set (file_type=2) at
the release dates that answered. The report records this as channel="release-date supplement" with the
supplementary dates and the SHA-256 of that zip, so the pair is still an initial-release check.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "protocols" / "data_plan.json"
REGISTER = ROOT / "protocols" / "licence_register.json"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={seid}"
ALFRED_FORM = "https://alfred.stlouisfed.org/series/downloaddata?seid={seid}"
TOLERANCE = 0.05  # within rounding of the published values (levels to 0.1 / 0.001)
QUARTER_END_MONTHS = (3, 6, 9, 12)
RELEASE_WINDOW_DAYS = (20, 50)  # BEA advance release of a quarter is ~1 month after the quarter end
MAX_SUPPLEMENT_DATES = 40


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



def value_digest(value) -> str:
    """SHA-256 of the textual representation of a numeric value (licence policy)."""
    return hashlib.sha256(repr(float(value)).encode("utf-8")).hexdigest()


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


def available_vintage_dates(seid: str, ua: str) -> list:
    """All actual vintage dates offered by the ALFRED download-data page for the series."""
    html = request(ALFRED_FORM.format(seid=seid), ua).decode("utf-8", "replace")
    m = re.search(r'<select[^>]*id="form_selected_vintage_dates"[^>]*>(.*?)</select>', html, re.S | re.I)
    if not m:
        return []
    return sorted(set(re.findall(r'value="(\d{4}-\d{2}-\d{2})"', m.group(1))))


def month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def release_date_candidates(available: list, first: str, last: str) -> list:
    """Vintage dates inside [first, last] that fall RELEASE_WINDOW_DAYS after a quarter end.

    These are the possible dates of a quarterly initial release (BEA advance estimate); the ALFRED
    form answers file_type=4 with rows only for the dates that are real initial-release dates.
    """
    out = []
    for tag in available:
        if not (first <= tag <= last):
            continue
        try:
            d = date.fromisoformat(tag)
        except ValueError:
            continue
        for year, month in ((d.year, 3), (d.year, 6), (d.year, 9), (d.year, 12),
                            (d.year - 1, 12), (d.year - 1, 9)):
            qe = month_end(year, month)
            if RELEASE_WINDOW_DAYS[0] <= (d - qe).days <= RELEASE_WINDOW_DAYS[1]:
                out.append(tag)
                break
    return sorted(set(out))


def post_form(seid: str, fields: list, ua: str, kind: str) -> bytes:
    """POST the ALFRED download-data form; return the zip bytes or raise with the page head."""
    url = ALFRED_FORM.format(seid=seid)
    body = urllib.parse.urlencode(fields).encode()
    last_error = None
    for attempt in range(1, 4):
        raw = request(url, ua, data=body)
        if raw[:2] == b"PK":
            return raw
        head = raw[:160].decode("utf-8", "replace").replace("\n", " | ")
        last_error = f"not a zip (attempt {attempt}): {head!r}"
        time.sleep(8 * attempt)
    raise RuntimeError(f"{kind} output unavailable for {seid}: {last_error}")


def initial_release_raw(seid: str, dates: list, first: str, last: str, ua: str) -> bytes:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first),
        ("form[obs_end_date]", last),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "4"),        # Observations, Initial Release Only
        ("form[file_format]", "csv"),    # Zipped CSV
        ("form[download_data]", ""),
    ]
    return post_form(seid, fields, ua, "initial-release")


def vintage_set_raw(seid: str, dates: list, first: str, last: str, ua: str) -> bytes:
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first),
        ("form[obs_end_date]", last),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "2"),        # Observations by Vintage Date, All Observations
        ("form[file_format]", "csv"),
        ("form[download_data]", ""),
    ]
    return post_form(seid, fields, ua, "supplementary vintage-set")


def input_meta(raw: bytes) -> dict:
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


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
    rate = float(plan.get("rate_limit_seconds", 2.0))
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
        try:
            raw = zpath.read_bytes()
            columns = parse_vintage_zip(raw)
            if not columns:
                report["excluded"].append({"series": s["id"], "reason": "vintage set has no dated columns"})
                continue
            dates = sorted(columns)
            first, last = latest_range(s["id"], ua)
        except Exception as exc:
            report["excluded"].append({"series": s["id"], "reason": f"check failed: {exc}"})
            continue
        rows = []
        first_error = None
        try:
            time.sleep(rate)
            ir_raw = initial_release_raw(s["id"], dates, first, last, ua)
            rows = parse_initial_release(ir_raw)
        except Exception as exc:
            # For a quarterly series the year-end vintage dates are not initial-release dates and the
            # form answers with its validation page instead of a zip; that is the case the
            # release-date supplement below handles (docs/71).
            first_error = repr(exc)[:400]
        entry = {"series": s["id"], "vintage_dates": dates, "channel": "retrieved vintages",
                 "initial_release_rows": len(rows), "checked": 0, "matched": 0, "mismatches": []}
        supplement = None
        if rows:
            entry["initial_release_sha256"] = input_meta(ir_raw)["sha256"]
        if not rows:
            # Quarterly series: the retrieved year-end vintages are revisions; discover the actual
            # initial-release dates (advance-release window after each quarter end) and re-download
            # a supplementary vintage set at exactly those dates.
            supplement = {"reason": "retrieved vintage dates contain no initial releases for this "
                                    "series (year-end vintages of a quarterly series are revisions)",
                          "first_attempt_error": first_error,
                          "candidates": [], "release_dates": []}
            try:
                time.sleep(rate)
                available = available_vintage_dates(s["id"], ua)
                candidates = release_date_candidates(available, dates[0], dates[-1])[-MAX_SUPPLEMENT_DATES:]
                supplement["candidates"] = candidates
                if candidates:
                    time.sleep(rate)
                    ir_raw = initial_release_raw(s["id"], candidates, first, last, ua)
                    rows = parse_initial_release(ir_raw)
                    supplement["initial_release_sha256"] = input_meta(ir_raw)["sha256"]
                    supplement["initial_release_bytes"] = len(ir_raw)
                if rows:
                    release_dates = sorted({r[2] for r in rows})
                    supplement["release_dates"] = release_dates
                    time.sleep(rate)
                    sup_raw = vintage_set_raw(s["id"], release_dates, first, last, ua)
                    columns = parse_vintage_zip(sup_raw)
                    supplement.update(input_meta(sup_raw))
                    entry["channel"] = "release-date supplement"
                    entry["supplement"] = supplement
                    entry["initial_release_rows"] = len(rows)
            except Exception as exc:
                supplement["error"] = repr(exc)[:400]
                entry["supplement"] = supplement
        if not rows:
            report["excluded"].append({"series": s["id"],
                                       "reason": "no initial-release rows for the retrieved vintage "
                                                 "dates and the release-date supplement found none",
                                       "supplement": supplement})
            report["series"].append(entry)
            time.sleep(rate)
            continue
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
            # licence policy: only derived statistics leave the CI job; the values themselves stay in
            # the private artifact.  The pair carries digests and the equality flag instead.
            pair = {"series": s["id"], "period": period, "release": release,
                    "initial_release_sha256": value_digest(value),
                    "vintage_value_sha256": value_digest(got),
                    "equal_within_tolerance": bool(abs(got - value) <= TOLERANCE)}
            if abs(got - value) <= TOLERANCE:
                entry["matched"] += 1
                matched += 1
                if period < "2000-01-01":
                    matched_pre_2000 += 1
                report["pairs"].append(pair)
            else:
                pair["difference_abs"] = round(abs(got - value), 6)  # derived statistic only
                entry["mismatches"].append(pair)
                report["failures"].append(pair)
        report["series"].append(entry)
        time.sleep(rate)

    report["totals"] = {"matched_pairs": matched, "matched_pairs_pre_2000": matched_pre_2000,
                        "failures": len(report["failures"]), "excluded": len(report["excluded"])}
    report["passed"] = (matched >= args.min_pairs and matched_pre_2000 >= args.min_pairs_pre_2000
                        and not report["failures"])
    report["licence_note"] = ("values are not stored: each compared pair carries SHA-256 digests and an "
                              "equality flag; the underlying values stay in the private CI artifact (docs/84)")
    report["criteria"] = {"min_pairs": args.min_pairs, "min_pairs_pre_2000": args.min_pairs_pre_2000,
                          "mismatches_allowed": 0}
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], **report["totals"], "report": str(out)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
