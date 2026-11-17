"""Tests for the adversarial external review of G2 (tools/external_review_g2.py, docs/160).

The review is a checklist, so the tests do not trust it either: they re-run it on the real
documents/artifacts and on deliberately tampered copies, and they pin the three properties the
previous version lacked — every track is actually verified (no silent skipping of T1-T4), the API
envelope claims read the right key, and the artifact hashes are parsed from the documents instead
of being hard-coded. Fail-closed behaviour and determinism of the review are covered as well.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import external_review_g2 as er  # noqa: E402

_REQUIRED_ARTEFACTS = (
    ROOT / "build" / "market_bridge" / "bridge_report.json",
    ROOT / "build" / "api" / "index.json",
)
if not all(path.exists() for path in _REQUIRED_ARTEFACTS):
    raise unittest.SkipTest(
        "G2 review artefacts are absent (private MRIO data): build/market_bridge, build/api")



def abm_document() -> dict:
    done = subprocess.run([sys.executable, str(ROOT / er.REQUIRED_TOOLS["abm_sfc"]), "--json"],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise unittest.SkipTest(f"ABM demo unavailable: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)


def review(overrides: dict | None = None, abm: dict | None = None) -> dict:
    return er.build_review(repo=ROOT, run_help=False, run_abm=False,
                           abm_document=abm, doc_overrides=overrides)


class ProvenanceAllowListTest(unittest.TestCase):
    """G8: a public-domain derived aggregate is allowed only with a valid provenance sidecar."""

    def test_repository_aggregate_is_allowed_with_provenance(self):
        result = er._public_domain_aggregates(ROOT)
        self.assertIn("data/oil/brent_monthly_eia.csv", result["allowed"])
        self.assertEqual(result["problems"], [])

    def test_missing_or_tampered_provenance_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "data" / "oil").mkdir(parents=True)
            source = ROOT / "data" / "oil" / "brent_monthly_eia.csv"
            (repo / "data" / "oil" / "brent_monthly_eia.csv").write_bytes(source.read_bytes())
            # no metadata at all -> a problem, not an allowed aggregate
            result = er._public_domain_aggregates(repo)
            self.assertEqual(result["allowed"], [])
            self.assertTrue(any("provenance not found" in item["problem"]
                                for item in result["problems"]))
            # metadata with a wrong hash -> a problem
            meta = json.loads((ROOT / "data" / "oil" / "brent_monthly_eia.meta.json")
                              .read_text(encoding="utf-8"))
            meta["file_sha256"] = "0" * 64
            (repo / "data" / "oil" / "brent_monthly_eia.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            result = er._public_domain_aggregates(repo)
            self.assertEqual(result["allowed"], [])
            self.assertTrue(any("hash does not match" in item["problem"]
                                for item in result["problems"]))


class CoverageTest(unittest.TestCase):
    """The review must verify every track, not just the one whose document is an API contract."""

    @classmethod
    def setUpClass(cls):
        cls.abm = abm_document()
        cls.review = review(abm=cls.abm)

    def test_all_five_tracks_are_verified(self):
        check = next(item for item in self.review["checks"] if item["id"] == "G2")
        evidence = check["evidence"]
        for track in ("price_bridge", "agri_bridge", "oil_backtest", "abm_sfc", "forecast_api"):
            self.assertIn(track, evidence, f"{track} was silently skipped by the number check")
            self.assertGreaterEqual(evidence[track]["checked"], 5)
        self.assertGreaterEqual(evidence["total_claims"], 45)

    def test_published_review_is_consistent(self):
        check = next(item for item in self.review["checks"] if item["id"] == "G2")
        # On the current documents/artifacts the adversarial checklist has to come out clean;
        # any finding here is printed so the failure is diagnosable.
        self.assertEqual(check["status"], "PASS", json.dumps(check["findings"], ensure_ascii=False))

    def test_verdicts_and_sign_off(self):
        self.assertEqual(self.review["summary"]["status"], "PASS")
        self.assertEqual(self.review["sign_off"]["state"], "READY_FOR_HUMAN_SIGN_OFF")
        self.assertEqual(self.review["sign_off"]["blocked_by"], [])
        for track, verdict in self.review["summary"]["verdicts"].items():
            self.assertEqual(verdict, "CONFIRMED", track)


class TamperTest(unittest.TestCase):
    """A forged number in a track document must be caught, not accepted."""

    @classmethod
    def setUpClass(cls):
        cls.abm = abm_document()

    def test_tampered_price_bridge_number_is_caught(self):
        original = (ROOT / er.DOCS["price_bridge"]).read_text(encoding="utf-8")
        tampered = original.replace("**+5,91676%**", "**+9,91676%**", 1)
        self.assertNotEqual(original, tampered)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "152.md"
            path.write_text(tampered, encoding="utf-8")
            review = er.build_review(repo=ROOT, run_help=False, run_abm=False,
                                     abm_document=self.abm,
                                     doc_overrides={"price_bridge": str(path)})
        check = next(item for item in review["checks"] if item["id"] == "G2")
        messages = [item["message"] for item in check["findings"]]
        self.assertTrue(any("aggregate" in message.lower() for message in messages), messages)
        self.assertTrue(any(item["severity"] == "HIGH" for item in check["findings"]))

    def test_missing_hash_citation_is_not_silently_accepted(self):
        original = (ROOT / er.DOCS["oil_backtest"]).read_text(encoding="utf-8")
        tampered = original.replace("66c995121910e90a85a185531210b5f4508bdd3009378a905c51f915aa814ed7",
                                    "0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "155.md"
            path.write_text(tampered, encoding="utf-8")
            review = er.build_review(repo=ROOT, run_help=False, run_abm=False,
                                     abm_document=self.abm,
                                     doc_overrides={"oil_backtest": str(path)})
        check = next(item for item in review["checks"] if item["id"] == "G1")
        self.assertTrue(any(item["severity"] == "MEDIUM" for item in check["findings"]))
        self.assertEqual(check["status"], "WARN")


class PrimitivesTest(unittest.TestCase):
    """The three regressions that made the first version of the review useless."""

    def test_envelope_claim_reads_the_metric_key(self):
        forecast = json.loads((ROOT / er.ARTIFACTS["api_forecast"]).read_text(encoding="utf-8"))
        index = json.loads((ROOT / er.ARTIFACTS["api_index"]).read_text(encoding="utf-8"))
        contract = dict(index)
        contract["forecast_envelope"] = forecast
        claims = {claim.label: claim for claim in er._api_claims(contract)}
        self.assertEqual(claims["Brent peak in the envelope"].getter(contract),
                         forecast["envelope"]["price_peak"])
        self.assertEqual(claims["Brent peak spread"].getter(contract),
                         forecast["envelope"]["range_peak"]["spread_pct"])

    def test_artifact_claims_are_parsed_from_documents(self):
        docs = {track: (ROOT / relative).read_text(encoding="utf-8")
                for track, relative in er.DOCS.items()}
        claims = er._parse_artifact_claims(docs)
        self.assertIn(er.ARTIFACTS["price_bridge_report"], claims)
        self.assertIn(er.ARTIFACTS["oil_backtest_report"], claims)
        self.assertIn(er.ARTIFACTS["oil_backtest_markdown"], claims)
        for claimed in claims.values():
            self.assertGreaterEqual(len(claimed["sha256_prefix"]), 8)
            self.assertGreater(claimed["bytes"], 0)

    def test_number_normalisation_accepts_document_formats(self):
        self.assertEqual(er._normalise_number("5 916,76"), 5916.76)
        self.assertEqual(er._normalise_number("−19,95"), -19.95)
        self.assertEqual(er._normalise_number("79.389."), 79.389)
        self.assertEqual(er._normalise_number("2 292.6"), 2292.6)
        self.assertEqual(er._normalise_number("2,9"), 2.9)
        self.assertIsNone(er._normalise_number("n/a"))

    def test_all_claim_patterns_compile(self):
        abm = abm_document()
        index = json.loads((ROOT / er.ARTIFACTS["api_index"]).read_text(encoding="utf-8"))
        index["forecast_envelope"] = json.loads(
            (ROOT / er.ARTIFACTS["api_forecast"]).read_text(encoding="utf-8"))
        index["openapi"] = json.loads((ROOT / er.ARTIFACTS["api_openapi"]).read_text(encoding="utf-8"))
        index["scenario_paths"] = json.loads(
            (ROOT / "build" / "api" / "scenarios" / "oil.brent__hormuz_closed_6m.json")
            .read_text(encoding="utf-8"))["result"]["price_path"]
        factories = (er._price_bridge_claims, er._agri_bridge_claims, er._oil_backtest_claims,
                     lambda: er._abm_claims(abm), lambda: er._api_claims(index))
        for factory in factories:
            for claim in factory():
                self.assertIsInstance(claim.patterns, tuple,
                                      f"{claim.label}: patterns must be a tuple, not a string")
                for pattern in claim.patterns:
                    self.assertIsInstance(pattern, str)
                    self.assertGreater(len(pattern), 1)
                    try:
                        __import__("re").compile(pattern)
                    except Exception as error:  # noqa: BLE001 - the message is the assertion
                        self.fail(f"{claim.label}: bad regex {pattern!r}: {error}")


class FailClosedTest(unittest.TestCase):
    def test_missing_artifact_fails_closed(self):
        with self.assertRaises(er.ReviewError):
            er._read_json(ROOT / "build" / "does-not-exist.json", "missing")
        with self.assertRaises(er.ReviewError):
            er._read_text(ROOT / "docs" / "does-not-exist.md", "missing")

    def test_write_review_and_digest_are_deterministic(self):
        abm = abm_document()
        first = review(abm=abm)
        second = review(abm=abm)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(er.render_markdown(first), er.render_markdown(second))
        self.assertIn("adversarial review", er.render_markdown(first))
        self.assertIsNone(first["generated_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
