#!/usr/bin/env python3
"""Tests for the marketing CRM pipeline validator and the first-5 outreach package."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "marketing_crm.py"
CRM = ROOT / "marketing" / "crm_pipeline.csv"
TARGETS = ROOT / "marketing" / "target_accounts.json"
OUTREACH = ROOT / "marketing" / "outreach_5.md"
LETTERS = ROOT / "marketing" / "letters"
DOC191 = ROOT / "docs" / "191_Маркетинг_нефтегаз.md"
FIRST_FIVE = ("TATNEFT", "TANECO", "GAZPROMNEFT", "NOVATEK", "IRKUTSK_OIL")
LETTER_FILES = {"TATNEFT": "tatneft.md", "TANECO": "taneco.md", "GAZPROMNEFT": "gazpromneft.md",
                "NOVATEK": "novatek.md", "IRKUTSK_OIL": "irk.md"}

spec = importlib.util.spec_from_file_location("marketing_crm", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT), env=env)


def write_csv(path: Path, rows: list[dict]) -> None:
    lines = [",".join(f'"{field}"' for field in mod.FIELDS)]
    for row in rows:
        lines.append(",".join(f'"{row[field]}"' for field in mod.FIELDS))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class MarketingCrmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.header, cls.rows = mod.read_csv()
        cls.targets = mod.load_targets()
        cls.tiers = mod.expected_tiers(cls.targets)

    # --- happy path -------------------------------------------------------------------------

    def test_pipeline_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.marketing.crm.v1")

    def test_twenty_accounts_and_header(self):
        self.assertEqual(len(self.rows), 20)
        self.assertEqual(self.header, mod.FIELDS)
        self.assertEqual(self.summary["accounts"], 20)

    def test_all_accounts_start_at_new(self):
        self.assertEqual(self.summary["stages"]["NEW"], 20)
        for stage in mod.STAGES:
            if stage != "NEW":
                self.assertEqual(self.summary["stages"][stage], 0)

    def test_tier_counts_match_ranking(self):
        self.assertEqual(self.summary["tiers"], {"A": 5, "B": 6, "C": 9})
        tier_a = {row["id"] for row in self.rows if row["tier"] == "A"}
        self.assertEqual(tier_a, {"TATNEFT", "GAZPROMNEFT", "TANECO", "LUKOIL", "ROSNEFT"})

    def test_tiers_match_scored_tool(self):
        self.assertTrue(self.tiers)
        for row in self.rows:
            self.assertEqual(row["tier"], self.tiers[row["id"]], row["id"])

    def test_ids_match_target_accounts(self):
        self.assertEqual({row["id"] for row in self.rows}, set(self.targets))
        self.assertEqual(len({row["id"] for row in self.rows}), 20)

    def test_next_dates_are_iso_and_ordered(self):
        for row in self.rows:
            self.assertRegex(row["next_date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(mod._check_date(row["next_date"]), row["next_date"])
        dates = [item["next_date"] for item in self.summary["next_actions"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(self.summary["next_date_range"]["earliest"], "2026-11-20")
        self.assertEqual(self.summary["next_date_range"]["latest"], "2026-12-30")

    def test_first_five_have_earliest_dates(self):
        dates = {item["id"]: item["next_date"] for item in self.summary["next_actions"]}
        for account in FIRST_FIVE:
            self.assertIn(account, dates)
        self.assertLessEqual(dates["TATNEFT"], dates["IRKUTSK_OIL"])

    # --- validation: duplicates, stages, dates, count ----------------------------------------

    def test_duplicate_id_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[1]["id"] = rows[0]["id"]
        issues = mod.validate(rows, mod.FIELDS, self.targets, self.tiers)
        codes = {issue["code"] for issue in issues}
        self.assertIn("duplicate_id", codes)

    def test_duplicate_company_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[1]["company"] = rows[0]["company"]
        issues = mod.validate(rows, mod.FIELDS)
        self.assertIn("duplicate_company", {issue["code"] for issue in issues})

    def test_unknown_stage_is_rejected(self):
        for bad in ("QUALIFIED", "new", ""):
            rows = copy.deepcopy(self.rows)
            rows[0]["stage"] = bad
            issues = mod.validate(rows, mod.FIELDS)
            codes = {issue["code"] for issue in issues}
            self.assertTrue(codes & {"stage", "stage_case", "required"}, (bad, codes))

    def test_every_stage_value_is_supported(self):
        for stage in mod.STAGES:
            rows = copy.deepcopy(self.rows)
            rows[0]["stage"] = stage
            issues = mod.validate(rows, mod.FIELDS)
            self.assertNotIn("stage", {issue["code"] for issue in issues}, stage)

    def test_bad_date_formats_are_rejected(self):
        for bad in ("20.11.2026", "2026-13-01", "2026-11-31", "20261120", "2026-1-1"):
            rows = copy.deepcopy(self.rows)
            rows[0]["next_date"] = bad
            issues = mod.validate(rows, mod.FIELDS)
            self.assertIn("date_format", {issue["code"] for issue in issues}, bad)

    def test_next_date_required_for_open_stage(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["stage"] = "CONTACTED"
        rows[0]["next_date"] = ""
        issues = mod.validate(rows, mod.FIELDS)
        self.assertIn("date_required", {issue["code"] for issue in issues})

    def test_closed_stage_may_have_empty_date(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["stage"] = "LOST"
        rows[0]["next_date"] = ""
        issues = mod.validate(rows, mod.FIELDS)
        self.assertNotIn("date_required", {issue["code"] for issue in issues})

    def test_row_count_is_enforced(self):
        rows = copy.deepcopy(self.rows)[:19]
        issues = mod.validate(rows, mod.FIELDS)
        self.assertIn("count", {issue["code"] for issue in issues})

    def test_tier_mismatch_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["tier"] = "C" if rows[0]["tier"] != "C" else "A"
        issues = mod.validate(rows, mod.FIELDS, self.targets, self.tiers)
        self.assertIn("tier_mismatch", {issue["code"] for issue in issues})

    def test_unknown_account_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["id"] = "NOT_A_TARGET"
        issues = mod.validate(rows, mod.FIELDS, self.targets, self.tiers)
        codes = {issue["code"] for issue in issues}
        self.assertIn("unknown_account", codes)
        self.assertIn("missing_account", codes)

    def test_header_mismatch_is_rejected(self):
        issues = mod.validate(copy.deepcopy(self.rows), ("id", "company"))
        self.assertIn("header_mismatch", {issue["code"] for issue in issues})

    # --- no invented contacts ----------------------------------------------------------------

    def test_forbidden_contact_patterns_are_blocked(self):
        at_sign = chr(64)
        rows = copy.deepcopy(self.rows)
        rows[0]["notes"] = f"писать на ivan.petrov{at_sign}example.ru"
        issues = mod.validate(rows, mod.FIELDS)
        self.assertIn("email-like value", {issue["code"] for issue in issues})
        rows[0]["notes"] = "звонить +7 495 000 00 00"
        issues = mod.validate(rows, mod.FIELDS)
        self.assertIn("phone-like value", {issue["code"] for issue in issues})

    def test_no_contacts_in_pipeline_file(self):
        text = CRM.read_text(encoding="utf-8")
        self.assertNotIn("@", text)
        self.assertIsNone(re.search(r"\+7", text))
        self.assertNotIn("tel:", text.lower())
        self.assertNotIn("mailto:", text.lower())

    def test_no_contacts_in_outreach_files(self):
        texts = [OUTREACH.read_text(encoding="utf-8")]
        for name in LETTER_FILES.values():
            texts.append((LETTERS / name).read_text(encoding="utf-8"))
        for text in texts:
            self.assertNotIn("@", text)
            self.assertIsNone(re.search(r"\+7\s?\(?\d", text))

    # --- determinism & CLI -------------------------------------------------------------------

    def test_summary_is_deterministic(self):
        first = json.dumps(mod.check(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(mod.check(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_cli_check_exits_zero(self):
        done = run_cli("--check")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("OK: 20 accounts", done.stdout)

    def test_cli_check_exits_two_on_bad_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "crm.csv"
            rows = copy.deepcopy(self.rows)
            rows[0]["next_date"] = "20/11/2026"
            write_csv(path, rows)
            done = run_cli("--check", "--path", str(path))
            self.assertEqual(done.returncode, 2)
            self.assertIn("FAIL", done.stderr)
            self.assertIn("date_format", done.stderr)

    def test_cli_json_schema_and_counts(self):
        done = run_cli("--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["schema"], "amfe.marketing.crm.v1")
        self.assertEqual(payload["accounts"], 20)
        self.assertTrue(payload["valid"])
        self.assertEqual(len(payload["next_actions"]), 20)
        self.assertEqual(payload["policy"]["admission"], False)
        self.assertEqual(payload["policy"]["sending"], "human_only")

    def test_cli_summary_renders_stages(self):
        done = run_cli("--summary")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("AMFE CRM: 20", done.stdout)
        self.assertIn("NEW=20", done.stdout)
        self.assertIn("Tier: A=5, B=6, C=9", done.stdout)

    def test_cli_missing_file_is_fail_closed(self):
        done = run_cli("--check", "--path", str(ROOT / "marketing" / "no_such_file.csv"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("BLOCKED_INPUT", done.stderr)

    def test_tool_does_not_write_files(self):
        before = sorted(path.name for path in ROOT.joinpath("marketing").iterdir())
        done = run_cli("--check")
        self.assertEqual(done.returncode, 0)
        after = sorted(path.name for path in ROOT.joinpath("marketing").iterdir())
        self.assertEqual(before, after)

    # --- package consistency -----------------------------------------------------------------

    def test_letters_exist_for_first_five(self):
        for account, name in LETTER_FILES.items():
            path = LETTERS / name
            self.assertTrue(path.exists(), account)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("@", text, account)
            for placeholder in ("[Имя]", "[почта]", "[должность]"):
                self.assertIn(placeholder, text, (account, placeholder))
            self.assertIn("Не отправлено", text, account)

    def test_outreach_covers_five_accounts(self):
        text = OUTREACH.read_text(encoding="utf-8")
        for account in FIRST_FIVE:
            self.assertIn(account, text)
        for section in ("Письмо 1", "Follow-up 2", "LinkedIn", "Созвон 20 минут", "Вложения",
                        "Критерий успеха"):
            self.assertIn(section, text, section)
        self.assertIn("RESEARCH_ONLY", text)
        self.assertIn("ничего", text)

    def test_outreach_documents_sending_is_blocked(self):
        text = OUTREACH.read_text(encoding="utf-8")
        self.assertIn("Почтовый канал", text)
        self.assertIn("Адреса и имена", text)
        self.assertIn("не отправлено", text.lower())
        self.assertIn("Никаких выдуманных контактов", text)
        self.assertIn("Никаких выдуманных фактов", text)

    def test_outreach_copies_letter_bodies_verbatim(self):
        outreach = OUTREACH.read_text(encoding="utf-8")

        def normalise(text: str) -> str:
            text = re.sub(r"^>\s?", "", text, flags=re.M)
            text = re.sub(r"[`*]", "", text)
            return re.sub(r"\s+", " ", text).strip()

        haystack = normalise(outreach)
        for account, name in LETTER_FILES.items():
            lines = (LETTERS / name).read_text(encoding="utf-8").splitlines()
            blocks = []
            current: list[str] = []
            for line in lines:
                if line.startswith(">"):
                    current.append(line[1:].strip())
                elif current:
                    blocks.append(" ".join(current))
                    current = []
            if current:
                blocks.append(" ".join(current))
            self.assertEqual(len(blocks), 3, account)  # письмо 1, follow-up, LinkedIn
            for block in blocks:
                self.assertIn(normalise(block), haystack, (account, block[:60]))

    def test_docs191_has_first_five_section(self):
        text = DOC191.read_text(encoding="utf-8")
        self.assertIn("Первые 5 писем", text)
        self.assertIn("marketing/outreach_5.md", text)
        self.assertIn("letters/", text)
        self.assertIn("crm_pipeline.csv", text)
        self.assertIn("требуют проверки", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
