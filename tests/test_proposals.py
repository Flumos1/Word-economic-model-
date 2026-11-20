"""Tests for the proposal journal and engine (G6-prep, docs/195)."""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.learning import proposals as prop  # noqa: E402
import proposal_engine as engine  # noqa: E402

AT = "2026-11-20"


def make_proposal(**overrides) -> dict:
    base = dict(
        kind="NEW_METHOD", title="Test proposal", rationale="Because the evidence says so.",
        evidence={"rule": "test", "value": 1}, dedupe_key="test:proposal", created_at=AT,
        benefit=3, risk=1, effort="S",
    )
    base.update(overrides)
    return prop.build_proposal(**base)


def write_learning_registry(path: Path, rows: list[dict]) -> Path:
    events = []
    for index, row in enumerate(rows):
        events.append({"schema": "AMFE-MODEL-REGISTRY/1.0.0", "action": "REGISTER",
                       "model_id": row["model_id"], "version": row["version"],
                       "quality": {"q": row["q"], "hard_constraints": {"pass": True}},
                       "evaluated_at": row.get("evaluated_at", "2026-11-15"),
                       "author": "automation", "code_sha256": "0" * 64,
                       "data_sha256": "1" * 64, "metrics": {"mae": 0.1},
                       "event_id": f"{index:064d}"})
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
                    encoding="utf-8", newline="\n")
    return path


class Journal(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "proposals.jsonl"

    def test_build_is_proposed_and_deterministic(self):
        first = make_proposal()
        second = make_proposal()
        self.assertEqual(first["status"], "PROPOSED")
        self.assertIsNone(first["human_approved_by"])
        self.assertIsNone(first["human_approved_at"])
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(first["expected_value"],
                         {"benefit": 3, "risk": 1, "value_score": 1.5, "effort": "S",
                          "effort_weeks": 1, "risk_level": "low"})
        self.assertNotEqual(make_proposal(evidence={"rule": "other"})["proposal_id"],
                            first["proposal_id"])

    def test_register_is_append_only(self):
        prop.register(self.path, make_proposal())
        prefix = self.path.read_bytes()
        prop.register(self.path, make_proposal(dedupe_key="test:second"))
        content = self.path.read_bytes()
        self.assertTrue(content.startswith(prefix))
        lines = [line for line in content.decode("utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            event = json.loads(line)
            self.assertEqual(line, json.dumps(event, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")))

    def test_auto_approved_is_refused(self):
        approved = make_proposal()
        approved["status"] = "APPROVED"
        with self.assertRaises(prop.ProposalError):
            prop.register(self.path, approved)
        approved_event = prop.build_approve_event(make_proposal(), by="A. Analyst", at=AT)
        approved_event["action"] = "REGISTER"
        with self.assertRaises(prop.ProposalError):
            prop.register(self.path, approved_event)
        duplicate = make_proposal()
        prop.register(self.path, duplicate)
        with self.assertRaises(prop.ProposalError):
            prop.register(self.path, duplicate)

    def test_human_gate(self):
        prop.register(self.path, make_proposal())
        proposal_id = make_proposal()["proposal_id"]
        for fake in ("automation", "system", "Proposal Engine", "bot", "ai", "auto"):
            with self.assertRaises(prop.ProposalError, msg=fake):
                prop.approve(self.path, proposal_id, by=fake, at=AT)
        with self.assertRaises(prop.ProposalError):
            prop.approve(self.path, "0" * 16, by="A. Analyst", at=AT)
        event = prop.approve(self.path, proposal_id, by="A. Analyst", at=AT)
        self.assertEqual(event["human_approved_by"], "A. Analyst")
        self.assertEqual(prop.get_proposal(self.path, proposal_id)["status"], "APPROVED")
        with self.assertRaises(prop.ProposalError):
            prop.approve(self.path, proposal_id, by="A. Analyst", at=AT)

    def test_reject_gate(self):
        prop.register(self.path, make_proposal())
        proposal_id = make_proposal()["proposal_id"]
        with self.assertRaises(prop.ProposalError):
            prop.reject(self.path, proposal_id, by="A. Analyst", at=AT, reason="")
        prop.reject(self.path, proposal_id, by="A. Analyst", at=AT, reason="duplicate of X")
        proposal = prop.get_proposal(self.path, proposal_id)
        self.assertEqual(proposal["status"], "REJECTED")
        self.assertEqual(proposal["reject_reason"], "duplicate of X")
        with self.assertRaises(prop.ProposalError):
            prop.approve(self.path, proposal_id, by="A. Analyst", at=AT)

    def test_implement_requires_approval(self):
        prop.register(self.path, make_proposal())
        proposal_id = make_proposal()["proposal_id"]
        with self.assertRaises(prop.ProposalError):
            prop.implement(self.path, proposal_id, by="A. Analyst", at=AT)
        prop.approve(self.path, proposal_id, by="A. Analyst", at=AT)
        prop.implement(self.path, proposal_id, by="A. Analyst", at=AT)
        self.assertEqual(prop.get_proposal(self.path, proposal_id)["status"], "IMPLEMENTED")

    def test_export_and_filters(self):
        prop.register(self.path, make_proposal())
        prop.register(self.path, make_proposal(kind="DATA_GAP", dedupe_key="gap:x"))
        export = prop.export_for_api(self.path)
        self.assertEqual(export["schema"], prop.EXPORT_SCHEMA)
        self.assertEqual(export["counts"]["proposals"], 2)
        self.assertEqual(export["counts"]["by_status"]["PROPOSED"], 2)
        filtered = prop.list_proposals(self.path, kind="DATA_GAP")
        self.assertEqual(len(filtered), 1)
        with self.assertRaises(prop.ProposalError):
            prop.list_proposals(self.path, status="BOGUS")
        self.assertTrue(prop.validate_journal(self.path)["valid"])

    def test_corrupted_journal(self):
        broken = Path(self.directory.name) / "broken.jsonl"
        broken.write_text("{oops\n", encoding="utf-8")
        with self.assertRaises(prop.ProposalError):
            prop.load_events(broken)
        self.assertFalse(prop.validate_journal(broken)["valid"])


class EngineScan(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry = Path(self.directory.name) / "proposals.jsonl"

    def test_scan_on_real_artifacts(self):
        # not brittle: the expected DATA_GAP count follows the live source registry, which other
        # tracks update (wave 26 verification promotes sources to VERIFIED while we test)
        from amfe.data import source_registry as sr

        sources_document = sr.load(engine.DEFAULT_SOURCES)
        expected_gaps = sum(1 for source in sources_document["sources"]
                            if source["status"] != "VERIFIED")
        report = engine.scan(registry=self.registry)
        kinds = report["signals"]["by_kind"]
        self.assertEqual(kinds["DATA_GAP"], expected_gaps)
        self.assertGreaterEqual(kinds["NEW_METHOD"], 2)  # C4 ambiguity + C6 coverage
        self.assertIn(kinds["NEW_LINK"], (0, 1))          # one missed C6 week at most
        self.assertEqual(report["signals"]["proposals"], sum(kinds.values()))
        self.assertEqual(report["counts"]["created"], report["signals"]["proposals"])
        proposals = prop.list_proposals(self.registry)
        self.assertEqual(len(proposals), report["signals"]["proposals"])
        self.assertTrue(all(item["status"] == "PROPOSED" for item in proposals))
        self.assertTrue(any("learning registry" in note for note in report["notes"]))

    def test_scan_is_deterministic_and_deduplicated(self):
        first = engine.scan(registry=self.registry)
        second = engine.scan(registry=self.registry)
        self.assertEqual(first["counts"]["created"], len(first["created"]))
        self.assertEqual(second["counts"]["created"], 0)
        self.assertEqual(second["counts"]["skipped"], first["counts"]["created"])
        self.assertTrue(all(item["reason"] == "already proposed" for item in second["skipped"]))
        other = Path(self.directory.name) / "second.jsonl"
        third = engine.scan(registry=other)
        self.assertEqual(json.dumps({k: v for k, v in first.items() if k != "registry"},
                                    sort_keys=True),
                         json.dumps({k: v for k, v in third.items() if k != "registry"},
                                    sort_keys=True))

    def test_dry_run_does_not_write(self):
        report = engine.scan(registry=self.registry, dry_run=True)
        self.assertGreater(report["counts"]["created"], 0)
        self.assertFalse(self.registry.exists())

    def test_q_degradation_rule(self):
        learning = write_learning_registry(
            Path(self.directory.name) / "learning.jsonl",
            [{"model_id": "MODEL_A", "version": "1.0.0", "q": 0.70},
             {"model_id": "MODEL_A", "version": "1.1.0", "q": 0.60},
             {"model_id": "ENSEMBLE_REGIME_V5", "version": "1.0.0", "q": 0.70},
             {"model_id": "ENSEMBLE_REGIME_V5", "version": "1.1.0", "q": 0.50}])
        report = engine.scan(registry=self.registry, learning=learning)
        kinds = [item["kind"] for item in report["created"]]
        self.assertIn("NEW_METHOD", kinds)
        self.assertIn("NEW_REGIME", kinds)
        by_dedupe = {item["dedupe_key"]: item["kind"] for item in report["created"]}
        self.assertEqual(by_dedupe["q_degradation:MODEL_A:1.0.0:1.1.0"], "NEW_METHOD")
        self.assertEqual(by_dedupe["q_degradation:ENSEMBLE_REGIME_V5:1.0.0:1.1.0"],
                         "NEW_REGIME")

    def test_individual_rules(self):
        c4 = engine.rule_c4(engine.DEFAULT_C4, AT)[0]
        self.assertEqual(len(c4), 1)
        self.assertEqual(c4[0]["kind"], "NEW_METHOD")
        self.assertEqual(c4[0]["dedupe_key"], "c4_mask_ambiguity")
        shadow = json.loads((ROOT / "build" / "shadow" / "shadow_status.json").read_text(
            encoding="utf-8"))
        c6 = engine.rule_c6(shadow, engine.DEFAULT_C6_AUDIT,
                            engine._load_jsonl(engine.DEFAULT_C6_AUDIT), AT)[0]
        self.assertEqual(len(c6), 1)
        self.assertEqual(c6[0]["kind"], "NEW_LINK")
        coverage = engine.rule_coverage(shadow, AT)[0]
        self.assertEqual(coverage[0]["kind"], "NEW_METHOD")
        self.assertEqual(coverage[0]["dedupe_key"], "coverage_tracking:c6")
        no_gaps = engine.rule_coverage({"c6": {"coverage": 0.8, "coverage_met": True}}, AT)
        self.assertEqual(no_gaps[0], [])

    def test_fail_closed_missing_artifacts(self):
        missing = Path(self.directory.name) / "missing.json"
        with self.assertRaises(engine.EngineError):
            engine.scan(registry=self.registry, shadow=missing)
        with self.assertRaises(engine.EngineError):
            engine.scan(registry=self.registry, c4=missing)
        with self.assertRaises(engine.EngineError):
            engine.scan(registry=self.registry, sources=missing)
        broken = Path(self.directory.name) / "learning.jsonl"
        broken.write_text("{not jsonl\n", encoding="utf-8")
        with self.assertRaises(engine.EngineError):
            engine.scan(registry=self.registry, learning=broken)


class Cli(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry = Path(self.directory.name) / "proposals.jsonl"

    def _run(self, *args) -> tuple[int, str]:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = engine.main([*args])
        return code, buffer.getvalue()

    def test_scan_list_approve_flow(self):
        code, output = self._run("--scan", "--registry", str(self.registry), "--json")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertGreater(report["counts"]["created"], 0)
        code, output = self._run("--list", "--status", "PROPOSED", "--registry",
                                 str(self.registry), "--json")
        self.assertEqual(code, 0)
        items = json.loads(output)["proposals"]
        self.assertTrue(items)
        proposal_id = items[0]["proposal_id"]
        code, _ = self._run("--approve", proposal_id, "--by", "automation",
                            "--registry", str(self.registry))
        self.assertEqual(code, 1)
        code, _ = self._run("--approve", proposal_id, "--by", "A. Analyst",
                            "--registry", str(self.registry))
        self.assertEqual(code, 0)
        code, _ = self._run("--implement", proposal_id, "--by", "A. Analyst",
                            "--registry", str(self.registry))
        self.assertEqual(code, 0)

    def test_reject_and_missing_by(self):
        self._run("--scan", "--registry", str(self.registry), "--json")
        items = json.loads(self._run("--list", "--registry", str(self.registry),
                                     "--json")[1])["proposals"]
        proposal_id = items[0]["proposal_id"]
        code, _ = self._run("--reject", proposal_id, "--by", "A. Analyst",
                            "--registry", str(self.registry))
        self.assertEqual(code, 1)
        code, _ = self._run("--reject", proposal_id, "--by", "A. Analyst", "--reason", "duplicate",
                            "--registry", str(self.registry))
        self.assertEqual(code, 0)
        code, _ = self._run("--approve", proposal_id, "--by", "A. Analyst",
                            "--registry", str(self.registry))
        self.assertEqual(code, 1)

    def test_bad_registry_exits_one(self):
        broken = Path(self.directory.name) / "broken.jsonl"
        broken.write_text("{oops\n", encoding="utf-8")
        code, output = self._run("--list", "--registry", str(broken), "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
