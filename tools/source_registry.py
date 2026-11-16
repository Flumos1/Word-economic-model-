#!/usr/bin/env python3
"""CLI for the source registry (G3, docs/162): validate, list, missing, export, template, assumptions.

No network access.  The registry distinguishes **CATALOGUED** (record checked, no data retrieved),
**NOT_DOWNLOADED** (paid/licensed/manual, nothing retrieved) and **VERIFIED** (data actually
retrieved and checked, with a retrieval date and named evidence).  ``CATALOGUED`` is never reported
as verified; the ``--assumptions`` check only *reports* which assumptions are explicit and which
sources they could resolve to — it never promotes an assumption to observed data.

Usage:
    python tools/source_registry.py                          # validate (default)
    python tools/source_registry.py --validate --json
    python tools/source_registry.py --list --status VERIFIED
    python tools/source_registry.py --missing
    python tools/source_registry.py --json                   # API export
    python tools/source_registry.py --template NEW_SOURCE
    python tools/source_registry.py --assumptions
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from amfe.data import source_registry as sr  # noqa: E402

DEFAULT_REGISTRY = ROOT / "protocols" / "source_registry.json"


class AssumptionCheckError(RuntimeError):
    """An assumption/source link is broken (fail-closed for the --assumptions command)."""


def _relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def load_oil_assumptions() -> list[dict]:
    """Assumptions declared by the oil module (read-only import)."""
    from amfe.markets import oil

    rows = []
    for item in oil.assumptions():
        rows.append({"name": item.name, "unit": item.unit, "source": item.source,
                     "source_class": item.source_class})
    return rows


def load_agri_sources() -> list[dict]:
    """Source inventory of the agri track (data-only import; no data file is read)."""
    import agri_data

    return [{"source_id": entry.get("source_id"), "license": entry.get("license")}
            for entry in agri_data.SOURCES]


def check_assumptions(document: dict) -> dict:
    """Check that every oil/agri assumption has a source_id or is explicitly an ASSUMPTION."""
    registry_ids = {source["source_id"] for source in document["sources"]}
    oil_rows = []
    for item in load_oil_assumptions():
        resolution = sr.resolve_assumption_source(item["source"], document)
        explicit = (item["source_class"] == "assumption"
                    or resolution["explicit_assumption"])
        oil_rows.append({"name": item["name"], "source": item["source"],
                         "source_class": item["source_class"], "explicit_assumption": explicit,
                         "kind": resolution["kind"], "source_id": resolution["source_id"],
                         "suggestions": resolution["suggestions"]})
    unresolved_oil = [row for row in oil_rows
                      if not row["explicit_assumption"] and row["source_id"] is None]
    agri_rows = []
    for item in load_agri_sources():
        source_id = item["source_id"]
        agri_rows.append({"source_id": source_id, "in_registry": source_id in registry_ids})
    missing_agri = [row["source_id"] for row in agri_rows if not row["in_registry"]]
    report = {
        "schema": "AMFE-ASSUMPTION-CHECK/1.0.0",
        "oil_assumptions": len(oil_rows),
        "oil_explicit_assumption": sum(1 for row in oil_rows if row["explicit_assumption"]),
        "oil_with_source_id": sum(1 for row in oil_rows if row["source_id"] is not None),
        "oil_unresolved": unresolved_oil,
        "oil_rows": oil_rows,
        "agri_sources": len(agri_rows),
        "agri_linked": sum(1 for row in agri_rows if row["in_registry"]),
        "agri_missing": missing_agri,
        "ok": not unresolved_oil and not missing_agri,
    }
    return report


def _print_source_table(source_list: list[dict]) -> None:
    print(f"{'source_id':28s} {'status':15s} {'access':6s} {'retrieved_at':12s} publisher")
    for source in source_list:
        print(f"{source['source_id']:28s} {source['status']:15s} {source['access']:6s} "
              f"{str(source['retrieved_at'] or '-'):12s} {source['publisher']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Source registry (G3): validate/list/missing/export/template/assumptions")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                        help="registry JSON (default protocols/source_registry.json)")
    parser.add_argument("--validate", action="store_true", help="validate the registry")
    parser.add_argument("--list", action="store_true", help="list sources")
    parser.add_argument("--status", default=None, help="filter for --list: status")
    parser.add_argument("--access", default=None, help="filter for --list: access")
    parser.add_argument("--query", default=None, help="filter for --list: text query")
    parser.add_argument("--missing", action="store_true",
                        help="sources that are not VERIFIED (not usable as observed data)")
    parser.add_argument("--assumptions", action="store_true",
                        help="check oil/agri assumptions against the registry")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--template", nargs="?", const="NEW_SOURCE", default=None,
                        help="print a valid empty template for a new source (optional source_id)")
    args = parser.parse_args(argv)
    action_default = not any((args.validate, args.list, args.missing, args.assumptions,
                              args.json, args.template))
    path = Path(args.registry)
    exit_code = 0
    try:
        document = sr.load(path)
    except sr.SourceRegistryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.template is not None:
        try:
            template = sr.build_template(args.template)
        except sr.SourceRegistryError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(template, ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    report: dict = {}
    if args.validate or action_default:
        report["validation"] = sr.validate_file(path)
        if not report["validation"]["valid"]:
            exit_code = 1
    if args.list:
        selected = sr.find(document, status=args.status, access=args.access, query=args.query)
        report["list"] = selected
    if args.missing:
        report["missing"] = sr.missing(document)
    if args.assumptions:
        try:
            report["assumptions"] = check_assumptions(document)
        except (ImportError, OSError) as exc:
            print(json.dumps({"ok": False, "error": f"cannot read assumptions: {exc!r}"},
                             ensure_ascii=False))
            return 1
        if not report["assumptions"]["ok"]:
            exit_code = 1
    if args.json:
        if args.json and not any((args.validate, args.list, args.missing, args.assumptions)):
            report["export"] = sr.export_for_api(document, source_path=_relative(path))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1))
        return exit_code
    if action_default:
        validation = report["validation"]
        print(f"Источников: {validation['sources']}; valid={validation['valid']}; "
              f"по статусам: {validation['status_counts']}")
        print(f"  verified: {validation['verified']}")
        print(f"  по доступу: {validation['access_counts']}")
    if report.get("list") is not None:
        _print_source_table(report["list"])
    if "missing" in report:
        pending = report["missing"]["pending"]
        print(f"Не VERIFIED: {len(pending)}"
              + ("" if not pending else " (нельзя цитировать как наблюдения)"))
        for item in pending:
            print(f"  {item['source_id']}: {item['status']} — {item['reason']}")
    if "assumptions" in report:
        assumptions = report["assumptions"]
        print(f"Допущения oil: {assumptions['oil_assumptions']}, явные ASSUMPTION: "
              f"{assumptions['oil_explicit_assumption']}, с source_id: "
              f"{assumptions['oil_with_source_id']}; agri источники: "
              f"{assumptions['agri_linked']}/{assumptions['agri_sources']}")
        for row in assumptions["oil_rows"]:
            if row["source_id"] is None and not row["explicit_assumption"]:
                print(f"  НЕ РАЗРЕШЕНО: {row['name']} -> {row['source']!r}")
        for source_id in assumptions["agri_missing"]:
            print(f"  НЕ НАЙДЕН В РЕЕСТРЕ: {source_id}")
        print(f"  ok={assumptions['ok']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
