"""Tests for tools/world_pipeline.py (docs/127): build/validate/solve/scenarios/determinism.

The suite is fast: small synthetic tables exercise the algorithms, one 60x45 case checks the
performance envelope with a soft threshold.  No third-party data is read; the canonical roundtrip
uses a temporary directory.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import world_pipeline as wp  # noqa: E402
from amfe.mrio.loaders import write_dataset  # noqa: E402


def small(seed=7, countries=6, industries=4, workers=1):
    return wp.synthetic_table(countries, industries, seed=seed, foreign_entries=2, workers=workers)


def args(**overrides):
    namespace = wp.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


class SyntheticTable(unittest.TestCase):
    def test_shape_balances_and_metadata(self):
        table = small()
        self.assertEqual((table.n_countries, table.n_industries, table.n), (6, 4, 24))
        self.assertEqual(table.Z.shape, (24, 24))
        row = np.asarray(table.Z.sum(axis=1)).ravel() + table.y - table.x
        column = np.asarray(table.Z.sum(axis=0)).ravel() + table.va - table.x
        self.assertLess(float(np.max(np.abs(row))), 1e-9)
        self.assertLess(float(np.max(np.abs(column))), 1e-9)
        self.assertEqual(table.labels[0], "C01|S01")
        self.assertTrue(table.provenance["synthetic"])

    def test_same_seed_same_table(self):
        first, second = small(seed=11), small(seed=11)
        self.assertEqual((first.Z != second.Z).nnz, 0)
        np.testing.assert_array_equal(first.x, second.x)
        np.testing.assert_array_equal(first.y, second.y)
        np.testing.assert_array_equal(first.va, second.va)
        other = small(seed=12)
        self.assertFalse(np.array_equal(first.x, other.x))

    def test_parallel_build_matches_serial(self):
        serial = small(seed=3, workers=1)
        parallel = small(seed=3, workers=3)
        self.assertEqual((serial.Z != parallel.Z).nnz, 0)
        np.testing.assert_array_equal(serial.x, parallel.x)
        np.testing.assert_array_equal(serial.y, parallel.y)

    def test_table_rejects_bad_geometry(self):
        with self.assertRaises(wp.PipelineError):
            wp.synthetic_table(0, 5)


class Validation(unittest.TestCase):
    def test_validation_passes_and_agrees_across_workers(self):
        table = small()
        serial = wp.validate_table(table, workers=1)
        parallel = wp.validate_table(table, workers=2)
        self.assertTrue(serial["pass"])
        self.assertEqual(serial, parallel)  # identical, not merely close
        self.assertTrue(serial["checks"]["product_balance"])
        self.assertTrue(serial["checks"]["value_added_balance"])
        self.assertEqual(serial["workers"] if "workers" in serial else 1, 1)

    def test_validation_fails_closed_on_corrupt_table(self):
        table = small()
        broken = wp.WorldTable(countries=table.countries, industries=table.industries,
                               Z=table.Z.copy(), y=table.y.copy(), x=table.x.copy(),
                               va=table.va.copy(), units=table.units, tolerance=table.tolerance)
        broken.y[0] = broken.y[0] + 100.0  # row balance no longer holds
        with self.assertRaises(wp.PipelineError):
            wp.validate_table(broken, workers=1)
        negative = wp.WorldTable(countries=table.countries, industries=table.industries,
                                 Z=table.Z.copy(), y=table.y.copy(), x=table.x.copy(),
                                 va=table.va.copy())
        negative.Z.data[0] = -1.0
        with self.assertRaises(wp.PipelineError):
            wp.validate_table(negative, workers=1)

    def test_country_aggregation_matches_io_core(self):
        table = small()
        Zc = wp.aggregate_country_parallel(table, workers=1)
        dataset = table.to_iodataset("country")
        reference = np.asarray(wp.aggregate_matrix_by_country(dataset, dataset.Z), dtype=float)
        np.testing.assert_allclose(Zc.toarray(), reference, rtol=0, atol=1e-9)
        parallel = wp.aggregate_country_parallel(table, workers=2)
        self.assertEqual((parallel != Zc).nnz, 0)


class CoreAndSolve(unittest.TestCase):
    def test_country_core_productiveness(self):
        table = small()
        core = wp.country_core(table)
        self.assertTrue(core["productive"])
        self.assertTrue(core["hawkins_simon_pass"])
        self.assertLess(core["spectral_radius"], 1.0)
        self.assertGreater(core["productiveness_margin"], 0.0)

    def test_solver_matches_dense_inverse_and_balance(self):
        table = small()
        solver = wp.WorldSolver(table)
        A = table.technical_sparse().toarray()
        expected = np.linalg.solve(np.eye(table.n) - A, table.y)
        np.testing.assert_allclose(solver.baseline_x, expected, rtol=1e-10, atol=1e-9)
        self.assertLess(solver.material_residual(), 1e-8)

    def test_solver_refuses_nonproductive_technology(self):
        countries, industries = ("C1", "C2"), ("S1", "S2")
        n = 4
        Z = np.zeros((n, n))
        Z[0, 0] = 2.0  # A[0,0] = 2 > 1
        Z[1, 1] = 2.0
        table = wp.WorldTable(countries=countries, industries=industries,
                              Z=wp.sp.csr_matrix(np.zeros((n, n)) + 0.0),
                              y=np.zeros(n), x=np.ones(n), va=np.zeros(n))
        table.Z = wp.sp.csr_matrix(Z)
        with self.assertRaises(wp.PipelineError):
            wp.WorldSolver(table)

    def test_perron_root_matches_dense_eigenvalues(self):
        table = small()
        A = table.technical_sparse()
        sparse_rho = wp.perron_root(A)
        dense_rho = float(np.max(np.abs(np.linalg.eigvals(A.toarray()))))
        self.assertAlmostEqual(sparse_rho, dense_rho, places=9)


class Scenarios(unittest.TestCase):
    def test_energy_scenario_reduces_output(self):
        table = small()
        solver = wp.WorldSolver(table)
        report = wp.scenario_energy(table, solver, share=0.10)
        self.assertLess(report["result"]["total_delta_output"], 0.0)
        self.assertLessEqual(report["delta_final_demand"]["total"], 0.0)
        self.assertEqual(report["scenario"], "energy")
        self.assertLess(report["balances"]["max_abs_material_residual"], 1e-8)
        self.assertIn(report["parameters"]["industries"][0], table.industries)

    def test_energy_industries_are_configurable_and_strict(self):
        table = small()
        solver = wp.WorldSolver(table)
        report = wp.scenario_energy(table, solver, share=0.05, industries=["S02"])
        self.assertEqual(report["parameters"]["industries"], ["S02"])
        with self.assertRaises(wp.PipelineError):
            wp.scenario_energy(table, solver, industries=["NOPE"])

    def test_import_scenario_reduces_output_and_rechecks_productiveness(self):
        table = small()
        solver = wp.WorldSolver(table)
        report = wp.scenario_import(table, solver, share=0.25)
        self.assertLess(report["result"]["total_delta_output"], 0.0)
        self.assertTrue(report["technology"]["productive_after"])
        self.assertGreaterEqual(report["technology"]["spectral_radius_after"], 0.0)
        self.assertLess(report["balances"]["max_abs_material_residual"], 1e-8)
        with self.assertRaises(wp.PipelineError):
            wp.scenario_import(table, solver, share=1.5)

    def test_fx_scenario_and_sfc_bookkeeping(self):
        table = small()
        solver = wp.WorldSolver(table)
        report = wp.scenario_fx(table, solver, country=table.countries[0], pass_through=0.10)
        self.assertEqual(report["parameters"]["country"], table.countries[0])
        self.assertTrue(report["sfc_check"]["pass"])
        self.assertEqual(report["sfc_check"]["world_net_worth_residual"], 0.0)
        self.assertLess(report["balances"]["max_abs_material_residual"], 1e-8)
        with self.assertRaises(wp.PipelineError):
            wp.scenario_fx(table, solver, country="NOPE")


class CanonicalRoundtrip(unittest.TestCase):
    def test_dense_canonical_write_load_roundtrip(self):
        table = small(seed=5)
        dataset = table.to_canonical_dense(limit=100)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dataset.json"
            digest = write_dataset(dataset, path)
            from amfe.mrio.loaders import load_dataset

            loaded = load_dataset(str(path), enforce_p2_scale=False)
            self.assertEqual(loaded.sha256, digest)
            restored = wp.table_from_canonical(loaded)
        # the canonical document rounds floats to 9 decimals, so compare numerically
        difference = abs(restored.Z - table.Z)
        self.assertEqual((difference > 1e-6).nnz, 0)
        np.testing.assert_allclose(restored.x, table.x, rtol=0, atol=1e-6)
        np.testing.assert_allclose(restored.y, table.y, rtol=0, atol=1e-6)
        self.assertEqual(restored.countries, table.countries)

    def test_dense_conversion_is_refused_above_limit(self):
        table = small()
        with self.assertRaises(wp.PipelineError):
            table.to_canonical_dense(limit=10)


class ReportAndCli(unittest.TestCase):
    def test_report_is_deterministic_across_worker_counts(self):
        first = wp.run_pipeline(args(synthetic=True, countries=6, industries=4, workers=1,
                                     scenario="energy", share=0.05))
        second = wp.run_pipeline(args(synthetic=True, countries=6, industries=4, workers=2,
                                      scenario="energy", share=0.05))
        self.assertEqual(wp.strip_timings(first), wp.strip_timings(second))
        self.assertEqual(first["status"], wp.STATUS_OK)
        self.assertIn("timings_ms", first)
        self.assertGreater(first["peak_memory_mb"], 0.0)

    def test_report_is_aggregate_only(self):
        report = wp.run_pipeline(args(synthetic=True, countries=6, industries=4, workers=1))
        text = json.dumps(report)
        self.assertNotIn('"Z"', text)  # never the raw flow matrix
        self.assertNotIn('"quantiles"', text)
        self.assertIn("by_country", json.dumps(wp.run_pipeline(args(
            synthetic=True, countries=6, industries=4, workers=1, scenario="energy"))))

    def test_cli_blocks_without_data_and_writes_report_with_synthetic(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                previous = os.environ.pop("AMFE_MRIO_ROOT", None)
            except Exception:  # pragma: no cover
                previous = None
            try:
                code = wp.main(["--build", "--root", tmp, "--out", str(Path(tmp) / "out")])
                self.assertEqual(code, wp.EXIT_BLOCKED)
            finally:
                if previous is not None:
                    os.environ["AMFE_MRIO_ROOT"] = previous
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "world"
            code = wp.main(["--synthetic", "--build", "--solve", "--out", str(out)])
            self.assertEqual(code, wp.EXIT_OK)
            report = json.loads((out / "world_pipeline_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], wp.STATUS_OK)
            self.assertIn("validation", report)
            self.assertIn("solve", report)

    def test_cli_rejects_invalid_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "dataset.json"
            bad.write_text('{"schema": "not-mrio"}', encoding="utf-8")
            self.assertIn(wp.main(["--dataset", str(bad), "--root", tmp,
                                   "--out", str(Path(tmp) / "out")]),
                          (wp.EXIT_BLOCKED, wp.EXIT_INVALID))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(wp.main(["--dataset", str(Path(tmp) / "missing.json"),
                                      "--root", tmp, "--out", str(Path(tmp) / "out")]),
                             wp.EXIT_BLOCKED)


class BenchmarkEnvelope(unittest.TestCase):
    """Soft performance envelope: 60 countries x 45 industries = 2700 sectors."""

    def test_60x45_pipeline_within_soft_threshold(self):
        started = time.perf_counter()
        report = wp.run_pipeline(args(synthetic=True, countries=60, industries=45, workers=1,
                                      scenario="energy", share=0.05))
        elapsed = time.perf_counter() - started
        self.assertEqual(report["table"]["sectors"], 2700)
        self.assertLess(elapsed, 120.0)  # soft: a hard failure here means a pathologically slow box
        self.assertLess(report["peak_memory_mb"], 2048.0)  # 2 GB budget
        self.assertTrue(report["validation"]["pass"])
        self.assertTrue(report["core"]["productive"])
        timings = report["timings_ms"]
        for stage in ("load", "core", "factorise", "solve", "scenario_energy"):
            self.assertIn(stage, timings)
        self.assertGreater(timings["factorise"], 0.0)
        # the scenario reuses the single sparse factorisation: it must be far cheaper than it
        self.assertLess(timings["scenario_energy"], timings["factorise"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
