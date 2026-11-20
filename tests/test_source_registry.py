"""Tests for the source registry (G3, docs/162): schema, honesty, fail-closed, CLI, assumptions."""
import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.data import source_registry as sr  # noqa: E402
import source_registry as cli  # noqa: E402

REGISTRY = ROOT / "protocols" / "source_registry.json"


def document() -> dict:
    return sr.load(REGISTRY)


def source(**overrides) -> dict:
    base = {
        "source_id": "TEST_SOURCE", "name": "Test source", "publisher": "Test publisher",
        "url": "https://example.org/data", "license": "CC BY 4.0", "access": "api",
        "coverage": {"geography": "global", "period": "2020-2025", "frequency": "monthly"},
        "fields": ["field one"], "retrieved_at": None, "vintage_support": "none",
        "status": "CATALOGUED", "notes": "",
    }
    base.update(overrides)
    return base


def wrapped(*sources) -> dict:
    return {"schema": sr.SCHEMA, "date": "2026-11-16", "intended_use": "research only",
            "statuses": {status: f"definition of {status}" for status in sr.STATUSES},
            "sources": list(sources)}


class RealRegistry(unittest.TestCase):
    def test_loads_and_has_the_required_sources(self):
        doc = document()
        self.assertEqual(doc["schema"], sr.SCHEMA)
        self.assertEqual(len(doc["sources"]), 15)
        ids = [item["source_id"] for item in doc["sources"]]
        self.assertEqual(ids, sorted(ids))
        for required in ("EIA", "IEA", "OPEC", "USDA_PSD", "USDA_FAS_EXPORT_SALES",
                         "FAO_FAOSTAT", "WORLD_BANK", "UN_COMTRADE", "OECD_ICIO", "EUROSTAT",
                         "FRED_ALFRED", "ICE_NYMEX_SETTLEMENTS", "KPLER", "VORTEXA", "BLS"):
            self.assertIn(required, ids)

    def test_status_and_access_distribution(self):
        counts = sr.counts(document())
        self.assertEqual(counts["sources"], 15)
        self.assertEqual(counts["by_status"], {"CATALOGUED": 6, "NOT_DOWNLOADED": 4,
                                               "ACCESS_VERIFIED": 2, "VERIFIED": 3})
        self.assertEqual(counts["by_access"], {"api": 6, "file": 5, "paid": 4})
        self.assertEqual(set(sr.missing(document())["verified"]),
                         {"EIA", "OECD_ICIO", "WORLD_BANK"})
        self.assertEqual(set(sr.missing(document())["access_verified"]),
                         {"EUROSTAT", "UN_COMTRADE"})

    def test_every_record_is_complete_and_https(self):
        for item in sr.sources(document()):
            for field in sr.REQUIRED_FIELDS:
                self.assertIn(field, item)
            self.assertTrue(item["url"].startswith("https://"))
            self.assertTrue(item["license"].strip())
            self.assertTrue(item["fields"])
            self.assertEqual(set(item["coverage"]), set(sr.COVERAGE_KEYS))

    def test_honesty_verified_is_not_catalogued(self):
        for item in sr.sources(document()):
            if item["status"] == "VERIFIED":
                self.assertIsNotNone(item["retrieved_at"], item["source_id"])
                self.assertTrue(item["notes"].strip(), item["source_id"])
            if item["status"] == "CATALOGUED":
                self.assertIn(item["retrieved_at"], (None,), item["source_id"])
            if item["status"] == "ACCESS_VERIFIED":
                # access evidence only: must not claim data retrieval and must not be VERIFIED
                self.assertIsNone(item["retrieved_at"], item["source_id"])
                self.assertTrue(item["notes"].strip(), item["source_id"])
                self.assertNotEqual(item["status"], "VERIFIED", item["source_id"])
            if item["access"] == "paid":
                self.assertEqual(item["status"], "NOT_DOWNLOADED", item["source_id"])

    def test_validation_report(self):
        report = sr.validate_file(REGISTRY)
        self.assertTrue(report["valid"])
        self.assertEqual(report["sources"], 15)
        self.assertEqual(report["errors"], [])
        self.assertEqual(sorted(report["verified"]), ["EIA", "OECD_ICIO", "WORLD_BANK"])


class Validation(unittest.TestCase):
    def test_single_source_validation(self):
        record = sr.validate_source(source())
        self.assertEqual(record["source_id"], "TEST_SOURCE")
        self.assertIsNone(record["retrieved_at"])

    def test_fail_closed_required_fields(self):
        for field in sr.REQUIRED_FIELDS:
            broken = source()
            broken.pop(field)
            with self.assertRaises(sr.SourceRegistryError, msg=field):
                sr.validate_source(broken)

    def test_fail_closed_values(self):
        cases = (
            {"source_id": "bad-id"}, {"source_id": "lowercase_id"}, {"url": "http://example.org"},
            {"url": "https://bad url"}, {"access": "ftp"}, {"status": "OK"},
            {"vintage_support": "sometimes"}, {"retrieved_at": "16.11.2026"},
            {"coverage": {"geography": "global"}}, {"fields": []}, {"license": "  "},
            {"name": ""}, {"publisher": ""},
        )
        for overrides in cases:
            with self.assertRaises(sr.SourceRegistryError, msg=str(overrides)):
                sr.validate_source(source(**overrides))

    def test_access_verified_invariants(self):
        good = sr.validate_source(source(status="ACCESS_VERIFIED",
                                         notes="page/terms checked, evidence log"))
        self.assertEqual(good["status"], "ACCESS_VERIFIED")
        self.assertIsNone(good["retrieved_at"])
        # access evidence may not claim a retrieval date (= masquerading as VERIFIED)
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_source(source(status="ACCESS_VERIFIED", retrieved_at="2026-11-20",
                                      notes="page/terms checked"))
        # ... and it must name the evidence
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_source(source(status="ACCESS_VERIFIED", notes=""))
        # a paid source may be ACCESS_VERIFIED (page checked) but not VERIFIED
        self.assertEqual(sr.validate_source(source(status="ACCESS_VERIFIED", access="paid",
                                                   notes="page checked"))["status"],
                         "ACCESS_VERIFIED")

    def test_verified_invariants(self):
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_source(source(status="VERIFIED"))
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_source(source(status="VERIFIED", retrieved_at="2026-11-16"))
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_source(source(status="VERIFIED", retrieved_at="2026-11-16",
                                      license="unknown terms"))
        good = sr.validate_source(source(status="VERIFIED", retrieved_at="2026-11-16",
                                         notes="dataset sha256 in docs/146"))
        self.assertEqual(good["status"], "VERIFIED")

    def test_document_validation(self):
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_document(wrapped(source(), source()))  # duplicate id
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_document(wrapped(source(), source(source_id="OTHER",
                                                          url="https://example.org/data")))
        bad_schema = wrapped(source())
        bad_schema["schema"] = "OTHER/1.0.0"
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_document(bad_schema)
        missing_statuses = wrapped(source())
        missing_statuses["statuses"].pop("VERIFIED")
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_document(missing_statuses)
        with self.assertRaises(sr.SourceRegistryError):
            sr.validate_document({})

    def test_missing_or_broken_file(self):
        with self.assertRaises(sr.SourceRegistryError):
            sr.load(ROOT / "protocols" / "does_not_exist.json")
        report = sr.validate_file(ROOT / "protocols" / "does_not_exist.json")
        self.assertFalse(report["valid"])
        broken = Path(__file__).parent / "_broken_registry.json"
        broken.write_text("{not json", encoding="utf-8")
        try:
            with self.assertRaises(sr.SourceRegistryError):
                sr.load(broken)
            self.assertFalse(sr.validate_file(broken)["valid"])
        finally:
            broken.unlink()


class Views(unittest.TestCase):
    def test_find_filters(self):
        doc = document()
        verified = sr.find(doc, status="VERIFIED")
        self.assertEqual([item["source_id"] for item in verified],
                         ["EIA", "OECD_ICIO", "WORLD_BANK"])
        paid = sr.find(doc, access="paid")
        self.assertEqual({item["status"] for item in paid}, {"NOT_DOWNLOADED"})
        crude = [item["source_id"] for item in sr.find(doc, query="crude")]
        self.assertIn("EIA", crude)
        self.assertIn("OPEC", crude)
        self.assertGreaterEqual(len(sr.find(doc, query="oil")), 3)
        with self.assertRaises(sr.SourceRegistryError):
            sr.find(doc, status="BOGUS")
        with self.assertRaises(sr.SourceRegistryError):
            sr.get(doc, "UNKNOWN")

    def test_counts_and_license_classes(self):
        self.assertEqual(sr.counts(document())["by_status"]["VERIFIED"], 3)
        self.assertEqual(sr.counts(document())["by_status"]["ACCESS_VERIFIED"], 2)
        self.assertEqual(sr.license_class("public domain (U.S. Government work)"), "public_domain")
        self.assertEqual(sr.license_class("CC BY 4.0"), "open_attribution")
        self.assertEqual(sr.license_class("CC BY-NC-SA 3.0 IGO (non-commercial)"),
                         "non_commercial")
        self.assertEqual(sr.license_class("commercial subscription licence"), "commercial")
        self.assertEqual(sr.license_class("unknown"), "unknown")

    def test_export_is_deterministic(self):
        first = sr.export_for_api(REGISTRY, source_path="protocols/source_registry.json")
        second = sr.export_for_api(REGISTRY, source_path="protocols/source_registry.json")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["schema"], sr.EXPORT_SCHEMA)
        ids = [item["source_id"] for item in first["sources"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(first["counts"]["sources"], 15)
        self.assertNotIn("protocols/source_registry.json", json.dumps(first["sources"]))

    def test_template_is_valid_catalogued(self):
        template = sr.build_template("NEW_SOURCE")
        self.assertEqual(template["status"], "CATALOGUED")
        self.assertIsNone(template["retrieved_at"])
        self.assertEqual(template["source_id"], "NEW_SOURCE")


class Assumptions(unittest.TestCase):
    def test_resolution_never_verifies(self):
        doc = document()
        explicit = sr.resolve_assumption_source("ASSUMPTION: literature ranges", doc)
        self.assertEqual(explicit["kind"], "ASSUMPTION")
        self.assertTrue(explicit["explicit_assumption"])
        self.assertIsNone(explicit["source_id"])
        direct = sr.resolve_assumption_source("ICE/EIA daily Brent quote", doc)
        self.assertEqual(direct["kind"], "SOURCE")
        self.assertIn(direct["source_id"], {"EIA", "ICE_NYMEX_SETTLEMENTS"})
        suggested = sr.resolve_assumption_source("Brent price from some external feed", doc)
        self.assertEqual(suggested["kind"], "UNKNOWN")
        self.assertFalse(suggested["resolved"])
        self.assertEqual(sr.resolve_assumption_source("", doc)["kind"], "ASSUMPTION")

    def test_check_assumptions_ok(self):
        report = cli.check_assumptions(document())
        self.assertTrue(report["ok"])
        self.assertEqual(report["oil_assumptions"], 17)
        self.assertEqual(report["oil_explicit_assumption"], 17)
        self.assertEqual(report["oil_unresolved"], [])
        self.assertEqual(report["agri_sources"], 5)
        self.assertEqual(report["agri_linked"], 5)
        self.assertEqual(report["agri_missing"], [])

    def test_check_assumptions_detects_missing_agri_source(self):
        trimmed = document()
        trimmed["sources"] = [item for item in trimmed["sources"]
                              if item["source_id"] != "USDA_PSD"]
        report = cli.check_assumptions(sr.validate_document(trimmed))
        self.assertFalse(report["ok"])
        self.assertIn("USDA_PSD", report["agri_missing"])


class Cli(unittest.TestCase):
    def _run(self, *args) -> tuple[int, str]:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main([*args])
        return code, buffer.getvalue()

    def test_validate_and_list(self):
        code, output = self._run("--validate", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["validation"]["valid"])
        code, output = self._run("--list", "--status", "VERIFIED", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual([item["source_id"] for item in payload["list"]],
                         ["EIA", "OECD_ICIO", "WORLD_BANK"])

    def test_missing_and_export(self):
        code, output = self._run("--missing", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["missing"]["n_pending"], 12)
        self.assertEqual(payload["missing"]["verified"],
                         ["EIA", "OECD_ICIO", "WORLD_BANK"])
        code, output = self._run("--json")
        self.assertEqual(code, 0)
        export = json.loads(output)
        self.assertEqual(export["export"]["schema"], sr.EXPORT_SCHEMA)

    def test_template_and_bad_id(self):
        code, output = self._run("--template", "CUSTOM")
        self.assertEqual(code, 0)
        template = json.loads(output)
        self.assertEqual(template["source_id"], "CUSTOM")
        code, _ = self._run("--template", "bad id")
        self.assertEqual(code, 1)

    def test_assumptions_command(self):
        code, output = self._run("--assumptions", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["assumptions"]["ok"])

    def test_bad_registry_exits_one(self):
        broken = Path(__file__).parent / "_bad_registry.json"
        broken.write_text("{not json", encoding="utf-8")
        try:
            code, output = self._run("--registry", str(broken), "--validate")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output)["ok"])
        finally:
            broken.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
