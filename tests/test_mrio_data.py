"""Tests for the AMFE MRIO data track: canonical schema, validator, fail-closed loader, manifest, CLI.

Everything runs offline on **synthetic fixtures** under ``tests/fixtures/mrio`` (invented and
internally consistent numbers, never third-party observations).  The 10+ x 30+ P2-scale dataset is
generated in memory by :func:`p2_scale_document` so the repository stays small.

Contracts covered
-----------------
* canonical schema: shapes, unique labels, ``USD_mn`` units, non-negativity, serialisation round-trip;
* accounting identities ``x = Z 1 + y``, ``x = 1' Z + va``, ``sum(va) = sum(y)`` and country totals;
* the P2 scale gate (>= 10 countries, >= 30 industries) and its message;
* fail-closed loading from ``AMFE_MRIO_ROOT``: missing root/file, hash mismatch, invalid data;
* deterministic dataset bytes/digests and aggregate-only manifests (AVAILABLE / BLOCKED_DATA);
* the dated source matrix (facts recorded by real HTTP probes, never invented);
* the CLI exit codes 0 / 1 / 2.

Run from the repository root: ``python tests/test_mrio_data.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.mrio import (  # noqa: E402
    MIN_COUNTRIES,
    MIN_INDUSTRIES,
    SOURCE_MATRIX,
    SOURCE_STATUSES,
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    DataUnavailable,
    SchemaError,
    ValidationError,
    available_datasets,
    build_manifest,
    dataset_from_document,
    dataset_to_bytes,
    load_dataset,
    mrio_root,
    probe_sources,
    require_available,
    source_status_summary,
    validate_dataset,
    write_dataset,
    write_manifest,
)
from amfe.mrio.schema import MRIODataset, canonical_bytes  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "mrio"
TOOL = ROOT / "tools" / "mrio_data.py"
FIXED_STAMP = "2026-11-14T00:00:00+00:00"


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def fixture_document(name: str) -> dict:
    return json.loads((FIXTURES / name / "dataset.json").read_text(encoding="utf-8"))


def load_fixture(name: str) -> MRIODataset:
    return dataset_from_document(fixture_document(name))


def p2_scale_document(countries: int = MIN_COUNTRIES, industries: int = MIN_INDUSTRIES,
                      year: int = 2020) -> dict:
    """A consistent synthetic dataset at the P2 scale (generated, not stored in the repo)."""
    size = countries * industries
    Z = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(size):
            if row == column:
                Z[row][column] = 2.0
            elif (row * 7 + column * 13) % 11 == 0:
                Z[row][column] = 0.5
    y = [10.0 + (row % 5) for row in range(size)]
    x = [sum(row) + y[index] for index, row in enumerate(Z)]
    va = []
    for column in range(size):
        intermediate = sum(Z[row][column] for row in range(size))
        va.append(x[column] - intermediate)
    assert min(va) >= 0, min(va)
    return {
        "schema": "amfe.mrio.dataset.v1",
        "schema_version": "1.0.0",
        "countries": [f"C{index:02d}" for index in range(countries)],
        "industries": [f"I{index:02d}" for index in range(industries)],
        "years": [year],
        "units": "USD_mn",
        "Z": Z,
        "y": y,
        "x": x,
        "va": va,
        "source": {"name": "synthetic P2-scale fixture", "third_party": False},
    }


def numeric_arrays(node, path: str = "") -> list[str]:
    """Paths of numeric arrays longer than 3 entries (potential raw observations)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(numeric_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        is_numeric = node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                                  for item in node)
        if is_numeric and len(node) > 3:
            found.append(path or "<root>")
        else:
            for position, value in enumerate(node):
                found.extend(numeric_arrays(value, f"{path}[{position}]"))
    return found


class TemporaryRoot(unittest.TestCase):
    """A temporary data root with the ``AMFE_MRIO_ROOT``/``AMFE_GENERATED_AT`` hygiene."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="amfe-mrio-")
        self.root = Path(self._tmp.name)
        self._old_env = {key: os.environ.get(key) for key in ("AMFE_MRIO_ROOT", "AMFE_GENERATED_AT")}
        os.environ.pop("AMFE_MRIO_ROOT", None)
        os.environ["AMFE_GENERATED_AT"] = FIXED_STAMP

        def restore():
            for key, value in self._old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._tmp.cleanup()

        self.addCleanup(restore)

    def install_good(self, dataset_id: str = "default", enforce_p2_scale: bool = False) -> Path:
        directory = self.root / dataset_id
        directory.mkdir(parents=True, exist_ok=True)
        payload = dataset_to_bytes(dataset_from_document(fixture_document("good")))
        path = directory / "dataset.json"
        path.write_bytes(payload)
        return path


# ---------------------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------------------
class SchemaTests(unittest.TestCase):
    def test_good_fixture_has_the_canonical_shape(self):
        dataset = load_fixture("good")
        self.assertEqual(dataset.countries, ("AA", "BB"))
        self.assertEqual(dataset.industries, ("i1", "i2", "i3"))
        self.assertEqual(dataset.years, (2020,))
        self.assertEqual(dataset.units, "USD_mn")
        self.assertEqual(dataset.n, 6)
        self.assertEqual((len(dataset.Z), len(dataset.Z[0])), (6, 6))
        self.assertEqual((len(dataset.y), len(dataset.x), len(dataset.va)), (6, 6, 6))
        self.assertEqual(dataset.labels[0], "AA|i1")
        self.assertEqual(dataset.country_of(3), "BB")
        self.assertEqual(dataset.industry_of(3), "i1")
        self.assertEqual(dataset.index_of("BB", "i1"), 3)
        self.assertEqual(list(dataset.country_index("AA")), [0, 1, 2])

    def test_serialisation_round_trip_and_deterministic_digest(self):
        dataset = load_fixture("good")
        again = dataset_from_document(dataset.to_document())
        self.assertEqual(dataset.digest(), again.digest())
        self.assertEqual(dataset_to_bytes(dataset), dataset_to_bytes(again))
        self.assertEqual(dataset.digest(), hashlib.sha256(canonical_bytes(dataset.to_document()))
                         .hexdigest())
        self.assertEqual(len(dataset.digest()), 64)

    def test_units_are_enforced(self):
        with self.assertRaises(SchemaError):
            load_fixture("bad_units")

    def test_duplicate_labels_are_rejected(self):
        with self.assertRaises(SchemaError):
            load_fixture("bad_duplicate")

    def test_dimension_mismatch_is_rejected(self):
        with self.assertRaises(SchemaError):
            load_fixture("bad_shape")

    def test_negative_entries_are_rejected(self):
        with self.assertRaises(SchemaError):
            load_fixture("bad_negative")

    def test_unknown_country_lookup_raises(self):
        dataset = load_fixture("good")
        with self.assertRaises(SchemaError):
            dataset.index_of("ZZ", "i1")
        with self.assertRaises(SchemaError):
            list(dataset.country_index("ZZ"))


# ---------------------------------------------------------------------------------------
# 2. Validator
# ---------------------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def test_good_fixture_satisfies_all_identities(self):
        dataset = load_fixture("good")
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, ())
        self.assertTrue(report.checks["row_balance"])
        self.assertTrue(report.checks["column_balance"])
        self.assertTrue(report.checks["global_value_added_equals_final_demand"])
        self.assertFalse(report.checks["p2_scale"])
        self.assertAlmostEqual(report.metrics["max_abs_row_residual"], 0.0, places=9)
        self.assertAlmostEqual(report.metrics["max_abs_column_residual"], 0.0, places=9)
        self.assertAlmostEqual(report.metrics["global_difference_va_minus_y"], 0.0, places=9)
        self.assertEqual(report.metrics["total_output"], 180.0)
        self.assertEqual(report.metrics["total_value_added"], 120.0)
        self.assertEqual(report.metrics["total_final_demand"], 120.0)
        self.assertEqual(report.metrics["x_by_country"], {"AA": 105.0, "BB": 75.0})
        self.assertEqual(report.metrics["net_exports_sum"], 0.0)
        self.assertEqual(report.metrics["net_exports_by_country"], {"AA": 0.0, "BB": 0.0})
        self.assertEqual(report.metrics["digest"], dataset.digest())
        self.assertIn("P2 scale gate", " ".join(report.warnings))

    def test_balance_violation_is_detected_with_metrics(self):
        dataset = dataset_from_document(fixture_document("bad_balance"))
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertFalse(report.ok)
        joined = " ".join(report.errors)
        self.assertIn("row balance violated", joined)
        self.assertIn("column balance violated", joined)
        self.assertIn("aggregate output identity violated", joined)
        self.assertGreater(report.metrics["max_abs_row_residual"], 1.0)
        self.assertGreater(abs(report.metrics["global_difference_output"]), 1.0)
        self.assertAlmostEqual(report.metrics["global_difference_va_minus_y"], 0.0, places=9)

    def test_p2_scale_gate_enforced_and_waived(self):
        dataset = load_fixture("good")
        strict = validate_dataset(dataset, enforce_p2_scale=True)
        self.assertFalse(strict.ok)
        self.assertIn("P2 scale gate", " ".join(strict.errors))
        waived = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(waived.ok)
        # the generated 10 x 30 table passes the gate
        big = dataset_from_document(p2_scale_document())
        report = validate_dataset(big, enforce_p2_scale=True)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.checks["p2_scale"])
        self.assertEqual(report.metrics["countries"], MIN_COUNTRIES)
        self.assertEqual(report.metrics["industries"], MIN_INDUSTRIES)
        self.assertEqual(report.metrics["n"], MIN_COUNTRIES * MIN_INDUSTRIES)

    def test_require_valid_raises_outside_the_gate(self):
        from amfe.mrio import require_valid

        with self.assertRaises(ValidationError):
            require_valid(load_fixture("good"), enforce_p2_scale=True)
        report = require_valid(load_fixture("good"), enforce_p2_scale=False)
        self.assertTrue(report.ok)

    def test_tolerance_is_respected(self):
        dataset = load_fixture("good")
        relaxed = validate_dataset(dataset, tolerance=1e-3, enforce_p2_scale=False)
        self.assertTrue(relaxed.ok)
        with self.assertRaises(SchemaError):
            validate_dataset(dataset, tolerance=0.0, enforce_p2_scale=False)


# ---------------------------------------------------------------------------------------
# 3. Fail-closed loader
# ---------------------------------------------------------------------------------------
class LoaderTests(TemporaryRoot):
    def test_missing_env_fails_closed(self):
        with self.assertRaises(DataUnavailable) as caught:
            mrio_root()
        self.assertIn(STATUS_BLOCKED, str(caught.exception))

    def test_missing_root_and_dataset_fail_closed(self):
        with self.assertRaises(DataUnavailable):
            mrio_root(self.root / "does-not-exist")
        with self.assertRaises(DataUnavailable) as caught:
            load_dataset("default", self.root)
        self.assertIn("dataset file not found", str(caught.exception))
        self.assertEqual(available_datasets(self.root), [])

    def test_hash_mismatch_fails_closed(self):
        path = self.install_good()
        with self.assertRaises(DataUnavailable) as caught:
            load_dataset(str(path), expected_sha256="0" * 64)
        self.assertIn("SHA-256 mismatch", str(caught.exception))
        loaded = load_dataset(str(path), expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              enforce_p2_scale=False)
        self.assertEqual(loaded.sha256, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_invalid_dataset_raises_validation_error(self):
        directory = self.root / "bad"
        directory.mkdir()
        (directory / "dataset.json").write_text(json.dumps(fixture_document("bad_balance")),
                                                encoding="utf-8")
        with self.assertRaises(ValidationError):
            load_dataset("bad", self.root, enforce_p2_scale=False)
        loaded = load_dataset("bad", self.root, enforce_p2_scale=False, strict=False)
        self.assertFalse(loaded.report.ok)

    def test_write_dataset_is_byte_stable_and_loadable(self):
        dataset = load_fixture("good")
        first = write_dataset(dataset, self.root / "default" / "dataset.json")
        second = write_dataset(dataset, self.root / "copy" / "dataset.json")
        self.assertEqual(first, second)
        loaded = load_dataset("default", self.root, enforce_p2_scale=False)
        self.assertEqual(loaded.sha256, first)
        self.assertEqual(loaded.size_bytes, (self.root / "default" / "dataset.json").stat().st_size)
        self.assertEqual(loaded.dataset.digest(), dataset.digest())

    def test_require_available_selects_the_first_valid_dataset(self):
        self.install_good()
        loaded = require_available(self.root, enforce_p2_scale=False)
        self.assertEqual(loaded.dataset.countries, ("AA", "BB"))
        with self.assertRaises(DataUnavailable):
            require_available(self.root / "empty", enforce_p2_scale=False)

    def test_environment_root_is_used(self):
        self.install_good()
        os.environ["AMFE_MRIO_ROOT"] = str(self.root)
        self.assertEqual(mrio_root(), self.root)
        self.assertEqual(available_datasets(), ["default"])
        loaded = load_dataset("default", enforce_p2_scale=False)
        self.assertEqual(loaded.dataset.units, "USD_mn")


# ---------------------------------------------------------------------------------------
# 4. Manifest
# ---------------------------------------------------------------------------------------
class ManifestTests(TemporaryRoot):
    def test_manifest_available_with_hashes_and_aggregates(self):
        path = self.install_good()
        manifest = build_manifest(self.root, enforce_p2_scale=False)
        self.assertEqual(manifest["status"], STATUS_AVAILABLE)
        self.assertIsNone(manifest["reason"])
        entry = manifest["datasets"][0]
        self.assertEqual(entry["status"], STATUS_AVAILABLE)
        self.assertEqual(entry["countries"], 2)
        self.assertEqual(entry["industries"], 3)
        self.assertEqual(entry["years"], [2020])
        self.assertEqual(entry["units"], "USD_mn")
        self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(entry["validation"]["ok"])
        self.assertEqual(entry["dataset_digest"], load_fixture("good").digest())
        # aggregate-only: no matrix / observation arrays
        self.assertEqual(numeric_arrays(manifest), [])

    def test_manifest_blocked_data_with_reason(self):
        manifest = build_manifest(self.root, enforce_p2_scale=False)
        self.assertEqual(manifest["status"], STATUS_BLOCKED)
        self.assertIn("no dataset", manifest["reason"])
        self.assertEqual(manifest["datasets"], [])

    def test_manifest_blocked_when_root_is_missing(self):
        manifest = build_manifest(self.root / "absent", dataset_ids=["default"])
        self.assertEqual(manifest["status"], STATUS_BLOCKED)
        self.assertIsNotNone(manifest["datasets"][0]["reason"])

    def test_manifest_hashes_are_deterministic(self):
        self.install_good()
        first = build_manifest(self.root, enforce_p2_scale=False)
        second = build_manifest(self.root, enforce_p2_scale=False)
        self.assertEqual(first["generated_at"], FIXED_STAMP)
        self.assertEqual(first["datasets"][0]["sha256"], second["datasets"][0]["sha256"])
        self.assertEqual(first["datasets"][0]["dataset_digest"],
                         second["datasets"][0]["dataset_digest"])
        digest_first = write_manifest(first, self.root / "manifest1.json")
        digest_second = write_manifest(second, self.root / "manifest2.json")
        self.assertEqual(digest_first, digest_second)

    def test_manifest_validates_the_p2_gate(self):
        self.install_good()
        manifest = build_manifest(self.root, enforce_p2_scale=True)
        self.assertEqual(manifest["status"], STATUS_BLOCKED)
        self.assertEqual(manifest["datasets"][0]["status"], STATUS_BLOCKED)
        self.assertIn("P2 scale gate", manifest["datasets"][0]["reason"])


# ---------------------------------------------------------------------------------------
# 5. Source matrix
# ---------------------------------------------------------------------------------------
class SourceMatrixTests(unittest.TestCase):
    EXPECTED = {"oecd_icio", "wiod_2016", "un_comtrade", "bea_io", "eurostat_figaro",
                "eora_mrio", "github_api"}

    def test_matrix_covers_the_candidate_sources(self):
        ids = {entry.source_id for entry in SOURCE_MATRIX}
        self.assertTrue(self.EXPECTED.issubset(ids), ids)
        self.assertGreaterEqual(len(SOURCE_MATRIX), 6)
        for entry in SOURCE_MATRIX:
            self.assertTrue(entry.url.startswith("http"), entry.source_id)
            self.assertTrue(entry.checked_at, entry.source_id)
            self.assertIn(entry.status, SOURCE_STATUSES)
            self.assertIsInstance(entry.licence_verified, bool)

    def test_verified_access_facts_are_recorded(self):
        by_id = {entry.source_id: entry for entry in SOURCE_MATRIX}
        self.assertEqual(by_id["un_comtrade"].status, "AVAILABLE_API")
        self.assertIn("preview", by_id["un_comtrade"].access)
        self.assertEqual(by_id["bea_io"].status, "BLOCKED_KEY")
        self.assertIn("UserId", by_id["bea_io"].check)
        self.assertEqual(by_id["eora_mrio"].status, "BLOCKED_LICENCE")
        self.assertIn("190", by_id["eora_mrio"].countries)
        self.assertEqual(by_id["wiod_2016"].status, "AVAILABLE_MANUAL")
        self.assertTrue(by_id["wiod_2016"].licence_verified)
        self.assertIn("2000-2014", by_id["wiod_2016"].years)
        self.assertIn("43", by_id["wiod_2016"].countries)
        self.assertEqual(by_id["eurostat_figaro"].status, "AVAILABLE_API")
        self.assertIn("NAIO_10_CP1700", by_id["eurostat_figaro"].url)
        self.assertEqual(by_id["github_api"].status, "AVAILABLE")

    def test_source_status_summary(self):
        summary = source_status_summary()
        self.assertEqual(summary["sources"], len(SOURCE_MATRIX))
        self.assertIn("bea_io", summary["blocked"])
        self.assertIn("un_comtrade", summary["with_data_api_no_key"])

    def test_offline_probe_marks_network_as_skipped(self):
        probes = probe_sources(network=False)
        self.assertEqual(len(probes), len(SOURCE_MATRIX))
        for probe in probes:
            self.assertFalse(probe["network"])
            self.assertIn("skipped", probe["probe"])
            self.assertIsNone(probe["probe"]["reachable"])
        summary = source_status_summary(probes)
        self.assertEqual(summary["reachable_now"], 0)


# ---------------------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------------------
class CLITests(TemporaryRoot):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("AMFE_MRIO_ROOT", None)
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True,
                              text=True, cwd=str(ROOT), timeout=120, env=env)

    def test_check_blocked_exit_code_2(self):
        result = self.run_tool("--check", "--no-network", "--root", str(self.root))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(STATUS_BLOCKED, result.stdout)

    def test_check_available_exit_code_0(self):
        self.install_good()
        result = self.run_tool("--check", "--no-network", "--no-p2-gate", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(STATUS_AVAILABLE, result.stdout)

    def test_validate_exit_codes(self):
        self.install_good()
        good = self.run_tool("--validate", "--no-p2-gate", "--root", str(self.root))
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("[OK", good.stdout)
        directory = self.root / "bad"
        directory.mkdir()
        (directory / "dataset.json").write_text(json.dumps(fixture_document("bad_balance")),
                                                encoding="utf-8")
        bad = self.run_tool("--validate", "--no-p2-gate", "--root", str(self.root))
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("row balance violated", bad.stdout)

    def test_manifest_cli_writes_json(self):
        self.install_good()
        out = self.root / "manifest.json"
        result = self.run_tool("--manifest", "--no-p2-gate", "--root", str(self.root),
                               "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], STATUS_AVAILABLE)
        self.assertIn("sha256", document["datasets"][0])
        self.assertEqual(numeric_arrays(document), [])

    def test_manifest_cli_blocked_still_writes(self):
        out = self.root / "manifest.json"
        result = self.run_tool("--manifest", "--root", str(self.root), "--out", str(out))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], STATUS_BLOCKED)
        self.assertTrue(document["reason"])

    def test_json_flag_is_machine_readable(self):
        result = self.run_tool("--check", "--no-network", "--root", str(self.root), "--json")
        document = json.loads(result.stdout)
        self.assertEqual(document["action"], "check")
        self.assertEqual(document["status"], STATUS_BLOCKED)
        self.assertEqual(len(document["sources"]), len(SOURCE_MATRIX))


if __name__ == "__main__":
    unittest.main(verbosity=2)
