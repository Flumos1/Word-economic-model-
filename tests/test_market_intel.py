#!/usr/bin/env python3
"""Tests for the market-intelligence org, its role charters and the brief generator."""
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
TOOL = ROOT / "tools" / "market_intel.py"
MI = ROOT / "agents" / "market_intelligence"
ORG = MI / "org.json"
ACCOUNTS = ROOT / "marketing" / "target_accounts.json"
DOCS = ROOT / "docs"
ROLES_DIR = MI / "roles"
PLAYBOOKS = MI / "playbooks"

spec = importlib.util.spec_from_file_location("market_intel", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PROMPT_SECTIONS = ("## Мандат", "## Входы", "## Можно", "## Нельзя", "## Формат отчёта",
                   "## Критерий приёмки", "## Передача")


def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, cwd=str(cwd), env=env)


def copy_package(tmp: str) -> tuple[Path, Path]:
    """Copy the org package and the accounts card into a temp dir (mutable fixture)."""
    destination = Path(tmp) / "agents" / "market_intelligence"
    shutil.copytree(MI, destination)
    accounts = Path(tmp) / "target_accounts.json"
    shutil.copy2(ACCOUNTS, accounts)
    return destination / "org.json", accounts


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"fixture text not found in {path.name}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class MarketIntelOrgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.org = json.loads(ORG.read_text(encoding="utf-8"))
        cls.roles = {role["id"]: role for role in cls.org["roles"]}
        cls.accounts = mod.load_accounts(ACCOUNTS)

    # --- happy path -------------------------------------------------------------------------

    def test_org_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.market_intel.check.v1")

    def test_schema_version_and_research_only(self):
        self.assertEqual(self.org["schema"], "amfe.market_intelligence.org.v1")
        self.assertRegex(self.org["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.org["status"], "RESEARCH_ONLY")
        self.assertIs(self.org["admission"], False)

    def test_one_lead_and_ten_roles(self):
        self.assertEqual(len(self.roles), 11)
        leads = [role for role in self.roles.values() if role["layer"] == "lead"]
        self.assertEqual([role["id"] for role in leads], ["director"])
        self.assertEqual(self.org["lead"], "director")
        self.assertEqual(self.org["team_limit"], len(self.roles))
        layers = sorted(role["layer"] for role in self.roles.values())
        self.assertEqual(layers.count("segment"), 5)
        self.assertEqual(layers.count("collection"), 2)
        self.assertEqual(layers.count("control"), 2)
        self.assertEqual(layers.count("output"), 1)

    def test_role_ids_are_kebab_and_unique(self):
        ids = [role["id"] for role in self.org["roles"]]
        self.assertEqual(len(ids), len(set(ids)))
        for role_id in ids:
            self.assertRegex(role_id, r"^[a-z][a-z0-9-]{1,40}$")

    def test_every_role_has_a_charter(self):
        prompts = sorted(path.name for path in ROLES_DIR.glob("*.md"))
        self.assertEqual(len(prompts), 11)
        for role in self.org["roles"]:
            path = MI / role["prompt"]
            self.assertTrue(path.is_file(), role["prompt"])
            text = path.read_text(encoding="utf-8")
            for section in PROMPT_SECTIONS:
                self.assertIn(section, text, f"{role['id']}: {section}")
            self.assertIn(role["id"], text, "charter must name its role id")
            self.assertGreater(len(text), 400)

    def test_review_chain_is_independent(self):
        external = {item["id"] for item in self.org["external_reviewers"]}
        self.assertIn("lead", external)
        for role in self.org["roles"]:
            reviewer = role["review_role"]
            self.assertNotEqual(reviewer, role["id"], role["id"])
            self.assertTrue(reviewer in self.roles or reviewer in external, role["id"])
        # the top of the network is reviewed outside the network
        self.assertEqual(self.roles["director"]["review_role"], "lead")

    def test_only_control_roles_certify_verified(self):
        signers = sorted(role["id"] for role in self.org["roles"]
                         if role["evidence_ceiling"] == "VERIFIED")
        self.assertEqual(signers, ["evidence-qa", "licensed-data"])
        self.assertEqual(self.roles["director"]["evidence_ceiling"], "PARTIAL")

    def test_process_pipeline_and_owners(self):
        process = self.org["process"]
        self.assertEqual(process["order"], list(mod.PROCESS_ORDER))
        self.assertEqual(process["states"], list(mod.PROCESS_STATES))
        self.assertEqual(process["owners"]["grade"], "evidence-qa")
        self.assertEqual(process["owners"]["qa"], "evidence-qa")
        self.assertEqual(process["owners"]["kb"], "kb-librarian")
        self.assertEqual(process["owners"]["collect"], "osint-scout")

    def test_boundaries_are_declared(self):
        boundaries = self.org["boundaries"]
        for key in mod.REQUIRED_BOUNDARIES:
            self.assertIn(key, boundaries)
        self.assertTrue(boundaries["lawful_sources_only"])
        self.assertIn("без взлома", boundaries["osint"])
        self.assertIn("персональные данные", boundaries["personal_data"])
        self.assertIn("санкционн", boundaries["sanctions"])
        self.assertIn("сети в среде нет", boundaries["network_in_env"])
        self.assertIn("не обходит авторизацию", boundaries["code"])
        self.assertGreaterEqual(len(boundaries["allowed_sources"]), 3)

    def test_evidence_scale(self):
        evidence = self.org["evidence"]
        self.assertEqual(tuple(evidence["grades"]), mod.GRADES)
        self.assertEqual(tuple(evidence["statuses"]), mod.STATUSES)
        self.assertEqual(evidence["rank"], mod.STATUS_RANK)
        self.assertGreaterEqual(len(evidence["verified_requires"]), 3)

    def test_accounts_are_owned_once(self):
        assigned = [account for role in self.org["roles"] for account in role["accounts"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(sorted(assigned), sorted(self.accounts))
        self.assertEqual(len(assigned), 20)

    def test_segment_coverage(self):
        catalog = {segment["id"]: segment for segment in self.org["segments"]}
        for account_id, record in self.accounts.items():
            self.assertIn(record["segment"], catalog, account_id)
        for segment_id, entry in catalog.items():
            if entry["in_portfolio"]:
                self.assertTrue(entry["covered_by"], segment_id)
        self.assertFalse(catalog["utilities_power"]["in_portfolio"])
        self.assertEqual(self.org["deferred_roles"][0]["id"], "utilities-industrials")

    def test_playbooks_exist(self):
        for name in mod.PLAYBOOKS:
            path = PLAYBOOKS / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(len(path.read_text(encoding="utf-8")), 400)

    def test_red_lines_cover_the_job(self):
        text = (PLAYBOOKS / "red_lines.md").read_text(encoding="utf-8")
        for marker in ("RL-01", "RL-11", "RL-13", "RL-16", "RL-18", "Процедура остановки"):
            self.assertIn(marker, text)
        grading = (PLAYBOOKS / "evidence_grading.md").read_text(encoding="utf-8")
        for marker in ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_FETCHED", "grade A"):
            self.assertIn(marker, grading)

    # --- CLI contract -----------------------------------------------------------------------

    def test_check_cli_exit_zero(self):
        result = run_cli("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_org_cli_lists_every_role(self):
        result = run_cli("--org")
        self.assertEqual(result.returncode, 0, result.stderr)
        for role_id in self.roles:
            self.assertIn(role_id, result.stdout)
        self.assertIn("QUESTION -> SOURCE_PLAN -> COLLECT -> GRADE -> ANALYSIS -> CASE -> QA -> KB",
                      result.stdout)

    def test_json_summary_contract(self):
        result = run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.market_intel.check.v1")
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["counts"]["roles"], 11)
        self.assertEqual(payload["counts"]["accounts_assigned"], 20)
        self.assertEqual(payload["policy"]["network"], "disabled")
        self.assertEqual(sorted(payload["boundaries"]), sorted(self.org["boundaries"]))

    # --- brief generator --------------------------------------------------------------------

    def test_brief_for_each_role(self):
        question = "Как изменится бюджет аккаунта при шоке цены на нефть"
        for role_id in self.roles:
            brief = mod.build_brief(self.org, role_id, question)
            self.assertIn(role_id, brief)
            self.assertIn(question, brief)
            self.assertIn("NOT_FETCHED", brief)
            self.assertIn("red lines", brief.lower())

    def test_brief_names_role_question_and_account(self):
        question = "Как шок цены меняет бюджет и CAPEX аккаунта"
        brief = mod.build_brief(self.org, "upstream", question, "TATNEFT", self.accounts)
        self.assertIn("Upstream & Integrated E&P Analyst", brief)
        self.assertIn("`TATNEFT`", brief)
        self.assertIn("HYPOTHESIS", brief)
        self.assertIn("marketing/target_accounts.json", brief)
        self.assertIn("evidence-qa", brief)
        self.assertIn("Source plan", brief)

    def test_brief_without_account_is_honest(self):
        brief = mod.build_brief(self.org, "gas-lng", "Что меняет фрахт СПГ в сценарии")
        self.assertIn("не указан", brief)
        self.assertIn("NOT_FETCHED", brief)

    def test_brief_contains_no_contacts(self):
        brief = mod.build_brief(self.org, "osint-scout", "Какие публичные документы закрывают вопрос")
        self.assertNotRegex(brief, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertNotRegex(brief, r"\+7[\s(-]")
        self.assertNotIn("admitted", brief.lower())

    def test_brief_is_deterministic(self):
        args = ("upstream", "Как шок цены меняет бюджет аккаунта", "TATNEFT")
        first = mod.build_brief(self.org, *args, self.accounts)
        second = mod.build_brief(self.org, *args, self.accounts)
        self.assertEqual(first, second)
        body = first.split("_Generated by tools/market_intel.py (no network).")[0]
        self.assertIn(mod._sha256(body), first)

    def test_brief_cli_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "brief.md"
            result = run_cli("--role", "upstream", "--question",
                             "Как шок цены меняет бюджет аккаунта", "--account", "TATNEFT",
                             "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn("TATNEFT", text)
            self.assertIn("NOT_FETCHED", text)

    def test_brief_cli_json(self):
        result = run_cli("--role", "evidence-qa", "--question",
                         "Как проверить evidence-таблицу по аккаунту", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.market_intel.brief.v1")
        self.assertEqual(payload["role"], "evidence-qa")
        self.assertEqual(payload["network"], "disabled")
        self.assertEqual(len(payload["brief_sha256"]), 64)

    def test_unknown_role_fails_closed(self):
        result = run_cli("--role", "nope", "--question", "Достаточно длинный вопрос про рынок")
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED_INPUT", result.stderr)

    def test_short_question_fails_closed(self):
        result = run_cli("--role", "upstream", "--question", "коротко")
        self.assertEqual(result.returncode, 2)
        self.assertIn("too short", result.stderr)

    def test_unknown_account_fails_closed(self):
        result = run_cli("--role", "upstream", "--question",
                         "Достаточно длинный вопрос про рынок", "--account", "NOPE")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown account", result.stderr)

    # --- guardrails -------------------------------------------------------------------------

    def test_tool_has_no_network_and_no_clock(self):
        text = TOOL.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import socket", "http.client",
                          "datetime.now", "date.today", "time.time", "urlopen"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_check_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            org, accounts = copy_package(tmp)
            before = {path: path.stat().st_mtime_ns
                      for path in Path(tmp).rglob("*") if path.is_file()}
            result = run_cli("--org-file", str(org), "--accounts-file", str(accounts), "--check")
            self.assertEqual(result.returncode, 0, result.stderr)
            after = {path: path.stat().st_mtime_ns
                     for path in Path(tmp).rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_broken_fixtures_fail_closed(self):
        cases = {
            "duplicate_id": ("roles", lambda org, _dir: org["roles"].append(dict(org["roles"][1]))),
            "self_review": ("roles", lambda org, _dir: org["roles"][1].__setitem__(
                "review_role", org["roles"][1]["id"])),
            "weak_boundary": ("roles", lambda org, _dir: org["boundaries"].pop("sanctions")),
            "bad_process": ("roles", lambda org, _dir: org["process"].__setitem__(
                "owners", {"question": "nobody"})),
            "missing_prompt": ("roles", lambda org, _dir: org["roles"][2].__setitem__(
                "prompt", "roles/does_not_exist.md")),
        }
        for name, (_, breaker) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                org_path, accounts = copy_package(tmp)
                org = json.loads(org_path.read_text(encoding="utf-8"))
                breaker(org, org_path.parent)
                org_path.write_text(json.dumps(org, ensure_ascii=False, indent=1), encoding="utf-8")
                result = run_cli("--org-file", str(org_path), "--accounts-file", str(accounts),
                                 "--check")
                self.assertEqual(result.returncode, 2, f"{name}: {result.stdout}{result.stderr}")
                self.assertIn("FAIL:", result.stderr)

    def test_missing_accounts_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path, _ = copy_package(tmp)
            result = run_cli("--org-file", str(org_path), "--accounts-file",
                             str(Path(tmp) / "nope.json"), "--check")
            self.assertEqual(result.returncode, 2)
            self.assertIn("accounts", result.stderr)

    def test_docs_reference_the_network(self):
        doc = DOCS / "212_Агентная_сеть_market_intelligence.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        for marker in ("director", "evidence-qa", "kb-librarian", "NOT_FETCHED", "OSINT",
                       "RESEARCH_ONLY"):
            self.assertIn(marker, text)


class GatedSourcesTest(unittest.TestCase):
    """Lawful access to gated sources: recognise G0-G4, request, never bypass, no secrets."""

    @classmethod
    def setUpClass(cls):
        cls.org = json.loads(ORG.read_text(encoding="utf-8"))
        cls.roles = {role["id"]: role for role in cls.org["roles"]}
        cls.gated = cls.org["gated_sources"]
        cls.classes = {entry["id"]: entry for entry in cls.gated["classes"]}
        cls.summary = mod.check()

    def test_gated_playbooks_are_declared_and_loaded(self):
        self.assertIn("gated_sources.md", mod.PLAYBOOKS)
        self.assertIn("access_request.md", mod.PLAYBOOKS)
        for name, markers in mod.GATED_MARKERS.items():
            path = PLAYBOOKS / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 800, name)
            low = text.lower()
            for marker in markers:
                self.assertIn(marker.lower(), low, f"{name}: {marker}")

    def test_gated_block_schema_and_statuses(self):
        self.assertEqual(self.gated["schema"], mod.GATED_SCHEMA)
        self.assertEqual(tuple(entry["id"] for entry in self.gated["classes"]), mod.GATED_CLASSES)
        self.assertEqual(tuple(self.gated["statuses"]), mod.GATED_STATUSES)
        fields = self.gated["journal"]["required_fields"]
        for field in mod.GATED_JOURNAL_FIELDS:
            self.assertIn(field, fields)
        self.assertFalse(self.gated["credentials"]["in_repo"])
        self.assertIn("NOT_AUTHORIZED", self.gated["status_titles"])

    def test_gated_classes_are_request_only_or_never(self):
        self.assertTrue(self.classes["G0"]["collect_now"])
        self.assertTrue(self.classes["G1"]["collect_now"])
        for class_id in ("G2", "G3"):
            self.assertFalse(self.classes[class_id]["collect_now"], class_id)
            self.assertTrue(self.classes[class_id]["request_required"], class_id)
            self.assertIn("access_request.md", self.classes[class_id]["request_path"])
        self.assertFalse(self.classes["G4"]["collect_now"])
        self.assertFalse(self.classes["G4"]["request_required"])
        self.assertEqual(self.classes["G4"]["access"], "NO_ACCESS")
        for entry in self.gated["classes"]:
            self.assertLessEqual(mod.STATUS_RANK[entry["evidence_ceiling"]],
                                 mod.STATUS_RANK["PARTIAL"], entry["id"])

    def test_gated_classes_map_to_source_classes(self):
        self.assertTrue(set(self.classes["G0"]["source_classes"]) <= {"S1", "S2", "S3"})
        self.assertTrue(set(self.classes["G1"]["source_classes"]) <= {"S1", "S2", "S3"})
        self.assertEqual(self.classes["G2"]["source_classes"], ["S4"])
        self.assertEqual(self.classes["G3"]["source_classes"], ["S4"])
        self.assertEqual(self.classes["G4"]["source_classes"], ["S5"])

    def test_osint_scout_has_gated_skills(self):
        role = self.roles["osint-scout"]
        for skill in mod.GATED_SKILLS:
            self.assertIn(skill, role["skills"], skill)
        for banned in mod.FORBIDDEN_SKILLS:
            self.assertNotIn(banned, role["skills"], banned)
        self.assertEqual(tuple(role["gated_classes"]), mod.GATED_CLASSES)
        self.assertEqual(role["gated_policy"], mod.GATED_POLICY)
        self.assertEqual(role["evidence_ceiling"], "PARTIAL")

    def test_osint_scout_outputs_and_forbidden_cover_access_request(self):
        role = self.roles["osint-scout"]
        outputs = " ".join(role["outputs"]).lower()
        self.assertIn("access request", outputs)
        self.assertIn("журнал", outputs)
        allowed = " ".join(role["allowed"]).lower()
        self.assertTrue("access request" in allowed or "запрос доступа" in allowed)
        forbidden = " ".join(role["forbidden"]).lower()
        self.assertIn("обход", forbidden)
        self.assertIn("утёкш", forbidden)
        self.assertIn("ключ", forbidden)

    def test_gated_no_bypass_policy(self):
        joined = " ".join(self.gated["no_bypass"]).lower()
        for topic in mod.GATED_NO_BYPASS_TOPICS:
            self.assertIn(topic, joined, topic)
        self.assertGreaterEqual(len(self.gated["rules"]), 3)

    def test_no_bypass_recipe_and_no_credentials_in_gated_docs(self):
        files = [ORG, ROLES_DIR / "06_osint_scout.md",
                 PLAYBOOKS / "gated_sources.md", PLAYBOOKS / "access_request.md",
                 DOCS / "224_Gated_источники.md"]
        for path in files:
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(mod.find_bypass_recipes(text), [], path.name)
            self.assertEqual(mod.find_credentials(text), [], path.name)

    def test_planted_credential_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path, accounts = copy_package(tmp)
            org = json.loads(org_path.read_text(encoding="utf-8"))
            org["gated_sources"]["credentials"]["api_key"] = "sk-" + "A" * 24
            org_path.write_text(json.dumps(org, ensure_ascii=False, indent=1), encoding="utf-8")
            result = run_cli("--org-file", str(org_path), "--accounts-file", str(accounts),
                             "--check")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("credential", result.stderr)

    def test_g4_collect_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path, accounts = copy_package(tmp)
            org = json.loads(org_path.read_text(encoding="utf-8"))
            entry = next(item for item in org["gated_sources"]["classes"] if item["id"] == "G4")
            entry["collect_now"] = True
            org_path.write_text(json.dumps(org, ensure_ascii=False, indent=1), encoding="utf-8")
            result = run_cli("--org-file", str(org_path), "--accounts-file", str(accounts),
                             "--check")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("gated_collect_policy", result.stderr)

    def test_missing_access_request_playbook_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            org_path, accounts = copy_package(tmp)
            (org_path.parent / "playbooks" / "access_request.md").unlink()
            result = run_cli("--org-file", str(org_path), "--accounts-file", str(accounts),
                             "--check")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("access_request.md", result.stderr)

    def test_access_request_builder(self):
        text = mod.build_access_request(
            self.org, "Price DB", "G2",
            "проверка CAPEX-бенчмарка по upstream-аккаунтам",
            "NDA от 2026-11-20, п. 3")
        for marker in ("REQUEST_PENDING", "G2", "NOT_AUTHORIZED", "Правовое основание",
                       "Цель", "Срок", "Что не копируем", "director", "osint-scout"):
            self.assertIn(marker, text)
        self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertIn(mod._sha256(text.split("_Generated by")[0]), text)
        self.assertEqual(text, mod.build_access_request(
            self.org, "Price DB", "G2",
            "проверка CAPEX-бенчмарка по upstream-аккаунтам",
            "NDA от 2026-11-20, п. 3"))

    def test_access_request_refuses_public_and_restricted(self):
        for gated_class, expected in (("G0", "public"), ("G4", "no lawful access"), ("G9", "unknown")):
            with self.subTest(gated_class=gated_class):
                with self.assertRaises(mod.OrgError) as ctx:
                    mod.build_access_request(self.org, "Source", gated_class,
                                             "достаточно длинная цель запроса",
                                             "NDA от 2026-11-20")
                self.assertIn(expected, str(ctx.exception))

    def test_access_request_cli(self):
        result = run_cli("--access-request", "--source", "Price DB", "--gated-class", "G2",
                         "--purpose", "проверка CAPEX-бенчмарка", "--legal-basis",
                         "NDA от 2026-11-20", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.market_intel.access_request.v1")
        self.assertEqual(payload["status"], "REQUEST_PENDING")
        self.assertTrue(payload["human_approval_required"])
        self.assertEqual(payload["network"], "disabled")
        self.assertIn("Price DB", payload["request"])
        short = run_cli("--access-request", "--source", "Price DB", "--gated-class", "G2",
                        "--purpose", "проверка CAPEX-бенчмарка", "--legal-basis", "")
        self.assertEqual(short.returncode, 2)
        self.assertIn("legal_basis", short.stderr)

    def test_json_summary_has_gated_contract(self):
        result = run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        gated = payload["gated"]
        self.assertEqual(gated["schema"], mod.GATED_SCHEMA)
        self.assertEqual(gated["collect_now"], ["G0", "G1"])
        self.assertEqual(gated["request_only"], ["G2", "G3"])
        self.assertEqual(gated["never"], ["G4"])
        self.assertFalse(gated["credentials_in_repo"])
        self.assertIn("G2/G3 request-only", payload["policy"]["gated"])

    def test_docs_212_and_224_cover_gated_sources(self):
        doc212 = (DOCS / "212_Агентная_сеть_market_intelligence.md").read_text(encoding="utf-8")
        self.assertIn("Gated sources: законный доступ", doc212)
        for marker in ("G0", "G1", "G2", "G3", "G4", "NDA", "FOIA", "NOT_AUTHORIZED"):
            self.assertIn(marker, doc212)
        doc224 = DOCS / "224_Gated_источники.md"
        self.assertTrue(doc224.is_file())
        text = doc224.read_text(encoding="utf-8")
        for marker in ("G0", "G1", "G2", "G3", "G4", "NDA", "FOIA", "журнал",
                       "NOT_AUTHORIZED", "access request"):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
