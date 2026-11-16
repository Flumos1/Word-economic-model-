#!/usr/bin/env python3
"""Register the G2 modules (oil / agri / finance / agentic / world P2 0.2) in the module registry.

The tool builds deterministic passports from the modules themselves (their exported passport /
model-card metadata) plus docs/tests/hashes; it does not modify module code, protocols or the
learning registry.  Records are appended to an append-only JSONL log; re-running against the same
log fails closed (duplicates are not overwritten).

Usage:
    python tools/register_modules.py                       # write build/registry/modules.jsonl
    python tools/register_modules.py --registry build/registry/modules.jsonl --json
    python tools/register_modules.py --out build/registry/modules_report.json
    python tools/register_modules.py --date 2026-11-16 --registry /tmp/modules.jsonl

Every passport is RESEARCH_ONLY: the modules are research prototypes, not admissions, forecasts or
investment advice.  Statuses above CANDIDATE require preregistered criteria and an independent review
and are not attempted here (see docs/157).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.learning import module_registry as mr  # noqa: E402

DEFAULT_REGISTRY = ROOT / "build" / "registry" / "modules.jsonl"
DEFAULT_REPORT = ROOT / "build" / "registry" / "modules_report.json"
DEFAULT_DATE = "2026-11-16"
P2_MANIFEST = ROOT / "build" / "p2_release" / "p2_release_0_2.json"

#: code manifests (paths relative to the repository root) for the combined code hash
CODE_FILES = {
    "oil": ["src/amfe/markets/__init__.py", "src/amfe/markets/contract.py",
                    "src/amfe/markets/engine.py", "src/amfe/markets/oil.py"],
    "agri": ["src/amfe/markets_agri/__init__.py", "src/amfe/markets_agri/contracts.py",
                 "src/amfe/markets_agri/soy.py", "src/amfe/markets_agri/balance.py",
                 "src/amfe/markets_agri/pricing.py", "src/amfe/markets_agri/seasonality.py"],
    "finance": ["src/amfe/markets_finance/__init__.py",
                        "src/amfe/markets_finance/contracts.py",
                        "src/amfe/markets_finance/factor_model.py",
                        "src/amfe/markets_finance/regimes.py",
                        "src/amfe/markets_finance/fx_rates.py",
                        "src/amfe/markets_finance/backtest.py"],
    "agentic": ["src/amfe/agentic/__init__.py", "src/amfe/agentic/simulator.py"],
    "world": ["src/amfe/world/io_core.py", "src/amfe/world/sfc_core.py",
                 "src/amfe/world/sfc_io.py", "tools/world_pipeline.py",
                 "tools/world_reports.py", "tools/p2_release.py"],
}


class RegistrationError(RuntimeError):
    """Missing module metadata or an inconsistent repository; the run is refused."""


def _require_files(paths: list[str]) -> list[dict]:
    records = []
    for relative in paths:
        path = ROOT / relative
        if not path.is_file():
            raise RegistrationError(f"required code file is missing: {relative}")
        records.append({"path": relative, "sha256": mr.sha256_file(path)})
    return records


def _module_passports() -> dict:
    """Import the module packages and read their declared passports/model cards."""
    import amfe.agentic  # noqa: F401
    from amfe import markets, markets_agri, markets_finance

    oil_docs = markets.modules_document()
    if not oil_docs:
        raise RegistrationError("amfe.markets exposes no module passport")
    oil = oil_docs[0]
    agri = markets_agri.module_passport()
    cards = markets_finance.model_cards()
    return {
        "oil": {"module_id": "oil.brent", "kind": "market", "version": "0.1.0",
                "display_name": "Oil: Brent response to a Hormuz closure (scenario module)",
                "summary": ("Deterministic monthly balance engine: supply/demand/bypass/stocks/"
                            "price; scenarios reported as a range. Research prototype, not a forecast."),
                "owner": "track-b", "data_status": "ASSUMPTION_BASED",
                "docs": ["docs/144_Товарные_модули_архитектура.md", "docs/147_Рынки_нефть.md"],
                "tests": ["tests/test_markets.py"],
                "entrypoints": ["amfe.markets.oil", "amfe.markets.contract.modules_document"],
                "limitations": ["declared assumptions, not calibrated parameters",
                                "three scenarios only; no real-data backtest or PIT vintages",
                                "scenario arithmetic, not a forecast, admission or investment advice"],
                "components": [item["scenario_id"] for item in oil.get("scenarios", [])],
                "note": "version derived from the track-B pilot release (docs/147)"},
        "agri": {"module_id": agri["module_id"], "kind": "agri",
                 "version": agri["version"], "display_name": agri["display_name"],
                 "summary": ("Agri archetype + soybean-in-Africa pilot: yield/weather, seasonality, "
                             "stocks-to-use, crush, substitutes, export restrictions, freight; range "
                             "sensitivity, not a forecast."),
                 "owner": "track-c", "data_status": "ASSUMPTION_BASED",
                 "docs": ["docs/148_Агро_соя.md"], "tests": ["tests/test_markets_agri.py"],
                 "entrypoints": ["amfe.markets_agri.SoyAgriModule", "amfe.markets_agri.run_pilot"],
                 "limitations": list(agri["limitations"]),
                 "components": list(agri.get("components", [])),
                 "note": "passport imported from amfe.markets_agri.module_passport()"},
        "finance": {"module_id": "markets.finance", "kind": "finance", "version": "0.1.0",
                    "display_name": "Finance layer: factor model, volatility/GMM regimes, FX/rates",
                    "summary": ("Research prototype of the finance layer: characteristic factor "
                                "model, volatility and Gaussian-mixture regimes, FX/rates "
                                "bookkeeping. Synthetic data; research only."),
                    "owner": "track-d", "data_status": "SYNTHETIC",
                    "docs": ["docs/149_Финансовый_слой.md"], "tests": ["tests/test_markets_finance.py"],
                    "entrypoints": ["amfe.markets_finance.model_cards",
                                    "amfe.markets_finance.demo"],
                    "limitations": ["model cards are RESEARCH_ONLY; no PIT fundamentals",
                                    "synthetic DGP is not an economic model; results do not transfer",
                                    "no transaction costs, capacity or borrow constraints in factors",
                                    "no FX forecasting: FX/rates block is an identity"],
                    "components": [card.to_dict().get("model_id", key) for key, card in cards.items()],
                    "note": "module-level passport; per-model cards registered in learning registry"},
        "agentic": {"module_id": "agentic.sim", "kind": "agentic", "version": "1.0.0",
                    "display_name": "Agentic simulator: households, firms, banks, state, AI agents",
                    "summary": ("Minimal ABM with a closed SFC ledger: income shock and AI "
                                "procurement share scenarios, double-entry consistency checks. "
                                "Research toy, not a model of the real economy."),
                    "owner": "track-e", "data_status": "SYNTHETIC",
                    "docs": ["docs/150_Агентный_симулятор.md"], "tests": ["tests/test_agentic.py"],
                    "entrypoints": ["amfe.agentic.run_demo", "amfe.agentic.AgenticSimulator"],
                    "limitations": ["research toy: mechanism design and accounting consistency only",
                                    "parameters are illustrative, not estimated",
                                    "no real-world behaviour, no forecast, no admission"],
                    "components": ["households", "firms", "banks", "state", "ai_agents"],
                    "note": "version from docs/150"},
        "world": {"module_id": "world.p2", "kind": "world", "version": "0.2.0",
                  "display_name": "World P2 0.2: OECD ICIO 2025 scenarios + SFC + release manifest",
                  "summary": ("World layer release P2 0.2: 8 scenarios on OECD ICIO 2025 "
                              "(81x50, 1995-2022), SFC checks and a research release manifest. "
                              "Research release, admission=false."),
                  "owner": "track-a", "data_status": "LOCAL_ONLY",
                  "docs": ["docs/146_P2_0.2.md"], "tests": ["tests/test_p2_release.py"],
                  "entrypoints": ["tools/world_pipeline.py", "tools/world_reports.py",
                                  "tools/p2_release.py"],
                  "limitations": ["research release, admission=false; no A/B/C/D admission",
                                  "OECD ICIO data live locally (not committed), release manifest "
                                  "is the provenance anchor",
                                  "scenario arithmetic with declared assumptions, not a forecast"],
                  "components": ["world_scenarios", "sfc_checks", "release_manifest"],
                  "note": "data_sha256 is the SHA-256 of the P2 0.2 release manifest"},
    }


def _passport(name: str, manifest: dict, *, created_at: str, data_sha256: str | None,
              data_status: str) -> dict:
    module = manifest["module_id"]
    for relative in CODE_FILES[name]:
        if not (ROOT / relative).is_file():
            raise RegistrationError(f"required code file is missing: {relative}")
    hashed = mr.code_hash({relative: ROOT / relative for relative in CODE_FILES[name]})
    return mr.build_passport(
        module_id=module, kind=manifest["kind"], version=manifest["version"],
        display_name=manifest["display_name"], summary=manifest["summary"],
        owner=manifest["owner"], code_sha256=hashed["sha256"], code_files=hashed["files"],
        data_sha256=data_sha256, data_status=data_status, created_at=created_at,
        docs=manifest["docs"], tests=manifest["tests"], entrypoints=manifest["entrypoints"],
        limitations=manifest["limitations"], components=manifest.get("components", ()),
        note=manifest.get("note", ""), status="RESEARCH_ONLY")


def build_report(*, registry: Path = DEFAULT_REGISTRY, created_at: str = DEFAULT_DATE,
                 p2_manifest: Path = P2_MANIFEST) -> dict:
    """Register all G2 modules into ``registry`` and return the deterministic JSON report."""
    manifests = _module_passports()
    data = {"world": None}
    if Path(p2_manifest).is_file():
        data["world"] = mr.sha256_file(Path(p2_manifest))
    code_warnings = []
    if data["world"] is None:
        code_warnings.append("P2 release manifest not found locally: world.p2 registered without "
                             "a data_sha256 (data_status=LOCAL_ONLY)")
    passports = {}
    for name, manifest in manifests.items():
        if name == "world":
            passports[name] = _passport(name, manifest, created_at=created_at,
                                        data_sha256=data["world"], data_status="LOCAL_ONLY")
        else:
            passports[name] = _passport(name, manifest, created_at=created_at, data_sha256=None,
                                        data_status=manifest["data_status"])
    order = ("oil", "agri", "finance", "agentic", "world")
    registered = []
    for name in order:
        passport = passports[name]
        mr.register(Path(registry), passport)
        registered.append({"module_id": passport["module_id"], "version": passport["version"],
                           "kind": passport["kind"], "status": passport["status"],
                           "owner": passport["owner"], "code_sha256": passport["code_sha256"],
                           "data_sha256": passport["data_sha256"],
                           "data_status": passport["data_status"]})
    current = mr.state(Path(registry))
    validation = mr.validate_registry(Path(registry))
    export = mr.export_for_api(Path(registry))
    return {
        "schema": "AMFE-MODULE-REGISTRATION/1.0.0", "date": created_at,
        "registry": str(registry), "entries": len(mr.load_entries(Path(registry))),
        "registered": registered, "warnings": code_warnings,
        "validation": {"valid": validation["valid"], "errors": validation["errors"]},
        "export_counts": export["counts"],
        "modules_present": sorted(current),
        "all_research_only": all(record["status"] == "RESEARCH_ONLY"
                                 for module, data_ in current.items()
                                 for record in data_["versions"].values()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Register G2 modules (oil/agri/finance/agentic/world P2 0.2) in the module "
                    "registry (append-only JSONL, RESEARCH_ONLY)")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                        help="append-only module registry JSONL (default build/registry/modules.jsonl)")
    parser.add_argument("--out", default=None,
                        help="write the JSON registration report to this path")
    parser.add_argument("--date", default=DEFAULT_DATE,
                        help=f"passport creation date (default {DEFAULT_DATE}, deterministic)")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    args = parser.parse_args(argv)
    try:
        report = build_report(registry=Path(args.registry), created_at=args.date)
    except (RegistrationError, mr.ModuleRegistryError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload + "\n", encoding="utf-8", newline="\n")
    if args.json:
        print(payload)
    else:
        print(f"Зарегистрировано модулей: {report['entries']} -> {report['registry']}")
        for item in report["registered"]:
            print(f"  {item['module_id']} v{item['version']} [{item['status']}] "
                  f"kind={item['kind']} owner={item['owner']} code={item['code_sha256'][:12]}…")
        print(f"  validation: {report['validation']['valid']}; "
              f"counts: {report['export_counts']}")
        for warning in report["warnings"]:
            print(f"  ВНИМАНИЕ: {warning}")
        if args.out:
            print(f"  отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
