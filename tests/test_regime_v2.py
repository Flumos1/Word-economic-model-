"""ENSEMBLE_REGIME_V2: second pre-registered risk-off candidate (docs/115)."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.ensemble import regime_v2  # noqa: E402


def lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class RegimeV2(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(regime_v2.CANDIDATE_ID, "ENSEMBLE_REGIME_V2")
        self.assertEqual(regime_v2.SAHM_THRESHOLD, 0.5)
        self.assertEqual(regime_v2.SHRINK, 0.5)

    def test_registration_matches_code(self):
        doc = json.loads((ROOT / "protocols" / "candidates_2026-11-12_regime_v2.json")
                         .read_text(encoding="utf-8"))
        entry = next(c for c in doc["candidates"] if c["id"] == "ENSEMBLE_REGIME_V2")
        self.assertEqual(entry["code_sha256"],
                         lf_sha256(ROOT / "src" / "amfe" / "ensemble" / "regime_v2.py"))
        self.assertTrue(doc["registered_before_evaluation"])

    def test_flag(self):
        self.assertFalse(regime_v2.defensive_flag("1990-08-31")["defensive"])
        self.assertTrue(regime_v2.defensive_flag("1991-01-31")["defensive"])

    def test_compose_blends_bvar_and_comparator(self):
        calls = []

        def predict(kind, name, thunk):
            calls.append(name)
            if name == "ENSEMBLE_INVERSE_MSE":
                return {"point": 100.0, "quantiles": [100.0] * 19, "data_class": "REAL_TIME"}
            return thunk()

        original = regime_v2.defensive_flag
        try:
            regime_v2.defensive_flag = lambda origin, data_root=None: {"sahm_gap": 0.9,
                                                                       "defensive": True}

            def member_forecast(member):
                point = {"AO": 1.0, "AR_BIC": 2.0, "BVAR_MINNESOTA": 4.0, "RW": 3.0}[member]
                return {"point": point, "quantiles": [point] * 19, "data_class": "REAL_TIME",
                        "vintage_date": "2026-01-01"}

            regime_v2.forecast = lambda member, *a, **k: type(
                "Part", (), {"to_dict": lambda self, m=member: member_forecast(m)})()
            result = regime_v2.compose(predict)("2001-06-30", 1, None, "auto")
            comp_point = (1.0 + 2.0 + 4.0 + 3.0) / 4.0
            expected = 0.5 * 4.0 + 0.5 * comp_point
            self.assertAlmostEqual(result["point"], expected)
            self.assertEqual(len(result["quantiles"]), 19)
            self.assertTrue(all(result["quantiles"][i] <= result["quantiles"][i + 1]
                                for i in range(18)))
            self.assertTrue(result["regime"]["defensive"])
        finally:
            regime_v2.defensive_flag = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
