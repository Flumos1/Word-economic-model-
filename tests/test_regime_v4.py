"""ENSEMBLE_REGIME_V4: fourth pre-registered risk-off candidate (docs/118).

Checks: identity/registration hash, the two-leg risk-off flag (Sahm-like gap AND 12-month TB3MS
mean), the coherent location/shape composition (point = BVAR point, inverse-MSE quantiles shifted by
the point differential, monotonicity), the compose branches, and determinism of the composition.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.ensemble import regime_v3, regime_v4  # noqa: E402

REGISTRATION = ROOT / "protocols" / "candidates_2026-11-14_regime_v4.json"
CODE = ROOT / "src" / "amfe" / "ensemble" / "regime_v4.py"


def lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _member(point, quantiles=None, data_class="REAL_TIME", vintage_date="2026-01-01"):
    return {
        "point": float(point),
        "quantiles": [float(v) for v in (quantiles if quantiles is not None
                                         else [point + 0.1 * i for i in range(19)])],
        "data_class": data_class,
        "vintage_date": vintage_date,
    }


class _FakeSeries:
    def __init__(self, values):
        self.values = [float(v) for v in values]


def _patch_series(test, unrate, tb3ms):
    """Monkeypatch the module-level data accessor with fixed origin series."""
    def fake(name, origin, mode="auto", data_root=None):
        if name == "UNRATE":
            return _FakeSeries(unrate), None
        if name == "TB3MS":
            return _FakeSeries(tb3ms), None
        raise AssertionError(f"unexpected series {name}")

    original = regime_v4.available_series
    regime_v4.available_series = fake
    test.addCleanup(setattr, regime_v4, "available_series", original)


def _sahm_series(gap=0.9):
    """12 UNRATE values with a 12m low of 4.0 and a 3m mean of 4.0 + gap."""
    base = [4.0] + [4.0 + (gap if i >= 9 else 0.0) for i in range(11)]
    return base


class RegimeV4(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(regime_v4.CANDIDATE_ID, "ENSEMBLE_REGIME_V4")
        self.assertEqual(regime_v4.SAHM_THRESHOLD, 0.5)
        self.assertEqual(regime_v4.RATE_SERIES, "TB3MS")
        self.assertEqual(regime_v4.RATE_WINDOW_MONTHS, 12)
        self.assertEqual(regime_v4.RATE_THRESHOLD, 4.0)
        self.assertEqual(regime_v4.RISK_OFF_MEMBER, "BVAR_MINNESOTA")
        self.assertEqual(regime_v4.RISK_ON, "ENSEMBLE_INVERSE_MSE")
        self.assertEqual(regime_v4.TARGET, "T1_CPI_YOY")
        self.assertEqual(
            regime_v4.SELECTED_RISK_OFF,
            "BVAR_MINNESOTA_LOCATION+ENSEMBLE_INVERSE_MSE_SHAPE")
        # the composition is the v3 rule re-exported, not a re-implementation
        self.assertIs(regime_v4.combine_risk_off, regime_v3.combine_risk_off)

    def test_registration_matches_code(self):
        doc = json.loads(REGISTRATION.read_text(encoding="utf-8"))
        entry = next(c for c in doc["candidates"] if c["id"] == "ENSEMBLE_REGIME_V4")
        self.assertEqual(entry["code_file"], "src/amfe/ensemble/regime_v4.py")
        self.assertEqual(entry["code_sha256"], lf_sha256(CODE))
        self.assertTrue(doc["registered_before_evaluation"])
        self.assertTrue(entry["registered_before_evaluation"])
        self.assertIn("design_disclosure", entry)
        disclosure = entry["design_disclosure"].lower()
        self.assertIn("post-diagnostic", disclosure)
        self.assertIn("not a re-score", disclosure)
        self.assertIn("12-month mean of tb3ms", entry["flag"]["rate_rule"].lower())
        self.assertIn(entry["code_sha256"], REGISTRATION.read_text(encoding="utf-8"))

    def test_flag_high_rate_recession_is_risk_off(self):
        # 1991-01: Sahm gap 0.7 and 12m TB3MS mean 7.49 -> risk-off
        info = regime_v4.defensive_flag("1991-01-31")
        self.assertTrue(info["sahm_flag"])
        self.assertTrue(info["rate_condition"])
        self.assertTrue(info["defensive"])
        self.assertGreaterEqual(info["sahm_gap"], regime_v4.SAHM_THRESHOLD)
        self.assertGreaterEqual(info["tb3ms_12m_mean"], regime_v4.RATE_THRESHOLD)
        # 2001-09: high-rate recession -> risk-off (12m mean 4.70)
        info = regime_v4.defensive_flag("2001-09-30")
        self.assertTrue(info["defensive"])
        self.assertGreaterEqual(info["tb3ms_12m_mean"], regime_v4.RATE_THRESHOLD)

    def test_flag_zero_bound_recession_is_risk_on(self):
        # 2008-10: Sahm gap 1.07 but the 12m TB3MS mean is 2.37 -> no risk-off
        info = regime_v4.defensive_flag("2008-10-31")
        self.assertGreaterEqual(info["sahm_gap"], regime_v4.SAHM_THRESHOLD)
        self.assertFalse(info["rate_condition"])
        self.assertFalse(info["defensive"])

    def test_flag_quiet_expansion_is_risk_on(self):
        self.assertFalse(regime_v4.defensive_flag("1990-08-31")["defensive"])
        self.assertFalse(regime_v4.defensive_flag("2019-06-30")["defensive"])

    def test_flag_is_the_conjunction_of_both_legs(self):
        # Sahm fires, but rates are low -> risk-on
        _patch_series(self, _sahm_series(gap=0.9), [3.0] * 12)
        info = regime_v4.defensive_flag("2008-10-31")
        self.assertTrue(info["sahm_flag"])
        self.assertFalse(info["rate_condition"])
        self.assertFalse(info["defensive"])
        # rates are high, but Sahm does not fire -> risk-on
        _patch_series(self, [4.0] * 12, [5.0] * 12)
        info = regime_v4.defensive_flag("2001-09-30")
        self.assertFalse(info["sahm_flag"])
        self.assertTrue(info["rate_condition"])
        self.assertFalse(info["defensive"])
        # both legs -> risk-off
        _patch_series(self, _sahm_series(gap=0.9), [5.0] * 12)
        self.assertTrue(regime_v4.defensive_flag("1991-01-31")["defensive"])

    def test_flag_uses_the_twelve_month_mean(self):
        # 11 months at 8.0 and one month at 0.0 -> mean 7.333 (>= 4.0); last-3 rule would differ
        _patch_series(self, _sahm_series(gap=0.9), [8.0] * 11 + [0.0])
        info = regime_v4.defensive_flag("1991-01-31")
        self.assertAlmostEqual(info["tb3ms_12m_mean"], 11 * 8.0 / 12.0)
        self.assertTrue(info["rate_condition"])
        # short history cannot satisfy the 12-month window -> risk-on
        _patch_series(self, _sahm_series(gap=0.9), [9.0] * 6)
        info = regime_v4.defensive_flag("1991-01-31")
        self.assertIsNone(info["tb3ms_12m_mean"])
        self.assertFalse(info["defensive"])

    def test_combine_location_and_shape(self):
        bvar = _member(3.0)
        shape = _member(2.5, quantiles=[2.5 + 0.05 * i for i in range(19)])
        out = regime_v4.combine_risk_off(bvar, shape)
        shift = 3.0 - 2.5
        self.assertAlmostEqual(out["point"], 3.0)  # point = BVAR
        self.assertEqual(len(out["quantiles"]), 19)
        for i, q in enumerate(out["quantiles"]):
            self.assertAlmostEqual(q, 2.5 + 0.05 * i + shift)  # inverse shape + delta
        # translation preserves the shape: consecutive spacings are unchanged
        spacings = [b - a for a, b in zip(out["quantiles"], out["quantiles"][1:])]
        self.assertTrue(all(abs(s - 0.05) < 1e-12 for s in spacings))
        # monotonic non-decreasing
        self.assertTrue(all(out["quantiles"][i] <= out["quantiles"][i + 1]
                            for i in range(18)))

    def test_combine_rejects_non_monotone_shape(self):
        bad = _member(2.5, quantiles=[2.5 + (0.1 * i if i < 10 else 0.1 * (19 - i))
                                      for i in range(19)])
        with self.assertRaises(ValueError):
            regime_v4.combine_risk_off(_member(3.0), bad)

    def test_compose_risk_on_is_inverse_only(self):
        calls = []

        def predict(kind, name, thunk):
            calls.append((kind, name))
            if name == regime_v4.RISK_ON:
                return _member(1.5)
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v4.defensive_flag
        try:
            regime_v4.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.1, "sahm_flag": False, "tb3ms_12m_mean": 2.0,
                "rate_condition": False, "defensive": False}
            result = regime_v4.compose(predict)("2019-06-30", 1, None, "auto")
        finally:
            regime_v4.defensive_flag = original
        self.assertEqual(calls, [("candidate", regime_v4.RISK_ON)])
        self.assertAlmostEqual(result["point"], 1.5)
        self.assertEqual(result["regime"]["selected"], regime_v4.RISK_ON)
        self.assertFalse(result["regime"]["defensive"])

    def test_compose_risk_off_uses_bvar_point_and_inverse_shape(self):
        calls = []
        bvar = _member(4.0)
        shape = _member(2.0, quantiles=[2.0 + 0.1 * i for i in range(19)])

        def predict(kind, name, thunk):
            calls.append((kind, name))
            if (kind, name) == ("model", regime_v4.RISK_OFF_MEMBER):
                return bvar
            if (kind, name) == ("candidate", regime_v4.RISK_ON):
                return shape
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v4.defensive_flag
        try:
            regime_v4.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.9, "sahm_flag": True, "tb3ms_12m_mean": 7.5,
                "rate_condition": True, "defensive": True}
            result = regime_v4.compose(predict)("2001-06-30", 1, None, "auto")
        finally:
            regime_v4.defensive_flag = original
        self.assertIn(("model", regime_v4.RISK_OFF_MEMBER), calls)
        self.assertIn(("candidate", regime_v4.RISK_ON), calls)
        self.assertAlmostEqual(result["point"], 4.0)
        delta = 4.0 - 2.0
        for i, q in enumerate(result["quantiles"]):
            self.assertAlmostEqual(q, 2.0 + 0.1 * i + delta)
        self.assertEqual(result["regime"]["selected"], regime_v4.SELECTED_RISK_OFF)
        self.assertTrue(result["regime"]["defensive"])

    def test_determinism(self):
        bvar = _member(3.0)
        shape = _member(2.5, quantiles=[2.5 + 0.05 * i for i in range(19)])

        def predict(kind, name, thunk):
            if (kind, name) == ("model", regime_v4.RISK_OFF_MEMBER):
                return bvar
            if (kind, name) == ("candidate", regime_v4.RISK_ON):
                return shape
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v4.defensive_flag
        try:
            regime_v4.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.9, "sahm_flag": True, "tb3ms_12m_mean": 7.5,
                "rate_condition": True, "defensive": True}
            first = regime_v4.compose(predict)("2001-06-30", 1, None, "auto")
            second = regime_v4.compose(predict)("2001-06-30", 1, None, "auto")
        finally:
            regime_v4.defensive_flag = original
        self.assertEqual(first["point"], second["point"])
        self.assertEqual(first["quantiles"], second["quantiles"])
        self.assertEqual(first["regime"], second["regime"])
        self.assertEqual(regime_v4.combine_risk_off(bvar, shape),
                         regime_v4.combine_risk_off(bvar, shape))


if __name__ == "__main__":
    unittest.main(verbosity=2)
