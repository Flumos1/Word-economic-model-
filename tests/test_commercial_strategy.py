#!/usr/bin/env python3
"""Tests for the lawful Commercial Strategy agent and role-based briefs."""
from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "commercial_strategy.py"
spec = importlib.util.spec_from_file_location("commercial_strategy", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)

class CommercialStrategyTests(unittest.TestCase):
    def test_role_and_playbooks_valid(self):
        self.assertEqual(mod.validate(), [])

    def test_role_id(self):
        data = json.loads((ROOT / "agents/commercial/org.json").read_text(encoding="utf-8"))
        self.assertEqual(data["role"]["id"], "commercial-strategist")
        self.assertEqual(data["role"]["evidence_ceiling"], "PARTIAL")

    def test_outreach_is_human_only(self):
        data = json.loads((ROOT / "agents/commercial/org.json").read_text(encoding="utf-8"))
        self.assertEqual(data["role"]["handoffs"]["approval_and_outreach"], "lead/human only")

    def test_skill_matrix_includes_procurement_and_discovery(self):
        data = json.loads((ROOT / "agents/commercial/org.json").read_text(encoding="utf-8"))
        skills = set(data["role"]["skills"])
        self.assertIn("public_procurement", skills)
        self.assertIn("discovery_questions", skills)
        self.assertIn("usd_pricebook", skills)

    def test_brief_has_public_role_hypothesis(self):
        brief = mod.build_brief("Stress-test one energy procurement decision", "public utility", "US", "C25-007")
        self.assertEqual(brief["status"], "HYPOTHESIS")
        self.assertEqual(brief["buyer_role"], "UNVERIFIED_ROLE_ONLY")
        self.assertIs(brief["sent"], False)
        self.assertIs(brief["outreach_authorized"], False)
        self.assertEqual(brief["offer"]["id"], "C25-007")

    def test_brief_price_matches_catalog(self):
        brief = mod.build_brief("Stress-test one energy procurement decision", "public utility", "US", "C25-007")
        self.assertEqual(brief["offer"]["fixed_price_usd"], 15000)

    def test_brief_has_handoffs(self):
        brief = mod.build_brief("Stress-test one energy procurement decision", "public utility", "US")
        self.assertGreaterEqual(len(brief["required_handoffs"]), 4)

    def test_unknown_offer_rejected(self):
        with self.assertRaises(ValueError):
            mod.build_brief("Stress-test one energy procurement decision", "utility", "US", "C25-999")

    def test_short_question_rejected(self):
        with self.assertRaises(ValueError):
            mod.build_brief("short", "utility", "US")

    def test_email_rejected(self):
        with self.assertRaises(ValueError):
            mod.build_brief("Evaluate procurement scenario", "buyer@example.org", "US")

    def test_phone_rejected(self):
        with self.assertRaises(ValueError):
            mod.build_brief("Evaluate procurement scenario", "+1 202 555 0123", "US")

    def test_covert_targeting_rejected(self):
        with self.assertRaises(ValueError):
            mod.build_brief("hidden network covert influence", "public fund", "US")

    def test_no_client_claims(self):
        brief = mod.build_brief("Evaluate procurement scenario", "public utility", "US", "C25-010")
        self.assertTrue(brief["offer"]["not_client"])

    def test_cli_check(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("human-only outreach", result.stdout)

    def test_cli_brief_json(self):
        result = run("--question", "Stress-test one energy procurement decision", "--buyer-type", "public utility",
                     "--jurisdiction", "US", "--offer-id", "C25-007", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["offer"]["id"], "C25-007")

    def test_cli_unknown_role_fails_closed(self):
        result = run("--role", "secret-agent", "--question", "Evaluate procurement scenario", "--buyer-type", "utility")
        self.assertEqual(result.returncode, 2)

    def test_docs_and_handoffs_exist(self):
        doc = (ROOT / "docs" / "228_Commercial_Strategy.md").read_text(encoding="utf-8")
        for token in ("osint-scout", "human-lawyer", "archivist", "outreach", "list price"):
            self.assertIn(token.lower(), doc.lower())

if __name__ == "__main__":
    unittest.main()
