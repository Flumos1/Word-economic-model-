#!/usr/bin/env python3
"""Offline tests of the alternative full-world MRIO importer (tools/alt_mrio_import.py).

No network and no pandas are required: the parsers are exercised through the *layout detector*
and the plain ``table`` interface, the downloader through ``file://`` sources, the archive
inspection through a zip fixture, and the reports through their documented fields.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import alt_mrio_import as importer  # noqa: E402


class FakeFrame:
    """The minimal frame interface ``wiod_layout``/``find_label_columns`` need."""

    def __init__(self, columns):
        self.columns = list(columns)


def wiod_columns(countries=("AAA", "BBB"), sectors=2, fd=5):
    columns = ["IndustryCode", "IndustryDescription", "Country", "RNr", "Year"]
    for country in countries:
        columns.extend(f"v{country}{n}" for n in range(1, sectors + 1))
        columns.extend(f"v{country}{n}" for n in range(sectors + 1, sectors + fd + 1))
    columns.append("TOT")
    return columns


class LayoutTest(unittest.TestCase):
    def test_wiod_layout_finds_sectors_and_final_demand(self):
        columns = wiod_columns()
        layout = importer.wiod_layout(FakeFrame(columns))
        self.assertEqual(layout["country_codes"], ["AAA", "BBB"])
        self.assertEqual(len(layout["sector_columns"]), 4)  # 2 countries x 2 sectors
        self.assertEqual(len(layout["fd_columns"]), 10)     # 2 countries x 5 categories
        self.assertIn("vAAA1", layout["sector_columns"])
        self.assertIn("vAAA3", layout["fd_columns"])

    def test_wiod_layout_rejects_incomplete_grid(self):
        columns = [column for column in wiod_columns() if column != "vAAA3"]
        with self.assertRaises(importer.ImportBlocked):
            importer.wiod_layout(FakeFrame(columns))

    def test_find_label_columns_requires_named_columns(self):
        with self.assertRaises(importer.ImportBlocked):
            importer.find_label_columns(FakeFrame(["a", "b", "TOT"]))
        layout = importer.find_label_columns(FakeFrame(wiod_columns()))
        self.assertEqual(layout["country_column"], "Country")


class MemberSelectionTest(unittest.TestCase):
    def test_member_is_chosen_by_year(self):
        members = [{"name": "WIOT2000_October16_ROW.dta", "is_dir": False},
                   {"name": "WIOT2014_October16_ROW.dta", "is_dir": False},
                   {"name": "README.txt", "is_dir": False}]
        self.assertEqual(importer.select_member(members, 2014), "WIOT2014_October16_ROW.dta")
        self.assertEqual(importer.select_member(members, 2000), "WIOT2000_October16_ROW.dta")

    def test_ambiguous_member_is_refused(self):
        members = [{"name": "table_a.dta", "is_dir": False},
                   {"name": "table_b.dta", "is_dir": False}]
        with self.assertRaises(importer.ImportBlocked):
            importer.select_member(members, 2014)


class ArchiveTest(unittest.TestCase):
    def test_inspect_archive_reports_members_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("WIOT2014.dta", b"x" * 16)
            info = importer.inspect_archive(path)
            self.assertEqual(info["bytes"], path.stat().st_size)
            self.assertRegex(info["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(info["members"][0]["name"], "WIOT2014.dta")
            self.assertEqual(info["members"][0]["bytes"], 16)

    def test_missing_archive_is_blocked(self):
        with self.assertRaises(importer.ImportBlocked):
            importer.inspect_archive(Path("does-not-exist.zip"))


class DownloadTest(unittest.TestCase):
    def test_download_with_size_and_md5_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.bin"
            source.write_bytes(b"abc" * 1000)
            target = Path(tmp) / "out.bin"
            result = importer.download(source.as_uri(), target, expected_bytes=3000,
                                       expected_md5=importer.md5_file(source))
            self.assertEqual(result["bytes"], 3000)
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_download_rejects_wrong_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.bin"
            source.write_bytes(b"abc")
            with self.assertRaises(importer.ImportBlocked):
                importer.download(source.as_uri(), Path(tmp) / "out.bin", expected_bytes=999)

    def test_total_from_content_range(self):
        self.assertEqual(importer._total_from_range("bytes 0-2047/638963496"), 638963496)
        self.assertIsNone(importer._total_from_range(None))


class TableMappingTest(unittest.TestCase):
    @staticmethod
    def table():
        keys = [("AAA", "S1"), ("AAA", "S2"), ("BBB", "S1"), ("BBB", "S2")]
        Z = [[10.0, 2.0, 1.0, 0.5], [3.0, 5.0, 0.0, 1.0],
             [2.0, 1.0, 4.0, 2.0], [0.0, 1.0, 1.0, 3.0]]
        va = [5.0, 4.0, 3.0, 4.0]
        column_sums = [sum(row[j] for row in Z) for j in range(4)]
        x = [round(column_sums[j] + va[j], 6) for j in range(4)]
        y = [round(x[i] - sum(Z[i]), 6) for i in range(4)]
        return {"row_keys": keys, "sector_columns": [f"v{c}{i}" for c, i in
                                                    (("AAA", 1), ("AAA", 2), ("BBB", 1),
                                                     ("BBB", 2))],
                "Z": Z, "y": y, "x": x, "va": va, "go": x, "year": 2014}

    def test_build_dataset_maps_the_canonical_schema(self):
        dataset = importer.build_dataset(self.table(), year=2014,
                                         source=importer.SOURCES["wiod_2016"])
        self.assertEqual(dataset.countries, ("AAA", "BBB"))
        self.assertEqual(dataset.industries, ("S1", "S2"))
        self.assertEqual(dataset.units, "USD_mn")
        self.assertEqual(dataset.years, (2014,))
        from amfe.mrio.schema import validate_dataset

        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)

    def test_differs_from_wiod_countries_constant_is_documented(self):
        # the real WIOD file carries 44 codes (ROW included) in file order; the constant lists them
        self.assertEqual(len(importer.WIOD_COUNTRIES), 44)
        self.assertIn("ROW", importer.WIOD_COUNTRIES)

    def test_negative_final_demand_is_rejected_by_the_schema_before_validation(self):
        table = self.table()
        table["y"][0] = -5.0
        with self.assertRaises(Exception):
            importer.build_dataset(table, year=2014, source=importer.SOURCES["wiod_2016"])


class ReportTest(unittest.TestCase):
    def test_blocked_report_lists_manual_steps_and_no_values(self):
        report = importer.blocked_report()
        text = json.dumps(report, ensure_ascii=False)
        self.assertIn("oecd_icio", text)
        self.assertIn("eora_mrio", text)
        self.assertIn("manual_steps", text)
        self.assertNotIn("dataset_", text)

    def test_manual_steps_are_actionable(self):
        steps = importer._manual_steps(importer.SOURCES["wiod_2016"])
        self.assertTrue(any("--download wiod_2016" in step for step in steps))
        oecd = importer._manual_steps(importer.SOURCES["oecd_icio"])
        self.assertTrue(any("browser" in step for step in oecd))

    def test_check_local_on_empty_root_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = importer.check_local(Path(tmp))
            self.assertEqual(status["status"], "BLOCKED_DATA")
            self.assertEqual(status["datasets"], [])


class ProbeTest(unittest.TestCase):
    def test_probe_url_records_a_failure_without_network(self):
        result = importer.probe_url("http://127.0.0.1:9/never", timeout=0.5)
        self.assertFalse(result["reachable"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["checked_at"][-1], "Z")

    def test_probe_sources_covers_every_source(self):
        with mock.patch.object(importer, "probe_url",
                               return_value={"url": "x", "reachable": True}):
            probes = importer.probe_sources(timeout=0.1)
        self.assertEqual({item["source_id"] for item in probes}, set(importer.SOURCES))
        self.assertTrue(all(item["probes"] for item in probes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
