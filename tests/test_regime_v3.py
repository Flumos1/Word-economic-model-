"""ENSEMBLE_REGIME_V3: third pre-registered risk-off candidate (docs/116).

Checks: identity/registration hash, Sahm flag, coherent location/shape composition
(point = BVAR point, quantiles = inverse-MSE quantiles shifted by the point differential,
monotonicity), and determinism of the cached composition.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.ensemble import regime_v3  # noqa: E402

REGISTRATION = ROOT / "protocols" / "candidates_2026-11-14_regime_v3.json"
CODE = ROOT / "src" / "amfe" / "ensemble" / "regime_v3.py"


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


class RegimeV3(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(regime_v3.CANDIDATE_ID, "ENSEMBLE_REGIME_V3")
        self.assertEqual(regime_v3.SAHM_THRESHOLD, 0.5)
        self.assertEqual(regime_v3.RISK_OFF_MEMBER, "BVAR_MINNESOTA")
        self.assertEqual(regime_v3.RISK_ON, "ENSEMBLE_INVERSE_MSE")
        self.assertEqual(regime_v3.TARGET, "T1_CPI_YOY")
        self.assertEqual(
            regime_v3.SELECTED_RISK_OFF,
            "BVAR_MINNESOTA_LOCATION+ENSEMBLE_INVERSE_MSE_SHAPE")

    def test_registration_matches_code(self):
        doc = json.loads(REGISTRATION.read_text(encoding="utf-8"))
        entry = next(c for c in doc["candidates"] if c["id"] == "ENSEMBLE_REGIME_V3")
        self.assertEqual(entry["code_file"], "src/amfe/ensemble/regime_v3.py")
        self.assertEqual(entry["code_sha256"], lf_sha256(CODE))
        self.assertTrue(doc["registered_before_evaluation"])
        self.assertTrue(entry["registered_before_evaluation"])
        self.assertIn("design_disclosure", entry)
        disclosure = entry["design_disclosure"].lower()
        self.assertIn("post-diagnostic", disclosure)
        self.assertIn("not a re-score", disclosure)
        self.assertIn(entry["code_sha256"], REGISTRATION.read_text(encoding="utf-8"))

    def test_flag(self):
        # 1990-08: unemployment gap is small -> risk-on (as in v2)
        self.assertFalse(regime_v3.defensive_flag("1990-08-31")["defensive"])
        # 1991-01: Sahm-like gap is above 0.5 -> risk-off
        info = regime_v3.defensive_flag("1991-01-31")
        self.assertTrue(info["sahm_flag"] if "sahm_flag" in info else info["defensive"])
        self.assertTrue(info["defensive"])
        self.assertGreaterEqual(info["sahm_gap"], regime_v3.SAHM_THRESHOLD)
        # 2019-06: quiet expansion -> risk-on
        self.assertFalse(regime_v3.defensive_flag("2019-06-30")["defensive"])

    def test_combine_location_and_shape(self):
        bvar = _member(3.0)
        shape = _member(2.5, quantiles=[2.5 + 0.05 * i for i in range(19)])
        out = regime_v3.combine_risk_off(bvar, shape)
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
        self.assertTrue(out["regime"] if "regime" in out else True)

    def test_combine_metadata(self):
        bvar = _member(3.0, data_class="REAL_TIME", vintage_date="2026-01-02")
        shape = _member(2.5, data_class="REAL_TIME", vintage_date="2026-01-01")
        out = regime_v3.combine_risk_off(bvar, shape)
        self.assertEqual(out["data_class"], "REAL_TIME")
        self.assertEqual(out["vintage_date"], "2026-01-02")
        self.assertIsNone(out["probability"])

    def test_combine_rejects_non_monotone_shape(self):
        bad = _member(2.5, quantiles=[2.5 + (0.1 * i if i < 10 else 0.1 * (19 - i))
                                      for i in range(19)])
        with self.assertRaises(ValueError):
            regime_v3.combine_risk_off(_member(3.0), bad)

    def test_compose_risk_on_is_inverse_only(self):
        calls = []

        def predict(kind, name, thunk):
            calls.append((kind, name))
            if name == regime_v3.RISK_ON:
                return _member(1.5)
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v3.defensive_flag
        try:
            regime_v3.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.1, "defensive": False}
            result = regime_v3.compose(predict)("2019-06-30", 1, None, "auto")
        finally:
            regime_v3.defensive_flag = original
        self.assertEqual(calls, [("candidate", regime_v3.RISK_ON)])
        self.assertAlmostEqual(result["point"], 1.5)
        self.assertEqual(result["regime"]["selected"], regime_v3.RISK_ON)
        self.assertFalse(result["regime"]["defensive"])

    def test_compose_risk_off_uses_bvar_point_and_inverse_shape(self):
        calls = []
        bvar = _member(4.0)
        shape = _member(2.0, quantiles=[2.0 + 0.1 * i for i in range(19)])

        def predict(kind, name, thunk):
            calls.append((kind, name))
            if (kind, name) == ("model", regime_v3.RISK_OFF_MEMBER):
                return bvar
            if (kind, name) == ("candidate", regime_v3.RISK_ON):
                return shape
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v3.defensive_flag
        try:
            regime_v3.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.9, "defensive": True}
            result = regime_v3.compose(predict)("2001-06-30", 1, None, "auto")
        finally:
            regime_v3.defensive_flag = original
        self.assertIn(("model", regime_v3.RISK_OFF_MEMBER), calls)
        self.assertIn(("candidate", regime_v3.RISK_ON), calls)
        self.assertAlmostEqual(result["point"], 4.0)
        delta = 4.0 - 2.0
        for i, q in enumerate(result["quantiles"]):
            self.assertAlmostEqual(q, 2.0 + 0.1 * i + delta)
        self.assertEqual(result["regime"]["selected"], regime_v3.SELECTED_RISK_OFF)
        self.assertTrue(result["regime"]["defensive"])

    def test_determinism(self):
        bvar = _member(3.0)
        shape = _member(2.5, quantiles=[2.5 + 0.05 * i for i in range(19)])

        def predict(kind, name, thunk):
            if (kind, name) == ("model", regime_v3.RISK_OFF_MEMBER):
                return bvar
            if (kind, name) == ("candidate", regime_v3.RISK_ON):
                return shape
            raise AssertionError(f"unexpected branch {kind}/{name}")

        original = regime_v3.defensive_flag
        try:
            regime_v3.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.9, "defensive": True}
            first = regime_v3.compose(predict)("2001-06-30", 1, None, "auto")
            second = regime_v3.compose(predict)("2001-06-30", 1, None, "auto")
        finally:
            regime_v3.defensive_flag = original
        self.assertEqual(first["point"], second["point"])
        self.assertEqual(first["quantiles"], second["quantiles"])
        self.assertEqual(first["regime"], second["regime"])
        self.assertEqual(regime_v3.combine_risk_off(bvar, shape),
                         regime_v3.combine_risk_off(bvar, shape))


if __name__ == "__main__":
    unittest.main(verbosity=2)
