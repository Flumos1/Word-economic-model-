"""Tests for the source-verification docket (docs/205): counts, gaps, licence matrix, fail-closed.

The suite is offline by construction: the tool reads local JSON only, and one test replaces
``socket`` with a raising stub to prove that no network path is taken.
"""
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import source_verification as sv  # noqa: E402

REGISTRY = ROOT / "protocols" / "source_registry.json"
LICENCES = ROOT / "protocols" / "licence_register.json"
DOC = ROOT / "docs" / "205_Верификация_источников.md"
TOOL = ROOT / "tools" / "source_verification.py"


# ------------------------------------------------------------------------------------- fixtures
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
    return {"schema": sv.REGISTRY_SCHEMA, "date": "2026-11-16", "intended_use": "research only",
            "statuses": {status: f"definition of {status}" for status in
                         ("CATALOGUED", "NOT_DOWNLOADED", "ACCESS_VERIFIED", "VERIFIED")},
            "sources": list(sources)}


def entry(**overrides) -> dict:
    base = {"id": "TEST_SOURCE", "date": "2026-11-11", "status": "OPEN", "level": "full",
            "conditions": ["attribution required"], "url": ["https://example.org/terms"]}
    base.update(overrides)
    return base


def journal(*entries) -> dict:
    return {"register_id": "AMFE-LICENCE-REGISTER", "entries": list(entries)}


class Workspace(unittest.TestCase):
    """A temporary directory with helper writers (offline)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="svtest_", dir=str(ROOT)))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name: str, payload) -> Path:
        path = self.dir / name
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path

    def build(self, sources, entries):
        registry = self.write("registry.json", wrapped(*sources))
        licences = self.write("licences.json", journal(*entries))
        return sv.build_docket(registry, licences)


# ------------------------------------------------------------------------------- real registry
class RealRegistryDocket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = sv.build_docket(REGISTRY, LICENCES)
        cls.by_id = {record["source_id"]: record for record in cls.report["sources"]}

    def test_counts_total_and_verdict_buckets(self):
        counts = self.report["counts"]
        self.assertEqual(counts["total"], 15)
        self.assertEqual(counts["by_docket_verdict"],
                         {"VERIFIED": 7, "PARTIAL": 1, "UNVERIFIED": 7})
        self.assertEqual(sum(counts["by_docket_verdict"].values()), counts["total"])

    def test_registry_status_counts_are_separate_from_the_docket_verdict(self):
        self.assertEqual(self.report["counts"]["by_registry_status"],
                         {"CATALOGUED": 4, "NOT_DOWNLOADED": 4, "ACCESS_VERIFIED": 0,
                          "VERIFIED": 7})
        self.assertEqual(self.report["verified"],
                         ["EIA", "EUROSTAT", "FRED_ALFRED", "OECD_ICIO", "UN_COMTRADE",
                          "USDA_PSD", "WORLD_BANK"])

    def test_partial_list_is_the_close_ring(self):
        self.assertEqual(self.report["partial"], ["BLS"])
        for source_id in self.report["partial"]:
            record = self.by_id[source_id]
            self.assertFalse(record["citation_allowed"])
            self.assertIn("retrieved_at", record["missing"])

    def test_no_automatic_promotion_or_demotion(self):
        for record in self.report["sources"]:
            if record["docket_verdict"] == "VERIFIED":
                self.assertEqual(record["registry_status"], "VERIFIED", record["source_id"])
            if record["registry_status"] == "VERIFIED":
                self.assertEqual(record["docket_verdict"], "VERIFIED", record["source_id"])
            if record["registry_status"] == "ACCESS_VERIFIED":
                self.assertNotEqual(record["docket_verdict"], "VERIFIED", record["source_id"])
        self.assertTrue(self.report["invariants"]["no_network"])
        self.assertFalse(self.report["invariants"]["auto_promotion"])
        self.assertFalse(self.report["invariants"]["registry_writes"])
        self.assertTrue(self.report["invariants"]["inputs_unchanged"])

    def test_only_verified_may_be_cited_as_observations(self):
        for record in self.report["sources"]:
            self.assertEqual(record["citation_allowed"],
                             record["docket_verdict"] == "VERIFIED", record["source_id"])

    def test_gaps_cover_every_non_verified_source(self):
        gaps = self.report["gaps"]
        self.assertEqual(len(gaps), 8)
        self.assertEqual([gap["source_id"] for gap in gaps],
                         sorted(set(self.by_id) - set(self.report["verified"])))
        for gap in gaps:
            self.assertTrue(gap["missing"], gap["source_id"])
            self.assertIn("retrieved_at", gap["missing"])
            self.assertIn("dataset_hash", gap["missing"])

    def test_missing_vocabulary_is_the_promotion_evidence(self):
        allowed = set(sv.PROMOTION_ITEMS)
        for record in self.report["sources"]:
            self.assertEqual(set(record["evidence"]), allowed)
            self.assertTrue(set(record["missing"]) <= allowed)
            self.assertTrue(set(record["weak_evidence"]) <= allowed)
            if record["docket_verdict"] != "VERIFIED":
                self.assertEqual(record["missing"],
                                 [item for item in sv.ITEM_ORDER
                                  if not record["evidence"][item]])

    def test_verified_sources_with_weak_evidence_are_reported(self):
        # The registry status is not demoted; the missing evidence is named instead.
        self.assertIn("dataset_hash", self.by_id["WORLD_BANK"]["weak_evidence"])
        self.assertEqual(self.by_id["WORLD_BANK"]["dataset_hash"]["state"], "absent")
        self.assertEqual(self.by_id["EIA"]["dataset_hash"]["state"], "full")
        self.assertEqual(len(self.by_id["EIA"]["dataset_hash"]["value"]), 64)
        self.assertEqual(self.by_id["OECD_ICIO"]["dataset_hash"]["state"], "truncated")
        self.assertTrue(any("усечён" in warning
                            for warning in self.by_id["OECD_ICIO"]["warnings"]))
        # FRED_ALFRED was promoted on 2026-11-26 with a pinned file, dataset hash and terms
        # evidence, so it is the one VERIFIED source with no weak evidence left.
        self.assertEqual(self.by_id["FRED_ALFRED"]["weak_evidence"], [])
        self.assertEqual(self.by_id["FRED_ALFRED"]["dataset_hash"]["state"], "full")
        for source_id in self.report["verified"]:
            if source_id == "FRED_ALFRED":
                continue
            self.assertTrue(self.by_id[source_id]["weak_evidence"], source_id)

    def test_journal_mapping_is_explicit_and_counted(self):
        journal = self.report["licence_journal"]
        self.assertEqual(journal["entries"], 30)
        self.assertEqual(journal["mapped_to_registry"], ["BLS", "FRED_ALFRED"])
        self.assertEqual(len(journal["unmapped_entries"]), 28)
        self.assertEqual(self.by_id["BLS"]["licence_entry"]["id"], "BLS_PRIMARY")
        self.assertEqual(self.by_id["FRED_ALFRED"]["licence_entry"]["id"],
                         "FRED_ALFRED_SERVICES")
        self.assertIsNone(self.by_id["EIA"]["licence_entry"]["id"])


# ------------------------------------------------------------------------------ licence matrix
class LicenceMatrix(unittest.TestCase):
    def test_matrix_is_complete_and_symmetric(self):
        matrix = sv.compatibility_matrix()
        self.assertEqual(matrix["classes"], ["OPEN", "ATTRIBUTION", "RESTRICTED", "UNKNOWN"])
        for left in sv.LICENCE_CLASSES:
            self.assertEqual(set(matrix["matrix"][left]), set(sv.LICENCE_CLASSES))
            for right in sv.LICENCE_CLASSES:
                self.assertIn(matrix["matrix"][left][right], sv.MATRIX_VERDICTS)
                self.assertEqual(matrix["matrix"][left][right],
                                 matrix["matrix"][right][left])

    def test_combination_rules_are_fail_closed(self):
        self.assertEqual(sv.combine_licences("OPEN", "OPEN"), "OK")
        self.assertEqual(sv.combine_licences("OPEN", "ATTRIBUTION"), "ATTRIBUTION_REQUIRED")
        self.assertEqual(sv.combine_licences("ATTRIBUTION", "ATTRIBUTION"),
                         "ATTRIBUTION_REQUIRED")
        self.assertEqual(sv.combine_licences("OPEN", "RESTRICTED"), "BLOCKED")
        self.assertEqual(sv.combine_licences("RESTRICTED", "RESTRICTED"), "BLOCKED")
        self.assertEqual(sv.combine_licences("UNKNOWN", "OPEN"), "BLOCKED_UNKNOWN")
        self.assertEqual(sv.combine_licences("UNKNOWN", "UNKNOWN"), "BLOCKED_UNKNOWN")
        with self.assertRaises(sv.SourceVerificationError):
            sv.combine_licences("PUBLIC", "OPEN")

    def test_publication_rule_blocks_restricted_and_unknown(self):
        rule = sv.compatibility_matrix()
        self.assertIn("matrix", rule)
        report = sv.build_docket(REGISTRY, LICENCES)
        publication = report["licence_matrix"]["publication_rule"]
        self.assertTrue(publication["derived_statistics_only"])
        self.assertEqual(publication["allowed_classes"], ["OPEN", "ATTRIBUTION"])
        self.assertEqual(publication["blocked_classes"], ["RESTRICTED", "UNKNOWN"])

    def test_source_classes_are_derived_and_flags_are_tracked(self):
        report = sv.build_docket(REGISTRY, LICENCES)
        by_id = {record["source_id"]: record for record in report["sources"]}
        self.assertEqual(by_id["EIA"]["licence"]["class"], "OPEN")
        self.assertEqual(by_id["USDA_PSD"]["licence"]["class"], "OPEN")
        self.assertEqual(by_id["WORLD_BANK"]["licence"]["class"], "ATTRIBUTION")
        self.assertEqual(by_id["BLS"]["licence"]["class"], "ATTRIBUTION")
        self.assertEqual(by_id["IEA"]["licence"]["class"], "RESTRICTED")
        self.assertEqual(by_id["FAO_FAOSTAT"]["licence"]["class"], "ATTRIBUTION")
        self.assertTrue(by_id["FAO_FAOSTAT"]["licence"]["share_alike"])
        self.assertTrue(by_id["FAO_FAOSTAT"]["licence"]["non_commercial"])
        self.assertFalse(by_id["BLS"]["licence"]["non_commercial"])
        flags = report["licence_matrix"]["class_flags"]
        self.assertEqual(flags["share_alike"], ["FAO_FAOSTAT"])
        self.assertEqual(report["counts"]["by_licence_class"],
                         {"OPEN": 3, "ATTRIBUTION": 8, "RESTRICTED": 4, "UNKNOWN": 0})


# ----------------------------------------------------------------------------------- fixtures
class DerivedVerdicts(Workspace):
    def test_journal_terms_make_a_catalogued_source_partial(self):
        report = self.build([source(source_id="NEAR")],
                            [entry(id="NEAR", terms_sha256="a" * 64)])
        record = report["sources"][0]
        self.assertEqual(record["docket_verdict"], "PARTIAL")
        self.assertEqual(record["missing"], ["retrieved_at", "dataset_hash"])

    def test_catalogue_without_terms_hash_stays_unverified(self):
        report = self.build([source(source_id="FAR")], [entry(id="FAR")])
        record = report["sources"][0]
        self.assertEqual(record["docket_verdict"], "UNVERIFIED")
        self.assertIn("terms_evidence", record["missing"])

    def test_paid_source_can_never_be_partial(self):
        report = self.build(
            [source(source_id="PAID", access="paid", status="NOT_DOWNLOADED")],
            [entry(id="PAID", terms_sha256="b" * 64)])
        record = report["sources"][0]
        self.assertEqual(record["docket_verdict"], "UNVERIFIED")
        self.assertTrue(any("платный" in warning for warning in record["warnings"]))

    def test_unknown_licence_forces_unverified(self):
        report = self.build([source(source_id="MYSTERY", license="unknown terms")], [])
        record = report["sources"][0]
        self.assertEqual(record["licence"]["class"], "UNKNOWN")
        self.assertEqual(record["docket_verdict"], "UNVERIFIED")
        self.assertIn("licence", record["missing"])

    def test_restricted_journal_entry_overrides_public_domain_text(self):
        report = self.build([source(source_id="PD", license="public domain")],
                            [entry(id="PD", status="RESTRICTED", conditions=[])])
        record = report["sources"][0]
        self.assertEqual(record["licence"]["class"], "RESTRICTED")
        self.assertEqual(record["licence"]["basis"], "licence_register:PD")

    def test_open_entry_with_attribution_upgrades_public_domain(self):
        report = self.build([source(source_id="PD", license="public domain")],
                            [entry(id="PD", conditions=["citation required"])])
        record = report["sources"][0]
        self.assertEqual(record["licence"]["class"], "ATTRIBUTION")
        self.assertTrue(record["licence"]["attribution_required"])

    def test_verified_requires_retrieval_hash_and_note(self):
        report = self.build([source(source_id="OK", status="VERIFIED", retrieved_at="2026-11-16",
                                    notes="dataset sha256 " + "c" * 64)] , [])
        record = report["sources"][0]
        self.assertEqual(record["docket_verdict"], "VERIFIED")
        self.assertEqual(record["dataset_hash"]["state"], "full")
        self.assertEqual(record["missing"], [])
        self.assertIn("licence_entry", record["weak_evidence"])
        self.assertTrue(record["citation_allowed"])

    def test_ambiguous_journal_match_is_not_resolved_silently(self):
        report = self.build([source(source_id="MULTI")],
                            [entry(id="MULTI_A"), entry(id="MULTI_B")])
        record = report["sources"][0]
        self.assertIsNone(record["licence_entry"]["id"])
        self.assertEqual(record["licence_entry"]["matched_by"], "ambiguous")
        self.assertTrue(any("несколько" in warning for warning in record["warnings"]))


# --------------------------------------------------------------------------------- fail-closed
class FailClosed(Workspace):
    def test_missing_registry_file(self):
        licences = self.write("licences.json", journal(entry()))
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(self.dir / "absent.json", licences)

    def test_broken_json_is_rejected(self):
        registry = self.write("registry.json", "{not json")
        licences = self.write("licences.json", journal(entry()))
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(registry, licences)

    def test_verified_without_retrieved_at_is_rejected(self):
        registry = self.write("registry.json",
                              wrapped(source(status="VERIFIED", retrieved_at=None)))
        licences = self.write("licences.json", journal(entry()))
        with self.assertRaises(sv.SourceVerificationError) as ctx:
            sv.build_docket(registry, licences)
        self.assertIn("VERIFIED", str(ctx.exception))

    def test_duplicate_source_ids_are_rejected(self):
        registry = self.write("registry.json", wrapped(
            source(source_id="DUP", url="https://example.org/a"),
            source(source_id="DUP", url="https://example.org/b")))
        licences = self.write("licences.json", journal(entry(id="DUP")))
        with self.assertRaises(sv.SourceVerificationError) as ctx:
            sv.build_docket(registry, licences)
        self.assertIn("duplicate", str(ctx.exception))

    def test_unknown_status_is_rejected(self):
        registry = self.write("registry.json", wrapped(source(status="MAYBE")))
        licences = self.write("licences.json", journal(entry()))
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(registry, licences)

    def test_unknown_licence_status_is_rejected(self):
        registry = self.write("registry.json", wrapped(source()))
        licences = self.write("licences.json", journal(entry(status="PROBABLY")))
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(registry, licences)

    def test_duplicate_journal_entry_is_rejected(self):
        registry = self.write("registry.json", wrapped(source()))
        licences = self.write("licences.json", journal(entry(id="ONE"), entry(id="ONE")))
        with self.assertRaises(sv.SourceVerificationError) as ctx:
            sv.build_docket(registry, licences)
        self.assertIn("duplicate", str(ctx.exception))

    def test_bad_terms_hash_is_rejected(self):
        registry = self.write("registry.json", wrapped(source()))
        licences = self.write("licences.json", journal(entry(terms_sha256="short")))
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(registry, licences)

    def test_wrong_register_id_is_rejected(self):
        registry = self.write("registry.json", wrapped(source()))
        licences = self.write("licences.json",
                              {"register_id": "OTHER", "entries": [entry()]})
        with self.assertRaises(sv.SourceVerificationError):
            sv.build_docket(registry, licences)

    def test_output_equal_to_an_input_is_refused(self):
        report = sv.build_docket(REGISTRY, LICENCES)
        before = REGISTRY.read_bytes()
        with self.assertRaises(sv.SourceVerificationError):
            sv.write_report(report, REGISTRY, inputs=(REGISTRY, LICENCES))
        self.assertEqual(REGISTRY.read_bytes(), before)

    def test_output_directory_must_exist(self):
        report = sv.build_docket(REGISTRY, LICENCES)
        with self.assertRaises(sv.SourceVerificationError):
            sv.write_report(report, self.dir / "missing" / "docket.json")

    def test_integrity_violations_are_detected_in_a_tampered_docket(self):
        report = sv.build_docket(REGISTRY, LICENCES)
        tampered = json.loads(sv.dumps(report))
        tampered["sources"][0]["docket_verdict"] = "VERIFIED"
        tampered["sources"][0]["registry_status"] = "CATALOGUED"
        violations = sv.validate_invariants(tampered)
        self.assertTrue(any("promotion" in item for item in violations), violations)


# ------------------------------------------------------------------------------------- offline
class NoNetwork(Workspace):
    def test_source_has_no_networking_imports(self):
        text = TOOL.read_text(encoding="utf-8")
        for forbidden in ("import socket", "import urllib", "import http", "import requests",
                          "from urllib", "urlopen", "requests."):
            self.assertNotIn(forbidden, text, forbidden)

    def test_docket_is_built_with_sockets_disabled(self):
        def boom(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("network access attempted")
        with mock.patch("socket.socket", boom), mock.patch("socket.create_connection", boom):
            report = sv.build_docket(REGISTRY, LICENCES)
        self.assertEqual(report["counts"]["total"], 15)

    def test_cli_runs_with_sockets_disabled(self):
        def boom(*args, **kwargs):  # pragma: no cover
            raise AssertionError("network access attempted")
        buffer = StringIO()
        with mock.patch("socket.socket", boom), redirect_stdout(buffer):
            code = sv.main(["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["ok"])


# --------------------------------------------------------------------------------- determinism
class Determinism(Workspace):
    def test_two_builds_are_byte_identical(self):
        first = sv.dumps(sv.build_docket(REGISTRY, LICENCES))
        second = sv.dumps(sv.build_docket(REGISTRY, LICENCES))
        self.assertEqual(first, second)

    def test_json_is_canonical(self):
        report = sv.build_docket(REGISTRY, LICENCES)
        canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        self.assertEqual(sv.dumps(report), canonical)

    def test_out_writes_identical_files(self):
        first = self.dir / "one.json"
        second = self.dir / "two.json"
        report = sv.build_docket(REGISTRY, LICENCES)
        sv.write_report(report, first, inputs=(REGISTRY, LICENCES))
        sv.write_report(report, second, inputs=(REGISTRY, LICENCES))
        self.assertEqual(first.read_bytes(), second.read_bytes())


# ------------------------------------------------------------------------------------------- cli
class CommandLine(Workspace):
    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sv.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_default_summary_exit_zero(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("VERIFIED 7", out)
        self.assertIn("PARTIAL 1", out)
        self.assertIn("UNVERIFIED 7", out)
        self.assertIn("Матрица", out)

    def test_check_mode_reports_ok(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("OK: 15", out)
        self.assertIn("нарушений нет", out)

    def test_json_mode_is_machine_readable(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["schema"], sv.SCHEMA)
        self.assertTrue(report["ok"])
        self.assertEqual(report["violations"], [])

    def test_bad_input_returns_two_and_json_error(self):
        code, out, _ = self.run_cli("--registry", str(self.dir / "absent.json"), "--json")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

    def test_bad_input_human_error_goes_to_stderr(self):
        code, out, err = self.run_cli("--registry", str(self.dir / "absent.json"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("fail-closed", err)

    def test_out_writes_the_docket(self):
        target = self.dir / "docket.json"
        code, _, _ = self.run_cli("--json", "--out", str(target))
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["counts"]["total"], 15)

    def test_out_to_an_input_is_refused_without_writing(self):
        before = REGISTRY.read_bytes()
        code, _, err = self.run_cli("--out", str(REGISTRY))
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)
        self.assertEqual(REGISTRY.read_bytes(), before)

    def test_violations_make_the_check_fail(self):
        tampered = sv.build_docket(REGISTRY, LICENCES)
        tampered = json.loads(sv.dumps(tampered))
        tampered["sources"][0]["docket_verdict"] = "VERIFIED"
        tampered["sources"][0]["registry_status"] = "CATALOGUED"
        tampered["ok"] = False
        tampered["violations"] = sv.validate_invariants(tampered)
        with mock.patch.object(sv, "build_docket", return_value=tampered):
            code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_violations_block_the_output_file(self):
        tampered = sv.build_docket(REGISTRY, LICENCES)
        tampered = json.loads(sv.dumps(tampered))
        tampered["sources"][0]["docket_verdict"] = "VERIFIED"
        tampered["sources"][0]["registry_status"] = "CATALOGUED"
        tampered["ok"] = False
        tampered["violations"] = sv.validate_invariants(tampered)
        target = self.dir / "blocked.json"
        with mock.patch.object(sv, "build_docket", return_value=tampered):
            code, _, _ = self.run_cli("--out", str(target))
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())

    def test_custom_registry_paths_are_honoured(self):
        registry = self.write("r.json", wrapped(
            source(source_id="ONE", retrieved_at=None),
            source(source_id="TWO", url="https://example.org/two")))
        licences = self.write("l.json", journal(entry(id="ONE")))
        code, out, _ = self.run_cli("--registry", str(registry), "--licences", str(licences),
                                    "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["counts"]["total"], 2)
        self.assertEqual(report["counts"]["by_docket_verdict"]["UNVERIFIED"], 2)


# -------------------------------------------------------------------------------------- docs
class Documentation(unittest.TestCase):
    def test_document_exists_and_states_the_current_ratio(self):
        self.assertTrue(DOC.is_file())
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("3/15", text)
        self.assertIn("205", text)

    def test_document_names_the_matrix_and_the_limits(self):
        text = DOC.read_text(encoding="utf-8")
        for token in ("OPEN", "ATTRIBUTION", "RESTRICTED", "UNKNOWN", "PARTIAL",
                      "метаданные", "юридическ"):
            self.assertIn(token, text)
        self.assertIn("source_verification.py", text)
        self.assertIn("test_source_verification.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
