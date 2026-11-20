#!/usr/bin/env python3
"""Tests of the source-verification tool (tools/verify_sources.py, docs/197).

Offline: every check runs against injected fake fetchers (no network).  The suite verifies the
verdict matrix (200 / 403 / HTML stub / missing markers / timeout / offline), that a normal run
never touches the registry, that promotion is an explicit ``--apply`` step restricted to eligible
CATALOGUED candidates, that the evidence log is append-only, and that the registry stays valid.
"""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import verify_sources as vs  # noqa: E402
from amfe.data import source_registry as registry_module  # noqa: E402

FIXED = "2026-11-20T07:30:00Z"


def fixture_document() -> dict:
    """Valid registry document based on the real one, with test-controlled statuses."""
    document = json.loads((ROOT / "protocols" / "source_registry.json").read_text(encoding="utf-8"))
    document = copy.deepcopy(document)
    for source in document["sources"]:
        if source["source_id"] in ("EIA", "UN_COMTRADE", "EUROSTAT", "USDA_PSD"):
            source["status"] = "CATALOGUED"
            source["retrieved_at"] = None
        if source["source_id"] == "IEA":
            source["status"] = "NOT_DOWNLOADED"
            source["retrieved_at"] = None
    registry_module.validate_document(document)
    return document


def page(text: str, status: int = 200) -> dict:
    return {"status": status, "final_url": "https://example.org/", "content_type": "text/html",
            "bytes": len(text.encode("utf-8")),
            "sha256": "a" * 64, "text": text, "error": None, "elapsed_ms": 1.0}


GOOD_PAGE = ("energy trade data portal monthly annual prices production stocks exports "
             "imports license copyright terms public domain " * 300)
GOOD_TERMS = ("terms of use license copyright public domain " * 300)


class FakeFetcher:
    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.calls = []

    def __call__(self, url, *, timeout=20.0, limit=2_000_000):
        self.calls.append(url)
        value = self.mapping.get(url)
        if value is None:
            return {"url": url, "status": 404, "final_url": url, "content_type": "",
                    "bytes": 0, "sha256": None, "text": "", "error": None, "elapsed_ms": 1.0}
        if isinstance(value, Exception):
            return {"url": url, "status": None, "final_url": url, "content_type": "",
                    "bytes": 0, "sha256": None, "text": "", "error": f"{type(value).__name__}: {value}",
                    "elapsed_ms": 1.0}
        return dict(value)


def check_one(source_id: str, mapping, document=None, offline=False):
    document = document or fixture_document()
    source = next(item for item in document["sources"] if item["source_id"] == source_id)
    fetcher = FakeFetcher(mapping)
    return vs.check_source(source, vs.SOURCE_CHECKS[source_id], fetch=fetcher,
                           checked_at=FIXED, offline=offline), fetcher


class VerdictTest(unittest.TestCase):
    def test_valid_page_and_terms_is_only_a_candidate(self):
        entry, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(GOOD_PAGE),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        self.assertEqual(entry["verdict"], "VERIFIED_CANDIDATE")
        self.assertTrue(entry["checks"]["license_found"])
        self.assertTrue(entry["checks"]["coverage_ok"])
        self.assertTrue(entry["checks"]["fields_ok"])

    def test_403_is_not_verified(self):
        entry, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page("", status=403),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        self.assertEqual(entry["verdict"], "NOT_VERIFIED")
        self.assertIn("403", " ".join(entry["reasons"]))

    def test_html_stub_is_not_verified(self):
        stub = "Access denied. Enable JavaScript to continue. " * 300
        entry, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(stub),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        self.assertEqual(entry["verdict"], "NOT_VERIFIED")
        self.assertFalse(entry["checks"]["page_not_stub"])

    def test_short_page_is_not_verified(self):
        entry, _ = check_one("USDA_PSD", {
            "https://apps.fas.usda.gov/psdonline/": page("<html>app</html>"),
            vs.SOURCE_CHECKS["USDA_PSD"]["terms_url"]: page(GOOD_TERMS)})
        self.assertEqual(entry["verdict"], "NOT_VERIFIED")
        self.assertIn("too small", " ".join(entry["reasons"]))

    def test_missing_license_markers_is_not_verified(self):
        plain = "energy data portal monthly annual prices production stocks " * 300
        entry, _ = check_one("EIA", {
            "https://www.eia.gov/opendata/": page(plain),
            vs.SOURCE_CHECKS["EIA"]["terms_url"]: page("nothing about rights here " * 300)})
        self.assertEqual(entry["verdict"], "NOT_VERIFIED")
        self.assertFalse(entry["checks"]["license_found"])

    def test_terms_404_with_license_on_page_is_candidate(self):
        entry, _ = check_one("EIA", {
            "https://www.eia.gov/opendata/": page(GOOD_PAGE + " public domain copyright "),
            vs.SOURCE_CHECKS["EIA"]["terms_url"]: page("", status=404)})
        self.assertEqual(entry["verdict"], "VERIFIED_CANDIDATE")

    def test_timeout_is_offline(self):
        entry, _ = check_one("FRED_ALFRED", {
            "https://fred.stlouisfed.org/": TimeoutError("timed out"),
            vs.SOURCE_CHECKS["FRED_ALFRED"]["terms_url"]: TimeoutError("timed out")})
        self.assertEqual(entry["verdict"], "OFFLINE")

    def test_offline_mode_makes_no_request(self):
        fetcher = FakeFetcher({})
        document = fixture_document()
        source = next(item for item in document["sources"] if item["source_id"] == "EIA")
        entry = vs.check_source(source, vs.SOURCE_CHECKS["EIA"], fetch=fetcher,
                                checked_at=FIXED, offline=True)
        self.assertEqual(entry["verdict"], "OFFLINE")
        self.assertEqual(fetcher.calls, [])
        self.assertFalse(entry["checks"]["network_attempted"])


class PromotionTest(unittest.TestCase):
    def test_check_does_not_modify_registry(self):
        document = fixture_document()
        before = copy.deepcopy(document)
        entry, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(GOOD_PAGE),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        vs.plan_promotions(document, [entry])
        self.assertEqual(document, before)

    def test_plan_only_for_eligible_catalogued_candidates(self):
        document = fixture_document()
        good, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(GOOD_PAGE),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        bad, _ = check_one("OPEC", {
            "https://www.opec.org/opec_web/en/publications/338.htm": page("", status=403)})
        plan = vs.plan_promotions(document, [good, bad])
        self.assertEqual([item["source_id"] for item in plan], ["UN_COMTRADE"])

    def test_apply_updates_only_planned_sources_and_keeps_valid(self):
        document = fixture_document()
        good, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(GOOD_PAGE),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        updated, applied = vs.apply_promotions(document, [good])
        registry_module.validate_document(updated)
        self.assertEqual([item["source_id"] for item in applied], ["UN_COMTRADE"])
        by_id = {source["source_id"]: source for source in updated["sources"]}
        self.assertEqual(by_id["UN_COMTRADE"]["status"], "ACCESS_VERIFIED")
        self.assertNotEqual(by_id["UN_COMTRADE"]["status"], "VERIFIED")
        self.assertIsNone(by_id["UN_COMTRADE"]["retrieved_at"])
        self.assertIn("ACCESS_VERIFIED", by_id["UN_COMTRADE"]["notes"])
        self.assertIn("NOT VERIFIED", by_id["UN_COMTRADE"]["notes"])
        self.assertEqual(by_id["IEA"]["status"], "NOT_DOWNLOADED")
        self.assertEqual(by_id["EIA"]["status"], "CATALOGUED")

    def test_apply_without_evidence_keeps_registry_identical(self):
        document = fixture_document()
        before = json.dumps(document, sort_keys=True)
        updated, applied = vs.apply_promotions(document, [])
        self.assertEqual(applied, [])
        self.assertEqual(json.dumps(updated, sort_keys=True), before)


class EvidenceLogTest(unittest.TestCase):
    def test_log_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text('{"existing": true}\n', encoding="utf-8")
            entry = {"schema": vs.SCHEMA, "source_id": "EIA", "verdict": "NOT_VERIFIED"}
            written = vs.append_log(path, [entry, entry])
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(written, 2)
        self.assertEqual(lines[0], '{"existing": true}')
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[1])["source_id"], "EIA")

    def test_broken_log_line_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaises(vs.VerificationError):
                vs.load_log(path)

    def test_entries_are_deterministic(self):
        mapping = {"https://comtradeplus.un.org/": page(GOOD_PAGE),
                   vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)}
        first, _ = check_one("UN_COMTRADE", mapping)
        second, _ = check_one("UN_COMTRADE", mapping)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class StatusSemanticsTest(unittest.TestCase):
    def test_access_verified_cannot_masquerade_as_verified(self):
        with self.assertRaises(registry_module.SourceRegistryError):
            registry_module.validate_source(
                {"source_id": "X", "name": "x", "publisher": "p", "url": "https://x.org",
                 "license": "CC BY 4.0", "access": "api",
                 "coverage": {"geography": "g", "period": "p", "frequency": "monthly"},
                 "fields": ["f"], "retrieved_at": "2026-11-20",
                 "vintage_support": "none", "status": "ACCESS_VERIFIED",
                 "notes": "page checked"})

    def test_promotion_never_yields_verified(self):
        document = fixture_document()
        good, _ = check_one("UN_COMTRADE", {
            "https://comtradeplus.un.org/": page(GOOD_PAGE),
            vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)})
        updated, applied = vs.apply_promotions(document, [good])
        statuses = {source["source_id"]: source["status"] for source in updated["sources"]}
        self.assertEqual(statuses["UN_COMTRADE"], "ACCESS_VERIFIED")
        self.assertNotEqual(statuses["UN_COMTRADE"], "VERIFIED")
        self.assertEqual(applied[0]["to_status"], "ACCESS_VERIFIED")

    def test_counts_include_access_verified(self):
        counts = registry_module.counts(fixture_document())
        self.assertIn("ACCESS_VERIFIED", counts["by_status"])


class CliTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            log_path = Path(tmp) / "log.jsonl"
            registry_path.write_text(json.dumps(fixture_document(), ensure_ascii=False),
                                     encoding="utf-8")
            before_registry = registry_path.read_text(encoding="utf-8")
            mapping = {"https://comtradeplus.un.org/": page(GOOD_PAGE),
                       vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)}
            with mock.patch.object(vs, "fetch", FakeFetcher(mapping)):
                with redirect_stdout(io.StringIO()):
                    code = vs.main(["--dry-run", "--registry", str(registry_path),
                                    "--log", str(log_path), "--source", "UN_COMTRADE",
                                    "--checked-at", FIXED, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(registry_path.read_text(encoding="utf-8"), before_registry)
            self.assertFalse(log_path.exists())

    def test_apply_promotes_only_with_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            log_path = Path(tmp) / "log.jsonl"
            registry_path.write_text(json.dumps(fixture_document(), ensure_ascii=False),
                                     encoding="utf-8")
            mapping = {"https://comtradeplus.un.org/": page(GOOD_PAGE),
                       vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)}
            with mock.patch.object(vs, "fetch", FakeFetcher(mapping)):
                with redirect_stdout(io.StringIO()):
                    code = vs.main(["--apply", "--registry", str(registry_path),
                                    "--log", str(log_path), "--source", "UN_COMTRADE",
                                    "--checked-at", FIXED])
            self.assertEqual(code, 0)
            document = json.loads(registry_path.read_text(encoding="utf-8"))
            registry_module.validate_document(document)
            by_id = {source["source_id"]: source for source in document["sources"]}
            self.assertEqual(by_id["UN_COMTRADE"]["status"], "ACCESS_VERIFIED")
            self.assertNotEqual(by_id["UN_COMTRADE"]["status"], "VERIFIED")
            self.assertIsNone(by_id["UN_COMTRADE"]["retrieved_at"])
            self.assertTrue(log_path.exists())

    def test_cli_check_without_apply_keeps_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            log_path = Path(tmp) / "log.jsonl"
            registry_path.write_text(json.dumps(fixture_document(), ensure_ascii=False),
                                     encoding="utf-8")
            before = registry_path.read_text(encoding="utf-8")
            mapping = {"https://comtradeplus.un.org/": page(GOOD_PAGE),
                       vs.SOURCE_CHECKS["UN_COMTRADE"]["terms_url"]: page(GOOD_TERMS)}
            with mock.patch.object(vs, "fetch", FakeFetcher(mapping)):
                with redirect_stdout(io.StringIO()):
                    code = vs.main(["--check", "--registry", str(registry_path),
                                    "--log", str(log_path), "--source", "UN_COMTRADE",
                                    "--checked-at", FIXED])
            self.assertEqual(code, 0)
            self.assertEqual(registry_path.read_text(encoding="utf-8"), before)
            self.assertEqual(len(vs.load_log(log_path)), 1)

    def test_unknown_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text(json.dumps(fixture_document(), ensure_ascii=False),
                                     encoding="utf-8")
            with self.assertRaises(vs.VerificationError):
                vs.check_sources(fixture_document(), source_ids=["NO_SUCH_SOURCE"],
                                 checked_at=FIXED, fetch=FakeFetcher({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
