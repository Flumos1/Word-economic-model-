"""Tests of tools/fetch_scorecard_data.py (NY Fed Staff Nowcast fetch/conversion).

The fetch tool is deliberately thin: the network step is isolated from the pure workbook parser and
the raw values are written only to a git-ignored directory.  These tests are offline and use a
synthetic workbook with the documented layout (Forecast Date + quarter columns), plus a temporary
manifest/CSV pair for the ``--check`` verifier.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

try:  # the project already depends on openpyxl for its Office artefacts
    import openpyxl
except ImportError:  # pragma: no cover - minimal environments
    openpyxl = None

import fetch_scorecard_data as fetch  # noqa: E402

BASE_DATE = datetime(2015, 12, 31)


def make_workbook() -> bytes:
    """A minimal workbook with the real header/data layout (values are synthetic, not NY Fed)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = fetch.SHEET
    sheet.cell(row=3, column=1, value="Forecast Date")
    sheet.cell(row=3, column=2, value="2015Q4")
    sheet.cell(row=3, column=3, value="2016Q1")
    sheet.cell(row=3, column=4, value="2016Q2")
    sheet.cell(row=3, column=5, value="Notes")
    rows = [
        (datetime(2015, 12, 31), 1.0, 2.0, None, "cross-quarter row"),
        (datetime(2016, 1, 29), None, 2.5, None, "as published"),
        (datetime(2016, 2, 29), None, "n/a", None, "bad value is skipped"),
        (datetime(2016, 3, 31), None, None, 3.0, "next quarter"),
    ]
    for offset, row in enumerate(rows, start=4):
        for column, value in enumerate(row, start=1):
            if value is not None:
                sheet.cell(row=offset, column=column, value=value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
class LivingstonFetchTests(unittest.TestCase):
    """Livingston fetch/derive/check with synthetic RGDPX and release-date workbooks."""

    @staticmethod
    def make_workbooks() -> tuple[bytes, bytes]:
        medians = openpyxl.Workbook()
        sheet = medians.active
        sheet.title = fetch.LIVINGSTON_SHEET_MEDIANS
        sheet.append(["Date", "RGDPX_BP", "RGDPX_ZM"])
        sheet.append([datetime(2003, 12, 1), 100.0, 101.0])       # strict subset excludes this
        sheet.append([datetime(2004, 6, 1), 101.0, 102.0])        # exact release
        sheet.append([datetime(2004, 12, 1), 103.0, 104.0])       # Not After release
        sheet.append([datetime(2005, 6, 1), 105.0, "#N/A"])       # missing level
        median_buffer = io.BytesIO()
        medians.save(median_buffer)

        releases = openpyxl.Workbook()
        dates = releases.active
        dates.title = fetch.LIVINGSTON_SHEET_DATES
        dates.cell(row=1, column=1, value="Survey")
        dates.cell(row=1, column=2, value="Livingston Survey Release Date")
        dates.cell(row=3, column=2, value="Exact")
        dates.cell(row=3, column=3, value="Not After")
        rows = [
            (datetime(2003, 12, 1), None, datetime(2003, 12, 20)),
            (datetime(2004, 6, 1), datetime(2004, 6, 18), None),
            (datetime(2004, 12, 1), None, datetime(2004, 12, 21)),
            (datetime(2005, 6, 1), datetime(2005, 6, 10), None),
        ]
        for offset, row in enumerate(rows, start=5):
            for column, value in enumerate(row, start=1):
                if value is not None:
                    dates.cell(row=offset, column=column, value=value)
        release_buffer = io.BytesIO()
        releases.save(release_buffer)
        return median_buffer.getvalue(), release_buffer.getvalue()

    def test_parse_derives_only_strict_post_2004_rows(self):
        medians, releases = self.make_workbooks()
        parsed = fetch.parse_livingston(medians, releases)
        self.assertEqual(parsed["medians_rows"], 4)
        self.assertEqual(parsed["release_rows"], 4)
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["release_basis"], {"exact": 1, "not_after": 1})
        self.assertEqual(parsed["first_survey"], "2004-06-01")
        self.assertEqual(parsed["last_survey"], "2004-12-01")
        self.assertEqual(parsed["first_origin"], "2004-06-30")
        self.assertEqual(parsed["rows"][0]["target_period"], "2004Q2")
        self.assertEqual(parsed["rows"][1]["target_period"], "2004Q4")
        self.assertEqual(parsed["skipped"],
                         {"missing_or_nonpositive_level": 1, "pre_2004_not_strict": 1})
        expected = 400.0 * __import__("math").log(102.0 / 101.0)
        self.assertAlmostEqual(parsed["rows"][0]["forecast"], expected)

    def test_manifest_and_check_do_not_contain_row_values(self):
        medians, releases = self.make_workbooks()
        parsed = fetch.parse_livingston(medians, releases)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "livingston_t2_h0.csv"
            info = fetch.write_livingston_csv(parsed["rows"], out)
            data = out.read_bytes()
            self.assertTrue(data.endswith(b"\n"))
            self.assertNotIn(b"\r\n", data)
            self.assertEqual(data.splitlines()[0].decode(),
                             ",".join(fetch.LIVINGSTON_COLUMNS))
            manifest = fetch.build_livingston_manifest(
                medians=medians, release_dates=releases, parsed=parsed, csv_info=info,
                retrieved_at="2026-11-26T00:00:00Z")
            text = json.dumps(manifest, ensure_ascii=False)
            self.assertEqual(manifest["schema"], fetch.LIVINGSTON_SCHEMA)
            self.assertEqual(manifest["parse"]["rows_total"], 2)
            self.assertEqual(manifest["parse"]["release_basis"], {"exact": 1, "not_after": 1})
            self.assertEqual(manifest["output"]["sha256"], info["sha256"])
            self.assertFalse(manifest["raw_files_committed"])
            self.assertNotIn("102.0", text)  # no per-row forecasts in the manifest
            meta = Path(directory) / "livingston.meta.json"
            meta.write_text(text, encoding="utf-8")
            good = fetch.check_livingston(out=out, meta=meta)
            self.assertTrue(good["ok"], good)
            out.write_text(out.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            bad = fetch.check_livingston(out=out, meta=meta)
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["reason"], "RAW_CSV_SHA256_MISMATCH")

    def test_fetch_livingston_writes_csv_and_manifest_with_mocked_network(self):
        medians, releases = self.make_workbooks()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "livingston_t2_h0.csv"
            meta = Path(directory) / "livingston.meta.json"
            with mock.patch.object(fetch, "fetch_workbook", side_effect=[medians, releases]):
                summary = fetch.fetch_livingston(out=out, meta=meta, log=None)
            self.assertEqual(summary["status"], "FETCHED")
            self.assertEqual(summary["rows_total"], 2)
            self.assertTrue(out.is_file())
            self.assertTrue(meta.is_file())
            self.assertTrue(fetch.check_livingston(out=out, meta=meta)["ok"])

    def test_cli_check_for_livingston_returns_zero_only_when_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "livingston_t2_h0.csv"
            meta = Path(directory) / "livingston.meta.json"
            self.assertEqual(fetch.main(["--livingston", "--check", "--livingston-out", str(out),
                                         "--livingston-meta", str(meta)]), 3)
            medians, releases = self.make_workbooks()
            with mock.patch.object(fetch, "fetch_workbook", side_effect=[medians, releases]):
                fetch.main(["--livingston", "--livingston-out", str(out),
                            "--livingston-meta", str(meta)])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = fetch.main(["--livingston", "--check", "--livingston-out", str(out),
                                   "--livingston-meta", str(meta)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(buffer.getvalue())["ok"])


@unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
class WorkbookParserTests(unittest.TestCase):
    def test_parse_workbook_uses_target_quarter_provenance_and_skips_bad_values(self):
        parsed = fetch.parse_workbook(make_workbook())
        rows = parsed["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(parsed["skipped"], 1)
        self.assertEqual(parsed["rows_published"], 3)
        self.assertEqual(parsed["rows_reconstructed"], 1)
        self.assertEqual([row["provenance"] for row in rows],
                         ["RECONSTRUCTED", "PUBLISHED", "PUBLISHED", "PUBLISHED"])
        # the 2015-12-31 row has two cells: 2015Q4 reconstructed, 2016Q1 published
        self.assertEqual(rows[0]["date"], "2015-12-31")
        self.assertEqual(rows[0]["quarter"], "2015Q4")
        self.assertEqual(rows[1]["quarter"], "2016Q1")
        self.assertEqual(rows[-1]["quarter"], "2016Q2")
        self.assertEqual(parsed["first_date"], "2015-12-31")
        self.assertEqual(parsed["last_date"], "2016-03-31")
        self.assertEqual(parsed["published_quarters"], ["2016Q1", "2016Q2"])

    def test_parse_workbook_rejects_the_wrong_sheet(self):
        workbook = openpyxl.Workbook()
        workbook.active.title = "Something Else"
        buffer = io.BytesIO()
        workbook.save(buffer)
        with self.assertRaises(fetch.FetchError):
            fetch.parse_workbook(buffer.getvalue())

    def test_parse_workbook_rejects_a_workbook_without_the_header(self):
        workbook = openpyxl.Workbook()
        workbook.active.title = fetch.SHEET
        workbook.active.cell(row=1, column=1, value="No header here")
        buffer = io.BytesIO()
        workbook.save(buffer)
        with self.assertRaises(fetch.FetchError):
            fetch.parse_workbook(buffer.getvalue())

    def test_parse_workbook_rejects_garbage_bytes(self):
        with self.assertRaises(fetch.FetchError):
            fetch.parse_workbook(b"not an xlsx")


class ManifestAndCsvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.parsed = fetch.parse_workbook(make_workbook()) if openpyxl is not None else None

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
    def test_write_csv_uses_lf_and_the_strict_adapter_columns(self):
        path = self.tmp / "nowcast.csv"
        info = fetch.write_csv(self.parsed["rows"], path)
        data = path.read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.splitlines()[0], b"date,quarter,nowcast,provenance")
        self.assertEqual(info["rows"], 4)
        self.assertEqual(info["bytes"], len(data))
        self.assertEqual(info["sha256"], fetch.sha256_bytes(data))
        self.assertEqual(fetch.sha256_file(path), info["sha256"])

    @unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
    def test_manifest_records_url_date_hash_counts_and_no_raw_values(self):
        path = self.tmp / "nowcast.csv"
        info = fetch.write_csv(self.parsed["rows"], path)
        raw = make_workbook()
        manifest = fetch.build_manifest(raw=raw, parsed=self.parsed, csv_info=info,
                                        url=fetch.URL, retrieved_at="2026-11-26T00:00:00Z")
        self.assertEqual(manifest["schema"], fetch.SCHEMA)
        self.assertEqual(manifest["source"]["url"], fetch.URL)
        self.assertEqual(manifest["source"]["sha256"], fetch.sha256_bytes(raw))
        self.assertEqual(manifest["source"]["bytes"], len(raw))
        self.assertEqual(manifest["retrieved_at"], "2026-11-26T00:00:00Z")
        self.assertEqual(manifest["parse"]["rows_total"], 4)
        self.assertEqual(manifest["parse"]["rows_published"], 3)
        self.assertEqual(manifest["parse"]["rows_reconstructed"], 1)
        self.assertEqual(manifest["parse"]["published_start_quarter"], fetch.PUBLISHED_START_QUARTER)
        self.assertEqual(manifest["output"]["sha256"], info["sha256"])
        self.assertFalse(manifest["raw_files_committed"])
        self.assertNotIn("timestamp", manifest)  # the fetch time is the only clock value
        text = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn("2.5,", text)  # no per-row values in the manifest

    @unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
    def test_check_output_verifies_the_sha_and_detects_tampering(self):
        path = self.tmp / "nowcast.csv"
        meta = self.tmp / "nowcast.meta.json"
        info = fetch.write_csv(self.parsed["rows"], path)
        manifest = fetch.build_manifest(raw=make_workbook(), parsed=self.parsed, csv_info=info,
                                        url=fetch.URL, retrieved_at="fixed")
        meta.write_text(json.dumps(manifest), encoding="utf-8")
        good = fetch.check_output(out=path, meta=meta)
        self.assertTrue(good["ok"])
        self.assertEqual(good["sha256"], info["sha256"])
        path.write_text(path.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
        bad = fetch.check_output(out=path, meta=meta)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "RAW_CSV_SHA256_MISMATCH")
        missing = fetch.check_output(out=self.tmp / "missing.csv", meta=meta)
        self.assertEqual(missing["reason"], "RAW_CSV_NOT_FOUND")

    @unittest.skipUnless(openpyxl is not None, "openpyxl is not installed")
    def test_cli_check_returns_zero_only_for_a_matching_manifest(self):
        path = self.tmp / "nowcast.csv"
        meta = self.tmp / "nowcast.meta.json"
        info = fetch.write_csv(self.parsed["rows"], path)
        manifest = fetch.build_manifest(raw=make_workbook(), parsed=self.parsed, csv_info=info,
                                        url=fetch.URL, retrieved_at="fixed")
        meta.write_text(json.dumps(manifest), encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = fetch.main(["--check", "--out", str(path), "--meta", str(meta)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["ok"])
        path.write_text("broken\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as buffer:
            code = fetch.main(["--check", "--out", str(path), "--meta", str(meta)])
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


class FetchGuardTests(unittest.TestCase):
    def test_non_xlsx_response_is_rejected(self):
        fake = mock.Mock()
        fake.status = 200
        fake.read.return_value = b"<html>not a workbook</html>"
        fake.__enter__ = mock.Mock(return_value=fake)
        fake.__exit__ = mock.Mock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=fake):
            with self.assertRaises(fetch.FetchError):
                fetch.fetch_workbook("https://example.invalid/nowcast.xlsx", retries=1)

    def test_sha256_helpers_agree(self):
        data = b"scorecard"
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "x.bin"
            path.write_bytes(data)
            self.assertEqual(fetch.sha256_bytes(data), fetch.sha256_file(path))
        self.assertEqual(len(fetch.sha256_bytes(data)), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
