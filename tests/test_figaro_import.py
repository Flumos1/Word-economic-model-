"""FIGARO importer: JSON-stat parsing, assembly, FX, RoW, coverage, validation, determinism.

No network is used: a tiny JSON-stat fixture reproduces the FIGARO dimension layout
(``freq, prd_use, prd_ava, c_dest, unit, c_orig, time``), and every network function is exercised
through its cached / pure branch.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import figaro_import  # noqa: E402
from amfe.mrio.schema import (  # noqa: E402
    SCHEMA,
    ValidationError,
    dataset_from_document,
    dataset_to_bytes,
    validate_dataset,
)

COUNTRIES = ("AA", "BB")
PRODUCTS = ("CPA_A01", "CPA_A02")
FINALS = list(figaro_import.FINAL_DEMAND_CODES)
VAS = list(figaro_import.VALUE_ADDED_CODES)
DESTINATIONS = list(COUNTRIES) + [figaro_import.ROW_CODE]
ORIGINS = DESTINATIONS + [figaro_import.DOM_CODE]
USES = list(PRODUCTS) + FINALS
AVAS = list(PRODUCTS) + VAS
SIZES = [1, len(USES), len(AVAS), len(DESTINATIONS), 1, len(ORIGINS), 1]


def flat_index(orig, ava, use, dest, sizes=None):
    return figaro_import.jsonstat_index([0, use, ava, dest, 0, orig, 0], sizes or SIZES)


def canonical_fixture_values():
    """Build a tiny internally consistent SIOT (in EUR).

    ``Z`` is constant (10 EUR mn) on every cell, so the row total and the column total of the
    square 6x6 matrix are equal (60); the country/product row total is then ``60 + y`` and the
    matching value added of a column is exactly ``y`` of that column.
    """
    z_eur = {(origin, produced, dest, used): 10.0
             for origin in DESTINATIONS for produced in PRODUCTS
             for dest in DESTINATIONS for used in PRODUCTS}
    y_eur = {(origin, produced): 100.0 + 10 * oi + 4 * pi
             for oi, origin in enumerate(DESTINATIONS) for pi, produced in enumerate(PRODUCTS)}
    va_eur = {(dest, used): y_eur[(dest, used)]
              for dest in DESTINATIONS for used in PRODUCTS}
    return z_eur, y_eur, va_eur


def fixture_document(dest=None):
    """A JSON-stat document with the FIGARO dimension order and sparse values."""
    z_eur, y_eur, va_eur = canonical_fixture_values()
    sizes = [1, len(USES), len(AVAS), len(DESTINATIONS) if dest is None else 1, 1,
             len(ORIGINS), 1]
    values = {}
    for origin in ORIGINS:
        oi = ORIGINS.index(origin)
        for ava in AVAS:
            ai = AVAS.index(ava)
            for use in USES:
                ui = USES.index(use)
                for d, destination in enumerate(DESTINATIONS):
                    if dest is not None and destination != dest:
                        continue
                    if origin == figaro_import.DOM_CODE:
                        if ava in VAS and use in PRODUCTS:
                            value = va_eur[(destination, use)] if ava == VAS[0] else 0.0
                        else:
                            value = 0.0
                    else:
                        if ava in PRODUCTS and use in PRODUCTS:
                            value = z_eur[(origin, ava, destination, use)]
                        elif ava in PRODUCTS and use == FINALS[0]:
                            value = y_eur[(origin, ava)] / len(DESTINATIONS)
                        else:
                            value = 0.0
                    if value:
                        values[str(flat_index(oi, ai, ui, 0 if dest is not None else d, sizes))] = value
    destinations = DESTINATIONS if dest is None else [dest]
    document = {
        "version": "2.0", "class": "dataset", "label": "AMFE FIGARO fixture",
        "value": values,
        "id": ["freq", "prd_use", "prd_ava", "c_dest", "unit", "c_orig", "time"],
        "size": [1, len(USES), len(AVAS), len(destinations), 1, len(ORIGINS), 1],
        "dimension": {
            "freq": {"category": {"index": {"A": 0}}},
            "prd_use": {"category": {"index": {code: i for i, code in enumerate(USES)}}},
            "prd_ava": {"category": {"index": {code: i for i, code in enumerate(AVAS)}}},
            "c_dest": {"category": {"index": {code: i for i, code in enumerate(destinations)}}},
            "unit": {"category": {"index": {"MIO_EUR": 0}}},
            "c_orig": {"category": {"index": {code: i for i, code in enumerate(ORIGINS)}}},
            "time": {"category": {"index": {"2019": 0}}},
        },
    }
    return document


def fixture_meta(year=2019):
    return {
        "dataset": figaro_import.DATASET_CODE, "year": year,
        "url": figaro_import.api_url(year),
        "destinations": list(DESTINATIONS), "origins": list(ORIGINS),
        "products_used": list(USES), "products_available": list(AVAS),
        "times": ["2019"], "units": ["MIO_EUR"],
    }


def fixture_chunk(dest):
    """Dense (origin, ava, use) EUR array for one destination, as the parser would produce."""
    z_eur, y_eur, va_eur = canonical_fixture_values()
    array = np.zeros((len(ORIGINS), len(AVAS), len(USES)), dtype=float)
    for oi, origin in enumerate(ORIGINS):
        for ai, ava in enumerate(AVAS):
            if origin == figaro_import.DOM_CODE:
                if ava in VAS:
                    array[oi, ai, USES.index(PRODUCTS[0]):] = 0.0
                    for used in PRODUCTS:
                        array[oi, ai, USES.index(used)] = va_eur[(dest, used)] if ava == VAS[0] else 0.0
                continue
            for used in PRODUCTS:
                array[oi, ai, USES.index(used)] = z_eur[(origin, ava, dest, used)] \
                    if ava in PRODUCTS else 0.0
            if ava in PRODUCTS:
                array[oi, ai, USES.index(FINALS[0])] = y_eur[(origin, ava)] / len(DESTINATIONS)
    return array


def assembled_document(**kwargs):
    chunks = [{"dest": dest, "array": fixture_chunk(dest)} for dest in DESTINATIONS]
    return figaro_import.assemble_document(fixture_meta(), {"chunks": chunks},
                                           usd_per_eur=kwargs.pop("usd_per_eur", 2.0),
                                           **kwargs)


class JSONStatParsing(unittest.TestCase):
    def test_flat_index_round_trip(self):
        for coordinates in ((0, 0, 0, 0, 0, 0, 0), (0, 3, 5, 2, 0, 3, 0), (0, 6, 4, 1, 0, 2, 0)):
            index = figaro_import.jsonstat_index(coordinates, SIZES)
            self.assertEqual(figaro_import.jsonstat_coordinates(index, SIZES), coordinates)

    def test_parse_jsonstat_document(self):
        parsed = figaro_import.parse_jsonstat_document(fixture_document())
        self.assertEqual(parsed["ids"][0], "freq")
        self.assertEqual(parsed["sizes"], SIZES)
        self.assertEqual(list(parsed["indexes"]["c_dest"]), DESTINATIONS)
        self.assertEqual(list(parsed["indexes"]["c_orig"]), ORIGINS)
        self.assertEqual(len(parsed["value"]), len(fixture_document()["value"]))

    def test_streaming_value_iterator_matches_json(self):
        document = fixture_document()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "slice.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            streamed = dict(figaro_import.iter_jsonstat_values(path, chunk_size=7))
        expected = {int(key): float(value) for key, value in document["value"].items()}
        self.assertEqual(streamed, expected)

    def test_streaming_iterator_rejects_a_file_without_values(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "broken.json"
            path.write_text('{"id": [], "size": []}', encoding="utf-8")
            with self.assertRaises(figaro_import.FigaroError):
                list(figaro_import.iter_jsonstat_values(path))

    def test_parse_dest_worker_matches_the_fixture(self):
        document = fixture_document(dest="AA")
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "AA.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            parsed = figaro_import.parse_dest_worker(str(path))
        self.assertEqual(parsed["dest"], "AA")
        expected = fixture_chunk("AA")
        np.testing.assert_allclose(parsed["array"], expected, rtol=0, atol=1e-12)
        self.assertEqual(parsed["dimensions"]["origins"], list(ORIGINS))


class Assembly(unittest.TestCase):
    def test_canonical_document_is_valid(self):
        document = assembled_document()
        self.assertEqual(document["schema"], SCHEMA)
        self.assertEqual(document["units"], "USD_mn")
        self.assertEqual(document["countries"], ["AA", "BB", "ROW"])
        self.assertEqual(document["industries"], list(PRODUCTS))
        self.assertEqual(document["source"]["row_block"], "WRL_REST -> ROW")
        dataset = dataset_from_document(document)
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.checks["row_balance"])
        self.assertTrue(report.checks["column_balance"])
        self.assertTrue(report.checks["global_value_added_equals_final_demand"])
        # P2 scale is enforced separately and must fail on this tiny fixture
        self.assertFalse(report.checks["p2_scale"])

    def test_fx_conversion_scales_the_values(self):
        eur = assembled_document(usd_per_eur=1.0)
        usd = assembled_document(usd_per_eur=2.0)
        self.assertAlmostEqual(usd["Z"][0][0], 2.0 * eur["Z"][0][0], places=6)
        self.assertAlmostEqual(sum(usd["x"]), 2.0 * sum(eur["x"]), places=3)
        self.assertEqual(usd["fx"]["USD_per_EUR"], 2.0)
        self.assertEqual(usd["fx"]["EUR_per_USD"], 0.5)

    def test_invalid_fx_is_rejected(self):
        chunks = [{"dest": dest, "array": fixture_chunk(dest)} for dest in DESTINATIONS]
        for rate in (0.0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(figaro_import.FigaroError):
                figaro_import.assemble_document(fixture_meta(), {"chunks": chunks},
                                                usd_per_eur=rate)

    def test_missing_row_block_is_rejected(self):
        meta = fixture_meta()
        meta["destinations"] = [code for code in meta["destinations"]
                                if code != figaro_import.ROW_CODE]
        meta["origins"] = [code for code in meta["origins"]
                           if code != figaro_import.ROW_CODE]
        chunks = [{"dest": dest, "array": fixture_chunk(dest)}
                  for dest in meta["destinations"]]
        with self.assertRaises(figaro_import.FigaroError):
            figaro_import.assemble_document(meta, {"chunks": chunks}, usd_per_eur=2.0)

    def test_negative_final_demand_is_shifted_into_the_identities(self):
        # A source-consistent negative cell: y and va of one (country, product) are both lowered
        # by the same amount, so the source table stays balanced and only the canonical
        # non-negativity shift is needed.
        chunks = [{"dest": dest, "array": fixture_chunk(dest)} for dest in DESTINATIONS]
        dom = ORIGINS.index(figaro_import.DOM_CODE)
        va_row = AVAS.index(VAS[0])
        chunks[0]["array"][0, 0, USES.index(FINALS[0])] -= 150.0
        chunks[0]["array"][dom, va_row, USES.index(PRODUCTS[0])] -= 150.0
        document = figaro_import.assemble_document(fixture_meta(), {"chunks": chunks},
                                                   usd_per_eur=2.0)
        self.assertTrue(all(value >= 0 for value in document["y"]))
        self.assertGreater(document["source"]["non_negativity_shift_usd_mn"], 0)
        report = validate_dataset(dataset_from_document(document), enforce_p2_scale=False)
        self.assertTrue(report.ok, report.errors)

    def test_dom_is_never_a_country(self):
        document = assembled_document()
        self.assertNotIn(figaro_import.DOM_CODE, document["countries"])
        self.assertIn("ROW", document["countries"])


class OutputAndValidation(unittest.TestCase):
    def test_write_dataset_pack_writes_hashed_files(self):
        document = assembled_document()
        with tempfile.TemporaryDirectory() as base:
            result = figaro_import.write_dataset_pack(
                Path(base), document, "figaro_test", enforce_p2_scale=False)
            dataset_path = Path(result["dataset_path"])
            manifest_path = Path(result["manifest_path"])
            self.assertTrue(dataset_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(figaro_import.sha256_file(dataset_path), result["dataset_sha256"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["dataset"]["sha256"], result["dataset_sha256"])
            self.assertEqual(manifest["dataset"]["bytes"], result["dataset_bytes"])
            self.assertTrue(manifest["validation"]["ok"])
            self.assertNotIn("Z", set(manifest["dataset"]))
            loaded = json.loads(dataset_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], SCHEMA)

    def test_invalid_document_is_fail_closed(self):
        document = assembled_document()
        document["va"][0] = document["va"][0] + 1.0  # break the column identity
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(figaro_import.FigaroError):
                figaro_import.write_dataset_pack(Path(base), document, "figaro_bad")

    def test_dataset_bytes_are_deterministic(self):
        document = assembled_document()
        first = dataset_to_bytes(dataset_from_document(document))
        second = dataset_to_bytes(dataset_from_document(document))
        self.assertEqual(first, second)

    def test_small_dataset_fails_p2_gate_but_not_identities(self):
        dataset = dataset_from_document(assembled_document())
        with self.assertRaises(ValidationError):
            from amfe.mrio.schema import require_valid
            require_valid(dataset)
        report = validate_dataset(dataset, enforce_p2_scale=False)
        self.assertTrue(report.ok)


class CoverageAndCache(unittest.TestCase):
    def test_gdp_coverage_with_row(self):
        gdp = {"year": 2019, "world_usd": 1000.0, "source": "fixture",
               "gdp_usd": {"AA": 300.0, "BB": 200.0, "GR": 100.0, "GB": 50.0}}
        coverage = figaro_import.gdp_coverage(["AA", "BB", "EL", "UK", "ROW"], gdp)
        self.assertEqual(coverage["named_countries"], 4)
        self.assertEqual(coverage["named_countries_with_gdp"], 4)
        self.assertAlmostEqual(coverage["named_share_pct"], 65.0, places=6)
        self.assertAlmostEqual(coverage["row_share_pct"], 35.0, places=6)
        self.assertEqual(coverage["coverage_with_row_pct"], 100.0)
        self.assertFalse(coverage["p2_gate_min_80pct"])

    def test_gdp_coverage_passes_the_80pct_gate(self):
        gdp = {"year": 2019, "world_usd": 1000.0, "source": "fixture",
               "gdp_usd": {"AA": 500.0, "BB": 350.0}}
        coverage = figaro_import.gdp_coverage(["AA", "BB", "ROW"], gdp)
        self.assertAlmostEqual(coverage["named_share_pct"], 85.0, places=6)
        self.assertTrue(coverage["p2_gate_min_80pct"])

    def test_gdp_coverage_reports_missing_countries(self):
        gdp = {"year": 2019, "world_usd": 1000.0, "source": "fixture",
               "gdp_usd": {"AA": 300.0}}
        coverage = figaro_import.gdp_coverage(["AA", "ZZ", "ROW"], gdp)
        self.assertEqual(coverage["missing_gdp"], ["ZZ"])
        self.assertAlmostEqual(coverage["named_share_pct"], 30.0, places=6)
        self.assertFalse(coverage["p2_gate_min_80pct"])

    def test_cached_destination_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as base:
            cache = Path(base) / "cache" / "2019"
            cache.mkdir(parents=True)
            (cache / "AA.json").write_text("{}", encoding="utf-8")
            result = figaro_import.fetch_dest_worker((str(base), 2019, "AA", False, 5.0))
        self.assertTrue(result["cached"])
        self.assertEqual(result["bytes"], 2)
        self.assertEqual(len(result["sha256"]), 64)

    def test_api_url_contains_the_filters(self):
        url = figaro_import.api_url(2019, "DE")
        self.assertIn("NAIO_10_FCP_IP3", url)
        self.assertIn("time=2019", url)
        self.assertIn("unit=MIO_EUR", url)
        self.assertIn("c_dest=DE", url)


class SparseStorage(unittest.TestCase):
    """Sparse ``Z_sparse`` path: same numbers as dense, deterministic bytes, fail-closed."""

    def _chunks(self):
        return [{"dest": dest, "array": fixture_chunk(dest)} for dest in DESTINATIONS]

    def test_sparse_assembly_matches_dense(self):
        sparse = figaro_import.assemble_sparse_document(
            fixture_meta(), {"chunks": self._chunks()}, usd_per_eur=2.0)
        dense = assembled_document()
        n = len(dense["x"])
        matrix = np.zeros((n, n), dtype=float)
        for row, col, value in zip(sparse["Z_sparse"]["rows"],
                                   sparse["Z_sparse"]["cols"],
                                   sparse["Z_sparse"]["values"]):
            matrix[int(row), int(col)] += float(value)
        np.testing.assert_allclose(matrix, np.asarray(dense["Z"], dtype=float), rtol=0, atol=1e-12)
        np.testing.assert_allclose(sparse["x"], dense["x"], rtol=0, atol=1e-12)
        np.testing.assert_allclose(sparse["y"], dense["y"], rtol=0, atol=1e-12)
        np.testing.assert_allclose(sparse["va"], dense["va"], rtol=0, atol=1e-12)
        report = figaro_import.validate_sparse_document(sparse, enforce_p2_scale=False)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["metrics"]["stored_Z"], int(np.count_nonzero(matrix)))

    def test_sparse_writer_reader_roundtrip_is_deterministic(self):
        document = figaro_import.assemble_sparse_document(
            fixture_meta(), {"chunks": self._chunks()}, usd_per_eur=2.0)
        with tempfile.TemporaryDirectory() as base:
            first = Path(base) / "first.json"
            second = Path(base) / "second.json"
            digest = figaro_import.write_sparse_dataset(document, first)
            again = figaro_import.write_sparse_dataset(document, second)
            self.assertEqual(digest, again)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertTrue(figaro_import.is_sparse_file(first))
            metadata, arrays = figaro_import.read_sparse_document(first)
            self.assertEqual(metadata["countries"], document["countries"])
            np.testing.assert_array_equal(arrays["rows"], document["Z_sparse"]["rows"])
            np.testing.assert_array_equal(arrays["cols"], document["Z_sparse"]["cols"])
            np.testing.assert_allclose(arrays["values"], document["Z_sparse"]["values"])
            restored = dict(metadata)
            restored["Z_sparse"] = arrays
            report = figaro_import.validate_sparse_document(restored, enforce_p2_scale=False)
            self.assertTrue(report["ok"], report["errors"])

    def test_sparse_validation_and_writer_are_fail_closed(self):
        document = figaro_import.assemble_sparse_document(
            fixture_meta(), {"chunks": self._chunks()}, usd_per_eur=2.0)
        broken = {key: (value.copy() if isinstance(value, np.ndarray) else value)
                  for key, value in document.items()}
        broken["Z_sparse"] = {key: (value.copy() if isinstance(value, np.ndarray) else value)
                              for key, value in document["Z_sparse"].items()}
        broken["va"] = np.asarray(broken["va"]).copy()
        broken["va"][0] += 1.0
        report = figaro_import.validate_sparse_document(broken, enforce_p2_scale=False)
        self.assertFalse(report["ok"])
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(figaro_import.FigaroError):
                figaro_import.write_sparse_pack(Path(base), broken, "figaro_bad",
                                                enforce_p2_scale=False)
        shorter = {key: (value.copy() if isinstance(value, np.ndarray) else value)
                   for key, value in document.items()}
        shorter["Z_sparse"] = {"rows": document["Z_sparse"]["rows"][:-1],
                               "cols": document["Z_sparse"]["cols"],
                               "values": document["Z_sparse"]["values"]}
        with self.assertRaises(figaro_import.FigaroError):
            figaro_import.validate_sparse_document(shorter, enforce_p2_scale=False)

    def test_dense_document_is_not_a_sparse_document(self):
        dense = assembled_document()
        with self.assertRaises(figaro_import.FigaroError):
            figaro_import.validate_sparse_document(dense, enforce_p2_scale=False)



if __name__ == "__main__":
    unittest.main(verbosity=2)
