"""Tests for oil module v3 (src/amfe/markets/oil_v3.py, docs/165).

Covers the three things v3 adds: the real EIA monthly series loader (hash, gaps, fail-closed),
the demand-shift channel in the engine, and the regime-split pre-registered calibration with a
monthly-path OOS.  The tests do not trust the report either: they re-run the fit with perturbed OOS
actuals to prove the parameters do not move, and they pin the pre-registered grid/regime rules.
"""
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets import oil_v3 as v3  # noqa: E402
from amfe.markets.engine import MarketCalibration, Shock, simulate  # noqa: E402


def calibration(**overrides) -> MarketCalibration:
    base = dict(commodity="test", base_price=100.0, base_demand=100.0, baseline_supply=100.0,
                elasticity_short=-0.25, elasticity_long=-0.55, elasticity_ramp_months=6,
                risk_premium=0.0)
    base.update(overrides)
    return MarketCalibration(**base)


class SeriesTest(unittest.TestCase):
    def test_real_series_is_complete_and_pinned(self):
        series = v3.load_series()
        self.assertEqual(series["series_id"], "RBRTE")
        self.assertEqual(series["observations"], 474)
        self.assertEqual(series["first_month"], "1987-05")
        self.assertEqual(series["last_month"], "2026-10")
        self.assertEqual(len(series["sha256"]), 64)
        prices = series["prices"]
        self.assertEqual(prices[(1990, 10)], 36.02)
        self.assertEqual(prices[(2020, 3)], 32.01)
        self.assertEqual(prices[(2020, 4)], 18.38)
        self.assertEqual(prices[(2022, 6)], 122.71)
        self.assertEqual(len(prices), series["observations"])

    def test_missing_file_fails_closed(self):
        with self.assertRaises(v3.OilV3Error):
            v3.load_series(ROOT / "data" / "oil" / "does-not-exist.csv")

    def test_malformed_series_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("year,01..\n1990,abc\n", encoding="utf-8")
            with self.assertRaises(v3.OilV3Error):
                v3.load_series(path)
            path.write_text("year,01,02,03,04,05,06,07,08,09,10,11,12\n"
                            "1990,1,2,3,4,5,6,7,8,9,10,11\n", encoding="utf-8")
            with self.assertRaises(v3.OilV3Error):
                v3.load_series(path)


class EpisodesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = v3.load_series()

    def test_episode_table_is_consistent(self):
        ids = [item.episode_id for item in v3.EPISODES]
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(ids), len(set(ids)))
        regimes = {item.episode_id: item.regime for item in v3.EPISODES}
        self.assertEqual(regimes["2005_katrina"], "acute")
        self.assertEqual(regimes["2019_abqaiq"], "acute")
        self.assertEqual(regimes["2020_covid"], "demand")
        for item in v3.EPISODES:
            if item.regime == "acute":
                self.assertLessEqual(item.loss_months, v3.ACUTE_MAX_MONTHS)
            if item.regime == "prolonged":
                self.assertGreater(item.loss_months, v3.ACUTE_MAX_MONTHS)

    def test_train_and_oos_are_disjoint(self):
        train = [item for ids in v3.TRAIN_IDS.values() for item in ids]
        self.assertTrue(set(train).isdisjoint(set(v3.OOS_IDS)))
        for regime, ids in v3.TRAIN_IDS.items():
            for episode_id in ids:
                self.assertEqual(v3.episode(episode_id).regime, regime)

    def test_observed_extremes_from_real_series(self):
        expected = {
            "1990_gulf": 18.85, "2019_abqaiq": 3.79, "2022_russia": 36.20,
            "2020_covid": -45.27, "2002_venezuela": 8.43, "2005_katrina": 6.46,
            "2011_libya": 26.74, "2018_iran": 15.71,
        }
        for episode_id, value in expected.items():
            summary = v3.episode(episode_id).actual_summary(self.series)
            self.assertAlmostEqual(summary["actual_peak_change"], value, places=2)

    def test_unknown_episode_fails_closed(self):
        with self.assertRaises(v3.OilV3Error):
            v3.episode("1999_unknown")

    def test_invalid_definitions_fail_closed(self):
        base = dict(regime="demand", base_month="2020-01", start="2020-02", horizon_months=2,
                    base_demand=100.0, baseline_supply=100.0, demand_shift=(-1.0,))
        with self.assertRaises(v3.OilV3Error):
            v3.EpisodeV3(episode_id="x", name="x", **base)  # shift length mismatch
        with self.assertRaises(v3.OilV3Error):
            v3.EpisodeV3(episode_id="x", name="x",
                         **{**base, "demand_shift": (1.0, 2.0)})
        with self.assertRaises(v3.OilV3Error):
            v3.EpisodeV3(episode_id="x", name="x", regime="acute", base_month="2020-01",
                         start="2020-02", horizon_months=2, base_demand=100.0,
                         baseline_supply=100.0)  # no loss
        with self.assertRaises(v3.OilV3Error):
            v3.EpisodeV3(episode_id="x", name="x", regime="demand", base_month="2020-01",
                         start="2020-02", horizon_months=2, base_demand=100.0,
                         baseline_supply=100.0, demand_shift=(-1.0, -1.0), risk_premium=5.0)


class EngineDemandChannelTest(unittest.TestCase):
    def test_negative_demand_shift_lowers_price(self):
        cal = calibration()
        baseline = simulate(cal, Shock(lost_supply=(0.0, 0.0, 0.0)))
        collapse = simulate(cal, Shock(lost_supply=(0.0, 0.0, 0.0),
                                       demand_shift=(-5.0, -10.0, -8.0)))
        self.assertTrue(baseline.ok and collapse.ok)
        for month in range(3):
            self.assertLess(collapse.price[month], baseline.price[month])

    def test_zero_shift_is_identical_to_no_shift(self):
        cal = calibration()
        without = simulate(cal, Shock(lost_supply=(4.0, 4.0, 0.0)))
        with_zero = simulate(cal, Shock(lost_supply=(4.0, 4.0, 0.0),
                                        demand_shift=(0.0, 0.0, 0.0)))
        self.assertEqual(without.price, with_zero.price)
        self.assertEqual(without.demand, with_zero.demand)

    def test_impossible_demand_shift_fails_closed(self):
        cal = calibration()
        with self.assertRaises(Exception):
            simulate(cal, Shock(lost_supply=(0.0,), demand_shift=(-150.0,)))

    def test_shift_vector_length_is_validated(self):
        with self.assertRaises(Exception):
            Shock(lost_supply=(1.0, 1.0), demand_shift=(-1.0,))


class ProtocolTest(unittest.TestCase):
    def test_protocol_is_pre_registered(self):
        protocol = v3.build_protocol()
        self.assertEqual(protocol["schema"], v3.PROTOCOL_VERSION)
        self.assertTrue(protocol["train_oos_disjoint"])
        self.assertEqual(set(protocol["oos_episodes"]), set(v3.OOS_IDS))
        self.assertEqual(protocol["grid"]["acute"]["size"], 12)
        self.assertEqual(protocol["grid"]["prolonged"]["size"], 42)
        self.assertEqual(len(v3.protocol_sha256(protocol)), 64)
        self.assertEqual(protocol["contamination_note"].count("1990"), 1)
        for criteria in ("OOS_PASS", "OOS_PARTIAL", "OOS_FAIL"):
            self.assertIn(criteria, protocol["success_criteria"])

    def test_protocol_hash_is_deterministic(self):
        first = v3.protocol_sha256(v3.build_protocol())
        second = v3.protocol_sha256(v3.build_protocol())
        self.assertEqual(first, second)


class FitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = v3.load_series()

    def test_fit_uses_train_only_and_is_deterministic(self):
        fitted = v3.fit_regime("acute", self.series)
        again = v3.fit_regime("acute", self.series)
        self.assertEqual(fitted.params, again.params)
        self.assertEqual(fitted.candidates, 12)
        self.assertEqual(fitted.train["n"], 1)
        self.assertLess(fitted.params["elasticity_short"], 0.0)
        self.assertLessEqual(abs(fitted.params["elasticity_short"]),
                             abs(fitted.params["elasticity_long"]))

    def test_fit_rejects_oos_and_wrong_regime(self):
        with self.assertRaises(v3.OilV3Error):
            v3.fit_regime("acute", self.series, [v3.episode("2019_abqaiq")])
        with self.assertRaises(v3.OilV3Error):
            v3.fit_regime("acute", self.series, [v3.episode("2011_libya")])
        with self.assertRaises(v3.OilV3Error):
            v3.fit_regime("demand", self.series)

    def test_fit_is_independent_of_oos_actuals(self):
        fitted = v3.fit_regime("prolonged", self.series)
        moved = dict(self.series)
        moved["prices"] = dict(self.series["prices"])
        for month in ((1990, 8), (2019, 9), (2022, 3)):
            moved["prices"][month] = moved["prices"][month] * 1.5
        self.assertEqual(fitted.params, v3.fit_regime("prolonged", moved).params)


class ScoringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = v3.load_series()

    def test_score_shape_and_types(self):
        row = v3.score_episode(v3.episode("1990_gulf"),
                               {"regime": "prolonged", "elasticity_short": -0.12,
                                "elasticity_long": -0.12}, self.series)
        for key in ("path_mae", "path_rmse", "naive_mae", "peak_change_error",
                    "skill_vs_no_shock", "direction_ok", "order_ok", "metric"):
            self.assertIn(key, row)
        self.assertGreaterEqual(row["path_mae"], 0.0)
        self.assertLessEqual(row["path_mae"], row["path_rmse"] + 1e-9)
        self.assertEqual(row["metric"], "peak")

    def test_demand_episode_uses_trough(self):
        row = v3.score_episode(v3.episode("2020_covid"),
                               {"regime": "demand", "elasticity_short": -0.25,
                                "elasticity_long": -0.55}, self.series)
        self.assertEqual(row["metric"], "trough")
        self.assertLess(row["actual_peak_change"], 0.0)
        self.assertLess(row["model_peak_change"], 0.0)

    def test_known_reference_score(self):
        row = v3.score_episode(v3.episode("2019_abqaiq"),
                               v3.reference_params(v3.episode("2019_abqaiq")), self.series)
        self.assertAlmostEqual(row["model_peak_change"], 2.496397, places=4)
        self.assertAlmostEqual(row["path_rmse"], 1.15, delta=0.01)

    def test_criteria_are_applied_mechanically(self):
        good = {"n": 4, "direction_hits": 4, "order_hits": 3, "mean_path_rmse": 1.0,
                "mean_path_mae": 1.0, "mean_naive_mae": 2.0, "peak_change_mae": 1.0}
        reference = {"mean_path_rmse": 2.0, "peak_change_mae": 2.0}
        self.assertEqual(v3.apply_criteria(good, reference), "OOS_PASS")
        partial = dict(good, order_hits=2, mean_path_rmse=3.0, peak_change_mae=1.5)
        self.assertEqual(v3.apply_criteria(partial, reference), "OOS_PARTIAL")
        bad = dict(good, direction_hits=3, order_hits=1, mean_path_rmse=5.0,
                   peak_change_mae=5.0)
        self.assertEqual(v3.apply_criteria(bad, reference), "OOS_FAIL")


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = v3.load_series()
        cls.report = v3.calibrate(cls.series)

    def test_report_structure_and_honesty(self):
        document = self.report.as_dict()
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertTrue(document["research_only"])
        self.assertIn(document["verdict"], ("OOS_PASS", "OOS_PARTIAL", "OOS_FAIL"))
        self.assertEqual(document["oos"]["direction_hits"], 4)
        self.assertEqual(len(document["data"]["sha256"]), 64)
        self.assertEqual(len(self.report.protocol_sha256), 64)
        self.assertEqual(len(self.report.digest()), 64)
        self.assertIn("demand", document["fits"]["params"])

    def test_markdown_renders_and_is_deterministic(self):
        first = v3.report_markdown(self.report)
        second = v3.report_markdown(self.report)
        self.assertEqual(first, second)
        self.assertIn(self.report.verdict, first)
        self.assertIn("Ограничения", first)

    def test_calibrate_is_deterministic(self):
        again = v3.calibrate(self.series)
        self.assertEqual(self.report.digest(), again.digest())


class CliTest(unittest.TestCase):
    def test_cli_protocol_and_full_run(self):
        import oil_calibration_v3 as cli
        with tempfile.TemporaryDirectory() as directory:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(["--protocol-only", "--out", directory])
            self.assertEqual(code, 0)
            self.assertIn("protocol SHA-256", buffer.getvalue())
            self.assertTrue((Path(directory) / "protocol.json").is_file())
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(["--out", directory, "--quiet"])
            self.assertEqual(code, 0)
            for name in ("protocol.json", "protocol.sha256.txt", "report.json", "report.md"):
                self.assertTrue((Path(directory) / name).is_file(), name)
            document = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertIn(document["verdict"], ("OOS_PASS", "OOS_PARTIAL", "OOS_FAIL"))
            sha = (Path(directory) / "protocol.sha256.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(len(sha), 64)

    def test_cli_fails_closed_on_missing_series(self):
        import oil_calibration_v3 as cli
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--series", str(ROOT / "missing.csv"), "--out",
                             str(ROOT / "build" / "does-not-matter")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
