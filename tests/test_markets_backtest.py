"""Tests for the oil-market historical backtest (``amfe.markets.backtest``, docs/155).

Offline and deterministic: the episodes are declared inputs (public approximations), the engine is
run in-process, and the metrics are checked both for internal consistency and against the reference
run recorded in docs/155 (2026-11-16).  If the oil parameters or the engine change, the reference
numbers must be re-baselined together with the docs.

Run from the repository root: ``python tests/test_markets_backtest.py``.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets.backtest import (  # noqa: E402
    EPISODES,
    ORDER_HIGH,
    ORDER_LOW,
    BacktestError,
    Episode,
    backtest,
    build_case,
    episode,
    report_markdown,
    run_episode,
)
from amfe.markets.contract import MarketError, ScenarioError  # noqa: E402
from amfe.markets.engine import simulate  # noqa: E402

#: Reference run of 2026-11-16 (docs/155).  Re-baseline if the oil parameters change.
REFERENCE = {
    "1990_gulf": {"model_delta_usd": 4.052785, "error_usd": -19.947215, "ratio": 0.168866},
    "2019_abqaiq": {"model_delta_usd": 14.918091, "error_usd": 5.918091, "ratio": 1.657566},
    "2022_russia": {"model_delta_usd": 16.730443, "error_usd": -13.269557, "ratio": 0.557681},
}
REFERENCE_METRICS = {"mae_model_usd": 13.044954, "mae_baseline_usd": 21.0,
                     "skill_score_vs_no_shock": 0.378812, "rmse_model_usd": 14.247764,
                     "direction_hits": 3, "order_hits": 2, "sensitivity_covers_actual": 1}
EXPECTED_EPISODES = ("1990_gulf", "2019_abqaiq", "2022_russia")


class EpisodeLibraryTests(unittest.TestCase):
    def test_three_episodes_with_declared_inputs(self):
        self.assertEqual(tuple(item.episode_id for item in EPISODES), EXPECTED_EPISODES)
        for target in EPISODES:
            self.assertEqual(target.date[:4], target.episode_id[:4])
            self.assertGreater(target.actual_delta_usd, 0.0)
            self.assertGreater(target.base_price, 0.0)
            self.assertGreaterEqual(target.horizon_months, target.loss_months)
            assumptions = target.inputs()
            self.assertGreaterEqual(len(assumptions), 10)
            self.assertTrue(all(item.source_class == "assumption" for item in assumptions))
            self.assertTrue(all(item.unit for item in assumptions))
            names = {item.name for item in assumptions}
            for required in ("supply_loss", "loss_months", "actual_delta_usd"):
                self.assertIn(f"{target.episode_id}.{required}", names)

    def test_unknown_episode_is_refused(self):
        with self.assertRaises(ScenarioError):
            episode("1995_never")
        with self.assertRaises(ScenarioError):
            backtest(["1995_never"])

    def test_invalid_episode_inputs_are_refused(self):
        template = EPISODES[0]
        with self.assertRaises(BacktestError):
            Episode(**{**template.__dict__, "supply_loss": -1.0})
        with self.assertRaises(BacktestError):
            Episode(**{**template.__dict__, "loss_months": 0})
        with self.assertRaises(BacktestError):
            Episode(**{**template.__dict__, "loss_months": template.horizon_months + 1})
        with self.assertRaises(BacktestError):
            Episode(**{**template.__dict__, "actual_delta_usd": 0.0})
        with self.assertRaises(BacktestError):
            Episode(**{**template.__dict__, "elasticity_short": 0.2})

    def test_empty_selection_is_refused(self):
        with self.assertRaises(BacktestError):
            backtest([])


class CaseTests(unittest.TestCase):
    def test_shock_vectors_are_built_from_the_episode(self):
        target = episode("1990_gulf")
        calibration, shock = build_case(target)
        self.assertEqual(shock.months, target.horizon_months)
        self.assertEqual(len(shock.lost_supply), target.horizon_months)
        self.assertAlmostEqual(shock.lost_supply[0], target.supply_loss)
        self.assertEqual(sum(1 for value in shock.lost_supply if value > 0), target.loss_months)
        first_month = (target.bypass_capacity / target.bypass_ramp_months
                       + target.supply_response / target.supply_response_ramp_months)
        self.assertAlmostEqual(shock.added_supply[0], first_month, places=9)
        self.assertAlmostEqual(shock.added_supply[-1],
                               target.bypass_capacity + target.supply_response, places=9)
        self.assertLessEqual(sum(shock.strategic_release) * 30.4375,
                             target.spr_stock + 1e-6)
        self.assertEqual(calibration.base_price, target.base_price)

    def test_unknown_override_is_refused(self):
        with self.assertRaises(BacktestError):
            build_case(episode("2019_abqaiq"), not_a_parameter=1.0)

    def test_engine_path_checks_pass_for_every_episode(self):
        for target in EPISODES:
            calibration, shock = build_case(target)
            path = simulate(calibration, shock)
            self.assertTrue(path.ok, f"{target.episode_id}: {path.checks}")


class MetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = backtest()
        cls.by_id = {item.episode_id: item for item in cls.report.episodes}

    def test_reference_numbers_match_docs_155(self):
        for episode_id, expected in REFERENCE.items():
            result = self.by_id[episode_id]
            self.assertAlmostEqual(result.model_delta_usd, expected["model_delta_usd"], places=6)
            self.assertAlmostEqual(result.error_usd, expected["error_usd"], places=6)
            self.assertAlmostEqual(result.ratio, expected["ratio"], places=6)
        for key, value in REFERENCE_METRICS.items():
            self.assertAlmostEqual(self.report.metrics[key], value, places=6, msg=key)

    def test_error_formulas_are_consistent(self):
        for result in self.report.episodes:
            self.assertEqual(result.error_usd,
                             round(result.model_delta_usd - result.actual_delta_usd, 6))
            self.assertEqual(result.abs_error_usd, round(abs(result.error_usd), 6))
            self.assertEqual(result.rel_error,
                             round(result.error_usd / result.actual_delta_usd, 6))
            self.assertEqual(result.ratio,
                             round(result.model_delta_usd / result.actual_delta_usd, 6))
            self.assertEqual(result.model_delta_usd,
                             round(result.model_peak_price - result.base_price, 6))

    def test_direction_and_order_flags(self):
        for result in self.report.episodes:
            self.assertTrue(result.direction_ok)
            self.assertEqual(result.direction_ok,
                             (result.model_delta_usd > 0) == (result.actual_delta_usd > 0))
            self.assertEqual(result.order_ok, ORDER_LOW <= result.ratio <= ORDER_HIGH)
        # 1990 is the declared miss of the order band
        self.assertFalse(self.by_id["1990_gulf"].order_ok)

    def test_baseline_and_skill(self):
        for result in self.report.episodes:
            self.assertEqual(result.baseline_abs_error_usd,
                             round(abs(result.actual_delta_usd), 6))
            self.assertEqual(result.beats_baseline,
                             result.abs_error_usd < result.baseline_abs_error_usd)
        expected_skill = 1.0 - self.report.metrics["mae_model_usd"] \
            / self.report.metrics["mae_baseline_usd"]
        self.assertAlmostEqual(self.report.metrics["skill_score_vs_no_shock"],
                               round(expected_skill, 6), places=6)
        self.assertGreater(self.report.metrics["mae_baseline_usd"],
                           self.report.metrics["mae_model_usd"])

    def test_aggregate_mae_and_rmse(self):
        errors = [item.abs_error_usd for item in self.report.episodes]
        self.assertAlmostEqual(self.report.metrics["mae_model_usd"],
                               round(math.fsum(errors) / len(errors), 6), places=6)
        squared = [item.error_usd ** 2 for item in self.report.episodes]
        self.assertAlmostEqual(self.report.metrics["rmse_model_usd"],
                               round(math.sqrt(math.fsum(squared) / len(squared)), 6),
                               places=6)
        self.assertEqual(self.report.metrics["direction_hits"], 3)
        self.assertEqual(self.report.metrics["order_hits"], 2)

    def test_verdict_and_where_worse_are_honest(self):
        self.assertEqual(self.report.verdict, "DIRECTION_OK_MAGNITUDE_OFF")
        self.assertIn(self.report.verdict, {"PASS_DIRECTION_AND_ORDER",
                                            "DIRECTION_OK_MAGNITUDE_OFF", "FAIL"})
        self.assertEqual(self.report.where_worse, ())
        self.assertEqual(self.report.metrics["sensitivity_covers_actual"], 1)

    def test_sensitivity_variants(self):
        for result in self.report.episodes:
            variants = [item["variant"] for item in result.sensitivity]
            self.assertEqual(len(variants), 8)
            for name in ("elasticity_literature_low", "elasticity_literature_high",
                         "no_strategic_release", "bypass_plus_1mbd", "no_risk_premium",
                         "premium_doubled"):
                self.assertIn(name, variants)
            deltas = [item["model_delta_usd"] for item in result.sensitivity]
            self.assertEqual(deltas, sorted(deltas))
            self.assertLessEqual(result.sensitivity_range["low"],
                                 result.sensitivity_range["high"])
            self.assertEqual(result.sensitivity_range["actual_within_range"],
                             result.sensitivity_range["low"] <= result.actual_delta_usd
                             <= result.sensitivity_range["high"])

    def test_report_is_deterministic_and_aggregate_only(self):
        again = backtest()
        self.assertEqual(self.report.digest(), again.digest())
        self.assertEqual(self.report.as_dict()["episodes"], again.as_dict()["episodes"])
        document = self.report.as_dict()
        self.assertEqual(document["schema"], "amfe.markets.backtest.v1")
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertTrue(document["research_only"])
        self.assertEqual(len(document["digest"]), 64)
        text = json.dumps(document, ensure_ascii=False)
        for forbidden in ('"matrix"', '"Z"', "timestamp", "generated_at"):
            self.assertNotIn(forbidden, text)
        assumptions = document["assumptions"]
        self.assertTrue(assumptions)
        self.assertTrue(all(item["source_class"] == "assumption" for item in assumptions))

    def test_markdown_report(self):
        markdown = report_markdown(self.report)
        self.assertIn("Бэктест нефтяного модуля", markdown)
        self.assertIn("DIRECTION_OK_MAGNITUDE_OFF", markdown)
        for episode_id in EXPECTED_EPISODES:
            self.assertIn(episode_id, markdown)
        self.assertIn(self.report.digest(), markdown)


class CLITests(unittest.TestCase):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "tools" / "oil_backtest.py"),
                               *args], capture_output=True, text=True, cwd=str(ROOT), timeout=120)

    def test_json_mode(self):
        result = self.run_tool("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["verdict"], "DIRECTION_OK_MAGNITUDE_OFF")
        self.assertEqual(len(document["episodes"]), 3)

    def test_episode_subset_and_output_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_tool("--episodes", "1990_gulf", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            report_path = Path(tmp) / "report.json"
            markdown_path = Path(tmp) / "report.md"
            self.assertTrue(report_path.is_file())
            self.assertTrue(markdown_path.is_file())
            document = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(len(document["episodes"]), 1)
            self.assertEqual(document["episodes"][0]["episode_id"], "1990_gulf")

    def test_unknown_episode_exits_non_zero(self):
        result = self.run_tool("--episodes", "1995_never", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown episodes", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
