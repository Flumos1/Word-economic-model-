"""Robustness of the T2/T1 predictive-distribution mixture (docs/94).

The incident: the T2 ensemble mixture produced q95 up to ~18 600 on 111 of 360 origins because one
member (the SPF-derived distribution) was built from raw GDP levels instead of the percent target.
The mixture quantile lies inside the member envelope, so a single broken member poisons the whole
density and CRPS became unusable (mean ~158, max ~894).

These tests pin the robust-mixture contract:
  * well-formed inputs give exactly the plain protocol mixture (no behaviour change);
  * a broken/off-scale member cannot blow the mixture up;
  * tails are bounded, quantiles stay monotone and the q95 > k*(q75-q25) guard holds;
  * real T1/T2 data in the local archive are unaffected (skipped when data/raw is absent).

Run: ``python -m unittest tests.test_mixture_robustness -v``.
"""
import math
import sys
import unittest
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.evaluation.distributions import (  # noqa: E402
    CONSENSUS_K,
    FINAL_SPREAD_K,
    INDEX_Q05,
    INDEX_Q50,
    INDEX_Q95,
    QUANTILE_LEVELS,
    ROBUST_ASSUMPTIONS,
    TAIL_SIGMA_K,
    WINSOR_K,
    _member_cdf,
    mixture_quantiles,
)

DATA_PRESENT = (ROOT / "data" / "raw" / "GDPC1" / "vintages.zip").exists()

Z = np.array([-1.6449, -1.2816, -1.0364, -0.8416, -0.6745, -0.5244, -0.3853, -0.2533, -0.1257,
              0.0, 0.1257, 0.2533, 0.3853, 0.5244, 0.6745, 0.8416, 1.0364, 1.2816, 1.6449])


def normal_member(center: float, spread: float) -> np.ndarray:
    return center + spread * Z


def historical_broken_spf() -> np.ndarray:
    """The 2026-11 T2 defect shape: an SPF density in raw GDP levels (q05 ~ 9 000, q95 ~ 18 600)."""
    return np.array([9000.0, 9500.0, 10000.0, 10300.0, 10600.0, 10900.0, 11200.0, 11400.0,
                     11500.0, 11600.0, 11700.0, 11800.0, 12000.0, 12300.0, 12700.0, 13300.0,
                     14200.0, 16000.0, 18600.0])


def normal_ensemble() -> list:
    return [normal_member(2.0, 1.0), normal_member(2.5, 1.2), normal_member(1.5, 0.9),
            normal_member(2.2, 1.1), normal_member(2.1, 1.0)]


class PlainProtocolMixture(unittest.TestCase):
    def test_well_formed_inputs_are_unchanged(self):
        members = normal_ensemble()
        robust = mixture_quantiles(members)
        plain = mixture_quantiles(members, robust=False)
        np.testing.assert_array_equal(robust, plain)
        self.assertFalse(np.any(np.diff(robust) < 0.0))

    def test_single_member_recovers_its_own_quantiles(self):
        vector = normal_member(3.0, 1.5)
        mixed = mixture_quantiles([vector])
        np.testing.assert_allclose(mixed, vector, atol=0.06)
        self.assertTrue(np.all(np.diff(mixed) >= -1e-12))

    def test_t1_regression_small_inflation_values(self):
        members = [normal_member(3.0, 0.7), normal_member(2.4, 0.6), normal_member(2.8, 0.8)]
        robust = mixture_quantiles(members)
        plain = mixture_quantiles(members, robust=False)
        np.testing.assert_array_equal(robust, plain)
        self.assertLess(float(robust[-1]), 10.0)   # CPI y/y percentage points stay bounded
        self.assertGreater(float(robust[0]), -10.0)

    def test_constants_and_assumptions_are_documented(self):
        for name in ("winsor_k", "consensus_k", "tail_sigma_k", "final_spread_k", "rule"):
            self.assertIn(name, ROBUST_ASSUMPTIONS)
        self.assertEqual(ROBUST_ASSUMPTIONS["winsor_k"], WINSOR_K)
        self.assertEqual(ROBUST_ASSUMPTIONS["consensus_k"], CONSENSUS_K)
        self.assertEqual(ROBUST_ASSUMPTIONS["tail_sigma_k"], TAIL_SIGMA_K)
        self.assertEqual(ROBUST_ASSUMPTIONS["final_spread_k"], FINAL_SPREAD_K)


class BrokenMember(unittest.TestCase):
    def test_historical_t2_defect_does_not_explode_the_mixture(self):
        members = normal_ensemble() + [historical_broken_spf()]
        plain = mixture_quantiles(members, robust=False)
        robust, diagnostics = mixture_quantiles(members, return_diagnostics=True)
        # the historical defect is reproduced by the plain path (q95 in the thousands)
        self.assertGreater(float(plain[-1]), 1000.0)
        # the robust path bounds it and reports the winsorized member
        self.assertLess(float(robust[-1]), 50.0)
        self.assertLess(float(robust[0]), 20.0)
        self.assertIn(5, diagnostics["winsorized_members"])
        self.assertTrue(np.all(np.diff(robust) >= -1e-12))
        self.assertTrue(np.all(np.isfinite(robust)))

    def test_off_scale_member_center_is_contained(self):
        members = normal_ensemble() + [historical_broken_spf()]
        robust = mixture_quantiles(members)
        # the mixture median must stay in the sane members' range, not at the level-unit 11 500
        self.assertLess(abs(float(robust[INDEX_Q50])), 20.0)
        self.assertLess(float(robust[INDEX_Q95]), 50.0)

    def test_tail_sigma_cap_bounds_gaussian_extrapolation(self):
        # A member whose q90..q95 gap is huge would produce a runaway Gaussian tail without the cap.
        flat_tail = np.linspace(0.0, 5.0, 19)
        flat_tail[17] = 5.0
        flat_tail[18] = 60.0
        x = np.array([500.0, 1000.0, 5000.0])
        uncapped = _member_cdf(flat_tail, x, tail_sigma_k=None)
        capped = _member_cdf(flat_tail, x, tail_sigma_k=TAIL_SIGMA_K)
        self.assertTrue(np.all(capped >= uncapped), (capped, uncapped))
        self.assertTrue(np.all(capped <= 1.0))
        self.assertLess(float(capped[-1]), 1.0 + 1e-12)
        # with the cap, a far-away point is already almost surely exceeded
        self.assertGreater(float(capped[-1]), 0.999)
        mixture = mixture_quantiles(normal_ensemble() + [flat_tail])
        self.assertLess(float(mixture[-1]), 100.0)

    def test_broken_member_with_large_spread_is_reported(self):
        members = normal_ensemble() + [historical_broken_spf()]
        _, diagnostics = mixture_quantiles(members, return_diagnostics=True)
        self.assertTrue(diagnostics["robust"])
        self.assertIn(5, diagnostics["winsorized_members"])
        self.assertIsNotNone(diagnostics["consensus_envelope"])
        low, high = diagnostics["consensus_envelope"]
        self.assertLess(high, 100.0)


class GuardsAndMonotonicity(unittest.TestCase):
    def test_random_adversarial_members_stay_monotone_and_finite(self):
        rng = np.random.default_rng(20261112)
        for _ in range(25):
            members = []
            for _ in range(rng.integers(1, 7)):
                base = rng.normal(0.0, 3.0)
                spread = float(np.exp(rng.uniform(-3.0, 6.0)))  # up to ~400x typical spread
                vector = base + spread * Z
                members.append(np.maximum.accumulate(vector))
            mixed = mixture_quantiles(members)
            self.assertTrue(np.all(np.isfinite(mixed)))
            self.assertTrue(np.all(np.diff(mixed) >= -1e-12))
            self.assertLessEqual(float(mixed[-1]), 1e6)  # no catastrophic blow-up

    def test_final_spread_guard_holds(self):
        # Bimodal, heavily separated members: the final guard keeps q95 within k * (q75 - q25).
        members = [normal_member(-40.0, 0.5), normal_member(40.0, 0.5),
                   normal_member(-40.0, 0.5), normal_member(40.0, 0.5)]
        mixed, diagnostics = mixture_quantiles(members, return_diagnostics=True)
        low, high = diagnostics["final_bounds"]
        self.assertGreaterEqual(float(mixed[0]), low - 1e-9)
        self.assertLessEqual(float(mixed[-1]), high + 1e-9)
        self.assertTrue(np.all(np.diff(mixed) >= -1e-12))

    def test_degenerate_member_is_handled(self):
        constant = np.full(len(QUANTILE_LEVELS), 2.0)
        mixed = mixture_quantiles([constant, normal_member(2.0, 1.0), normal_member(3.0, 0.5)])
        self.assertTrue(np.all(np.isfinite(mixed)))
        self.assertTrue(np.all(np.diff(mixed) >= -1e-12))
        auto_bound = mixture_quantiles([constant])
        self.assertTrue(np.all(np.isfinite(auto_bound)))

    def test_deterministic_output_and_diagnostics(self):
        members = normal_ensemble() + [historical_broken_spf()]
        first, first_diag = mixture_quantiles(members, return_diagnostics=True)
        second, second_diag = mixture_quantiles(members, return_diagnostics=True)
        np.testing.assert_array_equal(first, second)
        self.assertEqual(first_diag["winsorized_members"], second_diag["winsorized_members"])
        np.testing.assert_array_equal(first_diag["consensus_envelope"],
                                      second_diag["consensus_envelope"])
        self.assertIn("final_guard_triggered", first_diag)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            mixture_quantiles([])
        with self.assertRaises(ValueError):
            mixture_quantiles([np.zeros(18)])
        with self.assertRaises(ValueError):
            broken = normal_member(0.0, 1.0)
            broken[3] = np.nan
            mixture_quantiles([broken, normal_member(1.0, 1.0)])


@unittest.skipUnless(DATA_PRESENT, "data/raw is git-ignored and not downloaded")
class RealDataRegression(unittest.TestCase):
    """The real T2/T1 mixtures must be sane on the local dense vintage archive."""

    @staticmethod
    def _origins(window: str, target: str = "T2_GDP_GROWTH", horizon: int = 0) -> list:
        import json
        import windows
        protocol = json.loads((ROOT / "protocols" / "evaluation_protocol_v4.json")
                              .read_text(encoding="utf-8"))
        return windows.origins_for(protocol, target, horizon, window)

    def test_real_t2_mixture_has_no_extreme_tails(self):
        from datetime import date as day_type
        from amfe.ensemble import ensemble_forecast
        from amfe.forecast.data import month_end_of
        import json
        from amfe.evaluation.actuals import actual_pair
        import evaluate as harness

        origins = self._origins("W1") + self._origins("W2")
        # SPF deadline months are the historically affected origins (SPF member available there)
        sample = [row for row in origins if row["origin"][:7] in
                  ("1997-02", "2000-02", "2004-05", "2008-02", "2012-05", "2016-02")]
        self.assertGreaterEqual(len(sample), 4)
        for row in sample:
            origin = day_type.fromisoformat(row["origin"])
            forecast = ensemble_forecast("ENSEMBLE_EQUAL", "T2_GDP_GROWTH", origin, 0, mode="auto")
            quantiles = np.asarray(forecast["quantiles"], dtype=float)
            self.assertTrue(np.all(np.diff(quantiles) >= -1e-12), row["origin"])
            self.assertLess(float(quantiles[-1]), 50.0, row["origin"])
            self.assertGreater(float(quantiles[0]), -50.0, row["origin"])
            actual = actual_pair("T2_GDP_GROWTH", origin, 0)["primary"]
            crps = float(harness.crps(quantiles[None, :], np.array([actual]))[0])
            self.assertLess(crps, 50.0, (row["origin"], crps))

    def test_real_t2_robust_equals_plain_on_current_members(self):
        from amfe.ensemble import target_member_ids
        from amfe.ensemble.history import current_member_parts
        from amfe.forecast.data import month_end_of
        members = target_member_ids("T2_GDP_GROWTH", 0)
        for label in ("1997-02-28", "2004-05-31", "2012-05-31"):
            origin = month_end_of(label)
            parts, _excluded = current_member_parts("T2_GDP_GROWTH", origin, 0, members, mode="auto")
            vectors = [np.asarray(part["quantiles"], dtype=float)
                       for part in parts.values() if part.get("quantiles") is not None]
            if len(vectors) < 2:
                self.skipTest(f"fewer than two member distributions at {label}")
            robust = mixture_quantiles(vectors)
            plain = mixture_quantiles(vectors, robust=False)
            np.testing.assert_allclose(robust, plain, atol=1e-9, err_msg=label)
            self.assertLess(float(robust[-1]), 50.0)
            self.assertGreater(float(robust[0]), -50.0)

    def test_real_t1_regression_unchanged(self):
        from amfe.ensemble import ensemble_forecast
        from amfe.forecast.data import month_end_of
        for label in ("1997-02-28", "2004-05-31", "2012-05-31"):
            origin = month_end_of(label)
            forecast = ensemble_forecast("ENSEMBLE_EQUAL", "T1_CPI_YOY", origin, 1, mode="auto")
            quantiles = np.asarray(forecast["quantiles"], dtype=float)
            self.assertTrue(np.all(np.diff(quantiles) >= -1e-12))
            self.assertLess(float(quantiles[-1]), 100.0)
            self.assertGreater(float(quantiles[0]), -100.0)


class WeightedMixtureRobustness(unittest.TestCase):
    """docs/104: the weighted ensemble path uses the same robust helper as the equal-weight path."""

    @staticmethod
    def _plain_weighted_reference(vectors, weights, grid_size: int = 4001) -> np.ndarray:
        """Independent historical reference: raw grid, weighted CDFs, uncapped Gaussian tails."""
        vectors = [np.asarray(vector, dtype=float) for vector in vectors]
        values = np.asarray(weights, dtype=float)
        values = values / values.sum()
        low = min(float(vector[0]) for vector in vectors)
        high = max(float(vector[-1]) for vector in vectors)
        span = max(high - low, 1e-6)
        grid = np.linspace(low - 0.75 * span, high + 0.75 * span, grid_size)
        cdf = np.zeros_like(grid)
        for vector, weight in zip(vectors, values):
            cdf += weight * _member_cdf(vector, grid, tail_sigma_k=None)
        cdf = np.clip(cdf, 0.0, 1.0)
        out = np.array([float(np.interp(level, cdf, grid)) for level in QUANTILE_LEVELS])
        return np.maximum.accumulate(out)

    def _vectors(self):
        return normal_ensemble()

    def test_well_formed_weighted_matches_legacy_behaviour(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors()
        weights = [0.35, 0.25, 0.2, 0.12, 0.08]
        robust = weighted_mixture_quantiles(vectors, weights)
        legacy = self._plain_weighted_reference(vectors, weights)
        legacy_flagged = weighted_mixture_quantiles(vectors, weights, robust=False)
        np.testing.assert_allclose(robust, legacy, atol=1e-9, rtol=0.0)
        np.testing.assert_allclose(legacy_flagged, legacy, atol=1e-9, rtol=0.0)

    def test_uniform_weighted_matches_the_equal_weight_mixture(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors()
        uniform = np.full(len(vectors), 1.0 / len(vectors))
        weighted = weighted_mixture_quantiles(vectors, uniform)
        equal = mixture_quantiles(vectors)
        np.testing.assert_allclose(weighted, equal, atol=1e-12, rtol=0.0)
        # the public wrapper keeps the same delegation for equal weights
        from amfe.ensemble.mixture import mix_distributions
        np.testing.assert_allclose(mix_distributions(vectors), equal, atol=1e-12, rtol=0.0)

    def test_broken_member_does_not_explode_the_weighted_q95(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors() + [historical_broken_spf()]
        weights = [1.0 / len(vectors)] * len(vectors)
        legacy = self._plain_weighted_reference(vectors, weights)
        robust, diagnostics = weighted_mixture_quantiles(vectors, weights, return_diagnostics=True)
        self.assertGreater(float(legacy[-1]), 1000.0)          # the historical defect is reproduced
        self.assertLess(float(robust[-1]), 50.0)               # the weighted path is protected
        self.assertLess(abs(float(robust[INDEX_Q50])), 20.0)
        self.assertTrue(np.all(np.diff(robust) >= -1e-12))
        self.assertIn(5, diagnostics["winsorized_members"])
        self.assertEqual(diagnostics["method"], "WEIGHTED_MIXTURE_ROBUST")

    def test_dominant_broken_member_is_contained(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors() + [historical_broken_spf()]
        weights = [0.04, 0.04, 0.04, 0.04, 0.04, 0.8]
        robust, diagnostics = weighted_mixture_quantiles(vectors, weights, return_diagnostics=True)
        self.assertLess(float(robust[-1]), 100.0)
        self.assertTrue(np.all(np.isfinite(robust)))
        self.assertTrue(np.all(np.diff(robust) >= -1e-12))
        self.assertIn(5, diagnostics["winsorized_members"])
        # the mixture quantiles must stay inside the winsorized member envelope
        low, high = diagnostics["consensus_envelope"]
        self.assertGreaterEqual(float(robust[0]), low - 1e-9)
        self.assertLessEqual(float(robust[-1]), high + 1e-9)

    def test_weighted_guard_and_monotonicity_on_adversarial_inputs(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        rng = np.random.default_rng(20261113)
        for _ in range(20):
            n = int(rng.integers(2, 7))
            vectors = []
            for _ in range(n):
                center = rng.normal(0.0, 3.0)
                spread = float(np.exp(rng.uniform(-3.0, 6.0)))
                vectors.append(np.maximum.accumulate(center + spread * Z))
            weights = rng.uniform(0.0, 1.0, size=n)
            mixed, diagnostics = weighted_mixture_quantiles(vectors, weights, return_diagnostics=True)
            self.assertTrue(np.all(np.isfinite(mixed)))
            self.assertTrue(np.all(np.diff(mixed) >= -1e-12))
            low, high = diagnostics["final_bounds"]
            self.assertGreaterEqual(float(mixed[0]), low - 1e-9)
            self.assertLessEqual(float(mixed[-1]), high + 1e-9)

    def test_weighted_diagnostics_and_legacy_flag(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors() + [historical_broken_spf()]
        weights = [0.25, 0.25, 0.15, 0.15, 0.1, 0.1]
        robust, diagnostics = weighted_mixture_quantiles(vectors, weights, return_diagnostics=True)
        self.assertTrue(diagnostics["robust"])
        self.assertEqual(diagnostics["method"], "WEIGHTED_MIXTURE_ROBUST")
        self.assertEqual(len(diagnostics["weights"]), len(vectors))
        self.assertTrue(math.isclose(sum(diagnostics["weights"]), 1.0, abs_tol=1e-12))
        for key in ("winsorized_members", "consensus_envelope", "final_guard_triggered",
                    "final_bounds", "q05", "q50", "q95", "assumptions"):
            self.assertIn(key, diagnostics)
        self.assertEqual(diagnostics["assumptions"]["winsor_k"], WINSOR_K)
        legacy, legacy_diagnostics = weighted_mixture_quantiles(
            vectors, weights, robust=False, return_diagnostics=True)
        self.assertFalse(legacy_diagnostics["robust"])
        self.assertEqual(legacy_diagnostics["method"], "WEIGHTED_MIXTURE_PLAIN")
        self.assertEqual(legacy_diagnostics["winsorized_members"], [])
        self.assertGreater(float(legacy[-1]), 1000.0)
        self.assertLess(float(robust[-1]), 50.0)

    def test_weighted_tail_cap_bounds_a_flat_tail_member(self):
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        flat_tail = np.linspace(0.0, 5.0, 19)
        flat_tail[17] = 5.0
        flat_tail[18] = 60.0
        vectors = self._vectors() + [flat_tail]
        weights = [1.0 / len(vectors)] * len(vectors)
        legacy = self._plain_weighted_reference(vectors, weights)
        robust = weighted_mixture_quantiles(vectors, weights)
        self.assertGreater(float(legacy[-1]), float(robust[-1]))
        self.assertLess(float(robust[-1]), 100.0)

    def test_weighted_invalid_inputs_are_rejected(self):
        from amfe.ensemble import EnsembleError
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        vectors = self._vectors()
        with self.assertRaises(EnsembleError):
            weighted_mixture_quantiles(vectors, [0.5, 0.5])             # wrong count
        with self.assertRaises(EnsembleError):
            weighted_mixture_quantiles(vectors, [1.0, -0.5, 0.0, 0.0, 0.5])  # negative
        with self.assertRaises(EnsembleError):
            weighted_mixture_quantiles(vectors, [0.0, 0.0, 0.0, 0.0, 0.0])   # zero sum
        with self.assertRaises(EnsembleError):
            weighted_mixture_quantiles([np.zeros(18)], None)            # bad vector length

    @unittest.skipUnless(DATA_PRESENT, "data/raw is git-ignored and not downloaded")
    def test_real_weighted_members_are_sane_and_unchanged(self):
        from amfe.ensemble import target_member_ids
        from amfe.ensemble.history import current_member_parts
        from amfe.ensemble.mixture import weighted_mixture_quantiles
        from amfe.forecast.data import month_end_of

        members = target_member_ids("T2_GDP_GROWTH", 0)
        for label in ("1997-02-28", "2004-05-31", "2012-05-31"):
            origin = month_end_of(label)
            parts, _excluded = current_member_parts("T2_GDP_GROWTH", origin, 0, members, mode="auto")
            vectors = [np.asarray(part["quantiles"], dtype=float)
                       for part in parts.values() if part.get("quantiles") is not None]
            if len(vectors) < 2:
                self.skipTest(f"fewer than two member distributions at {label}")
            weights = np.linspace(1.0, 2.0, len(vectors))
            robust = weighted_mixture_quantiles(vectors, weights)
            legacy = weighted_mixture_quantiles(vectors, weights, robust=False)
            np.testing.assert_allclose(robust, legacy, atol=1e-9, rtol=0.0, err_msg=label)
            self.assertLess(float(robust[-1]), 50.0, label)
            self.assertGreater(float(robust[0]), -50.0, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
