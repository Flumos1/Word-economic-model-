"""R2 data gate report: structure and acceptance criteria of protocols/r2_gate_report.json."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "protocols" / "r2_gate_report.json"


class R2GateReport(unittest.TestCase):
    def test_report_structure_and_criteria(self):
        if not REPORT.exists():
            self.skipTest("the gate report is created by the data-refresh workflow")
        r = json.loads(REPORT.read_text(encoding="utf-8"))
        self.assertEqual(r["gate"], "R2")
        self.assertIn("matched_pairs", r["totals"])
        self.assertIn("matched_pairs_pre_2000", r["totals"])
        self.assertIn("passed", r)
        self.assertGreaterEqual(r["criteria"]["min_pairs"], 30)
        self.assertGreaterEqual(r["criteria"]["min_pairs_pre_2000"], 10)
        for pair in r["pairs"]:
            self.assertLessEqual(abs(pair["vintage_value"] - pair["initial_release"]), r["tolerance"], pair)
        # every compared pair belongs to a series in the plan
        plan = json.loads((ROOT / "protocols" / "data_plan.json").read_text(encoding="utf-8"))
        ids = {s["id"] for s in plan["series"]}
        for pair in r["pairs"]:
            self.assertIn(pair["series"], ids)
        # the metadata-only rule: the report must not contain raw observations beyond the pairs
        self.assertNotIn("entries", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
