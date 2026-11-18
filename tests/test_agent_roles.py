#!/usr/bin/env python3
"""Contract tests for the agent-role registry and the 50-use-case registry."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "protocols" / "agent_roles.json"
CASES = ROOT / "protocols" / "use_cases_50.json"


class AgentRolesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(ROLES.read_text(encoding="utf-8"))
        cls.roles = {role["id"]: role for role in cls.data["roles"]}

    def test_schema_and_orchestrator(self):
        self.assertEqual(self.data["schema"], "amfe.agent_roles.v1")
        self.assertEqual(self.data["orchestrator"], "lead")
        self.assertIn("lead", self.roles)

    def test_all_team_members_have_a_role(self):
        targets = {role["target"] for role in self.roles.values()}
        expected = {"lead", "protocol-reviewer", "licence-auditor", "baseline-models", "r2-completion",
                    "evaluation-harness", "dense-vintages", "multiplicity-rw", "accounting-core"}
        self.assertEqual(targets, expected)

    def test_every_role_has_a_mandate_and_outputs(self):
        for role in self.roles.values():
            self.assertTrue(role.get("mandate"), role["id"])
            self.assertTrue(role.get("outputs"), role["id"])
            self.assertTrue(role.get("owns"), role["id"])

    def test_review_roles_resolve(self):
        for role in self.roles.values():
            self.assertIn(role["review_role"], self.roles, role["id"])

    def test_no_role_owns_frozen_protocols(self):
        frozen = {"protocols/evaluation_protocol_v4.json", "protocols/evaluation_protocol_v5.json",
                  "protocols/r3_specification.json", "protocols/nf_sim_thresholds.json",
                  "protocols/comparator_composition_2026-11-11.json"}
        for role in self.roles.values():
            for owned in role.get("owns", []):
                self.assertNotIn(owned, frozen, role["id"])

    def test_definition_of_done_and_gates(self):
        self.assertGreaterEqual(len(self.data["definition_of_done"]), 5)
        self.assertGreaterEqual(len(self.data["review_gates"]), 4)
        self.assertTrue(self.data.get("fallback"))


class UseCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(CASES.read_text(encoding="utf-8"))

    def test_exactly_fifty_cases(self):
        self.assertEqual(self.data["count"], 50)
        self.assertEqual(len(self.data["cases"]), 50)

    def test_unique_ids_and_required_fields(self):
        ids = [case["id"] for case in self.data["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in self.data["cases"]:
            for field in ("id", "title", "category", "user", "question", "status",
                          "capability_gap", "priority", "wave"):
                self.assertIn(field, case, case.get("id"))

    def test_statuses_and_priorities(self):
        statuses = {case["status"] for case in self.data["cases"]}
        self.assertEqual(statuses, {"READY", "PARTIAL", "GAP"})
        for case in self.data["cases"]:
            self.assertIn(case["priority"], {"P0", "P1", "P2"})
            self.assertIn(case["wave"], {25, 26, 27})

    def test_every_gap_has_a_capability_statement(self):
        for case in self.data["cases"]:
            self.assertTrue(case["capability_gap"].strip(), case["id"])
            if case["status"] == "GAP":
                self.assertNotIn("READY", case["capability_gap"])

    def test_no_admission_claims_in_titles(self):
        for case in self.data["cases"]:
            self.assertNotIn("admitted", case["title"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
