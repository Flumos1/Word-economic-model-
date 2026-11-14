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

    def test_detect_icio_2016_matrix_layout(self):
        header = ["", "AUS_C01T05AGR", "AUT_C10T14MIN", "HFCE_AUS", "INV_AUS", "DISC"]
        self.assertEqual(oecd.detect_layout(header), "icio_matrix")

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


def icio2016_csv() -> str:
    """A two-economy x two-industry slice of the real ICIO 2016 matrix layout (balanced)."""
    header = ["", "AAA_C1", "AAA_C2", "BBB_C1", "BBB_C2",
              "HFCE_AAA", "INV_AAA", "DIRP_AAA", "HFCE_BBB", "INV_BBB", "DIRP_BBB", "DISC"]
    # rows: Z row, HFCE, INV, DIRP, DISC; OUT and VA+TAXSUB are computed to balance exactly
    data = {
        "AAA_C1": ([10, 1, 2, 0], 4.0, -1.0, 0.0, 0.5),
        "AAA_C2": ([0, 5, 0, 1], 2.0, 0.0, 0.0, 0.0),
        "BBB_C1": ([1, 0, 6, 2], 3.0, 0.5, 0.0, 0.0),
        "BBB_C2": ([0, 2, 0, 4], 1.0, 0.0, 0.0, 0.0),
    }
    Z = [data[label][0] for label in data]
    y = [data[label][1] + data[label][3] + data[label][4] for label in data]
    inv = [data[label][2] for label in data]
    x = [sum(Z[row]) + y[row] + inv[row] for row in range(4)]
    va = [x[column] - sum(Z[row][column] for row in range(4)) for column in range(4)]
    rows = []
    for position, (label, (inter, hfce, inv_value, dirp, disc)) in enumerate(data.items()):
        rows.append([label] + [str(value) for value in inter]
                    + [str(hfce), str(inv_value), str(dirp), "0", "0", "0", str(disc)])
    rows.append(["VA+TAXSUB"] + [str(value) for value in va] + [""] * 7)
    rows.append(["OUT"] + [str(value) for value in x] + [""] * 7)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


class Icio2016MatrixTest(unittest.TestCase):
    def test_parse_icio_matrix_maps_intermediate_final_demand_and_bottom_rows(self):
        buffer = io.StringIO(icio2016_csv())
        reader = csv.reader(buffer)
        header = next(reader)
        lines = [line for line in reader]
        cells = oecd.parse_icio_matrix(header, lines, 2011)
        industries = {cell["row_industry"] for cell in cells}
        self.assertIn("C1", industries)
        self.assertIn("VA+TAXSUB", industries)
        self.assertIn("OUT", industries)
        final = [cell for cell in cells if cell["col_industry"] == "HFCE"]
        self.assertEqual({cell["col_country"] for cell in final}, {"AAA", "BBB"})

    def test_icio_matrix_builds_a_consistent_v11_document(self):
        reader = csv.reader(io.StringIO(icio2016_csv()))
        header = next(reader)
        lines = [line for line in reader]
        cells = oecd.parse_icio_matrix(header, lines, 2011)
        document = oecd.build_document(cells, year=2011, source={"name": "test"})
        self.assertEqual(document["countries"], ["AAA", "BBB"])
        self.assertEqual(document["industries"], ["C1", "C2"])
        # x comes from the OUT row, y from HFCE/NPS/... and inventory from INV
        self.assertEqual(document["x"][:2], [16.5, 8.0])
        self.assertEqual(document["inventory_changes"][0], -1.0)
        residual = [sum(document["Z"][row]) + document["y"][row]
                    + document["inventory_changes"][row] + document["discrepancy"][row]
                    - document["x"][row] for row in range(4)]
        self.assertTrue(all(abs(value) < 1e-9 for value in residual), residual)


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


class Webfs2025Test(unittest.TestCase):
    page = """<a href="https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/ReadMe_ICIO_small.xlsx">r</a>
    <a href="https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/1995-2000_SML.zip">a</a>
    <a href="https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2016-2022_SML.zip">b</a>
    <a href="https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2023/2016-2020_SML.zip">c</a>"""

    def test_discover_webfs_links_by_release(self):
        links = oecd.discover_webfs_links(self.page)
        self.assertIn("2025", links)
        self.assertIn("2023", links)
        self.assertEqual(links["2025"]["readme"], ["ReadMe_ICIO_small.xlsx"])
        self.assertIn("2016-2022_SML.zip", links["2025"]["files"])
        only_2023 = oecd.discover_webfs_links(self.page, release="2023")
        self.assertEqual(sorted(only_2023), ["2023"])

    def test_expected_2025_archives_have_five_blocks_and_readme(self):
        expected = oecd.EXPECTED_2025_ARCHIVES
        self.assertIn("ReadMe_ICIO_small.xlsx", expected)
        blocks = [name for name in expected if "_SML.zip" in name]
        self.assertEqual(len(blocks), 5)
        self.assertEqual(sorted(oecd.ICIO2025_BLOCKS), sorted(
            name.split("_")[0] for name in blocks))

    def test_check_archive_passes_on_valid_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "archive"
            directory.mkdir()
            (directory / "ReadMe_ICIO_small.xlsx").write_bytes(b"PK\x03\x04readme")
            for block in oecd.ICIO2025_BLOCKS:
                path = directory / f"{block}_SML.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(f"{block}.csv", "V1,AAA_A01\n1,2\n")
            expected = {name: {"suffix": spec["suffix"], "min_bytes": 1}
                        for name, spec in oecd.EXPECTED_2025_ARCHIVES.items()}
            report = oecd.check_archive(Path(tmp), expected=expected)
            self.assertEqual(report["status"], "PASS", report["problems"])

    def test_check_archive_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "archive"
            directory.mkdir()
            (directory / "1995-2000_SML.zip").write_bytes(b"<html>error page</html>")
            expected = {"1995-2000_SML.zip": {"suffix": ".zip", "min_bytes": 100},
                        "2016-2022_SML.zip": {"suffix": ".zip", "min_bytes": 100}}
            report = oecd.check_archive(Path(tmp), expected=expected)
            self.assertEqual(report["status"], "BLOCKED_DATA")
            statuses = {item["name"]: item["status"] for item in report["files"]}
            self.assertEqual(statuses["1995-2000_SML.zip"], "TOO_SMALL")
            self.assertEqual(statuses["2016-2022_SML.zip"], "MISSING")

    def test_2025_style_matrix_rows_and_fd_orientation(self):
        # <ISO3>_<INDUSTRY> intermediate columns, <ISO3>_<FD> final-demand columns, OUT column
        header = ["V1", "AAA_A01", "AAA_B05", "BBB_A01", "BBB_B05",
                  "AAA_HFCE", "AAA_INVNT", "AAA_DPABR", "BBB_HFCE", "BBB_INVNT",
                  "BBB_DPABR", "OUT"]
        rows = [
            ["AAA_A01", "10", "1", "2", "0", "3", "0", "0", "2", "0", "0", "18"],
            ["AAA_B05", "0", "5", "0", "1", "1", "0", "0", "0", "0", "0", "7"],
            ["BBB_A01", "1", "0", "6", "2", "0", "0", "0", "3", "-1", "0", "11"],
            ["BBB_B05", "0", "2", "0", "4", "0", "0", "0", "1", "0", "1", "8"],
            ["TLS", "1", "0", "1", "0", "", "", "", "", "", "", ""],
            ["VA", "6", "1", "3", "2", "", "", "", "", "", "", ""],
            ["OUT", "18", "7", "11", "8", "", "", "", "", "", "", ""],
        ]
        import io as _io
        buffer = _io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2019_SML.csv"
            path.write_text(buffer.getvalue(), encoding="utf-8")
            document = oecd.build_document_icio_matrix(path, 2019, {"name": "test"})
            self.assertEqual(document["countries"], ["AAA", "BBB"])
            self.assertEqual(document["industries"], ["A01", "B05"])
            self.assertEqual(document["x"][:2], [18.0, 7.0])
            self.assertEqual(document["inventory_changes"][2], -1.0)
            self.assertEqual(document["discrepancy"], [0.0, 0.0, 0.0, 0.0])


class WaybackTest(unittest.TestCase):
    def test_playback_url_is_built_from_a_snapshot(self):
        snapshot = ("http://web.archive.org/web/20210310170654/"
                    "http://www.oecd.org/sti/ind/ICIO2016_2011.zip")
        playback = oecd._wayback_playback_url(snapshot)
        self.assertEqual(playback, "https://web.archive.org/web/20210310170654id_/"
                                   "http://www.oecd.org/sti/ind/ICIO2016_2011.zip")
        self.assertIsNone(oecd._wayback_playback_url("https://example.org/file.zip"))

    def test_data_year_is_not_the_edition_year(self):
        self.assertEqual(oecd._years_from_name("ICIO2016_2011.zip"), [2011])
        self.assertEqual(oecd._years_from_name("ICIO2016_1995.zip"), [1995])
        self.assertEqual(oecd._years_from_name("ICIO2019.csv"), [2019])
        self.assertEqual(oecd._years_from_name("table.csv"), [])


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
