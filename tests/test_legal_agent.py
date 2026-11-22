#!/usr/bin/env python3
"""Tests for the legal-counsel role, its playbooks, citation integrity and the CLI."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "legal_research.py"
LEGAL = ROOT / "agents" / "legal"
ORG = LEGAL / "org.json"
DOCS = ROOT / "docs"
PLAYBOOKS = LEGAL / "playbooks"

spec = importlib.util.spec_from_file_location("legal_research", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CHARTER_SECTIONS = ("## Мандат", "## Входы", "## Можно", "## Нельзя", "## Формат отчёта",
                    "## Критерий приёмки", "## Передача")
PUBLIC_DOMAINS = ("treaties", "custom", "un-charter", "state-responsibility",
                  "sanctions-export-control", "investment-protection", "wto-trade",
                  "human-rights", "law-of-the-sea")
PRIVATE_DOMAINS = ("jurisdiction", "choice-of-law", "rome-i-ii", "hague-conventions",
                   "recognition-enforcement", "arbitration", "contracts", "corporate", "ip",
                   "data-protection", "employment")


def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, cwd=str(cwd), env=env)


def citation(**overrides) -> dict:
    """A synthetic, fully-formed citation row (not a real citation, only a fixture)."""
    row = {
        "id": "C-01",
        "forum": "icj",
        "jurisdiction": "intl",
        "title": "Synthetic Test Case A v. Synthetic Test Case B",
        "date": "2000-01-01",
        "numbers": "ICJ Reports 2000, p. 1",
        "pinpoint": "para. 42",
        "holding_type": "ratio",
        "good_law": "GOOD_LAW",
        "status": "VERIFIED",
        "url": "https://www.icj-cij.org/case/1",
        "retrieved_at": "2026-11-22",
        "verified_by": "human:test-reviewer",
        "note": "synthetic fixture, not a real citation",
    }
    row.update(overrides)
    return row


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def copy_package(tmp: str) -> tuple[Path, Path]:
    destination = Path(tmp) / "agents" / "legal"
    shutil.copytree(LEGAL, destination)
    return destination / "org.json", destination


class LegalPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.org = json.loads(ORG.read_text(encoding="utf-8"))
        cls.role = cls.org["role"]
        cls.domains = {entry["id"]: entry for entry in cls.org["domains"]}

    # --- package -----------------------------------------------------------------------------

    def test_package_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.legal.check.v1")

    def test_schema_status_and_no_advice(self):
        self.assertEqual(self.org["schema"], "amfe.legal.org.v1")
        self.assertRegex(self.org["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.org["status"], "RESEARCH_ONLY")
        self.assertIs(self.org["admission"], False)
        self.assertIs(self.org["advice"], False)
        self.assertEqual(self.role["advice_ceiling"], "RESEARCH_MEMO")
        self.assertTrue(mod.is_iso_date(self.org["as_of"]))

    def test_role_identity_charter_and_skills(self):
        self.assertEqual(self.role["id"], "international-counsel")
        self.assertEqual(self.role["alias"], "legal-counsel")
        self.assertEqual(self.role["layer"], "advisory")
        self.assertEqual(self.role["human_reviewer"], "human-lawyer")
        self.assertEqual(self.role["memory"], "agents/memory/international-counsel.jsonl")
        for name in ("charter.md", "skills.md"):
            text = (LEGAL / name).read_text(encoding="utf-8")
            self.assertIn("international-counsel", text)
            self.assertGreater(len(text), 800)
        charter = (LEGAL / "charter.md").read_text(encoding="utf-8")
        for section in CHARTER_SECTIONS:
            self.assertIn(section, charter)
        self.assertIn(mod.DISCLAIMER_ANCHOR, charter)

    def test_skills_matrix_covers_every_domain(self):
        matrix = {entry["id"]: entry for entry in self.role["skills_matrix"]}
        self.assertEqual(sorted(matrix), sorted(mod.SKILL_IDS))
        for entry in matrix.values():
            self.assertIn(entry["level"], mod.SKILL_LEVELS)
        public = set(matrix["public-international-law"]["domains"])
        private = set(matrix["private-international-law"]["domains"])
        self.assertEqual(public, set(PUBLIC_DOMAINS))
        self.assertEqual(private, set(PRIVATE_DOMAINS))
        self.assertFalse(public & private)
        self.assertEqual(public | private, set(self.domains))
        self.assertEqual(matrix["precedent-research"]["domains"], [])
        self.assertEqual(matrix["legal-opinion"]["domains"], [])

    def test_domains_are_covered_by_the_right_playbook(self):
        self.assertEqual(len(self.domains), 20)
        public_text = (PLAYBOOKS / "public_international_law.md").read_text(encoding="utf-8")
        private_text = (PLAYBOOKS / "private_international_law.md").read_text(encoding="utf-8")
        for domain_id in PUBLIC_DOMAINS:
            self.assertEqual(self.domains[domain_id]["group"], "public")
            self.assertIn(domain_id, public_text)
            self.assertGreaterEqual(len(self.domains[domain_id]["questions"]), 2)
            self.assertTrue(self.domains[domain_id]["limits"])
        for domain_id in PRIVATE_DOMAINS:
            self.assertEqual(self.domains[domain_id]["group"], "private")
            self.assertIn(domain_id, private_text)
            self.assertGreaterEqual(len(self.domains[domain_id]["questions"]), 2)
            self.assertTrue(self.domains[domain_id]["limits"])

    def test_public_playbook_covers_public_international_law(self):
        text = (PLAYBOOKS / "public_international_law.md").read_text(encoding="utf-8")
        for marker in ("VCLT", "opinio juris", "Устав ООН", "ARSIWA" if "ARSIWA" in text else "ILC",
                       "санкц", "экспортн", "инвестиц", "ВТО", "права человека", "UNCLOS"):
            self.assertIn(marker.lower(), text.lower(), marker)

    def test_private_playbook_covers_private_international_law(self):
        text = (PLAYBOOKS / "private_international_law.md").read_text(encoding="utf-8")
        for marker in ("Rome I", "Rome II", "Hague", "New York", "Brussels Ia", "ICC", "LCIA",
                       "UNCITRAL", "CISG", "GDPR", "lex arbitri", "ordre public"):
            self.assertIn(marker.lower(), text.lower(), marker)

    def test_precedent_playbook_requires_verification(self):
        text = (PLAYBOOKS / "precedent_research.md").read_text(encoding="utf-8")
        for marker in ("ICJ", "PCIJ", "ITLOS", "WTO", "ICSID", "HUDOC", "CURIA", "UNILEX",
                       "CISG", "ratio", "obiter", "no fake citation", "pinpoint",
                       "GOOD_LAW", "NOT_VERIFIED"):
            self.assertIn(marker.lower(), text.lower(), marker)
        self.assertIn("по памяти", text)

    def test_source_hierarchy_is_fail_closed(self):
        hierarchy = self.org["source_hierarchy"]
        self.assertEqual([level["id"] for level in hierarchy["levels"]], list(mod.LEVELS))
        first, last = hierarchy["levels"][0], hierarchy["levels"][-1]
        self.assertEqual(first["evidence_ceiling"], "VERIFIED")
        self.assertTrue(first["citation_required"])
        self.assertEqual(last["evidence_ceiling"], "NOT_FETCHED")
        self.assertFalse(last["citation_required"])
        self.assertGreaterEqual(len(hierarchy["forbidden_as_sole_basis"]), 3)
        self.assertTrue(hierarchy["rule"])

    def test_opinion_format_and_disclaimer(self):
        opinion = self.org["opinion"]
        self.assertEqual(tuple(opinion["sections"]), mod.OPINION_SECTIONS)
        for section in mod.OPINION_SECTIONS:
            self.assertTrue(opinion["titles"][section])
            self.assertTrue(opinion["requirements"][section])
        self.assertTrue(opinion["disclaimer_required"])
        self.assertIn(mod.DISCLAIMER_ANCHOR, opinion["disclaimer"])
        self.assertIs(opinion["client_advice"], False)
        self.assertTrue(opinion["human_review_required"])

    def test_boundaries_ethics_and_handoffs(self):
        boundaries = self.org["boundaries"]
        for key in mod.REQUIRED_BOUNDARIES:
            self.assertIn(key, boundaries)
        self.assertIn("не консультация", boundaries["no_legal_advice"])
        self.assertIn("персональные данные", boundaries["no_personal_data"])
        self.assertIn("обход", boundaries["sanctions"])
        self.assertIn("не подписывает", boundaries["no_signing"])
        self.assertIn("не выдумываются", boundaries["no_fake_citations"])
        handoffs = {key: entry["role"] for key, entry in self.org["handoffs"].items()}
        self.assertEqual(handoffs["lead"], "lead")
        self.assertEqual(handoffs["osint"], "osint-scout")
        self.assertEqual(handoffs["archivist"], "kb-librarian")
        self.assertEqual(handoffs["human_lawyer"], "human-lawyer")
        self.assertGreaterEqual(len(self.org["non_goals"]), 3)
        self.assertGreaterEqual(len(self.org["review_gates"]), 4)
        self.assertGreaterEqual(len(self.org["limits"]), 3)

    def test_citation_policy(self):
        citation = self.org["citation"]
        self.assertEqual(tuple(citation["statuses"]), mod.STATUSES)
        self.assertEqual(citation["status_rank"], mod.STATUS_RANK)
        self.assertEqual(tuple(citation["holding_types"]), mod.HOLDING_TYPES)
        self.assertEqual(tuple(citation["good_law"]), mod.GOOD_LAW)
        self.assertTrue(citation["ai_verification_forbidden"])
        self.assertEqual(tuple(citation["verified_by_prefixes"]), ("human:", "official:"))
        self.assertGreaterEqual(len(citation["forums"]), mod.MIN_CITATION_FORUMS)
        self.assertGreaterEqual(len(citation["verified_requires"]), 4)
        for forum_id, forum in citation["forums"].items():
            for host in forum["hosts"]:
                self.assertTrue(mod.host_matches(host.lstrip("*."), citation["official_hosts"]),
                                f"{forum_id}: {host}")
        self.assertTrue(mod.host_matches("en.wikipedia.org", citation["forbidden_hosts"]))
        self.assertTrue(mod.host_matches("sub.example", citation["official_hosts"]) is False)

    # --- citation integrity ------------------------------------------------------------------

    def test_valid_citation_passes(self):
        self.assertEqual(mod.check_citations([citation()], self.org), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "citations.json",
                              {"schema": "amfe.legal.citation.v1", "citations": [citation()]})
            result = run_cli("--check", "--citations", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_fake_or_missing_citation_fails_closed(self):
        cases = {
            "ai_verifier": citation(verified_by="assistant"),
            "model_verifier": citation(verified_by="human:model"),
            "future_date": citation(date="2030-01-01"),
            "wikipedia_locator": citation(url="https://en.wikipedia.org/wiki/Case"),
            "http_not_https": citation(url="http://www.icj-cij.org/case/1"),
            "unknown_forum": citation(forum="secret-tribunal"),
            "missing_pinpoint": citation(pinpoint=""),
            "pinpoint_without_number": citation(pinpoint="para."),
            "missing_numbers": citation(numbers=""),
            "todo_marker": citation(title="TODO v. TBD"),
            "unknown_good_law": citation(good_law="MAYBE"),
            "unknown_holding": citation(holding_type="guess"),
            "unknown_status": citation(status="PROBABLY"),
            "verified_without_locator": citation(url=""),
            "verified_without_retrieval": citation(retrieved_at=""),
        }
        for name, row in cases.items():
            with self.subTest(case=name):
                issues = mod.check_citations([row], self.org)
                self.assertTrue(issues, f"{name} should fail")
        # a NOT_VERIFIED row is acceptable only with an explanation note
        without_note = citation(status="NOT_VERIFIED", url="", retrieved_at="", verified_by="",
                                note="")
        self.assertTrue(mod.check_citations([without_note], self.org))

    def test_duplicate_and_unknown_jurisdiction_citations(self):
        duplicates = [citation(), citation(title="Another case")]
        codes = {issue["code"] for issue in mod.check_citations(duplicates, self.org)}
        self.assertIn("citation_duplicate", codes)
        unknown = citation(id="C-09", jurisdiction="atlantis", forum="cjeu",
                           url="https://curia.europa.eu/juris/list.jsf")
        codes = {issue["code"] for issue in mod.check_citations([unknown], self.org)}
        self.assertIn("citation_jurisdiction", codes)
        empty = mod.check_citations([], self.org)
        self.assertEqual(empty[0]["code"], "citations_empty")

    def test_conflict_of_laws_fields(self):
        jurisdictions = {entry["id"] for entry in self.org["jurisdictions"]}
        for required in ("intl", "eu", "echr", "uk-ew", "us-federal", "de", "fr", "ch", "ua",
                         "md"):
            self.assertIn(required, jurisdictions)
        for domain_id in ("jurisdiction", "choice-of-law", "rome-i-ii", "hague-conventions",
                          "recognition-enforcement", "arbitration", "data-protection",
                          "employment"):
            self.assertIn(domain_id, self.domains)
        eu_case = citation(id="C-01", forum="cjeu", jurisdiction="eu",
                           title="Synthetic EU Test Case", numbers="ECLI:EU:C:2000:1",
                           url="https://curia.europa.eu/juris/list.jsf")
        self.assertEqual(mod.check_citations([eu_case], self.org), [])
        no_jurisdiction = citation(id="C-02", jurisdiction="")
        codes = {issue["code"] for issue in mod.check_citations([no_jurisdiction], self.org)}
        self.assertIn("citation_required", codes)

    # --- CLI ---------------------------------------------------------------------------------

    def test_check_cli_exit_zero(self):
        result = run_cli("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK:", result.stdout)
        self.assertIn("advice=false", result.stdout)

    def test_json_and_summary_contract(self):
        result = run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.legal.check.v1")
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["counts"]["domains"], 20)
        self.assertEqual(payload["counts"]["domains_public"], 9)
        self.assertEqual(payload["counts"]["domains_private"], 11)
        self.assertEqual(payload["policy"]["network"], "disabled")
        self.assertIs(payload["policy"]["advice"], False)
        self.assertEqual(payload["policy"]["personal_data"], "forbidden")
        self.assertEqual(payload["citation_policy"]["ai_verification"], "forbidden")
        summary = run_cli("--summary")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn("international-counsel", summary.stdout)
        self.assertIn("L1", summary.stdout)

    def test_source_plan_cli_and_json(self):
        result = run_cli("--source-plan", "--jurisdiction", "eu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EUR-Lex", result.stdout)
        self.assertIn("NOT_FETCHED", result.stdout)
        self.assertIn("L5", result.stdout)
        payload = json.loads(run_cli("--source-plan", "--jurisdiction", "intl", "--json").stdout)
        self.assertEqual(payload["schema"], "amfe.legal.source_plan.v1")
        self.assertEqual(payload["jurisdiction"], "intl")
        self.assertEqual([level["id"] for level in payload["levels"]], list(mod.LEVELS))
        self.assertTrue(payload["portals"])
        self.assertEqual(payload["network"], "disabled")

    def test_unknown_jurisdiction_fails_closed(self):
        result = run_cli("--source-plan", "--jurisdiction", "atlantis")
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED_INPUT", result.stderr)
        self.assertIn("unknown jurisdiction", result.stderr)

    # --- brief -------------------------------------------------------------------------------

    def test_brief_names_role_question_and_jurisdiction(self):
        question = "Применимо ли право ЕС к поставке товара двойного назначения в третью страну"
        brief = mod.build_brief(self.org, "international-counsel", question, "eu")
        self.assertIn("international-counsel", brief)
        self.assertIn(question, brief)
        self.assertIn("Европейский союз", brief)
        self.assertIn("NOT_VERIFIED", brief)
        self.assertIn("NOT_FETCHED", brief)
        self.assertIn("ratio", brief)
        self.assertIn(mod.DISCLAIMER_ANCHOR, brief.lower())
        self.assertIn("human-lawyer", brief)

    def test_brief_alias_works(self):
        brief = mod.build_brief(self.org, "legal-counsel",
                                "Что говорит практика о признании решения в другой юрисдикции",
                                "uk-ew")
        self.assertIn("`international-counsel`", brief)
        self.assertIn("UK Supreme Court", brief)

    def test_brief_is_deterministic(self):
        args = ("international-counsel", "Можно ли применить CISG к поставке между сторонами", "intl")
        first = mod.build_brief(self.org, *args)
        second = mod.build_brief(self.org, *args)
        self.assertEqual(first, second)
        body = first.split("_Generated by tools/legal_research.py")[0]
        self.assertIn(mod._sha256(body), first)

    def test_brief_with_citations_and_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write_json(Path(tmp) / "good.json", [citation()])
            result = run_cli("--role", "international-counsel", "--question",
                             "Применима ли юрисдикционная оговорка к спору с третьей стороной",
                             "--jurisdiction", "intl", "--citations", str(good), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], "amfe.legal.brief.v1")
            self.assertEqual(payload["citations"], 1)
            self.assertIs(payload["advice"], False)
            self.assertIn("C-01", payload["brief"])
            bad = write_json(Path(tmp) / "bad.json", [citation(verified_by="assistant")])
            rejected = run_cli("--role", "international-counsel", "--question",
                               "Применима ли юрисдикционная оговорка к спору с третьей стороной",
                               "--citations", str(bad))
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("citations rejected", rejected.stderr)

    def test_brief_has_no_personal_data_or_advice_claims(self):
        brief = mod.build_brief(self.org, "international-counsel",
                                "Какие санкционные ограничения применяются к поставке в страну A",
                                "intl")
        self.assertNotRegex(brief, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertNotRegex(brief, r"\+7[\s(-]")
        self.assertNotRegex(brief, r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
        self.assertNotIn("гарантиру", brief.lower())
        self.assertNotIn("attorney-client", brief.lower())

    def test_unknown_role_and_short_question_fail_closed(self):
        result = run_cli("--role", "nope", "--question", "Достаточно длинный правовой вопрос")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown role", result.stderr)
        result = run_cli("--role", "international-counsel", "--question", "коротко")
        self.assertEqual(result.returncode, 2)
        self.assertIn("too short", result.stderr)

    # --- guardrails --------------------------------------------------------------------------

    def test_tool_has_no_network_and_no_clock(self):
        text = TOOL.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import socket", "http.client",
                          "datetime.now", "date.today", "time.time", "urlopen"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_check_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path, legal_dir = copy_package(tmp)
            before = {path: path.stat().st_mtime_ns
                      for path in Path(tmp).rglob("*") if path.is_file()}
            result = run_cli("--org-file", str(org_path), "--check")
            self.assertEqual(result.returncode, 0, result.stderr)
            after = {path: path.stat().st_mtime_ns
                     for path in Path(tmp).rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_broken_fixtures_fail_closed(self):
        def drop_playbook(org, legal_dir):
            (legal_dir / "playbooks" / "precedent_research.md").unlink()

        def break_l1(org, legal_dir):
            org["source_hierarchy"]["levels"][0]["evidence_ceiling"] = "PARTIAL"

        def disable_disclaimer(org, legal_dir):
            org["opinion"]["disclaimer_required"] = False

        def drop_sanctions(org, legal_dir):
            org["boundaries"].pop("sanctions")

        def unknown_handoff(org, legal_dir):
            org["handoffs"]["osint"]["role"] = "nobody"

        def same_alias(org, legal_dir):
            org["role"]["alias"] = org["role"]["id"]

        def drop_domain(org, legal_dir):
            org["domains"] = [entry for entry in org["domains"] if entry["id"] != "treaties"]

        def drop_status(org, legal_dir):
            org["citation"]["statuses"] = ["VERIFIED", "PARTIAL", "NOT_FETCHED"]

        def break_charter(org, legal_dir):
            path = legal_dir / "charter.md"
            text = path.read_text(encoding="utf-8").replace("## Передача", "## Хендофф", 1)
            path.write_text(text, encoding="utf-8")

        cases = {
            "missing_playbook": drop_playbook,
            "l1_not_verified": break_l1,
            "disclaimer_off": disable_disclaimer,
            "sanctions_boundary_missing": drop_sanctions,
            "unknown_handoff": unknown_handoff,
            "alias_equals_id": same_alias,
            "domain_removed": drop_domain,
            "status_removed": drop_status,
            "charter_section_removed": break_charter,
        }
        for name, breaker in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                org_path, legal_dir = copy_package(tmp)
                org = json.loads(org_path.read_text(encoding="utf-8"))
                breaker(org, legal_dir)
                write_json(org_path, org)
                result = run_cli("--org-file", str(org_path), "--check")
                self.assertEqual(result.returncode, 2, f"{name}: {result.stdout}{result.stderr}")
                self.assertIn("FAIL:", result.stderr)

    def test_missing_citation_file_fails_closed(self):
        result = run_cli("--check", "--citations", str(Path(tempfile.gettempdir()) / "nope-legal.json"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("FAIL:", result.stderr)
        self.assertIn("citation_file", result.stderr)

    def test_docs_reference_the_role_and_interactions(self):
        doc = DOCS / "222_Юрист_международник.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        for marker in ("international-counsel", "legal-counsel", "osint-scout", "kb-librarian",
                       "archivist", "Lead", "human-lawyer", "RESEARCH_ONLY", "санкц",
                       "не является юридической консультацией", "fake citation"):
            self.assertIn(marker.lower(), text.lower(), marker)


if __name__ == "__main__":
    unittest.main()
