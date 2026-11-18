"""Tests for the storage + futures-curve layer (src/amfe/markets/storage.py, docs/180).

The tests do not trust the report: they re-derive the normal cover, check the no-arbitrage
identities to 1e-9, force both curve signs from the stock level, run the two pre-registered windows,
and inspect the CLI output for raw data (the reports may contain aggregates and short paths only).
"""
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets import storage as st  # noqa: E402
from amfe.markets import oil_v3  # noqa: E402

TOL = 1e-9
DATA_CSV = ROOT / "data" / "oil" / "brent_monthly_eia.csv"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _numbers(node, trail="root"):
    """Yield (trail, list) for every JSON list whose entries are all numbers."""
    if isinstance(node, list):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in node):
            yield trail, node
        for index, item in enumerate(node):
            yield from _numbers(item, f"{trail}[{index}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _numbers(value, f"{trail}.{key}")


class ParamsAndCurveTest(unittest.TestCase):
    def test_normal_cover_is_derived_from_r_c_and_the_yield_curve(self):
        params = st.DEFAULT_PARAMS
        normal = params.normal_cover_days
        # the derivation: y(cover_normal) == r + c, so the carry is exactly zero there
        self.assertAlmostEqual(params.convenience_yield(normal) - params.cost_of_carry, 0.0,
                               delta=1e-12)
        self.assertAlmostEqual(params.carry(normal), 0.0, delta=1e-12)
        self.assertAlmostEqual(normal,
                               params.cover_scale_days
                               * math.log(params.convenience_max / params.cost_of_carry),
                               delta=1e-12)
        self.assertTrue(31.0 < normal < 33.0, normal)  # ~32.03 days with the declared defaults
        # the convenience yield is strictly decreasing in cover
        values = [params.convenience_yield(cover) for cover in (0.0, 10.0, 30.0, 32.0, 50.0)]
        self.assertTrue(all(values[i] > values[i + 1] for i in range(len(values) - 1)), values)
        self.assertGreater(values[0], params.cost_of_carry)

    def test_curve_identities_hold_to_tolerance(self):
        params = st.DEFAULT_PARAMS
        for spot in (18.38, 66.6, 123.93):
            for cover in (5.0, 26.0, 30.0, 32.03296512496319, 45.0):
                curve = st.build_curve(params, spot, cover)
                self.assertTrue(curve.ok, (spot, cover, curve.checks))
                # F(0) = S exactly (exp(0) = 1)
                self.assertEqual(params.forward_price(spot, cover, 0.0), spot)
                # F(T1) * F(T2) = F(T1 + T2) * S, relative residual <= 1e-9
                self.assertLessEqual(curve.residual_multiplicative, TOL)
                # the implied forward rate equals the carry, absolute residual <= 1e-9
                self.assertLessEqual(curve.residual_forward, TOL)
                self.assertTrue(curve.checks["multiplicative_identity"])
                self.assertTrue(curve.checks["forward_rate_identity"])
                # no negative or zero prices anywhere
                self.assertTrue(all(point.price > 0.0 for point in curve.points))
                # the 6-month rule is the formula, verified independently
                expected = spot * math.exp((params.cost_of_carry
                                            - params.convenience_yield(cover)) * 6.0 / 12.0)
                six = [point for point in curve.points if point.months == 6.0][0]
                self.assertLessEqual(abs(six.price - expected), 1e-9)

    def test_high_cover_gives_contango_and_an_increasing_curve(self):
        params = st.DEFAULT_PARAMS
        cover = params.normal_cover_days + 8.0
        self.assertGreater(params.carry(cover), 0.0)
        curve = st.build_curve(params, 70.0, cover)
        self.assertEqual(curve.regime, "contango")
        prices = [point.price for point in curve.points]
        self.assertTrue(all(price > 70.0 for price in prices), prices)
        self.assertTrue(all(prices[i] < prices[i + 1] for i in range(len(prices) - 1)), prices)
        self.assertGreater(curve.six_month_spread_usd, 0.0)
        self.assertEqual(params.regime(cover), "contango")

    def test_low_cover_gives_backwardation_and_a_decreasing_curve(self):
        params = st.DEFAULT_PARAMS
        cover = params.normal_cover_days - 8.0
        self.assertLess(params.carry(cover), 0.0)
        curve = st.build_curve(params, 70.0, cover)
        self.assertEqual(curve.regime, "backwardation")
        prices = [point.price for point in curve.points]
        self.assertTrue(all(price < 70.0 for price in prices), prices)
        self.assertTrue(all(prices[i] > prices[i + 1] for i in range(len(prices) - 1)), prices)
        self.assertLess(curve.six_month_spread_usd, 0.0)
        self.assertEqual(params.regime(cover), "backwardation")

    def test_regime_switches_exactly_at_the_normal_cover(self):
        params = st.DEFAULT_PARAMS
        normal = params.normal_cover_days
        self.assertEqual(params.regime(normal), "flat")
        self.assertEqual(params.regime(normal + 0.5), "contango")
        self.assertEqual(params.regime(normal - 0.5), "backwardation")
        flat = st.build_curve(params, 50.0, normal)
        self.assertEqual(flat.regime, "flat")
        self.assertTrue(all(abs(point.price - 50.0) <= TOL for point in flat.points))

    def test_curve_and_params_fail_closed_on_invalid_input(self):
        params = st.DEFAULT_PARAMS
        for bad_spot in (0.0, -1.0, float("nan")):
            with self.assertRaises(st.StorageError):
                st.build_curve(params, bad_spot, 30.0)
        with self.assertRaises(st.StorageError):
            st.build_curve(params, 50.0, -1.0)
        with self.assertRaises(st.StorageError):
            st.build_curve(params, 50.0, 30.0, tenors=(6,))
        with self.assertRaises(st.StorageError):
            st.build_curve(params, 50.0, 30.0, tenors=(6, 6))
        with self.assertRaises(st.StorageError):  # no normal cover exists
            st.StorageParams(convenience_max=0.065)
        with self.assertRaises(st.StorageError):
            st.StorageParams(loss_fraction_per_month=1.0)
        with self.assertRaises(st.StorageError):
            st.StorageParams(capacity_mb=0.0)
        with self.assertRaises(st.StorageError):
            params.forward_price(50.0, 30.0, -1.0)


class StoragePathTest(unittest.TestCase):
    def _outage_path(self):
        params = st.DEFAULT_PARAMS
        state = st.StorageState(stock_commercial_mb=2000.0, stock_spr_mb=150.0,
                                demand_mb_per_day=100.0)
        shock = st.SupplyShock.outage(6, 2.0, label="outage_2mbd")
        return params, state, st.simulate_storage(params, state, shock, 100.0)

    def test_stock_identity_and_mass_balance_hold_to_tolerance(self):
        params, state, path = self._outage_path()
        self.assertTrue(path.ok, path.checks)
        self.assertLessEqual(max(abs(item) for item in path.identity_residuals), TOL)
        self.assertLessEqual(abs(path.mass_residual), TOL)
        # telescoping identity: final - initial = build - draw - loss (in mb)
        total_delta = path.total_end_mb[-1] - state.total_mb
        flows = (math.fsum(path.build_mb) - math.fsum(path.draw_commercial_mb)
                 - math.fsum(path.draw_strategic_mb) - math.fsum(path.loss_mb))
        # the per-month residuals telescope to the same statement; the two agree to 1e-9
        self.assertLessEqual(abs(total_delta - flows), TOL)
        self.assertLess(path.cover_days[-1], path.cover_days[0])
        self.assertTrue(all(item == "backwardation" for item in path.regime))

    def test_unserved_demand_is_reported_as_unmet_not_negative_stock(self):
        params = st.DEFAULT_PARAMS
        state = st.StorageState(stock_commercial_mb=20.0, stock_spr_mb=5.0,
                                demand_mb_per_day=100.0)
        shock = st.SupplyShock.outage(3, 30.0, label="severe_outage")
        path = st.simulate_storage(params, state, shock, 100.0)
        self.assertTrue(path.ok, path.checks)
        self.assertGreater(path.unmet_total, 0.0)
        self.assertTrue(all(value >= 0.0 for value in path.total_end_mb))
        self.assertTrue(all(value >= 0.0 for value in path.unmet_mb_per_day))
        self.assertTrue(all(value <= demand + TOL
                            for value, demand in zip(path.unmet_mb_per_day,
                                                     path.demand_mb_per_day)))
        self.assertEqual(math.fsum(path.surplus_mb_per_day), 0.0)

    def test_full_tanks_report_surplus_instead_of_negative_prices(self):
        params = st.DEFAULT_PARAMS
        state = st.StorageState(stock_commercial_mb=3990.0, demand_mb_per_day=100.0)
        shock = st.SupplyShock(lost_supply_mb_per_day=(0.0,) * 3,
                               demand_shift_mb_per_day=(-5.0, -13.0, -13.0), label="collapse")
        path = st.simulate_storage(params, state, shock, 100.0)
        self.assertTrue(path.ok, path.checks)
        self.assertGreater(math.fsum(path.surplus_mb_per_day), 0.0)
        self.assertLessEqual(max(path.total_end_mb), params.capacity_mb + TOL)
        curve = st.build_curve(params, 20.0, path.cover_days[-1])
        self.assertTrue(all(point.price > 0.0 for point in curve.points))

    def test_simulate_fails_closed_on_invalid_state_and_shock(self):
        params = st.DEFAULT_PARAMS
        with self.assertRaises(st.StorageError):  # start above capacity
            st.simulate_storage(params, st.StorageState(stock_commercial_mb=5000.0),
                                st.SupplyShock.outage(1, 1.0), 100.0)
        with self.assertRaises(st.StorageError):  # demand wiped out by the shift
            st.simulate_storage(params, st.StorageState(),
                                st.SupplyShock(lost_supply_mb_per_day=(0.0,),
                                               demand_shift_mb_per_day=(-100.0,)), 100.0)
        with self.assertRaises(st.StorageError):  # negative lost supply
            st.SupplyShock(lost_supply_mb_per_day=(-1.0,))
        with self.assertRaises(st.StorageError):  # empty shock
            st.SupplyShock(lost_supply_mb_per_day=())
        with self.assertRaises(st.StorageError):  # mismatched vector lengths
            st.SupplyShock(lost_supply_mb_per_day=(1.0, 2.0),
                           added_supply_mb_per_day=(1.0,))
        with self.assertRaises(st.StorageError):  # negative base supply
            st.simulate_storage(params, st.StorageState(), st.SupplyShock.outage(1, 1.0), -100.0)
        with self.assertRaises(st.StorageError):  # zero demand state
            st.StorageState(stock_commercial_mb=10.0, demand_mb_per_day=0.0)

    def test_simulation_is_deterministic(self):
        params, state, first = self._outage_path()
        _params, _state, second = self._outage_path()
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(first.rows(), second.rows())
        other = st.simulate_storage(params, state, st.SupplyShock.outage(6, 3.0), 100.0)
        self.assertNotEqual(first.digest(), other.digest())


class EpisodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = oil_v3.load_series()

    def test_2020_episode_builds_stocks_and_turns_to_contango_in_three_of_four_months(self):
        result = st.run_episode(st.DEFAULT_PARAMS, st.episode("2020_storage_build"), self.series)
        document = result.as_dict()
        self.assertTrue(all(result.path.checks.values()), result.path.checks)
        self.assertGreater(result.path.total_end_mb[-1], result.path.total_end_mb[0])
        self.assertGreaterEqual(result.matching_months, 3)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(sum(1 for item in result.regimes if item == "contango"), 3)
        self.assertEqual(result.regimes[0], "backwardation")  # March: stocks still below normal
        # the monthly regime is the curve sign and the same as the path's own classification
        for index, regime in enumerate(result.regimes):
            self.assertEqual(regime, result.path.regime[index])
            self.assertEqual(regime, result.curves[index].regime)
        self.assertEqual(len(document["rows"]), 4)
        self.assertEqual([row["month"] for row in document["rows"]],
                         ["2020-03", "2020-04", "2020-05", "2020-06"])

    def test_low_cover_episode_is_backwardation_in_four_of_six_months(self):
        result = st.run_episode(st.DEFAULT_PARAMS, st.episode("2021_2022_low_cover"), self.series)
        self.assertTrue(all(result.path.checks.values()), result.path.checks)
        self.assertLess(result.path.total_end_mb[-1], result.path.total_end_mb[0])
        self.assertGreaterEqual(result.matching_months, 4)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(sum(1 for item in result.regimes if item == "backwardation"), 6)
        self.assertTrue(all(result.path.cover_days[index] < st.DEFAULT_PARAMS.normal_cover_days
                            for index in range(len(result.path.cover_days))))

    def test_episode_inputs_and_spot_alignment_fail_closed(self):
        with self.assertRaises(st.StorageError):
            st.episode("no_such_episode")
        target = st.episode("2020_storage_build")
        truncated = {"prices": {key: 50.0 for key in
                                [(2020, 3), (2020, 4), (2020, 5)]}}
        with self.assertRaises(st.StorageError):
            st.episode_spots(target, truncated)  # 2020-06 is missing
        self.assertEqual(len(st.episode_spots(target, self.series)), 4)
        self.assertEqual(st.episode_spots(target, self.series)[1], 18.38)  # 2020-04 observed
        with self.assertRaises(st.StorageError):
            st.episode_spots(target, {"prices": {}})
        with self.assertRaises(st.StorageError):
            st.StorageEpisode(episode_id="bad", name="bad", start_month="2020-13", months=1,
                              stock_start_mb=1.0, demand_mb_per_day=1.0, supply_mb_per_day=1.0)
        with self.assertRaises(st.StorageError):
            st.StorageEpisode(episode_id="bad", name="bad", start_month="2020-01", months=2,
                              stock_start_mb=1.0, demand_mb_per_day=1.0, supply_mb_per_day=1.0,
                              demand_shift_mb_per_day=(-1.0,))

    def test_cross_check_with_the_oil_module_is_within_ten_percent(self):
        cross = st.cross_check_with_oil()
        self.assertEqual(cross["oil_module"], "oil.brent")
        self.assertTrue(cross["checks"]["within_band"], cross["comparisons"])
        self.assertTrue(cross["checks"]["direction_consistent"])
        for name in ("net_supply_loss_mb_per_day", "strategic_release_mb"):
            item = cross["comparisons"][name]
            self.assertTrue(item["within_band"], (name, item))
            self.assertLessEqual(abs(item["ratio"] - 1.0), st.CROSS_CHECK_BAND)
        # the declared scenario is 20.5 mb/d lost with a 4.2 mb/d bypass ramp: 6*20.5 - 25.5
        self.assertAlmostEqual(cross["comparisons"]["net_supply_loss_mb_per_day"]["storage"],
                               97.5, delta=1e-9)
        self.assertIn("commercial_draw_mb", cross["structural_differences"])
        note = cross["structural_differences"]["commercial_draw_mb"]["note"]
        self.assertIn("structural difference", note)


class ReportAndCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = oil_v3.load_series()
        cls.report = st.build_report(cls.series)
        cls.document = cls.report.as_dict()

    def test_report_contract_status_and_checks(self):
        self.assertEqual(self.document["status"], "RESEARCH_ONLY")
        self.assertTrue(self.document["research_only"])
        self.assertEqual(self.document["module_id"], "oil.storage_curve")
        self.assertTrue(all(self.document["checks"].values()), self.document["checks"])
        self.assertEqual(len(self.document["digest"]), 64)
        self.assertEqual(self.document["digest"], self.report.digest())
        self.assertEqual(self.document["data"]["spot_source_class"], "observed")
        classes = {item["name"]: item["source_class"] for item in self.document["assumptions"]}
        self.assertEqual(classes["spot_usd_per_bbl"], "observed")
        self.assertEqual(classes["risk_free_rate"], "assumption")
        self.assertEqual(classes["stock_commercial_mb"], "assumption")
        self.assertIn("not an admission", self.document["note"])
        self.assertEqual(len(self.document["episodes"]), 2)
        self.assertTrue(all(item["verdict"] == "PASS" for item in self.document["episodes"]))

    def test_report_is_deterministic_and_spot_override_is_declared(self):
        again = st.build_report(self.series).as_dict()
        self.assertEqual(again, self.document)
        overridden = st.build_report(self.series, spot=40.0).as_dict()
        self.assertEqual(overridden["data"]["spot_usd_per_bbl"], 40.0)
        self.assertEqual(overridden["data"]["spot_source_class"],
                         "assumption (explicit override)")
        self.assertNotEqual(overridden["digest"], self.document["digest"])
        month = st.build_report(self.series, month="2020-04").as_dict()
        self.assertEqual(month["data"]["spot_month"], "2020-04")
        self.assertEqual(month["data"]["spot_usd_per_bbl"], 18.38)
        with self.assertRaises(st.StorageError):  # stock and cover are exclusive
            st.build_report(self.series, stock_mb=3000.0, cover_days=30.0)
        with self.assertRaises(st.StorageError):  # month outside the series
            st.build_report(self.series, month="1900-01")

    def test_report_contains_no_raw_series(self):
        self.assertFalse(self.document["data"]["raw_data_committed"])
        # no numeric array of 8+ values anywhere: only aggregates and short episode paths
        for trail, values in _numbers(self.document):
            self.assertLess(len(values), 8, f"{trail} has {len(values)} raw-looking values")
        text = json.dumps(self.document, ensure_ascii=False)
        self.assertNotIn('"prices"', text)
        for key in ("observations_values", "series", "raw"):
            self.assertNotIn(f'"{key}": [', text)
        # the provenance of the observed input is pinned by hash, not by the values
        observed = oil_v3.load_series()
        self.assertEqual(self.document["data"]["sha256"], _sha256(DATA_CSV))
        self.assertEqual(self.document["data"]["sha256"], observed["sha256"])

    def test_cli_writes_json_and_markdown_deterministically(self):
        import storage_futures as cli
        before = _sha256(DATA_CSV)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as first, \
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as second:
            with redirect_stdout(io.StringIO()):
                code = cli.main(["--out", first, "--quiet"])
            self.assertEqual(code, 0)
            for name in ("report.json", "report.md", "params.json", "params.sha256.txt"):
                self.assertTrue((Path(first) / name).is_file(), name)
            document = json.loads((Path(first) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "RESEARCH_ONLY")
            self.assertTrue(all(document["checks"].values()))
            markdown = (Path(first) / "report.md").read_text(encoding="utf-8")
            self.assertIn("RESEARCH_ONLY", markdown)
            self.assertIn("F(T) = S·exp((r + c − y(cover))·T/12)", markdown)
            self.assertIn("backwardation", markdown)
            self.assertIn("2020_storage_build", markdown)
            self.assertEqual(len((Path(first) / "params.sha256.txt")
                                 .read_text(encoding="utf-8").strip()), 64)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--out", second, "--quiet"]), 0)
            self.assertEqual((Path(first) / "report.json").read_bytes(),
                             (Path(second) / "report.json").read_bytes())
            self.assertEqual((Path(first) / "report.md").read_bytes(),
                             (Path(second) / "report.md").read_bytes())
        self.assertEqual(_sha256(DATA_CSV), before)  # the tool never touches the data file

    def test_cli_json_mode_and_fail_closed(self):
        import storage_futures as cli
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli.main(["--json"]), 0)
        document = json.loads(buffer.getvalue())
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            missing = cli.main(["--series", str(ROOT / "missing.csv"),
                                "--out", str(ROOT / ".tmp-storage-missing")])
            both = cli.main(["--spot", "40", "--month", "2020-04",
                             "--out", str(ROOT / ".tmp-storage-missing")])
            too_big = cli.main(["--stock", "9999", "--out", str(ROOT / ".tmp-storage-missing")])
        self.assertEqual(missing, 2)
        self.assertEqual(both, 2)
        self.assertEqual(too_big, 2)
        self.assertFalse((ROOT / ".tmp-storage-missing").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
