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


# ================================================================================== P2 0.2
def _write02(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def make_fixture(tmp: Path, *, scenario_count: int = 8, energy_plus50: bool = True,
                 d1_status: str = "D1_PASS") -> dict:
    """Synthetic P2 0.2 tree: repo (docs/tools/tests), data root and world output."""
    repo = tmp / "repo"
    for relative in (p2_release.REQUIRED_DOCS_02 + p2_release.REQUIRED_TOOLS_02
                     + p2_release.REQUIRED_TESTS_02):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    _write02(repo / "protocols" / "oecd_icio_coverage.json", {
        "source_id": "oecd_icio",
        "release_2025": {"status": "AVAILABLE", "economies": 81, "industries": 50,
                         "sectors": 4050, "years": "1995-2022", "datasets_written": 28,
                         "units": "USD_mn", "fd_convention": "product total over destinations",
                         "channel": "webfs-sti.oecd.org", "archives": {"count": 5}}})
    _write02(repo / "protocols" / "evaluation_protocol_v5.json", {"version": "5.0.0"})
    data_root = tmp / "data"
    _write02(data_root / "oecd_icio_2025" / "reports" / "d1.json", {
        "status": d1_status,
        "datasets": [{"year": [2019], "d1": d1_status == "D1_PASS",
                      "coverage": {"covered_share_of_world_va": 0.9567,
                                   "world_bank_cross_check": {"countries_matched": 54}}}]})
    _write02(data_root / "oecd_icio_2025" / "reports" / "manifest.json", {
        "schema": "amfe.mrio.manifest.v1.1",
        "coverage": {"min": 0.946, "max": 0.972, "gate": 0.80}})
    world_out = tmp / "world"
    sha = "c3ac04b6c8337e34ef16e9f7f55c0808dd481fcae7bebfaa1ed819c44b80673d"
    _write02(world_out / "world_pipeline_report.json", {
        "status": "OK",
        "dataset": {"sha256": sha, "bytes": 94661541},
        "solve": {"spectral_radius": 0.593807523563, "total_output": 168480917.490438,
                  "productive": True, "max_abs_material_residual": 26471.49,
                  "sectors": 4050, "stored_A": 9778368},
        "core": {"spectral_radius": 0.581383375695, "productive": True,
                 "hawkins_simon_pass": True, "max_abs_balance_residual": 2.3e-10},
        "validation": {"ok": True, "checks": {"non_negative": True, "labels": True}}})
    scenarios = []
    ids = ["energy_minus5", "import_plus10", "import_plus20", "fx_minus10_USA",
           "fx_minus20_USA", "fx_minus10_DEU", "fx_minus20_DEU"]
    if energy_plus50:
        ids.insert(1, "energy_plus50")
    while len(ids) < scenario_count:
        ids.append("extra_%d" % len(ids))
    for scenario_id in ids[:scenario_count]:
        scenarios.append({"id": scenario_id,
                          "headline": {"total_delta_output": -1000.0,
                                       "relative_delta_output": -0.0006,
                                       "largest_country": "USA",
                                       "largest_industry": "B05",
                                       "max_abs_material_residual": 9e-9}})
    _write02(world_out / "report_index.json", {
        "status": "OK",
        "dataset": {"sha256": sha, "bytes": 94661541, "sectors": 4050, "years": [2019],
                    "units": "USD_mn"},
        "scenarios": scenarios,
        "checks": {"scenario_count": scenario_count, "all_feasible": True,
                   "all_validations_pass": True, "all_sfc_pass": True,
                   "max_abs_material_residual": 9e-9},
        "sfc": {"pass": True, "totals": {"savings": 1.0, "investment": 1.0,
                                         "current_account": 0.0},
                "checks": {"saving_investment_identity_max_abs": 0.0,
                           "global_current_account_sum": 0.0},
                "assumptions": {"investment_share": 0.2}}})
    return {"repo": repo, "data": data_root, "world": world_out}


class Manifest02Structure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixture = make_fixture(Path(cls.tmp.name))
        cls.manifest = p2_release.build_manifest_02(
            repo=cls.fixture["repo"], data_root=cls.fixture["data"],
            world_out=cls.fixture["world"], generated_at=FIXED, date=FIXED_DATE,
            git_commit="test-commit")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_release_metadata(self):
        manifest = self.manifest
        self.assertEqual(manifest["schema"], p2_release.SCHEMA_02)
        self.assertEqual(manifest["release"], "P2 0.2")
        self.assertEqual(manifest["version"], "0.2")
        self.assertEqual(manifest["status"], "RESEARCH_RELEASE")
        self.assertFalse(manifest["admission"])
        self.assertIn("не является допуском", manifest["admission_statement"])

    def test_criteria_present_and_d4_research_only(self):
        ids = [entry["id"] for entry in self.manifest["criteria"]]
        self.assertEqual(ids, ["D1", "D2", "D3", "D4", "D5", "D6"])
        summary = self.manifest["criteria_summary"]
        self.assertEqual(summary["D4"], "RESEARCH_ONLY")
        self.assertEqual(summary["D1"], "PASS")
        self.assertEqual(summary["D2"], "PASS")
        self.assertEqual(summary["D3"], "PASS")
        self.assertEqual(summary["D5"], "PASS")
        self.assertIn(summary["D6"], ("PASS", "PARTIAL"))

    def test_world_numbers_and_energy_scenario(self):
        world = self.manifest["world"]
        oecd = world["oecd_2025"]
        self.assertEqual((oecd["economies"], oecd["industries"], oecd["sectors"]),
                         (81, 50, 4050))
        self.assertAlmostEqual(oecd["baseline"]["spectral_radius"], 0.593807523563, places=12)
        self.assertAlmostEqual(oecd["baseline"]["total_output"], 168480917.490438, places=6)
        self.assertEqual(oecd["d1"]["datasets"], 1)
        scenarios = world["scenarios"]
        self.assertEqual(scenarios["scenario_count"], 8)
        self.assertTrue(scenarios["has_energy_plus50"])
        self.assertIn("energy_plus50", scenarios["scenario_ids"])
        self.assertTrue(world["sfc"]["pass"])

    def test_inputs_are_hashed_and_deterministic(self):
        inputs = {entry["id"]: entry for entry in self.manifest["inputs"]}
        for key in ("oecd_coverage", "world_index", "world_pipeline", "oecd_2025_d1",
                    "oecd_2025_manifest"):
            self.assertTrue(inputs[key]["sha256"])
            self.assertTrue(inputs[key]["bytes"] > 0)
        second = p2_release.build_manifest_02(
            repo=self.fixture["repo"], data_root=self.fixture["data"],
            world_out=self.fixture["world"], generated_at=FIXED, date=FIXED_DATE,
            git_commit="test-commit")
        self.assertEqual(json.dumps(self.manifest, sort_keys=True),
                         json.dumps(second, sort_keys=True))


class Manifest02FailClosed(unittest.TestCase):
    def test_missing_world_index_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp))
            (fixture["world"] / "report_index.json").unlink()
            with self.assertRaises(p2_release.ReleaseError):
                p2_release.build_manifest_02(repo=fixture["repo"], data_root=fixture["data"],
                                             world_out=fixture["world"], generated_at=FIXED,
                                             date=FIXED_DATE, git_commit="c")

    def test_missing_pipeline_report_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp))
            (fixture["world"] / "world_pipeline_report.json").unlink()
            with self.assertRaises(p2_release.ReleaseError):
                p2_release.build_manifest_02(repo=fixture["repo"], data_root=fixture["data"],
                                             world_out=fixture["world"], generated_at=FIXED,
                                             date=FIXED_DATE, git_commit="c")

    def test_missing_d1_report_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp))
            (fixture["data"] / "oecd_icio_2025" / "reports" / "d1.json").unlink()
            with self.assertRaises(p2_release.ReleaseError):
                p2_release.build_manifest_02(repo=fixture["repo"], data_root=fixture["data"],
                                             world_out=fixture["world"], generated_at=FIXED,
                                             date=FIXED_DATE, git_commit="c")

    def test_missing_energy_plus50_fails_d5(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp), energy_plus50=False)
            manifest = p2_release.build_manifest_02(
                repo=fixture["repo"], data_root=fixture["data"], world_out=fixture["world"],
                generated_at=FIXED, date=FIXED_DATE, git_commit="c")
            self.assertEqual(manifest["criteria_summary"]["D5"], "FAIL")
            self.assertFalse(manifest["world"]["scenarios"]["has_energy_plus50"])

    def test_seven_scenarios_fail_d5(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp), scenario_count=7)
            manifest = p2_release.build_manifest_02(
                repo=fixture["repo"], data_root=fixture["data"], world_out=fixture["world"],
                generated_at=FIXED, date=FIXED_DATE, git_commit="c")
            self.assertEqual(manifest["criteria_summary"]["D5"], "FAIL")

    def test_d1_failure_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_fixture(Path(tmp), d1_status="D1_FAIL")
            manifest = p2_release.build_manifest_02(
                repo=fixture["repo"], data_root=fixture["data"], world_out=fixture["world"],
                generated_at=FIXED, date=FIXED_DATE, git_commit="c")
            self.assertEqual(manifest["criteria_summary"]["D1"], "FAIL")


LOCAL_WORLD = ROOT / "build" / "world_oecd_2025" / "report_index.json"
LOCAL_PIPELINE = ROOT / "build" / "world_oecd_2025" / "world_pipeline_report.json"
LOCAL_DATA = Path("F:/Мировая экономика модель/data/mrio")


@unittest.skipUnless(LOCAL_WORLD.is_file() and LOCAL_PIPELINE.is_file()
                     and (LOCAL_DATA / "oecd_icio_2025" / "reports" / "d1.json").is_file(),
                     "local OECD 2025 artefacts are not present")
class Manifest02RealArtefacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = p2_release.build_manifest_02(
            repo=ROOT, data_root=LOCAL_DATA, generated_at=FIXED, date=FIXED_DATE,
            git_commit="test-commit")

    def test_real_oecd_2025_numbers(self):
        world = self.manifest["world"]
        oecd = world["oecd_2025"]
        self.assertEqual((oecd["economies"], oecd["industries"]), (81, 50))
        self.assertAlmostEqual(oecd["baseline"]["spectral_radius"], 0.593807523563, places=9)
        self.assertLess(oecd["baseline"]["spectral_radius"], 1.0)
        self.assertTrue(oecd["core"]["hawkins_simon_pass"])
        self.assertEqual(oecd["d1"]["status"], "D1_PASS")
        self.assertGreaterEqual(oecd["d1"]["coverage_min"], 0.80)
        self.assertFalse(self.manifest["admission"])
        self.assertEqual(self.manifest["criteria_summary"]["D4"], "RESEARCH_ONLY")
        self.assertEqual(self.manifest["criteria_summary"]["D5"], "PASS")
        self.assertTrue(world["scenarios"]["has_energy_plus50"])
        self.assertTrue(world["sfc"]["pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

