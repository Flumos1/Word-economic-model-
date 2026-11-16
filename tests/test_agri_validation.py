"""Tests for G3 historical validation of the agri/soy module (docs/163).

Offline only: evaluation uses the deterministic synthetic MRIO cost structure from the G2 bridge;
no network, no F: dataset, no raw third-party data.  The tests cover episode definitions, the
episode adapter, scoring on known numbers, aggregate metrics, determinism, coverage, sensitivity,
fail-closed behaviour, bridge integration, the no-point-claim rule and the CLI.

Run: ``python tests/test_agri_validation.py`` or
``python -m unittest tests.test_agri_validation -v``.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import agri_bridge as bridge_tool  # noqa: E402
import agri_validation as validation  # noqa: E402


class EpisodeRegistry(unittest.TestCase):
    def test_four_documented_episodes(self):
        self.assertEqual(len(validation.EPISODES), 4)
        ids = [episode.episode_id for episode in validation.EPISODES]
        self.assertEqual(ids, ["2012_us_drought", "2021_22_brazil_prices",
                               "2008_argentina_export_restriction",
                               "2022_indonesia_palm_ban"])
        for episode in validation.EPISODES:
            data = episode.to_dict()
            self.assertTrue(data["source_note"])
            self.assertTrue(data["assumption_note"])
            self.assertGreaterEqual(len(data["shocks"]), 1)
            self.assertEqual(data["source_class"], "assumption")
            for commodity in ("soybeans", "soymeal"):
                self.assertTrue(math.isfinite(data["actual_price_change"][commodity]))
                self.assertGreater(data["base_prices_usd_t"][commodity], 0.0)

    def test_episode_fail_closed(self):
        base = validation.EPISODES[0]
        with self.assertRaises(validation.AgriValidationError):
            replace(base, episode_id="")
        with self.assertRaises(validation.AgriValidationError):
            replace(base, yield_region="Atlantis")
        with self.assertRaises(validation.AgriValidationError):
            replace(base, yield_shock=0.10)
        with self.assertRaises(validation.AgriValidationError):
            replace(base, export_cut=0.2)  # export cut without a region
        with self.assertRaises(validation.AgriValidationError):
            replace(base, export_region="Brazil", export_cut=1.5)
        with self.assertRaises(validation.AgriValidationError):
            replace(base, actual_soy_change=float("nan"))
        with self.assertRaises(validation.AgriValidationError):
            replace(base, base_soy_price_usd_t=-1.0)
        with self.assertRaises(validation.AgriValidationError):
            replace(base, yield_region=None, yield_shock=0.0, export_region=None,
                    export_cut=0.0, demand_shock_mt=0.0)


class AdapterAndScoring(unittest.TestCase):
    def test_episode_prediction_ranges_are_ordered(self):
        for episode in validation.EPISODES:
            result = validation.episode_agri_result(episode)
            for commodity in ("soybeans", "soymeal"):
                interval = result.price_ranges[commodity]
                self.assertLess(interval.low, interval.high)
                self.assertGreater(interval.low, 0.0)
            effects = result.to_dict()["effects"]
            self.assertGreater(effects["price_multiplier_range"]["low"], 1.0)
            self.assertLessEqual(effects["effective_supply_loss_mt"]["low"],
                                 effects["effective_supply_loss_mt"]["high"])

    def test_balance_identity_in_episode(self):
        result = validation.episode_agri_result(validation.EPISODES[0]).to_dict()
        self.assertAlmostEqual(result["balance"]["identity_check"]["baseline_residual_mt"],
                               0.0, places=9)
        self.assertGreaterEqual(result["balance"]["scenario"]["ending_stocks_mt"], 0.0)

    def test_score_prediction_known_numbers(self):
        covered = validation.score_prediction(predicted_low=0.10, predicted_high=0.20,
                                              actual=0.15)
        self.assertTrue(covered["covered"])
        self.assertTrue(covered["direction_correct"])
        self.assertAlmostEqual(covered["predicted_midpoint"], 0.15, places=12)
        self.assertAlmostEqual(covered["midpoint_error"], 0.0, places=12)
        self.assertAlmostEqual(covered["naive_error"], 0.15, places=12)
        self.assertAlmostEqual(covered["bias"], 0.0, places=12)
        self.assertTrue(covered["better_than_naive"])
        missed = validation.score_prediction(predicted_low=0.01, predicted_high=0.05,
                                             actual=0.20)
        self.assertFalse(missed["covered"])
        self.assertAlmostEqual(missed["midpoint_error"], 0.17, places=12)
        self.assertTrue(missed["better_than_naive"])  # 0.17 < 0.20, but the range still misses
        self.assertAlmostEqual(missed["bias"], -0.17, places=12)
        overshoot = validation.score_prediction(predicted_low=0.10, predicted_high=0.20,
                                                actual=0.02)
        self.assertFalse(overshoot["covered"])
        self.assertFalse(overshoot["better_than_naive"])
        self.assertGreater(overshoot["bias"], 0.0)
        wrong_direction = validation.score_prediction(predicted_low=-0.10, predicted_high=-0.05,
                                                     actual=0.10)
        self.assertFalse(wrong_direction["direction_correct"])
        zero = validation.score_prediction(predicted_low=-0.01, predicted_high=0.01, actual=0.0)
        self.assertTrue(zero["direction_correct"])
        self.assertTrue(zero["covered"])

    def test_score_prediction_fail_closed(self):
        with self.assertRaises(validation.AgriValidationError):
            validation.score_prediction(predicted_low=0.2, predicted_high=0.1, actual=0.1)
        with self.assertRaises(validation.AgriValidationError):
            validation.score_prediction(predicted_low=0.1, predicted_high=float("nan"),
                                        actual=0.1)

    def test_sensitivity_to_shock_size_and_exports(self):
        mild = validation.EPISODES[0]
        severe = replace(mild, yield_shock=-0.20)
        mild_result = validation.episode_agri_result(mild).to_dict()
        severe_result = validation.episode_agri_result(severe).to_dict()
        self.assertGreater(severe_result["effects"]["price_multiplier_range"]["high"],
                           mild_result["effects"]["price_multiplier_range"]["high"])
        self.assertGreater(severe_result["effects"]["production_loss_mt"],
                           mild_result["effects"]["production_loss_mt"])
        # an export restriction on an exporter must also add to the effective supply loss
        export_episode = replace(mild, export_region="Brazil", export_cut=0.5)
        export_result = validation.episode_agri_result(export_episode).to_dict()
        self.assertGreater(export_result["effects"]["restricted_exports_mt"], 0.0)
        self.assertGreater(export_result["effects"]["effective_supply_loss_mt"]["high"],
                           mild_result["effects"]["effective_supply_loss_mt"]["high"])


class AggregateEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.structure = bridge_tool.synthetic_cost_structure()
        cls.report = validation.evaluate_episodes(validation.EPISODES[:2],
                                                  structure=cls.structure)

    def test_metrics_are_complete_and_consistent(self):
        aggregate = self.report["aggregate"]
        self.assertEqual(aggregate["n_episodes"], 2)
        self.assertEqual(aggregate["n_commodity_cells"], 4)
        self.assertLessEqual(aggregate["direction_hits"], 4)
        self.assertLessEqual(aggregate["coverage_hits"], 4)
        errors = [row["metrics"][commodity]["midpoint_error"]
                  for row in self.report["episodes"] for commodity in ("soybeans", "soymeal")]
        naive = [row["metrics"][commodity]["naive_error"]
                 for row in self.report["episodes"] for commodity in ("soybeans", "soymeal")]
        self.assertAlmostEqual(aggregate["model_mae"], sum(errors) / 4, places=6)
        self.assertAlmostEqual(aggregate["naive_mae"], sum(naive) / 4, places=6)
        self.assertAlmostEqual(aggregate["mae_improvement_vs_naive"],
                               aggregate["naive_mae"] - aggregate["model_mae"], places=6)
        for commodity in ("soybeans", "soymeal"):
            entry = aggregate["by_commodity"][commodity]
            self.assertEqual(entry["n"], 2)
            self.assertLessEqual(entry["coverage_hits"], 2)

    def test_where_worse_is_consistent(self):
        cells = [(row["episode"]["episode_id"], commodity, row["metrics"][commodity])
                 for row in self.report["episodes"] for commodity in ("soybeans", "soymeal")]
        for item in self.report["where_worse"]:
            metric = next(metric for episode_id, commodity, metric in cells
                          if episode_id == item["episode_id"] and commodity == item["commodity"])
            self.assertTrue((not metric["covered"]) or (not metric["better_than_naive"]))
        # in this prototype the historical moves are larger than the supply-only model ranges
        self.assertGreaterEqual(len(self.report["where_worse"]), 1)

    def test_determinism(self):
        again = validation.evaluate_episodes(validation.EPISODES[:2],
                                             structure=bridge_tool.synthetic_cost_structure())
        self.assertEqual(json.dumps(self.report, sort_keys=True),
                         json.dumps(again, sort_keys=True))
        self.assertEqual(self.report["digest"], again["digest"])

    def test_no_point_claims_and_verdict(self):
        self.assertTrue(self.report["not_a_forecast"])
        self.assertFalse(self.report["verdict"]["validated"])
        self.assertIn(self.report["verdict"]["status"],
                      ("NOT_VALIDATED", "WEAK_SUPPORT_MAE_ONLY", "PARTIAL_SUPPORT"))
        self.assertEqual(len(self.report["digest"]), 64)
        for row in self.report["episodes"]:
            for commodity in ("soybeans", "soymeal"):
                metric = row["metrics"][commodity]
                self.assertTrue(metric["midpoint_used_for_scoring_only"])
                self.assertLessEqual(metric["predicted_low"], metric["predicted_high"])
        text = json.dumps(self.report, ensure_ascii=False)
        self.assertNotIn('"point_forecast"', text)

    def test_bridge_integration(self):
        for row in self.report["episodes"]:
            bridge = row["bridge"]
            self.assertTrue(bridge["available"])
            self.assertFalse(bridge["included_in_world_totals"])
            self.assertLessEqual(bridge["food_cpi_pp"]["low"], bridge["food_cpi_pp"]["high"])
        # a broken structure must be recorded as a bridge error, not crash the validation
        broken = validation.evaluate_episodes(validation.EPISODES[:1],
                                              structure=object())
        self.assertFalse(broken["episodes"][0]["bridge"]["available"])
        self.assertIn("error", broken["episodes"][0]["bridge"])

    def test_markdown_render(self):
        text = validation.render_markdown(self.report)
        self.assertIn("Agri/soy historical validation", text)
        self.assertIn("2012 US drought", text)
        self.assertIn("Verdict", text)
        self.assertIn("No forecast-accuracy or admission claim", text)


class FailClosedAndCLI(unittest.TestCase):
    def test_evaluate_fail_closed(self):
        with self.assertRaises(validation.AgriValidationError):
            validation.evaluate_episodes([])
        with self.assertRaises(validation.AgriValidationError):
            validation.evaluate_episodes([{"not": "an episode"}])

    def test_self_test_and_cli_help(self):
        payload = validation.self_test()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checks"]["episodes_present"], True)
        import subprocess
        tool = ROOT / "tools" / "agri_validation.py"
        help_result = subprocess.run([sys.executable, str(tool), "--help"],
                                     capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--synthetic-bridge", help_result.stdout)
        json_result = subprocess.run([sys.executable, str(tool), "--synthetic-bridge",
                                      "--json", "--no-write"],
                                     capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(json_result.returncode, 0)
        report = json.loads(json_result.stdout)
        self.assertEqual(report["schema"], validation.SCHEMA)
        self.assertTrue(report["not_a_forecast"])
        self.assertFalse(report["verdict"]["validated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
