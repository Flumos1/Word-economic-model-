#!/usr/bin/env python3
"""Contract tests for protocols/signoff_registry.json and the docs/185 sign-off packet (task-57).

The registry records a human gate signature. An agent may only publish an entry with status
``PENDING_HUMAN_SIGNATURE`` and null ``signature``/``date``: these tests fail if a signature is
invented, if the pinned SHA no longer recomputes, or if the entry is malformed. When a human
actually signs, the Lead updates the registry *and* this file in one reviewed change; an agent
must never flip the status on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "protocols" / "signoff_registry.json"
DOC160 = ROOT / "docs" / "160_Внешняя_проверка_G2.md"
PACKET = ROOT / "docs" / "185_Подпись_docs_160.md"

SCHEMA = "amfe.signoff_registry.v1"
SIGNATURE_FIELDS = ("name", "role", "signer_type", "date", "decision", "scope")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SignoffError(Exception):
    """A registry entry is invalid (fail-closed verification)."""


def lf_sha256(path: Path) -> str:
    """SHA-256 over LF-normalised bytes — the project convention for text artifacts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def validate_registry(registry: dict, root: Path = ROOT) -> None:
    """Fail-closed validator: raises SignoffError on any structural or pin problem."""
    if registry.get("schema") != SCHEMA:
        raise SignoffError(f"unexpected schema: {registry.get('schema')!r}")
    rules = registry.get("rules") or {}
    if rules.get("fail_closed") is not True:
        raise SignoffError("rules.fail_closed must be true")
    if rules.get("signature_authority") != "human":
        raise SignoffError("only a human may sign")
    if rules.get("agent_may_sign") is not False:
        raise SignoffError("rules.agent_may_sign must be false")
    statuses = set(registry.get("status_values") or [])
    if "PENDING_HUMAN_SIGNATURE" not in statuses:
        raise SignoffError("PENDING_HUMAN_SIGNATURE must be an allowed status")
    entries = registry.get("signoffs")
    if not isinstance(entries, list) or not entries:
        raise SignoffError("signoffs must be a non-empty list")
    for entry in entries:
        sid = entry.get("id") or "<missing id>"
        for key in ("object", "packet"):
            block = entry.get(key) or {}
            rel = block.get("path")
            if not rel:
                raise SignoffError(f"{sid}: {key}.path is missing")
            path = root / rel
            if not path.is_file():
                raise SignoffError(f"{sid}: {key} file is missing: {rel}")
            claimed = str(block.get("sha256", "")).lower()
            if not HEX64.match(claimed):
                raise SignoffError(f"{sid}: {key}.sha256 is not 64 lowercase hex chars")
            actual = lf_sha256(path)
            if actual != claimed:
                raise SignoffError(f"{sid}: {key} sha mismatch ({actual} != {claimed})")
            if int(block.get("bytes", -1)) != path.stat().st_size:
                raise SignoffError(f"{sid}: {key}.bytes does not match the file size")
        status = entry.get("status")
        if status not in statuses:
            raise SignoffError(f"{sid}: unknown status {status!r}")
        signature = entry.get("signature")
        signed_date = entry.get("date")
        if status == "PENDING_HUMAN_SIGNATURE":
            if signature is not None or signed_date is not None:
                raise SignoffError(f"{sid}: pending entry must not carry a signature or a date")
            continue
        if not isinstance(signature, dict):
            raise SignoffError(f"{sid}: status {status} requires a signature object")
        if signature.get("signer_type") != "human":
            raise SignoffError(f"{sid}: signature must come from a human, not an agent")
        missing = [field for field in SIGNATURE_FIELDS
                   if field != "date" and not str(signature.get(field) or "").strip()]
        if missing:
            raise SignoffError(f"{sid}: signature is missing {missing}")
        if not signed_date or not ISO_DATE.match(str(signed_date)):
            raise SignoffError(f"{sid}: signed status requires an ISO date")
        if signature.get("decision") not in (rules.get("decision_values") or []):
            raise SignoffError(f"{sid}: decision {signature.get('decision')!r} is not allowed")
        if status == "SIGNED_BY_HUMAN" and signature.get("decision") == "REJECT":
            raise SignoffError(f"{sid}: REJECT belongs to REJECTED_BY_HUMAN")
        if status == "REJECTED_BY_HUMAN" and signature.get("decision") != "REJECT":
            raise SignoffError(f"{sid}: REJECTED_BY_HUMAN requires decision=REJECT")


class SignoffRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = REGISTRY.read_text(encoding="utf-8")
        cls.registry = json.loads(cls.raw)
        cls.entry = cls.registry["signoffs"][0]

    # -- schema / structure -------------------------------------------------------------------
    def test_schema_and_hash_policy(self):
        self.assertEqual(self.registry["schema"], SCHEMA)
        self.assertEqual(self.registry["hash_policy"]["algorithm"], "sha256")
        self.assertIn("LF", self.registry["hash_policy"]["normalisation"])
        self.assertEqual(self.registry["rules"]["signature_authority"], "human")
        self.assertFalse(self.registry["rules"]["agent_may_sign"])
        self.assertEqual(self.registry["rules"]["agent_max_status"], "PENDING_HUMAN_SIGNATURE")

    def test_single_entry_targets_docs_160(self):
        self.assertEqual(len(self.registry["signoffs"]), 1)
        self.assertEqual(self.entry["id"], "G4-SIGN-160")
        self.assertEqual(self.entry["gate"], "G4")
        self.assertEqual(Path(self.entry["object"]["path"]).name, DOC160.name)
        self.assertEqual(Path(self.entry["packet"]["path"]).name, PACKET.name)
        self.assertTrue(DOC160.is_file() and PACKET.is_file())

    def test_current_registry_is_valid_fail_closed(self):
        validate_registry(self.registry)  # must not raise

    # -- human signature recorded from the scan ------------------------------------------------
    def test_status_signed_by_human(self):
        self.assertEqual(self.entry["status"], "SIGNED_BY_HUMAN")
        self.assertIsInstance(self.entry["signature"], dict)
        self.assertEqual(self.entry["signature"]["signer_type"], "human")
        self.assertEqual(self.entry["signature"]["decision"], "APPROVE")
        self.assertEqual(self.entry["date"], "2026-11-20")
        self.assertEqual(self.entry["signature"]["name"], "Еременко Виктор Иванович")
        self.assertEqual(self.entry["blocked_by"], [])
        self.assertTrue(self.entry["not_admission"])

    def test_raw_json_has_the_human_signature_not_fabricated_aliases(self):
        self.assertIn('"status": "SIGNED_BY_HUMAN"', self.raw)
        self.assertIn('"signature": {', self.raw)
        self.assertIn('"date": "2026-11-20"', self.raw)
        for forbidden in ("signed_by", "signed_at", "signature_name", "signed_date", "signed_off_by"):
            self.assertNotIn(f'"{forbidden}"', self.raw, forbidden)
        self.assertIsNone(self.entry.get("signed_by"))
        self.assertIsNone(self.entry.get("signature_date"))

    # -- pins recompute -----------------------------------------------------------------------
    def test_object_sha_recomputes(self):
        block = self.entry["object"]
        self.assertEqual(block["sha256"], lf_sha256(DOC160))
        self.assertEqual(block["bytes"], DOC160.stat().st_size)
        self.assertNotEqual(block["bytes"], 0)

    def test_packet_sha_recomputes(self):
        block = self.entry["packet"]
        self.assertEqual(block["sha256"], lf_sha256(PACKET))
        self.assertEqual(block["bytes"], PACKET.stat().st_size)
        self.assertNotEqual(block["sha256"], self.entry["object"]["sha256"])

    def test_pins_change_when_the_document_changes(self):
        tampered = copy.deepcopy(self.entry)
        tampered["object"]["sha256"] = "0" * 64
        with self.assertRaises(SignoffError):
            validate_registry({"schema": SCHEMA, "status_values": self.registry["status_values"],
                               "rules": self.registry["rules"], "signoffs": [tampered]})

    # -- packet content -----------------------------------------------------------------------
    def test_packet_states_the_evidence(self):
        text = PACKET.read_text(encoding="utf-8")
        for token in ("9/9", "69", "PENDING_HUMAN_SIGNATURE", "READY_FOR_HUMAN_SIGN_OFF",
                      "допуск", "admission=false", "NOT_CONFIRMED", "Не означает"):
            self.assertIn(token, text, token)

    def test_packet_has_human_checklist_and_signature_form(self):
        text = PACKET.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("- [ ]"), 5)
        for field in ("Имя, фамилия", "Роль/должность", "Дата (ISO 8601)", "Решение", "Подпись",
                      "Тип подписанта"):
            self.assertIn(field, text, field)
        self.assertIn("APPROVE_WITH_NOTES", text)
        self.assertIn("не заполнено", text)

    def test_docs_160_points_to_packet_and_registry(self):
        text = DOC160.read_text(encoding="utf-8")
        self.assertIn("185_Подпись_docs_160.md", text)
        self.assertIn("signoff_registry.json", text)
        self.assertIn("PENDING_HUMAN_SIGNATURE", text)

    def test_counts_are_consistent_between_registry_and_packet(self):
        review = self.entry["automated_review"]
        self.assertEqual(review["checks"], 9)
        self.assertEqual(review["checks_passed"], 9)
        self.assertEqual(review["numbers_checked"], 69)
        self.assertEqual(sum(review["numbers_by_track"].values()), 69)
        self.assertEqual(review["numbers_mismatched"], 0)
        self.assertEqual(review["findings"], 0)
        self.assertEqual(set(review["findings_by_severity"].values()), {0})
        self.assertEqual(set(review["verdicts"].values()), {"CONFIRMED"})
        self.assertEqual(review["review_state"], "READY_FOR_HUMAN_SIGN_OFF")
        text = PACKET.read_text(encoding="utf-8")
        self.assertIn(self.entry["object"]["sha256"], text)

    # -- fail-closed negatives ----------------------------------------------------------------
    def test_fail_closed_on_missing_object(self):
        broken = copy.deepcopy(self.registry)
        broken["signoffs"][0]["object"]["path"] = "docs/does-not-exist.md"
        with self.assertRaises(SignoffError):
            validate_registry(broken)

    def test_fail_closed_on_signed_status_without_signature(self):
        broken = copy.deepcopy(self.registry)
        broken["signoffs"][0]["status"] = "SIGNED_BY_HUMAN"
        broken["signoffs"][0]["signature"] = None
        broken["signoffs"][0]["date"] = None
        with self.assertRaises(SignoffError):
            validate_registry(broken)

    def test_fail_closed_on_agent_signature(self):
        broken = copy.deepcopy(self.registry)
        broken["signoffs"][0]["status"] = "SIGNED_BY_HUMAN"
        broken["signoffs"][0]["date"] = "2026-11-18"
        broken["signoffs"][0]["signature"] = {
            "name": "DeepSeek agent", "role": "protocol-reviewer", "signer_type": "agent",
            "date": "2026-11-18", "decision": "APPROVE", "scope": "docs/160",
        }
        with self.assertRaises(SignoffError):
            validate_registry(broken)

    def test_human_signature_shape_is_accepted_and_repo_is_signed(self):
        signed = copy.deepcopy(self.registry)
        signed["signoffs"][0]["status"] = "SIGNED_BY_HUMAN"
        signed["signoffs"][0]["date"] = "2026-11-18"
        signed["signoffs"][0]["signature"] = {
            "name": "Test Human", "role": "reviewer", "signer_type": "human",
            "date": "2026-11-18", "decision": "APPROVE_WITH_NOTES", "scope": "docs/160 only",
        }
        validate_registry(signed)  # the schema can express a real signature
        self.assertEqual(self.entry["status"], "SIGNED_BY_HUMAN")  # the human-signed entry is recorded


if __name__ == "__main__":
    unittest.main(verbosity=2)
