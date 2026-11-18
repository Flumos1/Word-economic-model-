#!/usr/bin/env python3
"""Contract tests for the agent memory model and the librarian catalog."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "protocols" / "agent_roles.json"
INDEX_JSON = ROOT / "agents" / "library" / "index.json"
INDEX_MD = ROOT / "agents" / "library" / "INDEX.md"
MEMORY_TOOL = ROOT / "agents" / "tools" / "memory.py"

spec = importlib.util.spec_from_file_location("amfe_memory", MEMORY_TOOL)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


class MemoryRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roles = json.loads(ROLES.read_text(encoding="utf-8"))
        cls.role_ids = [role["id"] for role in cls.roles["roles"]]

    def test_librarian_and_memory_model(self):
        self.assertEqual(self.roles["librarian"], "dense-vintages")
        self.assertEqual(self.roles["memory_model"]["schema"], "amfe.agent_memory.v1")
        self.assertIn("agents/memory", self.roles["memory_model"]["directory"])

    def test_every_role_has_a_memory_file(self):
        for role in self.roles["roles"]:
            self.assertEqual(role["memory"], f"agents/memory/{role['id']}.jsonl", role["id"])
            self.assertTrue((ROOT / role["memory"]).exists(), role["id"])

    def test_librarian_has_the_duty(self):
        librarian = next(role for role in self.roles["roles"] if role["id"] == "dense-vintages")
        self.assertIn("librarian", librarian["duty"])
        self.assertIn("библиотек", librarian["mandate"].lower())

    def test_all_entries_are_valid(self):
        entries = memory.all_entries()
        self.assertGreaterEqual(len(entries), 9)
        for entry in entries:
            self.assertEqual(memory.validate_entry(entry), [], entry.get("id"))

    def test_ids_are_unique_and_sequential(self):
        for entry in memory.all_entries():
            self.assertRegex(entry["id"], rf"^mem-{re.escape(entry['role'])}-\d{{4}}$")
        ids = [entry["id"] for entry in memory.all_entries()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_secrets_in_memory(self):
        for path in (ROOT / "agents" / "memory").glob("*.jsonl"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(memory.SECRET_PATTERNS.search(text), path.name)

    def test_index_matches_the_memory_logs(self):
        index = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], "amfe.agent_library.v1")
        self.assertEqual(index["entries"], len(memory.all_entries()))
        for role in self.role_ids:
            self.assertEqual(index["by_role"].get(role), len(memory.read_entries(role)), role)
        self.assertTrue(INDEX_MD.exists())
        md = INDEX_MD.read_text(encoding="utf-8")
        self.assertIn("Библиотека памяти", md)
        self.assertIn("dense-vintages", md)

    def test_context_pack_for_any_model(self):
        done = subprocess.run([sys.executable, str(MEMORY_TOOL), "pack", "--role", "lead",
                               "--limit", "2"], capture_output=True, text=True, timeout=60,
                              cwd=str(ROOT))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Контекст роли `lead`", done.stdout)

    def test_validator_rejects_secrets(self):
        entry = {"id": "mem-x-0001", "ts": "2026-11-18T00:00:00Z", "role": "x",
                 "kind": "lesson", "title": "t", "summary": "api_key=abc123"}
        problems = memory.validate_entry(entry)
        self.assertIn("possible secret in entry", problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
