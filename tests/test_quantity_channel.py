#!/usr/bin/env python3
"""Tests of the quantity channel (src/amfe/markets/demand.py, tools/quantity_bridge.py, docs/159).

Offline: MRIO fixtures are small synthetic canonical datasets; the oil module is the real
deterministic module (no network).  The suite checks the elasticity algebra, the failure bounds,
the Leontief output algebra, determinism, the separation of channels and the research-only status.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.markets import demand  # noqa: E402
import market_bridge as mb  # noqa: E402
import quantity_bridge as qb  # noqa: E402


def synthetic_dataset(path: Path, *, countries=("C0", "C1"), industries=None) -> Path:
    """Small canonical dataset with a productive A and non-negative final demand."""
    industries = list(industries or ["CPA_B", "CPA_C19", "CPA_D35", "CPA_H49", "CPA_C10-12",
                                     "CPA_G47"])
    n = len(countries) * len(industries)
    Z = np.zeros((n, n))
    for index in range(n):
        Z[index, index] = 4.0
    x = np.full(n, 20.0)
    y = np.full(n, 16.0)
    document = {
        "schema": "amfe.mrio.dataset.v1.1", "schema_version": "1.1.0",
        "countries": list(countries), "industries": industries, "years": [2019],
        "units": "USD_mn", "Z": Z.tolist(), "y": y.tolist(), "x": x.tolist(),
        "va": (y * 0.5).tolist(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class ElasticityTest(unittest.TestCase):
    def test_short_long_ramp(self):
        # month 1 is already partway along the ramp; month 6 is the long-run value
        short = demand.elasticity_at("energy", 1, horizon_months=6, ramp_months=6)
        long = demand.elasticity_at("energy", 6, horizon_months=6, ramp_months=6)
        self.assertAlmostEqual(short, demand.FINAL_DEMAND_ELASTICITIES["energy"]["short"]
                               + (demand.FINAL_DEMAND_ELASTICITIES["energy"]["long"]
                                  - demand.FINAL_DEMAND_ELASTICITIES["energy"]["short"]) / 6.0)
        self.assertAlmostEqual(long, demand.FINAL_DEMAND_ELASTICITIES["energy"]["long"])

    def test_elasticity_path_is_monotone_and_deterministic(self):
        first = demand.elasticity_path("transport", horizon_months=6, ramp_months=3)
        second = demand.elasticity_path("transport", horizon_months=6, ramp_months=3)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        self.assertLess(abs(first[0]), abs(first[-1]))  # more negative over time
        self.assertAlmostEqual(first[-1], demand.FINAL_DEMAND_ELASTICITIES["transport"]["long"])

    def test_default_elasticities_within_bounds(self):
        table = demand.validate_elasticities()
        for values in table.values():
            for value in values.values():
                self.assertGreaterEqual(value, demand.ELASTICITY_BOUNDS[0])
                self.assertLessEqual(value, demand.ELASTICITY_BOUNDS[1])

    def test_out_of_bounds_elasticity_is_refused(self):
        with self.assertRaises(demand.BoundsError):
            demand.validate_elasticities({"energy": {"short": 0.5, "long": -0.5}})

    def test_unknown_group_is_refused(self):
        with self.assertRaises(demand.DemandError):
            demand.elasticity_at("no-such-group", 1)

    def test_classification_covers_both_mriо_classifications(self):
        self.assertEqual(demand.classify("figaro", "CPA_C19"), "energy")
        self.assertEqual(demand.classify("figaro", "CPA_H50"), "transport")
        self.assertEqual(demand.classify("figaro", "CPA_C24"), "manufacturing")
        self.assertEqual(demand.classify("figaro", "CPA_G47"), "services")
        self.assertEqual(demand.classify("oecd_2025", "C19"), "energy")
        self.assertEqual(demand.classify("oecd_2025", "D"), "energy")
        self.assertEqual(demand.classify("oecd_2025", "H52"), "transport")
        self.assertEqual(demand.classify("oecd_2025", "A01"), "services")
        with self.assertRaises(demand.DemandError):
            demand.classify("unknown-classification", "X")


class DemandResponseTest(unittest.TestCase):
    def test_price_increase_reduces_demand(self):
        response = demand.demand_response([0.10, 0.0], [100.0, 100.0], ["energy", "services"])
        self.assertLess(response["aggregate"]["delta_short"], 0.0)
        self.assertLess(response["aggregate"]["delta_long"], 0.0)
        self.assertEqual(response["by_sector"][1]["delta_long"], 0.0)

    def test_price_fall_raises_demand(self):
        response = demand.demand_response([-0.10], [100.0], ["energy"])
        self.assertGreater(response["aggregate"]["delta_long"], 0.0)

    def test_demand_response_is_linear_in_price(self):
        small = demand.demand_response([0.05], [100.0], ["energy"])
        large = demand.demand_response([0.10], [100.0], ["energy"])
        self.assertAlmostEqual(large["aggregate"]["delta_long"],
                               2.0 * small["aggregate"]["delta_long"], places=9)

    def test_long_effect_is_at_least_short_effect_in_abs(self):
        response = demand.demand_response([0.1], [100.0], ["transport"])
        self.assertGreaterEqual(abs(response["aggregate"]["delta_long"]),
                                abs(response["aggregate"]["delta_short"]))

    def test_monthly_path_has_horizon_entries(self):
        response = demand.demand_response([0.1], [100.0], ["manufacturing"], horizon_months=6,
                                          ramp_months=4)
        self.assertEqual(len(response["monthly"]), 6)
        self.assertEqual(response["monthly"][0]["month"], 1)
        self.assertLess(response["monthly"][-1]["delta_demand"],
                        response["monthly"][0]["delta_demand"])

    def test_negative_base_quantity_is_refused(self):
        with self.assertRaises(demand.BoundsError):
            demand.demand_response([0.1], [-5.0], ["energy"])

    def test_non_finite_price_is_refused(self):
        with self.assertRaises(demand.DemandError):
            demand.demand_response([float("nan")], [10.0], ["energy"])

    def test_huge_price_change_is_refused(self):
        with self.assertRaises(demand.DemandError):
            demand.demand_response([10.0], [10.0], ["energy"])

    def test_aggregate_bound_is_enforced(self):
        with self.assertRaises(demand.BoundsError):
            demand.demand_response([0.9], [100.0], ["energy"],
                                   elasticities={"energy": {"short": -1.0, "long": -1.0}},
                                   max_relative_change=0.5)

    def test_response_is_deterministic(self):
        first = demand.demand_response([0.1, 0.02], [100.0, 50.0], ["energy", "services"])
        second = demand.demand_response([0.1, 0.02], [100.0, 50.0], ["energy", "services"])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class LeontiefOutputTest(unittest.TestCase):
    def test_two_sector_analytic_solution(self):
        Z = np.array([[20.0, 100.0], [10.0, 60.0]])
        x = np.array([100.0, 200.0])
        document = {"schema": "amfe.mrio.dataset.v1.1", "countries": ["C0"],
                    "industries": ["I0", "I1"], "years": [2019], "units": "USD_mn",
                    "Z": Z.tolist(), "y": [70.0, 140.0], "x": x.tolist(), "va": [70.0, 140.0]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            matrix = mb.load_dataset_matrix(path)
        delta_demand = np.array([1.0, 0.0])
        delta_output = qb.solve_output(matrix, delta_demand)
        B = np.array([[0.2, 0.5], [0.1, 0.3]])
        expected = np.linalg.solve(np.eye(2) - B, delta_demand)
        np.testing.assert_allclose(delta_output, expected, rtol=1e-12, atol=1e-12)

    def test_output_identity_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = synthetic_dataset(Path(tmp) / "figaro" / "figaro_2019" / "dataset.json")
            matrix = mb.load_dataset_matrix(path)
        delta_demand = np.full(matrix["n"], -0.5)
        delta_output = qb.solve_output(matrix, delta_demand)
        B = matrix["A"].T.tocsr()
        residual = B @ delta_output + delta_demand - delta_output
        self.assertLess(float(np.max(np.abs(residual))), 1e-9)


class QuantityReportTest(unittest.TestCase):
    def _root(self, tmp: Path) -> Path:
        root = Path(tmp)
        synthetic_dataset(root / "figaro" / "figaro_2019" / "dataset.json")
        return root

    def test_report_structure_and_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                      pass_throughs=(0.6,), out_dir=Path(tmp) / "out",
                                      generated_at="2026-11-16T00:00:00Z")
            report = outcome["report"]
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["admission"])
        entry = report["datasets"]["figaro"]
        self.assertEqual(entry["status"], "OK")
        channels = entry["variants"]["0.6"]["channels"]
        self.assertEqual(set(channels), {"final_demand", "intermediate_input", "combined",
                                         "classification", "groups"})
        self.assertLessEqual(channels["final_demand"]["long"]["relative_delta_output"], 0.0)
        self.assertTrue(report["checks"]["no_combination_with_legacy_proxy"])
        self.assertTrue(report["checks"]["no_point_forecast"])

    def test_final_and_intermediate_are_different_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                      pass_throughs=(0.6,), out_dir=Path(tmp) / "out",
                                      generated_at="2026-11-16T00:00:00Z")
            channels = outcome["report"]["datasets"]["figaro"]["variants"]["0.6"]["channels"]
        final = channels["final_demand"]["long"]["relative_delta_output"]
        intermediate = channels["intermediate_input"]["long"]["relative_delta_output"]
        combined = channels["combined"]["long"]["relative_delta_output"]
        self.assertNotAlmostEqual(final, intermediate, places=6)
        self.assertAlmostEqual(combined, final + intermediate, places=6)

    def test_pass_through_zero_gives_zero_output_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                      pass_throughs=(0.0,), out_dir=Path(tmp) / "out",
                                      generated_at="2026-11-16T00:00:00Z")
            channels = outcome["report"]["datasets"]["figaro"]["variants"]["0.0"]["channels"]
        for key in ("final_demand", "intermediate_input", "combined"):
            self.assertEqual(channels[key]["long"]["total_delta_output"], 0.0)

    def test_larger_pass_through_has_larger_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                      pass_throughs=(0.3, 0.6, 1.0),
                                      out_dir=Path(tmp) / "out",
                                      generated_at="2026-11-16T00:00:00Z")
            variants = outcome["report"]["datasets"]["figaro"]["variants"]
        small = variants["0.3"]["channels"]["final_demand"]["long"]["relative_delta_output"]
        large = variants["1.0"]["channels"]["final_demand"]["long"]["relative_delta_output"]
        self.assertLess(large, small)

    def test_report_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                    pass_throughs=(0.6,), out_dir=Path(tmp) / "out1",
                                    generated_at="2026-11-16T00:00:00Z")
            second = qb.build_report(self._root(Path(tmp)), datasets=("figaro",),
                                     pass_throughs=(0.6,), out_dir=Path(tmp) / "out2",
                                     generated_at="2026-11-16T00:00:00Z")
        self.assertEqual(first["sha256"], second["sha256"])

    def test_price_stage_matches_price_bridge_math(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = synthetic_dataset(Path(tmp) / "figaro" / "figaro_2019" / "dataset.json")
            matrix = mb.load_dataset_matrix(path)
        price = qb.price_stage(matrix, "figaro", pass_through=0.6)
        reference = mb.run_price_model(matrix, classification="figaro",
                                       oil=price["oil"], pass_through=0.6)
        self.assertAlmostEqual(price["aggregate_relative"],
                               reference["results"]["aggregate_price_index_pct"] / 100.0,
                               places=6)

    def test_missing_dataset_is_blocked_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = qb.build_report(Path(tmp), datasets=("figaro",),
                                      pass_throughs=(0.6,), out_dir=Path(tmp) / "out",
                                      generated_at="2026-11-16T00:00:00Z")
        entry = outcome["report"]["datasets"]["figaro"]
        self.assertEqual(entry["status"], "BLOCKED_DATA")
        self.assertFalse(outcome["report"]["checks"]["quantity_channel_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
