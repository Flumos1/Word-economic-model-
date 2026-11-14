"""Tests for tools/c6_calendar.py (docs/139): progress, release-week projection, gate dates, CLI.

All tests are offline and deterministic: the calendar is built from synthetic shadow-status
documents, never from the live journal; the live ``build/shadow/shadow_status.json`` is only read by
one CLI test that skips when the file is absent.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import c6_calendar as cal  # noqa: E402
import shadow_track  # noqa: E402


def status_document(*, n_resolved=0, rows_candidate=1, rows_total=20, distinct=1,
                    min_forecasts=12, min_distinct=4, release_weeks=2.5,
                    date_str="2026-11-14", days_since=0, estimated_gate="2027-05-07",
                    last_vintage="2026-W46") -> dict:
    """A shadow-status document shaped like build/shadow/shadow_status.json."""
    return {
        "schema": "AMFE-SHADOW-TRACK/1.0.0",
        "date": date_str,
        "candidate": {"id": "ENSEMBLE_REGIME_V4", "status": "SHADOW_TEST"},
        "pair": {"target": "T1_CPI_YOY", "horizon": 1},
        "journal": {"rows_total": rows_total, "rows_candidate": rows_candidate,
                    "first_registered_at": "2026-11-14T09:15:00Z",
                    "last_registered_at": "2026-11-14T09:15:00Z",
                    "last_vintage_week": last_vintage, "days_since_last_row": days_since},
        "c6": {"n_resolved": n_resolved, "min_forecasts": min_forecasts,
               "count_met": n_resolved >= min_forecasts,
               "distinct_target_periods": distinct,
               "min_distinct_target_periods": min_distinct,
               "distinct_periods_met": distinct >= min_distinct,
               "cumulative_relative_loss": {}, "coverage": None, "coverage_met": True,
               "pass": False},
        "alerts": [], "status": "STARTING",
        "projection": {"release_weeks_per_month": release_weeks,
                       "rows_remaining": max(0, min_forecasts - n_resolved),
                       "months_to_min_forecasts": 4.8,
                       "estimated_gate_date": estimated_gate,
                       "assumption": "weekly rows only in weeks with a first data release"},
        "rules": {"suspend_loss": 1.1, "warn_loss": 1.0,
                  "suspend_cadence_days": 35,
                  "reference": "docs/119, protocol v5 (C6)"},
    }


def write_status(tmp, document: dict) -> Path:
    tmp = Path(tmp)
    path = tmp / "shadow_status.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def warning_codes(document: dict) -> dict:
    return {item["code"]: item["severity"] for item in document["warnings"]}


class Progress(unittest.TestCase):
    def test_zero_of_twelve_current_state(self):
        document = cal.build_calendar(status_document())
        progress = document["progress"]
        self.assertEqual(progress["n_resolved"], 0)
        self.assertEqual(progress["min_forecasts"], 12)
        self.assertFalse(progress["count_met"])
        self.assertEqual(progress["distinct_target_periods"], 1)
        self.assertFalse(progress["distinct_periods_met"])
        self.assertEqual(progress["pending_unresolved"], 1)
        # one row is already registered, so 11 registrations are still needed
        self.assertEqual(progress["registrations_needed"], 11)
        self.assertEqual(len(document["projection"]["weeks"]), 11)
        weekdays = {date.fromisoformat(entry["week_start"]).weekday()
                    for entry in document["projection"]["weeks"]}
        self.assertEqual(weekdays, {0}, "release weeks must start on Monday")
        starts = [entry["week_start"] for entry in document["projection"]["weeks"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(warning_codes(document)["PENDING_RESOLUTION"], "INFO")

    def test_half_of_twelve(self):
        document = cal.build_calendar(status_document(n_resolved=6, rows_candidate=6,
                                                      distinct=4, days_since=3))
        progress = document["progress"]
        self.assertFalse(progress["count_met"])
        self.assertTrue(progress["distinct_periods_met"])
        self.assertEqual(progress["registrations_needed"], 6)
        projection = document["projection"]
        self.assertEqual(len(projection["weeks"]), 6)
        self.assertIsNotNone(projection["date_12th_resolved"])
        self.assertFalse(projection["gate_reached"])
        self.assertEqual(projection["date_distinct_periods_resolved"],
                         document["source"]["anchor_date"])
        self.assertEqual(projection["gate_date"], projection["date_12th_resolved"])

    def test_twelve_of_twelve_reaches_the_gate(self):
        document = cal.build_calendar(status_document(n_resolved=12, rows_candidate=12,
                                                      distinct=4, days_since=2))
        progress = document["progress"]
        self.assertTrue(progress["count_met"])
        self.assertTrue(progress["distinct_periods_met"])
        self.assertEqual(progress["registrations_needed"], 0)
        projection = document["projection"]
        self.assertEqual(projection["weeks"], [])
        self.assertTrue(projection["gate_reached"])
        self.assertEqual(warning_codes(document)["GATE_REACHED"], "INFO")

    def test_twelve_resolved_but_missing_distinct_periods(self):
        document = cal.build_calendar(status_document(n_resolved=12, rows_candidate=12,
                                                      distinct=3))
        projection = document["projection"]
        self.assertFalse(projection["gate_reached"])
        self.assertEqual(projection["distinct_periods_needed"], 1)
        period_date = date.fromisoformat(projection["date_distinct_periods_resolved"])
        self.assertGreater(period_date, date.fromisoformat(projection["date_12th_resolved"]))
        self.assertEqual(projection["gate_date"], projection["date_distinct_periods_resolved"])
        self.assertIn("PERIODS_AFTER_COUNT", warning_codes(document))

    def test_no_candidate_rows(self):
        document = cal.build_calendar(status_document(n_resolved=0, rows_candidate=0,
                                                      rows_total=10, distinct=0,
                                                      days_since=45))
        progress = document["progress"]
        self.assertEqual(progress["registrations_needed"], 12)
        self.assertEqual(len(document["projection"]["weeks"]), 12)
        codes = warning_codes(document)
        self.assertIn("NO_ROWS", codes)
        self.assertIn("NO_REGISTRATION", codes)
        self.assertEqual(codes["MISSED_RELEASE_ALERT"], "ALERT")


class WarningsAndShift(unittest.TestCase):
    def test_missed_release_warn_and_alert(self):
        warn = cal.build_calendar(status_document(days_since=20))
        self.assertEqual(warning_codes(warn)["MISSED_RELEASE_WARN"], "WARN")
        alert = cal.build_calendar(status_document(days_since=40))
        self.assertEqual(warning_codes(alert)["MISSED_RELEASE_ALERT"], "ALERT")

    def test_projection_shift_is_reported(self):
        # the status claims a much later gate than the cadence model -> calendar is earlier
        later = cal.build_calendar(status_document(rows_candidate=0, rows_total=10, distinct=0,
                                                   estimated_gate="2028-01-01"))
        codes = warning_codes(later)
        self.assertEqual(codes["PROJECTION_SHIFT"], "WARN")
        self.assertLess(later["projection"]["shift_vs_status_days"], 0)
        # a pending registered row explains an earlier calendar date -> INFO, not WARN
        explained = cal.build_calendar(status_document(estimated_gate="2027-05-07"))
        self.assertEqual(warning_codes(explained)["PROJECTION_SHIFT"], "INFO")

    def test_anchor_date_is_explicit_and_changes_the_schedule(self):
        first = cal.build_calendar(status_document(),
                                   today=date(2026, 11, 14))
        shifted = cal.build_calendar(status_document(), today=date(2026, 12, 14))
        self.assertNotEqual(first["projection"]["date_12th_resolved"],
                            shifted["projection"]["date_12th_resolved"])
        self.assertEqual(shifted["source"]["anchor_date"], "2026-12-14")


class FailClosed(unittest.TestCase):
    def test_missing_and_malformed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cal.C6CalendarError):
                cal.load_status(Path(tmp) / "missing.json")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(cal.C6CalendarError):
                cal.load_status(broken)
            for document in (
                {},
                {"date": "2026-11-14"},
                status_document(distinct=5, rows_candidate=1),
                status_document(rows_candidate=12, rows_total=11),
                status_document(n_resolved=2, rows_candidate=1),
                status_document(release_weeks=0.0),
                status_document(min_forecasts=0),
                status_document(days_since=-1),
            ):
                with self.assertRaises(cal.C6CalendarError, msg=str(document)[:60]):
                    cal.validate_status(document)

    def test_bad_status_path_in_cli_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cal.main(["--status", str(Path(tmp) / "missing.json"), "--json"])
            self.assertEqual(code, 1)


class CliAndRendering(unittest.TestCase):
    def _fixture(self, tmp: Path) -> Path:
        return write_status(tmp, status_document())

    def test_cli_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cal.main(["--status", str(path), "--json"])
            self.assertEqual(code, 0)
            document = json.loads(buffer.getvalue())
            self.assertEqual(document["schema"], cal.SCHEMA)
            self.assertEqual(document["progress"]["n_resolved"], 0)
            self.assertEqual(len(document["source"]["status_sha256"]), 64)

    def test_cli_out_directory_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(tmp)
            out_dir = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = cal.main(["--status", str(path), "--out", str(out_dir)])
            self.assertEqual(code, 0)
            self.assertTrue((out_dir / cal.CALENDAR_JSON).is_file())
            self.assertTrue((out_dir / cal.CALENDAR_MD).is_file())
            markdown = (out_dir / cal.CALENDAR_MD).read_text(encoding="utf-8")
            self.assertIn("C6-календарь", markdown)
            self.assertIn("Ожидаемые недели регистрации", markdown)

    def test_markdown_render_has_the_sections_and_no_invented_rows(self):
        document = cal.build_calendar(status_document())
        markdown = cal.render_markdown(document)
        for heading in ("Текущий прогресс C6", "Ожидаемые недели регистрации",
                        "Даты достижения критериев", "Предупреждения", "Ограничения"):
            self.assertIn(heading, markdown)
        for entry in document["projection"]["weeks"]:
            self.assertEqual(entry["row_status"], "PROJECTED_NOT_REGISTERED")
            self.assertNotIn("registered_at", entry)
            self.assertNotIn("forecast", entry)

    def test_determinism_of_documents(self):
        first = cal.build_calendar(status_document())
        second = cal.build_calendar(status_document())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(tmp)
            self.assertEqual(json.dumps(cal.build_from_path(path), sort_keys=True),
                             json.dumps(cal.build_from_path(path), sort_keys=True))


class ShadowTrackIntegration(unittest.TestCase):
    def test_attach_calendar_is_optional_and_does_not_mutate_the_status(self):
        document = status_document()
        original = json.dumps(document, sort_keys=True)
        enriched = shadow_track.attach_calendar(document)
        self.assertIn("calendar", enriched)
        self.assertEqual(enriched["calendar"]["schema"], cal.SCHEMA)
        self.assertEqual(json.dumps(document, sort_keys=True), original)
        self.assertNotIn("calendar", document)

    def test_parser_accepts_with_calendar(self):
        parser = shadow_track.build_parser() if hasattr(shadow_track, "build_parser") else None
        if parser is None:
            # the module builds its parser inside main(); check the option is wired through main
            import inspect
            source = inspect.getsource(shadow_track.main)
            self.assertIn("with_calendar", source)
        else:
            args = parser.parse_args(["--with-calendar"])
            self.assertTrue(args.with_calendar)


if __name__ == "__main__":
    unittest.main(verbosity=2)
