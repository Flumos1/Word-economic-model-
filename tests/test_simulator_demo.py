#!/usr/bin/env python3
"""Тесты сценарного 30-минутного демо Simulator (tools/simulator_demo.py, docs/231)."""
from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "simulator_demo.py"
spec = importlib.util.spec_from_file_location("simulator_demo", TOOL)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
simulator = demo.simulator_ui


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)


class RunbookStructure(unittest.TestCase):
    def setUp(self):
        self.document = demo.runbook_document()
        self.by_id = {spec_["id"]: spec_ for spec_ in simulator.SCENARIOS}

    def test_schema_and_flags(self):
        self.assertEqual(self.document["schema"], "amfe.simulator.demo.v1")
        self.assertEqual(self.document["status"], "RESEARCH_ONLY")
        self.assertIs(self.document["admission"], False)
        self.assertIs(self.document["causal_claim"], False)

    def test_exactly_six_steps(self):
        self.assertEqual(len(self.document["steps"]), 6)
        self.assertEqual([s["step"] for s in self.document["steps"]], [1, 2, 3, 4, 5, 6])

    def test_covers_every_declared_scenario_once(self):
        steps = [s["scenario"] for s in self.document["steps"]]
        self.assertEqual(sorted(steps), sorted(self.by_id))
        self.assertEqual(len(set(steps)), len(steps), "сценарии не должны повторяться")

    def test_payload_scenario_matches_step(self):
        for step in self.document["steps"]:
            self.assertEqual(step["payload"]["scenario"], step["scenario"])

    def test_each_step_expects_a_digest(self):
        for step in self.document["steps"]:
            self.assertIn("digest", step["expect"])

    def test_bilingual_titles_and_narratives(self):
        for step in self.document["steps"]:
            for field in ("title_ru", "title_en", "narrative_ru", "narrative_en"):
                self.assertTrue(step[field].strip(), f"шаг {step['step']}: пустое поле {field}")

    def test_minutes_are_ordered_and_fit_thirty_minutes(self):
        ranges = []
        for step in self.document["steps"]:
            low, high = step["minute"].replace("–", "-").split("-")
            ranges.append((int(low), int(high)))
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 30)
        for previous, current in zip(ranges, ranges[1:]):
            self.assertEqual(previous[1], current[0], "интервалы не должны пересекаться")

    def test_validate_accepts_current_runbook(self):
        self.assertEqual(demo.validate_runbook(self.document, self.by_id), [])


class RunbookValidation(unittest.TestCase):
    def setUp(self):
        self.document = demo.runbook_document()
        self.by_id = {spec_["id"]: spec_ for spec_ in simulator.SCENARIOS}

    def test_missing_scenario_is_rejected(self):
        broken = json.loads(json.dumps(self.document))
        broken["steps"].pop()
        self.assertTrue(demo.validate_runbook(broken, self.by_id))

    def test_unknown_scenario_is_rejected(self):
        broken = json.loads(json.dumps(self.document))
        broken["steps"][0]["scenario"] = "gold_shock"
        broken["steps"][0]["payload"]["scenario"] = "gold_shock"
        errors = demo.validate_runbook(broken, self.by_id)
        self.assertTrue(any("unknown scenario" in error for error in errors))

    def test_duplicate_scenario_is_rejected(self):
        broken = json.loads(json.dumps(self.document))
        broken["steps"][1]["scenario"] = broken["steps"][0]["scenario"]
        broken["steps"][1]["payload"]["scenario"] = broken["steps"][0]["scenario"]
        self.assertTrue(any("duplicate" in error
                            for error in demo.validate_runbook(broken, self.by_id)))

    def test_payload_mismatch_is_rejected(self):
        broken = json.loads(json.dumps(self.document))
        broken["steps"][0]["payload"]["scenario"] = "gas_shock"
        self.assertTrue(any("payload" in error
                            for error in demo.validate_runbook(broken, self.by_id)))

    def test_admission_true_is_rejected(self):
        broken = json.loads(json.dumps(self.document))
        broken["admission"] = True
        self.assertTrue(demo.validate_runbook(broken, self.by_id))


class RunbookExecution(unittest.TestCase):
    """Прогон всех шести шагов в процессе — без сервера и без сети."""

    @classmethod
    def setUpClass(cls):
        cls.document = demo.runbook_document()
        cls.app = simulator.SimulatorApp(geopolitics_mode="synthetic")

    def test_all_six_steps_run_and_stay_research_only(self):
        summary = demo.run_demo(self.document, self.app)
        self.assertEqual(summary["steps_run"], 6)
        self.assertTrue(summary["all_research_only"])
        for item in summary["steps"]:
            self.assertEqual(item["status"], "RESEARCH_ONLY", item["scenario"])
            self.assertIs(item["admission"], False, item["scenario"])
            self.assertIs(item["causal_claim"], False, item["scenario"])
            self.assertTrue(item["digest"], item["scenario"])
            self.assertGreater(item["cards"], 0, item["scenario"])

    def test_run_is_deterministic(self):
        first = demo.run_demo(self.document, self.app)
        second = demo.run_demo(self.document, self.app)
        self.assertEqual([s["digest"] for s in first["steps"]],
                         [s["digest"] for s in second["steps"]])

    def test_scenarios_are_the_declared_ones(self):
        summary = demo.run_demo(self.document, self.app)
        self.assertEqual([s["scenario"] for s in summary["steps"]],
                         [s["scenario"] for s in self.document["steps"]])


class CommandLine(unittest.TestCase):
    def test_check_passes(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["steps"], 6)
        self.assertEqual(len(payload["scenarios_covered"]), 6)

    def test_runbook_json_is_valid(self):
        result = run("--runbook-json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema"], "amfe.simulator.demo.v1")

    def test_runbook_files_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run("--runbook", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("runbook.md", "runbook_en.md"):
                path = Path(tmp) / name
                self.assertTrue(path.is_file(), name)
                text = path.read_text(encoding="utf-8")
                for scenario in simulator.SCENARIO_BY_ID:
                    self.assertIn(scenario, text, f"{name}: {scenario}")
                self.assertIn("RESEARCH_ONLY", text)

    def test_run_drives_all_scenarios(self):
        result = run("--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["steps_run"], 6)
        self.assertTrue(all(step["status"] == "RESEARCH_ONLY" for step in payload["steps"]))

    def test_no_network_or_file_writes_without_out(self):
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RESEARCH_ONLY")


class Documentation(unittest.TestCase):
    def test_doc_covers_commands_and_boundaries(self):
        text = (ROOT / "docs" / "231_Simulator_демо_сценарий.md").read_text(encoding="utf-8")
        for token in ("--check", "--run", "--runbook", "RESEARCH_ONLY", "admission=false",
                      "6 шагов" if "6 шагов" in text else "6"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
