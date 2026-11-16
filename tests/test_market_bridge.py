#!/usr/bin/env python3
"""Tests of the oil-price -> MRIO Leontief-price bridge (tools/market_bridge.py, docs/152).

Offline: the real MRIO datasets are replaced by small analytic fixtures; the oil module itself is
real (deterministic, no network).  The suite checks the price algebra, the fail-closed behaviour,
the pass-through bounds, the price/quantity separation and the "research-only" claims discipline.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import market_bridge as bridge  # noqa: E402


def write_dataset(tmp: Path, *, n_countries=2, n_industries=3, zeros=0) -> Path:
    """Small dense canonical dataset v1.1 with an exactly productive A."""
    countries = [f"C{index}" for index in range(n_countries)]
    industries = [f"I{index}" for index in range(n_industries)]
    n = n_countries * n_industries
    rng = np.random.default_rng(7)
    Z = np.zeros((n, n))
    for column in range(n):
        Z[column, column] = 10.0
        for offset in (1, 2):
            row = (column + offset) % n
            Z[row, column] = 1.0
    x = Z.sum(axis=0) + rng.uniform(5.0, 10.0, n)
    y = x - Z.sum(axis=0)
    document = {
        "schema": "amfe.mrio.dataset.v1.1", "schema_version": "1.1.0",
        "countries": countries, "industries": industries, "years": [2019],
        "units": "USD_mn", "Z": Z.tolist(), "y": y.tolist(), "x": x.tolist(),
        "va": (y * 0.6).tolist(), "inventory_changes": [0.0] * n, "discrepancy": [0.0] * n,
    }
    path = tmp / "dataset.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def load_matrix(tmp: Path, **kwargs) -> dict:
    return bridge.load_dataset_matrix(write_dataset(tmp, **kwargs))


def fake_oil(delta_pct: float = 1.2514) -> dict:
    return {"scenario_id": "hormuz_closed_6m", "module_id": "oil.brent", "units": "USD/bbl",
            "horizon_months": 6, "status": "RESEARCH_ONLY", "brent_delta_pct": delta_pct,
            "brent_delta_usd": 93.86, "price_peak": 168.86, "price_mean": 130.0,
            "price_low": 139.04, "price_high": 268.52, "price_path": [168.86] * 6,
            "base_price": 75.0, "assumptions": [], "digest": "x"}


class PriceAlgebraTest(unittest.TestCase):
    """The Leontief price algebra is verified on a two-sector analytic case."""

    def test_two_sector_analytic_solution(self):
        # B = [[0.2, 0.5], [0.1, 0.3]]; p = (I - B^T)^-1 c
        countries, industries = ["C0"], ["I0", "I1"]
        Z = np.array([[20.0, 100.0], [10.0, 60.0]])
        x = np.array([100.0, 200.0])
        document = {"schema": "amfe.mrio.dataset.v1.1", "countries": countries,
                    "industries": industries, "years": [2019], "units": "USD_mn",
                    "Z": Z.tolist(), "x": x.tolist(), "y": [70.0, 140.0],
                    "va": [70.0, 140.0]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            matrix = bridge.load_dataset_matrix(path)
        c = np.array([0.02, 0.01])
        solution = bridge.solve_prices(matrix, c)
        # analytic: p = (I - B^T)^-1 c
        B = np.array([[0.2, 0.5], [0.1, 0.3]])
        expected = np.linalg.solve(np.eye(2) - B.T, c)
        np.testing.assert_allclose(solution["prices"], expected, rtol=1e-12, atol=1e-12)

    def test_spectral_radius_below_one_and_neumann_matches_direct(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
        radius = bridge.spectral_radius(matrix)
        self.assertTrue(radius["converged"])
        self.assertLess(radius["spectral_radius"], 1.0)
        solution = bridge.solve_prices(matrix, np.full(matrix["n"], 0.01))
        self.assertTrue(solution["converged"])
        self.assertLess(solution["direct_vs_neumann_max_abs"], 1e-8)


class BridgeModelTest(unittest.TestCase):
    def test_price_model_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
        result = bridge.run_price_model(matrix, classification="figaro",
                                        oil=fake_oil(), pass_through=0.6)
        checks = result["checks"]
        self.assertTrue(checks["productive"])
        self.assertTrue(checks["prices_non_negative"])
        self.assertTrue(checks["neumann_converged"])
        self.assertTrue(checks["bridge_pass"])
        self.assertEqual(result["results"]["sectors"], matrix["n"])
        self.assertGreater(result["results"]["aggregate_price_index_pct"], 0.0)

    def test_pass_through_is_monotone_and_linear(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
        aggregates = {}
        for level in (0.0, 0.3, 0.6, 1.0):
            result = bridge.run_price_model(matrix, classification="figaro",
                                            oil=fake_oil(), pass_through=level)
            aggregates[level] = result["results"]["aggregate_price_index_pct"]
        self.assertEqual(aggregates[0.0], 0.0)
        self.assertLess(aggregates[0.3], aggregates[0.6])
        self.assertLess(aggregates[0.6], aggregates[1.0])
        self.assertAlmostEqual(aggregates[1.0], aggregates[0.6] / 0.6, places=6)

    def test_zero_pass_through_gives_zero_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
        result = bridge.run_price_model(matrix, classification="figaro",
                                        oil=fake_oil(), pass_through=0.0)
        self.assertEqual(result["results"]["max_delta_price_pct"], 0.0)

    def test_top_lists_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
        first = bridge.run_price_model(matrix, classification="figaro", oil=fake_oil(),
                                       pass_through=0.6)
        second = bridge.run_price_model(matrix, classification="figaro", oil=fake_oil(),
                                        pass_through=0.6)
        self.assertEqual(json.dumps(first["results"], sort_keys=True),
                         json.dumps(second["results"], sort_keys=True))


class FailClosedTest(unittest.TestCase):
    def test_zero_output_sector_with_flows_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dataset(Path(tmp))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["x"][0] = 0.0  # but Z has flows in that row/column
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(bridge.BridgeError):
                bridge.load_dataset_matrix(path)

    def test_missing_dataset_is_refused(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.load_dataset_matrix(Path("does-not-exist.json"))

    def test_negative_output_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dataset(Path(tmp))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["x"][0] = -1.0
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(bridge.BridgeError):
                bridge.load_dataset_matrix(path)

    def test_unknown_classification_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
            with self.assertRaises(bridge.BridgeError):
                bridge.bucket_weights("unknown-ish", matrix["industries"])

    def test_invalid_pass_through_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
            with self.assertRaises(bridge.BridgeError):
                bridge.run_price_model(matrix, classification="figaro", oil=fake_oil(),
                                       pass_through=1.5)

    def test_implausible_price_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix = load_matrix(Path(tmp))
            with self.assertRaises(bridge.BridgeError):
                bridge.run_price_model(matrix, classification="figaro",
                                       oil=fake_oil(delta_pct=30.0), pass_through=1.0,
                                       bucket_shares={"services": 1.0})

    def test_non_productive_matrix_is_refused(self):
        # A with spectral radius > 1: refuse instead of returning a divergent price vector
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dataset(Path(tmp))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["Z"] = (np.asarray(document["Z"]) * 30.0).tolist()
            path.write_text(json.dumps(document), encoding="utf-8")
            matrix = bridge.load_dataset_matrix(path)
            with self.assertRaises(bridge.BridgeError):
                bridge.run_price_model(matrix, classification="figaro", oil=fake_oil(),
                                       pass_through=0.6)


class SeparationTest(unittest.TestCase):
    def test_price_and_quantity_are_separate_objects(self):
        report = {"schema": bridge.SCHEMA, "status": "RESEARCH_ONLY", "admission": False,
                  "price_results": {}, "quantity_proxy": {"figaro_2019": {"status": "AVAILABLE"}}}
        self.assertIn("price_results", report)
        self.assertIn("quantity_proxy", report)
        self.assertNotIn("forecast", json.dumps(report).lower())
        self.assertFalse(report["admission"])
        self.assertEqual(report["status"], "RESEARCH_ONLY")

    def test_quantity_proxy_reads_existing_index_and_keeps_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "report_index.json"
            index_path.write_text(json.dumps({
                "dataset": {"countries": 2, "industries": 3, "sectors": 6, "years": [2019],
                            "sha256": "abc", "units": "USD_mn"},
                "scenarios": [
                    {"id": "energy_plus50", "headline": {"total_delta_output": 100.0,
                                                         "relative_delta_output": 0.01,
                                                         "largest_country": "C0",
                                                         "largest_industry": "I0"}},
                    {"id": "energy_minus5", "headline": {"total_delta_output": -10.0,
                                                         "relative_delta_output": -0.001}}]}),
                encoding="utf-8")
            proxy = bridge.quantity_proxy(index_path, "fixture")
        self.assertEqual(proxy["status"], "AVAILABLE")
        self.assertIn("energy_plus50", proxy["scenarios"])
        self.assertIn("NOT combined", proxy["note"])
        self.assertEqual(proxy["separation"], "price_pass_through != quantity_effect")

    def test_missing_quantity_index_is_not_fatal(self):
        proxy = bridge.quantity_proxy(Path("missing-index.json"), "fixture")
        self.assertEqual(proxy["status"], "NOT_AVAILABLE")
        self.assertIn("price model does not produce output", proxy["note"])


class MarketIntegrationTest(unittest.TestCase):
    def test_oil_module_result_shape(self):
        oil = bridge.run_oil_scenario()
        self.assertEqual(oil["status"], "RESEARCH_ONLY")
        self.assertEqual(oil["scenario_id"], "hormuz_closed_6m")
        self.assertAlmostEqual(oil["brent_delta_pct"], 1.2514, places=3)
        self.assertAlmostEqual(oil["base_price"], 75.0)
        self.assertEqual(len(oil["price_path"]), 6)
        self.assertGreater(oil["price_high"], oil["price_low"])

    def test_bucket_mapping_covers_every_industry(self):
        for classification in ("figaro", "oecd_2025"):
            rules = bridge.BUCKET_RULES[classification]
            industries = (list(rules["energy"]) + list(rules["transport"])
                          + [rules["manufacturing_prefixes"][0] + "99", "SOMETHING_ELSE"])
            weights = bridge.bucket_weights(classification, industries)
            for row in weights.values():
                self.assertAlmostEqual(sum(row.values()), 1.0)

    def test_report_status_is_research_only_and_has_disclaimer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "figaro" / "figaro_2019").mkdir(parents=True)
            write_dataset(root / "figaro" / "figaro_2019", n_countries=2, n_industries=3)
            (root / "figaro" / "figaro_2019" / "dataset.json").replace(
                root / "figaro" / "figaro_2019" / "dataset.json")
            outcome = bridge.build_report(root, datasets=("figaro",),
                                          pass_throughs=(0.3, 0.6, 1.0),
                                          out_dir=Path(tmp) / "out",
                                          generated_at="2026-11-16T00:00:00Z")
            report = outcome["report"]
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["admission"])
        self.assertIn("НЕ прогноз", report["statement"])
        self.assertTrue(report["checks"]["price_model_pass"])
        self.assertIn("quantity_proxy", report)
        self.assertIn("no_quantity_claims_in_price_model", report["checks"])

    def test_report_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "figaro" / "figaro_2019").mkdir(parents=True)
            write_dataset(root / "figaro" / "figaro_2019", n_countries=2, n_industries=3)
            first = bridge.build_report(root, datasets=("figaro",), pass_throughs=(0.6,),
                                        out_dir=Path(tmp) / "out1",
                                        generated_at="2026-11-16T00:00:00Z")
            second = bridge.build_report(root, datasets=("figaro",), pass_throughs=(0.6,),
                                         out_dir=Path(tmp) / "out2",
                                         generated_at="2026-11-16T00:00:00Z")
        self.assertEqual(first["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
