#!/usr/bin/env python3
"""Proposal engine (G6-prep, L-2/L-3, docs/195): signals -> proposals -> human gate.

Rules are plain code, no LLM and no network.  The engine reads actual project artefacts and proposes:

  * ``q_degradation``  — a model version in the learning registry has lower Q than its predecessor;
  * ``c4_ambiguity``   — the C4/W1 analysis reports structural failures of the mask;
  * ``data_gap``       — a source in the source registry is not VERIFIED;
  * ``c6_release_week``— the C6 audit shows a week without a registered row (or cadence is stale);
  * ``coverage_gap``   — C6 coverage is not measured / outside the nominal tolerance.

Every proposal is appended as ``PROPOSED``; the engine never approves.  Approval requires
``--approve ID --by "Real Name"``: automation-like names are refused by the human gate and an
approved proposal is the only one that can be implemented.

Usage:
    python tools/proposal_engine.py --scan                    # default; --json for a report
    python tools/proposal_engine.py --scan --dst build/registry/proposals.jsonl
    python tools/proposal_engine.py --list [--status PROPOSED] [--kind DATA_GAP]
    python tools/proposal_engine.py --approve <id> --by "Analyst Name" --at 2026-11-20
    python tools/proposal_engine.py --reject <id> --by "Analyst Name" --reason "duplicate"
    python tools/proposal_engine.py --implement <id> --by "Analyst Name"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.learning import proposals as prop  # noqa: E402

DEFAULT_REGISTRY = ROOT / "build" / "registry" / "proposals.jsonl"
DEFAULT_SHADOW = ROOT / "build" / "shadow" / "shadow_status.json"
DEFAULT_C6_AUDIT = ROOT / "build" / "c6" / "c6_audit.jsonl"
DEFAULT_C4 = ROOT / "protocols" / "c4_analysis.json"
DEFAULT_SOURCES = ROOT / "protocols" / "source_registry.json"
DEFAULT_LEARNING = ROOT / "build" / "registry" / "learning.jsonl"
DEFAULT_AT = "2026-11-20"
RULE_VERSION = "1.0.0"
Q_DEGRADATION_MIN = 0.01
Q_MIN_ACTIVE = 0.60


class EngineError(RuntimeError):
    """A required signal artefact is missing or inconsistent; the scan is refused."""


def _relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _load_json(path: Path, name: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise EngineError(f"{name} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineError(f"cannot read {name} {path}: {exc!r}") from exc


def _proposal(kind: str, title: str, rationale: str, evidence: dict, dedupe_key: str,
              benefit: int, risk: int, effort: str, at: str) -> dict:
    return prop.build_proposal(kind=kind, title=title, rationale=rationale, evidence=evidence,
                               dedupe_key=dedupe_key, created_at=at, benefit=benefit, risk=risk,
                               effort=effort,
                               expected_value_note="H-008 proxy: benefit / (1 + risk), effort S/M/L",
                               note=f"rule set v{RULE_VERSION}")


# ------------------------------------------------------------------------------------- rules
def rule_q_degradation(learning_path: Path, at: str) -> tuple[list[dict], list[str]]:
    proposals: list[dict] = []
    notes: list[str] = []
    learning_path = Path(learning_path)
    if not learning_path.is_file():
        notes.append(f"learning registry not found ({_relative(learning_path)}): Q rule skipped")
        return proposals, notes
    try:
        events = json.loads("[" + ",".join(
            line for line in learning_path.read_text(encoding="utf-8").splitlines()
            if line.strip()) + "]")
    except ValueError as exc:
        raise EngineError(f"learning registry is not JSONL: {exc!r}") from exc
    latest: dict = {}
    for event in events:
        if not isinstance(event, dict) or event.get("action") != "REGISTER":
            continue
        quality = event.get("quality") or {}
        q = quality.get("q")
        if not isinstance(q, (int, float)):
            continue
        model = event.get("model_id")
        version = event.get("version")
        if model in latest:
            previous_version, previous_q = latest[model]
            delta = float(q) - previous_q
            if delta <= -Q_DEGRADATION_MIN:
                kind = "NEW_REGIME" if "regime" in str(model).lower() else "NEW_METHOD"
                proposals.append(_proposal(
                    kind,
                    f"Разобрать деградацию Q модели {model} ({previous_version} -> {version})",
                    (f"В learning registry Q упал с {previous_q:.3f} до {float(q):.3f} "
                     f"(delta {delta:+.3f}, порог {Q_DEGRADATION_MIN}). Предложение: проверить "
                     "причину (данные, режим, калибровка) и оформить эксперимент до смены версии."),
                    {"rule": "q_degradation", "model_id": model,
                     "previous_version": previous_version, "previous_q": previous_q,
                     "version": version, "q": float(q), "delta_q": round(delta, 6),
                     "source": _relative(learning_path)},
                    f"q_degradation:{model}:{previous_version}:{version}",
                    benefit=4, risk=2, effort="M", at=at))
        latest[model] = (version, float(q))
    if not latest:
        notes.append("learning registry has no REGISTER events with Q: Q rule skipped")
    return proposals, notes


def rule_c4(c4_path: Path, at: str) -> tuple[list[dict], list[str]]:
    document = _load_json(c4_path, "C4 analysis")
    findings = document.get("structural_findings") or []
    failures = [item for item in findings
                if isinstance(item, dict) and "fail" in str(item.get("finding", "")).lower()]
    if not failures:
        return [], ["C4 analysis has no structural failure findings"]
    ids = [item.get("id") for item in failures]
    return [_proposal(
        "NEW_METHOD",
        "Уточнить маску C4/W1 по структурным находкам",
        ("C4-анализ фиксирует провал критерия при маске (t,t+h]: "
         + "; ".join(str(item.get("finding")) for item in failures[:2])
         + ". Предложение: отдельный эксперимент по маске/периодам до изменения протокола; "
           "frozen-файлы не трогать."),
        {"rule": "c4_ambiguity", "finding_ids": ids,
         "findings": [item.get("finding") for item in failures],
         "source": _relative(c4_path)},
        "c4_mask_ambiguity", benefit=4, risk=2, effort="M", at=at)], []


def rule_data_gaps(sources_path: Path, at: str) -> tuple[list[dict], list[str]]:
    document = _load_json(sources_path, "source registry")
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EngineError("source registry has no sources list")
    proposals = []
    for source in sources:
        status = source.get("status")
        if status == "VERIFIED":
            continue
        source_id = source.get("source_id")
        proposals.append(_proposal(
            "DATA_GAP",
            f"Источник {source_id} не VERIFIED ({status})",
            (f"Источник {source_id} ({source.get('publisher')}) имеет статус {status}, "
             f"доступ {source.get('access')}, retrieved_at={source.get('retrieved_at')}. "
             "Пока источник не VERIFIED, его числа нельзя цитировать как наблюдения; "
             "предложение: решить вопрос доступа/загрузки и обновить запись."),
            {"rule": "data_gap", "source_id": source_id, "status": status,
             "access": source.get("access"), "retrieved_at": source.get("retrieved_at"),
             "license": source.get("license"), "source": _relative(sources_path)},
            f"data_gap:{source_id}", benefit=3, risk=1, effort="S", at=at))
    return proposals, []


def _load_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError as exc:
            raise EngineError(f"{path} is not valid JSONL: {exc!r}") from exc
    return entries


def rule_c6(shadow: dict, c6_audit_path: Path, c6_audit: list[dict] | None, at: str) -> tuple[list[dict], list[str]]:
    proposals = []
    notes: list[str] = []
    journal = shadow.get("journal") or {}
    days_since = journal.get("days_since_last_row")
    rules = shadow.get("rules") or {}
    cadence = rules.get("suspend_cadence_days")
    if isinstance(days_since, int) and isinstance(cadence, int) and days_since > cadence:
        proposals.append(_proposal(
            "NEW_LINK",
            "Восстановить cadence C6: пропущены релизные недели",
            (f"С последней строки C6 прошло {days_since} дн. при пороге {cadence}: "
             "предложение связать календарь релизов с журналом и восстановить еженедельную запись."),
            {"rule": "c6_cadence", "days_since_last_row": days_since,
             "suspend_cadence_days": cadence, "source": "build/shadow/shadow_status.json"},
            f"c6_cadence:{days_since}", benefit=3, risk=1, effort="S", at=at))
    if c6_audit:
        last = c6_audit[-1]
        if last.get("event") == "WEEK_CHECK" and last.get("registered") is False:
            week = last.get("week")
            proposals.append(_proposal(
                "NEW_LINK",
                f"Зарегистрировать строку C6 за неделю {week}",
                (f"C6-аудит фиксирует WEEK_CHECK для {week} с registered=false: строка кандидата "
                 "не зарегистрирована. Предложение: связать неделю релиза с регистрацией и "
                 "записать строку (PIT, без задним числом)."),
                {"rule": "c6_release_week", "week": week, "registered": False,
                 "event_at": last.get("at"), "source": _relative(c6_audit_path)},
                f"c6_release_week:{week}", benefit=3, risk=1, effort="S", at=at))
    else:
        notes.append("C6 audit not found: release-week rule skipped")
    return proposals, notes


def rule_coverage(shadow: dict, at: str) -> tuple[list[dict], list[str]]:
    c6 = shadow.get("c6") or {}
    coverage = c6.get("coverage")
    n_resolved = c6.get("n_resolved")
    if coverage is None:
        return [_proposal(
            "NEW_METHOD",
            "Добавить измерение покрытия интервалов C6",
            ("C6.coverage = null: покрытие интервалов 50/80/90% не измеряется, поэтому "
             f"допуск по покрытию нельзя проверить (n_resolved={n_resolved}). Предложение: "
             "методика расчёта покрытия на разрешённой выборке и запись метрики в статус."),
            {"rule": "coverage_gap", "coverage": None, "n_resolved": n_resolved,
             "coverage_met": c6.get("coverage_met"),
             "source": "build/shadow/shadow_status.json"},
            "coverage_tracking:c6", benefit=4, risk=2, effort="M", at=at)], []
    if c6.get("coverage_met") is False:
        return [_proposal(
            "NEW_METHOD",
            "Разобрать покрытие C6 вне допуска",
            (f"C6.coverage={coverage} вне номинального допуска (coverage_met=false) при "
             f"n_resolved={n_resolved}. Предложение: аудит калибровки интервалов до новых версий."),
            {"rule": "coverage_gap", "coverage": coverage, "coverage_met": False,
             "n_resolved": n_resolved, "source": "build/shadow/shadow_status.json"},
            "coverage_outside_tolerance", benefit=5, risk=2, effort="S", at=at)], []
    return [], ["C6 coverage is measured and in tolerance: coverage rule skipped"]


# --------------------------------------------------------------------------------------- scan
def scan(*, registry: Path = DEFAULT_REGISTRY, shadow: Path = DEFAULT_SHADOW,
         c6_audit: Path = DEFAULT_C6_AUDIT, c4: Path = DEFAULT_C4,
         sources: Path = DEFAULT_SOURCES, learning: Path = DEFAULT_LEARNING,
         at: str = DEFAULT_AT, dry_run: bool = False) -> dict:
    """Collect signals, build proposals and (unless dry_run) append new PROPOSED events."""
    shadow_doc = _load_json(shadow, "shadow status")
    elements: list[dict] = []
    notes: list[str] = []
    for maker in (lambda: rule_q_degradation(learning, at),
                  lambda: rule_c4(c4, at),
                  lambda: rule_data_gaps(sources, at),
                  lambda: rule_c6(shadow_doc, c6_audit, _load_jsonl(c6_audit), at),
                  lambda: rule_coverage(shadow_doc, at)):
        produced, produced_notes = maker()
        elements.extend(produced)
        notes.extend(produced_notes)
    current = prop.state(registry) if Path(registry).is_file() else {}
    existing_keys = {proposal["dedupe_key"]: proposal_id
                     for proposal_id, proposal in current.items()}
    created, skipped = [], []
    seen: dict = {}
    for element in elements:
        key = element["dedupe_key"]
        if key in existing_keys:
            skipped.append({"dedupe_key": key, "proposal_id": existing_keys[key],
                            "reason": "already proposed",
                            "status": current[existing_keys[key]]["status"]})
            continue
        if key in seen:
            skipped.append({"dedupe_key": key, "proposal_id": seen[key],
                            "reason": "duplicate within the scan"})
            continue
        if dry_run:
            created.append({"proposal_id": element["proposal_id"], "kind": element["kind"],
                            "title": element["title"], "dedupe_key": key, "dry_run": True})
            seen[key] = element["proposal_id"]
            continue
        appended = prop.register(Path(registry), element)
        created.append({"proposal_id": appended["proposal_id"], "kind": appended["kind"],
                        "title": appended["title"], "dedupe_key": key})
        seen[key] = appended["proposal_id"]
    counts_kind = {kind: 0 for kind in prop.KINDS}
    for element in elements:
        counts_kind[element["kind"]] += 1
    return {"schema": "AMFE-PROPOSAL-SCAN/1.0.0", "at": at, "rules_version": RULE_VERSION,
            "registry": str(registry), "dry_run": bool(dry_run),
            "signals": {"proposals": len(elements), "by_kind": counts_kind},
            "created": created, "skipped": skipped, "notes": notes,
            "counts": {"created": len(created), "skipped": len(skipped)}}


# ----------------------------------------------------------------------------------------- CLI
def _print_report(report: dict) -> None:
    print(f"Прогон предложений ({report['at']}, rules v{report['rules_version']}): "
          f"сигналов {report['signals']['proposals']} {report['signals']['by_kind']}; "
          f"создано {report['counts']['created']}, пропущено {report['counts']['skipped']}")
    for item in report["created"]:
        print(f"  + {item['proposal_id']} {item['kind']}: {item['title']}")
    for item in report["skipped"]:
        print(f"  = {item['dedupe_key']} ({item['reason']}, status={item.get('status')})")
    for note in report["notes"]:
        print(f"  примечание: {note}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Proposal engine (G6-prep): signals -> PROPOSED proposals; humans approve")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                        help="append-only proposal journal (default build/registry/proposals.jsonl)")
    parser.add_argument("--at", default=DEFAULT_AT, help=f"event date (default {DEFAULT_AT})")
    parser.add_argument("--shadow", default=str(DEFAULT_SHADOW))
    parser.add_argument("--c6-audit", dest="c6_audit", default=str(DEFAULT_C6_AUDIT))
    parser.add_argument("--c4", default=str(DEFAULT_C4))
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES))
    parser.add_argument("--learning", default=str(DEFAULT_LEARNING),
                        help="learning registry JSONL (optional; Q rule is skipped if absent)")
    parser.add_argument("--scan", action="store_true", help="run the rules (default action)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="build proposals without writing them")
    parser.add_argument("--list", action="store_true", help="list proposals from the journal")
    parser.add_argument("--status", default=None, help="filter for --list")
    parser.add_argument("--kind", default=None, help="filter for --list")
    parser.add_argument("--approve", default=None, metavar="PROPOSAL_ID",
                        help="approve a proposal; requires --by (a human name)")
    parser.add_argument("--reject", default=None, metavar="PROPOSAL_ID",
                        help="reject a proposal; requires --by and --reason")
    parser.add_argument("--implement", default=None, metavar="PROPOSAL_ID",
                        help="mark an APPROVED proposal implemented; requires --by")
    parser.add_argument("--by", default=None, help="human name (required for approve/reject/implement)")
    parser.add_argument("--reason", default=None, help="rejection reason")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    registry = Path(args.registry)
    try:
        if args.approve or args.reject or args.implement:
            if not args.by:
                raise prop.ProposalError("human approval requires --by 'Real Name'")
            if args.approve:
                event = prop.approve(registry, args.approve, by=args.by, at=args.at)
                result = {"ok": True, "event": event}
            elif args.reject:
                if not args.reason:
                    raise prop.ProposalError("reject requires --reason")
                event = prop.reject(registry, args.reject, by=args.by, at=args.at,
                                    reason=args.reason)
                result = {"ok": True, "event": event}
            else:
                event = prop.implement(registry, args.implement, by=args.by, at=args.at)
                result = {"ok": True, "event": event}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1)
                  if args.json else
                  f"OK: {event['action']} {event['proposal_id']} by {args.by}")
            return 0
        if args.list:
            items = prop.list_proposals(registry, status=args.status, kind=args.kind) \
                if registry.is_file() else []
            if args.json:
                print(json.dumps({"proposals": items}, ensure_ascii=False, sort_keys=True, indent=1))
            else:
                for item in items:
                    print(f"{item['proposal_id']} {item['kind']:10s} {item['status']:10s} "
                          f"{item['title']}")
                print(f"всего: {len(items)}")
            return 0
        report = scan(registry=registry, shadow=Path(args.shadow),
                      c6_audit=Path(args.c6_audit), c4=Path(args.c4),
                      sources=Path(args.sources), learning=Path(args.learning),
                      at=args.at, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1))
        else:
            _print_report(report)
        return 0
    except (EngineError, prop.ProposalError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
