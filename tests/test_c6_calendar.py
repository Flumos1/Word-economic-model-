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


# ------------------------------------------------------------------ operational process fixtures
QUANTILES = [round(1.0 + 0.1 * index, 2) for index in range(1, 20)]


def process_row(*, candidate="ENSEMBLE_REGIME_V4", target="T1_CPI_YOY", horizon=1,
                origin="2026-11", vintage_week="2026-W46",
                registered_at="2026-11-13T09:00:00Z", control=False, point=2.0) -> dict:
    row = {
        "candidate_id": candidate,
        "code_commit": "a" * 40,
        "data_vintage_hash": "b" * 64,
        "target": target,
        "horizon": int(horizon),
        "origin": origin,
        "scoring_rule_commit": "c" * 40,
        "registered_at": registered_at,
        "point": float(point),
        "quantiles_5_to_95_step_5": list(QUANTILES),
        "vintage_week": vintage_week,
    }
    if control:
        row["control_arm"] = True
    if target == "T3_RECESSION":
        row["point"] = None
        row["event_probability"] = 0.3
        row.pop("quantiles_5_to_95_step_5")
    return row


def write_journal(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(cal._canonical(row) + "\n" for row in rows), encoding="utf-8",
                    newline="\n")


def controls_for(origin: str, vintage_week: str, registered_at: str) -> list[dict]:
    return [process_row(origin=origin, vintage_week=vintage_week, registered_at=registered_at,
                        candidate=arm, control=True)
            for arm in ("EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY")]

def facts(mapping: dict):
    def lookup(target, origin, horizon):
        entry = mapping.get((target, str(origin), int(horizon)), {})
        return {"primary": entry.get("primary"),
                "primary_vintage": entry.get("published"),
                "available": entry.get("available", True)}
    return lookup


def codes(document: dict) -> set[str]:
    return {item["code"] for item in document["alerts"]}


class RegistrationProcess(unittest.TestCase):
    def test_weekly_batch_registers_candidate_and_control_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            audit = Path(tmp) / "audit.jsonl"
            batch = [process_row(vintage_week="2026-W47", registered_at="2026-11-20T09:00:00Z")]
            batch += controls_for("2026-11", "2026-W47", "2026-11-20T09:00:00Z")
            result = cal.register_week(batch, journal=journal, audit=audit,
                                       as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertEqual(len(result["appended"]), 3)
            self.assertEqual(len(cal._read_journal(journal)), 3)
            events = cal.read_audit(audit)
            self.assertEqual(len(events), 3)
            self.assertTrue(all(event["event"] == "ROW_REGISTERED" for event in events))
            self.assertEqual({event["vintage_week"] for event in events}, {"2026-W47"})

    def test_registration_without_control_group_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            with self.assertRaises(cal.C6CalendarError) as caught:
                cal.register_week([process_row(vintage_week="2026-W47",
                                               registered_at="2026-11-20T09:00:00Z")],
                                  journal=journal, audit=Path(tmp) / "audit.jsonl",
                                  as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertIn("control group", str(caught.exception))
            self.assertEqual(cal._read_journal(journal), [])

    def test_candidate_only_batch_uses_controls_already_in_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            write_journal(journal, controls_for("2026-11", "2026-W47", "2026-11-20T09:00:00Z"))
            result = cal.register_week(
                [process_row(vintage_week="2026-W47", registered_at="2026-11-20T09:00:00Z")],
                journal=journal, audit=Path(tmp) / "audit.jsonl",
                as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertEqual(len(result["appended"]), 1)
            self.assertEqual(len(cal._read_journal(journal)), 3)

    def test_registration_refuses_a_published_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            batch = [process_row(vintage_week="2026-W47", registered_at="2026-11-20T09:00:00Z")]
            batch += controls_for("2026-11", "2026-W47", "2026-11-20T09:00:00Z")
            lookup = facts({("T1_CPI_YOY", "2026-11", 1):
                            {"primary": 3.0, "published": "2026-11-15"}})
            with self.assertRaises(cal.C6CalendarError) as caught:
                cal.register_week(batch, journal=journal, audit=Path(tmp) / "audit.jsonl",
                                  as_of=date(2026, 11, 20), fact_lookup=lookup)
            self.assertIn("look-ahead", str(caught.exception))
            self.assertEqual(cal._read_journal(journal), [])

    def test_registration_refuses_past_or_future_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = [process_row(vintage_week="2026-W46", registered_at="2026-11-13T09:00:00Z")]
            batch += controls_for("2026-11", "2026-W46", "2026-11-13T09:00:00Z")
            with self.assertRaises(cal.C6CalendarError) as caught:
                cal.register_week(batch, journal=Path(tmp) / "forecasts.jsonl",
                                  audit=Path(tmp) / "audit.jsonl",
                                  as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertIn("backdating", str(caught.exception))

    def test_duplicate_identical_batch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            audit = Path(tmp) / "audit.jsonl"
            batch = [process_row(vintage_week="2026-W47", registered_at="2026-11-20T09:00:00Z")]
            batch += controls_for("2026-11", "2026-W47", "2026-11-20T09:00:00Z")
            first = cal.register_week(batch, journal=journal, audit=audit,
                                      as_of=date(2026, 11, 20), fact_lookup=facts({}))
            second = cal.register_week(batch, journal=journal, audit=audit,
                                       as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertEqual(len(first["appended"]), 3)
            self.assertEqual(second["appended"], [])
            self.assertEqual(len(second["skipped"]), 3)
            self.assertEqual(len(cal._read_journal(journal)), 3)
            self.assertEqual(len(cal.read_audit(audit)), 3)


class ResolutionProcess(unittest.TestCase):
    def _journal(self, tmp: Path) -> Path:
        journal = tmp / "forecasts.jsonl"
        write_journal(journal, [
            process_row(origin="2026-09", vintage_week="2026-W37",
                        registered_at="2026-09-09T09:00:00Z"),
            process_row(origin="2026-10", vintage_week="2026-W41",
                        registered_at="2026-10-07T09:00:00Z"),
        ])
        return journal

    def test_resolution_only_after_first_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(Path(tmp))
            lookup = facts({
                ("T1_CPI_YOY", "2026-09", 1): {"primary": 2.5, "published": "2026-10-10"},
                ("T1_CPI_YOY", "2026-10", 1): {"primary": 2.6, "published": "2026-11-25"},
            })
            result = cal.resolve_journal(journal=journal, audit=Path(tmp) / "audit.jsonl",
                                         as_of=date(2026, 11, 20), fact_lookup=lookup)
            self.assertEqual(result["resolved"], 1)
            self.assertEqual(result["future_fact_blocked"], 1)
            self.assertEqual(result["unresolved"], 0)
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["first_publication"], "2026-10-10")

    def test_resolution_audit_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(Path(tmp))
            audit = Path(tmp) / "audit.jsonl"
            lookup = facts({
                ("T1_CPI_YOY", "2026-09", 1): {"primary": 2.5, "published": "2026-10-10"},
                ("T1_CPI_YOY", "2026-10", 1): {"primary": 2.6, "published": "2026-11-25"},
            })
            first = cal.resolve_journal(journal=journal, audit=audit, as_of=date(2026, 11, 20),
                                        fact_lookup=lookup, persist=True)
            second = cal.resolve_journal(journal=journal, audit=audit, as_of=date(2026, 11, 20),
                                         fact_lookup=lookup, persist=True)
            self.assertEqual(first["written"], 1)
            self.assertEqual(second["written"], 0)
            self.assertEqual(len(cal.read_audit(audit)), 1)

    def test_no_raw_values_in_the_audit_or_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._journal(Path(tmp))
            audit = Path(tmp) / "audit.jsonl"
            lookup = facts({
                ("T1_CPI_YOY", "2026-09", 1): {"primary": 9.876543, "published": "2026-10-10"},
                ("T1_CPI_YOY", "2026-10", 1): {"primary": None, "published": None},
            })
            cal.resolve_journal(journal=journal, audit=audit, as_of=date(2026, 11, 20),
                                fact_lookup=lookup, persist=True)
            document = cal.build_process_report(journal=journal, audit=audit,
                                                as_of=date(2026, 11, 20), fact_lookup=lookup)
            audit_text = audit.read_text(encoding="utf-8")
            report_text = json.dumps(document, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("9.876543", audit_text)
            self.assertNotIn("9.876543", report_text)
            self.assertFalse(document["raw_values"])


class ReportProcess(unittest.TestCase):
    def _full_sample(self, tmp: Path) -> Path:
        journal = tmp / "forecasts.jsonl"
        specs = [
            ("2026-08", "2026-W32", "2026-08-05"),
            ("2026-08", "2026-W33", "2026-08-12"),
            ("2026-08", "2026-W34", "2026-08-19"),
            ("2026-09", "2026-W37", "2026-09-09"),
            ("2026-09", "2026-W38", "2026-09-16"),
            ("2026-09", "2026-W39", "2026-09-23"),
            ("2026-10", "2026-W41", "2026-10-07"),
            ("2026-10", "2026-W42", "2026-10-14"),
            ("2026-10", "2026-W43", "2026-10-21"),
            ("2026-11", "2026-W45", "2026-11-04"),
            ("2026-11", "2026-W46", "2026-11-11"),
            ("2026-11", "2026-W47", "2026-11-18"),
        ]
        rows = []
        for origin, week, registered in specs:
            rows.append(process_row(origin=origin, vintage_week=week,
                                    registered_at=f"{registered}T09:00:00Z"))
            rows += controls_for(origin, week, f"{registered}T09:00:00Z")
        write_journal(journal, rows)
        return journal

    def _full_facts(self):
        return facts({
            ("T1_CPI_YOY", "2026-08", 1): {"primary": 2.4, "published": "2026-09-10"},
            ("T1_CPI_YOY", "2026-09", 1): {"primary": 2.5, "published": "2026-10-10"},
            ("T1_CPI_YOY", "2026-10", 1): {"primary": 2.6, "published": "2026-11-10"},
            ("T1_CPI_YOY", "2026-11", 1): {"primary": 2.7, "published": "2026-12-10"},
        })

    def test_twelve_of_twelve_and_four_periods(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._full_sample(Path(tmp))
            document = cal.build_process_report(journal=journal, audit=Path(tmp) / "audit.jsonl",
                                                as_of=date(2026, 12, 20),
                                                fact_lookup=self._full_facts())
            c6 = document["c6"]
            self.assertEqual(c6["n_resolved"], 12)
            self.assertEqual(c6["distinct_target_periods"], 4)
            self.assertEqual(c6["distinct_resolved_periods"], 4)
            self.assertTrue(c6["counts_and_periods_met"])
            self.assertEqual(c6["status"], "COUNTS_MET")
            self.assertEqual(c6["status_line"], "12/12 прогнозов, 4/4 периодов")
            self.assertEqual(c6["gate"], "COUNTS_MET_LOSS_PENDING")
            self.assertFalse(c6["loss_coverage_checked"])
            self.assertTrue(document["control_group"]["complete"])

    def test_control_group_gap_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            write_journal(journal, [
                process_row(vintage_week="2026-W47", registered_at="2026-11-18T09:00:00Z"),
                process_row(vintage_week="2026-W47", registered_at="2026-11-18T09:00:00Z",
                            candidate="EQUAL_WEIGHT_POINT", control=True),
            ])
            document = cal.build_process_report(journal=journal,
                                                audit=Path(tmp) / "audit.jsonl",
                                                as_of=date(2026, 11, 20), fact_lookup=facts({}))
            self.assertFalse(document["control_group"]["complete"])
            self.assertEqual(document["control_group"]["incomplete"][0]["missing_arms"],
                             ["EQUAL_WEIGHT_DENSITY"])
            self.assertIn("CONTROL_GROUP_MISSING", codes(document))

    def test_two_consecutive_missed_weeks_raise_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            rows = [process_row(origin="2026-10", vintage_week="2026-W44",
                                registered_at="2026-10-28T09:00:00Z")]
            rows += controls_for("2026-10", "2026-W44", "2026-10-28T09:00:00Z")
            rows.append(process_row(origin="2026-11", vintage_week="2026-W47",
                                    registered_at="2026-11-18T09:00:00Z"))
            rows += controls_for("2026-11", "2026-W47", "2026-11-18T09:00:00Z")
            write_journal(journal, rows)
            document = cal.build_process_report(journal=journal,
                                                audit=Path(tmp) / "audit.jsonl",
                                                as_of=date(2026, 11, 27), fact_lookup=facts({}))
            self.assertEqual(document["weeks"]["missed"], ["2026-W45", "2026-W46"])
            self.assertEqual(document["weeks"]["max_consecutive_missed"], 2)
            self.assertIn("MISSED_WEEK_ALERT", codes(document))

    def test_missing_week_check_is_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            rows = [process_row(origin="2026-11", vintage_week="2026-W45",
                                registered_at="2026-11-04T09:00:00Z")]
            rows += controls_for("2026-11", "2026-W45", "2026-11-04T09:00:00Z")
            rows.append(process_row(origin="2026-11", vintage_week="2026-W47",
                                    registered_at="2026-11-18T09:00:00Z"))
            rows += controls_for("2026-11", "2026-W47", "2026-11-18T09:00:00Z")
            write_journal(journal, rows)
            audit = Path(tmp) / "audit.jsonl"
            cal.append_audit_events([
                {"schema": cal.AUDIT_SCHEMA, "event": "WEEK_CHECK", "at": "2026-11-06",
                 "week": "2026-W45", "candidate_rows": 1, "control_rows": 2, "registered": True},
                {"schema": cal.AUDIT_SCHEMA, "event": "WEEK_CHECK", "at": "2026-11-20",
                 "week": "2026-W47", "candidate_rows": 1, "control_rows": 2, "registered": True},
            ], audit)
            document = cal.build_process_report(journal=journal, audit=audit,
                                                as_of=date(2026, 11, 27), fact_lookup=facts({}))
            self.assertEqual(document["weeks"]["missed_checks"], ["2026-W46"])
            self.assertIn("MISSED_CHECK", codes(document))

    def test_unresolved_overdue_is_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            rows = [process_row(origin="2026-01", vintage_week="2026-W05",
                                registered_at="2026-02-02T09:00:00Z")]
            rows += controls_for("2026-01", "2026-W05", "2026-02-02T09:00:00Z")
            write_journal(journal, rows)
            document = cal.build_process_report(journal=journal,
                                                audit=Path(tmp) / "audit.jsonl",
                                                as_of=date(2026, 12, 31), fact_lookup=facts({}))
            self.assertEqual(document["unresolved"]["pending"], 1)
            self.assertEqual(len(document["unresolved"]["overdue"]), 1)
            self.assertIn("UNRESOLVED_OVERDUE", codes(document))

    def test_process_report_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._full_sample(Path(tmp))
            first = cal.build_process_report(journal=journal, audit=Path(tmp) / "audit.jsonl",
                                             as_of=date(2026, 12, 20),
                                             fact_lookup=self._full_facts())
            second = cal.build_process_report(journal=journal, audit=Path(tmp) / "audit.jsonl",
                                              as_of=date(2026, 12, 20),
                                              fact_lookup=self._full_facts())
            self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                             json.dumps(second, ensure_ascii=False, sort_keys=True))


class ProcessFailClosed(unittest.TestCase):
    def test_broken_journal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            journal.write_text("{not json\n", encoding="utf-8")
            with self.assertRaises(cal.C6CalendarError):
                cal.build_process_report(journal=journal, audit=Path(tmp) / "audit.jsonl",
                                         as_of=date(2026, 11, 20), fact_lookup=facts({}))

    def test_broken_audit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            write_journal(journal, [process_row()])
            audit = Path(tmp) / "audit.jsonl"
            audit.write_text("{not json\n", encoding="utf-8")
            with self.assertRaises(cal.C6CalendarError):
                cal.build_process_report(journal=journal, audit=audit,
                                         as_of=date(2026, 11, 20), fact_lookup=facts({}))

    def test_invalid_record_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = process_row()
            row["target"] = "NOT_A_TARGET"
            with self.assertRaises(cal.C6CalendarError):
                cal.register_week([row], journal=Path(tmp) / "forecasts.jsonl",
                                  audit=Path(tmp) / "audit.jsonl",
                                  as_of=date(2026, 11, 20), fact_lookup=facts({}))

    def test_missing_records_file_in_cli_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cal.main(["--register", "--records", str(Path(tmp) / "missing.json"),
                             "--date", "2026-11-20",
                             "--journal", str(Path(tmp) / "forecasts.jsonl"),
                             "--audit", str(Path(tmp) / "audit.jsonl")])
            self.assertEqual(code, 1)


class OperationalCli(unittest.TestCase):
    def _fixture(self, tmp: Path) -> Path:
        journal = tmp / "forecasts.jsonl"
        rows = [process_row(origin="2026-11", vintage_week="2026-W46",
                            registered_at="2026-11-11T09:00:00Z")]
        rows += controls_for("2026-11", "2026-W46", "2026-11-11T09:00:00Z")
        write_journal(journal, rows)
        return journal

    def test_weekly_cli_writes_report_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._fixture(Path(tmp))
            audit = Path(tmp) / "audit.jsonl"
            out = Path(tmp) / "report"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cal.main(["--weekly", "--date", "2026-11-20", "--journal", str(journal),
                                 "--audit", str(audit), "--report-out", str(out), "--json"])
            self.assertEqual(code, 0)
            document = json.loads(buffer.getvalue())
            self.assertEqual(document["schema"], cal.PROCESS_SCHEMA)
            self.assertEqual(document["c6"]["n_resolved"], 0)
            self.assertTrue((out / cal.REPORT_JSON).is_file())
            self.assertTrue((out / cal.REPORT_MD).is_file())
            events = cal.read_audit(audit)
            self.assertEqual(events[-1]["event"], "WEEK_CHECK")
            self.assertEqual(events[-1]["week"], "2026-W47")

    def test_strict_mode_returns_two_on_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "forecasts.jsonl"
            rows = [process_row(origin="2026-10", vintage_week="2026-W44",
                                registered_at="2026-10-28T09:00:00Z")]
            rows += controls_for("2026-10", "2026-W44", "2026-10-28T09:00:00Z")
            rows.append(process_row(origin="2026-11", vintage_week="2026-W47",
                                    registered_at="2026-11-18T09:00:00Z"))
            rows += controls_for("2026-11", "2026-W47", "2026-11-18T09:00:00Z")
            write_journal(journal, rows)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cal.main(["--weekly", "--strict", "--date", "2026-11-27",
                                 "--journal", str(journal),
                                 "--audit", str(Path(tmp) / "audit.jsonl")])
            self.assertEqual(code, 2)


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
