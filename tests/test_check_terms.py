"""tools/check_terms.py: offline hash check, online excerpt check and the CLI contract.

Offline and read-only; the online mode is exercised with a synthetic fetcher (no network in the tests),
the way the CI step would call it. The real excerpts come from docs/90 and docs/97.

Run: python tests/test_check_terms.py
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_terms  # noqa: E402

REGISTER = check_terms.load_json(check_terms.REGISTER)
SOURCES = check_terms.terms_sources(REGISTER)
BLOCKS = check_terms.load_document_blocks(SOURCES)


def fetcher_from(passages, extra=""):
    """Synthetic page containing all normalised excerpts (in order)."""
    text = "\n".join(passages) + "\n" + extra
    recorded = []

    def fetch(url, user_agent=check_terms.USER_AGENT, timeout=60, retries=2):
        recorded.append(url)
        return text
    fetch.recorded = recorded
    return fetch


class Normalisation(unittest.TestCase):
    def test_whitespace_and_nfc(self):
        source = "  one\r\nTwo\u00a0 three\tfour  "
        self.assertEqual(check_terms.normalise(source), "one Two three four")
        composed = "e\u0301"  # e + combining acute
        self.assertEqual(check_terms.normalise(composed), "\u00e9")

    def test_fingerprint_is_sha256_of_joined_excerpts(self):
        passages = ["  a  b ", "c"]
        self.assertEqual(check_terms.fingerprint(passages),
                         __import__("hashlib").sha256(b"a b\nc").hexdigest())


class OfflineCheck(unittest.TestCase):
    def test_every_terms_source_is_ok_against_the_documents(self):
        report = check_terms.check_offline(REGISTER)
        self.assertEqual(report["mode"], "offline")
        self.assertEqual(report["checked"], len(SOURCES))
        self.assertGreaterEqual(report["checked"], 6)
        self.assertEqual(report["result"], check_terms.STATUS_OK)
        for item in report["sources"]:
            self.assertEqual(item["status"], check_terms.STATUS_OK, item["id"])

    def test_a_changed_hash_is_reported(self):
        register = json.loads(json.dumps(REGISTER))
        target = register["entries"][next(i for i, e in enumerate(register["entries"])
                                           if e["id"] == "FRED_ALFRED_SERVICES")]
        target["terms_sha256"] = "0" * 64
        report = check_terms.check_offline(register, BLOCKS)
        item = next(i for i in report["sources"] if i["id"] == "FRED_ALFRED_SERVICES")
        self.assertEqual(item["status"], check_terms.STATUS_MISMATCH)
        self.assertEqual(item["computed"], SOURCES[0]["sha256"])
        self.assertEqual(report["result"], check_terms.STATUS_MISMATCH)

    def test_a_missing_document_block_is_reported(self):
        blocks = {docs: dict(source_blocks) for docs, source_blocks in BLOCKS.items()}
        blocks[SOURCES[0]["docs"]].pop(SOURCES[0]["id"])
        report = check_terms.check_offline(REGISTER, blocks)
        item = next(i for i in report["sources"] if i["id"] == SOURCES[0]["id"])
        self.assertEqual(item["status"], check_terms.STATUS_MISSING)

    def test_registered_passage_count_mismatch_is_reported(self):
        register = json.loads(json.dumps(REGISTER))
        entry = next(e for e in register["entries"] if e["id"] == "BLS_PRIMARY")
        entry["terms_passages"] = entry["terms_passages"] + 1
        report = check_terms.check_offline(register, BLOCKS)
        item = next(i for i in report["sources"] if i["id"] == "BLS_PRIMARY")
        self.assertEqual(item["status"], check_terms.STATUS_MISMATCH)
        self.assertIn("excerpts", item["reason"])

    def test_offline_report_carries_no_page_text(self):
        report = check_terms.check_offline(REGISTER)
        text = json.dumps(report, ensure_ascii=False)
        self.assertLess(len(text), 20_000)
        for source_id, passages in BLOCKS.items():
            blocks = passages
            for passage in blocks.values():
                for excerpt in passage:
                    self.assertNotIn(excerpt, text, source_id)


class OnlineCheck(unittest.TestCase):
    def test_unchanged_pages_pass(self):
        fetched = []

        def fetcher(url, user_agent=check_terms.USER_AGENT, timeout=60, retries=2):
            fetched.append(url)
            passage_lines = []
            for source in SOURCES:
                if url in source["urls"]:
                    passage_lines.extend(BLOCKS[source["docs"]][source["id"]])
            return "\n".join(dict.fromkeys(passage_lines))

        report = check_terms.check_online(REGISTER, BLOCKS, fetcher=fetcher)
        self.assertEqual(report["result"], check_terms.STATUS_OK)
        expected_urls = {url for source in SOURCES for url in source["urls"]}
        self.assertEqual(set(fetched), expected_urls, "every terms URL must be checked")

    def test_page_sha256_is_recorded(self):
        url = SOURCES[0]["urls"][0]
        passages = BLOCKS[SOURCES[0]["docs"]][SOURCES[0]["id"]]
        report = check_terms.check_online(REGISTER, BLOCKS, fetcher=fetcher_from(passages))
        pages = [p for item in report["sources"] for p in item.get("pages", [])]
        page = next(p for p in pages if p["url"] == url)
        self.assertRegex(page["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(page["chars"], 0)

    def test_a_missing_excerpt_is_reported(self):
        target = next(s for s in SOURCES if len(s["urls"]) == 1 and s["id"] == "FRED_ALFRED_SERVICES")
        passages = list(BLOCKS[target["docs"]][target["id"]])
        passages.pop(0)
        report = check_terms.check_online(REGISTER, BLOCKS, fetcher=fetcher_from(passages))
        item = next(i for i in report["sources"] if i["id"] == target["id"])
        self.assertEqual(item["status"], check_terms.STATUS_MISMATCH)
        self.assertIn(0, [m["excerpt"] for m in item["missing"]])

    def test_a_changed_word_is_reported(self):
        target = next(s for s in SOURCES if s["id"] == "NBER_CYCLE_DATES")
        passages = list(BLOCKS[target["docs"]][target["id"]])
        passages[0] = passages[0].replace("copy", "reproduction")
        report = check_terms.check_online(REGISTER, BLOCKS, fetcher=fetcher_from(passages))
        item = next(i for i in report["sources"] if i["id"] == target["id"])
        self.assertEqual(item["status"], check_terms.STATUS_MISMATCH)

    def test_fetch_error_is_reported_not_raised(self):
        def failing(url, user_agent=check_terms.USER_AGENT, timeout=60, retries=2):
            raise RuntimeError("network unavailable")

        report = check_terms.check_online(REGISTER, BLOCKS, fetcher=failing)
        self.assertEqual(report["result"], check_terms.STATUS_MISMATCH)
        first = report["sources"][0]
        self.assertEqual(first["status"], check_terms.STATUS_ERROR)
        self.assertIn("network unavailable", first["reason"])


class Cli(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = check_terms.main(argv)
        return code, buffer.getvalue()

    def test_offline_cli_is_green_and_json(self):
        code, out = self.run_cli(["--offline", "--json"])
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual(report["result"], check_terms.STATUS_OK)
        self.assertEqual(report["checked"], len(SOURCES))

    def test_source_filter(self):
        code, out = self.run_cli(["--offline", "--json", "--source", "BLS_PRIMARY"])
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual([item["id"] for item in report["sources"]], ["BLS_PRIMARY"])

    def test_online_cli_fails_on_a_changed_page(self):
        target = SOURCES[0]
        passages = list(BLOCKS[target["docs"]][target["id"]])
        passages.pop(0)

        def fetcher(url, user_agent=check_terms.USER_AGENT, timeout=60, retries=2):
            return "\n".join(passages)

        with mock.patch.object(check_terms, "fetch_text", fetcher):
            code, out = self.run_cli(["--online", "--json"])
        self.assertEqual(code, 1, out)
        report = json.loads(out)
        self.assertEqual(report["result"], check_terms.STATUS_MISMATCH)

    def test_text_output_names_the_source(self):
        code, out = self.run_cli(["--offline", "--source", "FRED_ALFRED_SERVICES"])
        self.assertEqual(code, 0)
        self.assertIn("FRED_ALFRED_SERVICES", out)
        self.assertIn("OK", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
