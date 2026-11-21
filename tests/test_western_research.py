#!/usr/bin/env python3
"""Tests for the western-account research dossiers and their validator."""
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
TOOL = ROOT / "tools" / "western_research.py"
RESEARCH = ROOT / "marketing" / "western_research"
ACCOUNTS = RESEARCH / "accounts.json"
README = RESEARCH / "README.md"
DOC215 = ROOT / "docs" / "215_Западные_заказчики.md"
REQUIRED = ("EXXONMOBIL", "CHEVRON", "SHELL", "BP", "TOTALENERGIES",
            "EQUINOR", "CONOCOPHILLIPS", "ENI", "SLB", "BAKERHUGHES")
ANCHOR = "## 9. Источники и границы"

spec = importlib.util.spec_from_file_location("western_research", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT), env=env)


def copy_package(tmp: str) -> Path:
    destination = Path(tmp) / "western_research"
    shutil.copytree(RESEARCH, destination)
    return destination


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"fixture text not found in {path.name}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class WesternResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.accounts = mod.load_accounts(ACCOUNTS)
        cls.texts = {account: (RESEARCH / f"{account.lower()}.md").read_text(encoding="utf-8")
                     for account in REQUIRED}

    # --- happy path -------------------------------------------------------------------------

    def test_package_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.western_research.v1")

    def test_ten_dossiers(self):
        self.assertEqual(self.summary["accounts"], 10)
        found = sorted(path.name for path in RESEARCH.glob("*.md")
                       if path.name.casefold() != "readme.md")
        self.assertEqual(found, sorted(f"{account.lower()}.md" for account in REQUIRED))
        self.assertEqual([row["id"] for row in self.accounts], list(REQUIRED))

    def test_headers_match_registry(self):
        by_id = {row["id"]: row for row in self.accounts}
        for detail in self.summary["dossiers"]:
            row = by_id[detail["id"]]
            for field in ("company", "segment", "region", "hq"):
                self.assertEqual(detail[field], row[field], f"{detail['id']}: {field}")

    def test_evidence_status_is_allowed_and_consistent(self):
        self.assertEqual(self.summary["evidence"]["VERIFIED"], 0)
        self.assertEqual(self.summary["evidence"]["PARTIAL"], 10)
        for account, text in self.texts.items():
            parsed = mod.parse_dossier(text)
            status = parsed["fields"]["evidence_status"]
            self.assertIn(status, mod.ALLOWED_STATUSES, account)
            tags = set(mod.TAG_RE.findall(parsed["sections"][1]))
            strongest = max(tags, key=lambda tag: mod.EVIDENCE_RANK[tag])
            self.assertEqual(mod.EVIDENCE_RANK[status], mod.EVIDENCE_RANK[strongest], account)

    def test_partial_dossiers_cite_a_source(self):
        for account, text in self.texts.items():
            urls = mod.URL_RE.findall(text)
            self.assertTrue(urls, account)
            self.assertIn("en.wikipedia.org", text, account)
            self.assertIn("2026-11-21", text, account)

    def test_counts_per_dossier(self):
        for detail in self.summary["dossiers"]:
            self.assertEqual(detail["pains"], 3, detail["id"])
            self.assertEqual(detail["questions"], 5, detail["id"])
            self.assertEqual(detail["use_case_rows"], 3, detail["id"])
            self.assertGreaterEqual(detail["buying_center_roles"], 4, detail["id"])
            self.assertGreaterEqual(detail["competitors"], 4, detail["id"])
            self.assertGreaterEqual(detail["verify_items"], 5, detail["id"])

    def test_all_pains_are_hypotheses(self):
        for account, text in self.texts.items():
            pains = mod.numbered_items(mod.parse_dossier(text)["sections"][2])
            self.assertEqual(len(pains), 3, account)
            for index, pain in enumerate(pains, start=1):
                self.assertIn("[HYPOTHESIS]", pain, f"{account} pain {index}")

    def test_four_trigger_classes(self):
        self.assertEqual(self.summary["trigger_coverage"],
                         {stem: 10 for stem in mod.TRIGGER_STEMS})

    def test_use_cases_reference_known_modules(self):
        for detail in self.summary["dossiers"]:
            self.assertGreaterEqual(len(detail["modules"]), 2, detail["id"])
            self.assertTrue(set(detail["modules"]) <= mod.MODULE_IDS, detail["id"])
        self.assertIn("oil_price_scenarios", self.summary["use_case_coverage"])
        self.assertIn("procurement_tco", self.summary["use_case_coverage"])

    # --- policy: no contacts, no figures, no admitted model ----------------------------------

    def test_no_contacts_or_financial_figures(self):
        for account, text in self.texts.items():
            for pattern, code, _ in mod.FORBIDDEN_PATTERNS:
                self.assertIsNone(pattern.search(text), f"{account}: {code}")
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("admission=false", text)

    # --- negative cases on a temporary copy -------------------------------------------------

    def test_email_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "shell.md", ANCHOR, "Контакт: analyst@example.com\n\n" + ANCHOR)
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("email_like", codes)

    def test_phone_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "bp.md", ANCHOR, "Телефон: +1 713 555 0100\n\n" + ANCHOR)
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("phone_like", codes)

    def test_revenue_figure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "eni.md", ANCHOR, "Revenue: $12 billion\n\n" + ANCHOR)
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("financial_figure", codes)

    def test_admitted_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "slb.md", ANCHOR, "У нас admitted модель.\n\n" + ANCHOR)
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("admission_claim", codes)

    def test_personal_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "chevron.md", ANCHOR, "Контакт: Иван Петрович.\n\n" + ANCHOR)
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("personal_name", codes)

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "equinor.md").unlink()
            summary = mod.check(research, ACCOUNTS)
            self.assertFalse(summary["valid"])
            self.assertIn("file_missing", {issue["code"] for issue in summary["issues"]})

    def test_extra_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "extra.md").write_text("# лишний файл\n", encoding="utf-8")
            summary = mod.check(research, ACCOUNTS)
            self.assertIn("file_unexpected", {issue["code"] for issue in summary["issues"]})

    def test_unknown_evidence_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "totalenergies.md", "- evidence_status: PARTIAL",
                   "- evidence_status: CONFIRMED")
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("evidence_status", codes)

    def test_verified_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "conocophillips.md", "- evidence_status: PARTIAL",
                   "- evidence_status: VERIFIED")
            issues = mod.check(research, ACCOUNTS)["issues"]
            self.assertIn("evidence_status", {issue["code"] for issue in issues})
            self.assertTrue(any("first-party" in issue["message"] for issue in issues))

    def test_status_must_match_strongest_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "bakerhughes.md", "- evidence_status: PARTIAL",
                   "- evidence_status: HYPOTHESIS")
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("evidence_status_mismatch", codes)

    def test_partial_without_source_is_rejected(self):
        text = (RESEARCH / "shell.md").read_text(encoding="utf-8")
        stripped = mod.URL_RE.sub("источник не открывался", text)
        codes = {issue["code"] for issue in mod.validate_text(stripped, "SHELL")}
        self.assertIn("source_url", codes)

    def test_not_fetched_with_source_is_rejected(self):
        text = (RESEARCH / "shell.md").read_text(encoding="utf-8")
        text = text.replace("- evidence_status: PARTIAL", "- evidence_status: NOT_FETCHED", 1)
        text = text.replace("[PARTIAL]", "[NOT_FETCHED]")
        codes = {issue["code"] for issue in mod.validate_text(text, "SHELL")}
        self.assertIn("source_url_mismatch", codes)

    def test_pain_count_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "exxonmobil.md", "## 3. Buying center (роли)",
                   "4. **Лишняя гипотеза боли для теста.** Достаточно длинная строка, "
                   "чтобы пройти проверку длины. `[HYPOTHESIS]`\n\n## 3. Buying center (роли)")
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("pain_count", codes)

    def test_question_format_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            text = (research / "shell.md").read_text(encoding="utf-8")
            question = mod.numbered_items(mod.parse_dossier(text)["sections"][6])[0]
            mutate(research / "shell.md", question, question.rstrip("?"))
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("question_format", codes)

    def test_use_case_count_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "slb.md", "## 6. Вопросы для discovery (5)",
                   "| 4 | Лишний кейс (`oil_price_scenarios`) | модуль | показ | граница |\n\n"
                   "## 6. Вопросы для discovery (5)")
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("use_case_count", codes)

    def test_registry_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            mutate(research / "shell.md", "- company: Shell", "- company: Shell plc")
            codes = {issue["code"] for issue in mod.check(research, ACCOUNTS)["issues"]}
            self.assertIn("company_mismatch", codes)

    def test_missing_registry_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            with self.assertRaises(mod.WesternResearchError):
                mod.check(research, Path(tmp) / "absent.json")

    # --- CLI and documentation --------------------------------------------------------------

    def test_cli_check_ok(self):
        result = run_cli("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: 10 dossiers", result.stdout)

    def test_cli_json_schema(self):
        result = run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.western_research.v1")
        self.assertEqual(payload["accounts"], 10)
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["policy"]["admission"])
        self.assertEqual(payload["policy"]["sending"], "human_only")
        self.assertEqual(payload["policy"]["contacts"], "none")
        self.assertEqual(payload["policy"]["financials"], "none")

    def test_cli_fails_on_broken_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            research = copy_package(tmp)
            (research / "bp.md").unlink()
            result = run_cli("--check", "--dir", str(research), "--accounts", str(ACCOUNTS))
            self.assertEqual(result.returncode, 2)
            self.assertIn("FAIL", result.stderr)

    def test_summary_is_deterministic(self):
        first = mod.check()
        second = mod.check()
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))
        rendered = mod.render_summary(first)
        self.assertIn("EXXONMOBIL", rendered)
        self.assertIn("PARTIAL=10", rendered)

    def test_readme_and_doc215_describe_policy(self):
        self.assertTrue(README.exists())
        readme = README.read_text(encoding="utf-8")
        for marker in ("PARTIAL", "HYPOTHESIS", "NOT_FETCHED", "VERIFIED", "admission=false",
                       "не отправ", "lawful OSINT", "закрытые базы", "персональн",
                       "python tools/western_research.py --check"):
            self.assertIn(marker, readme)
        self.assertTrue(DOC215.exists())
        doc = DOC215.read_text(encoding="utf-8")
        for marker in ("Топ-3", "RESEARCH_ONLY", "Equinor", "Shell", "ConocoPhillips",
                       "пробел"):
            self.assertIn(marker, doc)


if __name__ == "__main__":
    unittest.main()
