"""Tests for the early-PIT reconstruction (``tools/reconstruct_pit.py``).

Everything here runs offline on **synthetic fixtures** created in a temporary directory: the
tests never read ``data/raw`` or ``data/reconstructed`` (both are git-ignored downloads).  A
last test cross-checks the committed dossier against the artefacts when the local build is
present and is skipped otherwise, like the other data-dependent tests of this repository.

The fixture reproduces the real data situation that made the tape override necessary: the
CPIAUCSL archive starts with sparse ~1-year snapshots (only recent history) and only becomes a
full-history archive at a known date, while the other series start with a full column.

Covered contracts
-----------------
* date grid: monthly vintages on the 15th, 1990-01-15 .. 1996-11-15;
* tape override: real sparse columns before the full-history date are replaced by tape columns
  on the same dates, genuine full-history columns are preserved verbatim, and a genuine column
  wins on a grid clash;
* history at an early origin: CPIAUCSL >= 180 observations and >= 60 y/y values at 1990-06-30,
  so ``forecast('RW'/'AR_BIC', 'T1_CPI_YOY', ...)`` runs instead of raising ``InsufficientData``;
* no look-ahead in any written vintage (observation period end < vintage date);
* untouched genuine vintages after the archive start give exactly the same data as the source;
* deterministic output (byte-identical rebuild -> identical SHA-256 digests);
* dossier schema (v2, tape_override), aggregate-only content (no series values), artefact digests;
* no revisions for the market rates, material revisions for GDPC1.

Run from the repository root: ``python tests/test_reconstruct_pit.py``.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import reconstruct_pit as rp  # noqa: E402
from amfe.forecast import data as fd  # noqa: E402
from amfe.forecast.models import forecast  # noqa: E402

MONTHLY_SERIES = ("CPIAUCSL", "CPILFESL", "UNRATE", "GS10", "TB3MS")
QUARTERLY_SERIES = ("GDPC1",)
ALL_SERIES = MONTHLY_SERIES + QUARTERLY_SERIES + ("USREC",)
FIXED_GENERATED_AT = "2026-11-12T00:00:00+00:00"

#: Synthetic observation span: long enough that an early origin has a full history after the
#: tape override (>= 180 observations at 1990-06-30) and that the first genuine column already
#: carries >= MIN_FULL_HISTORY_OBS observations for the series without a sparse archive.
OBS_START_YEAR = 1974
OBS_END_YEAR = 1997

#: Synthetic vintage columns for the monthly fixture (includes grid collisions: 1990-06-15,
#: 1990-09-15, 1992-09-15, 1995-06-15, and genuine columns before/after the window).
MONTHLY_VINTAGES = (
    date(1988, 3, 1), date(1989, 6, 1), date(1990, 4, 20), date(1990, 6, 15),
    date(1990, 9, 15), date(1991, 6, 14), date(1992, 9, 15), date(1994, 3, 16),
    date(1995, 6, 15), date(1996, 12, 3), date(1997, 3, 4),
    date(1998, 6, 3), date(1999, 9, 1), date(2000, 12, 5),
)
QUARTERLY_VINTAGES = (
    date(1989, 3, 1), date(1991, 12, 20), date(1993, 6, 25), date(1995, 6, 26),
    date(1996, 3, 26), date(1996, 12, 25), date(1998, 3, 26), date(1999, 6, 24),
    date(2000, 12, 21),
)
#: The synthetic CPIAUCSL archive is a sparse ~1-year snapshot before this date and full after it
#: (mirrors the real archive: sparse 1990-03-20 .. 1993-12-16, full from 1994-03-16).
SPARSE_CPIAUCSL_UNTIL = date(1994, 3, 16)
SPARSE_MONTHS = 14          # a sparse snapshot carries only the last ~14 months
MONTHLY_LAG_DAYS = 45       # synthetic publication lag of a monthly observation
QUARTERLY_LAG_DAYS = 60     # synthetic publication lag of a quarterly observation


# ---------------------------------------------------------------------------------------
# Synthetic fixture
# ---------------------------------------------------------------------------------------
def _observation_dates(freq: str) -> list[date]:
    if freq == "M":
        return [date(y, m, 1) for y in range(OBS_START_YEAR, OBS_END_YEAR + 1) for m in range(1, 13)]
    return [date(y, m, 1) for y in range(OBS_START_YEAR, OBS_END_YEAR + 1) for m in (1, 4, 7, 10)]


def _synthetic_value(series: str, observation: date, vintage: date) -> str:
    """Deterministic synthetic level with a known revision pattern.

    * GS10/TB3MS -- constant across vintages (zero revision);
    * CPIAUCSL/CPILFESL -- a +0.3 revision of pre-1991 observations from 1996-12 on;
    * UNRATE -- constant (no revision);
    * GDPC1 -- a +10 revision of pre-1995 levels from 1996-12 on.
    """
    if series in ("GS10", "TB3MS"):
        base = 2.0 if series == "GS10" else 3.0
        value = base + 0.05 * ((observation.year - 1987) * 12 + observation.month - 1)
        return f"{value:.2f}"
    if series == "UNRATE":
        return f"{5.0 + 0.02 * ((observation.year - 1987) * 12 + observation.month - 1):.2f}"
    if series in ("CPIAUCSL", "CPILFESL"):
        base = 100.0 if series == "CPIAUCSL" else 95.0
        value = base + 0.5 * ((observation.year - 1987) * 12 + observation.month - 1)
        if vintage >= date(1996, 12, 1) and observation < date(1991, 1, 1):
            value += 0.3
        return f"{value:.3f}"
    # GDPC1
    value = 1000.0 + 20.0 * ((observation.year - 1987) * 4 + (observation.month - 1) // 3)
    if vintage >= date(1996, 12, 1) and observation < date(1995, 1, 1):
        value += 10.0
    return f"{value:.3f}"


def _period_end(freq: str, observation: date) -> date:
    if freq == "M":
        return fd.month_end_of(observation)
    last_month = ((observation.month - 1) // 3) * 3 + 3
    return fd.month_end_of(date(observation.year, last_month, 1))


def _in_column(series: str, observation: date, vintage: date) -> bool:
    """Is the cell present in this vintage column of the synthetic archive?

    All series publish with a lag; the CPIAUCSL fixture is additionally a sparse snapshot (only
    the last ``SPARSE_MONTHS`` months) before ``SPARSE_CPIAUCSL_UNTIL``.
    """
    freq = "Q" if series in QUARTERLY_SERIES else "M"
    lag = QUARTERLY_LAG_DAYS if freq == "Q" else MONTHLY_LAG_DAYS
    if _period_end(freq, observation) + timedelta(days=lag) > vintage:
        return False
    if series == "CPIAUCSL" and vintage < SPARSE_CPIAUCSL_UNTIL:
        sparse_start = vintage - timedelta(days=30 * SPARSE_MONTHS)
        return observation >= sparse_start
    return True


def _fixture_zip(series: str, vintages: tuple[date, ...]) -> bytes:
    freq = "Q" if series in QUARTERLY_SERIES else "M"
    observations = _observation_dates(freq)
    rows = []
    for observation in observations:
        row = [observation.isoformat()]
        for vintage in vintages:
            row.append(_synthetic_value(series, observation, vintage)
                       if _in_column(series, observation, vintage) else "")
        rows.append(row)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["observation_date"] + [f"{series}_{v:%Y%m%d}" for v in vintages])
    writer.writerows(rows)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"vintages_starting_{vintages[0].isoformat()}.csv",
                         buffer.getvalue().encode("utf-8-sig"))
        archive.writestr("README.txt", "synthetic fixture")
    return payload.getvalue()


def _write_latest(path: Path, series: str, freq: str) -> None:
    observations = _observation_dates(freq)
    lines = ["observation_date," + series]
    for observation in observations:
        value = _synthetic_value(series, observation, date(2000, 12, 5))
        lines.append(f"{observation.isoformat()},{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def make_fixture(root: Path) -> Path:
    """Create a synthetic ``data/raw``-like tree: 6 vintage archives + 7 latest tapes."""
    for series in MONTHLY_SERIES:
        directory = root / series
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vintages.zip").write_bytes(_fixture_zip(series, MONTHLY_VINTAGES))
        _write_latest(directory / "latest.csv", series, "M")
    for series in QUARTERLY_SERIES:
        directory = root / series
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vintages.zip").write_bytes(_fixture_zip(series, QUARTERLY_VINTAGES))
        _write_latest(directory / "latest.csv", series, "Q")
    usrec = root / "USREC"
    usrec.mkdir(parents=True, exist_ok=True)
    lines = ["observation_date,USREC"]
    for year in range(1980, 1998):
        for month in range(1, 13):
            flag = 1 if (year, month) in {(1990, 7), (1990, 8), (1991, 1)} else 0
            lines.append(f"{year:04d}-{month:02d}-01,{flag}")
    (usrec / "latest.csv").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return root


def _numeric_arrays(node, path: str = "") -> list[str]:
    """Paths of numeric arrays (potential raw observations) inside a JSON document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_numeric_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in node):
            found.append(path or "<root>")
        else:
            for position, value in enumerate(node):
                found.extend(_numeric_arrays(value, f"{path}[{position}]"))
    return found


class FixtureBuild(unittest.TestCase):
    """One synthetic source tree shared by the class (built once, read many times)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="amfe-pit-")
        base = Path(cls._tmp.name)
        cls.source = make_fixture(base / "raw")
        cls.out = base / "reconstructed"
        cls.dossier_path = base / "pit_reconstruction.json"
        cls.dossier = rp.build(data_root=cls.source, out_root=cls.out,
                               dossier_path=cls.dossier_path,
                               generated_at=FIXED_GENERATED_AT, verbose=False)
        cls.vintages = {series: fd.load_vintages(series, cls.out) for series in rp.RECONSTRUCTED_SERIES}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


# ---------------------------------------------------------------------------------------
# Grid and inclusion rules
# ---------------------------------------------------------------------------------------
class DateGrid(unittest.TestCase):
    def test_grid_is_monthly_on_the_15th_1990_01_to_1996_11(self):
        grid = rp.reconstruction_grid()
        self.assertEqual(len(grid), 83)  # 7 years * 12 - 1
        self.assertEqual(grid[0], date(1990, 1, 15))
        self.assertEqual(grid[-1], date(1996, 11, 15))
        self.assertTrue(all(d.day == 15 for d in grid))
        for previous, current in zip(grid, grid[1:]):
            months = (current.year - previous.year) * 12 + current.month - previous.month
            self.assertEqual(months, 1, f"{previous} -> {current}")

    def test_monthly_inclusion_rule(self):
        # the observation of January 1990 has period_end 1990-01-31
        self.assertFalse(rp.observation_available("CPIAUCSL", "1990-01-01", date(1990, 1, 15)))
        self.assertFalse(rp.observation_available("CPIAUCSL", "1990-01-01", date(1990, 1, 31)))
        self.assertTrue(rp.observation_available("CPIAUCSL", "1990-01-01", date(1990, 2, 1)))

    def test_quarterly_inclusion_rule_is_conservative(self):
        # 1990Q1 ends 1990-03-31; quarter_end + 1 month = 1990-04-30
        self.assertFalse(rp.observation_available("GDPC1", "1990-01-01", date(1990, 4, 15)))
        self.assertFalse(rp.observation_available("GDPC1", "1990-01-01", date(1990, 4, 30)))
        self.assertTrue(rp.observation_available("GDPC1", "1990-01-01", date(1990, 5, 1)))


# ---------------------------------------------------------------------------------------
# Tape override
# ---------------------------------------------------------------------------------------
class TapeOverride(FixtureBuild):
    def test_sparse_series_is_detected_and_overridden(self):
        entry = self.dossier["series"]["CPIAUCSL"]["tape_override"]
        self.assertEqual(entry["full_history_date"], SPARSE_CPIAUCSL_UNTIL.isoformat())
        self.assertGreaterEqual(entry["full_history_observations"], rp.MIN_FULL_HISTORY_OBS)
        self.assertEqual(entry["replaced_real_vintages_count"], 7)
        for replaced in ("1988-03-01", "1989-06-01", "1990-04-20", "1990-06-15",
                         "1990-09-15", "1991-06-14", "1992-09-15"):
            self.assertIn(replaced, entry["replaced_real_vintages"])
        self.assertEqual(entry["kept_real_vintages_count"], len(MONTHLY_VINTAGES) - 7)

    def test_full_history_series_are_not_overridden(self):
        for series in ("CPILFESL", "UNRATE", "GS10", "TB3MS", "GDPC1"):
            entry = self.dossier["series"][series]["tape_override"]
            expected_first = (MONTHLY_VINTAGES if series in MONTHLY_SERIES else QUARTERLY_VINTAGES)[0]
            self.assertEqual(entry["full_history_date"], expected_first.isoformat(), series)
            self.assertEqual(entry["replaced_real_vintages_count"], 0, series)

    def test_tape_override_keeps_the_same_dates_but_with_full_history(self):
        vintages = self.vintages["CPIAUCSL"]
        # the replaced real dates still exist in the archive, now as tape columns
        for replaced in ("1988-03-01", "1990-06-15", "1992-09-15"):
            self.assertIn(date.fromisoformat(replaced), vintages)
        column = vintages[date(1990, 6, 15)]
        self.assertGreaterEqual(len(column), 180,
                                "the tape column before the full-history date must carry full history")
        # and it carries values only up to the reference periods completed before that date
        self.assertLessEqual(column.last_index, fd.month_index(date(1990, 5, 31)))

    def test_genuine_column_wins_after_the_full_history_date(self):
        entry = self.dossier["series"]["CPIAUCSL"]
        self.assertEqual(entry["grid_collisions_with_real_vintages"], ["1995-06-15"])
        self.assertEqual(entry["tape_override"]["replaced_real_vintages_also_on_grid"],
                         ["1990-06-15", "1990-09-15", "1992-09-15"])


# ---------------------------------------------------------------------------------------
# Build output
# ---------------------------------------------------------------------------------------
class BuiltArchive(FixtureBuild):
    def test_every_grid_month_is_covered(self):
        grid = rp.reconstruction_grid()
        for series, vintages in self.vintages.items():
            dates = set(vintages)
            for day in grid:
                if day in dates:
                    continue
                # a genuine vintage column of the same month may replace the tape date
                same_month = [d for d in dates if (d.year, d.month) == (day.year, day.month)]
                self.assertTrue(same_month, f"{series}: no vintage covers {day.isoformat()}")

    def test_genuine_vintages_are_preserved(self):
        for series, vintages in self.vintages.items():
            all_dates = MONTHLY_VINTAGES if series in MONTHLY_SERIES else QUARTERLY_VINTAGES
            full = self.dossier["series"][series]["tape_override"]["full_history_date"]
            kept = [d for d in all_dates if d.isoformat() >= full]
            for day in kept:
                self.assertIn(day, vintages, f"{series}: genuine vintage {day} lost")

    def test_no_lookahead_in_any_vintage(self):
        for series, vintages in self.vintages.items():
            for vintage_date, series_object in vintages.items():
                last = series_object.last_index
                self.assertIsNotNone(last, f"{series}: empty vintage {vintage_date}")
                last_observation = rp._period_first_date(series, last)
                # loose, applies to tape and genuine columns alike: the period must be complete
                self.assertLess(
                    rp.observation_period_end(series, last_observation.isoformat()), vintage_date,
                    f"{series}: vintage {vintage_date} exposes {last_observation}")
                fd.assert_no_lookahead(series_object, vintage_date)

    def test_tape_columns_use_the_conservative_quarterly_lag(self):
        # 1990Q1 ends 1990-03-31; quarter_end + 1 month = 1990-04-30: not available on 1990-04-15
        tape = self.vintages["GDPC1"][date(1990, 4, 15)]
        self.assertLess(tape.last_index, fd.quarter_index(date(1990, 1, 1)))
        later = self.vintages["GDPC1"][date(1990, 5, 15)]
        self.assertEqual(later.last_index, fd.quarter_index(date(1990, 1, 1)))

    def test_latest_tapes_are_copied_byte_for_byte(self):
        for series in ALL_SERIES:
            source = (self.source / series / "latest.csv").read_bytes()
            copied = (self.out / series / "latest.csv").read_bytes()
            self.assertEqual(source, copied, series)
            self.assertEqual(self.dossier["artifacts"]["series"][series]["latest.csv"]["sha256"],
                             rp.sha256_file(self.out / series / "latest.csv"))

    def test_archive_shape_matches_the_reader(self):
        for series in rp.RECONSTRUCTED_SERIES:
            with zipfile.ZipFile(self.out / series / "vintages.zip") as archive:
                members = archive.namelist()
                self.assertEqual(len([m for m in members if m.lower().endswith(".csv")]), 1)
                self.assertIn("README.txt", members)
                header = archive.read([m for m in members if m.lower().endswith(".csv")][0]
                                      ).decode("utf-8-sig").splitlines()[0]
                readme = archive.read("README.txt").decode("utf-8")
            columns = header.split(",")
            self.assertEqual(columns[0], "observation_date")
            self.assertTrue(all(column.startswith(series + "_") for column in columns[1:]))
            self.assertEqual(len(columns) - 1, len(self.vintages[series]))
            self.assertIn("tape override", readme)
            self.assertIn("Full-history date", readme)


# ---------------------------------------------------------------------------------------
# Untouched genuine vintages agree with the source root
# ---------------------------------------------------------------------------------------
class UntouchedGenuineVintages(FixtureBuild):
    def test_origin_after_the_archive_start_matches_the_source(self):
        origin = "1997-06-30"
        for series in rp.RECONSTRUCTED_SERIES:
            raw, raw_meta = fd.available_series(series, origin, data_root=self.source)
            new, new_meta = fd.available_series(series, origin, data_root=self.out)
            self.assertEqual(raw_meta.vintage_date, new_meta.vintage_date, series)
            self.assertEqual(raw.index.tolist(), new.index.tolist(), series)
            self.assertEqual(raw.values.tolist(), new.values.tolist(), series)


# ---------------------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------------------
class Determinism(unittest.TestCase):
    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="amfe-pit-det-") as tmp:
            base = Path(tmp)
            source = make_fixture(base / "raw")
            first = base / "first"
            second = base / "second"
            one = rp.build(data_root=source, out_root=first, dossier_path=None,
                           generated_at=FIXED_GENERATED_AT, verbose=False)
            two = rp.build(data_root=source, out_root=second, dossier_path=None,
                           generated_at=FIXED_GENERATED_AT, verbose=False)
            self.assertEqual(one["artifacts"]["digest"], two["artifacts"]["digest"])
            for series in rp.RECONSTRUCTED_SERIES:
                for name in ("vintages.zip", "latest.csv"):
                    self.assertEqual(rp.sha256_file(first / series / name),
                                     rp.sha256_file(second / series / name),
                                     f"{series}/{name}")

    def test_zip_members_carry_a_fixed_timestamp(self):
        with tempfile.TemporaryDirectory(prefix="amfe-pit-zip-") as tmp:
            base = Path(tmp)
            source = make_fixture(base / "raw")
            out = base / "out"
            rp.build(data_root=source, out_root=out, dossier_path=None,
                     generated_at=FIXED_GENERATED_AT, verbose=False)
            with zipfile.ZipFile(out / "GS10" / "vintages.zip") as archive:
                for info in archive.infolist():
                    self.assertEqual(info.date_time, rp.FIXED_ZIP_TIMESTAMP)


# ---------------------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------------------
class Dossier(FixtureBuild):
    def test_schema_and_status(self):
        dossier = self.dossier
        self.assertEqual(dossier["schema"], "amfe.pit_reconstruction.v2")
        self.assertEqual(dossier["status"], "PROPOSED")
        self.assertEqual(dossier["generated_at"], FIXED_GENERATED_AT)
        for key in ("method", "reconstruction", "series", "tape_override", "look_ahead_disclosure",
                    "artifacts", "rates_zero_revision_check", "verification", "limitations", "usage"):
            self.assertIn(key, dossier)
        self.assertEqual(dossier["reconstruction"]["range"],
                         {"start": "1990-01-15", "end": "1996-11-15"})
        for series in rp.RECONSTRUCTED_SERIES:
            entry = dossier["series"][series]
            expected_first = (MONTHLY_VINTAGES if series in MONTHLY_SERIES else QUARTERLY_VINTAGES)[0]
            self.assertEqual(entry["first_real_vintage"], expected_first.isoformat())
            revisions = entry["revisions"]
            self.assertIn(revisions["revision_class"],
                          {"ZERO", "ZERO_WITHIN_ROUNDING", "SMALL", "MATERIAL"})
            for block in ("earliest_vs_latest", "step_1"):
                for metric in ("n", "mean_abs", "max", "median"):
                    self.assertIn(metric, revisions["levels"][block])
            self.assertIn("tape_override", entry)
            for field in ("full_history_date", "full_history_observations",
                          "replaced_real_vintages_count", "kept_real_vintages_count",
                          "tape_vintages_written", "carrier_after_vintage_date"):
                self.assertIn(field, entry["tape_override"])
        self.assertEqual(dossier["tape_override"]["min_full_history_observations"],
                         rp.MIN_FULL_HISTORY_OBS)
        self.assertEqual(set(dossier["tape_override"]["series"]), set(rp.RECONSTRUCTED_SERIES))
        for series in rp.RECONSTRUCTED_SERIES:
            self.assertIn(series, dossier["tape_override"]["disclosure"])

    def test_dossier_contains_no_series_values(self):
        with self.dossier_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(_numeric_arrays(document), [],
                         "the dossier must contain aggregates only (licence policy)")

    def test_artifact_digests_match_the_files(self):
        for series, files in self.dossier["artifacts"]["series"].items():
            for name, meta in files.items():
                path = self.out / series / name
                self.assertTrue(path.exists(), path)
                self.assertEqual(meta["sha256"], rp.sha256_file(path))
                self.assertEqual(meta["bytes"], path.stat().st_size)
        self.assertEqual(self.dossier["artifacts"]["digest"], rp.tree_digest(self.out))

    def test_rates_have_no_revisions(self):
        rates = self.dossier["rates_zero_revision_check"]["series"]
        for series in ("GS10", "TB3MS"):
            self.assertTrue(rates[series]["zero_revision"], series)
            self.assertEqual(rates[series]["revision_class"], "ZERO", series)
            self.assertEqual(rates[series]["earliest_vs_latest"]["max"], 0.0, series)
            self.assertEqual(rates[series]["step_1"]["max"], 0.0, series)

    def test_gdp_revisions_are_material(self):
        revisions = self.dossier["series"]["GDPC1"]["revisions"]
        self.assertEqual(revisions["revision_class"], "MATERIAL")
        self.assertIn("target_units", revisions)
        self.assertGreater(revisions["target_units"]["earliest_vs_latest"]["max"], 0.5)
        self.assertIn("must not use this data root",
                      self.dossier["look_ahead_disclosure"]["t2_usage"])
        self.assertEqual(self.dossier["look_ahead_disclosure"]["gdp"],
                         "material revisions -> do not use for T2")
        self.assertIn("T2 must not use", self.dossier["tape_override"]["disclosure"]["GDPC1"])

    def test_classification_thresholds_are_declared(self):
        rules = self.dossier["method"]["revision_classification"]
        self.assertEqual(set(rules), {"ZERO", "ZERO_WITHIN_ROUNDING", "SMALL", "MATERIAL"})
        self.assertEqual(rp._classify([0.0, 0.0]), "ZERO")
        self.assertEqual(rp._classify([0.0] * 1000 + [0.01]), "ZERO_WITHIN_ROUNDING")
        self.assertEqual(rp._classify([0.02] * 100 + [0.3]), "SMALL")
        self.assertEqual(rp._classify([1.0, 3.0]), "MATERIAL")


# ---------------------------------------------------------------------------------------
# Rates are reconstructed exactly
# ---------------------------------------------------------------------------------------
class RatesAreExact(FixtureBuild):
    def test_reconstructed_rates_equal_their_earliest_vintage(self):
        for series in ("GS10", "TB3MS"):
            with zipfile.ZipFile(self.source / series / "vintages.zip") as archive:
                member = [m for m in archive.namelist() if m.lower().endswith(".csv")][0]
                rows = list(csv.reader(archive.read(member).decode("utf-8-sig").splitlines()))
            header = rows[0]
            earliest = {}
            for row in rows[1:]:
                for position in range(1, len(header)):
                    if position < len(row) and row[position].strip() not in ("", "."):
                        earliest[row[0]] = float(row[position])
                        break
            reconstructed = self.vintages[series][date(1993, 6, 15)]
            checked = 0
            for order, period in enumerate(reconstructed.index):
                observation = rp._period_first_date(series, int(period)).isoformat()
                if observation not in earliest:
                    continue
                self.assertAlmostEqual(float(reconstructed.values[order]), earliest[observation],
                                       places=9)
                checked += 1
            self.assertGreater(checked, 50, series)

    def test_reconstructed_rates_are_constant_across_vintages(self):
        for series in ("GS10", "TB3MS"):
            vintages = self.vintages[series]
            dates = [day for day in rp.reconstruction_grid() if day in vintages]
            self.assertGreater(len(dates), 40)
            values = [vintages[day].values_dict() for day in dates]
            common = set(values[0])
            for mapping in values[1:]:
                common &= set(mapping)
            self.assertGreater(len(common), 10)
            for period in common:
                self.assertEqual({mapping[period] for mapping in values}, {values[0][period]}, series)


# ---------------------------------------------------------------------------------------
# Early origin: full history, no InsufficientData, models run
# ---------------------------------------------------------------------------------------
class EarlyOrigin(FixtureBuild):
    def setUp(self):
        self._old_env = os.environ.get("AMFE_DATA_ROOT")
        os.environ["AMFE_DATA_ROOT"] = str(self.out)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AMFE_DATA_ROOT", None)
        else:
            os.environ["AMFE_DATA_ROOT"] = self._old_env

    def test_cpiaucsl_has_full_history_at_1990_06(self):
        context = fd.DataContext.from_local("1990-06-30")
        self.assertEqual(context.data_class, "REAL_TIME")
        cpi = context.require("CPIAUCSL")
        self.assertGreaterEqual(len(cpi), 180)
        fd.assert_no_lookahead(cpi, "1990-06-30")
        self.assertLessEqual(cpi.last_index, fd.month_index(date(1990, 6, 30)))
        yoy = context.target_series("T1_CPI_YOY")
        self.assertGreaterEqual(len(yoy), 60)

    def test_forecast_models_run_at_1990_06(self):
        for model_id in ("RW", "AR_BIC", "AO"):
            result = forecast(model_id, "T1_CPI_YOY", "1990-06-30", 1)
            self.assertTrue(math.isfinite(result.point), model_id)
            self.assertTrue(all(math.isfinite(value) for value in result.quantiles), model_id)
            self.assertLess(result.quantiles[0], result.quantiles[-1], model_id)

    def test_env_root_is_used_by_the_reader(self):
        vintages = fd.load_vintages("GS10")
        self.assertIn(date(1990, 1, 15), vintages)
        self.assertEqual(min(vintages), MONTHLY_VINTAGES[0])
        series, meta = fd.available_series("CPIAUCSL", "1990-06-30")
        self.assertEqual(meta.mode, "vintage")
        self.assertEqual(meta.vintage_date, date(1990, 6, 15))
        self.assertGreaterEqual(len(series), 180)
        self.assertLessEqual(series.last_index, fd.month_index(date(1990, 6, 30)))

    def test_origin_1996_06_keeps_genuine_columns_untouched(self):
        """A genuine vintage selected by the source root is byte-identical in the new root."""
        for series in rp.RECONSTRUCTED_SERIES:
            raw, raw_meta = fd.available_series(series, "1996-06-30", data_root=self.source)
            new_root_vintages = fd.load_vintages(series, self.out)
            if raw_meta.vintage_date is None:
                # the source archive has no genuine vintage before 1996-12 for these series:
                # the tape override is exactly what provides the information set there
                self.assertIsNotNone(fd.vintage_as_of(series, "1996-06-30", self.out), series)
                continue
            self.assertIn(raw_meta.vintage_date, new_root_vintages, series)
            same = new_root_vintages[raw_meta.vintage_date]
            self.assertEqual(raw.index.tolist(), same.index.tolist(), series)
            self.assertEqual(raw.values.tolist(), same.values.tolist(), series)


# ---------------------------------------------------------------------------------------
# Local artefacts (skipped when data/reconstructed has not been built)
# ---------------------------------------------------------------------------------------
LOCAL_BUILD = (ROOT / "data" / "reconstructed").is_dir()
DOSSIER_FILE = ROOT / "protocols" / "pit_reconstruction.json"


@unittest.skipUnless(LOCAL_BUILD and DOSSIER_FILE.exists(),
                     "data/reconstructed is not built locally (git-ignored)")
class LocalArtefacts(unittest.TestCase):
    def test_dossier_matches_the_local_build(self):
        with DOSSIER_FILE.open("r", encoding="utf-8") as handle:
            dossier = json.load(handle)
        self.assertEqual(dossier["status"], "PROPOSED")
        root = ROOT / "data" / "reconstructed"
        self.assertEqual(dossier["artifacts"]["digest"], rp.tree_digest(root))
        for series in rp.RECONSTRUCTED_SERIES:
            for name, meta in dossier["artifacts"]["series"][series].items():
                self.assertEqual(meta["sha256"], rp.sha256_file(root / series / name))
        self.assertTrue(dossier["rates_zero_revision_check"]["series"]["GS10"]["zero_revision"])

    def test_tape_override_on_the_real_archive(self):
        with DOSSIER_FILE.open("r", encoding="utf-8") as handle:
            dossier = json.load(handle)
        entry = dossier["tape_override"]["series"]["CPIAUCSL"]
        self.assertEqual(entry["full_history_date"], "1994-03-16")
        self.assertGreaterEqual(entry["full_history_observations"], 120)
        self.assertGreater(entry["replaced_real_vintages_count"], 0)
        self.assertEqual(entry["replaced_real_vintages_count"],
                         len(entry["replaced_real_vintages"]))
        for series in ("CPILFESL", "UNRATE", "GS10", "TB3MS", "GDPC1"):
            self.assertEqual(dossier["tape_override"]["series"][series]
                             ["replaced_real_vintages_count"], 0, series)
        self.assertIn("T2 must not use", dossier["tape_override"]["disclosure"]["GDPC1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
