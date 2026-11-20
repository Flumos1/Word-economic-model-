"""Tests for the AMFE event bus and append-only audit journal (docs/194).

Offline, deterministic and isolated: every test uses its own temporary journal.  The tests cover
canonical event ids, the prev_hash chain, tamper/gap/truncation detection, payload and refs
fail-closed rules, filtering, the integration wrappers, export with a manifest, and the CLI demo
over synthetic artefacts (sources must stay untouched).

Run from the repository root: ``python tests/test_event_bus.py``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.data.audit_log import (  # noqa: E402
    EVENT_SCHEMA,
    GENESIS_HASH,
    AuditError,
    AuditLog,
    JournalCorruption,
    canonical_json,
    emit_export_event,
    emit_forecast_event,
    emit_scenario_event,
    event_id_for,
)
from amfe.data.event_bus import (  # noqa: E402
    EventBus,
    EventBusError,
    UnknownKind,
    utc_now_iso,
)

TS = "2026-11-20T09:00:00Z"


class BusCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="amfe-events-")
        self.base = Path(self._tmp.name)
        self.path = self.base / "journal.jsonl"
        self.bus = EventBus(self.path, clock=lambda: TS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def publish(self, kind="DATA_RELEASE", payload=None, **kwargs):
        return self.bus.publish(kind, payload if payload is not None else {"dataset": "wiod_2010"},
                                ts=kwargs.pop("ts", TS), **kwargs)


class PublishTests(BusCase):
    def test_publish_roundtrip(self):
        event = self.publish(actor="data.import", subject="wiod_2010",
                             refs=[{"path": "build/x.json", "sha256": "a" * 64}])
        self.assertEqual(event.kind, "DATA_RELEASE")
        self.assertEqual(event.seq, 1)
        self.assertEqual(event.prev_hash, GENESIS_HASH)
        self.assertEqual(event.payload, {"dataset": "wiod_2010"})
        self.assertEqual(event.refs, [{"path": "build/x.json", "sha256": "a" * 64}])
        self.assertEqual(self.bus.count(), 1)
        self.assertEqual(self.bus.head()["event_id"], event.event_id)
        record = event.as_dict()
        self.assertEqual(record["schema"], EVENT_SCHEMA)
        self.assertEqual(record["ts"], TS)
        self.assertEqual(AuditLog(self.path).read(), [record])

    def test_event_id_is_canonical_and_deterministic(self):
        first = self.publish()
        recomputed = event_id_for(first.as_dict())
        self.assertEqual(recomputed, first.event_id)
        second = EventBus(self.base / "other.jsonl", clock=lambda: TS).publish(
            "DATA_RELEASE", {"dataset": "wiod_2010"}, ts=TS)
        self.assertEqual(second.event_id, first.event_id)
        again = self.publish().as_dict()  # same content but seq 2 -> new id
        self.assertNotEqual(again["event_id"], first.event_id)

    def test_chain_links_previous_events(self):
        first = self.publish()
        second = self.publish("SIGNOFF", {"gate": "G4"})
        self.assertEqual(second.prev_hash, first.event_id)
        verification = self.bus.verify()
        self.assertTrue(verification["ok"], verification["errors"])
        self.assertEqual(verification["checks"]["events"], 2)
        self.assertTrue(verification["checks"]["chain_ok"])
        self.assertEqual(verification["head"], second.event_id)

    def test_empty_journal_verifies(self):
        verification = self.bus.verify()
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["checks"]["events"], 0)
        self.assertIsNone(verification["head"])

    def test_append_only_prefix_is_preserved(self):
        self.publish()
        prefix = self.path.read_bytes()
        self.publish("MODEL_REGISTERED", {"model_id": "m1"})
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        self.assertEqual(len(prefix.splitlines()), 1)


class ChainVerificationTests(BusCase):
    def setUp(self) -> None:
        super().setUp()
        for index in range(3):
            self.publish("DATA_RELEASE", {"index": index})

    def _lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def _write(self, lines: list[str]) -> None:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_payload_tampering_is_detected(self):
        lines = self._lines()
        record = json.loads(lines[1])
        record["payload"]["index"] = 99
        lines[1] = canonical_json(record)
        self._write(lines)
        verification = self.bus.verify()
        self.assertFalse(verification["ok"])
        codes = {error["code"] for error in verification["errors"]}
        self.assertIn("id_mismatch", codes)
        self.assertEqual(verification["errors"][0]["line"], 2)

    def test_chain_tampering_is_detected(self):
        lines = self._lines()
        record = json.loads(lines[2])
        record["prev_hash"] = "b" * 64
        lines[2] = canonical_json(record)
        self._write(lines)
        codes = {error["code"] for error in self.bus.verify()["errors"]}
        self.assertIn("chain_broken", codes)

    def test_removed_event_is_detected(self):
        lines = self._lines()
        del lines[1]
        self._write(lines)
        codes = {error["code"] for error in self.bus.verify()["errors"]}
        self.assertTrue({"seq_gap", "chain_broken"} & codes, codes)

    def test_truncation_is_detected_with_expected_head(self):
        head = self.bus.head()["event_id"]
        self._write(self._lines()[:-1])
        verification = self.bus.verify(expected_head=head, expected_count=3)
        codes = {error["code"] for error in verification["errors"]}
        self.assertIn("head_mismatch", codes)
        self.assertIn("count_mismatch", codes)
        self.assertFalse(verification["ok"])

    def test_append_refuses_broken_continuity(self):
        record = json.loads(self._lines()[0])
        record["seq"] = 7
        with self.assertRaises(JournalCorruption):
            AuditLog(self.path).append_record(record)
        with self.assertRaises(JournalCorruption):
            AuditLog(self.path).append_record(
                {**record, "seq": 4, "prev_hash": "c" * 64})


class FailClosedPayloadTests(BusCase):
    def test_unknown_kind_is_refused(self):
        with self.assertRaises(UnknownKind):
            self.bus.publish("NOT_A_KIND", {"x": 1}, ts=TS)
        self.assertEqual(self.bus.count(), 0)

    def test_secret_keys_are_refused(self):
        for key in ("password", "token", "api_key", "private_key", "email"):
            with self.assertRaises(AuditError):
                self.bus.publish("MODEL_REGISTERED", {key: "value"}, ts=TS)
        self.assertEqual(self.bus.count(), 0)

    def test_raw_data_keys_are_refused(self):
        for payload in ({"z": [[1.0]]}, {"matrix": [[1.0]]}, {"values": [1, 2]},
                        {"data": [1, 2]}, {"rows": [{"a": 1}]}, {"series": [1, 2]}):
            with self.assertRaises(AuditError):
                self.bus.publish("DATA_RELEASE", payload, ts=TS)
        self.assertEqual(self.bus.count(), 0)

    def test_oversized_and_non_json_payloads_are_refused(self):
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"blob": "x" * 9000}, ts=TS)
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"value": float("nan")}, ts=TS)
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"value": object()}, ts=TS)
        deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", deep, ts=TS)

    def test_refs_validation(self):
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"a": 1}, ts=TS,
                             refs=[{"path": "/etc/passwd"}])
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"a": 1}, ts=TS,
                             refs=[{"path": "secrets.env"}])
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"a": 1}, ts=TS,
                             refs=[{"path": "build/x.json", "sha256": "not-a-hash"}])
        event = self.bus.publish("DATA_RELEASE", {"a": 1}, ts=TS,
                                 refs=[{"path": "build/x.json", "sha256": "a" * 64}])
        self.assertEqual(event.refs[0]["sha256"], "a" * 64)

    def test_invalid_timestamp_is_refused(self):
        with self.assertRaises(AuditError):
            self.bus.publish("DATA_RELEASE", {"a": 1}, ts="2026-11-20 09:00")


class FilterTests(BusCase):
    def test_filters_and_limits(self):
        self.publish("DATA_RELEASE", {"i": 1}, subject="a", actor="x")
        self.publish("SIGNOFF", {"gate": "G4"}, subject="b", actor="y")
        self.publish("DATA_RELEASE", {"i": 2}, subject="c", actor="x")
        self.assertEqual(len(self.bus.filter()), 3)
        self.assertEqual(len(self.bus.filter(kind="DATA_RELEASE")), 2)
        self.assertEqual(len(self.bus.filter(actor="y")), 1)
        self.assertEqual(len(self.bus.filter(subject="c")), 1)
        self.assertEqual(len(self.bus.filter(limit=2)), 2)
        self.assertEqual(len(self.bus.filter(since_ts="2030-01-01T00:00:00Z")), 0)
        with self.assertRaises(UnknownKind):
            self.bus.filter(kind="NOPE")


class WrapperTests(BusCase):
    def test_forecast_scenario_export_wrappers(self):
        forecast = emit_forecast_event(self.bus, candidate_id="ENSEMBLE_ADAPTIVE",
                                       target="T1_CPI_YOY", origin="2026-11", point=3.5,
                                       quantiles=[2.0, 3.0, 4.0], ts=TS)
        self.assertEqual(forecast.kind, "FORECAST_RECORDED")
        self.assertEqual(forecast.payload["quantiles_count"], 3)
        scenario = emit_scenario_event(self.bus, scenario_id="hormuz_closed_6m",
                                       commodity="Brent", price_low=139.0, price_central=168.9,
                                       price_high=268.5, horizon_months=6, ts=TS)
        self.assertEqual(scenario.kind, "SCENARIO_RUN")
        export = emit_export_event(self.bus, product="client_pack", format="json",
                                   bytes_count=1024, sha256="d" * 64, ts=TS)
        self.assertEqual(export.kind, "PRODUCT_EXPORT")
        self.assertTrue(self.bus.verify()["ok"])
        with self.assertRaises(AuditError):
            emit_export_event(self.bus, product="p", format="json", bytes_count=1,
                              sha256="nope", ts=TS)


class ExportTests(BusCase):
    def test_export_copy_and_manifest(self):
        self.publish()
        self.publish("SIGNOFF", {"gate": "G4"})
        info = self.bus.export(self.base / "export", exported_at=TS)
        journal_copy = Path(info.journal_path)
        manifest = json.loads(Path(info.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(journal_copy.read_bytes(), self.path.read_bytes())
        self.assertEqual(manifest["schema"], "amfe.audit.export.v1")
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(manifest["head"], self.bus.head()["event_id"])
        self.assertEqual(manifest["journal_sha256"],
                         hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue(manifest["verify_ok"])
        self.assertEqual(manifest["exported_at"], TS)
        self.assertEqual(Path(info.manifest_path).parent, self.base / "export")


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="amfe-events-cli-")
        self.base = Path(self._tmp.name)
        self.journal = self.base / "journal.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-X", "utf8",
                               str(ROOT / "tools" / "event_bus.py"), *args],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=120)

    def test_emit_list_verify_json(self):
        emit = self.run_tool("--journal", str(self.journal), "--emit", "MODEL_REGISTERED",
                             "--payload", '{"model_id": "m1"}', "--actor", "test",
                             "--ts", TS, "--json")
        self.assertEqual(emit.returncode, 0, emit.stderr)
        event = json.loads(emit.stdout)["event"]
        self.assertEqual(event["event_id"], event_id_for(event))
        listing = self.run_tool("--journal", str(self.journal), "--list", "--json")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)["count"], 1)
        verify = self.run_tool("--journal", str(self.journal), "--verify", "--json")
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])

    def test_verify_detects_tampering_and_fails(self):
        self.run_tool("--journal", str(self.journal), "--emit", "DATA_RELEASE",
                      "--payload", '{"a": 1}', "--ts", TS)
        record = json.loads(self.journal.read_text(encoding="utf-8").strip())
        record["payload"]["a"] = 2
        self.journal.write_text(canonical_json(record) + "\n", encoding="utf-8")
        verify = self.run_tool("--journal", str(self.journal), "--verify", "--json")
        self.assertEqual(verify.returncode, 1)
        self.assertFalse(json.loads(verify.stdout)["ok"])

    def test_unknown_kind_exits_non_zero(self):
        result = self.run_tool("--journal", str(self.journal), "--emit", "NOPE",
                               "--payload", "{}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown event kind", result.stderr)


class DemoTests(unittest.TestCase):
    def _make_sources(self, root: Path) -> None:
        (root / "registry").mkdir(parents=True)
        (root / "protocols").mkdir()
        (root / "build" / "c6").mkdir(parents=True)
        forecast = {"candidate_id": "C1", "target": "T1_CPI_YOY", "origin": "2026-11",
                    "horizon": 1, "point": 3.5, "quantiles_5_to_95_step_5": [2.0] * 19}
        (root / "registry" / "forecasts.jsonl").write_text(
            "\n".join(json.dumps(forecast) for _ in range(2)) + "\n", encoding="utf-8")
        signoff = {"schema": "amfe.signoff_registry.v1", "signoffs": [{
            "id": "G4-SIGN-160", "gate": "G4", "status": "SIGNED_BY_HUMAN",
            "object": {"path": "docs/160.md", "sha256": "a" * 64},
            "packet": {"path": "docs/185.md", "sha256": "b" * 64},
            "automated_review": {"tests_count": 13, "checks_passed": 9, "numbers_checked": 69,
                                 "numbers_mismatched": 0, "findings": 0,
                                 "review_state": "READY_FOR_HUMAN_SIGN_OFF"},
        }]}
        (root / "protocols" / "signoff_registry.json").write_text(
            json.dumps(signoff), encoding="utf-8")
        c6 = {"schema": "AMFE-C6-AUDIT/1.0.0", "at": "2026-11-18", "week": "2026-W47",
              "event": "WEEK_CHECK", "registered": False, "candidate_rows": 0, "control_rows": 0}
        (root / "build" / "c6" / "c6_audit.jsonl").write_text(
            json.dumps(c6) + "\n", encoding="utf-8")

    def test_demo_over_synthetic_artefacts(self):
        with tempfile.TemporaryDirectory(prefix="amfe-events-demo-") as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            self._make_sources(root)
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in root.rglob("*") if path.is_file()}
            out = root / "build" / "event_bus" / "demo.jsonl"
            proc = subprocess.run([sys.executable, "-X", "utf8",
                                   str(ROOT / "tools" / "event_bus.py"), "--demo",
                                   "--root", str(root), "--demo-out", str(out), "--json"],
                                  capture_output=True, text=True, cwd=str(ROOT), timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["published"], 4)  # 2 forecasts + 1 signoff + 1 c6
            self.assertTrue(result["verify_ok"])
            self.assertEqual(result["kinds"], {"DATA_RELEASE": 1, "FORECAST_RECORDED": 2,
                                               "SIGNOFF": 1})
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in root.rglob("*") if path.is_file() and "event_bus" not in str(path)}
            self.assertEqual(before, after)  # sources untouched
            # a second demo run must not silently append to the demo journal
            again = subprocess.run([sys.executable, "-X", "utf8",
                                    str(ROOT / "tools" / "event_bus.py"), "--demo",
                                    "--root", str(root), "--demo-out", str(out)],
                                   capture_output=True, text=True, cwd=str(ROOT), timeout=120)
            self.assertEqual(again.returncode, 1)
            self.assertIn("already exists", again.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
