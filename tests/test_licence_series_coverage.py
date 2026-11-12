"""Licence coverage of the 19 data_plan series and the complete terms-hash set (F-5, task-36).

Offline and read-only. Sources of truth:
* protocols/data_plan.json - the 19 series retrieved through ALFRED;
* protocols/licence_register.json - per-series ALFRED:<ID> entries, the BEA/BLS anchors and terms_sha256;
* docs/97_Лицензии_ряды_и_terms.md - the series-to-entry table and the BLS/BEA terms excerpts;
* docs/90_Условия_источников_хеши.md - the FRED/Philadelphia/NBER terms excerpts.

The BLS and BEA hashes are recomputed from the excerpts in docs/97, so a mismatch is caught offline.

Run: python tests/test_licence_series_coverage.py
"""
import hashlib
import json
import re
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ROOT / "protocols"
REGISTER = PROTOCOLS / "licence_register.json"
PLAN = PROTOCOLS / "data_plan.json"
DOCS97 = ROOT / "docs" / "97_Лицензии_ряды_и_terms.md"
DOCS90 = ROOT / "docs" / "90_Условия_источников_хеши.md"
METHOD = "terms-passages-v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_LABELS = {"Public Domain: Citation Requested", "Copyrighted: Citation Required",
                  "Copyrighted: Pre-approval required"}
TERMS_EXPECTED = {
    "FRED_ALFRED_SERVICES": "docs/90_Условия_источников_хеши.md",
    "PHILADELPHIA_FED": "docs/90_Условия_источников_хеши.md",
    "PHILADELPHIA_FED_RTDSM": "docs/90_Условия_источников_хеши.md",
    "NBER_CYCLE_DATES": "docs/90_Условия_источников_хеши.md",
    "BLS_PRIMARY": "docs/97_Лицензии_ряды_и_terms.md",
    "BEA_PRIMARY": "docs/97_Лицензии_ряды_и_terms.md",
}
BLOCK = re.compile(r"<!-- terms-passages: ([A-Za-z0-9_:]+) -->[ \t]*\n```text terms-passages\n(.*?)\n```", re.S)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalise(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def fingerprint(passages):
    return hashlib.sha256("\n".join(normalise(p) for p in passages).encode("utf-8")).hexdigest()


def blocks(path):
    out = {}
    for match in BLOCK.finditer(Path(path).read_text(encoding="utf-8")):
        out[match.group(1)] = match.group(2).split("\n")
    return out


def numeric_arrays(obj, depth=0):
    found = []
    if depth > 8:
        return found
    if isinstance(obj, dict):
        for value in obj.values():
            found.extend(numeric_arrays(value, depth + 1))
    elif isinstance(obj, list):
        numbers = [x for x in obj if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if len(obj) >= 10 and len(numbers) >= 0.9 * len(obj):
            found.append(len(obj))
        for value in obj[:2]:
            found.extend(numeric_arrays(value, depth + 1))
    return found


class PlanCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load(PLAN)
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}
        cls.series = [s["id"] for s in cls.plan["series"]]

    def test_plan_has_19_series(self):
        self.assertEqual(len(self.series), 19, self.series)
        self.assertEqual(len(self.series), len(set(self.series)))

    def test_every_plan_series_has_a_per_series_alfred_entry(self):
        missing = [s for s in self.series if "ALFRED:" + s not in self.entries]
        self.assertEqual(missing, [], "every retrieved series needs its own ALFRED:<ID> entry")

    def test_every_series_entry_is_open(self):
        for series in self.series:
            entry = self.entries["ALFRED:" + series]
            self.assertEqual(entry["status"], "OPEN", series)
            self.assertIn(entry["level"], {"full", "extraction", "none"}, series)

    def test_plan_register_field_points_to_an_open_entry(self):
        for series in self.plan["series"]:
            anchor = series["register"]
            self.assertIn(anchor, self.entries, series["id"])
            self.assertEqual(self.entries[anchor]["status"], "OPEN", series["id"])

    def test_no_restricted_or_unverified_source_is_used_by_the_plan(self):
        used = {s["register"] for s in self.plan["series"]}
        used |= {"ALFRED:" + s for s in self.series}
        self.assertNotIn("ATLANTA_FED_GDPNOW", used)
        for source in used:
            self.assertNotEqual(self.entries[source]["status"], "RESTRICTED", source)
            self.assertNotEqual(self.entries[source]["status"], "UNVERIFIED", source)


class SeriesLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}
        cls.series = [s["id"] for s in load(PLAN)["series"]]

    def test_labels_are_one_of_the_three_fred_labels(self):
        for series in self.series:
            entry = self.entries["ALFRED:" + series]
            self.assertIn(entry.get("label"), ALLOWED_LABELS, series)

    def test_label_verification_level_is_declared(self):
        for series in self.series:
            entry = self.entries["ALFRED:" + series]
            self.assertIn(entry.get("label_verification"), {"direct", "publisher_group"}, series)
            self.assertTrue(entry.get("label_source"), series)
            if series not in {"CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS", "USREC"}:
                self.assertRegex(entry["date"], ISO_DATE, series)

    def test_all_labels_are_direct(self):
        # after task-45 every plan series has its FRED page read directly; publisher_group is gone
        group = [s for s in self.series
                 if self.entries["ALFRED:" + s].get("label_verification") != "direct"]
        self.assertEqual(group, [])
        for s in self.series:
            self.assertIn("fred.stlouisfed.org/series/" + s, self.entries["ALFRED:" + s]["label_source"])
        for series in group:
            entry = self.entries["ALFRED:" + series]
            self.assertIn(entry.get("primary_anchor"), {"BEA_PRIMARY", "BLS_PRIMARY"}, series)
            self.assertIn("publisher group", entry["label_source"].lower(), series)
            self.assertEqual(entry["status"], "OPEN", series)

    def test_direct_labels_cite_the_fred_series_page(self):
        for series in self.series:
            entry = self.entries["ALFRED:" + series]
            self.assertEqual(entry["label_verification"], "direct", series)
            self.assertIn("fred.stlouisfed.org/series/" + series, entry["label_source"], series)

    def test_primary_anchor_entries_carry_the_series_list(self):
        for anchor in ("BEA_PRIMARY", "BLS_PRIMARY"):
            entry = self.entries[anchor]
            text = " ".join(entry["conditions"])
            self.assertIn("series anchor", text, anchor)
            self.assertEqual(entry["status"], "OPEN", anchor)


class TermsHashes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}
        cls.fingerprint_method = cls.register.get("terms_hash_method", {}).get("id")

    def test_every_required_source_has_a_terms_hash(self):
        self.assertEqual(self.fingerprint_method, METHOD)
        for source_id, docs in TERMS_EXPECTED.items():
            entry = self.entries[source_id]
            self.assertRegex(entry.get("terms_sha256", ""), SHA256, source_id)
            self.assertRegex(entry.get("terms_retrieved_at", ""), ISO_DATE, source_id)
            self.assertTrue(entry.get("terms_urls"), source_id)
            for url in entry["terms_urls"]:
                self.assertEqual(urlparse(url).scheme, "https", source_id)
            self.assertGreater(entry.get("terms_passages", 0), 0, source_id)
            self.assertEqual(entry.get("terms_docs"), docs, source_id)
            self.assertIn(METHOD, entry.get("terms_normalisation", ""), source_id)

    def test_docs90_hashes_still_match_their_excerpts(self):
        source_blocks = blocks(DOCS90)
        for source_id in ("FRED_ALFRED_SERVICES", "PHILADELPHIA_FED",
                          "PHILADELPHIA_FED_RTDSM", "NBER_CYCLE_DATES"):
            self.assertEqual(fingerprint(source_blocks[source_id]),
                             self.entries[source_id]["terms_sha256"], source_id)

    def test_bls_and_bea_hashes_recompute_from_docs97(self):
        source_blocks = blocks(DOCS97)
        for source_id in ("BLS_PRIMARY", "BEA_PRIMARY"):
            self.assertIn(source_id, source_blocks, "docs/97 must carry the reproducible excerpts")
            self.assertEqual(fingerprint(source_blocks[source_id]),
                             self.entries[source_id]["terms_sha256"], source_id)
            self.assertEqual(len(source_blocks[source_id]), self.entries[source_id]["terms_passages"], source_id)

    def test_docs97_quotes_the_hashes(self):
        text = DOCS97.read_text(encoding="utf-8")
        for source_id in TERMS_EXPECTED:
            self.assertIn("`" + self.entries[source_id]["terms_sha256"] + "`", text, source_id)


class Documentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCS97.read_text(encoding="utf-8")
        cls.plan = load(PLAN)
        cls.register = load(REGISTER)

    def test_docs_covers_every_plan_series(self):
        for series in self.plan["series"]:
            self.assertIn("`" + series["id"] + "`", self.text, series["id"])
            self.assertIn("`ALFRED:" + series["id"] + "`", self.text, series["id"])

    def test_docs_records_the_audit_context(self):
        for token in ("19/19", "F-5", "terms_sha256", "2026-11-12",
                      "не юридическая консультация", "Что осталось"):
            self.assertIn(token, self.text, token)

    def test_docs_is_bounded_metadata_not_page_text(self):
        self.assertLess(len(self.text.encode("utf-8")), 60_000)
        lowered = self.text.lower()
        for forbidden in ("<html", "<script", "<div"):
            self.assertNotIn(forbidden, lowered, forbidden)
        for passages in blocks(DOCS97).values():
            self.assertLessEqual(len(passages), 6)
            for passage in passages:
                self.assertLessEqual(len(passage), 600)


class RegisterHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}

    def test_register_ids_are_unique_and_well_formed(self):
        ids = [e["id"] for e in self.register["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in self.register["entries"]:
            self.assertIn(entry["status"], {"OPEN", "RESTRICTED", "UNVERIFIED"}, entry["id"])
            self.assertIn(entry["level"], {"full", "extraction", "none"}, entry["id"])
            self.assertRegex(entry["date"], ISO_DATE, entry["id"])
            self.assertTrue(entry["basis"], entry["id"])
            if entry["level"] != "none":
                self.assertTrue(entry["url"], entry["id"])

    def test_register_and_docs_carry_no_data_values(self):
        self.assertEqual(numeric_arrays(self.register), [], "the register must not carry data values")
        text = DOCS97.read_text(encoding="utf-8")
        for digits in re.findall(r"\b\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?\b", text):
            self.fail(f"a data-like number is embedded in docs/97: {digits}")

    def test_no_terms_text_is_embedded_in_the_register(self):
        for entry in self.register["entries"]:
            for key, value in entry.items():
                if not key.startswith("terms_"):
                    continue
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 400, f"{entry['id']}:{key}")
                if isinstance(value, list):
                    for item in value:
                        self.assertIsInstance(item, str)
                        self.assertLessEqual(len(item), 400, f"{entry['id']}:{key}")

    def test_series_coverage_block_is_consistent(self):
        coverage = self.register.get("series_coverage")
        self.assertIsNotNone(coverage)
        self.assertEqual(coverage["series_count"], len(load(PLAN)["series"]))
        self.assertEqual(coverage["checked_at"], "2026-11-12")
        self.assertTrue(coverage["docs"].startswith("docs/97"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
