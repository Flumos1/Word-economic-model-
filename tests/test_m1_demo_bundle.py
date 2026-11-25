#!/usr/bin/env python3
"""Тесты единой приёмки M1 (tools/m1_demo_bundle.py, docs/237)."""
from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "m1_demo_bundle.py"
spec = importlib.util.spec_from_file_location("m1_demo_bundle", TOOL)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900)


class CheckPlan(unittest.TestCase):
    def test_schema_and_status(self):
        self.assertEqual(bundle.SCHEMA, "amfe.m1.acceptance.v1")
        self.assertEqual(bundle.STATUS, "RESEARCH_ONLY")

    def test_plan_lists_all_acceptance_lines(self):
        ids = [item["id"] for item in bundle.CHECKS]
        for expected in ("bridge_built", "client_pack", "client_pack_pdf", "no_violations",
                         "simulator_demo"):
            self.assertIn(expected, ids)

    def test_every_check_has_title_and_how(self):
        for item in bundle.CHECKS:
            self.assertTrue(item["title"].strip(), item["id"])
            self.assertTrue(item["how"].strip(), item["id"])

    def test_cli_check_prints_the_plan(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.m1.acceptance.v1")
        self.assertEqual(len(payload["checks"]), 5)


class BundleRun(unittest.TestCase):
    """Полный прогон без PDF: четыре проверки проходят, PDF честно помечен как невыполненный."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "bundle"
        cls.summary = bundle.run_bundle(cls.out, make_pdf=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_not_admitted_and_research_only(self):
        self.assertEqual(self.summary["status"], "RESEARCH_ONLY")
        self.assertIs(self.summary["admission"], False)

    def test_bridge_is_built_or_partial(self):
        check = next(item for item in self.summary["checks"] if item["id"] == "bridge_built")
        self.assertTrue(check["ok"])
        self.assertIn(check["detail"]["status"], ("RESEARCH_ONLY", "PARTIAL_INPUTS"))
        self.assertIs(check["detail"]["checks"]["no_raw_data"], True)

    def test_client_pack_is_built_with_bridge_and_blocks(self):
        check = next(item for item in self.summary["checks"] if item["id"] == "client_pack")
        self.assertTrue(check["ok"])
        self.assertEqual(check["detail"]["status"], "RESEARCH_ONLY")
        self.assertEqual(check["detail"]["modules"], 9)
        self.assertIs(check["detail"]["bridge_available"], True)
        self.assertEqual(check["detail"]["bridge_scenarios"], 3)

    def test_pdf_check_is_honest_without_export(self):
        check = next(item for item in self.summary["checks"] if item["id"] == "client_pack_pdf")
        self.assertFalse(check["ok"])
        self.assertEqual(check["detail"]["pdfs"], [])
        self.assertEqual(self.summary["failed"], ["client_pack_pdf"])
        self.assertFalse(self.summary["ok"])

    def test_no_admission_or_raw_data_claims(self):
        check = next(item for item in self.summary["checks"] if item["id"] == "no_violations")
        self.assertTrue(check["ok"])
        self.assertIs(check["detail"]["admitted"], False)
        self.assertEqual(check["detail"]["status"], "RESEARCH_ONLY")

    def test_simulator_demo_runs_six_scenarios(self):
        check = next(item for item in self.summary["checks"] if item["id"] == "simulator_demo")
        self.assertTrue(check["ok"])
        self.assertEqual(check["detail"]["steps_run"], 6)
        self.assertIs(check["detail"]["all_research_only"], True)
        self.assertEqual(sorted(check["detail"]["scenarios"]),
                         sorted(bundle.demo.simulator_ui.SCENARIO_BY_ID))

    def test_pack_files_are_written_to_the_out_dir(self):
        forced = self.out / "client_pack"
        self.assertTrue((forced / "04_Machine_Readable.json").is_file())
        self.assertTrue((forced / "07_Reproducibility.json").is_file())

    def test_build_is_cleaned_up(self):
        self.assertFalse((ROOT / "build" / "m1_bundle").exists())

    def test_deterministic_pack_has_seven_files(self):
        manifest = json.loads((self.out / "client_pack" / "07_Reproducibility.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(manifest["file_count"], 7)
        self.assertEqual(manifest["file_count"], len(manifest["deterministic_artifacts"]))


class BundleRunCli(unittest.TestCase):
    def test_cli_run_without_pdf_returns_nonzero_and_reports_the_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run("--run", "--no-pdf", "--out", tmp)
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["failed"], ["client_pack_pdf"])

    def test_cli_without_run_is_a_usage_hint(self):
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RESEARCH_ONLY")


class Documentation(unittest.TestCase):
    def test_doc_exists_and_states_the_honest_limits(self):
        text = (ROOT / "docs" / "237_Единая_приёмка_M1.md").read_text(encoding="utf-8").lower()
        for token in ("--check", "--run", "--no-pdf", "research_only", "admission=false",
                      "не sla", "не деплой"):
            self.assertIn(token.lower(), text)


if __name__ == "__main__":
    unittest.main()
