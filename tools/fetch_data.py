#!/usr/bin/env python3
"""Automated data retrieval for AMFE (protocol v4, owner decisions D-131/D-132).

No human download step. Two modes, both rate-limited and logged:
  * latest file: GET the public FRED CSV endpoint (fredgraph.csv?id=...);
  * vintage set: GET the ALFRED download-data page once (it lists the actual vintage dates), map
    every planned date of protocols/data_plan.json to the latest actual vintage date **at or before**
    that planned date (never a future date), request the resulting vintage columns in batches of at
    most VINTAGE_CHUNK_DATES dates per POST (the download form takes a limited number of dates per
    request), unzip each answer and merge all columns into one data/raw/<SERIES>/vintages.zip: a
    single CSV with one column per vintage, sorted observation dates. The merged archive is checked
    with vintage_check: no vintage column may contain an observation later than its vintage date.

A dense plan (quarter-end dates 1990-2026) therefore yields ~147 vintage columns per series, which
is what a REAL_TIME evaluation needs. The R2 gate (tools/verify_data_gate.py) posts its own form
request and is subject to the same per-request date limit; the tool additionally writes a compact
spot-check view (data/raw/gate/<SERIES>/vintages.zip, at most GATE_VIEW_DATES dated columns chosen
across 1990-2026 with at least GATE_VIEW_PRE_2000 of them before 2000) so the gate keeps checking a
representative subset of the dense archive without changing its frozen code.

Raw files are written outside the repository (data/ is git-ignored); only metadata goes into the
data log. The FRED API service is not used. This is the project's reading of the terms in
protocols/licence_register.json (FRED_ALFRED_SERVICES); it is not legal advice.
"""
from __future__ import annotations

import argparse
import bisect
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

VINTAGE_CHUNK_DATES = 40   # documented practical limit of the download form per POST request
GATE_VIEW_DATES = 36       # dated columns in the compact R2 gate view (the gate posts them itself)
GATE_VIEW_PRE_2000 = 18    # of which before 2000, so the gate keeps its pre-2000 pairs
QUARTER_END_MONTHS = (3, 6, 9, 12)


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


def latest_actual_on_or_before(planned_date: str, available: list) -> str | None:
    """Latest actual ALFRED vintage date <= planned_date (``available`` must be sorted)."""
    index = bisect.bisect_right(available, planned_date) - 1
    return available[index] if index >= 0 else None


def map_planned_dates(planned: list, available: list) -> list:
    """Map each planned date to the latest actual vintage date at or before it.

    A vintage later than the planned date would leak revisions that did not exist yet, so the
    mapping never looks forward (unlike the old same-year rule). Planned dates earlier than the
    first actual vintage of the series stay unmapped; see :func:`unmapped_planned_dates`.
    Duplicates (two planned dates inside one revision gap) are collapsed, result is sorted.
    """
    ordered = sorted(available)
    mapped = set()
    for planned_date in planned:
        actual = latest_actual_on_or_before(planned_date, ordered)
        if actual is not None:
            mapped.add(actual)
    return sorted(mapped)


def unmapped_planned_dates(planned: list, available: list) -> list:
    """Planned dates with no actual vintage at or before them (e.g. GS10/TB3MS before 1996-12)."""
    ordered = sorted(available)
    return [d for d in planned if latest_actual_on_or_before(d, ordered) is None]


def chunk_dates(dates: list, size: int = VINTAGE_CHUNK_DATES) -> list:
    """Split vintage dates into POST-sized chunks, preserving order."""
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [list(dates[i:i + size]) for i in range(0, len(dates), size)]


def vintage_date_of_column(column_name: str) -> str | None:
    """ISO vintage date of an ALFRED vintage CSV column (``<SERIES>_<YYYYMMDD>`` or ISO)."""
    name = column_name.strip()
    tag = name.rsplit("_", 1)[-1]
    if len(tag) == 8 and tag.isdigit():
        return f"{tag[0:4]}-{tag[4:6]}-{tag[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        return name
    return None


def read_vintage_zip(raw: bytes) -> dict:
    """Parse one (possibly merged) zipped vintage CSV into columns and values.

    Returns ``{"columns": [iso, ...], "values": {iso: {period: value}}, "periods": [period, ...]}``.
    Values are kept as the published strings (no float conversion), missing cells are dropped.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv") and "readme" not in n.lower()]
        if not names:
            raise RuntimeError("no CSV inside the vintage zip")
        text = zf.read(names[0]).decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        raise RuntimeError("empty vintage CSV")
    columns: list = []
    index: dict = {}
    for i, name in enumerate(header[1:], start=1):
        iso = vintage_date_of_column(name)
        if iso is None or iso in index:
            continue
        index[iso] = i
        columns.append(iso)
    values = {iso: {} for iso in columns}
    periods: list = []
    for row in reader:
        if not row or not row[0].strip():
            continue
        period = row[0].strip()[:10]
        periods.append(period)
        for iso in columns:
            i = index[iso]
            if i < len(row):
                cell = row[i].strip()
                if cell not in ("", "."):
                    values[iso][period] = cell
    return {"columns": columns, "values": values, "periods": periods}


def pick_evenly(items: list, count: int) -> list:
    """Deterministic even spread: first and last are always kept."""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    last = len(items) - 1
    positions = sorted({round(i * last / (count - 1)) for i in range(count)})
    return [items[p] for p in positions]


def build_vintage_zip(series_id: str, vintages: list, periods: list, values: dict,
                      note: str = "") -> bytes:
    """Write one CSV (one column per vintage, observation_date sorted) plus a README into a zip."""
    if not vintages:
        raise RuntimeError(f"{series_id}: refusing to write a vintage archive with no columns")
    ordered = sorted(vintages)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["observation_date"] + [f"{series_id}_{v.replace('-', '')}" for v in ordered])
    for period in sorted(periods):
        writer.writerow([period] + [values.get(v, {}).get(period, "") for v in ordered])
    csv_bytes = buffer.getvalue().encode("utf-8")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"vintages_starting_{ordered[0]}.csv", csv_bytes)
        zf.writestr("README.txt", (
            "-------------------------------------------------------------------\n"
            f"Series ID: {series_id}\n"
            "Archival Federal Reserve Economic Data (ALFRED), Federal Reserve Bank of St. Louis\n"
            f"Link: https://alfred.stlouisfed.org/series?seid={series_id}\n"
            "Output Format: Observations by Vintage Date, All Observations (merged columns)\n"
            f"Vintages: {len(ordered)} ({ordered[0]} .. {ordered[-1]})\n"
            f"Observation rows: {len(periods)}\n"
            f"Created: {utc_now()}\n"
            f"Note: {note}\n"
            "-------------------------------------------------------------------\n"
        ).encode("utf-8"))
    return payload.getvalue()


def merge_vintage_parts(parts: list, series_id: str) -> tuple:
    """Merge parsed chunk results into one zip: unique sorted observation dates, all vintage columns."""
    values: dict = {}
    periods: set = set()
    for part in parts:
        for vintage_date, observations in part["values"].items():
            values.setdefault(vintage_date, {}).update(observations)
        periods.update(part["periods"])
    vintages = sorted(values)
    raw = build_vintage_zip(series_id, vintages, list(periods), values,
                            note="merged from chunked ALFRED download-data requests")
    return raw, {"vintages": vintages, "vintage_count": len(vintages), "rows": len(periods)}


def vintage_meta(zip_bytes: bytes) -> dict:
    """Per-vintage last observation and the point-in-time rule for a merged archive."""
    part = read_vintage_zip(zip_bytes)
    vintages = sorted(part["values"])
    last_obs: dict = {}
    for vintage_date in vintages:
        observations = [p for p in part["values"][vintage_date]]
        last_obs[vintage_date] = max(observations) if observations else None
    check = all(last is None or last <= vintage_date for vintage_date, last in last_obs.items())
    return {"vintages": vintages, "vintage_count": len(vintages), "rows": len(part["periods"]),
            "vintage_last_observation": last_obs, "vintage_check": check}


def post_vintage_form(seid: str, dates: list, first_obs: str, last_obs: str, ua: str) -> bytes:
    """POST one chunk of vintage dates to the ALFRED download-data form; return the zipped CSV."""
    fields = [
        ("form[units]", "lin"),
        ("form[obs_start_date]", first_obs),
        ("form[obs_end_date]", last_obs),
        ("form[entered_vintage_dates]", " ".join(dates)),
        ("form[file_type]", "2"),        # Observations by Vintage Date, All Observations
        ("form[file_format]", "csv"),    # Zipped CSV
        ("form[download_data]", ""),
    ]
    body = urllib.parse.urlencode(fields).encode()
    raw = request(ALFRED_FORM.format(seid=seid), ua, data=body)
    if raw[:2] != b"PK":
        head = raw[:200].decode("utf-8", "replace").replace("\n", " | ")
        raise RuntimeError(f"ALFRED download form did not return a zip for {len(dates)} dates; "
                           f"head={head!r}")
    return raw


def fetch_vintages(seid: str, planned: list, first_obs: str, last_obs: str, ua: str,
                   rate: float = 2.0) -> tuple:
    """Retrieve the mapped vintage columns in chunks and merge them into one zipped CSV.

    Returns ``(merged_zip, actual_dates, available_dates, unmapped_planned, chunks_meta, merged_meta)``.
    A chunk that the form rejects is split in halves down to a single date before the series fails.
    """
    html = request(ALFRED_FORM.format(seid=seid), ua).decode("utf-8", "replace")
    available = page_vintage_dates(html)
    actual = map_planned_dates(planned, available)
    unmapped = unmapped_planned_dates(planned, available)
    if not actual:
        raise RuntimeError(f"no actual ALFRED vintage date at or before the planned dates "
                           f"(planned {len(planned)}, available {available[:3]})")
    parts: list = []
    chunks: list = []

    def fetch_parts(dates: list, depth: int = 0) -> list:
        try:
            raw = post_vintage_form(seid, dates, first_obs, last_obs, ua)
        except RuntimeError:
            if len(dates) == 1 or depth >= 6:
                raise
            middle = len(dates) // 2
            time.sleep(rate)
            return fetch_parts(dates[:middle], depth + 1) + fetch_parts(dates[middle:], depth + 1)
        time.sleep(rate)
        part = read_vintage_zip(raw)
        chunks.append({"index": len(chunks) + 1, "requested_dates": len(dates),
                       "first_requested": dates[0], "last_requested": dates[-1],
                       "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                       "vintage_count": len(part["columns"]), "rows": len(part["periods"]),
                       "split": depth > 0})
        return [part]

    for group in chunk_dates(actual):
        parts.extend(fetch_parts(group))
    merged_raw, merged = merge_vintage_parts(parts, seid)
    missing = [d for d in actual if d not in merged["vintages"]]
    if missing:
        raise RuntimeError(f"merged archive is missing {len(missing)} requested vintage columns: "
                           f"{missing[:3]}")
    return merged_raw, actual, available, unmapped, chunks, merged


def subset_vintage_zip(raw: bytes, series_id: str, limit: int = GATE_VIEW_DATES,
                       pre_2000_limit: int = GATE_VIEW_PRE_2000) -> tuple:
    """Compact spot-check view of a merged archive for the R2 gate (bounded number of columns).

    The gate posts all dated columns of a series in one form request, so the view keeps the number
    of columns inside the form's per-request limit while spanning the whole 1990-2026 range and
    keeping at least ``pre_2000_limit`` columns before 2000.
    """
    part = read_vintage_zip(raw)
    vintages = sorted(part["values"])
    if not vintages:
        raise RuntimeError("the merged archive has no dated vintage columns")
    pre_2000 = [d for d in vintages if d < "2000-01-01"]
    after_2000 = [d for d in vintages if d >= "2000-01-01"]
    chosen = pick_evenly(pre_2000, min(pre_2000_limit, len(pre_2000)))
    chosen += pick_evenly(after_2000, max(0, limit - len(chosen)))
    chosen = sorted(chosen)
    values = {v: part["values"][v] for v in chosen}
    raw_out = build_vintage_zip(series_id, chosen, part["periods"], values,
                                note="compact R2 gate spot-check view of the dense vintage archive")
    return raw_out, {"dates": chosen, "vintage_count": len(chosen), "rows": len(part["periods"])}


def build_gate_view(data_dir: Path, plan: dict) -> tuple:
    """Write data/raw/gate/<SERIES>/vintages.zip views from the merged archives; return metadata."""
    gate_dir = data_dir / "gate"
    entries: list = []
    failures: list = []
    for s in plan["series"]:
        if not s.get("vintages"):
            continue
        source = data_dir / s["id"] / "vintages.zip"
        if not source.exists():
            failures.append({"id": s["id"], "vintage": "gate_view",
                             "reason": "no merged vintage archive to build the gate view from"})
            continue
        try:
            raw, meta = subset_vintage_zip(source.read_bytes(), s["id"])
            destination = gate_dir / s["id"] / "vintages.zip"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            entries.append({
                "series": s["id"], "file": str(destination).replace("\\", "/"),
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "vintage_count": meta["vintage_count"], "rows": meta["rows"],
                "vintage_dates": meta["dates"],
            })
        except Exception as exc:
            failures.append({"id": s["id"], "vintage": "gate_view", "reason": str(exc)[:400]})
    return entries, failures


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

    entries: list = []
    failures: list = []
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
            except Exception as exc:
                failures.append({"id": s["id"], "vintage": "latest", "reason": str(exc)[:400]})
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
            print(f"[{s['id']}] vintages: planned {len(planned)} dates "
                  f"({planned[0]} .. {planned[-1]})", flush=True)
            try:
                raw, actual, available, unmapped, chunks, merged = fetch_vintages(
                    s["id"], planned, latest_meta["first_observation"],
                    latest_meta["last_observation"], ua, rate)
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
                    "unmapped_planned_dates": unmapped,
                    "available_vintage_dates": available,
                    "vintage_count": meta["vintage_count"], "rows": meta["rows"],
                    "vintage_last_observation": meta["vintage_last_observation"],
                    "vintage_check": meta["vintage_check"], "is_csv": True,
                    "chunks": chunks,
                    "merged": {"from_chunks": len(chunks), "vintage_count": merged["vintage_count"],
                               "rows": merged["rows"],
                               "sha256": hashlib.sha256(raw).hexdigest()},
                })
            except Exception as exc:
                failures.append({"id": s["id"], "vintage": "set", "reason": str(exc)[:400]})

    log = {
        "log_id": "AMFE-DATA-LOG",
        "tool": "tools/fetch_data.py",
        "protocol": "protocols/evaluation_protocol_v4.json",
        "generated_at": utc_now(),
        "no_human_download": True,
        "include_vintages": args.include_vintages,
        "rate_limit_seconds": rate,
        "plan": str(args.plan).replace("\\", "/"),
        "plan_version": plan.get("version"),
        "retrieval": {
            "mapping": "planned date -> latest actual ALFRED vintage date at or before it",
            "chunk_dates_per_request": VINTAGE_CHUNK_DATES,
            "merged_archive": "one zipped CSV per series, one column per vintage, sorted observation_date",
            "gate_view_dates": GATE_VIEW_DATES,
            "gate_view_pre_2000": GATE_VIEW_PRE_2000,
        },
        "entries": entries,
        "failures": failures,
    }
    if args.include_vintages and not args.dry_run:
        gate_entries, gate_failures = build_gate_view(out, plan)
        log["gate_view"] = {"path": str(out / "gate").replace("\\", "/"),
                            "limit_dates": GATE_VIEW_DATES,
                            "pre_2000_limit": GATE_VIEW_PRE_2000,
                            "entries": gate_entries}
        failures.extend(gate_failures)
    if not args.dry_run:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                            newline="\n")
    print(json.dumps({"fetched": len(entries), "failed": len(failures), "log": str(log_path)},
                     ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
