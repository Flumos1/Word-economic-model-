"""Tests for the meso-micro labor/household layer (``amfe.world.labor``, docs/136).

Everything runs on a **synthetic** normalized SEA fixture written to a temporary directory: no
third-party data and no pandas are needed.  The real WIOD SEA data live on the local, git-ignored
data root and are only referenced by the importer tests through the pure normalisation function.

Run from the repository root: ``python tests/test_labor.py``.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.world import labor as lab  # noqa: E402

COUNTRIES = ("AAA", "BBB")
INDUSTRIES = ("S1", "S2", "S3")


def synthetic_document(years=(2010, 2014)) -> dict:
    """A normalized SEA-like document with known ratios (COMP/VA = 0.5 / 0.25 / 0.75)."""
    values = {}
    for year in years:
        block = {}
        for country_index, country in enumerate(COUNTRIES):
            industries = {}
            for industry_index, industry in enumerate(INDUSTRIES):
                va = 100.0 + 10.0 * industry_index
                comp = va * (0.5 if industry_index == 0 else 0.25 if industry_index == 1 else 0.75)
                employees = 10.0 + industry_index          # thousands
                hours = employees * 1.8                     # millions (1800 h/year)
                go = va * 2.0
                industries[industry] = {
                    "COMP": comp, "LAB": comp * 1.2, "VA": va, "GO": go,
                    "EMP": employees * 1.1, "EMPE": employees, "H_EMPE": hours,
                }
            block[country] = industries
        values[str(year)] = block
    return {"schema": lab.LAYOUT, "years": list(years), "countries": list(COUNTRIES),
            "industries": list(INDUSTRIES), "values": values,
            "units": {"COMP": "millions of national currency"}, "source": {"name": "synthetic"}}


def load_synthetic(**kwargs) -> lab.LaborData:
    document = synthetic_document(**kwargs)
    with tempfile.TemporaryDirectory(prefix="amfe-labor-") as tmp:
        path = Path(tmp) / "sea_normalized.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return load_from(path)


def load_from(path: Path) -> lab.LaborData:
    return lab.load_labor(path=path)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="amfe-labor-")
        base = Path(cls._tmp.name)
        cls.path = base / "sea_normalized.json"
        document = synthetic_document()
        cls.path.write_text(json.dumps(document), encoding="utf-8")
        cls.data = load_from(cls.path)
        cls.document = document

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


# ---------------------------------------------------------------------------------------
# Loader / fail-closed / determinism
# ---------------------------------------------------------------------------------------
class LoaderTests(Fixture):
    def test_load_synthetic_fixture(self):
        data = self.data
        self.assertEqual(data.years, (2010, 2014))
        self.assertEqual(data.countries, COUNTRIES)
        self.assertEqual(data.industries, INDUSTRIES)
        self.assertEqual(data.value(2014, "AAA", "S1", "VA"), 100.0)
        self.assertEqual(data.value(2014, "AAA", "S1", "COMP"), 50.0)
        self.assertIsNone(data.value(2014, "AAA", "S9", "VA"))
        self.assertEqual(len(data.digest()), 64)

    def test_missing_file_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(lab.LaborDataError) as caught:
                load_from(Path(tmp) / "absent.json")
        self.assertIn("BLOCKED_DATA", str(caught.exception))

    def test_missing_env_is_fail_closed(self):
        import os

        old = os.environ.pop("AMFE_MRIO_ROOT", None)
        try:
            with self.assertRaises(lab.LaborDataError):
                lab.load_labor()
        finally:
            if old is not None:
                os.environ["AMFE_MRIO_ROOT"] = old

    def test_malformed_document_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sea_normalized.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(lab.LaborDataError):
                load_from(path)
            document = synthetic_document()
            del document["values"]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(lab.LaborDataError):
                load_from(path)

    def test_non_finite_cell_is_refused(self):
        document = synthetic_document()
        document["values"]["2014"]["AAA"]["S1"]["VA"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sea_normalized.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(lab.LaborDataError):
                load_from(path)

    def test_digest_is_deterministic(self):
        second = load_synthetic()
        self.assertEqual(self.data.digest(), second.digest())
        self.assertEqual(self.data.describe()["countries"], len(COUNTRIES))


# ---------------------------------------------------------------------------------------
# Labour shares
# ---------------------------------------------------------------------------------------
class LaborShareTests(Fixture):
    def test_labor_share_by_country_industry(self):
        result = lab.labor_share(self.data, 2014)
        self.assertEqual(result["clipped"], 0)
        self.assertAlmostEqual(result["shares"]["AAA"]["S1"], 0.5, places=9)
        self.assertAlmostEqual(result["shares"]["AAA"]["S2"], 0.25, places=9)
        self.assertAlmostEqual(result["shares"]["BBB"]["S3"], 0.75, places=9)

    def test_outlier_is_clipped_and_recorded(self):
        document = synthetic_document()
        document["values"]["2014"]["AAA"]["S1"]["COMP"] = 400.0  # share 4.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sea_normalized.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            data = load_from(path)
        result = lab.labor_share(data, 2014)
        self.assertEqual(result["clipped"], 1)
        self.assertEqual(len(result["outliers"]), 1)
        self.assertEqual(result["shares"]["AAA"]["S1"], 1.0)
        # without clipping the raw ratio is preserved
        raw = lab.labor_share(data, 2014, clip=False)
        self.assertAlmostEqual(raw["shares"]["AAA"]["S1"], 4.0, places=9)

    def test_industry_shares_are_va_weighted(self):
        shares = lab.industry_labor_shares(self.data, 2014)
        # S1: each country comp 50 and 55, va 100 and 105 -> 105/205
        self.assertAlmostEqual(shares["S1"], 0.5, places=9)
        self.assertAlmostEqual(shares["S2"], 0.25, places=9)

    def test_world_labor_share(self):
        share = lab.labor_share_of_gdp(self.data, 2014)
        expected = (0.5 * 100.0 + 0.25 * 110.0 + 0.75 * 120.0) / (100.0 + 110.0 + 120.0)
        self.assertAlmostEqual(share, expected, places=9)
        including = lab.labor_share_of_gdp(self.data, 2014, "LAB")
        self.assertAlmostEqual(including, expected * 1.2, places=9)


# ---------------------------------------------------------------------------------------
# Country aggregates and employment intensity
# ---------------------------------------------------------------------------------------
class AggregatesTests(Fixture):
    def test_country_aggregates_and_units(self):
        aggregates = lab.country_aggregates(self.data, 2014)
        aaa = aggregates["AAA"]
        self.assertAlmostEqual(aaa["value_added_mn_lcu"], 100.0 + 110.0 + 120.0, places=6)
        self.assertAlmostEqual(aaa["employees_thousand"], 33.0, places=6)
        # H_EMPE millions / EMPE thousands -> x1000 = annual hours per employee
        self.assertAlmostEqual(aaa["hours_per_employee"], 1800.0, places=6)
        self.assertAlmostEqual(aaa["compensation_per_employee_lcu"],
                               (50.0 + 27.5 + 90.0) * 1e3 / 33.0, places=3)
        self.assertIsNotNone(aaa["labor_share"])

    def test_top_countries_sorted_and_complete(self):
        top = lab.top_countries_by_labor_share(self.data, 2014, limit=2)
        self.assertEqual(len(top), 2)
        self.assertGreaterEqual(top[0]["labor_share"], top[1]["labor_share"])
        including = lab.top_countries_by_labor_share(self.data, 2014, limit=1,
                                                     include_self_employed=True)
        expected = 1.2 * (0.5 * 100.0 + 0.25 * 110.0 + 0.75 * 120.0) / 330.0
        self.assertAlmostEqual(including[0]["labor_share"], expected, places=6)

    def test_employment_intensity_matches_io(self):
        io = {"countries": ["AAA", "BBB", "ROW"], "industries": list(INDUSTRIES),
              "x": [1000.0] * 3 + [2000.0] * 3 + [500.0] * 3, "units": "USD_mn"}
        intensity = lab.employment_intensity(self.data, io, 2014)
        self.assertEqual(intensity["matched"], 6)
        self.assertEqual(intensity["missing"], 0)
        row = next(item for item in intensity["rows"]
                   if item["country"] == "AAA" and item["industry"] == "S1")
        # H_EMPE = 10 * 1.8 = 18 million hours / 1000 USD_mn
        self.assertAlmostEqual(row["hours_per_usd_mn"], 18_000.0, places=6)

    def test_employment_intensity_requires_usd_units(self):
        io = {"countries": ["AAA"], "industries": list(INDUSTRIES), "x": [1.0, 1.0, 1.0],
              "units": "EUR_mn"}
        with self.assertRaises(lab.LaborError):
            lab.employment_intensity(self.data, io, 2014)

    def test_employment_intensity_counts_missing_pairs(self):
        io = {"countries": ["AAA"], "industries": ["S1"], "x": [1000.0], "units": "USD_mn"}
        intensity = lab.employment_intensity(self.data, io, 2014)
        self.assertEqual(intensity["matched"], 1)
        self.assertGreater(intensity["missing"], 0)


# ---------------------------------------------------------------------------------------
# Households, SFC link, invariants, report block
# ---------------------------------------------------------------------------------------
class HouseholdAndInvariantTests(Fixture):
    def test_household_block(self):
        block = lab.household_block(self.data, 2014,
                                    wdi={"consumption_share_of_gdp": {"AAA": 55.0},
                                         "gdp_per_capita_usd": {"AAA": 40000.0}})
        expected = (0.5 * 100.0 + 0.25 * 110.0 + 0.75 * 120.0) / 330.0
        self.assertAlmostEqual(block["world"]["labor_share"], expected, places=6)
        self.assertGreater(block["world"]["persons_engaged_million"], 0.0)
        self.assertEqual(block["countries"]["AAA"]["household_consumption_share_of_gdp"], 55.0)
        self.assertEqual(block["countries"]["BBB"]["household_consumption_share_of_gdp"], None)
        self.assertTrue(block["wdi_available"])

    def test_household_block_without_wdi_is_not_estimated(self):
        block = lab.household_block(self.data, 2014)
        self.assertFalse(block["wdi_available"])
        self.assertIsNone(block["countries"]["AAA"]["household_consumption_share_of_gdp"])

    def test_sfc_link(self):
        empty = lab.sfc_link({"labor_share_of_gdp": 0.5}, None)
        self.assertFalse(empty["sfc_available"])
        linked = lab.sfc_link({"labor_share_of_gdp": 0.5},
                              {"totals": {"household_income": 10.0, "savings": 2.0}})
        self.assertEqual(linked["household_income"], 10.0)
        self.assertTrue(linked["sfc_available"])

    def test_invariants_pass_on_good_data(self):
        for year in (2010, 2014):
            invariants = lab.labour_invariants(self.data, year)
            self.assertTrue(invariants["ok"], invariants["errors"])
            self.assertTrue(invariants["checks"]["shares_in_unit_interval"])
            self.assertTrue(invariants["checks"]["country_totals_equal_industry_sums"])

    def test_invariants_detect_bad_data(self):
        document = synthetic_document()
        for industry in INDUSTRIES:
            document["values"]["2014"]["AAA"][industry]["EMPE"] = 0.05  # implausible hours/person
        document["values"]["2014"]["AAA"]["S2"]["VA"] = -5.0           # negative value added cell
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sea_normalized.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            data = load_from(path)
        invariants = lab.labour_invariants(data, 2014)
        self.assertFalse(invariants["ok"])
        self.assertFalse(invariants["checks"]["hours_per_employee_plausible"])
        self.assertFalse(invariants["checks"]["non_negative_cells"])
        self.assertTrue(any("negative cells" in warning for warning in invariants["warnings"]))

    def test_report_block_is_aggregate_only(self):
        block = lab.build_labor_block(self.data, 2014, top_limit=1)
        self.assertEqual(block["status"], "RESEARCH_ONLY")
        expected = (0.5 * 100.0 + 0.25 * 110.0 + 0.75 * 120.0) / 330.0
        self.assertAlmostEqual(block["labor_share_of_gdp"], expected, places=6)
        self.assertEqual(len(block["top_countries_by_labor_share"]), 1)
        self.assertIn("checks", block)
        self.assertNotIn("values", json.dumps(block))  # no raw cell matrix in the block


# ---------------------------------------------------------------------------------------
# Importer pure functions (no pandas) and world_reports wiring
# ---------------------------------------------------------------------------------------
class ImporterTests(unittest.TestCase):
    def setUp(self):
        import labor_import

        self.importer = labor_import

    def test_normalize_rows(self):
        rows = [
            {"country": "AAA", "variable": "COMP", "code": "S1", 2010: 10.0, 2011: 11.0},
            {"country": "AAA", "variable": "VA", "code": "S1", 2010: 20.0, 2011: 22.0},
            {"country": "AAA", "variable": "CAP", "code": "S1", 2010: 99.0},  # not used
            {"country": "AAA", "variable": "EMP", "code": "S1", 2010: float("nan")},
        ]
        document = self.importer.normalize_sea_rows(rows)
        self.assertEqual(document["years"], [2010, 2011])
        self.assertEqual(document["countries"], ["AAA"])
        self.assertEqual(document["industries"], ["S1"])
        self.assertEqual(document["values"]["2010"]["AAA"]["S1"]["COMP"], 10.0)
        self.assertNotIn("CAP", document["values"]["2010"]["AAA"]["S1"])
        self.assertNotIn("EMP", document["values"]["2010"]["AAA"]["S1"])

    def test_normalize_rejects_duplicates(self):
        rows = [
            {"country": "AAA", "variable": "COMP", "code": "S1", 2010: 1.0},
            {"country": "AAA", "variable": "COMP", "code": "S1", 2010: 2.0},
        ]
        with self.assertRaises(self.importer.LaborImportError):
            self.importer.normalize_sea_rows(rows)

    def test_manifest_is_aggregate_only(self):
        document = self.importer.normalize_sea_rows([
            {"country": "AAA", "variable": "COMP", "code": "S1", 2010: 10.0},
            {"country": "AAA", "variable": "VA", "code": "S1", 2010: 20.0},
        ])
        document["source"] = {"name": "synthetic", "licence": "test"}
        manifest = self.importer.build_manifest(document)
        self.assertEqual(manifest["status"], "AVAILABLE")
        self.assertEqual(manifest["countries"], 1)
        self.assertEqual(manifest["industries"], 1)
        self.assertEqual(manifest["cells"]["variable_cells"], 2)
        self.assertNotIn("10.0", json.dumps(manifest["country_codes"]))

    def test_missing_sea_archive_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.importer.LaborImportError) as caught:
                self.importer.read_sea_workbook(Path(tmp) / "absent.xlsx")
        self.assertIn("BLOCKED_DATA", str(caught.exception))

    def test_world_reports_has_labor_flag_without_changing_defaults(self):
        import world_reports

        parser = world_reports.build_parser()
        default = parser.parse_args([])
        self.assertFalse(default.labor)
        enabled = parser.parse_args(["--labor", "--labor-root", "X"])
        self.assertTrue(enabled.labor)
        self.assertEqual(enabled.labor_root, "X")


if __name__ == "__main__":
    unittest.main(verbosity=2)
