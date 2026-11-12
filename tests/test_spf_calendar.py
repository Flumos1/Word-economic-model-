"""SPF news-release calendar: availability must follow the official release dates (docs/86)."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from amfe.ensemble import spf_calendar as cal  # noqa: E402


class SpfCalendar(unittest.TestCase):
    def test_official_anchors(self):
        self.assertEqual(cal.release_date("1990Q3"), date(1990, 8, 31))
        self.assertEqual(cal.release_date("2019Q1"), date(2019, 3, 22))
        self.assertEqual(cal.release_date("2020Q1"), date(2020, 2, 14))
        self.assertEqual(cal.release_date("2005Q1"), date(2005, 2, 14))

    def test_no_look_ahead_at_the_old_14_day_rule_dates(self):
        # the old "middle month + 14 days" rule would have marked these origins available
        self.assertFalse(cal.available_at(date(1990, 8, 15), "1990Q3"))
        self.assertFalse(cal.available_at(date(1990, 8, 30), "1990Q3"))
        self.assertTrue(cal.available_at(date(1990, 8, 31), "1990Q3"))
        self.assertFalse(cal.available_at(date(2019, 3, 15), "2019Q1"))
        self.assertTrue(cal.available_at(date(2019, 3, 22), "2019Q1"))

    def test_deadline_is_not_availability(self):
        self.assertFalse(cal.available_at(date(2020, 2, 11), "2020Q1"))
        self.assertTrue(cal.available_at(date(2020, 2, 14), "2020Q1"))

    def test_pre_1990q3_is_not_real_time(self):
        self.assertFalse(cal.is_real_time_quarter("1990Q2"))
        self.assertTrue(cal.is_real_time_quarter("1990Q3"))
        self.assertTrue(cal.is_real_time_quarter("2026Q3"))

    def test_fallback_for_unanchored_quarter_is_the_middle_month_end(self):
        self.assertEqual(cal.release_date("2018Q3"), date(2018, 8, 31))
        self.assertFalse(cal.available_at(date(2018, 8, 15), "2018Q3"))
        self.assertTrue(cal.available_at(date(2018, 8, 31), "2018Q3"))

    def test_anchor_ordering_and_quarter_parsing(self):
        self.assertEqual(cal.parse_quarter("2026Q1"), (2026, 1))
        with self.assertRaises(ValueError):
            cal.parse_quarter("2026-1")

    def test_spf_source_uses_the_release_calendar(self):
        import amfe.ensemble.sources as sources
        self.assertFalse(hasattr(sources, "SPF_MIN_LAG_DAYS"),
                         "the old fixed lag constant must be gone")
        self.assertTrue(hasattr(sources, "spf_release_date") or "release" in repr(sources))


if __name__ == "__main__":
    unittest.main(verbosity=2)
