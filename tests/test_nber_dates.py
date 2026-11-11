"""NBER announcement dates: structure, ordering and coverage of protocols/nber_dates.json.

The JSON is produced by tools/nber_dates.py in CI (workflow .github/workflows/r2-metadata.yml) and
committed as metadata. The tests are offline: they validate the committed file and the parser itself
on an inline sample of the NBER page markup.
"""
import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "protocols" / "nber_dates.json"
TOOL = ROOT / "tools" / "nber_dates.py"

FIXTURE = """
<p><a data-entity-substitution="canonical" href="/news/business-cycle-dating-committee-announcement-june-8-2020" title="Announcement">June 8, 2020</a>&nbsp;Determination of the February 2020 Peak in US Economic Activity</p>
<p><a data-entity-substitution="canonical" href="/news/business-cycle-dating-committee-announcement-september-20-2010" title="Announcement">September 20, 2010</a>&nbsp;&nbsp;Announcement of June 2009 business cycle trough/end of last recession</p>
<p><a data-entity-substitution="canonical" href="/news/business-cycle-dating-committee-announcement-april-12-2010" title="Memo">April 12, 2010</a>&nbsp;Memo from the Business Cycle Dating Committee</p>
"""


def load_tool():
    spec = importlib.util.spec_from_file_location("nber_dates_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NberDatesTool(unittest.TestCase):
    def test_parser_on_page_markup(self):
        tool = load_tool()
        rows = tool.parse_announcements(FIXTURE)
        self.assertEqual(len(rows), 3)
        peak = next(r for r in rows if r["type"] == "peak")
        self.assertEqual(peak["announcement_date"], "2020-06-08")
        self.assertEqual(peak["peak"], "2020-02")
        self.assertIsNone(peak["trough"])
        self.assertTrue(peak["source_url"].startswith("https://www.nber.org/news/"))
        trough = next(r for r in rows if r["type"] == "trough")
        self.assertEqual(trough["trough"], "2009-06")
        memo = next(r for r in rows if r["type"] == "memo")
        self.assertIsNone(memo["cycle_date"])
        # newest first
        self.assertEqual([r["announcement_date"] for r in rows],
                         ["2020-06-08", "2010-09-20", "2010-04-12"])

    def test_reference_pairs_need_announcement_after_peak(self):
        tool = load_tool()
        pairs = tool.reference_dates(tool.parse_announcements(FIXTURE))
        self.assertEqual(pairs[0]["peak"], "2020-02")
        self.assertTrue(pairs[0]["peak_announcement"] > pairs[0]["peak"])


class NberDatesJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DOC.exists():
            raise unittest.SkipTest("protocols/nber_dates.json is produced by the r2-metadata workflow")
        cls.doc = json.loads(DOC.read_text(encoding="utf-8"))

    def test_structure(self):
        d = self.doc
        for key in ("dataset", "tool", "source_url", "retrieved_at", "permission", "attribution",
                    "announcements", "reference_dates", "coverage", "source_page_sha256",
                    "announcements_sha256"):
            self.assertIn(key, d)
        self.assertEqual(d["tool"], "tools/nber_dates.py")
        self.assertIn("nber.org", d["source_url"])
        self.assertIn("attribution", d["permission"].lower())
        self.assertGreaterEqual(len(d["announcements"]), 8)

    def test_announcement_rows(self):
        for row in self.doc["announcements"]:
            for key in ("announcement_date", "type", "cycle_date", "peak", "trough", "title",
                        "source_url"):
                self.assertIn(key, row)
            self.assertRegex(row["announcement_date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn(row["type"], ("peak", "trough", "memo"))
            self.assertTrue(row["source_url"].startswith("https://www.nber.org/"))
            if row["type"] == "memo":
                self.assertIsNone(row["cycle_date"])
                continue
            self.assertRegex(row["cycle_date"], r"^\d{4}-\d{2}$")
            # the committee determines a peak/trough only after the fact
            self.assertLess(row["cycle_date"], row["announcement_date"][:7])
        dates = [row["announcement_date"] for row in self.doc["announcements"]]
        self.assertEqual(dates, sorted(dates, reverse=True), "announcements must be newest first")

    def test_coverage_1990_2026(self):
        d = self.doc
        recent = [a for a in d["announcements"] if "1990-01-01" <= a["announcement_date"] <= "2026-12-31"]
        self.assertGreaterEqual(len(recent), 8, "at least 8 announcements in 1990-2026")
        pairs = d["reference_dates"]
        for peak, trough in (("1990-07", "1991-03"), ("2007-12", "2009-06"),
                             ("2020-02", "2020-04")):
            match = [p for p in pairs if p["peak"] == peak]
            self.assertTrue(match, f"missing peak {peak}")
            self.assertEqual(match[0]["trough"], trough)
            self.assertGreater(match[0]["peak_announcement"], peak)
            self.assertGreater(match[0]["trough_announcement"], match[0]["peak_announcement"])

    def test_hashes(self):
        d = self.doc
        self.assertRegex(d["source_page_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(d["announcements_sha256"], r"^[0-9a-f]{64}$")
        blob = json.dumps(d["announcements"], ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        self.assertEqual(hashlib.sha256(blob.encode("utf-8")).hexdigest(),
                         d["announcements_sha256"],
                         "the stored hash must cover the stored announcements")


if __name__ == "__main__":
    unittest.main(verbosity=2)
