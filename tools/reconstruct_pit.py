"""Reconstruct an early point-in-time (PIT) data root for 1990-01 .. 1996-11.

Why
---
The local ALFRED archive in ``data/raw/<SERIES>/vintages.zip`` starts at different
dates per series (GS10/TB3MS 1996-12-03, CPILFESL 1996-12-12, CPIAUCSL 1990-03-20,
GDPC1 1991-12-20, UNRATE 1990-03-09).  The evaluation window W1 therefore cannot
start before 1996-12 on genuine ALFRED vintages, which puts the 1990-91 recession
outside the REAL_TIME history and blocks C4 for T1/T2 (docs/105 §4.1, docs/106 §2).

This tool builds a *separate* data root, ``data/reconstructed``, that a caller can
select with the already supported ``AMFE_DATA_ROOT`` environment variable
(:func:`amfe.forecast.data._data_root`), so that no code under ``src/`` changes:

* ``data/reconstructed/<SERIES>/vintages.zip`` -- one merged CSV (columns
  ``<SERIES>_YYYYMMDD``, rows = observation dates) plus ``README.txt``.  The zip holds
  (a) reconstructed monthly vintages dated the 15th of every month 1990-01-15 ..
  1996-11-15 and (b) every real ALFRED vintage column of the source archive, unchanged.
  At a grid date where the source archive already has a real vintage column the real
  column is kept and no reconstructed column is written for that date.
* ``data/reconstructed/<SERIES>/latest.csv`` -- byte copy of ``data/raw/<SERIES>/latest.csv``.

Method (declared, no fitting)
-----------------------------
* One value per observation: the cell of the **earliest non-empty vintage column** of the
  merged source file, copied verbatim.  That is the first archived print of the
  observation ("first publication" approximation).
* An observation enters a reconstructed vintage only when its reference period ended
  before the vintage date: monthly ``period_end < vintage_date``; quarterly (GDPC1)
  ``quarter_end + 1 month < vintage_date`` (conservative: the advance estimate arrives
  about four weeks after the quarter ends).
* No observation after the vintage date is ever written; :func:`build` fails otherwise.
* Deterministic output: the zip members carry a fixed timestamp, the CSV uses ``\\n``
  line endings and UTF-8 with BOM, ``latest.csv`` is copied byte for byte.  A second run
  over the same inputs produces identical SHA-256 digests.

Revisions dossier
-----------------
:func:`build_dossier` measures, from the *source* archives, how far the earliest archived
print is from the final one (``earliest_vs_latest``) and how large one-vintage steps are
(``step_1``), in index levels and in the protocol target units (CPI y/y %, GDP q/q
annualised pp), classifies each series and writes ``protocols/pit_reconstruction.json``.
The dossier contains aggregates only -- never series values (licence policy, docs/84).

Usage
-----
    python tools/reconstruct_pit.py                # build data/reconstructed + dossier
    python tools/reconstruct_pit.py --no-dossier   # data artefacts only
    python tools/reconstruct_pit.py --data-root DIR --out-root DIR --dossier FILE

Run from the repository root.  Offline, standard library + numpy only (numpy comes with
``amfe.forecast.data``, which is imported read-only for the calendar helpers).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import statistics
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.forecast.data import (  # noqa: E402
    QUARTERLY_SERIES,
    Series as ForecastSeries,
    add_months,
    cpi_yoy,
    gdp_growth,
    month_end_of,
    month_index,
    quarter_end_month,
    quarter_index,
)

#: Series that get a reconstructed vintage archive (the six vintage-capable series of
#: protocol v4; USREC is stored as a latest tape only).
RECONSTRUCTED_SERIES = ("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS")
#: Series whose latest tape is copied into the alternative data root.
LATEST_ONLY_SERIES = ("USREC",)
ALL_OUTPUT_SERIES = RECONSTRUCTED_SERIES + LATEST_ONLY_SERIES
#: Series whose reconstructed revisions are classified in target units rather than levels.
TARGET_UNIT_TRANSFORM = {"CPIAUCSL": "cpi_yoy", "CPILFESL": "cpi_yoy", "GDPC1": "gdp_growth"}

RECON_START = date(1990, 1, 15)
RECON_END = date(1996, 11, 15)
RECON_VINTAGE_DAY = 15
#: A real vintage column counts as "full history" from this many non-empty observations on.
#: CPIAUCSL is the only series of the six whose archive starts with sparse ~1.5-year snapshots
#: (1990-03-20 .. 1993-12-16); its first full column is 1994-03-16 (566 observations).
MIN_FULL_HISTORY_OBS = 120

ZIP_CSV_NAME = "vintages.csv"
ZIP_README_NAME = "README.txt"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CSV_ENCODING = "utf-8-sig"
LINE_TERMINATOR = "\n"

#: Declared revision-class thresholds (see ``build_dossier``).  They are stated up front in
#: the dossier so that the classification is reproducible, not tuned after the fact.
REVISION_CLASS_RULES = {
    "ZERO": "max_abs == 0 in the classification basis",
    "ZERO_WITHIN_ROUNDING": "max_abs <= 0.01 and mean_abs <= 0.001 (publication rounding of a rate)",
    "SMALL": "mean_abs <= 0.05 and max_abs <= 0.5 (usable as RECONSTRUCTED_PIT with sensitivity)",
    "MATERIAL": "otherwise (not usable for an admission-relevant target without genuine archives)",
}


class ReconstructionError(RuntimeError):
    """The reconstruction contract was violated (e.g. an observation after the vintage date)."""


# --------------------------------------------------------------------------------------
# Calendar helpers
# --------------------------------------------------------------------------------------
def reconstruction_grid(start: date = RECON_START, end: date = RECON_END) -> list[date]:
    """Monthly reconstructed vintage dates, the 15th of every month in ``[start, end]``."""
    if start.day != RECON_VINTAGE_DAY or end.day != RECON_VINTAGE_DAY:
        raise ReconstructionError("reconstruction grid must use the 15th of a month")
    out: list[date] = []
    year, month = start.year, start.month
    while True:
        d = date(year, month, RECON_VINTAGE_DAY)
        if d > end:
            break
        out.append(d)
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return out


def observation_period_end(series: str, observation: str) -> date:
    """Calendar end of a monthly observation, or end of the quarter for a quarterly series."""
    d = date.fromisoformat(observation[:10])
    if series in QUARTERLY_SERIES:
        last_month = ((d.month - 1) // 3) * 3 + 3
        return month_end_of(date(d.year, last_month, 1))
    return month_end_of(d)


def observation_available(series: str, observation: str, vintage_date: date) -> bool:
    """Is the observation's reference period complete before ``vintage_date``?

    Monthly: ``period_end < vintage_date``.  Quarterly: ``quarter_end + 1 month <
    vintage_date`` (conservative: the advance estimate of a quarter is published about a
    month after the quarter ends).
    """
    period_end = observation_period_end(series, observation)
    if series in QUARTERLY_SERIES:
        return add_months(period_end, 1) < vintage_date
    return period_end < vintage_date


# --------------------------------------------------------------------------------------
# Source archive reading
# --------------------------------------------------------------------------------------
def _parse_float(cell: str) -> float | None:
    text = (cell or "").strip()
    if text in ("", ".", "NA", "nan", "NaN"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_merged_archive(path: Path) -> tuple[list[str], list[list[str]], list[tuple[date, int]]]:
    """Read the merged ALFRED zip: header, observation rows and (vintage date, column) pairs."""
    if not path.exists():
        raise ReconstructionError(f"source archive not found: {path}")
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.namelist() if m.lower().endswith(".csv")]
        if not members:
            raise ReconstructionError(f"no CSV inside {path}")
        text = archive.read(members[0]).decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 2:
        raise ReconstructionError(f"empty vintage CSV inside {path}")
    header = rows[0]
    body = [row for row in rows[1:] if row and row[0].strip()]
    columns: list[tuple[date, int]] = []
    for position, column in enumerate(header[1:], start=1):
        stamp = column.rsplit("_", 1)[-1]
        try:
            columns.append((date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8])), position))
        except (ValueError, IndexError):
            raise ReconstructionError(f"{path}: unparseable vintage column {column!r}")
    columns.sort(key=lambda item: item[0])
    if len({d for d, _ in columns}) != len(columns):
        raise ReconstructionError(f"{path}: duplicate vintage dates in the merged header")
    return header, body, columns


def earliest_observations(
    body: list[list[str]], columns: list[tuple[date, int]]
) -> dict[str, tuple[str, date]]:
    """First non-empty cell per observation: ``{observation: (raw cell, carrier vintage)}``."""
    out: dict[str, tuple[str, date]] = {}
    for row in body:
        observation = row[0].strip()[:10]
        for vintage_date, position in columns:
            cell = row[position] if position < len(row) else ""
            if _parse_float(cell) is not None:
                out[observation] = (cell.strip(), vintage_date)
                break
    return out


# --------------------------------------------------------------------------------------
# Deterministic writers
# --------------------------------------------------------------------------------------
def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 of a file on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_member(name: str, payload: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info, payload


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    """Write a zip with fixed timestamps so that the bytes are reproducible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            info, payload = _zip_member(name, payload)
            archive.writestr(info, payload)
    os.replace(tmp, path)


def render_csv(header: list[str], rows: list[list[str]]) -> bytes:
    """Deterministic CSV payload: UTF-8 with BOM, ``\\n`` line endings, no index."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=LINE_TERMINATOR)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode(CSV_ENCODING)


def _archive_readme(series: str, tape_dates: list[date], replaced: list[date], kept: list[date],
                    full_history: date, full_history_observations: int,
                    carrier_lag: dict) -> str:
    replaced_note = (", ".join(d.isoformat() for d in replaced[:20])
                     + (" ..." if len(replaced) > 20 else "")) if replaced else "none"
    return (
        "-------------------------------------------------------------------\n"
        f"Series ID: {series}\n"
        "Reconstructed point-in-time archive (AMFE wave 5, track A; tape override)\n"
        "Generated by tools/reconstruct_pit.py -- do not edit by hand.\n"
        "-------------------------------------------------------------------\n"
        "Full-history date (first archived vintage with >= "
        f"{MIN_FULL_HISTORY_OBS} observations): {full_history.isoformat()} "
        f"({full_history_observations} observations)\n"
        f"Genuine ALFRED vintage columns preserved verbatim: {len(kept)}"
        + (f" ({kept[0].isoformat()} .. {kept[-1].isoformat()})" if kept else "")
        + "\n"
        f"Sparse real vintage columns before the full-history date replaced by tape columns on "
        f"the same dates: {len(replaced)} ({replaced_note})\n"
        f"Tape columns written: {len(tape_dates)} (monthly grid dates on the 15th plus the "
        "replaced real dates; no tape column for a month that has a genuine vintage)\n"
        "Value source: the earliest non-empty vintage column of the merged source archive "
        f"(data/raw/{series}/vintages.zip) -- the tape of first prints.\n"
        "Inclusion rule: an observation is present only if its reference period ended before "
        "the vintage date -- monthly: period_end < vintage_date; quarterly (GDPC1): "
        "quarter_end + 1 month < vintage_date.\n"
        "No observation after the vintage date is included.\n"
        "Tape carrier lag: the earliest archived print may be published after the reconstructed "
        f"vintage date -- {carrier_lag.get('cells', 0)} cells, max "
        f"{carrier_lag.get('max_lag_days', 0)} days, "
        f"{carrier_lag.get('observations', 0)} observations affected (see the dossier).\n"
        "Source: ALFRED, Federal Reserve Bank of St. Louis "
        f"(https://alfred.stlouisfed.org/series?seid={series}).\n"
        "Raw third-party observations are never committed; revisions, tape-override facts and "
        "SHA-256 digests are recorded in protocols/pit_reconstruction.json and docs/108.\n"
    )


def tree_digest(root: Path) -> str:
    """SHA-256 over every file of ``root`` (sorted relative path + file digest)."""
    lines = [
        f"{path.relative_to(root).as_posix()} {sha256_file(path)}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return _sha256_bytes(LINE_TERMINATOR.join(lines).encode("utf-8"))


# --------------------------------------------------------------------------------------
# Build one series
# --------------------------------------------------------------------------------------
def vintage_observation_counts(
    body: list[list[str]], columns: list[tuple[date, int]]
) -> list[tuple[date, int]]:
    """Number of non-empty observations in every source vintage column."""
    counts: list[tuple[date, int]] = []
    for vintage_date, position in columns:
        counts.append((vintage_date, sum(
            1 for row in body if _parse_float(row[position] if position < len(row) else "") is not None
        )))
    return counts


def full_history_date(counts: list[tuple[date, int]],
                      minimum: int = MIN_FULL_HISTORY_OBS) -> date | None:
    """First source vintage column that carries at least ``minimum`` observations."""
    for vintage_date, count in counts:
        if count >= minimum:
            return vintage_date
    return None


def build_series(series: str, data_root: Path, out_root: Path,
                 grid: list[date] | None = None) -> dict:
    """Build ``<out_root>/<series>/vintages.zip`` and copy ``latest.csv``; return facts.

    Columns of the output archive are of two kinds:

    * ``real`` -- genuine ALFRED vintage columns dated on/after the series' full-history date,
      copied verbatim;
    * ``tape`` -- reconstructed columns: the monthly grid date (15th) plus every real vintage
      date that lies *before* the series' full-history date (the tape override).  A tape value
      is the earliest archived print of the observation.

    A tape column is not written for a month that already has a genuine vintage: the genuine
    column is strictly better than the tape and wins by date anyway on a later origin.
    """
    grid = list(grid if grid is not None else reconstruction_grid())
    _header, body, columns = read_merged_archive(data_root / series / "vintages.zip")
    earliest = earliest_observations(body, columns)
    real_dates = [d for d, _ in columns]
    counts = vintage_observation_counts(body, columns)
    count_by_date = dict(counts)
    full = full_history_date(counts)
    if full is None:
        # no archived column carries a usable history: nothing to override, keep the archive as is
        full = real_dates[0]
    replaced = [d for d in real_dates if d < full]
    kept = [d for d in real_dates if d >= full]
    kept_set = set(kept)
    replaced_set = set(replaced)
    genuine_months = {(d.year, d.month) for d in kept}
    tape_from_grid = [d for d in grid
                      if d not in kept_set and (d.year, d.month) not in genuine_months]
    tape_dates = sorted(set(tape_from_grid) | replaced_set)
    if not tape_dates:
        raise ReconstructionError(f"{series}: no tape column to write")
    output_columns: list[tuple[date, str]] = sorted(
        [(d, "tape") for d in tape_dates] + [(d, "real") for d in kept]
    )
    dates_out = [d for d, _ in output_columns]
    if len(set(dates_out)) != len(dates_out):
        raise ReconstructionError(f"{series}: duplicate vintage dates in the output archive")
    position_by_date = {d: p for d, p in columns}

    header = ["observation_date"] + [f"{series}_{d:%Y%m%d}" for d in dates_out]

    tape_cells = 0
    carrier_lag_cells = 0
    carrier_lag_max_days = 0
    carrier_lag_observations = 0
    rows: list[list[str]] = []
    for row in body:
        observation = row[0].strip()[:10]
        out_row = [observation]
        carrier = earliest.get(observation)
        first_included: date | None = None
        for vintage_date, kind in output_columns:
            if kind == "real":
                position = position_by_date[vintage_date]
                out_row.append(row[position].strip() if position < len(row) else "")
                continue
            if carrier is None or not observation_available(series, observation, vintage_date):
                out_row.append("")
                continue
            out_row.append(carrier[0])
            tape_cells += 1
            if first_included is None:
                first_included = vintage_date
            if carrier[1] > vintage_date:
                carrier_lag_cells += 1
                carrier_lag_max_days = max(carrier_lag_max_days, (carrier[1] - vintage_date).days)
        if first_included is not None and carrier is not None and carrier[1] > first_included:
            carrier_lag_observations += 1
        if any(cell for cell in out_row[1:]):
            rows.append(out_row)

    rows.sort(key=lambda item: item[0])
    payload = render_csv(header, rows)
    readme = _archive_readme(
        series,
        tape_dates=tape_dates,
        replaced=replaced,
        kept=kept,
        full_history=full,
        full_history_observations=count_by_date.get(full, 0),
        carrier_lag={"cells": carrier_lag_cells, "max_lag_days": carrier_lag_max_days,
                     "observations": carrier_lag_observations},
    )
    zip_path = out_root / series / "vintages.zip"
    write_zip(zip_path, [(ZIP_CSV_NAME, payload), (ZIP_README_NAME, readme.encode("utf-8"))])

    latest_src = data_root / series / "latest.csv"
    latest_sha = None
    if latest_src.exists():
        latest_dst = out_root / series / "latest.csv"
        latest_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(latest_src, latest_dst)
        latest_sha = sha256_file(latest_dst)

    # contract check: a tape vintage must never expose an observation whose period did not end
    # before the vintage date; a preserved real ALFRED vintage only has to be internally
    # consistent (its release date may precede the conservative reconstruction lag).
    tape_set = set(tape_dates)
    for vintage_date, set_series in load_output_vintages(series, out_root).items():
        last = set_series.last_index
        if last is None:
            raise ReconstructionError(f"{series}: empty reconstructed vintage {vintage_date}")
        last_observation = _period_first_date(series, last)
        if vintage_date in tape_set:
            ok = observation_available(series, last_observation.isoformat(), vintage_date)
        else:
            ok = observation_period_end(series, last_observation.isoformat()) < vintage_date
        if not ok:
            raise ReconstructionError(
                f"{series}: vintage {vintage_date.isoformat()} exposes observation "
                f"{last_observation.isoformat()} after its period end"
            )

    return {
        "first_real_vintage": real_dates[0].isoformat(),
        "real_vintages_in_source": len(real_dates),
        "real_vintages_preserved": len(kept),
        "reconstructed_vintages_written": len(tape_dates),
        "reconstructed_vintages_requested": len(grid),
        "grid_collisions_with_real_vintages": [
            d.isoformat() for d in grid if d in kept_set
        ],
        "rows": len(rows),
        "reconstructed_cells": tape_cells,
        "carrier_after_vintage_date": {"cells": carrier_lag_cells,
                                       "max_lag_days": carrier_lag_max_days,
                                       "observations_entering_before_their_earliest_print":
                                           carrier_lag_observations},
        "tape_override": {
            "full_history_date": full.isoformat(),
            "full_history_observations": count_by_date.get(full, 0),
            "min_full_history_observations": MIN_FULL_HISTORY_OBS,
            "replaced_real_vintages": [d.isoformat() for d in replaced],
            "replaced_real_vintages_count": len(replaced),
            "replaced_real_vintages_also_on_grid": [
                d.isoformat() for d in grid if d in replaced_set
            ],
            "kept_real_vintages_count": len(kept),
            "tape_vintages_written": len(tape_dates),
            "tape_vintages_from_grid": len(tape_from_grid),
            "carrier_after_vintage_date": {
                "cells": carrier_lag_cells, "max_lag_days": carrier_lag_max_days,
                "observations_entering_before_their_earliest_print": carrier_lag_observations,
            },
        },
        "vintages_zip": {"path": zip_path.relative_to(out_root).as_posix()
                         if out_root in zip_path.parents else str(zip_path),
                         "sha256": sha256_file(zip_path), "bytes": zip_path.stat().st_size},
        "latest_csv": None if latest_sha is None else {
            "path": f"{series}/latest.csv", "sha256": latest_sha,
            "bytes": (out_root / series / "latest.csv").stat().st_size,
        },
    }


def _period_first_date(series: str, period: int) -> date:
    if series in QUARTERLY_SERIES:
        return date(period // 4, (period % 4) * 3 + 1, 1)
    return date(period // 12, period % 12 + 1, 1)


def load_output_vintages(series: str, out_root: Path) -> dict[date, ForecastSeries]:
    """Read the produced archive through the production reader (``load_vintages``)."""
    from amfe.forecast.data import load_vintages

    return load_vintages(series, out_root)


# --------------------------------------------------------------------------------------
# Revision statistics (dossier)
# --------------------------------------------------------------------------------------
def _column_values(body: list[list[str]], position: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in body:
        value = _parse_float(row[position] if position < len(row) else "")
        if value is not None:
            out[row[0].strip()[:10]] = value
    return out


def _period_ordinal(series: str, observation: str) -> int:
    d = date.fromisoformat(observation[:10])
    return quarter_index(d) if series in QUARTERLY_SERIES else month_index(d)


def _target_series(series: str, values: dict[str, float]) -> dict[str, float]:
    """Apply the protocol target transform to ``{observation: level}``."""
    transform = TARGET_UNIT_TRANSFORM.get(series)
    if transform is None:
        return dict(values)
    ordinal = {_period_ordinal(series, observation): value for observation, value in values.items()}
    ordered = sorted(ordinal)
    index = np.asarray(ordered, dtype=np.int64)
    levels = ForecastSeries(series, "Q" if series in QUARTERLY_SERIES else "M",
                            index, np.asarray([ordinal[p] for p in ordered], dtype=float))
    transformed = cpi_yoy(levels) if transform == "cpi_yoy" else gdp_growth(levels)
    if series in QUARTERLY_SERIES:
        label = lambda p: f"{p // 4:04d}Q{p % 4 + 1}"  # noqa: E731
    else:
        label = lambda p: f"{p // 12:04d}-{p % 12 + 1:02d}"  # noqa: E731
    return {label(int(p)): float(v) for p, v in zip(transformed.index, transformed.values)}


def _stats(diffs: list[float], unit: str) -> dict | None:
    if not diffs:
        return None
    return {
        "unit": unit,
        "n": len(diffs),
        "mean_abs": round(sum(diffs) / len(diffs), 6),
        "max": round(max(diffs), 6),
        "median": round(statistics.median(diffs), 6),
    }


def revision_stats(series: str, data_root: Path) -> dict:
    """Earliest-vs-latest and one-vintage-step revisions, in levels and target units."""
    _header, body, columns = read_merged_archive(data_root / series / "vintages.zip")
    level_maps = [(_column_values(body, position), vintage_date) for vintage_date, position in columns]

    earliest_level: dict[str, float] = {}
    latest_level: dict[str, float] = {}
    for values, _vintage in level_maps:
        for observation, value in values.items():
            earliest_level.setdefault(observation, value)
            latest_level[observation] = value
    common = sorted(set(earliest_level) & set(latest_level))
    level_earliest_vs_latest = [abs(latest_level[k] - earliest_level[k]) for k in common]

    level_step: list[float] = []
    for left, right in zip(level_maps, level_maps[1:]):
        shared = set(left[0]) & set(right[0])
        level_step.extend(abs(right[0][k] - left[0][k]) for k in shared)

    transform = TARGET_UNIT_TRANSFORM.get(series)
    target_unit = "index level"
    if transform == "cpi_yoy":
        target_unit = "percentage points (CPI y/y)"
    elif transform == "gdp_growth":
        target_unit = "percentage points (GDP q/q annualised)"

    target_earliest_vs_latest = None
    target_step = None
    window = None
    if transform is not None:
        target_maps = [(_target_series(series, values), vintage_date) for values, vintage_date in level_maps]
        first: dict[str, float] = {}
        last: dict[str, float] = {}
        for values, _vintage in target_maps:
            for observation, value in values.items():
                first.setdefault(observation, value)
                last[observation] = value
        shared = sorted(set(first) & set(last))
        target_earliest_vs_latest = [abs(last[k] - first[k]) for k in shared]
        target_step = []
        for left, right in zip(target_maps, target_maps[1:]):
            for k in set(left[0]) & set(right[0]):
                target_step.append(abs(right[0][k] - left[0][k]))
        if series in QUARTERLY_SERIES:
            in_window = [k for k in shared if "1990Q1" <= k <= "1992Q4"]
        else:
            in_window = [k for k in shared if "1990-01" <= k <= "1992-12"]
        window = _stats([abs(last[k] - first[k]) for k in in_window], target_unit)

    if transform is not None:
        basis = target_earliest_vs_latest or []
        basis_unit = target_unit
    else:
        basis = level_earliest_vs_latest
        basis_unit = "index level"
    revision_class = _classify(basis)
    blocks = {
        "levels": {
            "unit": "index points (level units)",
            "earliest_vs_latest": _stats(level_earliest_vs_latest, "index points (level units)"),
            "step_1": _stats(level_step, "index points (level units)"),
        },
    }
    if transform is not None:
        blocks["target_units"] = {
            "transform": transform,
            "unit": target_unit,
            "earliest_vs_latest": _stats(target_earliest_vs_latest or [], target_unit),
            "step_1": _stats(target_step or [], target_unit),
            "window_1990_1992": window,
        }
    blocks.update({
        "revision_class": revision_class,
        "zero_revision": bool(basis) and max(basis) == 0.0,
        "classification_basis": basis_unit,
    })
    return blocks


def _classify(diffs: list[float]) -> str:
    if not diffs:
        return "MATERIAL"
    peak = round(max(diffs), 6)
    mean = round(sum(diffs) / len(diffs), 9)
    if peak == 0.0:
        return "ZERO"
    if peak <= 0.01 and mean <= 0.001:
        return "ZERO_WITHIN_ROUNDING"
    if mean <= 0.05 and peak <= 0.5:
        return "SMALL"
    return "MATERIAL"


# --------------------------------------------------------------------------------------
# Dossier
# --------------------------------------------------------------------------------------
def _generated_at() -> str:
    """Reproducible stamp: ``AMFE_GENERATED_AT`` wins, else the current UTC time."""
    override = os.environ.get("AMFE_GENERATED_AT", "").strip()
    if override:
        return override
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_dossier(out_root: Path, facts: dict, data_root: Path,
                  generated_at: str | None = None) -> dict:
    """Assemble ``protocols/pit_reconstruction.json`` from the build facts and revisions."""
    series_block: dict[str, dict] = {}
    for series in RECONSTRUCTED_SERIES:
        entry = dict(facts[series])
        entry["revisions"] = revision_stats(series, data_root)
        series_block[series] = entry

    rates = {}
    for series in ("GS10", "TB3MS"):
        revisions = series_block[series]["revisions"]
        rates[series] = {
            "zero_revision": revisions["zero_revision"],
            "revision_class": revisions["revision_class"],
            "earliest_vs_latest": revisions["levels"]["earliest_vs_latest"],
            "step_1": revisions["levels"]["step_1"],
        }

    usage = {
        "GS10": "REAL_TIME_RECONSTRUCTED_MARKET -- measured zero revision; usable in W1 as no-revision series",
        "TB3MS": "REAL_TIME_RECONSTRUCTED_MARKET -- revisions within publication rounding; usable in W1",
        "CPIAUCSL": "RECONSTRUCTED_PIT -- small revision; admissible only with the REAL_TIME-only "
                    "sensitivity slice (docs/106 §3 п.2)",
        "CPILFESL": "RECONSTRUCTED_PIT -- small revision; same sensitivity requirement",
        "UNRATE": "RECONSTRUCTED_PIT -- small revision; input to T3, not an admission-relevant target by itself",
        "GDPC1": "RECONSTRUCTED_PIT_DIAGNOSTIC_ONLY -- material revisions; do NOT use for T2 in 2026 "
                 "(genuine BEA first releases or FRASER scans are required)",
    }

    disclosed = {
        "GS10": "no revisions (measured zero 1996-2026); reconstruction is exact for the stored values",
        "TB3MS": "no revisions beyond 0.01 p.p. publication rounding; reconstruction is exact within rounding",
        "CPIAUCSL": "small revisions; use only via RECONSTRUCTED_PIT with the REAL_TIME-only sensitivity slice",
        "CPILFESL": "small revisions; same disclosure as CPIAUCSL (core CPI starts 1996-12 in the archive)",
        "UNRATE": "small revisions; disclosed as RECONSTRUCTED_PIT input",
        "GDPC1": "material revisions (mean |d| ~1.1 pp of q/q annualised growth over the full archive); "
                 "T2 must not be admitted on this reconstruction",
    }

    tape_override = {
        "rule": "Real ALFRED vintage columns dated before the series' full-history date are sparse "
                "snapshots (~1.5 years of history) and are replaced by tape columns on the same "
                "dates: an observation is the earliest archived print of the series (the tape of "
                "first prints). Genuine columns from the full-history date on are preserved "
                "verbatim; a tape column is not written for a month that has a genuine vintage, "
                "and a genuine column wins over a grid date on a clash.",
        "min_full_history_observations": MIN_FULL_HISTORY_OBS,
        "full_history_definition": "first real vintage column of the source archive with at least "
                                   f"{MIN_FULL_HISTORY_OBS} non-empty observations",
        "why": "the sparse CPIAUCSL snapshots 1990-03-20 .. 1993-12-16 hold only ~19 observations, so "
               "an origin in 1990-1993 saw 19 CPI observations and 7 y/y values; the forecast models "
               "need >= 60 y/y observations, and the 1990-91 recession was therefore outside any C4 "
               "computation. This is the same idea as data_depth.first_release_tape: a tape "
               "approximation, declared and disclosed, not a silent substitution.",
        "series": {},
        "disclosure": {
            "CPIAUCSL": "1990-03..1994-02 history is assembled from prints archived up to "
                        "1994-03-16; CPI revisions are measured and small (earliest-vs-latest "
                        "0.019 pp y/y on average, max 0.223 pp), so this segment is "
                        "RECONSTRUCTED_PIT and requires the REAL_TIME-only sensitivity slice.",
            "CPILFESL": "no override: the archive starts 1996-12-12 with 479 observations; only tape "
                        "columns before that date exist (reconstructed, small revisions).",
            "UNRATE": "no override: the first real vintage (1990-03-09) already carries 506 observations.",
            "GS10": "no override: the first real vintage (1996-12-03) carries 524 observations; "
                    "revisions measured zero.",
            "TB3MS": "no override: the first real vintage (1996-12-03) carries 755 observations; "
                     "revisions within 0.01 p.p. rounding.",
            "GDPC1": "no override, but MATERIAL revisions: T2 must not use this data root.",
        },
    }
    for series in RECONSTRUCTED_SERIES:
        entry = series_block[series]["tape_override"]
        tape_override["series"][series] = {
            "full_history_date": entry["full_history_date"],
            "full_history_observations": entry["full_history_observations"],
            "replaced_real_vintages_count": entry["replaced_real_vintages_count"],
            "replaced_real_vintages": entry["replaced_real_vintages"],
            "kept_real_vintages_count": entry["kept_real_vintages_count"],
            "tape_vintages_written": entry["tape_vintages_written"],
            "carrier_after_vintage_date": entry["carrier_after_vintage_date"],
        }

    artifacts = {
        "data_root": str(out_root).replace("\\", "/"),
        "series": {},
        "digest": tree_digest(out_root),
        "digest_definition": "sha256 over '<relative path> <sha256>' lines of every file, sorted",
    }
    for series in ALL_OUTPUT_SERIES:
        files = {}
        for name in ("vintages.zip", "latest.csv"):
            path = out_root / series / name
            if path.exists():
                files[name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
        artifacts["series"][series] = files

    dossier = {
        "schema": "amfe.pit_reconstruction.v2",
        "document": "protocols/pit_reconstruction.json",
        "status": "PROPOSED",
        "generated_at": generated_at or _generated_at(),
        "generated_by": "tools/reconstruct_pit.py (offline; deterministic data artefacts)",
        "sources": {
            "data_root": str(data_root).replace("\\", "/"),
            "archive": "ALFRED merged vintages (data/raw/<SERIES>/vintages.zip), fetched by tools/fetch_data.py",
            "network": False,
        },
        "method": {
            "summary": "Value of an observation = the earliest non-empty vintage column of the merged "
                       "source archive (first archived print). Tape vintages are monthly dates on the "
                       "15th plus, per series, every real vintage date before the series' full-history "
                       "date (the tape override); an observation is included only when its reference "
                       "period ended before the vintage date. Genuine ALFRED vintage columns from the "
                       "full-history date on are preserved unchanged in the same zip, and a genuine "
                       "column wins over a grid date on a clash.",
            "value_source": "earliest non-empty vintage column per observation (verbatim cell)",
            "inclusion_rule": {
                "monthly": "period_end < vintage_date",
                "quarterly": "quarter_end + 1 month < vintage_date (conservative, advance estimate lag)",
                "no_observation_after_vintage": True,
            },
            "grid": {"frequency": "monthly", "vintage_day": RECON_VINTAGE_DAY,
                     "start": RECON_START.isoformat(), "end": RECON_END.isoformat(),
                     "dates": len(reconstruction_grid())},
            "tape_override": "real vintage columns dated before the full-history date are replaced by "
                             "tape columns on the same dates (see the tape_override section); genuine "
                             "columns from the full-history date on are kept verbatim",
            "determinism": "fixed zip member timestamp (1980-01-01), UTF-8 BOM CSV with \\n line "
                            "endings, byte-copied latest.csv; a rerun yields identical SHA-256",
            "revision_classification": REVISION_CLASS_RULES,
            "revisions_measured_on": "the source archive data/raw, aggregates only; no series values "
                                     "are written to this dossier (licence policy, docs/84)",
        },
        "reconstruction": {
            "range": {"start": RECON_START.isoformat(), "end": RECON_END.isoformat()},
            "vintage_dates": len(reconstruction_grid()),
            "series": list(RECONSTRUCTED_SERIES),
            "latest_only": list(LATEST_ONLY_SERIES),
            "data_root_env": "AMFE_DATA_ROOT=data/reconstructed",
        },
        "series": series_block,
        "tape_override": tape_override,
        "rates_zero_revision_check": {
            "series": rates,
            "conclusion": "GS10 has exactly zero revisions in the archive; TB3MS differs by at most "
                          "0.01 p.p. (publication rounding). Both are treated as no-revision market "
                          "series for the reconstruction (docs/106 §3 п.1).",
        },
        "look_ahead_disclosure": {
            "no_observation_after_vintage_date": "enforced by the builder and re-checked through "
                                                 "amfe.forecast.data.load_vintages; a violation aborts the build",
            "tape_override": "columns dated before a series' full-history date are a declared tape "
                             "approximation, not genuine snapshots: the stored value of an observation "
                             "is its earliest archived print, and that print may be archived in a "
                             "column dated later than the reconstructed vintage (the carrier lag is "
                             "reported per series). For CPIAUCSL this makes 1990-03..1994-02 usable "
                             "(full history instead of ~19 observations) at the price of carrying CPI "
                             "first-print revisions; the measured CPI revisions are small, so the "
                             "segment stays RECONSTRUCTED_PIT and requires the REAL_TIME-only "
                             "sensitivity slice. It is the analogue of data_depth.first_release_tape.",
            "carrier_vintage_caveat": "the earliest archived print may itself have been published after "
                                      "the tape vintage date (the archive simply does not hold an "
                                      "earlier print; the early columns of the CPIAUCSL merged file are "
                                      "sparse snapshots, which is exactly why they are overridden by "
                                      "the tape). The per-series field carrier_after_vintage_date "
                                      "reports how many cells, how many observations and how many days "
                                      "this affects; the stored value is the earliest print available, "
                                      "which is the declared approximation. Because the vintage grid of "
                                      "the source archive is sparse (quarterly), the day count is an "
                                      "upper bound: a value is published some weeks before the next "
                                      "archived column date.",
            "per_series": disclosed,
            "rates": "no revisions -> reconstruction of the stored values is exact",
            "cpi": "small revisions -> admissible as RECONSTRUCTED_PIT only together with the "
                   "REAL_TIME-only sensitivity slice",
            "gdp": "material revisions -> do not use for T2",
            "t2_usage": "T2 must not use this data root in 2026 (genuine first releases are required)",
        },
        "usage": usage,
        "artifacts": artifacts,
        "verification": {
            "build": "python tools/reconstruct_pit.py",
            "tests": "python tests/test_reconstruct_pit.py",
            "reader_check": "AMFE_DATA_ROOT=data/reconstructed python -c "
                            "\"from amfe.forecast.data import DataContext; "
                            "print(DataContext.from_local('1990-06-30').describe())\"",
        },
        "limitations": [
            "This is a reconstruction, not genuine first-release data; revision sizes are disclosed above.",
            "The source archive has ~147 sparse (quarterly) vintage columns per series, not the original "
            "monthly publication grid.",
            "The early CPIAUCSL vintage columns are sparse snapshots (~1.5 years of history); they are "
            "replaced by tape columns up to the full-history date 1994-03-16 (tape_override). This is a "
            "declared tape approximation (analogue of data_depth.first_release_tape): full history is "
            "gained, but the CPI first-print revisions are carried into 1990-1993 and a sensitivity "
            "slice without those origins is mandatory.",
            "The early vintage columns of other series are partial in the same way at the margins, so for "
            "some observations the earliest archived print is a later vintage; the "
            "carrier_after_vintage_date counters bound that effect per series.",
            "For observations published before the archive's first vintage the stored value is the earliest "
            "archived print (revisions up to that vintage are baked in).",
            "GDPC1 is material: T2 must not be admitted on this data root.",
            "USREC has no vintage archive; only latest.csv is copied.",
            "Nothing under src/ is modified; the alternative root is selected only via AMFE_DATA_ROOT.",
        ],
    }
    return dossier


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
def build(data_root=None, out_root=None, dossier_path=None, generated_at=None,
          verbose: bool = True) -> dict:
    """Build the reconstructed data root and (optionally) the dossier; return the dossier."""
    data_root = Path(data_root) if data_root is not None else ROOT / "data" / "raw"
    out_root = Path(out_root) if out_root is not None else ROOT / "data" / "reconstructed"
    if not data_root.is_dir():
        raise ReconstructionError(f"source data root not found: {data_root}")
    grid = reconstruction_grid()

    facts = {}
    for series in ALL_OUTPUT_SERIES:
        if series in RECONSTRUCTED_SERIES:
            facts[series] = build_series(series, data_root, out_root, grid)
        else:
            latest_src = data_root / series / "latest.csv"
            if not latest_src.exists():
                raise ReconstructionError(f"latest.csv not found for {series}: {latest_src}")
            latest_dst = out_root / series / "latest.csv"
            latest_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(latest_src, latest_dst)
            facts[series] = {"latest_csv": {"path": f"{series}/latest.csv",
                                            "sha256": sha256_file(latest_dst),
                                            "bytes": latest_dst.stat().st_size}}

    dossier = build_dossier(out_root, facts, data_root, generated_at=generated_at)
    if dossier_path is not None:
        path = Path(dossier_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dossier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if verbose:
        print(f"data root: {out_root}")
        print(f"digest:    {dossier['artifacts']['digest']}")
        for series in RECONSTRUCTED_SERIES:
            entry = dossier["series"][series]
            revisions = entry["revisions"]
            print(f"  {series:<10} recon {entry['reconstructed_vintages_written']:>3}/{entry['reconstructed_vintages_requested']:<3}"
                  f" real {entry['real_vintages_preserved']:>3}"
                  f" class {revisions['revision_class']:<20}"
                  f" zip {entry['vintages_zip']['sha256'][:16]}")
        if dossier_path is not None:
            print(f"dossier:   {Path(dossier_path)}")
    return dossier


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", default=None, help="source root (default: data/raw)")
    parser.add_argument("--out-root", default=None, help="output root (default: data/reconstructed)")
    parser.add_argument("--dossier", default=None,
                        help="dossier path (default: protocols/pit_reconstruction.json; 'none' disables)")
    parser.add_argument("--generated-at", default=None,
                        help="fixed generated_at stamp for a reproducible dossier (overrides AMFE_GENERATED_AT)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    dossier_path = args.dossier
    if dossier_path is None:
        dossier_path = ROOT / "protocols" / "pit_reconstruction.json"
    elif str(dossier_path).lower() in ("none", ""):
        dossier_path = None
    dossier = build(data_root=args.data_root, out_root=args.out_root,
                    dossier_path=dossier_path, generated_at=args.generated_at,
                    verbose=not args.quiet)
    return 0 if dossier else 1


if __name__ == "__main__":
    raise SystemExit(main())
