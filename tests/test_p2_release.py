"""P2 0.1 release manifest: schema, D1-D6 statuses, evidence consistency, determinism, fail-closed.

The suite reads the real release artefacts (no network, no data matrix): manifests, report indices,
protocol reports and documents.  It never writes into the repository: every build goes to a
temporary output path.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import p2_release  # noqa: E402

FIXED = "2026-11-14T20:30:00Z"
FIXED_DATE = "2026-11-14"


def build(**overrides):
    arguments = {"repo": ROOT, "generated_at": FIXED, "date": FIXED_DATE,
                 "git_commit": "test-commit"}
    arguments.update(overrides)
    return p2_release.build_manifest(**arguments)


class ManifestStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = build()

    def test_release_metadata(self):
        manifest = self.manifest
        self.assertEqual(manifest["schema"], p2_release.SCHEMA)
        self.assertEqual(manifest["release"], "P2 0.1")
        self.assertEqual(manifest["version"], "0.1")
        self.assertEqual(manifest["date"], FIXED_DATE)
        self.assertEqual(manifest["git_commit"], "test-commit")
        self.assertEqual(manifest["status"], "RESEARCH_RELEASE")
        self.assertFalse(manifest["admission"])
        self.assertIn("не является допуском", manifest["admission_statement"])
        self.assertIn("research release", " ".join(manifest["limitations"]).lower())

    def test_all_criteria_present_with_valid_statuses(self):
        manifest = self.manifest
        self.assertEqual([entry["id"] for entry in manifest["criteria"]], list("D1 D2 D3 D4 D5 D6".split()))
        for criterion in manifest["criteria"]:
            self.assertIn(criterion["status"], p2_release.STATUSES)
            self.assertTrue(criterion["title"])
            self.assertTrue(criterion["rationale"])
            self.assertIsInstance(criterion["evidence"], dict)
            self.assertIsInstance(criterion["limitations"], list)
        self.assertEqual(manifest["criteria_summary"],
                         {entry["id"]: entry["status"] for entry in manifest["criteria"]})

    def test_statuses_match_the_evidence(self):
        summary = self.manifest["criteria_summary"]
        world = self.manifest["world"]
        # D1: three real datasets, coverage above 80% everywhere
        self.assertEqual(summary["D1"], "PASS")
        self.assertGreaterEqual(world["figaro"]["gdp_coverage_named_pct"], 80.0)
        self.assertGreaterEqual(world["oecd"]["gdp_coverage_min_pct"], 80.0)
        self.assertGreaterEqual(world["wiod"]["gdp_coverage_min_pct"], 80.0)
        # D2/D3/D5: both pipelines and both SFC blocks pass
        for scenario in world["scenarios"]:
            self.assertEqual(scenario["scenario_count"], 8)
            self.assertTrue(scenario["all_validations_pass"])
            self.assertTrue(scenario["all_sfc_pass"])
        self.assertEqual(summary["D2"], "PASS")
        self.assertEqual(summary["D3"], "PASS")
        self.assertEqual(summary["D5"], "PASS")
        # D4: negative OOS is honestly recorded, so the criterion is research-only
        self.assertEqual(summary["D4"], "RESEARCH_ONLY")
        self.assertTrue(world["gvar"]["oos_negative"])
        self.assertLess(world["gvar"]["primary_better_cells"],
                        world["gvar"]["baseline_better_cells"])
        # D6: pipeline, tests and documents exist
        self.assertEqual(summary["D6"], "PASS")

    def test_world_summary_numbers(self):
        world = self.manifest["world"]
        figaro = world["figaro"]
        self.assertEqual((figaro["countries"], figaro["industries"]), (50, 64))
        self.assertEqual(figaro["sectors"], 3200)
        self.assertAlmostEqual(figaro["gdp_coverage_named_pct"], 87.2771, places=3)
        self.assertEqual(figaro["years"], [2019])
        oecd = world["oecd"]
        self.assertEqual((oecd["economies"], oecd["industries"]), (71, 34))
        self.assertAlmostEqual(oecd["gdp_coverage_min_pct"], 95.35, places=2)
        self.assertAlmostEqual(oecd["gdp_coverage_max_pct"], 95.47, places=2)
        wiod = world["wiod"]
        self.assertEqual((wiod["countries"], wiod["industries"]), (44, 56))
        self.assertGreaterEqual(wiod["year_count"], 3)
        self.assertEqual(wiod["years"], sorted(wiod["years"]))
        self.assertGreaterEqual(wiod["gdp_coverage_min_pct"], 84.0)
        self.assertLessEqual(wiod["gdp_coverage_max_pct"], 90.0)

    def test_candidates_are_not_admitted(self):
        candidates = self.manifest["world"]["candidates"]
        self.assertFalse(candidates["admitted"])
        self.assertIn("not deployment", candidates["note"])
        self.assertEqual(candidates["t1"]["status"], "CANDIDATE")
        self.assertFalse(candidates["t1"]["c4_pass"])
        self.assertEqual(candidates["regime"]["status"], "SHADOW_TEST")
        self.assertTrue(candidates["regime"]["conditional_data_class"])
        self.assertFalse(candidates["regime"]["stages"]["LIMITED_DEPLOYMENT_to_ACTIVE"])
        self.assertFalse(candidates["shadow"]["c6_pass"])

    def test_input_checksums_are_consistent(self):
        inputs = self.manifest["inputs"]
        self.assertGreaterEqual(len(inputs), 20)
        for entry in inputs:
            self.assertIn("id", entry)
            if not entry.get("exists"):
                self.assertIsNone(entry.get("sha256"))
                continue
            path = Path(entry["path"])
            if not path.is_absolute():
                path = ROOT / path
            self.assertTrue(path.is_file(), entry["id"])
            self.assertEqual(p2_release.sha256_file(path), entry["sha256"], entry["id"])
            self.assertEqual(path.stat().st_size, entry["bytes"], entry["id"])

    def test_required_inputs_are_hashed(self):
        ids = {entry["id"] for entry in self.manifest["inputs"]}
        for required in ("figaro_manifest", "oecd_coverage", "figaro_report_index", "report_v5",
                         "regime_report", "shadow_status", "protocol_v5"):
            self.assertIn(required, ids)
        self.assertTrue(any(entry.startswith("wiod_manifest_") for entry in ids))


class DeterminismAndFailClosed(unittest.TestCase):
    def test_determinism_except_generated_at(self):
        first = build()
        second = build()
        self.assertEqual(first, second)
        later = build(generated_at="2026-11-14T23:59:59Z")
        self.assertEqual(first["date"], later["date"])
        first_without = json.loads(json.dumps(first, sort_keys=True))
        later_without = json.loads(json.dumps(later, sort_keys=True))
        first_without.pop("generated_at")
        later_without.pop("generated_at")
        self.assertEqual(first_without, later_without)

    def test_written_manifest_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.json"
            right = Path(tmp) / "right.json"
            first = p2_release.write_manifest(build(), left)
            second = p2_release.write_manifest(build(), right)
            self.assertEqual(first, second)
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_missing_required_artifact_fails_closed_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p2_release.json"
            code = p2_release.main(["--repo", tmp, "--data-root", tmp, "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_broken_required_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "protocols").mkdir(parents=True)
            broken = repo / "protocols" / "first_real_evaluation_report_v5.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(p2_release.ReleaseError):
                p2_release.build_manifest(repo, data_root=Path(tmp) / "missing",
                                          generated_at=FIXED, git_commit="test")

    def test_cli_help(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "p2_release.py"), "--help"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("P2 0.1", completed.stdout)
        self.assertIn("--data-root", completed.stdout)

    def test_cli_writes_the_manifest_and_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p2_release.json"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "p2_release.py"),
                 "--out", str(out), "--generated-at", FIXED, "--date", FIXED_DATE,
                 "--git-commit", "test-commit"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["release"], "P2 0.1")
            self.assertEqual(summary["status"], "OK")
            self.assertTrue(out.is_file())
            document = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(document["release"], "P2 0.1")
            self.assertEqual(set(document["criteria_summary"]), {"D1", "D2", "D3", "D4", "D5", "D6"})
