"""Tests for the module registry (G2, docs/157): passports, statuses, append-only, export, tool."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.learning import module_registry as mr  # noqa: E402
import register_modules  # noqa: E402


def passport(**overrides) -> dict:
    base = dict(
        module_id="demo.module", kind="market", version="0.1.0",
        display_name="Demo module", summary="A demo module for tests.",
        owner="track-x", code_sha256=mr.sha256_text("demo-code"), data_sha256=None,
        data_status="SYNTHETIC", created_at="2026-11-16",
        docs=["docs/157_Реестр_модулей.md"], tests=["tests/test_module_registry.py"],
        entrypoints=["demo.module"], limitations=["research prototype only"],
        components=["part_a"], code_files={"src/demo.py": mr.sha256_text("file")},
        status="RESEARCH_ONLY", note="test",
    )
    base.update(overrides)
    return mr.build_passport(**base)


REVIEW = {"reviewer": "independent-reviewer", "independent": True, "pass": True,
          "reviewed_at": "2026-11-16", "report_sha256": mr.sha256_text("review-report")}
CRITERIA = {"registered_at": "2026-11-01",
            "criteria": {"min_walkforward_folds": 12, "min_q": 0.6}}


class Passports(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"

    def test_register_and_append_only(self):
        first = passport()
        mr.register(self.path, first)
        prefix = self.path.read_bytes()
        mr.register(self.path, passport(module_id="demo.other", code_sha256=mr.sha256_text("x")))
        content = self.path.read_bytes()
        self.assertTrue(content.startswith(prefix))
        lines = [line for line in content.decode("utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            entry = json.loads(line)
            self.assertEqual(entry["schema"], mr.SCHEMA)
            self.assertEqual(line, json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")))

    def test_duplicate_registration_fails(self):
        mr.register(self.path, passport())
        with self.assertRaises(mr.ModuleRegistryError):
            mr.register(self.path, passport())
        self.assertEqual(len(mr.load_entries(self.path)), 1)

    def test_status_on_registration_is_bounded(self):
        with self.assertRaises(mr.ModuleRegistryError):
            passport(status="LIMITED")
        with self.assertRaises(mr.ModuleRegistryError):
            passport(status="ACTIVE")
        with self.assertRaises(mr.ModuleRegistryError):
            passport(criteria=CRITERIA)
        self.assertEqual(passport(status="CANDIDATE")["status"], "CANDIDATE")

    def test_invalid_passports_fail_closed(self):
        for overrides in (
            {"kind": "unknown"}, {"version": "1"}, {"version": "0.1.0.1"},
            {"code_sha256": "short"}, {"docs": []}, {"tests": []}, {"limitations": []},
            {"owner": "  "}, {"created_at": "16.11.2026"}, {"status": "BOGUS"},
            {"data_status": "NONE", "data_sha256": mr.sha256_text("data")},
            {"data_sha256": "not-a-hash"},
        ):
            with self.assertRaises(mr.ModuleRegistryError, msg=str(overrides)):
                passport(**overrides)


class Statuses(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"
        mr.register(self.path, passport(status="CANDIDATE"))

    def test_above_candidate_requires_criteria_and_review(self):
        with self.assertRaises(mr.ModuleRegistryError) as context:
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16")
        self.assertIn("criteria", str(context.exception))
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA)
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", review=REVIEW)
        self.assertEqual(mr.state(self.path)["demo.module"]["status"], "CANDIDATE")

    def test_promotion_with_criteria_and_review(self):
        mr.set_status(self.path, module_id="demo.module", version="0.1.0", to_status="LIMITED",
                      at="2026-11-16", criteria=CRITERIA, review=REVIEW)
        module = mr.state(self.path)["demo.module"]
        self.assertEqual(module["status"], "LIMITED")
        record = module["versions"]["0.1.0"]
        self.assertEqual(record["criteria"]["sha256"],
                         mr.sha256_text(mr._canonical(dict(CRITERIA["criteria"]))))
        self.assertEqual(record["independent_review"]["reviewer"], "independent-reviewer")
        self.assertTrue(mr.validate_registry(self.path)["valid"])

    def test_promotion_gate_details(self):
        late = {"registered_at": "2026-12-01", "criteria": {"min_q": 0.6}}
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=late, review=REVIEW)
        self_review = dict(REVIEW, reviewer="track-x")
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=self_review)
        non_independent = dict(REVIEW, independent=False)
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=non_independent)
        failed = dict(REVIEW, **{"pass": False})
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=failed)

    def test_demotion_and_same_status(self):
        mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                      to_status="RESEARCH_ONLY", at="2026-11-16")
        self.assertEqual(mr.state(self.path)["demo.module"]["status"], "RESEARCH_ONLY")
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="RESEARCH_ONLY", at="2026-11-16")


class ReadsAndExport(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"
        mr.register(self.path, passport(module_id="b.module", kind="agri",
                                        code_sha256=mr.sha256_text("b")))
        mr.register(self.path, passport(module_id="a.module", kind="market", version="0.2.0",
                                        code_sha256=mr.sha256_text("a2")))
        mr.register(self.path, passport(module_id="a.module", kind="market", version="0.1.0",
                                        code_sha256=mr.sha256_text("a1")))

    def test_latest_version_and_find(self):
        modules = mr.state(self.path)
        self.assertEqual(modules["a.module"]["latest_version"], "0.2.0")
        self.assertEqual(modules["a.module"]["versions"]["0.1.0"]["status"], "RESEARCH_ONLY")
        latest = mr.find(self.path)
        self.assertEqual([record["module_id"] for record in latest],
                         ["a.module", "b.module"])
        self.assertEqual([record["version"] for record in latest], ["0.2.0", "0.1.0"])
        agri = mr.find(self.path, kind="agri")
        self.assertEqual([record["module_id"] for record in agri], ["b.module"])
        all_versions = mr.find(self.path, latest_only=False)
        self.assertEqual(len(all_versions), 3)

    def test_export_for_api_is_deterministic_and_sorted(self):
        first = mr.export_for_api(self.path)
        second = mr.export_for_api(self.path)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["schema"], mr.EXPORT_SCHEMA)
        self.assertEqual([module["module_id"] for module in first["modules"]],
                         ["a.module", "b.module"])
        self.assertEqual(first["counts"]["modules"], 2)
        self.assertEqual(first["counts"]["by_kind"], {"agri": 1, "market": 1})
        self.assertTrue(all(module["status"] == "RESEARCH_ONLY"
                            for module in first["modules"]))
        for module in first["modules"]:
            self.assertNotIn("status_history", module)

    def test_registry_determinism(self):
        first = Path(self.directory.name) / "first.jsonl"
        second = Path(self.directory.name) / "second.jsonl"
        for target in (first, second):
            for module_id, version, kind, seed in (
                ("x.module", "0.1.0", "core", "x"),
                ("y.module", "1.0.0", "other", "y"),
            ):
                mr.register(target, passport(module_id=module_id, version=version, kind=kind,
                                             code_sha256=mr.sha256_text(seed)))
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_corrupted_log_is_detected(self):
        broken = Path(self.directory.name) / "broken.jsonl"
        broken.write_text("{not json\n", encoding="utf-8")
        report = mr.validate_registry(broken)
        self.assertFalse(report["valid"])
        self.assertIn("not JSON", report["errors"][0])
        with self.assertRaises(mr.ModuleRegistryError):
            mr.load_entries(broken)
        # a missing log is treated as empty (nothing to validate), not as corruption
        empty = mr.validate_registry(self.path / "missing")
        self.assertTrue(empty["valid"])
        self.assertEqual(empty["entries"], 0)


class CodeHash(unittest.TestCase):
    def test_code_hash_is_deterministic_and_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.py"
            second = Path(tmp) / "b.py"
            first.write_text("print('a')\n", encoding="utf-8", newline="\n")
            second.write_text("print('b')\n", encoding="utf-8", newline="\n")
            one = mr.code_hash({"a.py": first, "b.py": second})
            two = mr.code_hash({"b.py": second, "a.py": first})
            self.assertEqual(one, two)
            self.assertEqual(len(one["sha256"]), 64)
            first.write_text("print('changed')\n", encoding="utf-8", newline="\n")
            self.assertNotEqual(one, mr.code_hash({"a.py": first, "b.py": second}))
            with self.assertRaises(mr.ModuleRegistryError):
                mr.code_hash({"missing.py": Path(tmp) / "missing.py"})


class Tool(unittest.TestCase):
    EXPECTED = {"oil.brent": "market", "AGRI_SOY": "agri", "markets.finance": "finance",
                "agentic.sim": "agentic", "world.p2": "world"}

    def test_registers_the_g2_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "modules.jsonl"
            report = register_modules.build_report(registry=registry)
            self.assertEqual(report["entries"], 5)
            self.assertTrue(report["all_research_only"])
            self.assertTrue(report["validation"]["valid"], report["validation"]["errors"])
            self.assertEqual(report["export_counts"]["by_status"], {"RESEARCH_ONLY": 5})
            found = {item["module_id"]: item for item in report["registered"]}
            self.assertEqual(set(found), set(self.EXPECTED))
            for module_id, kind in self.EXPECTED.items():
                self.assertEqual(found[module_id]["kind"], kind)
                self.assertEqual(found[module_id]["status"], "RESEARCH_ONLY")
                self.assertEqual(len(found[module_id]["code_sha256"]), 64)
            # world P2 0.2 is anchored to the local release manifest
            self.assertEqual(found["world.p2"]["version"], "0.2.0")
            self.assertEqual(found["world.p2"]["data_status"], "LOCAL_ONLY")
            self.assertEqual(len(found["world.p2"]["data_sha256"] or ""), 64)
            # the export is API-ready and the log validates
            export = mr.export_for_api(registry)
            self.assertEqual(export["counts"]["modules"], 5)
            self.assertEqual([module["module_id"] for module in export["modules"]],
                             sorted(self.EXPECTED))

    def test_repeated_run_is_deterministic_and_fails_on_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            first_report = register_modules.build_report(registry=first)
            second_report = register_modules.build_report(registry=second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            first_report.pop("registry")
            second_report.pop("registry")
            self.assertEqual(json.dumps(first_report, sort_keys=True),
                             json.dumps(second_report, sort_keys=True))
            with self.assertRaises(mr.ModuleRegistryError):
                register_modules.build_report(registry=first)
            self.assertEqual(len(mr.load_entries(first)), 5)

    def test_cli_fails_closed_on_bad_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = register_modules.main(["--registry", str(Path(tmp) / "modules.jsonl"),
                                          "--date", "bad-date", "--json"])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
