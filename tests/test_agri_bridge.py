"""Tests for the agri/soy -> MRIO bridge (G2, docs/145, docs/153).

The suite is offline and mostly synthetic: the synthetic cost structure exercises the Leontief
price model, the corner ordering and the fail-closed paths in milliseconds; the real MRIO cache
(if present under ``build/agri_bridge/``) is validated once for units, provenance and cost-share
bounds.  The African block is checked against the agri module directly, and the price/quantity
separation is asserted explicitly (the bridge must not claim any volume or output effect).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import agri_bridge as ab  # noqa: E402
from amfe.markets_agri import AgriMarketError, SoyAgriModule  # noqa: E402

CACHE = ROOT / "build" / "agri_bridge" / "mrio_cost_structure.json"
DOCS = ROOT / "docs" / "153_Мост_агро_MRIO.md"


def scenario() -> dict:
    return ab.run_scenario()


def synthetic_report(**overrides) -> dict:
    structure = ab.synthetic_cost_structure()
    run = scenario()
    params = dict(structure=structure, scenario=run,
                  base_soy_price=run["base_soy_price"], base_meal_price=run["base_meal_price"])
    params.update(overrides)
    return ab.run_bridge(**params)


class AssumptionsTests(unittest.TestCase):
    def test_default_assumptions_are_ordered_and_disclosed(self):
        assumptions = ab.BridgeAssumptions()
        payload = assumptions.to_dict()
        self.assertEqual(payload["source_class"], "assumption")
        self.assertLessEqual(payload["pass_through"][0], payload["pass_through"][1])
        self.assertGreaterEqual(payload["feed_cost_share_animal"] + payload["va_share_animal"],
                                0.0)
        self.assertLessEqual(payload["feed_cost_share_animal"] + payload["va_share_animal"], 1.0)

    def test_invalid_assumptions_fail_closed(self):
        with self.assertRaises(ab.BridgeError):
            ab.BridgeAssumptions(pass_through_low=0.9, pass_through_high=0.5)
        with self.assertRaises(ab.BridgeError):
            ab.BridgeAssumptions(food_cpi_weight_low=0.4, food_cpi_weight_high=0.2)
        with self.assertRaises(ab.BridgeError):
            ab.BridgeAssumptions(feed_cost_share_animal=0.9, va_share_animal=0.5)
        with self.assertRaises(ab.BridgeError):
            ab.BridgeAssumptions(soybean_share_of_feed=0.8, soymeal_share_of_feed=0.5)
        with self.assertRaises(ab.BridgeError):
            ab.BridgeAssumptions(soy_cost_share_food=1.2)


class CostStructureTests(unittest.TestCase):
    def test_synthetic_structure_is_valid_and_deterministic(self):
        first = ab.synthetic_cost_structure()
        second = ab.synthetic_cost_structure()
        self.assertEqual(first.groups, ab.MRIO_GROUPS)
        self.assertEqual(first.dataset["units"], "USD_mn")
        self.assertEqual(first.digest, second.digest)
        np.testing.assert_allclose(np.asarray(first.world_a), np.asarray(second.world_a))
        self.assertEqual(len(first.countries), 3)
        payload = first.payload()
        restored = ab.CostStructure.from_payload(payload)
        self.assertEqual(restored.digest, first.digest)

    def test_structure_validation_fails_closed(self):
        structure = ab.synthetic_cost_structure()
        with self.assertRaises(ab.BridgeError):
            ab.CostStructure(
                schema=ab.CACHE_SCHEMA,
                dataset=dict(structure.dataset, units="USD"),
                countries=structure.countries, groups=ab.MRIO_GROUPS,
                world_a=np.asarray(structure.world_a), country_a=dict(structure.country_a),
                country_output_usd_mn=dict(structure.country_output_usd_mn),
                world_output_usd_mn=dict(structure.world_output_usd_mn))
        bad_column = np.array([[0.6, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.0, 0.0]])
        with self.assertRaises(ab.BridgeError):
            ab.CostStructure(
                schema=ab.CACHE_SCHEMA, dataset=dict(structure.dataset), countries=("AAA",),
                groups=ab.MRIO_GROUPS, world_a=bad_column, country_a={"AAA": bad_column},
                country_output_usd_mn={"AAA": {"AGRI": 1.0, "FOOD": 1.0, "OTHER": 1.0}},
                world_output_usd_mn={"AGRI": 1.0, "FOOD": 1.0, "OTHER": 1.0})
        with self.assertRaises(ab.BridgeError):
            ab.CostStructure(schema="WRONG", dataset=dict(structure.dataset),
                             countries=structure.countries, groups=ab.MRIO_GROUPS,
                             world_a=np.asarray(structure.world_a),
                             country_a=dict(structure.country_a),
                             country_output_usd_mn=dict(structure.country_output_usd_mn),
                             world_output_usd_mn=dict(structure.world_output_usd_mn))

    def test_augmented_matrix_has_a_virtual_animal_sector(self):
        structure = ab.synthetic_cost_structure()
        assumptions = ab.BridgeAssumptions()
        matrix = structure.augmented_matrix(assumptions)
        self.assertEqual(matrix.shape, (4, 4))
        self.assertAlmostEqual(matrix[0, 3], assumptions.feed_cost_share_animal, places=12)
        self.assertAlmostEqual(matrix[2, 3],
                               1.0 - assumptions.feed_cost_share_animal
                               - assumptions.va_share_animal, places=12)
        column_sums = matrix.sum(axis=0)
        self.assertTrue(np.all(column_sums < 1.0))
        self.assertAlmostEqual(column_sums[3], 1.0 - assumptions.va_share_animal, places=12)


class LeontiefTests(unittest.TestCase):
    def test_zero_cost_push_gives_zero_prices(self):
        structure = ab.synthetic_cost_structure()
        prices = ab.solve_leontief_prices(structure.augmented_matrix(ab.BridgeAssumptions()),
                                          np.zeros(4))
        np.testing.assert_allclose(prices, np.zeros(4), atol=1e-15)

    def test_price_model_is_linear_and_residual_free(self):
        matrix = np.array([[0.10, 0.25, 0.02, 0.45],
                           [0.05, 0.12, 0.03, 0.00],
                           [0.20, 0.22, 0.35, 0.40],
                           [0.00, 0.00, 0.00, 0.00]])
        push = np.array([0.0, 0.02, 0.0, 0.05])
        prices = ab.solve_leontief_prices(matrix, push)
        residual = (np.eye(4) - matrix.T) @ prices - push
        self.assertLess(float(np.max(np.abs(residual))), 1e-12)
        doubled = ab.solve_leontief_prices(matrix, 2.0 * push)
        np.testing.assert_allclose(doubled, 2.0 * prices, atol=1e-12)
        self.assertTrue(np.all(prices >= 0.0))

    def test_leontief_fail_closed(self):
        with self.assertRaises(ab.BridgeError):
            ab.solve_leontief_prices(np.zeros((3, 2)), np.zeros(2))
        with self.assertRaises(ab.BridgeError):
            ab.solve_leontief_prices(np.zeros((2, 2)), np.zeros(3))
        with self.assertRaises(ab.BridgeError):
            ab.solve_leontief_prices(np.array([[np.nan, 0.0], [0.0, 0.0]]), np.zeros(2))
        with self.assertRaises(ab.BridgeError):
            ab.solve_leontief_prices(np.array([[1.0, 0.0], [0.0, 0.0]]), np.ones(2))
        with self.assertRaises(ab.BridgeError):
            ab.solve_leontief_prices(np.array([[-0.1, 0.0], [0.0, 0.0]]), np.ones(2))

    def test_direct_cost_push_uses_the_declared_shares(self):
        assumptions = ab.BridgeAssumptions()
        push = ab.direct_cost_push(0.10, 0.20, assumptions)
        self.assertEqual(push[0], 0.0)
        self.assertEqual(push[2], 0.0)
        self.assertAlmostEqual(push[1], assumptions.soy_cost_share_food * 0.10
                               + assumptions.meal_cost_share_food * 0.20, places=12)
        expected_animal = assumptions.feed_cost_share_animal * (
            assumptions.soybean_share_of_feed * 0.10 + assumptions.soymeal_share_of_feed * 0.20)
        self.assertAlmostEqual(push[3], expected_animal, places=12)
        with self.assertRaises(ab.BridgeError):
            ab.direct_cost_push(float("nan"), 0.0, assumptions)
        with self.assertRaises(ab.BridgeError):
            ab.direct_cost_push(-1.0, 0.0, assumptions)


class BridgeReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = synthetic_report()
        cls.agri = SoyAgriModule().run().to_dict()

    def test_price_channel_numbers_and_corner_ordering(self):
        prices = self.report["prices"]
        self.assertLess(prices["soybeans"]["scenario_range"]["low"],
                        prices["soybeans"]["scenario_range"]["high"])
        self.assertGreater(prices["soybeans"]["change_pct"]["high"], 0.0)
        low = self.report["leontief"]["producer_price_change_pct"]["low"]
        high = self.report["leontief"]["producer_price_change_pct"]["high"]
        for group in ab.PRICE_GROUPS:
            self.assertGreaterEqual(low[group], -1e-12)
            self.assertLessEqual(low[group], high[group] + 1e-12)
        self.assertGreater(high["ANIMAL"], high["FOOD"])
        self.assertGreater(high["FOOD"], high["AGRI"])

    def test_consumer_effect_is_a_range_and_not_a_point_estimate(self):
        consumer = self.report["consumer_food_prices"]
        self.assertFalse(consumer["point_estimate"])
        self.assertLess(consumer["range_pp"]["low"], consumer["range_pp"]["high"])
        self.assertGreater(consumer["range_pp"]["low"], 0.0)
        for corner in ("low", "high"):
            block = consumer["corners"][corner]["food_cpi_contribution_pp"]
            self.assertLessEqual(block["low"], block["high"])

    def test_africa_block_matches_the_agri_module_and_is_not_double_counted(self):
        africa = self.report["africa"]
        expected = self.agri["effects"]["africa"]
        self.assertAlmostEqual(africa["import_bill_change_usd_mn"]["low"],
                               expected["import_bill_change_usd_mn"]["low"], places=6)
        self.assertAlmostEqual(africa["import_bill_change_usd_mn"]["high"],
                               expected["import_bill_change_usd_mn"]["high"], places=6)
        self.assertAlmostEqual(africa["food_inflation_pp_range"]["high"],
                               expected["food_inflation_pp_range"]["high"], places=6)
        self.assertFalse(africa["included_in_world_totals"])
        self.assertIn("not added", africa["no_double_counting"])
        self.assertEqual(africa["source"], "amfe.markets_agri.SoyAgriModule")

    def test_price_and_quantity_channels_are_separate(self):
        separation = self.report["separation"]
        self.assertTrue(separation["price_based"])
        quantity = separation["quantity_based"]
        self.assertFalse(quantity["modelled"])
        self.assertTrue(quantity["volumes_held_constant"])
        for claim in ("output loss", "volume change", "GDP effect"):
            self.assertIn(claim, quantity["not_claimed"])
        self.assertIn("no quantity claims", separation["statement"])
        self.assertNotIn("quantity_change_pct", self.report)

    def test_metadata_units_and_no_admission_claims(self):
        provenance = self.report["provenance"]
        self.assertTrue(provenance["not_a_forecast"])
        self.assertTrue(provenance["no_admission_claim"])
        self.assertIn("OECD", provenance["mrio_licence"])
        self.assertIn("USD_mn", self.report["data"]["units"])
        self.assertEqual(self.report["data"]["mrio"]["units"], "USD_mn")
        self.assertEqual(self.report["data"]["assumptions"]["source_class"], "assumption")
        self.assertIn("not an admission", " ".join(self.report["limitations"]))

    def test_report_is_deterministic(self):
        again = synthetic_report()
        self.assertEqual(self.report["digest"], again["digest"])
        self.assertEqual(json.dumps(self.report, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_markdown_report_renders_separation_and_limitations(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = ab.write_report(self.report, tmp)
            markdown = Path(written["markdown"]).read_text(encoding="utf-8")
            self.assertIn("Quantity channel: NOT modelled", markdown)
            self.assertIn("no double counting", markdown)
            self.assertIn("not a point estimate", markdown)
            self.assertIn("Limitations", markdown)

    def test_scenario_contract_fails_closed(self):
        with self.assertRaises(ab.BridgeError):
            ab.run_bridge(ab.synthetic_cost_structure(), scenario={"name": "empty"})
        broken = dict(scenario())
        broken["agri"] = dict(broken["agri"])
        broken["agri"]["price_ranges"] = {}
        with self.assertRaises(ab.BridgeError):
            ab.run_bridge(ab.synthetic_cost_structure(), scenario=broken,
                          base_soy_price=420.0, base_meal_price=330.0)


class RealCacheTests(unittest.TestCase):
    @unittest.skipUnless(CACHE.is_file(), "real MRIO cache not built yet (run --rebuild-cache)")
    def test_real_cost_structure_units_provenance_and_shares(self):
        structure, origin = ab.load_or_build_structure(CACHE, ROOT / "no-such-dataset.json")
        self.assertEqual(origin, "cache")
        self.assertEqual(structure.dataset["units"], "USD_mn")
        self.assertRegex(structure.dataset["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(structure.countries), int(structure.dataset["countries"]))
        matrix = np.asarray(structure.world_a)
        self.assertTrue(np.all(np.isfinite(matrix)))
        self.assertTrue(np.all(matrix.sum(axis=0) < 1.0))
        self.assertIn("OECD", str(structure.dataset["source"]))
        report = ab.run_bridge(structure, scenario=scenario())
        self.assertGreater(report["prices"]["soybeans"]["change_pct"]["high"], 0.0)
        self.assertEqual(len(report["countries"]["top_by_consumer_food_effect"]), 8)
        self.assertEqual(report["countries"]["count"], len(structure.countries))
        for row in report["countries"]["top_by_consumer_food_effect"]:
            self.assertLessEqual(row["food_cpi_contribution_pp"]["low"],
                                 row["food_cpi_contribution_pp"]["high"])

    def test_cache_and_dataset_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            with self.assertRaises(ab.BridgeError):
                ab.load_or_build_structure(cache, Path(tmp) / "missing.json")
            cache.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ab.BridgeError):
                ab.load_or_build_structure(cache, Path(tmp) / "missing.json")
            cache.write_text(json.dumps({"schema": ab.CACHE_SCHEMA}), encoding="utf-8")
            with self.assertRaises(ab.BridgeError):
                ab.load_or_build_structure(cache, Path(tmp) / "missing.json")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, timeout=180):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "agri_bridge.py"), *args],
                              capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))

    def test_self_test_and_help(self):
        result = self.run_cli("--self-test", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], "AMFE-AGRI-BRIDGE-SELFTEST/1.0.0")
        help_done = self.run_cli("--help")
        self.assertEqual(help_done.returncode, 0)
        self.assertIn("usage", help_done.stdout.lower())

    def test_synthetic_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("--synthetic", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "OK")
            self.assertEqual(summary["origin"], "synthetic")
            self.assertTrue((Path(tmp) / "agri_bridge.json").is_file())
            self.assertTrue((Path(tmp) / "agri_bridge.md").is_file())
            report = json.loads((Path(tmp) / "agri_bridge.json").read_text(encoding="utf-8"))
            self.assertFalse(report["separation"]["quantity_based"]["modelled"])

    def test_missing_dataset_without_cache_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("--dataset", str(Path(tmp) / "missing.json"),
                                  "--cache", str(Path(tmp) / "cache.json"),
                                  "--out", str(Path(tmp) / "out"))
            self.assertEqual(result.returncode, 2)
            self.assertIn("BLOCKED_BRIDGE", result.stderr)
            self.assertFalse((Path(tmp) / "out").exists())


class DocsAndHonestyTests(unittest.TestCase):
    def test_docs_153_exists_and_states_the_limits(self):
        self.assertTrue(DOCS.is_file(), f"missing {DOCS}")
        text = DOCS.read_text(encoding="utf-8")
        for token in ("цен", "количеств", "Африк", "допущени", "Леонтьев"):
            self.assertIn(token, text)
        self.assertIn("не допуск", text)
        lowered = text.lower()
        for forbidden in ("гарантированн", "прибыльн", "рекомендуем покупать", "точечный прогноз цены"):
            self.assertNotIn(forbidden, lowered)

    def test_docs_reports_the_module_test_command(self):
        text = DOCS.read_text(encoding="utf-8")
        self.assertIn("tests/test_agri_bridge.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
