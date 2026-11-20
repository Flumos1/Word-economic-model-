"""CLI for the AMFE event bus and audit journal (docs/194).

Commands
--------
    python tools/event_bus.py --list                      # last events of the journal
    python tools/event_bus.py --emit MODEL_REGISTERED --payload '{"model_id":"m1"}' --actor me
    python tools/event_bus.py --verify                    # recompute the hash chain
    python tools/event_bus.py --export build/event_bus/export
    python tools/event_bus.py --demo                      # read-only demo over existing artefacts
    python tools/event_bus.py --summary --json            # machine-readable journal summary

The demo (``--demo``) reads the project's existing artefacts (forecast registry, sign-off registry,
c6 audit), emits an aggregate-only event per record into a *fresh* demo journal
(``build/event_bus/demo.jsonl`` by default), and verifies the chain.  It never modifies the source
artefacts and never commits anything.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.data.audit_log import (  # noqa: E402
    AuditError,
    canonical_json,
    emit_export_event,
    emit_forecast_event,
    emit_scenario_event,
)
from amfe.data.event_bus import (  # noqa: E402
    DEFAULT_JOURNAL,
    EVENT_SCHEMA,
    EventBus,
    EventBusError,
    UnknownKind,
)

DEMO_OUT = ROOT / "build" / "event_bus" / "demo.jsonl"
FORECAST_PATH = "registry/forecasts.jsonl"
SIGNOFF_PATH = "protocols/signoff_registry.json"
C6_PATH = "build/c6/c6_audit.jsonl"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_ts(path: Path) -> str:
    stamp = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def demo_events(root: Path) -> list[dict]:
    """Build the demo event list from existing artefacts (read-only, deterministic).

    Each returned item is ``{kind, payload, refs, actor, subject, ts}``; the timestamp is the source
    file's mtime so a demo replay over unchanged artefacts reproduces the same event ids.
    """
    events: list[dict] = []
    forecast_file = root / FORECAST_PATH
    if forecast_file.is_file():
        digest = _sha256(forecast_file)
        stamp = _file_ts(forecast_file)
        for record in _read_jsonl(forecast_file):
            quantiles = record.get("quantiles_5_to_95_step_5") or []
            events.append({
                "kind": "FORECAST_RECORDED", "actor": "registry.forecasts", "ts": stamp,
                "subject": f"{record.get('candidate_id')}:{record.get('target')}:"
                           f"{record.get('origin')}",
                "payload": {"candidate_id": record.get("candidate_id"),
                            "target": record.get("target"), "origin": record.get("origin"),
                            "horizon": record.get("horizon"), "point": record.get("point"),
                            "quantiles_count": len(quantiles)},
                "refs": [{"path": FORECAST_PATH, "sha256": digest}],
            })
    signoff_file = root / SIGNOFF_PATH
    if signoff_file.is_file():
        stamp = _file_ts(signoff_file)
        document = json.loads(signoff_file.read_text(encoding="utf-8"))
        for entry in document.get("signoffs", []):
            review = entry.get("automated_review") or {}
            obj = entry.get("object") or {}
            packet = entry.get("packet") or {}
            refs = []
            for item in (obj, packet):
                if item.get("path") and item.get("sha256"):
                    refs.append({"path": item["path"], "sha256": item["sha256"]})
            events.append({
                "kind": "SIGNOFF", "actor": "protocols.signoff", "ts": stamp,
                "subject": str(entry.get("id") or entry.get("gate") or "signoff"),
                "payload": {
                    "signoff_id": entry.get("id"), "gate": entry.get("gate"),
                    "status": entry.get("status"), "review_state": review.get("review_state"),
                    "tests_count": review.get("tests_count"),
                    "checks_passed": review.get("checks_passed"),
                    "numbers_checked": review.get("numbers_checked"),
                    "numbers_mismatched": review.get("numbers_mismatched"),
                    "findings": review.get("findings"),
                },
                "refs": refs,
            })
    c6_file = root / C6_PATH
    if c6_file.is_file():
        digest = _sha256(c6_file)
        stamp = _file_ts(c6_file)
        for record in _read_jsonl(c6_file):
            events.append({
                "kind": "DATA_RELEASE", "actor": "c6.audit", "ts": stamp,
                "subject": f"c6:{record.get('week')}:{record.get('event')}",
                "payload": {"week": record.get("week"), "event": record.get("event"),
                            "registered": record.get("registered"),
                            "candidate_rows": record.get("candidate_rows"),
                            "control_rows": record.get("control_rows"),
                            "source_schema": record.get("schema")},
                "refs": [{"path": C6_PATH, "sha256": digest}],
            })
    return events


def run_demo(root: Path, out_path: Path, *, force: bool = False) -> dict:
    """Emit the demo events into a fresh journal and verify the chain."""
    if out_path.exists() and not force:
        raise EventBusError(f"demo journal already exists: {out_path}; use --force to recreate it")
    if out_path.exists() and force:
        out_path.unlink()
    bus = EventBus(out_path)
    events = demo_events(root)
    published = []
    for item in events:
        event = bus.publish(item["kind"], item["payload"], actor=item["actor"],
                            subject=item["subject"], refs=item["refs"], ts=item["ts"])
        published.append(event)
    verification = bus.verify()
    return {"journal": str(out_path), "published": len(published),
            "verify_ok": verification["ok"], "head": verification["head"],
            "kinds": bus.summary()["kinds"], "events": [event.as_dict() for event in published],
            "journal_sha256": verification["journal_sha256"],
            "sources": [FORECAST_PATH, SIGNOFF_PATH, C6_PATH]}


def _load_payload(text: str) -> dict:
    if text.startswith("@"):
        return json.loads(Path(text[1:]).read_text(encoding="utf-8"))
    try:
        document = json.loads(text)
    except ValueError as error:
        raise EventBusError(f"--payload is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise EventBusError("--payload must be a JSON object of aggregates/hashes")
    return document


def _refs(paths: list[str]) -> list[dict]:
    refs = []
    for item in paths:
        path = Path(item)
        if not path.is_file():
            raise EventBusError(f"ref file not found: {item}")
        try:
            relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            raise EventBusError(f"ref must stay inside the project: {item}")
        refs.append({"path": relative, "sha256": _sha256(path)})
    return refs


def run(args) -> dict:
    journal = Path(args.journal)
    bus = EventBus(journal)
    if args.demo:
        out_path = Path(args.demo_out)
        return run_demo(Path(args.root), out_path, force=args.force)
    if args.verify:
        verification = bus.verify()
        return {"action": "verify", **verification}
    if args.export:
        info = bus.export(args.export, exported_at=args.exported_at)
        return {"action": "export", **info.as_dict()}
    if args.emit:
        payload = _load_payload(args.payload)
        event = bus.publish(args.emit, payload, actor=args.actor or "",
                            subject=args.subject or "", refs=_refs(args.ref),
                            correlation_id=args.correlation_id, ts=args.ts)
        return {"action": "emit", "event": event.as_dict()}
    if args.summary:
        return {"action": "summary", **bus.summary()}
    events = bus.filter(kind=args.kind, subject=args.subject, actor=args.actor,
                        limit=args.limit)
    return {"action": "list", "count": len(events),
            "events": [event.as_dict() for event in events]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AMFE event bus / audit journal (docs/194)")
    parser.add_argument("--journal", default=str(ROOT / DEFAULT_JOURNAL), help="journal path")
    parser.add_argument("--emit", default=None, metavar="KIND",
                        help="publish one event of this kind (see AMFE-EVENT/1.0.0)")
    parser.add_argument("--payload", default="{}", help="JSON object payload (or @file.json)")
    parser.add_argument("--actor", default=None, help="actor id (module/person)")
    parser.add_argument("--subject", default=None, help="subject id of the event")
    parser.add_argument("--correlation-id", dest="correlation_id", default=None)
    parser.add_argument("--ts", default=None, help="explicit ISO-8601 UTC timestamp (determinism)")
    parser.add_argument("--ref", action="append", default=[], metavar="PATH",
                        help="referenced artefact (relative to the project, sha256 computed)")
    parser.add_argument("--list", action="store_true", help="list events (default action)")
    parser.add_argument("--kind", default=None, help="filter by kind")
    parser.add_argument("--limit", type=int, default=None, help="limit the listing")
    parser.add_argument("--verify", action="store_true", help="recompute and verify the chain")
    parser.add_argument("--export", default=None, metavar="DIR",
                        help="export the journal plus a manifest into DIR")
    parser.add_argument("--exported-at", dest="exported_at", default=None,
                        help="optional explicit export timestamp")
    parser.add_argument("--summary", action="store_true", help="journal summary (counts/head)")
    parser.add_argument("--demo", action="store_true",
                        help="read-only demo over existing artefact registries")
    parser.add_argument("--root", default=str(ROOT), help="project root for --demo")
    parser.add_argument("--demo-out", default=str(DEMO_OUT), help="demo journal path")
    parser.add_argument("--force", action="store_true",
                        help="recreate the demo journal if it already exists")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (EventBusError, UnknownKind, AuditError) as error:
        print(f"event-bus error: {error}", file=sys.stderr)
        return 1
    ok = True
    if args.verify or args.demo:
        ok = bool(result.get("verify_ok", result.get("ok", True)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    elif args.demo:
        print(f"demo: {result['published']} events -> {result['journal']}")
        print(f"  kinds: {result['kinds']}")
        print(f"  verify: {'ok' if result['verify_ok'] else 'FAIL'}; head {result['head']}")
        print(f"  sources (read-only): {', '.join(result['sources'])}")
    elif args.verify:
        print(f"verify: {'ok' if result['ok'] else 'FAIL'}; events {result['count']}; "
              f"head {result['head']}")
        for error in result["errors"]:
            print(f"  line {error['line']}: {error['code']}: {error['message']}")
    elif args.export:
        print(f"exported {result['count']} events: {result['journal_path']} "
              f"(sha256 {result['sha256'][:16]})")
    elif args.emit:
        event = result["event"]
        print(f"published seq {event['seq']} {event['kind']} id {event['event_id'][:16]}… "
              f"(prev {event['prev_hash'][:16]}…)")
    elif args.summary:
        print(f"journal {result['journal']}: {result['count']} events, "
              f"verify {'ok' if result['verify_ok'] else 'FAIL'}")
        print(f"  kinds: {result['kinds']}")
        print(f"  head: {result['head']}")
    else:
        print(f"{result['count']} event(s) in {args.journal}")
        for event in result["events"]:
            subject = event.get("subject") or ""
            print(f"  {event['seq']:>3} {event['ts']} {event['kind']:<18} {subject[:60]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
