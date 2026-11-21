#!/usr/bin/env python3
"""Tests for the market-intelligence knowledge base (schema, evidence, freshness, tools)."""
from __future__ import annotations

import hashlib
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
TOOL = ROOT / "tools" / "kb.py"
KB = ROOT / "knowledge_base"
SEED = KB / "entries" / "seed.jsonl"
SOURCES = KB / "sources.json"
SCHEMA = KB / "schema.json"
ACCOUNTS = ROOT / "marketing" / "target_accounts.json"
DOC = ROOT / "docs" / "213_База_знаний.md"

spec = importlib.util.spec_from_file_location("kb", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_kb(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, cwd=str(cwd), env=env)


def copy_kb(tmp: str) -> Path:
    destination = Path(tmp) / "knowledge_base"
    shutil.copytree(KB, destination)
    return destination


def read_seed(path: Path = SEED) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                    encoding="utf-8", newline="\n")


def sample_entry(**overrides) -> dict:
    entry = mod.build_entry(
        kind="method", title="Проверка конвейера базы знаний",
        summary="Техническая запись для проверки add/merge, без рыночных утверждений.",
        author_role="evidence-qa", as_of="2026-11-21", evidence="HYPOTHESIS", grade="C",
        sources=["SRC-INTERNAL-CARD"], licence="INTERNAL", tags=["method", "test"],
        links=["knowledge_base/schema.json"], notes="Техническая запись для теста конвейера.",
        entry_id="KB-0020")
    entry.update(overrides)
    return entry


class KnowledgeBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.entries = read_seed()
        cls.sources_doc = json.loads(SOURCES.read_text(encoding="utf-8"))
        cls.sources = {source["id"]: source for source in cls.sources_doc["sources"]}
        cls.accounts = mod.load_accounts(ACCOUNTS)

    # --- happy path -------------------------------------------------------------------------

    def test_kb_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.kb.validate.v1")

    def test_schema_document(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["version"], "1.0.0")
        self.assertEqual(schema["entry"]["properties"]["schema"]["const"], "amfe.kb.entry.v1")
        self.assertEqual(tuple(schema["enums"]["kind"]), mod.KINDS)
        self.assertEqual(tuple(schema["enums"]["evidence_status"]), mod.STATUSES)
        self.assertEqual(tuple(schema["enums"]["licence"]), mod.LICENCES)
        self.assertGreaterEqual(len(schema["x-rules"]), 10)

    def test_seed_size_and_kinds(self):
        self.assertGreaterEqual(len(self.entries), 15)
        self.assertLessEqual(len(self.entries), 20)
        kinds = [entry["kind"] for entry in self.entries]
        for kind in ("segment", "source", "case_template", "target_account", "method"):
            self.assertIn(kind, kinds)
        self.assertEqual(len([k for k in kinds if k == "segment"]), 6)
        self.assertEqual(len([k for k in kinds if k == "target_account"]), 4)

    def test_ids_unique_ascending_and_well_formed(self):
        ids = [entry["id"] for entry in self.entries]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        for entry_id in ids:
            self.assertRegex(entry_id, r"^KB-\d{4}$")

    def test_required_fields_present(self):
        for entry in self.entries:
            for field in mod.REQUIRED_ENTRY_FIELDS:
                self.assertIn(field, entry, entry.get("id"))
            self.assertNotIn("__where", entry)

    def test_review_due_matches_ttl(self):
        from datetime import date, timedelta
        for entry in self.entries:
            expected = (date.fromisoformat(entry["as_of"])
                        + timedelta(days=entry["ttl_days"])).isoformat()
            self.assertEqual(entry["review_due"], expected, entry["id"])

    def test_evidence_scale_is_honest(self):
        statuses = {entry["evidence_status"] for entry in self.entries}
        self.assertTrue(statuses <= {"HYPOTHESIS", "NOT_FETCHED", "PARTIAL", "VERIFIED"})
        self.assertNotIn("VERIFIED", statuses)
        for entry in self.entries:
            if entry["evidence_status"] in ("HYPOTHESIS", "NOT_FETCHED"):
                self.assertIn(entry["evidence_grade"], (None, "C"), entry["id"])
            if entry["evidence_grade"] == "A":
                self.assertEqual(entry["evidence_status"], "VERIFIED", entry["id"])

    def test_no_figures_without_sources(self):
        for entry in self.entries:
            if entry["evidence_status"] not in ("HYPOTHESIS", "NOT_FETCHED"):
                continue
            text = " ".join(str(entry.get(key) or "") for key in ("title", "summary", "notes"))
            self.assertIsNone(mod.FIGURE_RE.search(text), f"{entry['id']}: {text}")

    def test_no_admission_wording(self):
        for entry in self.entries:
            text = " ".join(str(entry.get(key) or "") for key in ("title", "summary", "notes"))
            self.assertIsNone(mod.ADMISSION_RE.search(text), entry["id"])

    def test_sources_resolve_and_are_honest(self):
        for entry in self.entries:
            for source_id in entry["source_ids"]:
                self.assertIn(source_id, self.sources, entry["id"])
        self.assertEqual(len(self.sources), 16)
        for source in self.sources.values():
            self.assertIn(source["access"], mod.ACCESS)
            self.assertIn(source["class"], mod.SOURCE_CLASSES)
            self.assertIs(source["terms_checked"], False, source["id"])
            self.assertIsNone(source["terms_hash"], source["id"])
            self.assertIs(source["url_checked"], False, source["id"])
            self.assertIsNone(source["retrieved_at"], source["id"])

    def test_licence_rules(self):
        for entry in self.entries:
            if entry["licence"] == "NONE":
                self.assertEqual(entry["evidence_status"], "NOT_FETCHED", entry["id"])
            if entry["licence"] in mod.OPEN_LICENCES:
                for source_id in entry["source_ids"]:
                    self.assertNotIn(self.sources[source_id]["licence"],
                                     mod.RESTRICTED_LICENCES, f"{entry['id']} {source_id}")

    def test_links_exist(self):
        for entry in self.entries:
            for link in entry["links"]:
                self.assertTrue((ROOT / link).exists(), f"{entry['id']}: {link}")

    def test_target_accounts_are_real(self):
        accounts = [entry for entry in self.entries if entry["kind"] == "target_account"]
        self.assertTrue(accounts)
        for entry in accounts:
            self.assertIn(entry["account_id"], self.accounts)
        for entry in self.entries:
            if entry["kind"] != "target_account":
                self.assertIn(entry.get("account_id"), (None, ""), entry["id"])

    def test_not_fetched_has_a_reason(self):
        for entry in self.entries:
            if entry["evidence_status"] == "NOT_FETCHED":
                self.assertGreaterEqual(len(entry["notes"]), 20, entry["id"])

    # --- CLI: validate / search -------------------------------------------------------------

    def test_check_cli_exit_zero(self):
        result = run_kb("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_validate_json_contract(self):
        result = run_kb("--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "amfe.kb.validate.v1")
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["entries"], len(self.entries))
        self.assertEqual(payload["policy"]["network"], "disabled")
        self.assertEqual(payload["counts"]["total"], len(self.entries))

    def test_search_by_kind_and_evidence(self):
        result = run_kb("search", "TCO", "--kind", "case_template")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KB-0012", result.stdout)
        payload = json.loads(run_kb("search", "", "--evidence", "NOT_FETCHED", "--json").stdout)
        self.assertEqual(payload["count"], 5)
        for row in payload["results"]:
            self.assertEqual(row["evidence_status"], "NOT_FETCHED")

    def test_search_filters_and_stale(self):
        by_tag = mod.search_entries(self.entries, "", tag="pilot")
        self.assertEqual([entry["id"] for entry in by_tag], ["KB-0015"])
        result = run_kb("search", "", "--stale")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--today", result.stderr)
        stale = mod.search_entries(self.entries, "", today=mod._parse_date("2030-01-01"),
                                  only_stale=True)
        self.assertEqual(len(stale), len(self.entries))

    # --- CLI: add / merge -------------------------------------------------------------------

    def test_add_then_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            result = run_kb("--kb", str(kb), "add", "--kind", "method",
                            "--title", "Проверка конвейера базы знаний",
                            "--summary", "Техническая запись для проверки add и merge.",
                            "--author-role", "evidence-qa", "--as-of", "2026-11-21",
                            "--tags", "method,test", "--link", "knowledge_base/schema.json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("KB-0020", result.stdout)
            self.assertTrue((kb / "inbox" / "KB-0020.jsonl").is_file())
            self.assertEqual(run_kb("--kb", str(kb), "--check").returncode, 0)
            merged = run_kb("--kb", str(kb), "merge")
            self.assertEqual(merged.returncode, 0, merged.stderr)
            self.assertIn("merged 1", merged.stdout)
            self.assertEqual(run_kb("--kb", str(kb), "--check").returncode, 0)
            payload = json.loads(run_kb("--kb", str(kb), "--json", "--check").stdout)
            self.assertEqual(payload["entries"], len(self.entries) + 1)
            self.assertTrue((kb / "inbox" / "merged" / "KB-0020.jsonl").is_file())

    def test_add_rejects_duplicate_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            result = run_kb("--kb", str(kb), "add", "--kind", "method", "--title",
                            "Дубль идентификатора", "--summary",
                            "Запись с уже занятым идентификатором для проверки отказа.",
                            "--author-role", "evidence-qa", "--as-of", "2026-11-21",
                            "--tags", "method", "--link", "knowledge_base/schema.json",
                            "--id", "KB-0001")
            self.assertEqual(result.returncode, 2)
            self.assertIn("already exists", result.stderr)
            self.assertFalse((kb / "inbox" / "KB-0001.jsonl").exists())

    def test_add_rejects_figure_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            result = run_kb("--kb", str(kb), "add", "--kind", "segment", "--title",
                            "Цифра без источника", "--summary",
                            "Выручка выросла на 12% и это утверждение без источника.",
                            "--author-role", "upstream", "--as-of", "2026-11-21",
                            "--tags", "segment", "--link", "knowledge_base/schema.json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("figure_without_source", result.stderr)
            self.assertEqual(sorted(path.name for path in (kb / "inbox").glob("*.jsonl")), [])

    def test_add_rejects_unknown_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            result = run_kb("--kb", str(kb), "add", "--kind", "source", "--title",
                            "Неизвестный источник", "--summary",
                            "Запись ссылается на незарегистрированный источник.",
                            "--author-role", "osint-scout", "--as-of", "2026-11-21",
                            "--source", "SRC-NOPE", "--tags", "source",
                            "--link", "knowledge_base/sources.json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("source_unknown", result.stderr)

    def test_merge_rejects_invalid_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            bad = sample_entry(summary="Выручка выросла на 12% без источника и даты.")
            write_jsonl(kb / "inbox" / "KB-0020.jsonl", [bad])
            before = (kb / "entries" / "seed.jsonl").read_bytes()
            result = run_kb("--kb", str(kb), "merge")
            self.assertEqual(result.returncode, 2)
            self.assertIn("figure_without_source", result.stderr)
            self.assertEqual((kb / "entries" / "seed.jsonl").read_bytes(), before)
            self.assertTrue((kb / "inbox" / "KB-0020.jsonl").exists())

    # --- CLI: index / export ----------------------------------------------------------------

    def test_index_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            self.assertEqual(run_kb("--kb", str(kb), "index", "--today", "2026-11-21").returncode, 0)
            first_json = (kb / "index.json").read_bytes()
            first_md = (kb / "index.md").read_bytes()
            self.assertEqual(run_kb("--kb", str(kb), "index", "--today", "2026-11-21").returncode, 0)
            self.assertEqual((kb / "index.json").read_bytes(), first_json)
            self.assertEqual((kb / "index.md").read_bytes(), first_md)

    def test_index_freshness_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            run_kb("--kb", str(kb), "index", "--today", "2026-11-21")
            index = json.loads((kb / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["schema"], "amfe.kb.index.v1")
            self.assertEqual(index["freshness"]["fresh"], len(self.entries))
            self.assertEqual(index["freshness"]["stale"], 0)
            self.assertEqual(index["generated_for"], "2026-11-21")
            run_kb("--kb", str(kb), "index", "--today", "2030-01-01")
            index = json.loads((kb / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["freshness"]["stale"], len(self.entries))
            self.assertEqual(index["freshness"]["fresh"], 0)

    def test_index_without_today_marks_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            self.assertEqual(run_kb("--kb", str(kb), "index").returncode, 0)
            index = json.loads((kb / "index.json").read_text(encoding="utf-8"))
            self.assertIsNone(index["generated_for"])
            self.assertEqual(index["freshness"]["UNKNOWN"], len(self.entries))

    def test_export_json_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = mod.export(KB, "json")
            second = mod.export(KB, "json")
            self.assertEqual(first, second)
            payload = json.loads(first)
            self.assertEqual(payload["schema"], "amfe.kb.export.v1")
            self.assertEqual(len(payload["entries"]), len(self.entries))
            self.assertEqual(len(payload["sources"]), 16)
            markdown = mod.export(KB, "md")
            self.assertIn("# Экспорт базы знаний AMFE", markdown)
            self.assertIn("KB-0001", markdown)
            self.assertIn("## Источники", markdown)
            out = Path(tmp) / "kb.json"
            result = run_kb("export", "--format", "json", "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), first)

    # --- guardrails -------------------------------------------------------------------------

    def test_broken_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = copy_kb(tmp)
            records = read_seed(kb / "entries" / "seed.jsonl")
            records[0]["review_due"] = "2099-01-01"
            write_jsonl(kb / "entries" / "seed.jsonl", records)
            result = run_kb("--kb", str(kb), "--check")
            self.assertEqual(result.returncode, 2)
            self.assertIn("review_due_mismatch", result.stderr)

    def test_tool_has_no_network_and_no_clock(self):
        text = TOOL.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import socket", "http.client",
                          "datetime.now", "date.today", "time.time", "urlopen"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_check_does_not_write(self):
        before_seed = hashlib.sha256(SEED.read_bytes()).hexdigest()
        before_inbox = sorted(path.name for path in (KB / "inbox").glob("*.jsonl"))
        result = run_kb("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(), before_seed)
        self.assertEqual(sorted(path.name for path in (KB / "inbox").glob("*.jsonl")), before_inbox)

    def test_docs_describe_the_base(self):
        self.assertTrue(DOC.is_file())
        text = DOC.read_text(encoding="utf-8")
        for marker in ("schema.json", "seed.jsonl", "sources.json", "HYPOTHESIS", "NOT_FETCHED",
                       "RESEARCH_ONLY"):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
