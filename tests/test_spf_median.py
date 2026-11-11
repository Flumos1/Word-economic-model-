"""SPF metadata: structure, coverage and the licence rule (no third-party values in the repository).

The committed file protocols/spf_metadata.json is produced by tools/fetch_spf.py in CI (workflow
.github/workflows/r2-metadata.yml). The median values are third-party data and are written only to the
git-ignored data directory; these tests are offline and check the metadata plus the publication rule.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "protocols" / "spf_metadata.json"
VALUES_JSON = ROOT / "protocols" / "spf_median.json"  # must never be committed

CPI_CODES = {"INFCPI1YR", "INFCPI10YR"}
GDP_CODES = {"drgdp2", "drgdp3", "drgdp4", "drgdp5", "drgdp6"}


class SpfMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DOC.exists():
            raise unittest.SkipTest("protocols/spf_metadata.json is produced by the r2-metadata workflow")
        cls.doc = json.loads(DOC.read_text(encoding="utf-8"))

    def test_structure(self):
        d = self.doc
        for key in ("dataset", "tool", "source_url", "retrieved_at", "licence_register_entry",
                    "attribution", "publication_rule", "raw_files_committed", "values_location",
                    "files", "variables", "coverage", "limitations"):
            self.assertIn(key, d)
        self.assertEqual(d["tool"], "tools/fetch_spf.py")
        self.assertEqual(d["licence_register_entry"], "PHILADELPHIA_FED")
        self.assertFalse(d["raw_files_committed"])
        self.assertIn("never committed", d["publication_rule"])
        self.assertIn("philadelphiafed.org", d["source_url"])
        self.assertTrue(d["limitations"], "limitations must be recorded")

    def test_no_third_party_values_in_the_repository(self):
        d = self.doc
        # the values live in the git-ignored data directory, not next to the metadata
        self.assertTrue(d["values_location"].startswith("data/raw/"), d["values_location"])
        self.assertFalse(VALUES_JSON.exists(), "median values must not be committed under protocols/")
        self.assertNotIn("series", d, "the metadata file must not carry the value rows")
        for var in d["variables"]:
            self.assertNotIn("value", var)
            self.assertNotIn("values", var)
            self.assertRegex(var["values_sha256"], r"^[0-9a-f]{64}$")

    def test_files_are_metadata_only(self):
        for f in self.doc["files"]:
            for key in ("key", "name", "url", "sheet", "columns_used", "sheet_header", "bytes",
                        "sha256", "rows_used"):
                self.assertIn(key, f)
            self.assertTrue(f["url"].startswith("https://www.philadelphiafed.org/"))
            self.assertRegex(f["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(f["bytes"], 0)
            self.assertGreater(f["rows_used"], 0)

    def test_variables_and_coverage(self):
        d = self.doc
        codes = {v["code"] for v in d["variables"]}
        self.assertTrue(CPI_CODES <= codes, CPI_CODES - codes)
        self.assertTrue(GDP_CODES <= codes, GDP_CODES - codes)
        for var in d["variables"]:
            self.assertGreater(var["observations"], 0)
            self.assertRegex(var["first_survey_quarter"], r"^\d{4}Q[1-4]$")
        cov = d["coverage"]
        self.assertEqual(cov["first_survey_quarter"], "1990Q1")
        self.assertGreaterEqual(cov["survey_quarters"], 140)
        self.assertGreaterEqual(cov["last_survey_quarter"], "2020Q1", "recent surveys must be covered")
        self.assertRegex(cov["first_survey_date"], r"^\d{4}-\d{2}-01$")
        self.assertLess(cov["first_survey_date"], cov["last_survey_date"])
        self.assertEqual(sum(v["observations"] for v in d["variables"]), cov["observations"])

    def test_no_raw_workbooks_or_values_in_the_tree(self):
        # licence_policy: raw third-party files are never committed
        offenders = [p for p in list((ROOT / "protocols").glob("*.xls*"))
                     + list((ROOT / "tools").glob("*.xls*"))
                     + list((ROOT / "protocols").glob("spf_median.json"))]
        self.assertEqual(offenders, [], "raw workbooks and median values must not be committed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
