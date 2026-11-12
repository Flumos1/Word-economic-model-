"""ENSEMBLE_REGIME: pre-registered risk-off candidate (docs/112, registration 2026-11-12)."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.ensemble import regime  # noqa: E402


def lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class RegimeCandidate(unittest.TestCase):
    def test_identity_and_thresholds(self):
        self.assertEqual(regime.CANDIDATE_ID, "ENSEMBLE_REGIME")
        self.assertIn(regime.RISK_OFF_MEMBER, ("BVAR_MINNESOTA", "RW", "AO", "AR_BIC"))
        self.assertEqual(regime.SAHM_THRESHOLD, 0.5)
        self.assertEqual(regime.RISK_ON, "ENSEMBLE_INVERSE_MSE")

    def test_registration_matches_code(self):
        doc = json.loads((ROOT / "protocols" / "candidates_2026-11-12.json").read_text(encoding="utf-8"))
        entry = next(c for c in doc["candidates"] if c["id"] == "ENSEMBLE_REGIME")
        self.assertEqual(entry["code_file"], "src/amfe/ensemble/regime.py")
        self.assertEqual(entry["code_sha256"], lf_sha256(ROOT / "src" / "amfe" / "ensemble" / "regime.py"))
        self.assertTrue(doc["registered_before_evaluation"])
        self.assertTrue(entry["registered_before_evaluation"])
        self.assertIn("design_disclosure", entry)

    def test_flag_uses_origin_data(self):
        # 1990-08: unemployment gap is small and the curve is not inverted -> risk-on
        info = regime.defensive_flag("1990-08-31")
        self.assertFalse(info["defensive"])
        self.assertLess(info["sahm_gap"], regime.SAHM_THRESHOLD)
        # 1991-01: Sahm-like gap is above 0.5 -> risk-off
        info = regime.defensive_flag("1991-01-31")
        self.assertTrue(info["sahm_flag"])
        self.assertTrue(info["defensive"])

    def test_compose_selects_member(self):
        calls = []

        def predict(kind, name, thunk):
            calls.append(name)
            return {"point": 1.0, "quantiles": [1.0] * 19, "data_class": "REAL_TIME",
                    "regime": {}}

        compose = regime.compose(predict)
        risk_on = compose("2019-06-30", 1, None, "auto")
        self.assertEqual(risk_on["regime"]["selected"], regime.RISK_ON)
        self.assertIn(regime.RISK_ON, calls)

        # force the risk-off branch by patching the flag for this call
        original = regime.defensive_flag
        try:
            regime.defensive_flag = lambda origin, data_root=None: {
                "sahm_gap": 0.9, "spread": 0.2, "sahm_flag": True, "inversion_flag": False,
                "defensive": True}
            from amfe.forecast import forecast as real_forecast

            class Part:
                def to_dict(self):
                    return {"point": 2.0, "quantiles": [2.0] * 19, "data_class": "REAL_TIME"}

            regime.forecast = lambda *a, **k: Part()
            risk_off = compose("2019-06-30", 1, None, "auto")
            self.assertEqual(risk_off["regime"]["selected"], regime.RISK_OFF_MEMBER)
            self.assertTrue(risk_off["regime"]["defensive"])
            regime.forecast = real_forecast
        finally:
            regime.defensive_flag = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
