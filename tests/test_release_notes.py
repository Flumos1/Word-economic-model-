"""Release 1.0 notes: structure, frozen hashes, status consistency and the release workflow.

The tests are offline: they compare ``protocols/release_notes_1.0.json`` with the committed artefacts
and pins, with the release-check report and with the documentation, validate the new
``.github/workflows/release-check.yml`` (PyYAML when available, structural checks otherwise) and make
sure the release notes never claim an admitted model or a created tag.
"""
import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "protocols" / "release_notes_1.0.json"
REPORT = ROOT / "protocols" / "release_check_report.json"
DOC = ROOT / "docs" / "96_Релиз_1.0_подготовка.md"
WORKFLOW = ROOT / ".github" / "workflows" / "release-check.yml"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def lf_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pin(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8").split()[0]


def numeric_arrays(value, found=None, threshold: int = 8) -> list:
    found = [] if found is None else found
    if isinstance(value, dict):
        for child in value.values():
            numeric_arrays(child, found, threshold)
    elif isinstance(value, list):
        numbers = [item for item in value if isinstance(item, (int, float))
                   and not isinstance(item, bool)]
        if len(numbers) > threshold:
            found.append(len(numbers))
        for child in value:
            numeric_arrays(child, found, threshold)
    return found


class ReleaseNotesStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notes = load(NOTES)

    def test_document_identity(self):
        notes = self.notes
        self.assertEqual(notes["document_id"], "AMFE-RELEASE-NOTES-1.0")
        self.assertIn(notes["version"], ("1.0.0",))
        self.assertRegex(notes["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(notes["release_date"], "2026-11-12")
        self.assertEqual(notes["release_type"], "research-platform")
        self.assertIn("hash_convention", notes)

    def test_status_block(self):
        status = self.notes["status"]
        self.assertIn(status["release"], ("PASS", "CONDITIONAL", "FAIL"))
        self.assertIn("release_check", status)
        self.assertIn("scientific", status)
        self.assertIn(status["release_check"]["status"], ("PASS", "CONDITIONAL", "FAIL"))
        self.assertIsInstance(status["release_check"]["blockers"], list)

    def test_required_sections_are_present(self):
        for key in ("version", "release_date", "tag", "frozen_artifacts", "artifacts", "data",
                    "models", "included", "not_included", "known_limitations", "prerequisites",
                    "tagging_checklist", "reproduction", "verification", "ci", "documents",
                    "hash_convention", "status"):
            self.assertIn(key, self.notes, key)


class FrozenHashes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notes = load(NOTES)

    def test_v4_hash_matches_file_and_pin(self):
        entry = self.notes["frozen_artifacts"]["evaluation_protocol_v4"]
        self.assertEqual(entry["sha256"], lf_sha(ROOT / entry["file"]))
        self.assertEqual(entry["sha256"], pin(ROOT / entry["pin_file"]))
        self.assertEqual(entry["version"], "4.0.0")
        self.assertEqual(entry["status"], "FROZEN")

    def test_r3_hash_matches_file_and_pin(self):
        entry = self.notes["frozen_artifacts"]["r3_specification"]
        self.assertEqual(entry["sha256"], lf_sha(ROOT / entry["file"]))
        self.assertEqual(entry["sha256"], pin(ROOT / entry["pin_file"]))
        self.assertEqual(entry["version"], "1.0.1")
        self.assertEqual(entry["status"], "FROZEN")
        self.assertIn("D-141", entry["amendment"])

    def test_nf_sim_hash_matches_file_and_pin(self):
        entry = self.notes["frozen_artifacts"]["nf_sim_thresholds"]
        self.assertEqual(entry["sha256"], lf_sha(ROOT / entry["file"]))
        self.assertEqual(entry["sha256"], pin(ROOT / entry["pin_file"]))
        self.assertEqual(entry["entries"], 44)
        self.assertEqual(entry["replications"], 10000)
        self.assertIn("min(0.97", entry["threshold_rule"])

    def test_composition_hash_matches_file_and_pin(self):
        entry = self.notes["frozen_artifacts"]["comparator_composition"]
        self.assertEqual(entry["sha256"], lf_sha(ROOT / entry["file"]))
        self.assertEqual(entry["sha256"], pin(ROOT / entry["pin_file"]))
        self.assertEqual(entry["document_id"], "AMFE-COMPARATOR-COMPOSITION")
        self.assertEqual(entry["evaluation_date"], "2026-11-11")

    def test_artifact_hashes_match_the_files(self):
        artifacts = self.notes["artifacts"]
        for key in ("data_plan", "data_log", "r2_gate_report",
                    "first_real_evaluation_report", "candidates"):
            entry = artifacts[key]
            self.assertEqual(entry["sha256"], lf_sha(ROOT / entry["file"]), key)
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_every_hash_is_a_full_sha256(self):
        found = []

        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str) and len(value) == 64 and re.fullmatch(r"[0-9a-f]{64}", value):
                found.append(value)

        walk(self.notes)
        self.assertGreaterEqual(len(found), 8)


class ReleaseStatusConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notes = load(NOTES)
        cls.report = load(REPORT)

    def test_notes_match_the_release_check_report(self):
        recorded = self.notes["status"]["release_check"]
        self.assertEqual(recorded["status"], self.report["status"])
        self.assertEqual(recorded["exit_code"], self.report["exit_code"])
        self.assertEqual(recorded["blockers"], self.report["blockers"])
        self.assertEqual(recorded["warnings"], self.report["warnings"])
        self.assertEqual(recorded["checks_total"], self.report["counts"]["checks"])

    def test_workflow_does_not_claim_more_than_the_report(self):
        recorded = self.notes["status"]["release_check"]["full_local_run"]
        self.assertEqual(recorded["blockers"], [])
        self.assertEqual(recorded["test_files_failed"], 0)
        self.assertEqual(self.notes["status"]["scientific"]["admitted_models"], 0)

    def test_data_numbers_match_the_artefacts(self):
        data = self.notes["data"]
        plan = load(ROOT / "protocols" / "data_plan.json")
        gate = load(ROOT / "protocols" / "r2_gate_report.json")
        log = load(ROOT / "protocols" / "data_log.json")
        planned = [series["id"] for series in plan["series"] if series.get("vintages")]
        self.assertEqual(data["vintage_series_count"], len(planned))
        self.assertEqual(data["series_count"], len(plan["series"]))
        self.assertEqual(sorted(data["vintage_series"]), sorted(planned))
        self.assertEqual(data["gate"]["matched_pairs"], gate["totals"]["matched_pairs"])
        self.assertEqual(data["gate"]["matched_pairs_pre_2000"],
                         gate["totals"]["matched_pairs_pre_2000"])
        sets = [entry for entry in log["entries"] if entry.get("vintage") == "set"]
        self.assertEqual(self.notes["artifacts"]["data_log"]["vintage_sets"], len(sets))

    def test_evaluation_status_is_candidate_without_admission(self):
        model = self.notes["models"]["first_evaluation"]
        evaluation = load(ROOT / "protocols" / "first_real_evaluation_report.json")
        admission = evaluation["admission"]
        self.assertEqual(model["candidate"], evaluation["candidate"])
        self.assertEqual(model["result"].split(":")[0], "CANDIDATE")
        self.assertEqual(self.notes["status"]["scientific"]["candidate_status"], "CANDIDATE")
        self.assertEqual(admission["status"], "CANDIDATE")
        self.assertEqual(sum(1 for value in admission["stages"].values() if value), 0)
        criteria = self.notes["status"]["scientific"]["criteria"]
        self.assertEqual(criteria["C1"], "PASS")
        self.assertEqual(criteria["C4"], "FAIL")
        self.assertEqual(criteria["C6"], "NO_RESOLVED_FORECASTS")

    def test_release_notes_do_not_assign_admission(self):
        text = NOTES.read_text(encoding="utf-8")
        self.assertNotIn("ADMITTED", text)
        self.assertNotIn('"admission_granted": true', text)
        self.assertEqual(self.notes["status"]["scientific"]["admitted_models"], 0)
        self.assertIn("допущенной модели", " ".join(self.notes["not_included"]).lower()
                      .replace("допущенная модель", "допущенной модели"))


class Tagging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notes = load(NOTES)

    def test_tag_is_not_created_and_requires_the_owner(self):
        tag = self.notes["tag"]
        self.assertEqual(tag["name"], "v1.0.0")
        self.assertFalse(tag["created"])
        self.assertIsNone(tag["created_on"])
        self.assertTrue(tag["owner_decision_required"])
        self.assertIn("владелец", tag["policy"])

    def test_tagging_checklist_is_actionable(self):
        checklist = self.notes["tagging_checklist"]
        self.assertGreaterEqual(len(checklist), 6)
        owners = {item["owner"] for item in checklist}
        self.assertIn("owner", owners)
        self.assertIn("lead", owners)
        for item in checklist:
            for key in ("step", "owner", "action", "criterion"):
                self.assertIn(key, item, item)
        text = " ".join(item["action"] for item in checklist).lower()
        self.assertIn("release_check", text)
        self.assertIn("тег", text)

    def test_prerequisites_are_listed(self):
        prerequisites = " ".join(self.notes["prerequisites"]).lower()
        for token in ("владел", "release_check", "регресс", "ci", "хеш"):
            self.assertIn(token, prerequisites, token)


class LimitationsAndScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notes = load(NOTES)

    def test_known_limitations_cover_the_open_items(self):
        limitations = " ".join(self.notes["known_limitations"]).lower()
        for token in ("c4", "c6", "candidate", "forward", "t1"):
            self.assertIn(token, limitations, token)
        self.assertGreaterEqual(len(self.notes["known_limitations"]), 8)

    def test_scope_includes_and_excludes(self):
        included = " ".join(self.notes["included"]).lower()
        excluded = " ".join(self.notes["not_included"]).lower()
        for token in ("v4", "r3", "nf-sim", "винтаж", "candidate"):
            self.assertIn(token, included, token)
        for token in ("допущен", "c6", "c4", "w3", "сырые"):
            self.assertIn(token, excluded, token)

    def test_notes_store_no_raw_third_party_values(self):
        self.assertEqual(numeric_arrays(self.notes), [])
        dump = json.dumps(self.notes, ensure_ascii=False)
        self.assertNotIn('"values":', dump)
        self.assertNotIn('"observations":', dump)

    def test_reproduction_commands_reference_existing_artefacts(self):
        commands = " ".join(self.notes["reproduction"]["commands"])
        for token in ("tools/release_check.py", "tools/weekly_report.py",
                      "tools/regression_report.py", "tests", "run_real_evaluation.py",
                      "gh run download"):
            self.assertIn(token, commands, token)
        for document in ("protocols/release_notes_1.0.json", "docs/96_Релиз_1.0_подготовка.md",
                         "docs/89_Релизный_чеклист_этап9.md"):
            self.assertTrue((ROOT / document).exists(), document)


class ReleaseWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_file_exists_in_the_documented_location(self):
        self.assertTrue(WORKFLOW.is_file())
        notes = load(NOTES)
        self.assertEqual(notes["ci"]["workflows"]["release-check"],
                         ".github/workflows/release-check.yml")

    def test_yaml_is_valid(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            # no PyYAML on the runner: structural checks below still cover the file
            self.assertIn("jobs:", self.text)
            self.assertIn("steps:", self.text)
            return
        document = yaml.safe_load(self.text)
        self.assertIsInstance(document, dict)
        self.assertIn("jobs", document)
        self.assertIn("release-check", document["jobs"])
        triggers = document.get("on") or document.get(True) or {}
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(document["permissions"]["contents"], "read")
        self.assertEqual(document["permissions"]["actions"], "read")
        steps = document["jobs"]["release-check"]["steps"]
        names = " ".join(str(step.get("name") or "") for step in steps)
        uses = " ".join(str(step.get("uses") or "") for step in steps)
        runs = " ".join(str(step.get("run", "")) for step in steps)
        combined = names + " " + uses + " " + runs
        for token in ("numpy", "data-refresh.yml", "r2-metadata.yml",
                      "tools/release_check.py", "--out build/release_check.json",
                      "GITHUB_STEP_SUMMARY", "upload-artifact"):
            self.assertIn(token, combined, token)

    def test_the_workflow_runs_the_full_check_and_does_not_disable_it(self):
        self.assertIn("python tools/release_check.py", self.text)
        self.assertNotIn("--skip-tests", self.text)
        self.assertIn("exit 1", self.text)  # FAIL must fail the job
        self.assertIn("CONDITIONAL", self.text)  # but CONDITIONAL is a warning, not a failure

    def test_the_workflow_never_tags_or_commits(self):
        for token in ("git tag", "git commit", "git push", "gh release create"):
            self.assertNotIn(token, self.text, token)
        self.assertIn("владелец", load(NOTES)["tag"]["policy"].lower())


class Documentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")

    def test_release_preparation_document_exists_with_required_sections(self):
        self.assertTrue(DOC.is_file())
        for heading in ("Что входит", "НЕ входит", "Release notes", "воспроизвед",
                        "Известные ограничения", "Чек-лист тегирования"):
            self.assertIn(heading, self.text, heading)

    def test_document_references_the_release_artefacts(self):
        for token in ("protocols/release_notes_1.0.json", ".github/workflows/release-check.yml",
                      "tools/release_check.py", "docs/89_Релизный_чеклист_этап9.md",
                      "v1.0.0", "CONDITIONAL"):
            self.assertIn(token, self.text, token)

    def test_document_requires_the_owner_decision_for_tagging(self):
        lower = self.text.lower()
        self.assertIn("владел", lower)
        self.assertIn("решение", lower)
        self.assertIn("тег", lower)

    def test_document_does_not_disable_the_release_check(self):
        lower = self.text.lower()
        self.assertIn("отключать проверки", lower)
        self.assertIn("tools/release_check.py --date", self.text)
        self.assertIn("blockers=[]", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
