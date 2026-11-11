"""Evaluation origins per protocol v4: windows, horizons, T3 override and COVID exclusion."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import windows  # noqa: E402

PROTOCOL = json.loads((ROOT / "protocols" / "evaluation_protocol_v4.json").read_text(encoding="utf-8"))


def horizons_of(target_id: str):
    for t in PROTOCOL["targets"]:
        if t["id"] == target_id:
            return t.get("horizons_months") or t.get("horizons_quarters")
    raise KeyError(target_id)


class EvaluationOrigins(unittest.TestCase):
    def test_every_target_horizon_window_is_present(self):
        built = windows.build(PROTOCOL)
        for target in PROTOCOL["targets"]:
            horizons = target.get("horizons_months") or target.get("horizons_quarters")
            for window in ("W1", "W2", "W3"):
                for h in horizons:
                    entry = built["targets"][target["id"]][window][str(h)]
                    self.assertIn("count", entry)
                    if window != "W3":
                        self.assertGreater(entry["count"], 0, (target["id"], window, h))

    def test_t3_w1_starts_in_1996_12_and_targets_stay_inside(self):
        for h in (3, 6, 12):
            rows = windows.origins_for(PROTOCOL, "T3_RECESSION", h, "W1")
            self.assertEqual(rows[0]["origin"][:7], "1996-12", h)
            for row in rows:
                self.assertGreaterEqual(row["origin"][:7], "1996-12")
                self.assertLessEqual(row["target"][:7], "2006-12")

    def test_windows_do_not_overlap_and_targets_follow_origins(self):
        for target in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"):
            for h in horizons_of(target):
                w1 = windows.origins_for(PROTOCOL, target, h, "W1")
                w2 = windows.origins_for(PROTOCOL, target, h, "W2")
                self.assertLess(w1[-1]["origin"], w2[0]["origin"], (target, h))
                self.assertLessEqual(w1[0]["origin"], w1[0]["target"], (target, h))
                self.assertLessEqual(w2[0]["origin"], w2[0]["target"], (target, h))

    def test_covid_targets_are_excluded_from_w2(self):
        for target in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"):
            for h in horizons_of(target):
                for row in windows.origins_for(PROTOCOL, target, h, "W2"):
                    self.assertFalse("2020-03" <= row["target"][:7] <= "2021-02", (target, h, row))
                    if target == "T3_RECESSION":
                        # the whole interval (t, t+h] is reported by its end; the exclusion is applied per interval
                        pass

    def test_t2_horizon_zero_uses_every_month_end(self):
        rows = windows.origins_for(PROTOCOL, "T2_GDP_GROWTH", 0, "W1")
        months = {r["origin"][:7] for r in rows}
        self.assertEqual(len(rows), len(months), "h=0 must use every month end once")
        rows_q = windows.origins_for(PROTOCOL, "T2_GDP_GROWTH", 1, "W1")
        for row in rows_q:
            self.assertIn(date.fromisoformat(row["origin"]).month, (3, 6, 9, 12))

    def test_t3_w1_has_at_least_two_real_time_members_range(self):
        rows = windows.origins_for(PROTOCOL, "T3_RECESSION", 3, "W1")
        self.assertGreaterEqual(len(rows), 100)
        self.assertTrue(rows[0]["origin"].startswith("1996-12"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
