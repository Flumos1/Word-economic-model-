"""Dense ALFRED vintage archive: plan, planned->actual mapping, batch POSTs, merge and the check.

The tests are offline: the network layer of tools/fetch_data.py is either not reached (dry-run) or
replaced by a synthetic ALFRED download-data form (a small page listing actual vintage dates plus one
zipped CSV per POST chunk). This is what the CI workflow exercises on the real service.
"""
import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import fetch_data  # noqa: E402

PLAN = json.loads((ROOT / "protocols" / "data_plan.json").read_text(encoding="utf-8"))
VINTAGE_SERIES = [s["id"] for s in PLAN["series"] if s.get("vintages")]
EXPECTED_GRID = []  # quarter-end 1990-03-31 .. 2026-09-30


def _build_grid() -> list:
    out = []
    year, month = 1990, 3
    while True:
        if month == 12:
            day = date(year, 12, 31)
        else:
            day = date(year, month + 1, 1) - timedelta(days=1)
        iso = day.isoformat()
        if iso > "2026-09-30":
            break
        if iso >= "1990-03-31":
            out.append(iso)
        month += 3
        if month == 15:
            month, year = 3, year + 1
    return out


EXPECTED_GRID = _build_grid()


# --------------------------------------------------------------------------------------- helpers
def make_vintage_zip(series: str, columns: list, rows: list, name: str = "vintages.csv") -> bytes:
    """Synthetic ALFRED zipped CSV: columns = ISO dates, rows = (period, {iso: value})."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["observation_date"] + [f"{series}_{c.replace('-', '')}" for c in columns])
    for period, cells in rows:
        writer.writerow([period] + [cells.get(c, "") for c in columns])
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, buffer.getvalue().encode("utf-8-sig"))
        zf.writestr("README.txt", "test archive")
    return payload.getvalue()


def form_page(available: list) -> bytes:
    options = "".join(f'<option value="{d}">{d}</option>' for d in available)
    return (f'<html><select id="form_selected_vintage_dates">{options}</select></html>'
            ).encode("utf-8")


class DensePlan(unittest.TestCase):
    def test_plan_is_a_dense_quarterly_grid(self):
        self.assertGreaterEqual(len(VINTAGE_SERIES), 6, VINTAGE_SERIES)
        for series in PLAN["series"]:
            planned = series.get("vintages", [])
            if series["id"] == "USREC":
                self.assertEqual(planned, [], "USREC stays without vintage retrieval")
                continue
            self.assertEqual(planned, EXPECTED_GRID, series["id"])
            self.assertEqual(len(planned), 147, series["id"])
            self.assertEqual(planned[0], "1990-03-31", series["id"])
            self.assertEqual(planned[-1], "2026-09-30", series["id"])
            for tag in planned:
                day = date.fromisoformat(tag)
                self.assertIn(day.month, (3, 6, 9, 12), tag)
                self.assertLessEqual(tag, "2026-09-30")

    def test_plan_carries_the_retrieval_rules(self):
        self.assertIn("latest ACTUAL ALFRED vintage date at or before", PLAN["purpose"])
        self.assertIn("batches of at most 40", PLAN["purpose"])
        self.assertIn("vintage_check", PLAN["purpose"])


class PlannedDateMapping(unittest.TestCase):
    def test_maps_to_latest_actual_on_or_before_the_planned_date(self):
        available = ["1989-12-15", "1990-01-10", "1990-03-14", "1990-06-13", "1990-12-12"]
        planned = ["1990-03-31", "1990-06-30", "1990-12-31"]
        self.assertEqual(fetch_data.map_planned_dates(planned, available),
                         ["1990-03-14", "1990-06-13", "1990-12-12"])

    def test_never_maps_to_a_future_vintage_date(self):
        available = ["1996-12-03", "1997-01-07", "1997-12-02", "2026-08-31"]
        planned = EXPECTED_GRID
        mapped = fetch_data.map_planned_dates(planned, available)
        for vintage in mapped:
            self.assertTrue(any(p >= vintage for p in planned), vintage)
        self.assertLessEqual(mapped[-1], planned[-1])

    def test_planned_dates_before_the_first_vintage_stay_unmapped(self):
        available = ["1996-12-03", "1997-01-07"]
        planned = EXPECTED_GRID
        mapped = fetch_data.map_planned_dates(planned, available)
        unmapped = fetch_data.unmapped_planned_dates(planned, available)
        self.assertEqual(mapped, ["1996-12-03", "1997-01-07"])
        self.assertIn("1990-03-31", unmapped)
        self.assertIn("1996-09-30", unmapped)
        self.assertEqual(len(unmapped), 27, "1990-03-31 .. 1996-09-30 are before the first vintage")
        self.assertTrue(all(d < "1996-12-03" for d in unmapped))
        for planned_date in planned:
            if planned_date in unmapped:
                self.assertLess(planned_date, available[0])
            else:
                actual = fetch_data.latest_actual_on_or_before(planned_date, available)
                self.assertIsNotNone(actual)
                self.assertLessEqual(actual, planned_date)

    def test_duplicates_inside_one_revision_gap_are_collapsed(self):
        available = ["1990-03-14", "1990-12-12"]
        planned = ["1990-03-31", "1990-06-30", "1990-09-30"]
        self.assertEqual(fetch_data.map_planned_dates(planned, available), ["1990-03-14"])


class Batching(unittest.TestCase):
    def test_chunks_never_exceed_the_form_limit_and_cover_every_date(self):
        dates = EXPECTED_GRID
        chunks = fetch_data.chunk_dates(dates)
        self.assertEqual([len(c) for c in chunks], [40, 40, 40, 27])
        self.assertTrue(all(len(c) <= fetch_data.VINTAGE_CHUNK_DATES for c in chunks))
        self.assertEqual([d for chunk in chunks for d in chunk], dates)
        self.assertEqual(sum(len(c) for c in chunks), 147)


class Merge(unittest.TestCase):
    def test_two_chunks_merge_into_one_sorted_archive_with_all_columns(self):
        first = fetch_data.read_vintage_zip(make_vintage_zip(
            "CPIAUCSL", ["1990-03-14"], [("1990-01-01", {"1990-03-14": "1.0"}),
                                         ("1990-02-01", {"1990-03-14": "2.0"})]))
        second = fetch_data.read_vintage_zip(make_vintage_zip(
            "CPIAUCSL", ["1990-06-13", "1990-06-29"],
            [("1990-02-01", {"1990-06-13": "2.5"}),
             ("1990-03-01", {"1990-06-13": "3.0", "1990-06-29": "3.5"})]))
        merged_raw, merged = fetch_data.merge_vintage_parts([first, second], "CPIAUCSL")
        self.assertEqual(merged["vintages"], ["1990-03-14", "1990-06-13", "1990-06-29"])
        self.assertEqual(merged["vintage_count"], 3)
        self.assertEqual(merged["rows"], 3)
        parsed = fetch_data.read_vintage_zip(merged_raw)
        self.assertEqual(sorted(parsed["values"]),
                         ["1990-03-14", "1990-06-13", "1990-06-29"])
        self.assertEqual(parsed["periods"], ["1990-01-01", "1990-02-01", "1990-03-01"])
        self.assertEqual(len(set(parsed["periods"])), len(parsed["periods"]),
                         "observation_date must be unique")
        self.assertEqual(parsed["values"]["1990-03-14"], {"1990-01-01": "1.0", "1990-02-01": "2.0"})
        self.assertEqual(parsed["values"]["1990-06-13"], {"1990-02-01": "2.5", "1990-03-01": "3.0"})
        self.assertEqual(parsed["values"]["1990-06-29"], {"1990-03-01": "3.5"})
        header = parsed["columns"]
        self.assertEqual(header, sorted(header), "columns must be sorted by vintage date")
        with zipfile.ZipFile(io.BytesIO(merged_raw)) as zf:
            names = zf.namelist()
            self.assertTrue(any(n.lower().endswith(".csv") for n in names), names)
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            text = zf.read(csv_names[0]).decode("utf-8-sig")
        self.assertTrue(text.startswith("observation_date,CPIAUCSL_19900314,CPIAUCSL_19900613"))


class VintageCheck(unittest.TestCase):
    def test_check_flags_an_observation_later_than_the_vintage(self):
        bad = make_vintage_zip("UNRATE", ["1990-03-14"],
                               [("1990-06-01", {"1990-03-14": "5.4"})])
        self.assertFalse(fetch_data.vintage_meta(bad)["vintage_check"])
        good = make_vintage_zip("UNRATE", ["1990-03-14"],
                                [("1990-02-01", {"1990-03-14": "5.4"}),
                                 ("1990-03-01", {"1990-03-14": ""})])
        meta = fetch_data.vintage_meta(good)
        self.assertTrue(meta["vintage_check"])
        self.assertEqual(meta["vintage_count"], 1)
        self.assertEqual(meta["rows"], 2)
        self.assertEqual(meta["vintage_last_observation"], {"1990-03-14": "1990-02-01"})

    def test_column_name_parsing(self):
        self.assertEqual(fetch_data.vintage_date_of_column("CPIAUCSL_19900314"), "1990-03-14")
        self.assertEqual(fetch_data.vintage_date_of_column("1990-03-14"), "1990-03-14")
        self.assertIsNone(fetch_data.vintage_date_of_column("observation_date"))


class GateView(unittest.TestCase):
    def _dense_archive(self) -> bytes:
        vintages = EXPECTED_GRID
        rows = [("1990-01-01", {v: "1.0" for v in vintages[:5]}),
                ("2020-01-01", {v: "2.0" for v in vintages[-5:]})]
        return fetch_data.build_vintage_zip("CPIAUCSL", vintages, [r[0] for r in rows],
                                            dict(rows), note="test")

    def test_gate_view_is_within_the_form_limit_and_keeps_pre_2000_dates(self):
        archive = self._dense_archive()
        view_raw, meta = fetch_data.subset_vintage_zip(archive, "CPIAUCSL")
        self.assertLessEqual(meta["vintage_count"], fetch_data.GATE_VIEW_DATES)
        self.assertEqual(meta["vintage_count"], fetch_data.GATE_VIEW_DATES)
        pre = [d for d in meta["dates"] if d < "2000-01-01"]
        self.assertGreaterEqual(len(pre), fetch_data.GATE_VIEW_PRE_2000)
        self.assertGreater(len([d for d in meta["dates"] if d >= "2000-01-01"]), 0)
        self.assertTrue(set(meta["dates"]) <= set(EXPECTED_GRID))
        parsed = fetch_data.read_vintage_zip(view_raw)
        self.assertEqual(sorted(parsed["values"]), meta["dates"])
        self.assertTrue(fetch_data.vintage_meta(view_raw)["vintage_check"])


class OfflineDryRun(unittest.TestCase):
    def test_dry_run_covers_the_dense_plan_without_network_or_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "data_log.json"
            with mock.patch.object(fetch_data, "request",
                                   side_effect=AssertionError("dry-run must not use the network")):
                code = fetch_data.main(["--dry-run", "--include-vintages",
                                        "--log", str(log_path)])
            self.assertEqual(code, 0)
            self.assertFalse(log_path.exists(), "dry-run must not write the log")
        vintage_series = [s for s in PLAN["series"] if s.get("vintages")]
        self.assertGreaterEqual(len(vintage_series), 6)
        self.assertEqual(sum(len(s.get("vintages", [])) for s in PLAN["series"]),
                         len(vintage_series) * 147)


class ChunkedFetch(unittest.TestCase):
    """The real retrieval path with a synthetic ALFRED form (GET page + one zip per POST chunk)."""

    AVAILABLE = ["1989-12-15", "1990-03-14", "1990-06-13", "1990-09-19", "1990-12-12",
                 "1991-03-14", "1991-06-13"]
    PLANNED = ["1990-03-31", "1990-06-30", "1990-09-30", "1990-12-31", "1991-03-31",
               "1991-06-30", "1991-09-30", "2026-09-30"]
    OBS = ["1989-12-01", "1990-01-01", "1990-02-01"]

    def test_posts_are_batched_and_the_answers_are_merged(self):
        posted = []

        def fake_request(url, user_agent, data=None, timeout=240, retries=3):
            if data is None:
                return form_page(self.AVAILABLE)
            query = fetch_data.urllib.parse.parse_qs(data.decode())
            dates = query["form[entered_vintage_dates]"][0].split()
            posted.append(dates)
            self.assertLessEqual(len(dates), fetch_data.VINTAGE_CHUNK_DATES)
            self.assertTrue(set(dates) <= set(self.AVAILABLE), dates)
            rows = [(obs, {d: "1.0" for d in dates}) for obs in self.OBS]
            return make_vintage_zip("CPIAUCSL", dates, rows)

        with mock.patch.object(fetch_data, "request", side_effect=fake_request), \
                mock.patch.object(fetch_data.time, "sleep"):
            raw, actual, available, unmapped, chunks, merged = fetch_data.fetch_vintages(
                "CPIAUCSL", self.PLANNED, "1989-12-01", "1990-02-01", "test-agent", rate=0)
        self.assertEqual(actual, ["1990-03-14", "1990-06-13", "1990-09-19", "1990-12-12",
                                  "1991-03-14", "1991-06-13"])
        self.assertIn("1989-12-15", available)
        self.assertEqual(unmapped, [])
        self.assertEqual([len(p) for p in posted],
                         [len(c) for c in fetch_data.chunk_dates(actual)])
        meta = fetch_data.vintage_meta(raw)
        self.assertEqual(meta["vintage_count"], 6)
        self.assertEqual(meta["rows"], 3)
        self.assertTrue(meta["vintage_check"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["requested_dates"], 6)

    def test_a_rejected_chunk_is_split_down_to_single_dates(self):
        posted = []

        def fake_request(url, user_agent, data=None, timeout=240, retries=3):
            if data is None:
                return form_page(self.AVAILABLE)
            query = fetch_data.urllib.parse.parse_qs(data.decode())
            dates = query["form[entered_vintage_dates]"][0].split()
            posted.append(len(dates))
            if len(dates) > 2:
                return b"<!DOCTYPE html><html>No observations were retrieved</html>"
            return make_vintage_zip("CPIAUCSL", dates, [(obs, {d: "1.0" for d in dates})
                                                        for obs in self.OBS])

        with mock.patch.object(fetch_data, "request", side_effect=fake_request), \
                mock.patch.object(fetch_data.time, "sleep"):
            raw, actual, *_ = fetch_data.fetch_vintages(
                "CPIAUCSL", self.PLANNED, "1989-12-01", "1990-02-01", "test-agent", rate=0)
        self.assertEqual(len(actual), 6)
        self.assertEqual(fetch_data.vintage_meta(raw)["vintage_count"], 6)
        self.assertEqual(posted[0], 6, "the first POST uses the full batch")
        self.assertTrue(any(n <= 2 for n in posted), "the rejected batch is split")
        self.assertTrue(all(n <= 6 for n in posted))


class MainVintageLog(unittest.TestCase):
    """Full ``main --include-vintages`` path (fake FRED latest + fake ALFRED form) writes the log."""

    AVAILABLE = ["1990-03-14", "1990-06-13", "1990-09-19", "1995-03-15", "1999-12-10",
                 "2000-06-14", "2010-06-15", "2020-06-15", "2026-06-30"]

    def test_main_writes_vintage_and_gate_view_metadata(self):
        def fake_request(url, user_agent, data=None, timeout=240, retries=3):
            if "fredgraph.csv" in url:
                series = url.rsplit("=", 1)[-1]
                return (f"observation_date,{series}\n1947-01-01,1.0\n2026-10-01,2.0\n"
                        ).encode("utf-8")
            if data is None:
                return form_page(self.AVAILABLE)
            query = fetch_data.urllib.parse.parse_qs(data.decode())
            dates = query["form[entered_vintage_dates]"][0].split()
            rows = [("1947-01-01", {d: "1.0" for d in dates}),
                    ("1990-01-01", {d: "2.0" for d in dates})]
            return make_vintage_zip("CPIAUCSL", dates, rows)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "raw"
            log_path = Path(tmp) / "data_log.json"
            with mock.patch.object(fetch_data, "request", side_effect=fake_request), \
                    mock.patch.object(fetch_data.time, "sleep"):
                code = fetch_data.main(["--out", str(out), "--log", str(log_path),
                                        "--include-vintages"])
            self.assertEqual(code, 0, "all series must be retrieved without failures")
            log = json.loads(log_path.read_text(encoding="utf-8"))
            for gate_entry in log["gate_view"]["entries"]:
                self.assertTrue(Path(gate_entry["file"]).exists(), gate_entry["file"])
            for entry in log["entries"]:
                if entry["vintage"] == "set":
                    self.assertTrue(Path(entry["file"]).exists(), entry["file"])

        self.assertEqual(log["failures"], [])
        self.assertEqual(len(log["entries"]), len(PLAN["series"]) + len(VINTAGE_SERIES),
                         f"{len(PLAN['series'])} latest files + {len(VINTAGE_SERIES)} vintage sets")
        sets = [e for e in log["entries"] if e["vintage"] == "set"]
        self.assertEqual(len(sets), len(VINTAGE_SERIES))
        for entry in sets:
            self.assertEqual(entry["vintage_count"], len(self.AVAILABLE))
            self.assertTrue(entry["vintage_check"], entry["series"])
            self.assertEqual(sorted(entry["vintage_dates"]), sorted(self.AVAILABLE))
            self.assertEqual(entry["planned_vintages"], EXPECTED_GRID)
            self.assertEqual(entry["unmapped_planned_dates"], [])
            self.assertTrue(entry["chunks"], entry["series"])
            for chunk in entry["chunks"]:
                self.assertLessEqual(chunk["requested_dates"], fetch_data.VINTAGE_CHUNK_DATES)
                self.assertRegex(chunk["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["merged"]["from_chunks"], len(entry["chunks"]))
            self.assertEqual(entry["merged"]["vintage_count"], len(self.AVAILABLE))
            self.assertRegex(entry["merged"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(log["gate_view"]["limit_dates"], fetch_data.GATE_VIEW_DATES)
        self.assertEqual(len(log["gate_view"]["entries"]), len(VINTAGE_SERIES))
        for gate_entry in log["gate_view"]["entries"]:
            self.assertLessEqual(gate_entry["vintage_count"], fetch_data.GATE_VIEW_DATES)
            self.assertGreaterEqual(sum(1 for d in gate_entry["vintage_dates"] if d < "2000-01-01"),
                                    min(fetch_data.GATE_VIEW_PRE_2000, 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
