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
