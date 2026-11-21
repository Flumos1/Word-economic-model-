#!/usr/bin/env python3
"""Tests for the target-account research dossiers and their validator."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "account_research.py"
RESEARCH = ROOT / "marketing" / "research"
TARGETS = ROOT / "marketing" / "target_accounts.json"
README = RESEARCH / "README.md"
DOC209 = ROOT / "docs" / "209_Исследование_заказчиков.md"
REQUIRED = ("TATNEFT", "TANECO", "GAZPROMNEFT", "NOVATEK", "IRKUTSK_OIL",
            "LUKOIL", "ROSNEFT", "SURGUTNEFTEGAS", "SIBUR", "ZARUBEZHNEFT")

spec = importlib.util.spec_from_file_location("account_research", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT), env=env)


def copy_package(tmp: str) -> Path:
    destination = Path(tmp) / "research"
    shutil.copytree(RESEARCH, destination)
    return destination


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"fixture text not found in {path.name}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class AccountResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.targets = mod.load_targets(TARGETS)
        cls.vocabulary = mod.known_use_cases(cls.targets)
        cls.texts = {account: (RESEARCH / f"{account.lower()}.md").read_text(encoding="utf-8")
                     for account in REQUIRED}

    # --- happy path -------------------------------------------------------------------------

    def test_package_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.account_research.v1")

    def test_ten_dossiers(self):
        self.assertEqual(self.summary["accounts"], 10)
        found = sorted(path.name for path in RESEARCH.glob("*.md")
                       if path.name.casefold() != "readme.md")
        self.assertEqual(found, sorted(f"{account.lower()}.md" for account in REQUIRED))

    def test_ids_and_profiles_match_target_accounts(self):
        self.assertTrue(set(REQUIRED) <= set(self.targets))
        for detail in self.summary["dossiers"]:
            target = self.targets[detail["id"]]
            self.assertEqual(detail["company"], target["name"])
            self.assertEqual(detail["segment"], target["segment"])
            self.assertEqual(detail["hq"], target["hq"])

    def test_evidence_status_is_declared_and_consistent(self):
        self.assertEqual(self.summary["evidence"]["HYPOTHESIS"], 0)
        for account, text in self.texts.items():
            parsed = mod.parse_dossier(text)
            status = parsed["fields"]["evidence_status"]
            self.assertIn(status, mod.EVIDENCE_STATUSES)
            tags = set(mod.TAG_RE.findall(parsed["sections"][1]))
            strongest = max(tags, key=lambda tag: mod.EVIDENCE_RANK[tag])
            self.assertEqual(mod.EVIDENCE_RANK[status], mod.EVIDENCE_RANK[strongest], account)

    def test_pains_questions_and_use_cases_counts(self):
        for detail in self.summary["dossiers"]:
            self.assertEqual(detail["pains"], 3, detail["id"])
            self.assertEqual(detail["questions"], 5, detail["id"])
            self.assertEqual(detail["use_case_rows"], 3, detail["id"])
            self.assertGreaterEqual(detail["verify_items"], 5, detail["id"])

    def test_use_cases_are_known_modules(self):
        self.assertTrue(self.vocabulary)
        for detail in self.summary["dossiers"]:
            self.assertTrue(set(detail["use_cases"]) <= self.vocabulary, detail["id"])
            self.assertGreaterEqual(len(detail["use_cases"]), 3, detail["id"])

    def test_four_trigger_classes(self):
        self.assertEqual(self.summary["trigger_coverage"],
                         {stem: 10 for stem in mod.TRIGGER_STEMS})

    def test_all_pains_are_hypotheses(self):
        for account, text in self.texts.items():
            pains = mod.numbered_items(mod.parse_dossier(text)["sections"][2])
            for index, pain in enumerate(pains, start=1):
                self.assertIn("[HYPOTHESIS]", pain, f"{account} pain {index}")

    # --- policy: no contacts, no invented figures, no admitted model --------------------------

    def test_no_contacts_or_admission_claims(self):
        for account, text in self.texts.items():
            for pattern, code, _ in mod.FORBIDDEN_PATTERNS:
                self.assertIsNone(pattern.search(text), f"{account}: {code}")
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("admission=false", text)

    def test_no_revenue_or_headcount_figures(self):
        figure = re.compile(r"(?:выручк|персонал|численност)\w*\s*[:—–-]\s*\d", re.IGNORECASE)
        for account, text in self.texts.items():
            self.assertIsNone(figure.search(text), account)

    # --- negative cases on a temporary copy -------------------------------------------------

    def test_email_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "tatneft.md", "## 9. Источники",
                   "Контакт: analyst@example.com\n\n## 9. Источники")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("email_like", codes)

    def test_phone_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "taneco.md", "## 9. Источники",
                   "Телефон: +7 495 000 00 00\n\n## 9. Источники")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("phone_like", codes)

    def test_admitted_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "sibur.md", "## 9. Источники",
                   "У нас admitted модель.\n\n## 9. Источники")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("admission_claim", codes)

    def test_personal_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "rosneft.md", "## 9. Источники",
                   "Контакт: Иван Петрович.\n\n## 9. Источники")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("personal_name", codes)

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "taneco.md").unlink()
            summary = mod.check(research, TARGETS)
            self.assertFalse(summary["valid"])
            self.assertIn("file_missing", {issue["code"] for issue in summary["issues"]})

    def test_extra_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "extra.md").write_text("# лишний файл\n", encoding="utf-8")
            summary = mod.check(research, TARGETS)
            self.assertIn("file_unexpected", {issue["code"] for issue in summary["issues"]})

    def test_unknown_evidence_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "novatek.md", "- evidence_status: PARTIAL",
                   "- evidence_status: CONFIRMED")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("evidence_status", codes)

    def test_evidence_status_must_match_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "lukoil.md", "- evidence_status: PARTIAL",
                   "- evidence_status: VERIFIED")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("evidence_status_mismatch", codes)

    def test_pain_count_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "tatneft.md", "## 3. Buying center",
                   "4. **Лишняя гипотеза боли для теста.** Достаточно длинная строка, "
                   "чтобы пройти проверку длины. `[HYPOTHESIS]`\n\n## 3. Buying center")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("pain_count", codes)

    def test_question_format_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "gazpromneft.md",
                   "1. Какие модели уже пилотируете и что с ними происходит после пилота — отчёт, API или ничего?",
                   "1. Какие модели уже пилотируете и что с ними происходит после пилота.")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("question_format", codes)

    def test_use_case_count_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "surgutneftegas.md", "## 6. Вопросы для discovery",
                   "| 4 | Лишний кейс (`oil_price_scenarios`) | модуль | показ | граница |\n\n"
                   "## 6. Вопросы для discovery")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("use_case_count", codes)

    def test_segment_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "taneco.md", "- segment: refining", "- segment: upstream")
            codes = {issue["code"] for issue in mod.check(research, TARGETS)["issues"]}
            self.assertIn("segment_mismatch", codes)

    # --- CLI and documentation --------------------------------------------------------------

    def test_cli_check_ok(self):
        result = run_cli("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: 10 dossiers", result.stdout)

    def test_cli_json_schema(self):
        result = run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.account_research.v1")
        self.assertEqual(payload["accounts"], 10)
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["policy"]["admission"])
        self.assertEqual(payload["policy"]["sending"], "human_only")
        self.assertEqual(payload["policy"]["contacts"], "none")

    def test_cli_fails_on_broken_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "lukoil.md").unlink()
            result = run_cli("--check", "--dir", str(research))
            self.assertEqual(result.returncode, 2)
            self.assertIn("FAIL", result.stderr)

    def test_summary_is_deterministic(self):
        first = mod.check()
        second = mod.check()
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))
        rendered = mod.render_summary(first)
        self.assertIn("TATNEFT", rendered)
        self.assertIn("PARTIAL=10", rendered)

    def test_readme_and_doc209_describe_policy(self):
        self.assertTrue(README.exists())
        readme = README.read_text(encoding="utf-8")
        for marker in ("VERIFIED", "PARTIAL", "HYPOTHESIS", "admission=false",
                       "не отправ", "python tools/account_research.py --check"):
            self.assertIn(marker, readme)
        self.assertTrue(DOC209.exists())
        doc = DOC209.read_text(encoding="utf-8")
        self.assertIn("Татнефть", doc)
        self.assertIn("ТАНЕКО", doc)
        self.assertIn("RESEARCH_ONLY", doc)


if __name__ == "__main__":
    unittest.main()
