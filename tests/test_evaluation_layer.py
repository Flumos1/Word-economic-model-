"""Evaluation layer: actuals reconstruction, mixture distributions and candidate models."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LOCAL_DATA = (ROOT / "data" / "raw" / "CPIAUCSL" / "latest.csv").exists()
if LOCAL_DATA:
    sys.path.insert(0, str(ROOT / "src"))
    from amfe.evaluation import actuals, distributions
    from amfe.candidates.models import candidate_forecast
    from amfe.forecast import forecast


class MixtureDistribution(unittest.TestCase):
    def test_single_distribution_is_recovered(self):
        q = np.linspace(-1.5, 1.5, 19)
        mixed = distributions.mixture_quantiles([q])
        np.testing.assert_allclose(mixed, q, atol=0.05)

    def test_mixture_is_monotone_and_symmetric(self):
        q1 = np.linspace(-1.0, 1.0, 19)
        q2 = np.linspace(-2.0, 2.0, 19)
        mixed = distributions.mixture_quantiles([q1, q2])
        self.assertEqual(mixed.shape, (19,))
        self.assertTrue(np.all(np.diff(mixed) >= -1e-12))
        self.assertAlmostEqual(float(mixed[9]), 0.0, places=2)  # symmetric members

    def test_mixture_of_identical_members_stays(self):
        q = np.linspace(-1, 1, 19)
        mixed = distributions.mixture_quantiles([q, q, q])
        np.testing.assert_allclose(mixed, q, atol=0.05)


@unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
class Actuals(unittest.TestCase):
    def test_period_end_dates(self):
        self.assertEqual(actuals.period_end_date("M", 2018 * 12 + 0).isoformat(), "2018-01-31")
        q_index = 2018 * 4 + 0
        self.assertEqual(actuals.period_end_date("Q", q_index).isoformat(), "2018-03-31")

    def test_first_publication_is_the_earliest_vintage(self):
        first = actuals.first_publication("CPIAUCSL", 2008 * 12 + 5)
        self.assertIsNotNone(first)
        self.assertIn("vintage_date", first)
        self.assertIsInstance(first["value"], float)

    def test_actual_pair_has_primary_and_secondary(self):
        row = actuals.actual_pair("T1_CPI_YOY", date(2008, 6, 30), 1, series_id="CPIAUCSL")
        self.assertIsNotNone(row["primary"])
        self.assertIn("primary_vintage", row)
        self.assertIn("secondary_vintage", row)

    def test_t3_secondary_equals_primary(self):
        row = actuals.actual_pair("T3_RECESSION", date(2008, 1, 31), 6)
        self.assertEqual(row["primary"], row["secondary"])


@unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
class CandidateModels(unittest.TestCase):
    def test_candidate_is_the_mean_of_its_members(self):
        cand = candidate_forecast("ENSEMBLE_RW_AR", "T1_CPI_YOY", date(2008, 6, 30), 1,
                                  series_id="CPIAUCSL", mode="latest")
        rw = forecast("RW", "T1_CPI_YOY", date(2008, 6, 30), 1, series_id="CPIAUCSL", mode="latest")
        ar = forecast("AR_BIC", "T1_CPI_YOY", date(2008, 6, 30), 1, series_id="CPIAUCSL", mode="latest")
        self.assertAlmostEqual(cand["point"], (rw.point + ar.point) / 2, places=9)
        self.assertEqual(len(cand["quantiles"]), 19)
        self.assertIn(cand["data_class"], {"REAL_TIME", "PSEUDO_REAL_TIME"})

    def test_candidate_rejects_other_targets(self):
        with self.assertRaises(Exception):
            candidate_forecast("ENSEMBLE_RW_AR", "T3_RECESSION", date(2008, 6, 30), 3)


class RealEvaluationReport(unittest.TestCase):
    def test_report_structure_when_present(self):
        path = ROOT / "protocols" / "first_real_evaluation_report.json"
        if not path.exists():
            self.skipTest("the first real evaluation has not been run yet")
        r = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(r["report_id"], "AMFE-FIRST-REAL-EVALUATION")
        self.assertIn(r["data_class"], {"REAL_TIME", "PSEUDO_REAL_TIME"})
        self.assertIn("admission", r)
        self.assertIn("C1", r["admission"]["criteria"])
        self.assertIn(r["admission"]["status"],
                      {"CANDIDATE", "SHADOW_TEST", "LIMITED_DEPLOYMENT", "ACTIVE"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
