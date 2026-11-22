#!/usr/bin/env python3
"""Tests for the AMFE cross-project knowledge archive and tools/archivist.py."""
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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "archivist.py"
ARCHIVE = ROOT / "knowledge_archive"
SEED = ARCHIVE / "entries" / "seed.jsonl"
SCHEMA = ARCHIVE / "schema.json"
DOC = ROOT / "docs" / "223_Архивариус_и_межпроектная_база.md"

spec = importlib.util.spec_from_file_location("archivist", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run_tool(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180, cwd=str(cwd), env=env)


def copy_archive(tmp: str) -> Path:
    destination = Path(tmp) / "knowledge_archive"
    shutil.copytree(ARCHIVE, destination)
    return destination


def read_seed(path: Path = SEED) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True)
                              for record in records) + "\n", encoding="utf-8", newline="\n")


def tree_digest(root: Path) -> dict:
    digest = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def provenance(origin: str = "docs/223_Архивариус_и_межпроектная_база.md",
               origin_type: str = "INTERNAL_DOC", retrieved_at: str | None = "2026-11-22") -> dict:
    return {"origin": origin, "origin_type": origin_type, "collected_by": "archivist",
            "collected_at": "2026-11-22", "method": "MANUAL_READ",
            "retrieved_at": retrieved_at, "sha256": None}


def sample_entry(**overrides) -> dict:
    entry_id = overrides.pop("entry_id", "AR-0900")
    entry = mod.build_entry(
        kind="method", namespace="global", title="Тестовая запись архива",
        summary="Техническая запись для проверки правил архива, без рыночных утверждений.",
        owner_role="archivist", audience=["all"], as_of="2026-11-22",
        evidence="HYPOTHESIS", grade="C", licence="INTERNAL", access="PROJECT",
        provenance=provenance(), tags=["method", "test"],
        links=["knowledge_archive/schema.json"], notes="Техническая запись для теста.",
        entry_id=entry_id)
    entry.update(overrides)
    return entry


def add_args(*extra: str) -> list[str]:
    base = [
        "add", "--kind", "method", "--namespace", "global",
        "--title", "Запись из CLI для теста", "--summary",
        "Техническая запись, созданная из командной строки для проверки конвейера.",
        "--owner-role", "archivist", "--audience", "all", "--as-of", "2026-11-22",
        "--origin", "docs/223_Архивариус_и_межпроектная_база.md",
        "--origin-type", "INTERNAL_DOC", "--collected-by", "archivist",
        "--collected-at", "2026-11-22", "--method", "MANUAL_READ",
        "--retrieved-at", "2026-11-22", "--tag", "test",
        "--link", "knowledge_archive/schema.json",
    ]
    return base + list(extra)


class ArchiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = mod.check()
        cls.entries = read_seed()
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.namespaces = mod.load_namespaces(cls.schema)
        cls.roles = mod.load_roles(cls.schema)

    # --- happy path -------------------------------------------------------------------------

    def test_archive_is_valid(self):
        self.assertTrue(self.summary["valid"], self.summary["issues"])
        self.assertEqual(self.summary["errors"], 0)
        self.assertEqual(self.summary["schema"], "amfe.archive.validate.v1")
        self.assertFalse(self.summary["policy"]["admission"])

    def test_schema_document(self):
        self.assertEqual(self.schema["$id"], "amfe.archive.schema.v1")
        self.assertEqual(self.schema["version"], "1.0.0")
        self.assertEqual(self.schema["entry"]["properties"]["schema"]["const"],
                         "amfe.archive.entry.v1")
        self.assertEqual(tuple(self.schema["enums"]["kind"]), mod.KINDS)
        self.assertEqual(tuple(self.schema["enums"]["evidence_status"]), mod.STATUSES)
        self.assertEqual(tuple(self.schema["enums"]["licence"]), mod.LICENCES)
        self.assertEqual(tuple(self.schema["enums"]["access"]), mod.ACCESS_LEVELS)
        self.assertEqual(tuple(self.schema["enums"]["origin_type"]), mod.ORIGIN_TYPES)
        self.assertEqual(tuple(self.schema["enums"]["retention"]), mod.RETENTIONS)
        self.assertGreaterEqual(len(self.schema["x-rules"]), 10)

    def test_seed_size_and_coverage(self):
        self.assertGreaterEqual(len(self.entries), 10)
        self.assertLessEqual(len(self.entries), 15)
        kinds = {entry["kind"] for entry in self.entries}
        self.assertEqual(kinds, set(mod.KINDS))
        namespaces = {entry["namespace"] for entry in self.entries}
        for expected in ("global", "osint", "legal", "cases", "lead", "amfe"):
            self.assertIn(expected, namespaces)

    def test_ids_unique_ascending_and_well_formed(self):
        ids = [entry["id"] for entry in self.entries]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        for entry_id in ids:
            self.assertRegex(entry_id, r"^AR-\d{4}$")

    def test_required_fields_present(self):
        for entry in self.entries:
            for field in mod.REQUIRED_ENTRY_FIELDS:
                self.assertIn(field, entry, entry.get("id"))
            self.assertNotIn("__where", entry)

    def test_review_due_matches_ttl(self):
        for entry in self.entries:
            expected = (date.fromisoformat(entry["as_of"])
                        + timedelta(days=entry["ttl_days"])).isoformat()
            self.assertEqual(entry["review_due"], expected, entry["id"])

    def test_provenance_is_complete_and_checkable(self):
        for entry in self.entries:
            provenance = entry["provenance"]
            for field in mod.REQUIRED_PROVENANCE_FIELDS:
                self.assertIn(field, provenance, entry["id"])
            self.assertIn(provenance["origin_type"], mod.ORIGIN_TYPES, entry["id"])
            self.assertIn(provenance["method"], mod.METHODS, entry["id"])
            self.assertIn(provenance["collected_by"], self.roles, entry["id"])
            self.assertLessEqual(provenance["collected_at"], entry["as_of"], entry["id"])
            if provenance["origin_type"] == "INTERNAL_DOC":
                self.assertTrue((ROOT / provenance["origin"]).exists(), entry["id"])
            else:
                self.assertTrue(provenance["origin"].startswith("https://"), entry["id"])

    def test_evidence_scale_is_honest(self):
        statuses = {entry["evidence_status"] for entry in self.entries}
        self.assertNotIn("VERIFIED", statuses)
        for entry in self.entries:
            if entry["evidence_status"] in ("HYPOTHESIS", "NOT_FETCHED"):
                self.assertIn(entry["evidence_grade"], (None, "C"), entry["id"])
            if entry["evidence_grade"] == "A":
                self.assertEqual(entry["evidence_status"], "VERIFIED", entry["id"])
            if entry["evidence_status"] == "NOT_FETCHED":
                self.assertEqual(entry["licence"], "NONE", entry["id"])
                self.assertIsNone(entry["provenance"]["retrieved_at"], entry["id"])
                self.assertGreaterEqual(len(entry["notes"]), 20, entry["id"])

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

    def test_licence_and_access_matrix(self):
        for entry in self.entries:
            if entry["access"] == "RESTRICTED":
                self.assertNotIn(entry["licence"], mod.OPEN_LICENCES, entry["id"])
            if entry["access"] == "PUBLIC":
                self.assertNotIn(entry["licence"], mod.RESTRICTED_LICENCES, entry["id"])
            if entry["licence"] == "NONE":
                self.assertEqual(entry["evidence_status"], "NOT_FETCHED", entry["id"])
            if entry["retention"] == "LEGAL_HOLD":
                self.assertFalse(entry["archived"], entry["id"])

    def test_no_secrets_or_personal_data(self):
        for entry in self.entries:
            probe = " ".join([str(entry.get(key) or "") for key in ("title", "summary", "notes")])
            probe += " " + str(entry["provenance"].get("origin") or "")
            self.assertIsNone(mod.FORBIDDEN_TEXT_RE.search(probe), entry["id"])
        for path in sorted(ARCHIVE.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("-----BEGIN", text, path.name)
            self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}", path.name)

    def test_source_ids_resolve(self):
        by_id = {entry["id"]: entry for entry in self.entries}
        for entry in self.entries:
            for source_id in entry["source_ids"]:
                self.assertIn(source_id, by_id, entry["id"])
                self.assertEqual(by_id[source_id]["kind"], "source", entry["id"])
            if entry["kind"] in ("fact_pack", "legal"):
                if entry["evidence_status"] != "NOT_FETCHED":
                    self.assertTrue(entry["source_ids"], entry["id"])

    def test_links_exist(self):
        for entry in self.entries:
            self.assertTrue(entry["links"], entry["id"])
            for link in entry["links"]:
                self.assertTrue((ROOT / link).exists(), f"{entry['id']}: {link}")

    def test_namespaces_registered(self):
        for entry in self.entries:
            self.assertIn(entry["namespace"], self.namespaces, entry["id"])
            for role in entry["audience"]:
                self.assertTrue(role == "all" or role in self.roles, f"{entry['id']}: {role}")

    def test_no_duplicate_content(self):
        keys = set()
        for entry in self.entries:
            key = mod.content_key(entry)
            self.assertNotIn(key, keys, entry["id"])
            keys.add(key)

    def test_legal_entries_are_pointers(self):
        legal = [entry for entry in self.entries if entry["kind"] == "legal"]
        self.assertGreaterEqual(len(legal), 2)
        for entry in legal:
            self.assertIn(entry["jurisdiction"], ("EU", "UA", "US", "INTL"), entry["id"])
            self.assertIn(entry["evidence_status"], ("NOT_FETCHED", "PARTIAL"), entry["id"])

    # --- supersession -----------------------------------------------------------------------

    def test_supersedes_chain_rules(self):
        base = sample_entry(entry_id="AR-0900", version=1)
        successor = sample_entry(entry_id="AR-0901", title="Тестовая запись архива (v2)",
                                 version=2, supersedes="AR-0900")
        by_id = {"AR-0900": base, "AR-0901": successor}
        issues = mod.validate_entry(successor, namespaces=self.namespaces, roles=self.roles,
                                    by_id=by_id)
        self.assertEqual(issues, [], issues)

        fork = sample_entry(entry_id="AR-0902", title="Тестовая запись архива (v3)",
                            version=3, supersedes="AR-0900")
        by_id["AR-0902"] = fork
        codes = {issue["code"] for issue in mod.validate_entry(
            fork, namespaces=self.namespaces, roles=self.roles, by_id=by_id)}
        self.assertIn("supersedes_fork", codes)

        wrong_scope = sample_entry(entry_id="AR-0903", namespace="osint", title="Другая область",
                                   version=2, supersedes="AR-0900")
        codes = {issue["code"] for issue in mod.validate_entry(
            wrong_scope, namespaces=self.namespaces, roles=self.roles, by_id=by_id)}
        self.assertIn("supersedes_scope", codes)

        frozen = sample_entry(entry_id="AR-0904", version=1, supersedes="AR-0900")
        codes = {issue["code"] for issue in mod.validate_entry(
            frozen, namespaces=self.namespaces, roles=self.roles, by_id=by_id)}
        self.assertIn("version_order", codes)

        archived = sample_entry(entry_id="AR-0905", archived=True)
        by_id_archived = {"AR-0905": archived, "AR-0906": sample_entry(
            entry_id="AR-0906", title="Замена архивной", version=2, supersedes="AR-0905")}
        codes = {issue["code"] for issue in mod.validate_entry(
            by_id_archived["AR-0906"], namespaces=self.namespaces, roles=self.roles,
            by_id=by_id_archived)}
        self.assertIn("supersedes_archived", codes)

    # --- freshness --------------------------------------------------------------------------

    def test_freshness_classes(self):
        self.assertEqual(mod.freshness("2026-12-01", date(2026, 11, 22)), "due_soon")
        self.assertEqual(mod.freshness("2027-11-22", date(2026, 11, 22)), "fresh")
        self.assertEqual(mod.freshness("2026-11-21", date(2026, 11, 22)), "stale")
        self.assertEqual(mod.freshness("2027-11-22", None), "UNKNOWN")

    def test_stale_records_are_reported_not_deleted(self):
        summary = mod.validate_all(ARCHIVE, date(2030, 1, 1))
        self.assertEqual(summary["freshness"]["stale"], summary["entries"])
        self.assertEqual(len(read_seed()), summary["entries"])
        self.assertTrue(summary["valid"], summary["issues"])

    # --- context packs ----------------------------------------------------------------------

    def test_pack_visibility_and_audience(self):
        pack = mod.build_pack(ARCHIVE, project="osint", topic="", role="osint-scout",
                              today=date(2026, 11, 22), write=False)
        namespaces = {entry["namespace"] for entry in pack["entries"]}
        self.assertEqual(namespaces, {"osint", "global"})
        ids = {entry["id"] for entry in pack["entries"]}
        self.assertNotIn("AR-0014", ids)  # lead-record: audience does not include osint-scout
        self.assertIn("AR-0006", ids)
        reasons = {item["reason"] for item in pack["excluded"]}
        self.assertIn("namespace:legal", reasons)
        self.assertIn("audience:osint-scout", reasons)

    def test_pack_topic_relevance(self):
        pack = mod.build_pack(ARCHIVE, project="legal", topic="давность доказательства",
                              role="legal-claim", today=date(2026, 11, 22), write=False)
        ids = {entry["id"] for entry in pack["entries"]}
        self.assertIn("AR-0010", ids)
        self.assertIn("AR-0011", ids)
        self.assertNotIn("AR-0001", ids)
        for entry in pack["entries"]:
            self.assertTrue(entry["match"], entry["id"])

    def test_pack_stale_excluded_by_default(self):
        late = date(2030, 1, 1)
        pack = mod.build_pack(ARCHIVE, project="global", topic="", role="archivist",
                              today=late, write=False)
        self.assertEqual(pack["counts"]["selected"], 0)
        reasons = [item["reason"] for item in pack["excluded"]]
        self.assertIn("stale", reasons)
        pack = mod.build_pack(ARCHIVE, project="global", topic="", role="archivist", today=late,
                              include_stale=True, write=False)
        self.assertGreater(pack["counts"]["selected"], 0)
        self.assertTrue(all(entry["freshness"] == "stale" for entry in pack["entries"]))

    def test_pack_restricted_and_public_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            extra = [
                mod.build_entry(kind="method", namespace="cases",
                                title="Закрытый метод проекта", summary="Метод только для своего "
                                "проекта: проверка ограничения доступа в пакете.",
                                owner_role="archivist", audience=["all"], as_of="2026-11-22",
                                evidence="HYPOTHESIS", grade="C", licence="INTERNAL",
                                access="RESTRICTED", provenance=provenance(),
                                tags=["test"], links=["knowledge_archive/schema.json"],
                                notes="Тестовая запись про доступ.", entry_id="AR-0100"),
                mod.build_entry(kind="method", namespace="cases",
                                title="Открытый метод проекта", summary="Метод с доступом PUBLIC: "
                                "его берут пакеты других проектов.",
                                owner_role="archivist", audience=["all"], as_of="2026-11-22",
                                evidence="HYPOTHESIS", grade="C", licence="INTERNAL",
                                access="PUBLIC", provenance=provenance(),
                                tags=["test"], links=["knowledge_archive/schema.json"],
                                notes="Тестовая запись про доступ.", entry_id="AR-0101"),
            ]
            write_jsonl(archive / "entries" / "extra.jsonl", extra)
            other = mod.build_pack(archive, project="osint", topic="метод", role="archivist",
                                   today=date(2026, 11, 22), write=False)
            other_ids = {entry["id"] for entry in other["entries"]}
            self.assertNotIn("AR-0100", other_ids)
            self.assertIn("AR-0101", other_ids)
            own = mod.build_pack(archive, project="cases", topic="метод", role="archivist",
                                 today=date(2026, 11, 22), write=False)
            self.assertNotIn("AR-0100", {entry["id"] for entry in own["entries"]})
            allowed = mod.build_pack(archive, project="cases", topic="метод", role="archivist",
                                     today=date(2026, 11, 22), allow_restricted=True, write=False)
            self.assertIn("AR-0100", {entry["id"] for entry in allowed["entries"]})

    def test_pack_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            first = mod.build_pack(archive, project="osint", topic="законный сбор",
                                   role="osint-scout", today=date(2026, 11, 22))
            packed = sorted(archive.glob("context_packs/*"))
            before = {path.name: path.read_bytes() for path in packed}
            second = mod.build_pack(archive, project="osint", topic="законный сбор",
                                    role="osint-scout", today=date(2026, 11, 22))
            after = {path.name: path.read_bytes() for path in sorted(archive.glob("context_packs/*"))}
            self.assertEqual(before, after)
            self.assertEqual(first["counts"], second["counts"])

    def test_pack_rejects_unknown_project_and_role(self):
        with self.assertRaises(mod.ArchiveError):
            mod.build_pack(ARCHIVE, project="unknown-project", write=False)
        with self.assertRaises(mod.ArchiveError):
            mod.build_pack(ARCHIVE, project="global", role="nobody", write=False)

    def test_pack_gaps_when_nothing_matches(self):
        pack = mod.build_pack(ARCHIVE, project="osint", topic="квантовая гравитация",
                              role="osint-scout", today=date(2026, 11, 22), write=False)
        self.assertEqual(pack["counts"]["selected"], 0)
        self.assertTrue(any("квантовая" in gap for gap in pack["gaps"]), pack["gaps"])

    # --- add / merge ------------------------------------------------------------------------

    def test_merge_appends_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            added = run_tool("--archive", str(archive), *add_args())
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertTrue(list((archive / "inbox").glob("*.jsonl")))
            merged = run_tool("--archive", str(archive), "merge")
            self.assertEqual(merged.returncode, 0, merged.stderr)
            self.assertEqual(len(read_seed(archive / "entries" / "seed.jsonl")),
                             len(self.entries) + 1)
            self.assertEqual(list((archive / "inbox").glob("*.jsonl")), [])
            again = run_tool("--archive", str(archive), "merge")
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(len(read_seed(archive / "entries" / "seed.jsonl")),
                             len(self.entries) + 1)
            self.assertEqual(run_tool("--archive", str(archive), "--check").returncode, 0)

    def test_merge_skips_content_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            duplicate = sample_entry(entry_id="AR-0901", title=self.entries[0]["title"],
                                     namespace=self.entries[0]["namespace"],
                                     kind=self.entries[0]["kind"])
            write_jsonl(archive / "inbox" / "AR-0901.jsonl", [duplicate])
            merged = run_tool("--archive", str(archive), "merge")
            self.assertEqual(merged.returncode, 0, merged.stderr)
            self.assertIn("duplicates: 1", merged.stdout)
            self.assertEqual(len(read_seed(archive / "entries" / "seed.jsonl")),
                             len(self.entries))

    def test_add_rejects_secret_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            digest = tree_digest(archive)
            result = run_tool("--archive", str(archive), *add_args(
                "--title", "Утечка контакта",
                "--summary", "Тестовая запись с контактом user@example.com внутри текста."))
            self.assertEqual(result.returncode, 2)
            self.assertIn("BLOCKED_INPUT", result.stderr)
            self.assertEqual(tree_digest(archive), digest)

    def test_add_rejects_unknown_namespace_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            result = run_tool("--archive", str(archive), *add_args(
                "--namespace", "unknown-ns", "--title", "Чужой namespace"))
            self.assertEqual(result.returncode, 2)
            first = run_tool("--archive", str(archive), *add_args())
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_tool("--archive", str(archive), *add_args())
            self.assertEqual(second.returncode, 2)
            self.assertIn("duplicate content", second.stderr)

    def test_add_assigns_next_id_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            result = run_tool("--archive", str(archive), *add_args())
            self.assertEqual(result.returncode, 0, result.stderr)
            record = read_seed(archive / "inbox" / "AR-0015.jsonl")[0]
            self.assertEqual(record["id"], "AR-0015")
            self.assertEqual(record["review_due"], "2027-11-22")
            self.assertEqual(record["schema"], "amfe.archive.entry.v1")

    # --- CLI / index / export ---------------------------------------------------------------

    def test_check_cli_and_validate_json(self):
        result = run_tool("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        machine = run_tool("validate", "--json", "--today", "2026-11-22")
        self.assertEqual(machine.returncode, 0, machine.stderr)
        payload = json.loads(machine.stdout)
        self.assertEqual(payload["schema"], "amfe.archive.validate.v1")
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["entries"], len(self.entries))
        self.assertEqual(payload["issues"], [])

    def test_check_writes_nothing(self):
        digest = tree_digest(ARCHIVE)
        result = run_tool("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree_digest(ARCHIVE), digest)

    def test_index_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            first = mod.build_index(archive, date(2026, 11, 22))
            before = (archive / "index.json").read_bytes(), (archive / "index.md").read_bytes()
            mod.build_index(archive, date(2026, 11, 22))
            after = (archive / "index.json").read_bytes(), (archive / "index.md").read_bytes()
            self.assertEqual(before, after)
            self.assertEqual(first["totals"], len(self.entries))
            self.assertEqual(first["active"], len(self.entries))
            self.assertEqual({row["namespace"] for row in first["namespaces"]},
                             set(self.namespaces))

    def test_export_is_deterministic_and_complete(self):
        json_text = mod.export(ARCHIVE, "json", date(2026, 11, 22))
        self.assertEqual(json_text, mod.export(ARCHIVE, "json", date(2026, 11, 22)))
        payload = json.loads(json_text)
        self.assertEqual(payload["schema"], "amfe.archive.export.v1")
        self.assertEqual(len(payload["entries"]), len(self.entries))
        lines = [line for line in mod.export(ARCHIVE, "jsonl").splitlines() if line.strip()]
        self.assertEqual(len(lines), len(self.entries))
        markdown = mod.export(ARCHIVE, "md", date(2026, 11, 22))
        self.assertIn("## global", markdown)
        self.assertIn("AR-0001", markdown)

    def test_search_filters(self):
        by_legal = mod.search_entries(read_seed(), "давность", namespace="legal")
        self.assertEqual({entry["id"] for entry in by_legal}, {"AR-0010"})
        templates = mod.search_entries(read_seed(), "", kind="template")
        self.assertEqual({entry["id"] for entry in templates}, {"AR-0004", "AR-0005", "AR-0011"})
        stale = mod.search_entries(read_seed(), "", today=date(2030, 1, 1), only_stale=True)
        self.assertEqual(len(stale), len(self.entries))

    def test_broken_record_is_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = copy_archive(tmp)
            (archive / "entries" / "bad.jsonl").write_text("{not json}\n", encoding="utf-8")
            result = run_tool("--archive", str(archive), "--check")
            self.assertEqual(result.returncode, 2)
            self.assertIn("json", result.stderr)

    # --- determinism / safety ---------------------------------------------------------------

    def test_no_network_and_no_clock(self):
        source = TOOL.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import socket", "http.client",
                          "datetime.now", "date.today", "time.time", "import random"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertNotIn("os.environ", source)
        self.assertTrue(DOC.exists())

    def test_entry_rules_reject_defects(self):
        by_id = {"AR-0900": sample_entry()}
        cases = [
            (dict(archived="yes"), "archived_flag"),
            (dict(language="ru"), "unknown_field"),
            (dict(review_due="2027-01-01"), "review_due_mismatch"),
            (dict(retention="LEGAL_HOLD", archived=True), "legal_hold_archived"),
            (dict(licence="NONE"), "licence_none"),
            (dict(access="PUBLIC", licence="SUBSCRIPTION"), "public_restricted_licence"),
            (dict(access="RESTRICTED", licence="OPEN"), "restricted_open_licence"),
            (dict(jurisdiction="EU"), "jurisdiction_not_allowed"),
            (dict(provenance=provenance(origin="https://user:pass@example.org/x")),
             "origin_credentials"),
            (dict(provenance=provenance(origin="docs/223_Архивариус_и_межпроектная_база.md",
                                        origin_type="OFFICIAL_LAW")),
             "origin_required_url"),
            (dict(audience=["nobody-role"]), "audience_role"),
            (dict(version=0), "version"),
        ]
        for overrides, expected in cases:
            entry = sample_entry(**overrides)
            codes = {issue["code"] for issue in mod.validate_entry(
                entry, namespaces=self.namespaces, roles=self.roles, by_id=by_id)}
            self.assertIn(expected, codes, f"{overrides}: {codes}")

    def test_legal_kind_requires_jurisdiction_and_source(self):
        legal = mod.build_entry(kind="legal", namespace="legal", jurisdiction=None,
                                title="Правовая рамка без юрисдикции",
                                summary="Тестовая правовая запись без обязательных полей.",
                                owner_role="archivist", audience=["legal-claim"],
                                as_of="2026-11-22", evidence="HYPOTHESIS", grade="C",
                                licence="INTERNAL", provenance=provenance(),
                                tags=["legal"], links=["knowledge_archive/schema.json"],
                                notes="Тестовая запись.", entry_id="AR-0907")
        codes = {issue["code"] for issue in mod.validate_entry(
            legal, namespaces=self.namespaces, roles=self.roles, by_id={})}
        self.assertIn("jurisdiction_required", codes)
        self.assertIn("sources_required", codes)


if __name__ == "__main__":
    unittest.main()
