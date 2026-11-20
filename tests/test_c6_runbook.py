"""Tests for tools/c6_runbook.py (docs/202): counts 12/4, row verdicts, release windows,
registration gate, checklist, --check, no mutation, determinism and the CLI.

All tests are offline and deterministic: journal and shadow status are built in a temporary
directory from synthetic rows, never from the live registry; the only live input is the read-only
``build/shadow/shadow_status.json`` in one CLI smoke test that skips when the file is absent.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import c6_calendar as cal  # noqa: E402
import c6_runbook as rb  # noqa: E402
from registry import canonical_json  # noqa: E402

QUANTILES = [round(1.0 + 0.1 * index, 2) for index in range(1, 20)]
CANDIDATE = "ENSEMBLE_REGIME_V4"
ARMS = ("EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY")


# ------------------------------------------------------------------------------- journal fixtures
def row(*, candidate=CANDIDATE, target="T1_CPI_YOY", horizon=1, origin="2026-11",
        registered_at="2026-11-14T09:00:00Z", control=False, point=2.0) -> dict:
    """A valid registry row (the journal is validated fail-closed on read)."""
    day = date.fromisoformat(registered_at[:10])
    record = {
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
        "vintage_week": cal.iso_week_label(day),
    }
    if control:
        record["control_arm"] = True
    return record


def week_group(origin: str, registered_at: str, *, arms=ARMS, candidate=CANDIDATE) -> list[dict]:
    rows = [row(origin=origin, registered_at=registered_at)]
    rows += [row(candidate=arm, control=True, origin=origin, registered_at=registered_at)
             for arm in arms]
    return rows


def live_like_rows() -> list[dict]:
    """One candidate row (2026-11, 2026-W46) with its full control group — the live shape."""
    return week_group("2026-11", "2026-11-14T09:15:00Z")


def twelve_resolved_rows() -> list[dict]:
    """12 candidate rows over 4 target periods with controls: the C6 counts are met."""
    rows: list[dict] = []
    for origin, days in (("2026-07", (7, 14, 21)),
                         ("2026-08", (7, 14, 21)),
                         ("2026-09", (9, 16, 23)),
                         ("2026-10", (7, 14, 21))):
        for day in days:
            rows += week_group(origin, f"{origin}-{day:02d}T09:00:00Z")
    return rows


def resolved_facts(origins=("2026-07", "2026-08", "2026-09", "2026-10")):
    """First publications of the periods: mid of the following month, before the report date."""
    mapping = {}
    for origin in origins:
        year, month = (int(part) for part in origin.split("-"))
        published = date(year + (month // 12), month % 12 + 1, 15)
        mapping[("T1_CPI_YOY", origin, 1)] = {"primary": 2.5,
                                              "published": published.isoformat()}
    return mapping


def facts(mapping: dict):
    def lookup(target, origin, horizon):
        entry = mapping.get((target, str(origin), int(horizon)), {})
        return {"primary": entry.get("primary"),
                "primary_vintage": entry.get("published"),
                "available": entry.get("available", True)}
    return lookup


def write_journal(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical_json(item) + "\n" for item in rows), encoding="utf-8",
                    newline="\n")
    return path


def status_document(*, date_str="2026-11-14", rows_total=3, rows_candidate=1, n_resolved=0,
                    distinct=1, release_weeks=2.5, alerts=None,
                    estimated_gate="2027-05-07") -> dict:
    return {
        "schema": "AMFE-SHADOW-TRACK/1.0.0",
        "date": date_str,
        "candidate": {"id": CANDIDATE, "status": "SHADOW_TEST"},
        "pair": {"target": "T1_CPI_YOY", "horizon": 1},
        "journal": {"rows_total": rows_total, "rows_candidate": rows_candidate,
                    "first_registered_at": "2026-11-14T09:15:00Z",
                    "last_registered_at": "2026-11-14T09:15:00Z",
                    "last_vintage_week": "2026-W46", "days_since_last_row": 4},
        "c6": {"n_resolved": n_resolved, "min_forecasts": 12,
               "count_met": n_resolved >= 12, "distinct_target_periods": distinct,
               "min_distinct_target_periods": 4, "distinct_periods_met": distinct >= 4,
               "cumulative_relative_loss": {}, "coverage": None, "coverage_met": True,
               "pass": False},
        "alerts": list(alerts or []), "status": "STARTING",
        "projection": {"release_weeks_per_month": release_weeks,
                       "rows_remaining": max(0, 12 - n_resolved),
                       "months_to_min_forecasts": 4.8,
                       "estimated_gate_date": estimated_gate,
                       "assumption": "weekly rows only in weeks with a first data release"},
        "rules": {"suspend_loss": 1.1, "warn_loss": 1.0, "suspend_cadence_days": 35,
                  "reference": "docs/119, protocol v5 (C6)"},
    }


def write_status(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


class Fixture:
    """A temporary journal + status (+ optional audit) with the shape of the live inputs."""

    def __init__(self, tmp, rows=None, status=None, audit_events=None):
        self.tmp = Path(tmp)
        self.rows = list(live_like_rows() if rows is None else rows)
        status = status or status_document(rows_total=len(self.rows),
                                           rows_candidate=sum(
                                               1 for item in self.rows
                                               if item.get("candidate_id") == CANDIDATE
                                               and not item.get("control_arm")))
        self.journal = write_journal(self.tmp / "forecasts.jsonl", self.rows)
        self.status = write_status(self.tmp / "shadow_status.json", status)
        self.audit = self.tmp / "c6_audit.jsonl"
        if audit_events:
            self.audit.write_text("".join(canonical_json(event) + "\n" for event in audit_events),
                                  encoding="utf-8", newline="\n")

    def build(self, **kwargs):
        kwargs.setdefault("journal", self.journal)
        kwargs.setdefault("status", self.status)
        kwargs.setdefault("audit", self.audit)
        return rb.build_runbook(**kwargs)

    def check(self, **kwargs):
        kwargs.setdefault("journal", self.journal)
        kwargs.setdefault("status", self.status)
        kwargs.setdefault("audit", self.audit)
        return rb.build_check(**kwargs)


def alert_codes(document: dict) -> set:
    return {item["code"] for item in document["alerts"]}


# ------------------------------------------------------------------------------------- counts 12/4
class Counts(unittest.TestCase):
    def test_live_shape_zero_of_twelve_and_one_of_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            document = fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            c6 = document["c6"]
            self.assertEqual(c6["n_resolved"], 0)
            self.assertEqual(c6["distinct_target_periods"], 1)
            self.assertEqual(c6["forecasts_line"], "0/12")
            self.assertEqual(c6["periods_line"], "1/4")
            self.assertEqual(c6["remaining_forecasts"], 12)
            self.assertEqual(c6["remaining_periods"], 3)
            self.assertEqual(c6["status"], "STARTING")
            self.assertEqual(c6["gate"], "NOT_MET")
            self.assertFalse(c6["loss_coverage_checked"])
            self.assertEqual(document["rows"]["total"], 1)
            self.assertEqual(document["rows"]["counts"],
                             {"allowed": 0, "pending": 1, "blocked": 0, "rejected": 0})

    def test_twelve_resolved_rows_over_four_periods_meet_the_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = twelve_resolved_rows()
            status = status_document(rows_total=len(rows), rows_candidate=12,
                                     n_resolved=12, distinct=4)
            fixture = Fixture(tmp, rows=rows, status=status)
            document = fixture.build(as_of=date(2026, 11, 20),
                                     fact_lookup=facts(resolved_facts()))
            c6 = document["c6"]
            self.assertEqual(c6["n_resolved"], 12)
            self.assertEqual(c6["distinct_target_periods"], 4)
            self.assertEqual(c6["distinct_resolved_periods"], 4)
            self.assertEqual(c6["remaining_forecasts"], 0)
            self.assertEqual(c6["remaining_periods"], 0)
            self.assertTrue(c6["counts_and_periods_met"])
            self.assertEqual(c6["status"], "COUNTS_MET")
            self.assertEqual(c6["gate"], "COUNTS_MET_LOSS_PENDING")
            self.assertIn("COUNTS_MET", alert_codes(document))
            self.assertEqual(document["rows"]["counts"]["allowed"], 12)

    def test_pending_row_is_counted_separately_and_still_projected(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Fixture(tmp).build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            self.assertEqual(document["rows"]["pending"], ["2026-11/2026-W46"])
            self.assertEqual(document["c6"]["n_resolved"], 0)
            self.assertIn("PENDING_RESOLUTION", alert_codes(document))
            # the pending row is already registered, so 11 registrations are still needed
            self.assertEqual(document["c6"]["registrations_needed"], 11)
            self.assertIsNotNone(document["projection"]["date_12th_resolved"])

    def test_empty_journal_is_reported_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            journal = write_journal(tmp / "forecasts.jsonl", [])
            status = write_status(tmp / "shadow_status.json",
                                  status_document(rows_total=0, rows_candidate=0,
                                                  n_resolved=0, distinct=0))
            document = rb.build_runbook(journal=journal, status=status,
                                       audit=tmp / "c6_audit.jsonl",
                                       as_of=date(2026, 11, 18), fact_lookup=facts({}))
            self.assertEqual(document["rows"]["total"], 0)
            self.assertEqual(document["c6"]["status"], "STARTING")
            self.assertIn("NO_ROWS", alert_codes(document))
            report = rb.build_check(journal=journal, status=status,
                                    audit=tmp / "c6_audit.jsonl",
                                    as_of=date(2026, 11, 18), fact_lookup=facts({}))
            self.assertTrue(report["ok"])
            self.assertEqual(report["problems"], [])


# ----------------------------------------------------------------------------------- row verdicts
class RowVerdicts(unittest.TestCase):
    def _single(self, rows, *, as_of, lookup):
        with tempfile.TemporaryDirectory() as tmp:
            status = status_document(rows_total=len(rows),
                                     rows_candidate=sum(1 for item in rows
                                                        if item.get("candidate_id") == CANDIDATE
                                                        and not item.get("control_arm")),
                                     n_resolved=0, distinct=0)
            return Fixture(tmp, rows=rows, status=status).build(as_of=as_of, fact_lookup=lookup)

    def test_row_without_control_arms_is_rejected_and_not_counted(self):
        document = self._single([row(registered_at="2026-11-14T09:15:00Z")],
                                as_of=date(2026, 11, 20),
                                lookup=facts({("T1_CPI_YOY", "2026-11", 1):
                                              {"primary": 2.5, "published": "2026-11-15"}}))
        entry = document["rows"]["entries"][0]
        self.assertEqual(entry["state"], "REJECTED")
        self.assertIn("CONTROL_GROUP_MISSING", entry["codes"])
        self.assertEqual(document["c6"]["n_resolved"], 0)
        self.assertEqual(document["c6"]["distinct_target_periods"], 0)
        self.assertFalse(document["control_group"]["complete"])
        self.assertEqual(document["control_group"]["incomplete"][0]["missing_arms"], list(ARMS))
        self.assertIn("CONTROL_GROUP_MISSING", alert_codes(document))
        self.assertIn("CONTROL_GROUP", {item["code"] for item in document["missing"]})

    def test_look_ahead_row_is_rejected(self):
        rows = week_group("2026-11", "2026-11-14T09:15:00Z")
        document = self._single(rows, as_of=date(2026, 11, 20),
                                lookup=facts({("T1_CPI_YOY", "2026-11", 1):
                                              {"primary": 2.5, "published": "2026-11-10"}}))
        entry = document["rows"]["entries"][0]
        self.assertEqual(entry["state"], "REJECTED")
        self.assertIn("PUBLISHED_BEFORE_REGISTRATION", entry["codes"])
        self.assertFalse(entry["counts_toward_c6"])
        self.assertEqual(document["c6"]["n_resolved"], 0)

    def test_fact_published_after_the_report_date_is_blocked_not_rejected(self):
        rows = week_group("2026-11", "2026-11-14T09:15:00Z")
        document = self._single(rows, as_of=date(2026, 11, 20),
                                lookup=facts({("T1_CPI_YOY", "2026-11", 1):
                                              {"primary": 2.5, "published": "2026-12-15"}}))
        entry = document["rows"]["entries"][0]
        self.assertEqual(entry["state"], "BLOCKED")
        self.assertIn("FUTURE_FACT_BLOCKED", entry["codes"])
        self.assertEqual(document["c6"]["blocked"], 1)
        self.assertEqual(document["c6"]["n_resolved"], 0)
        self.assertIn("FUTURE_FACT_BLOCKED", alert_codes(document))

    def test_inconsistent_vintage_week_is_rejected(self):
        broken = row(registered_at="2026-11-14T09:15:00Z")
        broken["vintage_week"] = "2026-W40"
        rows = [broken] + [row(candidate=arm, control=True, registered_at="2026-11-14T09:15:00Z")
                           for arm in ARMS]
        document = self._single(rows, as_of=date(2026, 11, 20),
                                lookup=facts({("T1_CPI_YOY", "2026-11", 1):
                                              {"primary": 2.5, "published": "2026-11-15"}}))
        entries = [entry for entry in document["rows"]["entries"]
                   if entry["state"] == "REJECTED"]
        self.assertEqual(len(entries), 1)
        self.assertIn("VINTAGE_WEEK_MISMATCH", entries[0]["codes"])


# ---------------------------------------------------------------------------------- release windows
class ReleaseWindows(unittest.TestCase):
    def _document(self, as_of=date(2026, 11, 18)):
        with tempfile.TemporaryDirectory() as tmp:
            return Fixture(tmp).build(as_of=as_of, fact_lookup=facts({}))

    def test_windows_are_projected_not_registered_and_monotonic(self):
        document = self._document()
        windows = document["release_windows"]
        self.assertTrue(windows["estimate"])
        self.assertEqual(len(windows["windows"]), rb.MAX_RELEASE_WINDOWS)
        starts = [window["week_start"] for window in windows["windows"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(windows["next"], windows["windows"][0])
        for window in windows["windows"]:
            self.assertEqual(window["row_status"], "PROJECTED_NOT_REGISTERED")
            self.assertEqual(window["purpose"], "REGISTRATION_WINDOW")
            self.assertNotIn("registered_at", window)
            self.assertNotIn("forecast", window)

    def test_first_window_follows_the_cadence_model(self):
        document = self._document(as_of=date(2026, 11, 18))
        first = document["release_windows"]["windows"][0]
        self.assertEqual(first["week"], "2026-W48")
        self.assertEqual(first["week_start"], "2026-11-23")
        self.assertEqual(document["release_windows"]["days_to_next"], 7)
        self.assertFalse(document["release_windows"]["current_week_is_window"])

    def test_current_week_is_flagged_when_it_is_the_projected_window(self):
        document = self._document(as_of=date(2026, 11, 23))
        self.assertTrue(document["release_windows"]["current_week_is_window"])
        self.assertEqual(document["release_windows"]["days_to_next"], 0)
        self.assertEqual(document["release_windows"]["next"]["week"], "2026-W48")

    def test_projection_dates_match_the_calendar_model(self):
        document = self._document(as_of=date(2026, 11, 14))
        projection = document["projection"]
        # same cadence model and same anchor (the last registration) as docs/139
        self.assertEqual(projection["date_12th_resolved"], "2027-04-21")
        self.assertEqual(projection["date_4th_period"], "2027-02-01")
        self.assertEqual(projection["gate_date"], "2027-04-21")
        self.assertEqual(projection["weeks_to_12th"], 23)
        self.assertTrue(projection["estimate"])
        self.assertEqual(projection["status_estimated_gate_date"], "2027-05-07")
        self.assertLess(projection["shift_vs_status_days"], 0)
        self.assertIn("PROJECTION_SHIFT", alert_codes(document))

    def test_window_period_flag_marks_the_first_new_target_period(self):
        document = self._document(as_of=date(2026, 11, 18))
        windows = document["release_windows"]["windows"]
        self.assertFalse(windows[0]["adds_distinct_period"])  # 2026-11 is already registered
        self.assertTrue(any(window["adds_distinct_period"] for window in windows[1:]))
        first_new = next(window for window in windows[1:] if window["adds_distinct_period"])
        self.assertEqual(first_new["estimated_target_period"], "2026-12")

    def test_stale_journal_windows_are_renumbered_from_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = week_group("2026-08", "2026-08-07T09:00:00Z")
            status = status_document(rows_total=len(rows), rows_candidate=1)
            document = Fixture(tmp, rows=rows, status=status).build(
                as_of=date(2026, 11, 18), fact_lookup=facts({}))
            windows = document["release_windows"]["windows"]
            self.assertEqual([window["sequence"] for window in windows],
                             list(range(1, len(windows) + 1)))
            self.assertTrue(windows[0]["needed"])
            self.assertGreaterEqual(date.fromisoformat(windows[0]["week_end"]),
                                    date(2026, 11, 18))


# -------------------------------------------------------------------------------- registration gate
class RegistrationGate(unittest.TestCase):
    def test_without_human_confirmation_registration_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Fixture(tmp).build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            registration = document["registration"]
            self.assertFalse(registration["can_register"])
            self.assertFalse(registration["release_confirmed"])
            self.assertEqual([item["code"] for item in registration["cannot_register"]],
                             ["RELEASE_NOT_CONFIRMED"])
            self.assertEqual(registration["prospective_origin"], "2026-11")

    def test_confirmed_release_in_a_free_week_can_register(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Fixture(tmp).build(as_of=date(2026, 11, 23), fact_lookup=facts({}),
                                          release_confirmed=True)
            registration = document["registration"]
            self.assertTrue(registration["can_register"])
            self.assertEqual(registration["cannot_register"], [])
            self.assertEqual(registration["current_week"], "2026-W48")
            self.assertIn("CONTROL_ARMS_NOT_BOOKED",
                          {item["code"] for item in registration["warnings"]})
            steps = {step["code"]: step for step in document["checklist"]}
            self.assertEqual(steps["CONFIRM_RELEASE"]["status"], "DONE")
            self.assertEqual(steps["REGISTER_WEEK"]["status"], "TODO")

    def test_second_row_in_the_same_week_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = week_group("2026-11", "2026-11-18T09:00:00Z")
            status = status_document(rows_total=len(rows), rows_candidate=1)
            document = Fixture(tmp, rows=rows, status=status).build(
                as_of=date(2026, 11, 18), fact_lookup=facts({}), release_confirmed=True)
            codes = [item["code"] for item in document["registration"]["cannot_register"]]
            self.assertIn("WEEK_ALREADY_REGISTERED", codes)
            self.assertFalse(document["registration"]["can_register"])

    def test_published_fact_for_the_prospective_period_blocks_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            lookup = facts({("T1_CPI_YOY", "2026-11", 1):
                            {"primary": 2.4, "published": "2026-11-15"}})
            document = fixture.build(as_of=date(2026, 11, 20), fact_lookup=lookup,
                                     release_confirmed=True)
            codes = [item["code"] for item in document["registration"]["cannot_register"]]
            self.assertIn("FACT_ALREADY_PUBLISHED", codes)

    def test_status_alert_blocks_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = status_document(alerts=[{"code": "SUSPEND_LOSS", "severity": "ALERT",
                                              "message": "cumulative relative loss 1.2"}])
            document = Fixture(tmp, status=status).build(
                as_of=date(2026, 11, 23), fact_lookup=facts({}), release_confirmed=True)
            codes = [item["code"] for item in document["registration"]["cannot_register"]]
            self.assertIn("STATUS_SUSPEND_LOSS", codes)
            self.assertFalse(document["registration"]["can_register"])

    def test_rejected_row_of_the_week_blocks_a_new_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [row(registered_at="2026-11-18T09:00:00Z")]  # no control arms
            status = status_document(rows_total=1, rows_candidate=1, distinct=0)
            document = Fixture(tmp, rows=rows, status=status).build(
                as_of=date(2026, 11, 18), fact_lookup=facts({}), release_confirmed=True)
            codes = [item["code"] for item in document["registration"]["cannot_register"]]
            self.assertIn("WEEK_ROW_REJECTED", codes)
            self.assertNotIn("WEEK_ALREADY_REGISTERED", codes)
            self.assertFalse(document["registration"]["can_register"])


# ------------------------------------------------------------------------------------- checklist
class Checklist(unittest.TestCase):
    def test_checklist_has_the_nine_documented_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Fixture(tmp).build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            steps = document["checklist"]
            self.assertEqual([step["step"] for step in steps], list(range(1, 10)))
            self.assertEqual([step["code"] for step in steps],
                             ["OPEN_WEEK", "CONFIRM_RELEASE", "NO_RELEASE_NO_ROW", "BUILD_BATCH",
                              "REGISTER_WEEK", "VERIFY_REPORT", "VALIDATE_JOURNAL",
                              "MONITOR_TRACK", "RECORD_DECISION"])
            by_code = {step["code"]: step for step in steps}
            self.assertEqual(by_code["OPEN_WEEK"]["status"], "DONE")
            self.assertEqual(by_code["CONFIRM_RELEASE"]["status"], "NEEDS_HUMAN")
            self.assertEqual(by_code["BUILD_BATCH"]["status"], "BLOCKED")
            self.assertEqual(by_code["VALIDATE_JOURNAL"]["status"], "DONE")
            self.assertIn("tools/forward_forecast.py", by_code["BUILD_BATCH"]["command"])
            self.assertIn("tools/c6_calendar.py --weekly", by_code["REGISTER_WEEK"]["command"])
            self.assertIn("tools/shadow_track.py", by_code["MONITOR_TRACK"]["command"])

    def test_week_already_registered_marks_the_register_step_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = week_group("2026-11", "2026-11-18T09:00:00Z")
            status = status_document(rows_total=len(rows), rows_candidate=1)
            document = Fixture(tmp, rows=rows, status=status).build(
                as_of=date(2026, 11, 18), fact_lookup=facts({}))
            by_code = {step["code"]: step for step in document["checklist"]}
            self.assertEqual(by_code["REGISTER_WEEK"]["status"], "DONE")
            self.assertEqual(by_code["BUILD_BATCH"]["status"], "DONE")

    def test_week_check_in_the_audit_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = [{"schema": cal.AUDIT_SCHEMA, "event": "WEEK_CHECK", "at": "2026-11-18",
                      "week": "2026-W47", "candidate_rows": 0, "control_rows": 0,
                      "registered": False}]
            document = Fixture(tmp, audit_events=audit).build(as_of=date(2026, 11, 18),
                                                             fact_lookup=facts({}))
            missing = {item["code"] for item in document["missing"]}
            self.assertNotIn("WEEK_CHECK", missing)
            self.assertEqual(document["source"]["audit_events"], 1)
            self.assertNotIn("NO_WEEK_CHECK", alert_codes(document))


# ---------------------------------------------------------------------------------------- fail-closed
class FailClosed(unittest.TestCase):
    def test_missing_and_broken_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            status = write_status(tmp / "status.json", status_document())
            with self.assertRaises(rb.C6RunbookError):
                rb.build_runbook(journal=tmp / "missing.jsonl", status=status,
                                 as_of=date(2026, 11, 18), fact_lookup=facts({}))
            broken = tmp / "broken.jsonl"
            broken.write_text("{not json\n", encoding="utf-8")
            with self.assertRaises(rb.C6RunbookError):
                rb.build_runbook(journal=broken, status=status, as_of=date(2026, 11, 18),
                                 fact_lookup=facts({}))

    def test_invalid_journal_row_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad = row()
            del bad["code_commit"]
            journal = write_journal(tmp / "journal.jsonl", [bad])
            status = write_status(tmp / "status.json", status_document(rows_total=1))
            with self.assertRaises(rb.C6RunbookError):
                rb.build_runbook(journal=journal, status=status, as_of=date(2026, 11, 18),
                                 fact_lookup=facts({}))

    def test_missing_and_broken_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            journal = write_journal(tmp / "journal.jsonl", live_like_rows())
            with self.assertRaises(cal.C6CalendarError):
                rb.build_runbook(journal=journal, status=tmp / "missing.json",
                                 as_of=date(2026, 11, 18), fact_lookup=facts({}))
            broken = tmp / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(cal.C6CalendarError):
                rb.build_runbook(journal=journal, status=broken, as_of=date(2026, 11, 18),
                                 fact_lookup=facts({}))
            inconsistent = write_status(tmp / "bad.json",
                                        status_document(rows_candidate=12, rows_total=11))
            with self.assertRaises(cal.C6CalendarError):
                rb.build_runbook(journal=journal, status=inconsistent,
                                 as_of=date(2026, 11, 18), fact_lookup=facts({}))

    def test_rows_after_the_report_date_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            with self.assertRaises(rb.C6RunbookError) as caught:
                fixture.build(as_of=date(2026, 11, 10), fact_lookup=facts({}))
            self.assertIn("after the report date", str(caught.exception))

    def test_bad_cli_date_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = rb.main(["--journal", str(fixture.journal), "--status",
                                str(fixture.status), "--date", "not-a-date"])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(buffer.getvalue())["ok"])


# ----------------------------------------------------------------------------- --check validation
class CheckMode(unittest.TestCase):
    def test_clean_inputs_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Fixture(tmp).check(as_of=date(2026, 11, 18))
            self.assertTrue(report["ok"])
            self.assertEqual(report["problems"], [])
            self.assertEqual(report["schema"], rb.CHECK_SCHEMA)
            self.assertTrue(report["journal_valid"])
            self.assertTrue(report["status_valid"])
            self.assertEqual(report["journal_counts"]["rows_total"], 3)
            self.assertEqual(report["journal_counts"]["rows_candidate"], 1)

    def test_stale_status_counts_are_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = status_document(rows_total=99, rows_candidate=7, n_resolved=3, distinct=2)
            report = Fixture(tmp, status=status).check(as_of=date(2026, 11, 18))
            codes = {item["code"] for item in report["problems"]}
            self.assertFalse(report["ok"])
            self.assertIn("ROWS_TOTAL_MISMATCH", codes)
            self.assertIn("ROWS_CANDIDATE_MISMATCH", codes)
            self.assertIn("N_RESOLVED_MISMATCH", codes)
            self.assertIn("DISTINCT_MISMATCH", codes)

    def test_missing_inputs_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            report = rb.build_check(journal=tmp / "missing.jsonl", status=tmp / "missing.json",
                                    as_of=date(2026, 11, 18))
            self.assertFalse(report["ok"])
            codes = {item["code"] for item in report["problems"]}
            self.assertIn("JOURNAL_UNREADABLE", codes)
            self.assertIn("STATUS_UNREADABLE", codes)
            self.assertFalse(report["journal_valid"])
            self.assertFalse(report["status_valid"])

    def test_check_does_not_write_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            before = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()}
            fixture.check(as_of=date(2026, 11, 18))
            after = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()}
            self.assertEqual(before, after)


# --------------------------------------------------------------------- no mutation and determinism
class NoMutationAndDeterminism(unittest.TestCase):
    def test_build_does_not_touch_the_inputs_and_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            before = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()}
            fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            after = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(sorted(before), ["forecasts.jsonl", "shadow_status.json"])

    def test_documents_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            first = fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            second = fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_written_runbook_is_byte_identical_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            out = Path(tmp) / "out"
            first = fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            rb.write_runbook(first, out)
            payload = (out / rb.RUNBOOK_JSON).read_bytes()
            markdown = (out / rb.RUNBOOK_MD).read_bytes()
            second = fixture.build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            rb.write_runbook(second, out)
            self.assertEqual((out / rb.RUNBOOK_JSON).read_bytes(), payload)
            self.assertEqual((out / rb.RUNBOOK_MD).read_bytes(), markdown)
            self.assertEqual(json.loads(payload.decode("utf-8"))["as_of"], "2026-11-18")

    def test_markdown_has_the_runbook_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Fixture(tmp).build(as_of=date(2026, 11, 18), fact_lookup=facts({}))
            markdown = rb.render_markdown(document)
            for heading in ("Счётчики C6", "разрешённые и отклонённые", "Ближайшие релизные окна",
                            "Чего не хватает", "Нельзя регистрировать", "Чек-лист недели",
                            "Алерты", "Ограничения"):
                self.assertIn(heading, markdown)
            for window in document["release_windows"]["windows"]:
                self.assertIn(window["week"], markdown)
            self.assertIn("оценка", markdown.lower())


# ------------------------------------------------------------------------------------------- CLI
class Cli(unittest.TestCase):
    def _week8(self, tmp) -> Fixture:
        return Fixture(tmp)

    def test_json_output_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._week8(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = rb.main(["--journal", str(fixture.journal), "--status",
                                str(fixture.status), "--audit", str(fixture.audit),
                                "--date", "2026-11-18", "--json"])
            self.assertEqual(code, 0)
            document = json.loads(buffer.getvalue())
            self.assertEqual(document["schema"], rb.SCHEMA)
            self.assertEqual(document["c6"]["forecasts_line"], "0/12")
            self.assertEqual(len(document["source"]["journal_sha256"]), 64)

    def test_out_writes_only_the_runbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._week8(tmp)
            before = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()}
            out = fixture.tmp / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = rb.main(["--journal", str(fixture.journal), "--status",
                                str(fixture.status), "--audit", str(fixture.audit),
                                "--date", "2026-11-18", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(sorted(path.name for path in out.iterdir()),
                             [rb.RUNBOOK_JSON, rb.RUNBOOK_MD])
            after = {path.name: path.read_bytes() for path in fixture.tmp.iterdir()
                     if path.name != "out"}
            self.assertEqual(before, after)

    def test_confirm_release_changes_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._week8(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rb.main(["--journal", str(fixture.journal), "--status", str(fixture.status),
                         "--audit", str(fixture.audit), "--date", "2026-11-23",
                         "--confirm-release", "--json"])
            document = json.loads(buffer.getvalue())
            self.assertTrue(document["registration"]["can_register"])

    def test_check_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._week8(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = rb.main(["--journal", str(fixture.journal), "--status",
                                str(fixture.status), "--audit", str(fixture.audit),
                                "--date", "2026-11-18", "--check", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["ok"], True)
            broken = write_status(fixture.tmp / "broken_status.json",
                                  status_document(rows_total=42))
            with contextlib.redirect_stdout(io.StringIO()):
                code = rb.main(["--journal", str(fixture.journal), "--status", str(broken),
                                "--audit", str(fixture.audit), "--date", "2026-11-18", "--check"])
            self.assertEqual(code, 1)

    def test_bad_paths_exit_one_with_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = rb.main(["--journal", str(tmp / "nope.jsonl"), "--status",
                                str(tmp / "nope.json"), "--date", "2026-11-18", "--json"])
            self.assertEqual(code, 1)
            error = json.loads(buffer.getvalue())
            self.assertFalse(error["ok"])
            self.assertIn("journal", error["error"])

    def test_human_summary_mentions_the_week_and_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._week8(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = rb.main(["--journal", str(fixture.journal), "--status",
                                str(fixture.status), "--audit", str(fixture.audit),
                                "--date", "2026-11-18"])
            self.assertEqual(code, 0)
            text = buffer.getvalue()
            self.assertIn("C6 runbook", text)
            self.assertIn("2026-W47", text)
            self.assertIn("RELEASE_NOT_CONFIRMED", text)
            self.assertIn("не хватает", text)


class LiveSmoke(unittest.TestCase):
    def test_live_status_document_is_readable(self):
        status = ROOT / "build" / "shadow" / "shadow_status.json"
        journal = ROOT / "registry" / "forecasts.jsonl"
        if not (status.is_file() and journal.is_file()):
            self.skipTest("live shadow status / journal not built")
        document = rb.build_runbook(journal=journal, status=status,
                                    audit=ROOT / "build" / "c6" / "c6_audit.jsonl")
        self.assertEqual(document["schema"], rb.SCHEMA)
        self.assertIn(document["c6"]["status"], ("STARTING", "IN_PROGRESS", "COUNTS_MET"))
        self.assertEqual(len(document["release_windows"]["windows"]), rb.MAX_RELEASE_WINDOWS)
        self.assertFalse(document["registration"]["can_register"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
