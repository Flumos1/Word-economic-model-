#!/usr/bin/env python3
"""Offline tests of the OECD ICIO importer (tools/oecd_import.py).

No network: probes are exercised through a closed port / mock, parsing through small synthetic
long- and wide-layout fixtures, the dataset builder through a generated 10 x 31 ICIO-like table
that satisfies the accounting identities, and the D1 gate through positive and mutated cases.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import oecd_import as oecd  # noqa: E402


def tidy_csv(rows) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Row Country", "Row Industry", "Column Country", "Column Industry",
                     "Value", "Year"])
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def small_cells(countries=("AAA", "BBB"), industries=("D01T02", "D10T12"), year=2019):
    """A 2x2 ICIO-like cell list with exact identities (for parser/build tests)."""
    cells = []
    z = {(0, 0): 10.0, (0, 1): 2.0, (1, 0): 3.0, (1, 1): 5.0}
    y = {0: 4.0, 1: 6.0}
    inv = {0: -1.0, 1: 0.5}
    index = {pair: number for number, pair in enumerate(
        (country, industry) for country in countries for industry in industries)}
    for (i, j), value in z.items():
        cells.append({"year": year, "row_country": countries[i], "row_industry": industries[j],
                      "col_country": countries[i], "col_industry": industries[j], "value": value})
    for i in range(2):
        cells.append({"year": year, "row_country": countries[i],
                      "row_industry": industries[i], "col_country": countries[i],
                      "col_industry": "HFCE", "value": y[i]})
        cells.append({"year": year, "row_country": countries[i],
                      "row_industry": industries[i], "col_country": countries[i],
                      "col_industry": "INV", "value": inv[i]})
    for pair, number in index.items():
        cells.append({"year": year, "row_country": pair[0], "row_industry": "VA",
                      "col_country": countries[number % 2], "col_industry": pair[1],
                      "value": 0.0})
    return cells, z, y, inv


class LayoutTest(unittest.TestCase):
    def test_detect_long_layout(self):
        header = ["Row Country", "Row Industry", "Column Country", "Column Industry", "Value",
                  "Year"]
        self.assertEqual(oecd.detect_layout(header), "long")

    def test_detect_wide_layout(self):
        header = ["Country", "Industry", "AAA_D01T02", "AAA_HFCE", "BBB_D10T12"]
        self.assertEqual(oecd.detect_layout(header), "wide")

    def test_detect_sdmx_layout(self):
        header = ["REF_AREA", "ACTIVITY", "OBS_VALUE"]
        self.assertEqual(oecd.detect_layout(header), "sdmx")

    def test_classify_column(self):
        mapping = oecd.classify_column(["Row Country", "Row Industry", "Column Country",
                                        "Column Industry", "Value", "Year"])
        self.assertEqual(mapping["row_country"], "Row Country")
        self.assertEqual(mapping["col_industry"], "Column Industry")
        self.assertEqual(mapping["value"], "Value")
        self.assertEqual(mapping["year"], "Year")


class ParseTest(unittest.TestCase):
    def test_parse_tidy_rows(self):
        rows = [{"Row Country": "AAA", "Row Industry": "D01T02", "Column Country": "BBB",
                 "Column Industry": "D10T12", "Value": "2.5", "Year": "2019"}]
        cells = oecd.parse_tidy_rows(rows, oecd.classify_column(list(rows[0])))
        self.assertEqual(cells, [{"year": 2019, "row_country": "AAA",
                                  "row_industry": "D01T02", "col_country": "BBB",
                                  "col_industry": "D10T12", "value": 2.5}])

    def test_parse_tidy_rejects_non_numeric(self):
        rows = [{"Row Country": "AAA", "Row Industry": "D01T02", "Column Country": "BBB",
                 "Column Industry": "D10T12", "Value": "n/a", "Year": "2019"}]
        with self.assertRaises(oecd.OECDImportError):
            oecd.parse_tidy_rows(rows, oecd.classify_column(list(rows[0])))

    def test_parse_wide_rows_decodes_dimensions(self):
        header = ["Country", "Industry", "AAA_D01T02", "AAA_HFCE"]
        rows = [["AAA", "D01T02", "10", ""], ["AAA", "VA", "5", "0"]]
        cells = oecd.parse_wide_rows(header, rows, 2019)
        self.assertEqual(len(cells), 3)  # the empty cell is skipped
        first = cells[0]
        self.assertEqual((first["row_country"], first["row_industry"], first["col_country"],
                          first["col_industry"], first["value"]),
                         ("AAA", "D01T02", "AAA", "D01T02", 10.0))

    def test_read_archive_reads_zip_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ICIO2019.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ICIO2019.csv", tidy_csv([
                    ["AAA", "D01T02", "AAA", "D01T02", 1.0, 2019]]))
            cells = oecd.read_archive(path)
            self.assertEqual(len(cells), 1)
            self.assertEqual(cells[0]["year"], 2019)


class BuildDocumentTest(unittest.TestCase):
    def test_builds_v11_document_with_inventory_separated(self):
        cells, z, y, inv = small_cells()
        document = oecd.build_document(cells, year=2019,
                                       source={"name": "test", "provider": "test"})
        self.assertEqual(document["schema"], oecd.SCHEMA_V11)
        self.assertEqual(document["countries"], ["AAA", "BBB"])
        self.assertEqual(document["industries"], ["D01T02", "D10T12"])
        self.assertEqual(document["units"], "USD_mn")
        self.assertTrue(any(value < 0 for value in document["inventory_changes"]))
        self.assertTrue(all(value >= 0 for value in document["y"]))
        # x is derived from the row identity when the layout has no explicit total
        self.assertAlmostEqual(document["x"][0], 10.0 + 4.0 - 1.0, places=9)
        self.assertAlmostEqual(document["x"][3], 5.0 + 6.0 + 0.5, places=9)

    def test_unknown_column_code_is_refused(self):
        cells, *_ = small_cells()
        cells.append({"year": 2019, "row_country": "AAA", "row_industry": "D01T02",
                      "col_country": "AAA", "col_industry": "MYSTERY", "value": 1.0})
        with self.assertRaises(oecd.OECDImportError):
            oecd.build_document(cells, year=2019, source={"name": "test"})

    def test_missing_year_is_refused(self):
        cells, *_ = small_cells()
        with self.assertRaises(oecd.OECDImportError):
            oecd.build_document(cells, year=2001, source={"name": "test"})


def full_cells(countries=10, industries=31, year=2019):
    """A generated ICIO-like table that satisfies the identities exactly (10 x 31)."""
    country_codes = [f"C{index:02d}" for index in range(countries)]
    industry_codes = list(oecd.ICIO_INDUSTRIES[:industries])
    size = countries * industries
    Z = [[float((row * 7 + column * 3) % 11 + (1 if row == column else 0))
          for column in range(size)] for row in range(size)]
    y = [float(5 + (index % 3)) for index in range(size)]
    inventory = [float(-2 + (index % 4)) for index in range(size)]
    x = [sum(Z[row]) + y[row] + inventory[row] for row in range(size)]
    va = [x[column] - sum(Z[row][column] for row in range(size)) for column in range(size)]
    cells = []
    pairs = list((country, industry) for country in country_codes for industry in industry_codes)

    for row, (country, industry) in enumerate(pairs):
        for column, (col_country, col_industry) in enumerate(pairs):
            cells.append({"year": year, "row_country": country, "row_industry": industry,
                          "col_country": col_country, "col_industry": col_industry,
                          "value": Z[row][column]})
        cells.append({"year": year, "row_country": country, "row_industry": industry,
                      "col_country": country, "col_industry": "HFCE", "value": y[row]})
        cells.append({"year": year, "row_country": country, "row_industry": industry,
                      "col_country": country, "col_industry": "INV", "value": inventory[row]})
    for position, (country, industry) in enumerate(pairs):
        cells.append({"year": year, "row_country": country, "row_industry": "VA",
                      "col_country": country, "col_industry": industry, "value": va[position]})
    return cells


class ValidateD1Test(unittest.TestCase):
    def test_d1_passes_on_a_balanced_table(self):
        document = oecd.build_document(full_cells(), year=2019,
                                       source={"name": "test", "provider": "test"})
        validation = oecd.validate_v11(document)
        self.assertTrue(validation["checks"]["d1_pass"], validation["checks"])
        self.assertGreaterEqual(validation["checks"]["dimensions"]["countries"], 10)
        self.assertGreaterEqual(validation["checks"]["dimensions"]["industries"], 30)

    def test_d1_fails_on_a_broken_row_balance(self):
        document = oecd.build_document(full_cells(), year=2019,
                                       source={"name": "test", "provider": "test"})
        document["x"][0] += 1000.0
        validation = oecd.validate_v11(document)
        self.assertFalse(validation["checks"]["d1_pass"])
        self.assertFalse(validation["checks"]["row_balance"])

    def test_d1_rejects_too_few_industries(self):
        cells = full_cells(countries=10, industries=5)
        document = oecd.build_document(cells, year=2019, source={"name": "test"})
        with self.assertRaises(oecd.OECDImportError):
            oecd.validate_v11(document)


class CoverageTest(unittest.TestCase):
    def test_coverage_uses_row_residual_and_world_bank(self):
        va = {"AAA": 800.0, "BBB": 100.0, "ROW": 100.0}
        report = oecd.gdp_coverage({"checks": {"d1_pass": True}}, ["AAA", "BBB"], va,
                                   world_bank={"AAA": (2023, 900.0), "BBB": (2023, 110.0)})
        self.assertAlmostEqual(report["covered_share_of_world_va"], 0.9)
        self.assertTrue(report["coverage_pass"])
        self.assertEqual(report["world_bank_cross_check"]["countries_matched"], 2)

    def test_coverage_fails_below_the_d1_threshold(self):
        va = {"AAA": 500.0, "ROW": 500.0}
        report = oecd.gdp_coverage({"checks": {"d1_pass": True}}, ["AAA"], va, world_bank={})
        self.assertFalse(report["coverage_pass"])
        self.assertAlmostEqual(report["covered_share_of_world_va"], 0.5)


class InstructionTest(unittest.TestCase):
    def test_instructions_name_blocks_directory_and_parser(self):
        document = oecd.instruction_document()
        text = json.dumps(document, ensure_ascii=False)
        self.assertIn("1995-2000", text)
        self.assertIn("2016-2022", text)
        self.assertIn("data/mrio/oecd_icio/archive/", text)
        self.assertTrue(document["parser_ready"])
        self.assertIn("403", text)

    def test_manifest_has_no_values(self):
        manifest = oecd.aggregate_manifest(
            [{"year": 2019, "status": "AVAILABLE", "path": "x", "bytes": 1, "sha256": "ab",
              "countries": 10, "industries": 31,
              "validation": {"checks": {"d1_pass": True,
                                        "dimensions": {"countries": 10, "industries": 31}},
                             "max_abs_row_residual": 0.0, "max_abs_column_residual": 0.0,
                             "total_output": 1.0, "tolerance": 1e-3}}],
            [], None)
        text = json.dumps(manifest)
        for forbidden in ('"Z"', '"y"', '"va"', '"inventory_changes"'):
            self.assertNotIn(forbidden, text)
        self.assertIn("d1_pass", text)


class ProbeTest(unittest.TestCase):
    def test_probe_records_failure_offline(self):
        result = oecd.probe_url("http://127.0.0.1:9/never", timeout=0.5)
        self.assertFalse(result["reachable"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["checked_at"][-1], "Z")

    def test_looks_like_archive_sniffs_html_error_pages(self):
        self.assertTrue(oecd._looks_like_archive(b"PK\x03\x04rest", "bytes 0-127/2500000"))
        self.assertFalse(oecd._looks_like_archive(b"\n<!DOCTYPE html>", "bytes 0-127/175706"))
        self.assertFalse(oecd._looks_like_archive(b"PK\x03\x04", None))

    def test_probe_all_is_network_bound(self):
        # structure only: the target list is explicit and never invents a URL
        names = [name for name, _ in oecd.PROBE_URLS]
        self.assertIn("dataset_page", names)
        self.assertIn("sdmx_dataflow_oecd_sti_pie", names)
        self.assertEqual(len(names), len(set(names)))


class StatusTest(unittest.TestCase):
    def test_check_local_on_empty_root_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = oecd.check_local(Path(tmp))
            self.assertEqual(status["status"], "BLOCKED_DATA")

    def test_run_d1_without_data_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = oecd.run_d1(Path(tmp))
            self.assertEqual(report["status"], "BLOCKED_DATA")
            self.assertIsNone(report["coverage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
