"""Terms hashes (F-5) and the T10Y3M hygiene (F-6): structure, reproducibility and metadata-only.

Offline and read-only. The committed sources of truth are protocols/licence_register.json
(the `terms_sha256` / `terms_retrieved_at` fields and `terms_hash_method`) and
docs/90_Условия_источников_хеши.md (the exact terms excerpts and the normalisation rule).
The test recomputes every hash from the excerpts in docs/90, so a mismatch between the
document and the register is caught without network access.

Run: python tests/test_terms_hashes.py
"""
import hashlib
import json
import re
import subprocess
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ROOT / "protocols"
REGISTER = PROTOCOLS / "licence_register.json"
PLAN = PROTOCOLS / "data_plan.json"
DATA_LOG = PROTOCOLS / "data_log.json"
DOCS = ROOT / "docs" / "90_Условия_источников_хеши.md"
METHOD = "terms-passages-v1"
EXPECTED = ["FRED_ALFRED_SERVICES", "PHILADELPHIA_FED", "PHILADELPHIA_FED_RTDSM", "NBER_CYCLE_DATES"]
KNOWN_DOMAINS = {"fred.stlouisfed.org", "www.philadelphiafed.org", "www.nber.org"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK = re.compile(r"<!-- terms-passages: ([A-Za-z0-9_:]+) -->[ \t]*\n```text terms-passages\n(.*?)\n```", re.S)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalise(text: str) -> str:
    """The terms-passages-v1 rule: NFC, whitespace runs -> one ASCII space, strip."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def fingerprint(passages):
    joined = "\n".join(normalise(p) for p in passages)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def doc_passages():
    blocks = {}
    for match in BLOCK.finditer(DOCS.read_text(encoding="utf-8")):
        blocks[match.group(1)] = match.group(2).split("\n")
    return blocks


def tracked_files():
    if not (ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - environment dependent
        return None
    return [item for item in result.stdout.decode("utf-8", "surrogateescape").split("\0") if item]


class TermsHashStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}

    def test_method_block_is_declared(self):
        method = self.register.get("terms_hash_method")
        self.assertIsNotNone(method, "terms_hash_method is missing")
        self.assertEqual(method["id"], METHOD)
        self.assertRegex(method["retrieved_at"], ISO_DATE)
        self.assertEqual(method["docs"], "docs/90_Условия_источников_хеши.md")
        self.assertEqual(sorted(method["applies_to"]), sorted(EXPECTED))
        self.assertIn("sha256", method["definition"].lower())
        self.assertIn("mismatch", method["recheck_rule"].lower())

    def test_every_expected_source_has_the_terms_fields(self):
        self.assertEqual(sorted(self.register["terms_hash_method"]["applies_to"]), sorted(EXPECTED))
        for source_id in EXPECTED:
            entry = self.entries[source_id]
            self.assertRegex(entry["terms_sha256"], SHA256, source_id)
            self.assertRegex(entry["terms_retrieved_at"], ISO_DATE, source_id)
            self.assertIsInstance(entry["terms_urls"], list, source_id)
            self.assertTrue(entry["terms_urls"], source_id)
            self.assertIsInstance(entry["terms_passages"], int, source_id)
            self.assertGreater(entry["terms_passages"], 0, source_id)
            self.assertIn(METHOD, entry["terms_normalisation"], source_id)
            self.assertEqual(entry["terms_docs"], "docs/90_Условия_источников_хеши.md", source_id)

    def test_terms_urls_are_https_known_domains_and_listed_in_the_entry(self):
        for source_id in EXPECTED:
            entry = self.entries[source_id]
            for url in entry["terms_urls"]:
                parsed = urlparse(url)
                self.assertEqual(parsed.scheme, "https", source_id)
                self.assertIn(parsed.netloc, KNOWN_DOMAINS, source_id)
            self.assertTrue(set(entry["terms_urls"]) <= set(entry["url"]),
                            f"{source_id}: terms_urls must be part of the entry url list")

    def test_retrieved_dates_agree(self):
        dates = {self.entries[sid]["terms_retrieved_at"] for sid in EXPECTED}
        self.assertEqual(len(dates), 1, dates)
        self.assertEqual(dates, {self.register["terms_hash_method"]["retrieved_at"]})


class TermsHashReproducibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}
        cls.blocks = doc_passages()

    def test_docs_contains_a_passage_block_for_every_source(self):
        self.assertTrue(DOCS.exists(), "docs/90 is missing")
        self.assertEqual(sorted(self.blocks), sorted(EXPECTED), sorted(self.blocks))

    def test_hashes_recompute_from_the_documented_excerpts(self):
        for source_id in EXPECTED:
            expected = self.entries[source_id]["terms_sha256"]
            recomputed = fingerprint(self.blocks[source_id])
            self.assertEqual(recomputed, expected,
                             f"{source_id}: sha256 does not match the excerpts in docs/90")

    def test_passage_counts_agree(self):
        for source_id in EXPECTED:
            self.assertEqual(len(self.blocks[source_id]), self.entries[source_id]["terms_passages"], source_id)

    def test_docs_quotes_hash_in_the_table(self):
        text = DOCS.read_text(encoding="utf-8")
        for source_id in EXPECTED:
            self.assertIn("`" + self.entries[source_id]["terms_sha256"] + "`", text, source_id)
            self.assertIn(source_id, text, source_id)


class MetadataOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}
        cls.blocks = doc_passages()

    def test_no_terms_text_is_stored_in_the_register(self):
        for source_id in EXPECTED:
            entry = self.entries[source_id]
            for key, value in entry.items():
                if not key.startswith("terms_"):
                    continue
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 400, f"{source_id}:{key} looks like stored page text")
                if isinstance(value, list):
                    self.assertLessEqual(len(value), 6, f"{source_id}:{key}")
                    for item in value:
                        self.assertIsInstance(item, str)
                        self.assertLessEqual(len(item), 400, f"{source_id}:{key} item")

    def test_docs_is_bounded_and_has_no_page_markup(self):
        text = DOCS.read_text(encoding="utf-8")
        self.assertLess(len(text.encode("utf-8")), 60_000, "docs/90 must stay a bounded excerpt document")
        lowered = text.lower()
        for forbidden in ("<html", "<script", "<div"):
            self.assertNotIn(forbidden, lowered, f"docs/90 must not embed page markup: {forbidden}")
        for source_id, passages in self.blocks.items():
            self.assertLessEqual(len(passages), 12, source_id)
            for passage in passages:
                self.assertLessEqual(len(passage), 600, source_id)

    def test_no_data_values_in_the_register(self):
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

        self.assertEqual(numeric_arrays(self.register), [],
                         "the licence register must not carry data series values")


class T10Y3MHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load(PLAN)
        cls.log = load(DATA_LOG)
        cls.register = load(REGISTER)
        cls.entries = {e["id"]: e for e in cls.register["entries"]}

    def test_series_absent_from_plan_and_log(self):
        self.assertNotIn("T10Y3M", [s["id"] for s in self.plan["series"]])
        self.assertFalse([e for e in self.log["entries"] if "T10Y3M" in e.get("series", "")])
        self.assertFalse([e for e in self.log["entries"] if "T10Y3M" in e.get("register", "")])

    def test_series_absent_from_the_git_index(self):
        files = tracked_files()
        if files is None:
            self.skipTest("git index is not available")
        offenders = [f for f in files if "T10Y3M" in f]
        self.assertEqual(offenders, [])

    def test_local_probe_file_is_removed(self):
        self.assertFalse((ROOT / "data" / "raw" / "T10Y3M").exists(),
                         "the local T10Y3M probe file must stay deleted (docs/90, F-6)")

    def test_register_records_the_row_as_not_used(self):
        entry = self.entries["ALFRED:T10Y3M"]
        text = " ".join([entry["basis"], entry["decision"], *entry["conditions"]]).lower()
        self.assertIn("not used", text)
        self.assertIn("removed", text)
        self.assertIn("docs/90", text)


class Documentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCS.read_text(encoding="utf-8") if DOCS.exists() else ""

    def test_f5_f6_and_the_audit_context_are_recorded(self):
        self.assertTrue(DOCS.exists(), "docs/90 is missing")
        for token in ("F-5", "F-6", "T10Y3M", "2026-11-12", METHOD, "не юридическая консультация"):
            self.assertIn(token, self.text, token)

    def test_normalisation_and_recheck_rule_are_written_down(self):
        for token in ("NFC", "sha256", "нормализация", "CI", "terms_sha256", "terms_retrieved_at"):
            self.assertIn(token.lower(), self.text.lower(), token)

    def test_removal_of_the_local_file_is_described(self):
        self.assertIn("186 545", self.text)
        self.assertIn("data/raw/T10Y3M/latest.csv", self.text)
        self.assertIn("удалён", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
