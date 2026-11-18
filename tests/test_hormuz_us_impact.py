#!/usr/bin/env python3
"""Tests for the Hormuz -> US consumer/social impact translation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "hormuz_us_impact.py"
spec = importlib.util.spec_from_file_location("hormuz_us_impact", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class HormuzImpactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = mod.build_report("hormuz_closed_6m", "2026-11-18T00:00:00Z")

    def test_schema_and_status(self):
        self.assertEqual(self.report["schema"], "amfe.hormuz_us_impact.v1")
        self.assertEqual(self.report["status"], "RESEARCH_ONLY")
        self.assertFalse(self.report["admission"])
        self.assertFalse(self.report["causal_claim"])

    def test_path_shape_and_positivity(self):
        rows = self.report["monthly"]
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertGreater(row["brent_usd_bbl"], 0)
            self.assertGreater(row["gasoline_usd_gal"], 0)
            self.assertGreater(row["cpi_headline_pp"], 0)

    def test_oil_range_ordering(self):
        oil = self.report["oil_module"]
        rng = oil["price_range_usd_bbl"]
        self.assertLessEqual(rng["low"], rng["central"])
        self.assertLessEqual(rng["central"], rng["high"])
        self.assertAlmostEqual(oil["price_peak_usd_bbl"], 168.86, places=2)

    def test_gasoline_pass_through(self):
        peak = self.report["peak"]
        expected = 3.10 + (peak["brent_usd_bbl"] - 75.0) * 0.025
        self.assertAlmostEqual(peak["gasoline_usd_gal"], expected, places=2)
        self.assertLessEqual(peak["gasoline_range_usd_gal"]["low"], peak["gasoline_usd_gal"])
        self.assertGreaterEqual(peak["gasoline_range_usd_gal"]["high"], peak["gasoline_usd_gal"])

    def test_cpi_components_add_up(self):
        peak = self.report["peak"]
        self.assertGreater(peak["cpi_headline_pp"], peak["cpi_direct_pp"])
        self.assertGreaterEqual(peak["cpi_core_round_pp"], 0.0)
        self.assertAlmostEqual(peak["cpi_headline_pp"],
                               peak["cpi_direct_pp"] + peak["cpi_core_round_pp"], places=1)

    def test_household_cost(self):
        peak = self.report["peak"]
        self.assertAlmostEqual(peak["household_annual_cost_usd"],
                               12 * peak["household_monthly_cost_usd"], delta=0.1)
        self.assertGreater(peak["low_income_budget_shock_pct"],
                           peak["median_budget_shock_pct"])

    def test_social_risk_block(self):
        risk = self.report["social_political"]
        self.assertIn(risk["band"], {"low", "elevated", "high", "severe"})
        self.assertGreaterEqual(risk["score_0_100"], 0)
        self.assertLessEqual(risk["score_0_100"], 100)
        self.assertIn("not a probability", risk["note"])

    def test_determinism_and_digest(self):
        again = mod.build_report("hormuz_closed_6m", "2026-11-18T00:00:00Z")
        self.assertEqual(json.dumps(self.report, ensure_ascii=False, sort_keys=True),
                         json.dumps(again, ensure_ascii=False, sort_keys=True))
        text = json.dumps(self.report, ensure_ascii=False, sort_keys=True) + "\n"
        expected = hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        self.assertEqual(mod._sha(text), expected)

    def test_no_raw_data_payload(self):
        text = json.dumps(self.report, ensure_ascii=False).lower()
        for forbidden in ("used_origins", "vintage_dates", "actual_values", "forecast_values", "raw_data"):
            self.assertNotIn(forbidden, text)


    def test_stress_block(self):
        stress = mod.build_report("hormuz_closed_6m", "2026-11-18T00:00:00Z", stress=True)
        block = stress["stress"]
        self.assertIsNotNone(block)
        self.assertGreater(block["effective_quality_premium_usd_bbl"], 12.0)
        self.assertGreater(block["diesel_usd_gal"], block["gasoline_usd_gal"])
        self.assertGreater(block["jet_usd_gal"], 0.0)
        self.assertGreater(block["delivered_crude_delta_usd_bbl"],
                           block["brent_usd_bbl"] if "brent_usd_bbl" in block else 0.0)
        self.assertIn(block["social_political"]["band"], {"elevated", "high", "severe"})
        self.assertTrue(all(block["checks"].values()))

    def test_stress_is_off_by_default(self):
        self.assertIsNone(self.report["stress"])

    def test_stress_is_deterministic(self):
        first = mod.build_report("hormuz_closed_6m", "2026-11-18T00:00:00Z", stress=True)
        second = mod.build_report("hormuz_closed_6m", "2026-11-18T00:00:00Z", stress=True)
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))

    def test_three_month_scenario(self):
        three = mod.build_report("hormuz_closed_3m", "2026-11-18T00:00:00Z")
        self.assertEqual(len(three["monthly"]), 6)
        self.assertLessEqual(three["oil_module"]["price_mean_usd_bbl"],
                             self.report["oil_module"]["price_mean_usd_bbl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
