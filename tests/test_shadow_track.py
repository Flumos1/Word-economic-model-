"""Shadow-track monitor (tools/shadow_track.py): schema, alerts, thresholds, determinism, fail-closed."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))
import shadow_track  # noqa: E402
import registry_resolve  # noqa: E402

QUANTILES = [2.5 + 0.05 * i for i in range(19)]
V4_REPORT = ROOT / "protocols" / "regime_v4_candidate_evaluation_report.json"
JOURNAL = ROOT / "registry" / "forecasts.jsonl"


def journal_row(candidate_id, origin, point, *, control=False, week=None, quantiles=None):
    row = {
        "candidate_id": candidate_id,
        "code_commit": "1234567",
        "data_vintage_hash": "a" * 64,
        "target": "T1_CPI_YOY",
        "horizon": 1,
        "origin": origin,
        "scoring_rule_commit": "b" * 64,
        "registered_at": "2026-11-14T09:00:00Z",
        "point": float(point),
        "quantiles_5_to_95_step_5": [float(q) for q in (quantiles or QUANTILES)],
        "notes": "test row",
    }
    if control:
        row["control_arm"] = True
    if week:
        row["vintage_week"] = week
    return row


class ShadowTrack(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "protocols").mkdir()
        shutil.copy(V4_REPORT, self.tmp / "protocols" / V4_REPORT.name)
        (self.tmp / "registry").mkdir()
        shutil.copy(JOURNAL, self.tmp / "registry" / "forecasts.jsonl")
        self._actual = registry_resolve.default_actual

    def tearDown(self):
        registry_resolve.default_actual = self._actual
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_rows(self, rows):
        with (self.tmp / "registry" / "forecasts.jsonl").open("w", encoding="utf-8",
                                                              newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")) + "\n")

    def _resolved_rows(self, n=6, candidate_point=3.00, comparator_point=3.00,
                       candidate_quantiles=None):
        rows = []
        for index in range(n):
            origin = f"2020-{index + 1:02d}"
            rows.append(journal_row("ENSEMBLE_REGIME_V4", origin, candidate_point,
                                    week=f"2020-W{index + 1:02d}",
                                    quantiles=candidate_quantiles))
            rows.append(journal_row("RW", origin, comparator_point, control=True,
                                    week=f"2020-W{index + 1:02d}"))
            rows.append(journal_row("EQUAL_WEIGHT_POINT", origin, comparator_point, control=True,
                                    week=f"2020-W{index + 1:02d}"))
            rows.append(journal_row("EQUAL_WEIGHT_DENSITY", origin, comparator_point, control=True,
                                    week=f"2020-W{index + 1:02d}"))
        self._write_rows(rows)

    def test_schema_and_starting_status(self):
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14),
                                             generated_at="2026-11-14T09:30:00Z")
        self.assertEqual(document["schema"], "AMFE-SHADOW-TRACK/1.0.0")
        self.assertEqual(document["candidate"]["id"], "ENSEMBLE_REGIME_V4")
        self.assertEqual(document["candidate"]["status"], "SHADOW_TEST")
        self.assertTrue(document["candidate"]["conditional_data_class"])
        self.assertEqual(document["c6"]["n_resolved"], 0)
        self.assertEqual(document["status"], "STARTING")
        self.assertEqual(document["alerts"], [])
        self.assertEqual(document["journal"]["rows_candidate"], 1)
        self.assertEqual(document["journal"]["days_since_last_row"], 0)

    def test_on_track_when_losses_are_better(self):
        self._resolved_rows(n=6, candidate_point=3.00, comparator_point=3.02, candidate_quantiles=None)
        registry_resolve.default_actual = lambda target, origin, horizon: 3.00
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        self.assertEqual(document["c6"]["n_resolved"], 6)
        self.assertEqual(document["status"], "ON_TRACK")

    def test_warn_on_loss_above_one(self):
        self._resolved_rows(n=6, candidate_point=3.05, comparator_point=3.00)
        registry_resolve.default_actual = lambda target, origin, horizon: 3.00
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        codes = {alert["code"]: alert["severity"] for alert in document["alerts"]}
        self.assertEqual(codes.get("WARN_LOSS"), "WARN")
        self.assertEqual(document["status"], "WARN")

    def test_alert_on_loss_above_suspend_threshold(self):
        self._resolved_rows(n=6, candidate_point=3.50, comparator_point=3.00)
        registry_resolve.default_actual = lambda target, origin, horizon: 3.00
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        codes = {alert["code"]: alert["severity"] for alert in document["alerts"]}
        self.assertEqual(codes.get("SUSPEND_LOSS"), "ALERT")
        self.assertEqual(document["status"], "ALERT")

    def test_cadence_alert(self):
        registry_resolve.default_actual = lambda target, origin, horizon: None
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2027, 1, 15))
        self.assertGreater(document["journal"]["days_since_last_row"], 35)
        self.assertEqual(document["status"], "ALERT")
        self.assertIn("SUSPEND_CADENCE", [alert["code"] for alert in document["alerts"]])

    def test_coverage_alert_after_gate_size(self):
        self._resolved_rows(n=12, candidate_point=3.00, comparator_point=3.00,
                            candidate_quantiles=[1.0] * 19)
        registry_resolve.default_actual = lambda target, origin, horizon: 3.5
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        self.assertFalse(document["c6"]["coverage_met"])
        self.assertIn("SUSPEND_COVERAGE", [alert["code"] for alert in document["alerts"]])

    def test_projection(self):
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        projection = document["projection"]
        self.assertEqual(projection["rows_remaining"], 12)
        self.assertEqual(projection["months_to_min_forecasts"], 4.8)
        self.assertEqual(projection["estimated_gate_date"], "2027-05-07")

    def test_markdown_blocks(self):
        document = shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1,
                                             today=date(2026, 11, 14))
        markdown = shadow_track.render_markdown(document)
        for heading in ("# AMFE — теневой трек", "## 1. Журнал", "## 2. Прогресс к гейту C6",
                        "## 3. Алерты", "## 4. Прогноз календаря", "## 5. Что это значит"):
            self.assertIn(heading, markdown)
        self.assertIn("SHADOW_TEST", markdown)
        self.assertIn("STARTING", markdown)

    def test_fail_closed_without_journal(self):
        (self.tmp / "registry" / "forecasts.jsonl").unlink()
        with self.assertRaises(shadow_track.ShadowTrackError):
            shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1)

    def test_fail_closed_without_report(self):
        (self.tmp / "protocols" / V4_REPORT.name).unlink()
        with self.assertRaises(shadow_track.ShadowTrackError):
            shadow_track.build_status(self.tmp, "ENSEMBLE_REGIME_V4", "T1_CPI_YOY", 1)

    def test_cli_writes_files_and_is_deterministic(self):
        out = self.tmp / "build" / "shadow"
        code = shadow_track.main(["--root", str(self.tmp), "--date", "2026-11-14",
                                  "--generated-at", "2026-11-14T09:30:00Z",
                                  "--out-dir", str(out)])
        self.assertEqual(code, 0)
        first = (out / "shadow_status.json").read_text(encoding="utf-8")
        shadow_track.main(["--root", str(self.tmp), "--date", "2026-11-14",
                           "--generated-at", "2026-11-14T09:30:00Z", "--out-dir", str(out)])
        self.assertEqual((out / "shadow_status.json").read_text(encoding="utf-8"), first)
        self.assertTrue((out / "2026-11-14.md").exists())
        self.assertTrue((out / "2026-11-14.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
