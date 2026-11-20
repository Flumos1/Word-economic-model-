"""Tests for the G6-prep prereg 0.3 decision packet (tools/prereg_decision.py).

Covered: pin/content-hash consistency, evaluation-report summary, the three owner options and their
status tables, blockers/recommendation, the no-automatic-activation rule, read-only behaviour,
determinism, fail-closed cases (pin mismatch, content mismatch, missing report, non-human signature,
registry hash mismatch, output inside protocols/) and the CLI/Markdown output.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import prereg_decision  # noqa: E402

FIXED = "2026-11-20T00:00:00Z"
ANOTHER = "2026-11-20T06:00:00Z"

SOURCE_FILES = (
    "protocols/pit_preregistration_0_3.json",
    "protocols/pit_preregistration_0_3.sha256.txt",
    "protocols/pit_prereg_evaluation_report.json",
    "protocols/signoff_registry.json",
    "build/product/forecast_latest.json",
    "build/shadow/shadow_status.json",
    "docs/160_Внешняя_проверка_G2.md",
    "docs/185_Подпись_docs_160.md",
    "docs/170_Оценка_по_предрегистрации_0_3.md",
    "docs/172_Независимая_V2_оценки_0_3.md",
    "docs/164_План_и_статус_проекта.md",
)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = prereg_decision.main(argv)
    return code, out.getvalue(), err.getvalue()


def minimal_root(base: Path) -> Path:
    root = Path(base) / "repo"
    for relative in SOURCE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return root


class PacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.stdout, cls.stderr = run(["--json", "--generated-at", FIXED])
        cls.packet = json.loads(cls.stdout)

    # ------------------------------------------------------------------------------ happy path
    def test_packet_builds_and_schema(self):
        self.assertEqual(self.code, 0, self.stderr)
        self.assertEqual(self.packet["schema"], prereg_decision.SCHEMA)
        self.assertEqual(self.packet["decision"], "OWNER_DECISION_REQUIRED")
        self.assertFalse(self.packet["auto_activation"])
        self.assertFalse(self.packet["auto_status_change"])
        self.assertEqual(self.packet["generated_at"], FIXED)

    def test_preregistration_pins_and_content_hash(self):
        subject = self.packet["subject"]
        self.assertEqual(subject["status"], "PROPOSED")
        self.assertEqual(subject["version"], "0.3.0")
        self.assertEqual(subject["preregistration_id"], "AMFE-PIT-PREREG-0.3")
        self.assertEqual(subject["file_sha256"],
                         prereg_decision.lf_sha256(ROOT / prereg_decision.PREREG))
        self.assertEqual(subject["file_sha256"],
                         prereg_decision.read_pin(ROOT / prereg_decision.PREREG_PIN, "pin"))
        self.assertEqual(subject["content_sha256"],
                         prereg_decision.content_sha256(
                             json.loads((ROOT / prereg_decision.PREREG).read_text(encoding="utf-8"))))
        self.assertTrue(all(self.packet["checks"].values()), self.packet["checks"])

    def test_evaluation_summary_is_provisional_and_honest(self):
        evaluation = self.packet["evaluation"]
        self.assertEqual(evaluation["status"], "PROVISIONAL")
        self.assertIs(evaluation["admission"], False)
        self.assertEqual(evaluation["label"], "CANDIDATE")
        self.assertEqual(evaluation["c4_w1_ext"]["status"], "FAIL")
        self.assertAlmostEqual(evaluation["c4_w1_ext"]["relative_loss"], 1.0774564457828197,
                               places=12)
        self.assertEqual(evaluation["c4_w1_ext"]["recession_origins"], 16)
        self.assertEqual(evaluation["c4_w2"]["status"], "PASS")
        self.assertEqual(evaluation["slices"]["real_time_only"], "INCONCLUSIVE")
        self.assertEqual(evaluation["criteria"].get("C1"), True)

    def test_three_variants_with_gate_consequences(self):
        variants = {item["id"]: item for item in self.packet["variants"]}
        self.assertEqual(set(variants), {"A", "B", "C"})
        self.assertEqual(variants["A"]["prereg_status_after"], "ACTIVE")
        self.assertEqual(variants["B"]["prereg_status_after"], "PROPOSED")
        for variant in variants.values():
            for gate in ("G4", "G5", "G6"):
                self.assertTrue(variant["consequences"][gate], (variant["id"], gate))
            self.assertTrue(variant["can_claim"])
            self.assertTrue(variant["cannot_claim"])

    def test_status_table_changes_only_for_variant_a(self):
        table = self.packet["status_table"]
        self.assertEqual(table["T1_CPI_YOY"]["current"], "CANDIDATE")
        self.assertEqual({table["T1_CPI_YOY"][key] for key in ("A", "B", "C")}, {"CANDIDATE"})
        self.assertEqual(table["C6"]["current"], "0/12")
        self.assertEqual({table["C6"][key] for key in ("A", "B", "C")}, {"0/12"})
        self.assertEqual(table["AMFE-PIT-PREREG-0.3"]["A"], "ACTIVE")
        self.assertEqual(table["AMFE-PIT-PREREG-0.3"]["B"], "PROPOSED")
        self.assertEqual(table["AMFE-PIT-PREREG-0.3"]["C"], "PROPOSED")

    def test_blockers_capture_integrity_risks(self):
        ids = {blocker["id"] for blocker in self.packet["blockers"]}
        self.assertIn("post_hoc_activation_risk", ids)
        self.assertIn("no_independent_v2_of_reissue", ids)
        self.assertIn("prereg_limitations_stale", ids)
        self.assertIn("signature_scope_note", ids)
        high = [blocker for blocker in self.packet["blockers"]
                if blocker["severity"] == "HIGH"]
        self.assertGreaterEqual(len(high), 3)

    def test_recommendation_rejects_auto_activation(self):
        recommendation = self.packet["recommendation"]
        self.assertEqual(recommendation["primary"], "B")
        self.assertEqual(recommendation["secondary"], "C")
        self.assertEqual(recommendation["rejected"], "A")
        self.assertFalse(recommendation["auto_activation"])
        self.assertTrue(recommendation["owner_decisions_required"])
        self.assertIn("0.4", " ".join(self.packet["requirements_for_0_4"]))

    def test_human_signature_is_verified_and_scoped(self):
        signature = self.packet["signature"]
        self.assertEqual(signature["id"], "G4-SIGN-160")
        self.assertEqual(signature["status"], "SIGNED_BY_HUMAN")
        self.assertEqual(signature["decision"], "APPROVE")
        self.assertEqual(signature["date"], "2026-11-20")
        self.assertIn("docs/160", signature["scope"])
        self.assertEqual(len(signature["object_sha256"]), 64)
        self.assertEqual(len(signature["packet_sha256"]), 64)

    # ------------------------------------------------------------------------ determinism/read-only
    def test_determinism_except_generated_at(self):
        code, second, error = run(["--json", "--generated-at", ANOTHER])
        self.assertEqual(code, 0, error)
        left = json.loads(self.stdout)
        right = json.loads(second)
        left["generated_at"] = right["generated_at"] = "<generated_at>"
        self.assertEqual(left, right)
        self.assertEqual(self.stdout, run(["--json", "--generated-at", FIXED])[1])

    def test_tool_does_not_change_statuses(self):
        before = {relative: prereg_decision.lf_sha256(ROOT / relative) for relative in
                  (prereg_decision.PREREG, prereg_decision.EVAL_REPORT,
                   prereg_decision.REGISTRY, prereg_decision.PREREG_PIN)}
        run(["--json", "--generated-at", FIXED])
        after = {relative: prereg_decision.lf_sha256(ROOT / relative) for relative in before}
        self.assertEqual(before, after)

    # ---------------------------------------------------------------------------- fail-closed
    def test_fail_closed_on_pin_mismatch(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            (root / prereg_decision.PREREG_PIN).write_text("0" * 64 + "  pin\n", encoding="utf-8")
            code, _, error = run(["--root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("pin mismatch", error)

    def test_fail_closed_on_content_sha_mismatch(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            path = root / prereg_decision.PREREG
            document = json.loads(path.read_text(encoding="utf-8"))
            document["title"] = "edited but the content hash was not updated"
            path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            # re-pin the edited file so only the stored content_sha256 is stale
            (root / prereg_decision.PREREG_PIN).write_text(
                prereg_decision.lf_sha256(path) + "  edited.json\n", encoding="utf-8")
            code, _, error = run(["--root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("content_sha256 mismatch", error)

    def test_fail_closed_on_missing_report(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            (root / prereg_decision.EVAL_REPORT).unlink()
            code, _, error = run(["--root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("нет файла", error)

    def test_fail_closed_on_agent_signed_registry(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            path = root / prereg_decision.REGISTRY
            registry = json.loads(path.read_text(encoding="utf-8"))
            registry["signoffs"][0]["signature"]["signer_type"] = "agent"
            path.write_text(json.dumps(registry, ensure_ascii=False) + "\n", encoding="utf-8")
            code, _, error = run(["--root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("human", error)

    def test_fail_closed_on_registry_object_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            object_path = root / "docs" / "160_Внешняя_проверка_G2.md"
            object_path.write_text(object_path.read_text(encoding="utf-8") + "\nedited\n",
                                   encoding="utf-8")
            code, _, error = run(["--root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("SHA-256 mismatch", error)

    def test_out_inside_protocols_is_rejected(self):
        target = ROOT / "protocols" / "prereg_decision_should_not_exist.md"
        code, _, error = run(["--out", str(target), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("protocols", error)
        self.assertFalse(target.exists())

    # ----------------------------------------------------------------------------------- CLI/MD
    def test_markdown_contains_variants_and_disclaimer(self):
        code, stdout, error = run(["--generated-at", FIXED])
        self.assertEqual(code, 0, error)
        for token in ("Варианты решения", "A. Активировать", "B. Оставить", "C. Выпустить",
                      "Рекомендация", "G4", "G6", "не меняет", "OWNER_DECISION_REQUIRED"):
            self.assertIn(token, stdout, token)

    def test_out_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "decision.md"
            code, _, error = run(["--out", str(out), "--json", "--generated-at", FIXED])
            self.assertEqual(code, 0, error)
            self.assertTrue(out.is_file())
            self.assertIn("Рекомендация", out.read_text(encoding="utf-8"))

    def test_cli_runs_as_a_script(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "prereg_decision.py"), "--json",
             "--generated-at", FIXED],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["recommendation"]["primary"], "B")


if __name__ == "__main__":
    unittest.main(verbosity=2)
