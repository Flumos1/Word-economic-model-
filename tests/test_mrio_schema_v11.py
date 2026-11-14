"""Tests for the MRIO schema v1.1 (signed adjustments) and v1.0.0 backward compatibility.

Schema 1.1 adds two signed vectors so that a real source table with negative final-demand or
value-added cells (WIOD inventory changes, valuation rows) keeps exact accounting identities:

    x = Z 1 + y + final_demand_adjustments
    x = 1' Z + va + va_adjustments
    sum(va) + sum(vaa) = sum(y) + sum(fda)

The v1.0.0 documents (``amfe.mrio.dataset.v1``) stay readable and are equivalent to a 1.1.0
document with zero adjustments; a legacy document carrying non-zero adjustments is refused.

Run from the repository root: ``python tests/test_mrio_schema_v11.py``.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.mrio.schema import (  # noqa: E402
    LEGACY_SCHEMA,
    LEGACY_SCHEMA_VERSION,
    SCHEMA,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMAS,
    SUPPORTED_SCHEMA_VERSIONS,
    MRIODataset,
    SchemaError,
    canonical_bytes,
    dataset_from_document,
    dataset_to_bytes,
    validate_dataset,
)

FIXTURES = ROOT / "tests" / "fixtures" / "mrio"


def good_document() -> dict:
    """The committed v1.0.0 fixture document (2 countries x 3 industries, consistent)."""
    return json.loads((FIXTURES / "good" / "dataset.json").read_text(encoding="utf-8"))


def _numeric_arrays(node, path: str = "") -> list[str]:
    """Paths of numeric arrays longer than 3 entries (raw observations must not appear)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_numeric_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        numeric = node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                               for item in node)
        if numeric and len(node) > 3:
            found.append(path or "<root>")
        else:
            for position, value in enumerate(node):
                found.extend(_numeric_arrays(value, f"{path}[{position}]"))
    return found


def v11_document(**overrides) -> dict:
    """A v1.1 document: the good base table with a smaller gross output (signed adjustments)."""
    document = good_document()
    document["schema"] = SCHEMA
    document["schema_version"] = SCHEMA_VERSION
    document["x"] = list(document["x"])
    document["x"][0] = document["x"][0] - 4.0  # will be closed by negative fda/vaa
    document.update(overrides)
    return document


def consistent_adjustments(document: dict) -> dict:
    """Make the adjustments consistent with ``x`` and the base table (exact identities)."""
    size = len(document["x"])
    fda = list(document.get("final_demand_adjustments") or [0.0] * size)
    vaa = list(document.get("va_adjustments") or [0.0] * size)
    Z = document["Z"]
    for row in range(size):
        fda[row] = document["x"][row] - sum(Z[row]) - document["y"][row]
    for column in range(size):
        vaa[column] = (document["x"][column] - sum(Z[row][column] for row in range(size))
                       - document["va"][column])
    document["final_demand_adjustments"] = fda
    document["va_adjustments"] = vaa
    return document


# ---------------------------------------------------------------------------------------
# Version compatibility
# ---------------------------------------------------------------------------------------
class VersionCompatibility(unittest.TestCase):
    def test_schema_constants_describe_both_versions(self):
        self.assertEqual(SCHEMA, "amfe.mrio.dataset.v1.1")
        self.assertEqual(SCHEMA_VERSION, "1.1.0")
        self.assertEqual(LEGACY_SCHEMA, "amfe.mrio.dataset.v1")
        self.assertEqual(LEGACY_SCHEMA_VERSION, "1.0.0")
        self.assertIn(SCHEMA, SUPPORTED_SCHEMAS)
        self.assertIn(LEGACY_SCHEMA, SUPPORTED_SCHEMAS)
        self.assertEqual(set(SUPPORTED_SCHEMA_VERSIONS), {"1.1.0", "1.0.0"})

    def test_v10_document_is_still_accepted(self):
        document = good_document()
        self.assertEqual(document["schema"], LEGACY_SCHEMA)
        dataset = dataset_from_document(document)
        self.assertEqual(dataset.schema_version, LEGACY_SCHEMA_VERSION)
        self.assertEqual(dataset.final_demand_adjustments, (0.0,) * dataset.n)
        self.assertEqual(dataset.va_adjustments, (0.0,) * dataset.n)
        self.assertFalse(dataset.has_adjustments())
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.checks["schema_version"])

    def test_v10_is_equivalent_to_v11_with_zero_adjustments(self):
        legacy = dataset_from_document(good_document())
        upgraded = dataset_from_document(
            consistent_adjustments(good_document() | {"schema": SCHEMA,
                                                      "schema_version": SCHEMA_VERSION}))
        self.assertEqual(legacy.y, upgraded.y)
        self.assertEqual(legacy.x, upgraded.x)
        self.assertEqual(legacy.va, upgraded.va)
        self.assertEqual(legacy.Z, upgraded.Z)
        legacy_report = validate_dataset(legacy, enforce_p2_scale=False)
        upgraded_report = validate_dataset(upgraded, enforce_p2_scale=False)
        self.assertTrue(legacy_report.ok and upgraded_report.ok)
        self.assertAlmostEqual(legacy_report.metrics["max_abs_row_residual"], 0.0, places=9)
        self.assertAlmostEqual(upgraded_report.metrics["max_abs_row_residual"], 0.0, places=9)

    def test_legacy_document_with_adjustments_is_refused(self):
        document = good_document()
        document["final_demand_adjustments"] = [-5.0] + [0.0] * 5
        with self.assertRaises(SchemaError):
            dataset_from_document(document)

    def test_unknown_schema_or_version_is_refused(self):
        document = v11_document()
        document["schema"] = "amfe.mrio.dataset.v9"
        with self.assertRaises(SchemaError):
            dataset_from_document(document)
        document = v11_document()
        document["schema_version"] = "2.0.0"
        with self.assertRaises(SchemaError):
            dataset_from_document(document)
        document = v11_document()
        document["schema_version"] = LEGACY_SCHEMA_VERSION  # v1.1 id with 1.0.0 version
        with self.assertRaises(SchemaError):
            dataset_from_document(document)

    def test_missing_schema_version_defaults_by_schema_id(self):
        document = consistent_adjustments(v11_document())
        document.pop("schema_version")
        dataset = dataset_from_document(document)
        self.assertEqual(dataset.schema_version, SCHEMA_VERSION)
        legacy = good_document()
        legacy.pop("schema_version", None)
        self.assertEqual(dataset_from_document(legacy).schema_version, LEGACY_SCHEMA_VERSION)


# ---------------------------------------------------------------------------------------
# Adjustments and identities
# ---------------------------------------------------------------------------------------
class Adjustments(unittest.TestCase):
    def test_signed_adjustments_keep_exact_identities(self):
        document = consistent_adjustments(v11_document())
        dataset = dataset_from_document(document)
        self.assertTrue(dataset.has_adjustments())
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.checks["adjustments_shape"])
        self.assertAlmostEqual(report.metrics["max_abs_row_residual"], 0.0, places=9)
        self.assertAlmostEqual(report.metrics["max_abs_column_residual"], 0.0, places=9)
        self.assertAlmostEqual(report.metrics["global_difference_va_minus_y"], 0.0, places=9)
        self.assertAlmostEqual(report.metrics["global_difference_output"], 0.0, places=9)
        self.assertEqual(report.metrics["schema_version"], SCHEMA_VERSION)

    def test_negative_base_is_rejected_but_negative_adjustment_is_fine(self):
        document = consistent_adjustments(v11_document())
        document["y"] = list(document["y"])
        document["y"][0] = -1.0  # the base table must stay non-negative
        with self.assertRaises(SchemaError):
            dataset_from_document(document)
        document = consistent_adjustments(v11_document())
        document["va"] = list(document["va"])
        document["va"][0] = -2.0
        with self.assertRaises(SchemaError):
            dataset_from_document(document)

    def test_wrong_adjustment_length_is_rejected(self):
        document = v11_document()
        document["final_demand_adjustments"] = [0.0] * 5
        with self.assertRaises(SchemaError):
            dataset_from_document(document)
        document = v11_document()
        document["va_adjustments"] = [0.0] * 7
        with self.assertRaises(SchemaError):
            dataset_from_document(document)

    def test_non_finite_adjustment_is_rejected(self):
        document = v11_document()
        document["va_adjustments"] = [float("nan")] + [0.0] * 5
        with self.assertRaises(SchemaError):
            dataset_from_document(document)
        document = v11_document()
        document["final_demand_adjustments"] = [float("inf")] + [0.0] * 5
        with self.assertRaises(SchemaError):
            dataset_from_document(document)

    def test_inconsistent_adjustments_break_the_identity(self):
        document = v11_document()
        document["final_demand_adjustments"] = [-1.0] + [0.0] * 5  # too small to close the row
        dataset = dataset_from_document(document)
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertFalse(report.ok)
        self.assertIn("row balance violated", " ".join(report.errors))
        self.assertIn("global identity violated", " ".join(report.errors))

    def test_constructor_refuses_non_zero_adjustments_under_v10(self):
        base = dataset_from_document(good_document())
        with self.assertRaises(SchemaError):
            MRIODataset(
                countries=base.countries, industries=base.industries, years=base.years,
                Z=base.Z, y=base.y, x=base.x, va=base.va, units=base.units,
                final_demand_adjustments=(-1.0,) + (0.0,) * 5,
                schema_version=LEGACY_SCHEMA_VERSION,
            )

    def test_adjustments_change_the_digest_and_the_metadata(self):
        plain = dataset_from_document(consistent_adjustments(v11_document()))
        document = consistent_adjustments(v11_document())
        document["final_demand_adjustments"][0] -= 0.5
        document["va_adjustments"][1] += 0.5
        adjusted = dataset_from_document(document)
        self.assertNotEqual(plain.digest(), adjusted.digest())
        description = adjusted.describe()
        self.assertEqual(description["schema_version"], SCHEMA_VERSION)
        self.assertTrue(description["has_adjustments"])
        self.assertEqual(sum(description["adjustment_cells"].values()) > 0, True)
        self.assertIn("adjustment_totals", description)

    def test_document_round_trip_is_deterministic(self):
        dataset = dataset_from_document(consistent_adjustments(v11_document()))
        document = dataset.to_document()
        self.assertEqual(document["schema"], SCHEMA)
        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(document["final_demand_adjustments"]), dataset.n)
        second = dataset_from_document(document)
        self.assertEqual(dataset.digest(), second.digest())
        self.assertEqual(dataset_to_bytes(dataset), dataset_to_bytes(second))
        import hashlib

        self.assertEqual(dataset.digest(),
                         hashlib.sha256(canonical_bytes(document)).hexdigest())

    def test_both_versions_validate_in_one_loop(self):
        documents = [good_document(),
                     consistent_adjustments(v11_document())]
        versions = set()
        for document in documents:
            dataset = dataset_from_document(document)
            report = validate_dataset(dataset, enforce_p2_scale=False)
            self.assertTrue(report.ok, report.errors)
            versions.add(dataset.schema_version)
        self.assertEqual(versions, {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})


# ---------------------------------------------------------------------------------------
# Integration with the production loader / manifest (schema version propagates)
# ---------------------------------------------------------------------------------------
class LoaderIntegration(unittest.TestCase):
    def test_manifest_reports_the_v11_metrics(self):
        import tempfile

        from amfe.mrio.loaders import build_manifest, write_dataset

        dataset = dataset_from_document(consistent_adjustments(v11_document()))
        with tempfile.TemporaryDirectory(prefix="amfe-mrio-v11-") as tmp:
            root = Path(tmp)
            write_dataset(dataset, root / "default" / "dataset.json")
            manifest = build_manifest(root, enforce_p2_scale=False)
            self.assertEqual(manifest["status"], "AVAILABLE")
            entry = manifest["datasets"][0]
            self.assertTrue(entry["validation"]["ok"])
            self.assertTrue(entry["validation"]["checks"]["adjustments_shape"])
            self.assertTrue(entry["validation"]["checks"]["row_balance"])
            self.assertIn("max_abs_column_residual", entry["validation"])
            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            # aggregate-only: no observation arrays in the manifest
            self.assertEqual(_numeric_arrays(manifest), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
