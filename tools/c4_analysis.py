#!/usr/bin/env python3
"""C4/W1 structural analysis: exact REAL_TIME/PSEUDO origin counts and the lost 1990-91 recession.

The first REAL_TIME out-of-sample evaluation (docs/76) could not pass C4 on W1: the protocol needs
12 recession origins in a window (admission.recession_conditioning.min_recession_months) and only 8
are REAL_TIME.  This tool makes the reason exact and offline-reproducible:

* reconstructs the evaluation origins of protocol v4 exactly as tools/windows.py does and cross-checks
  them against protocols/evaluation_origins.json;
* classifies every origin REAL_TIME / PSEUDO_REAL_TIME per target, horizon and window from the local
  ALFRED vintage archives (data/raw/<SERIES>/vintages.zip), falling back to the documented first
  vintage dates of docs/75 when the archives are not downloaded (they are git-ignored);
* counts recession origins under the two plausible readings of the C4 instrument: the origin month is
  an NBER recession month (protocol text) and the forecast window (t, t+h] overlaps a recession month
  (the reading used by tools/run_real_evaluation.py's mask);
* shows where the 1990-91 recession is lost: the series whose first vintage is later than the origin
  (blockers) and the exact months;
* evaluates neutral protocol v5 variants (PSEUDO allowance, mask clarification, threshold change,
  archive extension, minimal-input classification, NOT_APPLICABLE) without touching the frozen files.

Writes protocols/c4_analysis.json.  Deterministic: no wall clock, only the pinned input hashes and the
``--date`` label.  The frozen protocol v4, R3 and NF-SIM files are read only.

Usage:
    python tools/c4_analysis.py
    python tools/c4_analysis.py --data-root data/raw --date 2026-11-12
    python tools/c4_analysis.py --check          # rebuild and verify the recorded document
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
ORIGINS = ROOT / "protocols" / "evaluation_origins.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
CANDIDATES = ROOT / "protocols" / "candidates_2026-11-11.json"
DEFAULT_OUT = ROOT / "protocols" / "c4_analysis.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"
NBER_DATES = ROOT / "protocols" / "nber_dates.json"
ANALYSIS_DATE = "2026-11-12"

ARCHIVE_SERIES = ("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS")
# First ALFRED vintages available in the project archives (docs/75 §7, verified against
# data/raw/<SERIES>/vintages.zip when the data are present).  The dense plan asks for 147 quarterly
# dates from 1990-03-31; the unmapped early dates are exactly the structural C4 limit.
DOCUMENTED_FIRST_VINTAGE = {
    "CPIAUCSL": "1990-03-20",
    "CPILFESL": "1996-12-12",
    "GDPC1": "1991-12-20",
    "UNRATE": "1990-03-09",
    "GS10": "1996-12-03",
    "TB3MS": "1996-12-03",
}
ARCHIVE_PATTERN = re.compile(r"_(\d{4})(\d{2})(\d{2})$")


class C4AnalysisError(RuntimeError):
    """The analysis input is inconsistent."""


# --------------------------------------------------------------------------------- small date helpers
def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return month_end(total // 12, total % 12 + 1)


def month_iter(start: str, end: str):
    year, month = map(int, start.split("-")[:2])
    end_year, end_month = map(int, end.split("-")[:2])
    while (year, month) <= (end_year, end_month):
        yield month_end(year, month)
        month += 1
        if month == 13:
            year, month = year + 1, 1


def month_index(value: date) -> int:
    return value.year * 12 + value.month - 1


def label_of(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def sha256_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


# -------------------------------------------------------------------------------------- origins (v4)
def origins_for(protocol: dict, target: str, horizon: int, window: str) -> list[dict]:
    """Exactly the origin grid of tools/windows.py (protocol v4)."""
    windows = protocol["windows"]
    covid_start, covid_end = windows["origin_exclusion"]["covid_targets"].split(" to ")
    start = windows[window]["start"]
    end = windows[window]["end"]
    override = windows.get("target_overrides", {}).get(target, {})
    if window == "W1" and override.get("W1_start"):
        start = override["W1_start"]

    def in_covid(period: date) -> bool:
        return covid_start <= period.isoformat()[:7] <= covid_end

    out: list[dict] = []
    if target == "T2_GDP_GROWTH":
        step = 1 if horizon == 0 else 3
        for day in month_iter(start, end):
            year, quarter = day.year, (day.month - 1) // 3 + 1
            if step == 3 and day.month != quarter * 3:
                continue
            target_quarter = year * 4 + (quarter - 1) + horizon
            target_date = month_end(target_quarter // 4, target_quarter % 4 * 3 + 3)
            if not start <= target_date.isoformat()[:7] <= end:
                continue
            if in_covid(target_date):
                continue
            out.append({"origin": day.isoformat(), "target": target_date.isoformat()})
        return out
    if target == "T1_CPI_YOY":
        for day in month_iter(start, end):
            target_date = add_months(day, horizon - 1)
            if not start <= target_date.isoformat()[:7] <= end:
                continue
            if in_covid(target_date):
                continue
            out.append({"origin": day.isoformat(), "target": target_date.isoformat()})
        return out
    if target == "T3_RECESSION":
        for day in month_iter(start, end):
            interval = [add_months(day, step) for step in range(1, horizon + 1)]
            if not start <= interval[-1].isoformat()[:7] <= end:
                continue
            if any(in_covid(moment) for moment in interval):
                continue
            out.append({"origin": day.isoformat(), "target": interval[-1].isoformat()})
        return out
    raise C4AnalysisError(f"unknown target {target!r}")


def cross_check_origins(protocol: dict, origins_doc: dict) -> dict:
    """Compare the reconstructed grid with protocols/evaluation_origins.json."""
    mismatches = []
    checked = 0
    for target in protocol["targets"]:
        target_id = target["id"]
        horizons = target.get("horizons_months") or target.get("horizons_quarters")
        for window in ("W1", "W2", "W3"):
            for horizon in horizons:
                rows = origins_for(protocol, target_id, horizon, window)
                recorded = origins_doc["targets"][target_id][window][str(horizon)]
                checked += 1
                if len(rows) != recorded["count"]:
                    mismatches.append({"target": target_id, "window": window, "horizon": horizon,
                                       "reconstructed": len(rows), "recorded": recorded["count"]})
                elif rows and (rows[0]["origin"] != recorded["first"]["origin"]
                               or rows[-1]["origin"] != recorded["last"]["origin"]):
                    mismatches.append({"target": target_id, "window": window, "horizon": horizon,
                                       "reason": "first/last origin differs"})
    return {"pairs_checked": checked, "mismatches": mismatches, "ok": not mismatches}


# ------------------------------------------------------------------------------------ NBER chronology
def read_nber_reference_monthly(path: Path = NBER_DATES) -> dict:
    """Fallback chronology from the committed NBER reference dates (peak/trough pairs).

    Recession months are the months after the peak through the trough included, exactly the USREC
    convention used by the frozen protocol's T3 definition.  The file is committed metadata, so the
    analysis still works on a machine without the git-ignored data/raw archives.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    reference = [item for item in payload.get("reference_dates", [])
                 if item.get("peak") and item.get("trough")]
    if not reference:
        raise C4AnalysisError(f"no peak/trough reference dates in {path}")
    months = {}
    peaks = []
    for item in reference:
        peak_year, peak_month = map(int, item["peak"].split("-"))
        trough_year, trough_month = map(int, item["trough"].split("-"))
        peak = peak_year * 12 + peak_month - 1
        trough = trough_year * 12 + trough_month - 1
        for index in range(peak + 1, trough + 1):
            months[index] = 1
        peaks.append(peak)
    first = min(months) - 24
    last = max(months) + 24
    for index in range(first, last + 1):
        months.setdefault(index, 0)
    return months


def chronology(data_root: Path) -> tuple[dict, dict]:
    """USREC latest file when the git-ignored data are present, else the committed NBER dates."""
    usrec_path = data_root / "USREC" / "latest.csv"
    if usrec_path.exists():
        return read_usrec(usrec_path), {"source": "data/raw/USREC/latest.csv",
                                        "sha256": sha256_file(usrec_path)}
    return read_nber_reference_monthly(), {
        "source": "protocols/nber_dates.json",
        "sha256": sha256_file(NBER_DATES),
        "note": "fallback: USREC latest file is git-ignored and absent; committed NBER peak/trough "
                "reference dates reproduce the same recession months",
    }


def read_usrec(path: Path) -> dict:
    """Month index -> 0/1 from the latest USREC file."""
    if not path.exists():
        raise C4AnalysisError(f"USREC file not found: {path}")
    values = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                year, month = map(int, row["observation_date"][:7].split("-"))
                values[year * 12 + month - 1] = int(float(row["USREC"]))
            except (KeyError, ValueError):
                continue
    if not values:
        raise C4AnalysisError("USREC file has no rows")
    return values


def recession_episodes(usrec: dict) -> list[dict]:
    """Contiguous USREC runs: the peak month is the month before the run, the trough the last month."""
    months = sorted(usrec)
    episodes = []
    run: list[int] = []
    for index in months:
        if usrec[index] >= 0.5:
            run.append(index)
        elif run:
            episodes.append(run)
            run = []
    if run:
        episodes.append(run)
    out = []
    for run in episodes:
        first, last = run[0], run[-1]
        peak = first - 1 if first - 1 in usrec else None
        out.append({
            "peak": label_of(peak) if peak is not None else None,
            "trough": label_of(last),
            "first_recession_month": label_of(first),
            "last_recession_month": label_of(last),
            "months": len(run),
            "month_labels": [label_of(index) for index in run],
            "key": f"{label_of(peak) if peak is not None else '?'}_{label_of(last)}",
        })
    return out


def episode_of_month(episodes: list[dict]) -> dict:
    mapping = {}
    for episode in episodes:
        for label in episode["month_labels"]:
            mapping[label] = episode["key"]
    return mapping


def recession_mask(origins: list[dict], horizon: int, usrec: dict, definition: str) -> list[int]:
    """0/1 per origin under the requested C4 instrument reading.

    origin_month     - the origin month itself is an NBER recession month (protocol C4 text);
    window_overlap   - at least one recession month lies in (t, t+h] (the mask used by
                       tools/run_real_evaluation.py through amfe.forecast.data.usrec_event).
    """
    if definition not in ("origin_month", "window_overlap"):
        raise C4AnalysisError(f"unknown mask definition {definition!r}")
    out = []
    for row in origins:
        origin = parse_day(row["origin"])
        start = month_index(origin)
        if definition == "origin_month":
            value = usrec.get(start)
            out.append(1 if value is not None and value >= 0.5 else 0)
            continue
        value = 0
        for step in range(1, int(horizon) + 1):
            month_value = usrec.get(start + step)
            if month_value is None:
                break
            if month_value >= 0.5:
                value = 1
                break
        out.append(value)
    return out


def recession_months_in_window(origin_label: str, horizon: int, usrec: dict) -> list[str]:
    """Recession month labels inside (t, t+h]; empty when the horizon is zero."""
    origin = parse_day(origin_label)
    start = month_index(origin)
    labels = []
    for step in range(1, int(horizon) + 1):
        if usrec.get(start + step, 0) >= 0.5:
            labels.append(label_of(start + step))
    return labels


# ------------------------------------------------------------------------------------ vintage archives
def read_archive(series: str, data_root: Path) -> dict:
    """First/last vintage dates and column count of data/raw/<series>/vintages.zip."""
    path = data_root / series / "vintages.zip"
    if not path.exists():
        return {"series": series, "available": False, "path": None}
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not names:
            return {"series": series, "available": False, "path": str(path)}
        header = archive.read(names[0]).split(b"\n")[0].decode("utf-8")
    columns = header.split(",")[1:]
    dates = []
    for column in columns:
        match = ARCHIVE_PATTERN.search(column.strip())
        if match:
            year, month, day = (int(part) for part in match.groups())
            dates.append(date(year, month, day))
    dates.sort()
    return {
        "series": series,
        "available": bool(dates),
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256_file(path),
        "file": names[0],
        "vintages": len(dates),
        "first_vintage": dates[0].isoformat() if dates else None,
        "last_vintage": dates[-1].isoformat() if dates else None,
    }


def vintages_metadata(data_root: Path) -> dict:
    """Archive metadata per series plus the resolved first-vintage table used for classification."""
    archives = {series: read_archive(series, data_root) for series in ARCHIVE_SERIES}
    resolved = {}
    for series in ARCHIVE_SERIES:
        archive = archives[series]
        documented = DOCUMENTED_FIRST_VINTAGE[series]
        if archive["available"]:
            first = archive["first_vintage"]
            source = "archive"
            if first != documented:
                source = "archive_mismatch_documented"
        else:
            first = documented
            source = "documented_fallback"
        resolved[series] = {"first_vintage": first, "source": source,
                            "documented_first_vintage": documented}
    return {"archives": archives, "first_vintages": resolved,
            "all_archives_available": all(item["available"] for item in archives.values())}


# ---------------------------------------------------------------------------------- required inputs
def series_name(register_id: str) -> str | None:
    """Map a register id to an archive series: ALFRED:CPIAUCSL -> CPIAUCSL, NBER -> None."""
    if register_id.startswith("ALFRED:"):
        candidate = register_id.split(":", 1)[1]
        return candidate if candidate in ARCHIVE_SERIES else None
    return None


def required_inputs(protocol: dict, composition: dict | None, target: str,
                    horizon: int) -> dict:
    """Full (primary comparator) and minimal (target's own series) input sets for a pair."""
    baselines = {baseline["id"]: baseline for baseline in protocol["baselines"]}
    full: set[str] = set()
    if composition is not None:
        block = composition.get("targets", {}).get(target, {}).get("losses", {})
        loss_name = "Brier" if target == "T3_RECESSION" else "RMSE"
        by_horizon = block.get(loss_name, {}).get("by_horizon", {}).get(str(int(horizon)), {})
        member_ids = by_horizon.get("members") or []
        for member_id in member_ids:
            baseline = baselines.get(member_id, {})
            for register_id in baseline.get("inputs_by_target", {}).get(target, []):
                name = series_name(register_id)
                if name is not None:
                    full.add(name)
    if not full:  # fallback: every scored comparator member that applies to the target
        for baseline in protocol["baselines"]:
            if not baseline.get("comparator_member") or not baseline.get("scored", True):
                continue
            if target not in baseline.get("applies_to", []):
                continue
            for register_id in baseline.get("inputs_by_target", {}).get(target, []):
                name = series_name(register_id)
                if name is not None:
                    full.add(name)
    target_spec = next(item for item in protocol["targets"] if item["id"] == target)
    minimal = set()
    for series in target_spec.get("series", []):
        name = series if series in ARCHIVE_SERIES else series_name(series)
        if name is not None:
            minimal.add(name)
    for baseline_series in target_spec.get("series", []):
        if baseline_series in ARCHIVE_SERIES:
            minimal.add(baseline_series)
    return {"full": sorted(full), "minimal": sorted(minimal)}


def candidate_inputs(protocol: dict, candidates_doc: dict | None, target: str) -> list[str]:
    """Series needed by the registered candidates that apply to the target (diagnostic only)."""
    if not candidates_doc:
        return []
    baselines = {baseline["id"]: baseline for baseline in protocol["baselines"]}
    needed: set[str] = set()
    for candidate in candidates_doc.get("candidates", []):
        if target not in candidate.get("targets", []):
            continue
        for member_id in candidate.get("members", []):
            baseline = baselines.get(member_id, {})
            for register_id in baseline.get("inputs_by_target", {}).get(target, []):
                name = series_name(register_id)
                if name is not None:
                    needed.add(name)
    return sorted(needed)


def classify_origin(origin_day: date, series: list[str], first_vintages: dict) -> dict:
    """REAL_TIME when every required series has a vintage on or before the origin date."""
    blockers = []
    for series in series:
        first = parse_day(first_vintages[series]["first_vintage"])
        if first > origin_day:
            blockers.append({"series": series,
                             "first_vintage": first_vintages[series]["first_vintage"],
                             "source": first_vintages[series]["source"]})
    return {"real_time": not blockers, "blockers": blockers}


# ----------------------------------------------------------------------------------- analysis blocks
def analyse_pair(protocol: dict, usrec: dict, episodes: list[dict], first_vintages: dict,
                 composition: dict | None, candidates_doc: dict | None,
                 target: str, horizon: int, window: str) -> dict:
    origins = origins_for(protocol, target, horizon, window)
    inputs = required_inputs(protocol, composition, target, horizon)
    candidate_series = candidate_inputs(protocol, candidates_doc, target)
    episode_map = episode_of_month(episodes)
    rows = []
    for row in origins:
        day = parse_day(row["origin"])
        full = classify_origin(day, inputs["full"], first_vintages)
        minimal = classify_origin(day, inputs["minimal"], first_vintages)
        candidate = classify_origin(day, candidate_series, first_vintages) if candidate_series else {
            "real_time": True, "blockers": []}
        overlap_months = recession_months_in_window(row["origin"], horizon, usrec)
        overlap_episodes = sorted({episode_map[label] for label in overlap_months
                                   if label in episode_map})
        rows.append({
            "origin": row["origin"],
            "target": row["target"],
            "real_time_full": full["real_time"],
            "real_time_minimal": minimal["real_time"],
            "real_time_candidate": candidate["real_time"],
            "blockers": full["blockers"],
            "recession_origin_month": 1 if episode_map.get(row["origin"][:7]) else 0,
            "recession_origin_episode": episode_map.get(row["origin"][:7]),
            "recession_window_overlap": 1 if overlap_months else 0,
            "recession_window_months": overlap_months,
            "recession_window_episodes": overlap_episodes,
        })
    real_time_full = [row for row in rows if row["real_time_full"]]
    first_real_time = min((row["origin"] for row in real_time_full), default=None)
    counts = {
        "origins": len(rows),
        "real_time_full": len(real_time_full),
        "pseudo_full": len(rows) - len(real_time_full),
        "real_time_minimal": sum(1 for row in rows if row["real_time_minimal"]),
        "real_time_candidate": sum(1 for row in rows if row["real_time_candidate"]),
        "first_real_time_origin": first_real_time,
        "last_real_time_origin": max((row["origin"] for row in real_time_full), default=None),
    }
    recession = {}
    for definition in ("origin_month", "window_overlap"):
        flag = f"recession_{definition}"
        episode_field = ("recession_origin_episode" if definition == "origin_month"
                         else "recession_window_episodes")
        real_time_rows = [row for row in rows if row[flag] and row["real_time_full"]]
        all_rows = [row for row in rows if row[flag]]
        if definition == "origin_month":
            episodes = {row[episode_field] for row in real_time_rows if row[episode_field]}
        else:
            episodes = {episode for row in real_time_rows
                        for episode in row[episode_field]}
        recession[definition] = {
            "origins": len(all_rows),
            "real_time_full": len(real_time_rows),
            "pseudo_full": len(all_rows) - len(real_time_rows),
            "real_time_minimal": sum(1 for row in all_rows if row["real_time_minimal"]),
            "real_time_candidate": sum(1 for row in all_rows if row["real_time_candidate"]),
            "episodes": sorted(episodes),
        }
    minimum = protocol["admission"]["recession_conditioning"]["min_recession_months"]
    applies_to = next(item for item in protocol["admission"]["criteria"]
                      if item["id"] == "C4").get("applies_to", [])
    applies = target in applies_to
    used_definition = "window_overlap"
    used_count = recession[used_definition]["real_time_full"]
    if not applies:
        status = "NOT_APPLICABLE_TO_TARGET"
    elif used_count >= int(minimum):
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
    blocked_recession = {}
    for row in rows:
        if not row[f"recession_{used_definition}"]:
            continue
        for blocker in row["blockers"]:
            blocked_recession.setdefault(blocker["series"], {
                "first_vintage": blocker["first_vintage"], "origins_blocked": 0,
                "episodes": set()})
            blocked_recession[blocker["series"]]["origins_blocked"] += 1
            blocked_recession[blocker["series"]]["episodes"].update(row["recession_window_episodes"])
    blocked_recession = {series: dict(value, episodes=sorted(value["episodes"]))
                         for series, value in sorted(blocked_recession.items())}
    return {
        "window": window,
        "horizon": int(horizon),
        "counts": counts,
        "inputs": {"full": inputs["full"], "minimal": inputs["minimal"],
                   "candidate": candidate_series},
        "recession": recession,
        "recession_blockers_under_used_mask": blocked_recession,
        "c4_v4": {
            "applies": bool(applies),
            "mask_definition": used_definition,
            "recession_origins": used_count,
            "minimum": int(minimum),
            "met": bool(applies and used_count >= int(minimum)),
            "status": status,
        },
        "_rows": rows,
    }


def lost_1990_91(protocol: dict, analyses: dict, usrec: dict, episodes: list[dict],
                 first_vintages: dict) -> dict:
    """The exact 1990-91 loss: months, origins, series blockers and per-target impact."""
    target_episode = next((episode for episode in episodes
                           if episode["peak"] == "1990-07"), None)
    if target_episode is None:
        return {"episode": None, "note": "1990-91 recession not found in the chronology"}
    by_target = {}
    for target, windows in analyses.items():
        blocks = {}
        for window in ("W1", "W2"):
            for horizon, pair in windows[window].items():
                rows = pair["_rows"]
                episode_rows = [row for row in rows
                                if row["recession_window_overlap"]
                                and target_episode["key"] in row["recession_window_episodes"]]
                if not episode_rows:
                    continue
                blockers = {}
                for row in episode_rows:
                    for blocker in row["blockers"]:
                        blockers[blocker["series"]] = blocker["first_vintage"]
                blocks.setdefault(window, {})[horizon] = {
                    "origins_in_window": len(episode_rows),
                    "real_time_full": sum(1 for row in episode_rows if row["real_time_full"]),
                    "pseudo_full": sum(1 for row in episode_rows if not row["real_time_full"]),
                    "first_origin": episode_rows[0]["origin"],
                    "last_origin": episode_rows[-1]["origin"],
                    "series_blockers": blockers,
                }
        by_target[target] = blocks
    affected = {}
    for target, windows in analyses.items():
        for window, horizons in windows.items():
            for horizon, pair in horizons.items():
                blockers = pair["recession_blockers_under_used_mask"]
                for series, value in blockers.items():
                    if target_episode["key"] not in value["episodes"]:
                        continue
                    entry = affected.setdefault(series, {
                        "first_vintage": value["first_vintage"],
                        "pairs": [], "documented_first_vintage":
                            first_vintages[series]["documented_first_vintage"]})
                    entry["pairs"].append(f"{target}/{window}/h{horizon}")
    for series, entry in affected.items():
        entry["pairs"] = sorted(set(entry["pairs"]))
        entry["episode_origins_blocked"] = max(
            [value["origins_blocked"]
             for target, windows in analyses.items()
             for window, horizons in windows.items()
             for pair in horizons.values()
             for name, value in pair["recession_blockers_under_used_mask"].items()
             if name == series and target_episode["key"] in value["episodes"]], default=0)
    return {
        "episode": {key: target_episode[key] for key in
                    ("peak", "trough", "first_recession_month", "last_recession_month",
                     "months", "month_labels", "key")},
        "by_target": by_target,
        "affected_series": affected,
        "summary": ("1990-91 is lost for every pair whose REAL_TIME start is 1996-12: the ALFRED "
                    "vintages of CPILFESL (1996-12-12, needed by RW/AO/AR_BIC and by the candidate) "
                    "and of GS10/TB3MS (1996-12-03, needed by BVAR and PROBIT) do not exist before "
                    "1996-12, so the eight recession months 1990-08..1991-03 stay PSEUDO; T2 adds "
                    "GDPC1 (first vintage 1991-12-20)."),
    }


def variant_counts(protocol: dict, analyses: dict, usrec: dict, first_vintages: dict,
                   series_override: dict | None = None) -> dict:
    """Recompute C4 counts for one world: optional first-vintage overrides and mask choices."""
    override = series_override or {}
    out = {}
    for target, windows in analyses.items():
        out[target] = {}
        for window, horizons in windows.items():
            out[target][window] = {}
            for horizon, pair in horizons.items():
                origins = origins_for(protocol, target, int(horizon), window)
                firsts = {series: dict(value) for series, value in first_vintages.items()}
                for series, first in override.items():
                    if series in firsts:
                        firsts[series]["first_vintage"] = first
                rows = [parse_day(row["origin"]) for row in origins]
                full_inputs = pair["inputs"]["full"]
                real_time = [day for day in rows
                             if classify_origin(day, full_inputs, firsts)["real_time"]]
                count_by_definition = {}
                for definition in ("origin_month", "window_overlap"):
                    mask = recession_mask(origins, int(horizon), usrec, definition)
                    count_by_definition[definition] = {
                        "real_time_full": sum(1 for flag, day in zip(mask, rows)
                                              if flag and day in real_time),
                        "all_origins": sum(mask),
                    }
                out[target][window][str(horizon)] = count_by_definition
    return out


def variants(protocol: dict, analyses: dict, usrec: dict, first_vintages: dict,
             lost: dict) -> dict:
    v0 = variant_counts(protocol, analyses, usrec, first_vintages)
    minimum = int(protocol["admission"]["recession_conditioning"]["min_recession_months"])

    def flatten(counts: dict, definition: str) -> dict:
        table = {}
        for target, windows in counts.items():
            for window, horizons in windows.items():
                for horizon, by_definition in horizons.items():
                    key = f"{target}/{window}/{horizon}"
                    table[key] = by_definition[definition]["real_time_full"]
        return table

    v0_origin = flatten(v0, "origin_month")
    v0_window = flatten(v0, "window_overlap")
    pseudo_counts = {}
    episode_table = {}
    for target, windows in analyses.items():
        pseudo_counts[target] = {}
        episode_table[target] = {}
        for window, horizons in windows.items():
            pseudo_counts[target][window] = {}
            episode_table[target][window] = {}
            for horizon, pair in horizons.items():
                pseudo_counts[target][window][horizon] = pair["recession"]["window_overlap"]["origins"]
                episode_table[target][window][horizon] = len(
                    pair["recession"]["window_overlap"]["episodes"])
    scenarios = {
        "V4a_extend_rates_and_core_cpi": {"CPILFESL": "1990-03-20", "GS10": "1990-03-20",
                                          "TB3MS": "1990-03-20"},
        "V4b_extend_rates_core_cpi_gdp": {"CPILFESL": "1990-03-20", "GS10": "1990-03-20",
                                          "TB3MS": "1990-03-20", "GDPC1": "1990-03-20"},
        "V4c_extend_rates_only": {"GS10": "1990-03-20", "TB3MS": "1990-03-20"},
    }
    extended = {name: variant_counts(protocol, analyses, usrec, first_vintages, override)
                for name, override in scenarios.items()}
    return {
        "V0_v4_current": {
            "description": "REAL_TIME full-comparator origins, mask = (t, t+h] overlap, minimum 12",
            "origin_month_mask": v0_origin, "window_overlap_mask": v0_window,
            "minimum": minimum,
            "met_origin_month": {key: value >= minimum for key, value in v0_origin.items()},
            "met_window_overlap": {key: value >= minimum for key, value in v0_window.items()},
        },
        "V1_mask_origin_month": {
            "description": ("clarify the C4 instrument as 'the origin month is a recession month' "
                            "(protocol text) instead of the (t,t+h] overlap mask used by the runner"),
            "counts": v0_origin, "minimum": minimum,
            "met": {key: value >= minimum for key, value in v0_origin.items()},
            "changed_pairs": {key: value for key, value in v0_origin.items()
                              if value != v0_window[key]},
        },
        "V2_allow_pseudo_for_c4": {
            "description": ("allow PSEUDO_REAL_TIME origins in the C4 recession split only "
                            "(C4 is a robustness split, not a forecast-quality comparison)"),
            "counts_window_overlap": pseudo_counts,
            "counts_origin_month": {
                target: {window: {horizon: pair["recession"]["origin_month"]["origins"]
                                  for horizon, pair in horizons.items()}
                         for window, horizons in windows.items()}
                for target, windows in analyses.items()},
            "minimum": minimum,
            "met_window_overlap": {f"{target}/{window}/{horizon}": value >= minimum
                                   for target, windows in pseudo_counts.items()
                                   for window, horizons in windows.items()
                                   for horizon, value in horizons.items()},
            "met_origin_month": {
                f"{target}/{window}/{horizon}":
                    pair["recession"]["origin_month"]["origins"] >= minimum
                for target, windows in analyses.items()
                for window, horizons in windows.items()
                for horizon, pair in horizons.items()},
        },
        "V3_relax_min_recession_origins": {
            "description": "lower min_recession_months from 12 to 8 / 6 (not recommended: power)",
            "counts_window_overlap": v0_window,
            "met_at_8": {key: value >= 8 for key, value in v0_window.items()},
            "met_at_6": {key: value >= 6 for key, value in v0_window.items()},
        },
        "V4_extend_vintage_archives": {
            "description": ("extend REAL_TIME availability to 1990: CPILFESL/GS10/TB3MS (and GDPC1 "
                            "for T2) with first vintage 1990-03-20"),
            "scenarios": {name: flatten(counts, "window_overlap")
                          for name, counts in extended.items()},
            "met_window_overlap": {
                name: {key: value >= minimum for key, value in flatten(counts, "window_overlap").items()}
                for name, counts in extended.items()},
            "feasibility": ("ALFRED has no vintages before 1996-12 for CPILFESL/GS10/TB3MS; the "
                            "extension needs a different real-time source (BLS CPI archive, a "
                            "historical H.15 tape). Philadelphia Fed RTDSM does not include H.15 "
                            "rates (protocol data_manifest). Not achievable from the current "
                            "channels without new licence documents."),
        },
        "V5_minimal_input_classification": {
            "description": ("classify REAL_TIME by the target's own series only instead of the full "
                            "comparator input set"),
            "counts": {f"{target}/{window}/{horizon}":
                       pair["recession"]["window_overlap"]["real_time_minimal"]
                       for target, windows in analyses.items()
                       for window, horizons in windows.items()
                       for horizon, pair in horizons.items()},
            "minimum": minimum,
        },
        "V6_not_applicable": {
            "description": ("declare C4 NOT_APPLICABLE for T1/T2 pairs that cannot collect the "
                            "minimum on REAL_TIME data, instead of INCONCLUSIVE; the pair then does "
                            "not pass the historical gate (it stays CANDIDATE) but the report does "
                            "not pretend the criterion was tested"),
            "applies": {f"{target}/{window}/{horizon}": value < minimum
                        for target, windows in analyses.items()
                        for window, horizons in windows.items()
                        for horizon, pair in horizons.items()
                        for value in [pair["c4_v4"]["recession_origins"]]},
        },
        "V7_recession_episodes_instead_of_months": {
            "description": ("count distinct recession episodes instead of months (a power-based "
                            "rule); with REAL_TIME only 1 episode survives in W1 and W2, so any "
                            "minimum above 1 still fails"),
            "episodes_window_overlap": episode_table,
            "episodes_min_2_met": {f"{target}/{window}/{horizon}": value >= 2
                                   for target, windows in episode_table.items()
                                   for window, horizons in windows.items()
                                   for horizon, value in horizons.items()},
        },
        "V8_c4_on_W2_only": {
            "description": ("restrict the gating C4 to W2 (18 recession origins) and report W1 as "
                            "a data-limited diagnostic; W2 alone is not enough for the protocol "
                            "('W1 and W2' requirement)"),
            "W2_counts_window_overlap": {f"{target}/W2/{horizon}":
                                         pair["recession"]["window_overlap"]["real_time_full"]
                                         for target, windows in analyses.items()
                                         for horizon, pair in windows["W2"].items()},
            "W2_counts_masked_by_definition": {
                target: {window: {horizon: pair["recession"]["window_overlap"]["real_time_full"]}
                         for horizon, pair in windows["W2"].items()}
                for target, windows in analyses.items()},
        },
    }


# -------------------------------------------------------------------------------------- assembly
def build_analysis(data_root: Path | str = DEFAULT_DATA_ROOT, date_label: str = ANALYSIS_DATE) -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    origins_doc = json.loads(ORIGINS.read_text(encoding="utf-8"))
    composition = (json.loads(COMPOSITION.read_text(encoding="utf-8"))
                   if COMPOSITION.exists() else None)
    candidates_doc = (json.loads(CANDIDATES.read_text(encoding="utf-8"))
                      if CANDIDATES.exists() else None)
    data_root = Path(data_root)
    usrec, chronology_source = chronology(data_root)
    episodes = recession_episodes(usrec)
    vintages = vintages_metadata(data_root)
    firsts = vintages["first_vintages"]
    check = cross_check_origins(protocol, origins_doc)
    if not check["ok"]:
        raise C4AnalysisError(f"origins cross-check failed: {check['mismatches'][:3]}")
    analyses = {}
    for target_spec in protocol["targets"]:
        target = target_spec["id"]
        horizons = target_spec.get("horizons_months") or target_spec.get("horizons_quarters")
        analyses[target] = {}
        for window in ("W1", "W2"):
            analyses[target][window] = {}
            for horizon in horizons:
                pair = analyse_pair(protocol, usrec, episodes, firsts, composition,
                                    candidates_doc, target, horizon, window)
                analyses[target][window][str(horizon)] = pair
    lost = lost_1990_91(protocol, analyses, usrec, episodes, firsts)
    variant_block = variants(protocol, analyses, usrec, firsts, lost)
    mask_anomalies = []
    for target, windows in analyses.items():
        for window, horizons in windows.items():
            for horizon, pair in horizons.items():
                overlap = pair["recession"]["window_overlap"]["origins"]
                origin_month = pair["recession"]["origin_month"]
                if int(horizon) == 0 and overlap == 0 and origin_month["origins"] > 0:
                    mask_anomalies.append({
                        "pair": f"{target}/{window}/h{horizon}",
                        "issue": ("the runner's (t, t+h] overlap mask is identically zero at "
                                  "horizon 0 because the interval has no months; the C4 text "
                                  "reading 'origin month is a recession month' gives counts"),
                        "window_overlap_origins": overlap,
                        "origin_month_origins": origin_month["origins"],
                        "origin_month_real_time_full": origin_month["real_time_full"],
                    })
                elif origin_month["origins"] != overlap:
                    mask_anomalies.append({
                        "pair": f"{target}/{window}/h{horizon}",
                        "issue": "origin-month and (t,t+h] overlap masks count different origins",
                        "window_overlap_origins": overlap,
                        "origin_month_origins": origin_month["origins"],
                        "window_overlap_real_time_full": pair["recession"]["window_overlap"]
                        ["real_time_full"],
                        "origin_month_real_time_full": origin_month["real_time_full"],
                    })
    public_analyses = {
        target: {window: {horizon: {key: value for key, value in pair.items()
                                    if key != "_rows"}
                          for horizon, pair in horizons.items()}
                 for window, horizons in windows.items()}
        for target, windows in analyses.items()}
    minimum = int(protocol["admission"]["recession_conditioning"]["min_recession_months"])
    w1_t1_origin = {horizon: pair["recession"]["origin_month"]["real_time_full"]
                    for horizon, pair in analyses["T1_CPI_YOY"]["W1"].items()}
    w1_t1_overlap = {horizon: pair["recession"]["window_overlap"]["real_time_full"]
                     for horizon, pair in analyses["T1_CPI_YOY"]["W1"].items()}
    t2_counts = {target_window: {horizon: pair["recession"]["window_overlap"]["real_time_full"]
                                 for horizon, pair in pair_block.items()}
                 for target_window, pair_block in (
                     ("W1", analyses["T2_GDP_GROWTH"]["W1"]),
                     ("W2", analyses["T2_GDP_GROWTH"]["W2"]))}
    structural_findings = [
        {"id": "F1",
         "finding": ("Under the mask actually used by the runner ((t,t+h] overlap) C4/W1 fails for "
                     "T1 only at h=1 (8 origins) and h=3 (10); h=6 (13) and h=12 (19) reach the "
                     "minimum, but the counts include overlapping forecasts and overstate the "
                     "independent evidence"),
         "values": {"overlap_mask_real_time": w1_t1_overlap,
                    "minimum": minimum}},
        {"id": "F2",
         "finding": ("Under the C4 text ('the origin month is an NBER recession month') every T1 "
                     "W1 horizon has 8 REAL_TIME recession origins; the mask definition is a real "
                     "protocol ambiguity that must be resolved in v5"),
         "values": {"origin_month_mask_real_time": w1_t1_origin}},
        {"id": "F3",
         "finding": ("T2 cannot satisfy C4 under the current rule in any window: W1 has 5-7 and "
                     "W2 6-7 REAL_TIME recession origins, and W2 has only 18 recession months = 6 "
                     "quarters, so 12 quarterly origins never exist; the 12-origin minimum is "
                     "structurally unreachable for T2 even with perfect data"),
         "values": {"real_time_recession_origins": t2_counts}},
        {"id": "F4",
         "finding": ("At horizon 0 the runner's (t,t+h] overlap mask is identically zero (the "
                     "interval is empty); the origin-month reading gives 16 origins in W1 and 18 "
                     "in W2 (8 and 18 REAL_TIME)"),
         "values": {"T2_W1_h0": analyses["T2_GDP_GROWTH"]["W1"]["0"]["recession"],
                    "T2_W2_h0": analyses["T2_GDP_GROWTH"]["W2"]["0"]["recession"]}},
        {"id": "F5",
         "finding": ("The 1990-91 recession is lost because ALFRED has no vintages before 1996-12 "
                     "for CPILFESL (1996-12-12), GS10 and TB3MS (1996-12-03), and none before "
                     "1991-12-20 for GDPC1; table B-1 lists the blocked pairs"),
         "values": {"affected_series": sorted(lost["affected_series"])}},
        {"id": "F6",
         "finding": ("Allowing PSEUDO_REAL_TIME in the C4 split restores T1 W1 to 16 origins at "
                     "every horizon and T2 W1 to 16/5/5/5; T2 W2 stays at 6-7 and remains below 12"),
         "values": {"T1_W1_h1_pseudo_allowed":
                    analyses["T1_CPI_YOY"]["W1"]["1"]["recession"]["window_overlap"]["origins"],
                    "T2_W1_h0_pseudo_allowed":
                    analyses["T2_GDP_GROWTH"]["W1"]["0"]["recession"]["window_overlap"]["origins"]}},
    ]
    recommendation = {
        "status": "DRAFT_FOR_OWNER_DECISION",
        "requires_owner_decision": True,
        "requires_independent_review": True,
        "draft_delta_reference": "docs/88_C4_W1_варианты_и_черновик_v5.md",
        "ordered_options": [
            {"rank": 1, "option": "V2_allow_pseudo_for_c4 + V1_mask_origin_month",
             "why": ("the loss is a source-level data limit (ALFRED has no earlier vintages), not a "
                     "forecast-quality signal; C4 is a robustness split, so a labelled PSEUDO "
                     "allowance with a REAL_TIME-only sensitivity keeps the criterion informative "
                     "for T1")},
            {"rank": 2, "option": "T2_specific_rule",
             "why": ("12 origins are unreachable for T2 even with perfect data (W2 has 6 quarters); "
                     "v5 needs a target-specific minimum (e.g. 4 quarterly origins, or a "
                     "per-episode rule with reporting) rather than relaxing the global 12"),
             "draft": {"T2_GDP_GROWTH": {"min_recession_origins": 4,
                                         "mask": "origin_month",
                                         "pseudo_allowed": True,
                                         "report_episodes": True}}},
            {"rank": 3, "option": "V6_not_applicable",
             "why": ("if the owner rejects the PSEUDO allowance, C4 must be stated NOT_APPLICABLE "
                     "for the blocked pairs; they stay CANDIDATE (no silent pass) and the data gap "
                     "is reported")},
        ],
        "not_recommended": [
            {"option": "V3_relax_min_recession_origins (8/6)",
             "why": "still fails T2 W2 (6-7) and lowers the power of the test"},
            {"option": "V4_extend_vintage_archives",
             "why": ("ALFRED cannot provide CPILFESL/GS10/TB3MS vintages before 1996-12 and RTDSM "
                     "has no H.15 rates; a historical tape would be a new source with new licence "
                     "and audit work")},
            {"option": "V8_c4_on_W2_only",
             "why": "protocol requires W1 and W2; W2 alone would silently drop half the evidence"},
        ],
    }

    w1_t1 = analyses["T1_CPI_YOY"]["W1"]["1"]

    summary = {
        "problem": ("C4 on W1 cannot reach the minimum: the REAL_TIME sample starts 1996-12 while "
                    "the 1990-91 recession months (1990-08..1991-03) are PSEUDO because ALFRED "
                    "vintages of CPILFESL and GS10/TB3MS start in 1996-12"),
        "w1_t1_h1": {"recession_origins_real_time": w1_t1["c4_v4"]["recession_origins"],
                     "minimum": minimum, "first_real_time_origin":
                         w1_t1["counts"]["first_real_time_origin"],
                     "recession_origins_if_pseudo_allowed":
                         w1_t1["recession"]["window_overlap"]["origins"]},
        "w2_t1_h1": {"recession_origins_real_time":
                     analyses["T1_CPI_YOY"]["W2"]["1"]["c4_v4"]["recession_origins"],
                     "minimum": minimum},
        "lost_episode": lost["episode"],
    }
    document = {
        "analysis_id": "AMFE-C4-ANALYSIS",
        "analysis_date": date_label,
        "purpose": ("neutral C4/W1 data-availability analysis for the protocol v5 owner decision; "
                    "does not modify the frozen protocol v4"),
        "protocol": {"file": "protocols/evaluation_protocol_v4.json",
                     "version": protocol["version"], "sha256": sha256_file(PROTOCOL)},
        "origins_source": {"file": "protocols/evaluation_origins.json",
                           "sha256": sha256_file(ORIGINS)},
        "composition_source": ({"file": "protocols/comparator_composition_2026-11-11.json",
                                "sha256": sha256_file(COMPOSITION)}
                               if composition is not None else None),
        "rule": {
            "criterion": "C4",
            "instrument": protocol["admission"]["recession_conditioning"]["instrument"],
            "min_recession_months": minimum,
            "applies_to": ["T1_CPI_YOY", "T2_GDP_GROWTH"],
            "c4_text": next(item["text"] for item in protocol["admission"]["criteria"]
                            if item["id"] == "C4"),
            "mask_definitions": {
                "origin_month": "the origin month itself is an NBER recession month (C4 text)",
                "window_overlap": "(t, t+h] contains a recession month (mask used by the runner)",
            },
        },
        "chronology": {
            "source": chronology_source["source"],
            "sha256": chronology_source.get("sha256"),
            "fallback_note": chronology_source.get("note"),
            "episodes": episodes,
        },
        "vintages": vintages,
        "origins_cross_check": check,
        "mask_anomalies": mask_anomalies,
        "targets": public_analyses,
        "lost_1990_91": lost,
        "variants": variant_block,
        "structural_findings": structural_findings,
        "recommendation": recommendation,
        "summary": summary,
        "cross_checks": {
            "docs76": {"W1_T1_h1_origins": 121, "W1_T1_h1_recession_origins": 8,
                       "W2_T1_h1_recession_origins": 18},
            "computed": {
                "W1_T1_h1_origins": w1_t1["counts"]["real_time_full"],
                "W1_T1_h1_recession_origins": w1_t1["c4_v4"]["recession_origins"],
                "W2_T1_h1_recession_origins":
                    analyses["T1_CPI_YOY"]["W2"]["1"]["c4_v4"]["recession_origins"],
            },
        },
        "invariants": [
            "frozen protocol v4, R3 specification, NF-SIM thresholds and comparator composition are "
            "read only, never modified",
            "no raw third-party data are stored in this document; only derived counts and hashes",
            "the analysis is deterministic: no wall clock, pinned input hashes",
        ],
    }
    return document


def write_analysis(document: dict, out: Path = DEFAULT_OUT) -> tuple[Path, str]:
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    out.write_bytes(text.encode("utf-8"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out, digest


def check_analysis(out: Path = DEFAULT_OUT, data_root: Path | str = DEFAULT_DATA_ROOT,
                   date_label: str = ANALYSIS_DATE) -> tuple[bool, str, str]:
    if not out.exists():
        return False, f"{out} does not exist", ""
    expected = json.dumps(build_analysis(data_root, date_label), ensure_ascii=False,
                          sort_keys=True, indent=2) + "\n"
    actual = out.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if actual != expected:
        return False, "document differs from the rebuilt analysis", digest
    return True, "document matches the rebuilt analysis", digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="C4/W1 data-availability analysis")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--date", default=ANALYSIS_DATE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)  # validate
    out = Path(args.out)
    if args.check:
        ok, message, digest = check_analysis(out, args.data_root, args.date)
        print(json.dumps({"check": "PASS" if ok else "FAIL", "message": message,
                          "sha256": digest}, ensure_ascii=False))
        return 0 if ok else 1
    document = build_analysis(args.data_root, args.date)
    path, digest = write_analysis(document, out)
    summary = {
        "written": str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT)
        else str(path),
        "sha256": digest,
        "W1_T1_h1": document["summary"]["w1_t1_h1"],
        "W2_T1_h1": document["summary"]["w2_t1_h1"],
        "V2_met_W1_T1_h1": document["variants"]["V2_allow_pseudo_for_c4"]
        ["met_window_overlap"].get("T1_CPI_YOY/W1/1"),
        "V4b_met_W1_T1_h1": document["variants"]["V4_extend_vintage_archives"]
        ["met_window_overlap"]["V4b_extend_rates_core_cpi_gdp"].get("T1_CPI_YOY/W1/1"),
        "archives_available": document["vintages"]["all_archives_available"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
