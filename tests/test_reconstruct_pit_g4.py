"""Tests for the G4-A extended PIT coverage audit (``tools/reconstruct_pit.py --g4``).

Everything runs offline on **synthetic fixtures**: the tests never read ``data/raw`` or
``data/reconstructed`` (both are git-ignored downloads).  The fixture covers every coverage lens of
the audit:

* ``ARCHSER``   -- genuine ALFRED columns covering the whole window                -> ARCHIVED_PIT;
* ``TAPEEXACT`` -- archive starts just after the window, zero revisions            -> REAL_TIME_RECONSTRUCTED_MARKET class;
* ``TAPESMALL`` -- first-print tape with small revisions                           -> RECONSTRUCTED_PIT class;
* ``TAPEMAT``   -- first-print tape with material revisions                        -> DIAGNOSTIC_ONLY class;
* ``LATESER``   -- archive starts later than the tape tolerance                    -> UNVERIFIED_LATE_ARCHIVE, excluded;
* ``LATESTONLY``-- no archive but latest.csv                                       -> LATEST_ONLY (label source);
* ``MISSING``   -- nothing at all                                                 -> MISSING_SOURCE, excluded.

Covered contracts
-----------------
* lens rules and their thresholds, revision classes and the protocol-v5 class mapping;
* the built data root: genuine-only archives for ARCHIVED_PIT, tape archives for TAPE_RECONSTRUCTED,
  latest-only copy for LATEST_ONLY, explicit exclusion lists;
* PIT invariants on every built archive (no observation whose reference period ends after the
  vintage date), aggregates-only coverage report (no series values), deterministic rerun;
* the targets/regressors blocks and the "what may be used where" declarations;
* the preregistration document ``protocols/pit_preregistration_0_3.json``: structure, the SHA-256
  content hash, the ``.sha256.txt`` pin, frozen-threshold agreement with the tool and the protocol
  pin agreement with the repository files.

Run from the repository root: ``python tests/test_reconstruct_pit_g4.py``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
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

FIXED_GENERATED_AT = "2026-11-17T00:00:00+00:00"
PREREG = ROOT / "protocols" / "pit_preregistration_0_3.json"
PREREG_SHA = ROOT / "protocols" / "pit_preregistration_0_3.sha256.txt"
SENTINEL = "12345.6789"  # must never appear in the coverage report
MONTHLY_LAG_DAYS = 20
QUARTERLY_LAG_DAYS = 70

FIXTURE_SERIES = ("ARCHSER", "TAPEEXACT", "TAPESMALL", "TAPEMAT", "LATESER", "LATESTONLY", "MISSING")


# ----------------------------------------------------------------------------------------------
# Synthetic fixture
# ----------------------------------------------------------------------------------------------
def _observation_dates(first_year: int = 1960, last_year: int = 1997) -> list[date]:
    return [date(year, month, 1) for year in range(first_year, last_year + 1) for month in range(1, 13)]


def _period_end(observation: date) -> date:
    return fd.month_end_of(observation)


def _value(series: str, observation: date, vintage: date) -> str:
    """Deterministic synthetic level with the declared revision patterns."""
    base = 100.0 + 0.25 * ((observation.year - 1985) * 12 + observation.month - 1)
    if series == "TAPESMALL":
        base += 0.01  # keeps the mean revision tiny but non-zero
        if vintage >= date(1997, 3, 1) and observation < date(1991, 1, 1):
            base += 5.0
    elif series == "TAPEMAT":
        if vintage >= date(1993, 6, 1) and observation < date(1992, 1, 1):
            base += 500.0
    elif series == "ARCHSER":
        base += 0.0  # zero revisions
    return f"{base:.4f}"


def _write_latest(path: Path) -> None:
    lines = ["observation_date,SERIES"]
    for observation in _observation_dates():
        lines.append(f"{observation.isoformat()},{_value('LATESTONLY', observation, date(2001, 1, 1))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _write_archive(path: Path, series: str, vintages: tuple[date, ...]) -> None:
    observations = _observation_dates()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["observation_date"] + [f"{series}_{v:%Y%m%d}" for v in vintages])
    for observation in observations:
        row = [observation.isoformat()]
        for vintage in vintages:
            cell = (""
                    if _period_end(observation) + timedelta(days=MONTHLY_LAG_DAYS) > vintage
                    else _value(series, observation, vintage))
            row.append(cell)
        if any(cell for cell in row[1:]):
            writer.writerow(row)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"vintages_starting_{vintages[0].isoformat()}.csv",
                         buffer.getvalue().encode("utf-8-sig"))
        archive.writestr("README.txt", "synthetic G4 fixture")
    path.write_bytes(payload.getvalue())


def _quarterly(year: int, month: int, day: int = 15) -> date:
    return date(year, month, day)


ARCHSER_VINTAGES = tuple([date(1989, 6, 15)] + [_quarterly(year, month) for year in range(1990, 1997)
                                                for month in (3, 6, 9, 12)])
TAPE_VINTAGES = (date(1996, 12, 3), date(1997, 3, 4), date(1998, 6, 3))
TAPEMAT_VINTAGES = (date(1991, 12, 31), date(1993, 6, 25), date(1995, 6, 26), date(1996, 12, 25))
LATESER_VINTAGES = (date(2001, 6, 13), date(2003, 9, 25), date(2006, 12, 21))


def make_fixture(root: Path) -> Path:
    """Create a synthetic ``data/raw``-like tree with one series per coverage lens."""
    archives = {"ARCHSER": ARCHSER_VINTAGES, "TAPEEXACT": TAPE_VINTAGES,
                "TAPESMALL": TAPE_VINTAGES, "TAPEMAT": TAPEMAT_VINTAGES,
                "LATESER": LATESER_VINTAGES}
    for series, vintages in archives.items():
        directory = root / series
        directory.mkdir(parents=True, exist_ok=True)
        _write_archive(directory / "vintages.zip", series, vintages)
        _write_latest(directory / "latest.csv")
    latest_only = root / "LATESTONLY"
    latest_only.mkdir(parents=True, exist_ok=True)
    _write_latest(latest_only / "latest.csv")
    return root


class G4Fixture(unittest.TestCase):
    """One synthetic source tree and one G4 build shared by the test classes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="amfe-g4-")
        base = Path(cls._tmp.name)
        cls.source = make_fixture(base / "raw")
        cls.out = base / "reconstructed"
        cls.coverage_path = cls.out / "g4_coverage.json"
        cls.coverage = rp.build_g4(data_root=cls.source, out_root=cls.out,
                                   coverage_path=cls.coverage_path, series=FIXTURE_SERIES,
                                   generated_at=FIXED_GENERATED_AT, verbose=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def series(self, name: str) -> dict:
        return self.coverage["series"][name]


class LensRules(G4Fixture):
    def test_lens_classification_per_series(self):
        expected = {"ARCHSER": "ARCHIVED_PIT", "TAPEEXACT": "TAPE_RECONSTRUCTED",
                    "TAPESMALL": "TAPE_RECONSTRUCTED", "TAPEMAT": "TAPE_RECONSTRUCTED",
                    "LATESER": "UNVERIFIED_LATE_ARCHIVE", "LATESTONLY": "LATEST_ONLY",
                    "MISSING": "MISSING_SOURCE"}
        for name, lens in expected.items():
            self.assertEqual(self.series(name)["lens"], lens, name)

    def test_window_counts_and_dates_are_recorded(self):
        archived = self.series("ARCHSER")
        self.assertGreaterEqual(archived["real_vintages_in_window"], rp.G4_MIN_ARCHIVED_VINTAGES)
        self.assertLessEqual(archived["first_real_vintage"], "1990-01-15")
        for year in rp.G4_WINDOW_YEARS:
            self.assertIn(str(year), archived["vintage_dates_by_year"])
        late = self.series("LATESER")
        self.assertEqual(late["real_vintages_in_window"], 0)
        self.assertIn(late["lens"], {"UNVERIFIED_LATE_ARCHIVE"})

    def test_revision_classes_and_protocol_class_mapping(self):
        expected = {
            "ARCHSER": ("REAL_TIME", "USABLE_GENUINE_ARCHIVE"),
            "TAPEEXACT": ("REAL_TIME_RECONSTRUCTED_MARKET", "USABLE_EXACT_WITH_TAPE_DISCLOSURE"),
            "TAPESMALL": ("RECONSTRUCTED_PIT", "USABLE_WITH_REAL_TIME_SENSITIVITY"),
            "TAPEMAT": ("DIAGNOSTIC_ONLY", "DIAGNOSTIC_ONLY"),
            "LATESER": ("NOT_AVAILABLE", "EXCLUDED_NO_IN_WINDOW_VINTAGE"),
            "LATESTONLY": ("LATEST_TAPE_ONLY", "LABEL_SOURCE_ONLY"),
            "MISSING": ("NOT_AVAILABLE", "EXCLUDED_MISSING_SOURCE"),
        }
        for name, (protocol_class, usage) in expected.items():
            entry = self.series(name)
            self.assertEqual(entry["protocol_class"], protocol_class, name)
            self.assertEqual(entry["usage_status"], usage, name)
        self.assertEqual(self.series("TAPEEXACT")["revision_class"], "ZERO")
        self.assertEqual(self.series("TAPESMALL")["revision_class"], "SMALL")
        self.assertEqual(self.series("TAPEMAT")["revision_class"], "MATERIAL")

    def test_thresholds_are_the_declared_constants(self):
        self.assertEqual(rp.G4_MIN_ARCHIVED_VINTAGES, 12)
        self.assertEqual(rp.G4_TAPE_MAX_DELAY_MONTHS, 12)
        self.assertEqual(rp.MIN_FULL_HISTORY_OBS, 120)
        self.assertEqual(rp.G4_WINDOW_YEARS, tuple(range(1990, 1997)))
        self.assertEqual(self.coverage["min_archived_vintages"], 12)
        self.assertEqual(self.coverage["lens_rules"], dict(rp.G4_LENSES))


class BuildOutputs(G4Fixture):
    def test_built_and_excluded_series(self):
        self.assertEqual(self.coverage["built_series"],
                         ["ARCHSER", "LATESTONLY", "TAPEEXACT", "TAPEMAT", "TAPESMALL"])
        excluded = {item["series"]: item for item in self.coverage["excluded"]}
        self.assertEqual(sorted(excluded), ["LATESER", "MISSING"])
        self.assertEqual(excluded["LATESER"]["lens"], "UNVERIFIED_LATE_ARCHIVE")
        self.assertEqual(excluded["MISSING"]["lens"], "MISSING_SOURCE")

    def test_genuine_archive_and_tape_archive_contents(self):
        archived = self.series("ARCHSER")["build"]
        self.assertEqual(archived["build_kind"], "GENUINE_ARCHIVE")
        self.assertEqual(self.series("ARCHSER")["build_status"], "BUILT")
        tape = self.series("TAPEEXACT")["build"]
        self.assertEqual(tape["build_kind"], "TAPE_RECONSTRUCTED")
        self.assertGreaterEqual(tape["reconstructed_vintages_written"], 1)
        latest = self.series("LATESTONLY")["build"]
        self.assertEqual(latest["build_kind"], "LATEST_ONLY")
        self.assertIsNone(latest["vintages_zip"])
        self.assertTrue((self.out / "LATESTONLY" / "latest.csv").exists())
        self.assertFalse((self.out / "LATESER").exists())
        self.assertFalse((self.out / "MISSING").exists())

    def test_coverage_by_year_is_aggregate(self):
        by_year = self.coverage["coverage_by_year"]
        self.assertEqual(sorted(by_year), [str(year) for year in rp.G4_WINDOW_YEARS])
        for year, block in by_year.items():
            self.assertIn("series_with_vintage", block)
            self.assertIn("vintage_columns", block)
            self.assertIsInstance(block["series"], list)
            self.assertEqual(block["series_with_vintage"], len(block["series"]), year)
            self.assertGreaterEqual(block["vintage_columns"], block["series_with_vintage"])
        self.assertGreater(sum(block["series_with_vintage"] for block in by_year.values()), 0)

    def test_targets_and_regressors_blocks(self):
        targets = self.coverage["targets"]
        self.assertEqual(sorted(targets), ["T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"])
        for block in targets.values():
            self.assertIn("inputs", block)
            self.assertIn("allowed_use", block)
            self.assertTrue(block["allowed_use"])
        self.assertIn("NBER", targets["T3_RECESSION"]["allowed_use"])
        self.assertIn("REAL_TIME", targets["T1_CPI_YOY"]["allowed_use"])
        self.assertIn("REAL_TIME-only", targets["T2_GDP_GROWTH"]["allowed_use"])
        self.assertIsInstance(self.coverage["regressors"], list)

    def test_roles_come_from_the_declared_table(self):
        for name in FIXTURE_SERIES:
            self.assertIn("role", self.series(name))
            self.assertIn("protocol_use", self.series(name))
        self.assertEqual(rp.G4_ROLES["GDPC1"]["role"], "T2 target")
        self.assertIn("CPIAUCSL", rp.G4_ROLES)
        self.assertEqual(len(rp.G4_FALLBACK_SERIES), 19)


class Invariants(G4Fixture):
    def test_no_observation_after_its_vintage_date(self):
        for name in self.coverage["built_series"]:
            if not (self.out / name / "vintages.zip").exists():
                continue
            for vintage, series in fd.load_vintages(name, self.out).items():
                last = series.last_index
                self.assertIsNotNone(last, (name, vintage))
                observation = rp._period_first_date(name, last)
                period_end = rp.observation_period_end(name, observation.isoformat())
                self.assertLess(period_end, vintage, (name, vintage, observation))

    def test_report_contains_no_series_values(self):
        text = json.dumps(self.coverage)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("12345", text)

        def numeric_arrays(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from numeric_arrays(value, f"{path}.{key}")
            elif isinstance(node, list):
                if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                                for item in node):
                    yield path, len(node)
                else:
                    for position, item in enumerate(node):
                        yield from numeric_arrays(item, f"{path}[{position}]")

        for path, length in numeric_arrays(self.coverage):
            self.assertLessEqual(length, 12, f"value-like array at {path}")

    def test_deterministic_rerun(self):
        second = rp.build_g4(data_root=self.source, out_root=self.out,
                             coverage_path=self.coverage_path, series=FIXTURE_SERIES,
                             generated_at=FIXED_GENERATED_AT, verbose=False)
        first_copy = json.loads(json.dumps(self.coverage))
        second_copy = json.loads(json.dumps(second))
        for payload in (first_copy, second_copy):
            payload.pop("generated_at", None)
            payload.get("artifacts", {}).pop("data_root", None)
        self.assertEqual(first_copy, second_copy)
        self.assertEqual(self.coverage["artifacts"]["tree_digest"],
                         second["artifacts"]["tree_digest"])

    def test_missing_series_never_crashes_the_build(self):
        self.assertIn("MISSING", {entry["series"] for entry in self.coverage["excluded"]})
        self.assertNotIn("MISSING", self.coverage["built_series"])


class CliAndPlan(unittest.TestCase):
    def test_plan_series_ids_default_is_the_19_series_plan(self):
        ids = rp.plan_series_ids()
        self.assertEqual(len(ids), 19)
        for required in ("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS", "USREC",
                         "PAYEMS", "INDPRO"):
            self.assertIn(required, ids)

    def test_cli_g4_writes_coverage_and_not_the_dossier(self):
        with tempfile.TemporaryDirectory(prefix="amfe-g4-cli-") as tmp:
            base = Path(tmp)
            source = make_fixture(base / "raw")
            out = base / "reconstructed"
            coverage_path = out / "g4_coverage.json"
            code = rp.main(["--g4", "--data-root", str(source), "--out-root", str(out),
                            "--g4-coverage", str(coverage_path),
                            "--g4-series", *FIXTURE_SERIES, "--quiet"])
            self.assertEqual(code, 0)
            self.assertTrue(coverage_path.exists())
            payload = json.loads(coverage_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], rp.G4_COVERAGE_SCHEMA)
            # the default G4 path must not write the committed dossier
            self.assertFalse((ROOT / "protocols" / "pit_reconstruction.json.tmp").exists())

    def test_cli_g4_rejects_a_missing_data_root(self):
        with tempfile.TemporaryDirectory(prefix="amfe-g4-missing-") as tmp:
            with self.assertRaises(rp.ReconstructionError):
                rp.main(["--g4", "--data-root", str(Path(tmp) / "absent"),
                         "--out-root", str(Path(tmp) / "out"), "--quiet"])


@unittest.skipUnless(PREREG.exists(), "preregistration file is not present")
class Preregistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(PREREG.read_text(encoding="utf-8"))

    def test_structure_and_status(self):
        payload = self.payload
        for key in ("schema", "preregistration_id", "version", "status", "created_at",
                    "supersedes_lineage", "protocol_basis", "data_basis", "lens_calibration",
                    "extended_window_design", "acceptance_criteria", "post_hoc_prohibitions",
                    "amendment_rule", "reproduce", "limitations", "content_sha256"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema"], "amfe.pit_preregistration.v1")
        self.assertEqual(payload["version"], "0.3.0")
        self.assertEqual(payload["status"], "PROPOSED")
        self.assertIn("PIT", payload["preregistration_id"])
        self.assertIn("0.3", payload["preregistration_id"])
        self.assertIn("G4", payload["author_track"])
        self.assertGreaterEqual(len(payload["post_hoc_prohibitions"]), 3)
        self.assertTrue(payload["amendments"] if "amendments" in payload else
                        payload["amendment_rule"])
        # a preregistration must not carry evaluation results
        for forbidden in ("results", "observed_metrics", "oos_results", "losses"):
            self.assertNotIn(forbidden, payload)

    def test_content_hash_and_sha256_pin(self):
        payload = dict(self.payload)
        expected = payload.pop("content_sha256")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), expected)
        file_sha = hashlib.sha256(PREREG.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        pin = PREREG_SHA.read_text(encoding="utf-8").strip().split()[0]
        self.assertEqual(pin, file_sha)

    def test_frozen_thresholds_match_the_tool(self):
        data_basis = self.payload["data_basis"]
        thresholds = data_basis["frozen_thresholds"]
        self.assertEqual(thresholds["min_full_history_observations"], rp.MIN_FULL_HISTORY_OBS)
        self.assertEqual(thresholds["min_archived_vintages"], rp.G4_MIN_ARCHIVED_VINTAGES)
        self.assertEqual(thresholds["tape_max_delay_months"], rp.G4_TAPE_MAX_DELAY_MONTHS)
        self.assertEqual(thresholds["revision_classes"], rp.REVISION_CLASS_RULES)
        self.assertEqual(data_basis["window"]["start"], rp.RECON_START.isoformat())
        self.assertEqual(data_basis["window"]["end"], rp.RECON_END.isoformat())

    def test_protocol_and_dossier_pins_match_the_repository(self):
        basis = self.payload["protocol_basis"]
        for key, filename in (("v5", "protocols/evaluation_protocol_v5.json"),
                             ("v4", "protocols/evaluation_protocol_v4.json")):
            path = ROOT / filename
            actual = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            self.assertEqual(basis[key]["sha256"], actual, filename)
        dossier_pin = (ROOT / "protocols" / "pit_reconstruction.sha256").read_text(encoding="utf-8")
        expected_pin = dossier_pin.split()[0]
        self.assertEqual(self.payload["data_basis"]["reconstruction_dossier"]["sha256"], expected_pin)

    def test_lens_statuses_are_declared_and_valid(self):
        statuses = self.payload["data_basis"]["series_status"]
        self.assertGreaterEqual(len(statuses), 15)
        for name, entry in statuses.items():
            self.assertIn(name, rp.plan_series_ids())
            self.assertIn(entry["lens"], rp.G4_LENSES)
            self.assertIn(entry["protocol_class"], set(rp.G4_PROTOCOL_CLASS.values()))
            self.assertIn("usage_status", entry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
