#!/usr/bin/env python3
"""Tests for the Human Counsel contour (jurisdiction matrix, panel, matter brief, signoff)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "human_counsel.py"
LEGAL = ROOT / "agents" / "legal" / "human_counsel"
MATRIX = LEGAL / "jurisdiction_matrix.json"
PANEL = LEGAL / "panel_registry.json"

spec = importlib.util.spec_from_file_location("human_counsel", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT), env=env)


def load_matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


class FilesAndMatrix(unittest.TestCase):
    def test_files_exist(self):
        for name in ("README.md", "charter.md", "skills.md", "engagement_protocol.md",
                     "conflict_check.md", "signoff.md", "jurisdiction_matrix.json",
                     "panel_registry.json"):
            self.assertTrue((LEGAL / name).is_file(), name)

    def test_matrix_schema_and_scale(self):
        data = load_matrix()
        self.assertEqual(data["schema"], "amfe.legal.human_counsel.matrix.v1")
        self.assertGreaterEqual(len(data["jurisdictions"]), 190)
        self.assertEqual(data["advice_ceiling"], "SIGNED_LEGAL_OPINION_BY_LICENSED_HUMAN")

    def test_codes_unique_and_iso2(self):
        codes = [item["code"] for item in load_matrix()["jurisdictions"]]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_statuses_and_no_invented_counsel(self):
        data = load_matrix()
        for item in data["jurisdictions"]:
            self.assertIn(item["status"], mod.STATUSES)
            self.assertIsNone(item["counsel_id"])
        self.assertGreaterEqual(sum(1 for item in data["jurisdictions"]
                                    if item["status"].startswith("PRIORITY")), 10)

    def test_no_contacts_or_pii(self):
        text = MATRIX.read_text(encoding="utf-8")
        self.assertIsNone(mod.EMAIL_RE.search(text))
        self.assertIsNone(mod.PHONE_RE.search(text))

    def test_panel_is_empty_and_safe(self):
        panel = json.loads(PANEL.read_text(encoding="utf-8"))
        self.assertEqual(panel["schema"], "amfe.legal.human_counsel.panel.v1")
        self.assertEqual(panel["counsel"], [])
        self.assertTrue(panel["requirements"]["human_verified_by"].startswith("human:"))
        self.assertEqual(panel["requirements"]["pii_location"], "external_secure_store")

    def test_docs_cover_handoff_and_limits(self):
        combined = "\n".join((LEGAL / name).read_text(encoding="utf-8")
                             for name in ("README.md", "charter.md", "skills.md",
                                          "engagement_protocol.md", "conflict_check.md",
                                          "signoff.md"))
        for token in ("лицензированн", "human", "конфликт", "санкц", "UNASSIGNED",
                      "не является юридической консультацией"):
            self.assertIn(token.lower(), combined.lower())


class Validation(unittest.TestCase):
    def setUp(self):
        self.matrix = load_matrix()
        self.panel = json.loads(PANEL.read_text(encoding="utf-8"))

    def test_valid_matrix_and_panel(self):
        self.assertEqual(mod.validate_matrix(self.matrix), [])
        self.assertEqual(mod.validate_panel(self.panel, self.matrix), [])

    def test_matrix_rejects_low_count_and_duplicate(self):
        bad = json.loads(json.dumps(self.matrix))
        bad["jurisdictions"] = bad["jurisdictions"][:3]
        self.assertTrue(any(i["code"] == "count" for i in mod.validate_matrix(bad)))
        bad = json.loads(json.dumps(self.matrix))
        bad["jurisdictions"][1]["code"] = bad["jurisdictions"][0]["code"]
        self.assertTrue(any(i["code"] == "duplicate" for i in mod.validate_matrix(bad)))

    def test_matrix_rejects_pii_and_assigned_without_id(self):
        bad = json.loads(json.dumps(self.matrix))
        bad["jurisdictions"][0]["notes"] = "call +1 202 555 0100"
        self.assertTrue(any(i["code"] == "pii" for i in mod.validate_matrix(bad)))
        bad = json.loads(json.dumps(self.matrix))
        bad["jurisdictions"][0]["counsel_id"] = "Иван"
        self.assertTrue(any(i["code"] == "counsel_id" for i in mod.validate_matrix(bad)))

    def test_panel_rejects_pii_and_bad_verifier(self):
        bad = json.loads(json.dumps(self.panel))
        bad["counsel"] = [{
            "id": "HC-DE-001", "jurisdictions": ["DE"], "license": "BAR-DE-1",
            "verified_at": "2026-11-22", "verified_by": "agent:model",
            "email": "x@example.org",
        }]
        codes = {i["code"] for i in mod.validate_panel(bad, self.matrix)}
        self.assertIn("verified_by", codes)
        self.assertIn("pii", codes)

    def test_brief_blocks_unassigned_jurisdiction(self):
        with self.assertRaises(mod.HumanCounselError):
            mod._brief(self.matrix, "DE", "Тестовый вопрос")

    def test_brief_rejects_unknown_jurisdiction(self):
        with self.assertRaises(mod.HumanCounselError):
            mod._brief(self.matrix, "ZZ", "Тестовый вопрос")

    def test_signoff_rejects_ai_signer_and_missing_fields(self):
        signoff = {
            "schema": "amfe.legal.signoff.v1", "matter_id": "LEG-1", "jurisdiction": "DE",
            "counsel_id": "HC-DE-001", "license": "BAR", "scope": "test",
            "signed_at": "2026-11-22", "verified_by": "human:reviewer",
            "signed_by": "AI model", "ai_research_memo": True,
        }
        codes = {i["code"] for i in mod._validate_signoff(signoff, self.matrix)}
        self.assertIn("ai_signer", codes)
        self.assertIn("unassigned", codes)
        bad = {"schema": "amfe.legal.signoff.v1"}
        self.assertTrue(mod._validate_signoff(bad, self.matrix))


class Cli(unittest.TestCase):
    def test_check_ok(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("human_signoff_required=true", result.stdout)

    def test_matrix_json(self):
        result = run("--matrix", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["jurisdictions"], 190)

    def test_jurisdictions_filter(self):
        result = run("--jurisdictions", "--region", "Europe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DE", result.stdout)
        self.assertIn("UNASSIGNED", result.stdout + "PRIORITY_UNASSIGNED")

    def test_matter_requires_assigned(self):
        result = run("--matter", "--jurisdiction", "DE", "--question", "test")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no assigned human counsel", result.stderr)

    def test_matter_requires_question(self):
        result = run("--matter", "--jurisdiction", "DE")
        self.assertEqual(result.returncode, 2)

    def test_no_write_without_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = set(Path(tmp).iterdir())
            run("--matter", "--jurisdiction", "ZZ", "--question", "x")
            self.assertEqual(before, set(Path(tmp).iterdir()))

    def test_deterministic_matrix(self):
        self.assertEqual(run("--matrix", "--json").stdout, run("--matrix", "--json").stdout)


if __name__ == "__main__":
    unittest.main()
