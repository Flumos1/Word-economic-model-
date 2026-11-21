#!/usr/bin/env python3
"""G6/beta readiness audit of AMFE 2.0 (docs/196, wave 26).

The audit is an independent checklist, not a release ceremony: it verifies what is *actually*
closed (gates G1-G5), what still blocks beta, which claims are forbidden, and whether the public
documents match the artefacts.  Nothing is changed: the tool only reads committed files and
``build/`` artefacts (plus optional read-only runs of ``release_check`` and the product test
suites).

Gates (docs/164 §4.1):

  G1  contracts, module registry, API passports
  G2  bridges (price->MRIO, agri->MRIO), oil backtest, ABM->SFC, external G2 review
  G3  PIT calibration, quantity channel, registry->API, source registry, G3 external review
  G4  pre-registration activation, independent V2 review, human signature of docs/160
  G5  products: API (static+gateway), Client Pack v2, FLAKS MVP, Simulator UI
  G6  beta 2.0: external audit, beta release, first clients - the gate this audit prepares

Checks:

  A1 g1_modules         registry: all modules RESEARCH_ONLY, API passports match the registry
  A2 g2_evidence        bridges/backtest reports exist, G2 external review PASS with 5 CONFIRMED
  A3 g3_calibration     docs/158-163 + PIT evaluation + quantity channel + source registry +
                        G3 review status (CONFIRMED_WITH_NOTES allowed, counter drift noted)
  A4 g4_prereg          pre-registration status, independent V2 verdict, C4 label, human signature
  A5 g5_products        API/Client Pack/FLAKS/Simulator artefacts and (optionally) their tests
  A6 g6_beta            beta blockers: G4 not passed, C6 0/12 & 1/4, no beta artefact/sign-off,
                        release_check status, raw-data deviation, stale README/docs
  A7 admission          admission=false, no ACTIVE stage/claim anywhere in the artefacts
  A8 c6                 C6 journal: 0/12 forecasts, 1/4 periods, gate NOT_MET
  A9 signatures         sign-off registry: object SHA-256 recomputed, statuses valid, who signed
  A10 data_licence      source registry (VERIFIED/CATALOGUED/NOT_DOWNLOADED), licence fields,
                        raw-data policy and git-tracked exceptions
  A11 docs_consistency  README and docs/164 claims vs the artefacts (dates, "not started" claims)
  A12 release_check     optional read-only run of tools/release_check.py (status/blockers/warnings)

Output: ``build/beta_audit/report.json`` + ``report.md`` (deterministic: no clock field; the audit
date is an explicit constant/argument).  Exit codes: 0 = audit completed (any verdict),
2 = fail-closed (a required input is missing/unreadable, nothing is written).

CLI::

    python tools/beta_audit.py                      # full audit (runs product tests)
    python tools/beta_audit.py --no-tests --no-release-check --json
    python tools/beta_audit.py --out build/beta_audit --date 2026-11-20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "AMFE-BETA-AUDIT/1.0.0"
AUDIT_DATE = "2026-11-20"
DEFAULT_OUT = ROOT / "build" / "beta_audit"
SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO")
GATE_STATUSES = ("PASS", "PASS_WITH_NOTES", "BLOCKED", "NOT_READY", "NOT_STARTED", "FAIL")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NEGATIONS = ("не ", "нет ", "без ", "недопуст", "не является", "запрещ", "нельзя", "not ",
             "no ", "≠", "не присвоен", "не ставится")
FORBIDDEN_CLAIMS = (
    (re.compile(r"допуск\w*\s+присвоен"), "admission assigned"),
    (re.compile(r"модель\s+допущена"), "model admitted"),
    (re.compile(r'"status"\s*:\s*"ACTIVE"'), "candidate status ACTIVE"),
    (re.compile(r"статус\w*\s*[=:]\s*ACTIVE"), "candidate status ACTIVE"),
    (re.compile(r"гарантирован\w*\s+(?:доходност|прибыл)"), "guaranteed return"),
    (re.compile(r"beta\s+2\.0\s+(?:готова|готов)"), "beta 2.0 ready claim"),
    (re.compile(r"C6\s+(?:пройден|выполнен|met)", re.I), "C6 passed claim"),
    (re.compile(r"рекоменду\w*\s+покупать"), "trading recommendation"),
)
C6_REQUIRED_FORECASTS = 12
C6_REQUIRED_PERIODS = 4
PRODUCT_TEST_SUITES = (
    "tests.test_forecast_api",
    "tests.test_forecast_api_server",
    "tests.test_flaks_pilot",
    "tests.test_simulator_ui",
    "tests.test_client_pack_v2",
    "tests.test_signoff_registry",
)
DOCS = {
    "plan": "docs/164_План_и_статус_проекта.md",
    "g2_review": "docs/160_Внешняя_проверка_G2.md",
    "g3_review": "docs/168_Независимое_ревью_G3.md",
    "v2_review": "docs/172_Независимая_V2_оценки_0_3.md",
    "prereg_eval": "docs/170_Оценка_по_предрегистрации_0_3.md",
    "registry_doc": "docs/161_Реестр_API_паспорта.md",
    "sources_doc": "docs/162_Реестр_источников.md",
    "agri_validation": "docs/163_Агро_валидация.md",
    "quantity_doc": "docs/159_Количественный_канал.md",
    "pit_doc": "docs/158_PIT_калибровка_нефти.md",
    "api_doc": "docs/183_Forecast_API_шлюз.md",
    "flaks_doc": "docs/184_FLAKS_Procurement_MVP.md",
    "flaks_pilot_doc": "docs/189_FLAKS_пилот.md",
    "simulator_doc": "docs/192_Simulator_UI.md",
    "client_pack_doc": "docs/171_Client_Pack_v2.md",
}
ARTIFACTS = {
    "modules_report": "build/registry/modules_report.json",
    "api_index": "build/api/index.json",
    "api_openapi": "build/api/openapi.json",
    "p2_release": "build/p2_release/p2_release.json",
    "c6_report": "build/c6/c6_report.json",
    "signoff_registry": "protocols/signoff_registry.json",
    "prereg": "protocols/pit_preregistration_0_3.json",
    "prereg_eval": "protocols/pit_prereg_evaluation_report.json",
    "source_registry": "protocols/source_registry.json",
    "price_bridge": "build/market_bridge/bridge_report.json",
    "oil_backtest": "build/market_backtest/report.json",
    "agri_bridge": "build/agri_bridge/agri_bridge.json",
    "quantity_bridge": "build/quantity_bridge/quantity_report.json",
    "g2_review_json": "build/external_review_g2/review.json",
    "p2_review_json": "build/external_review/review.json",
    "geopolitics": "build/geopolitics/report.json",
}
RAW_ALLOWED = {
    "registry/forecasts.jsonl",
    "data/oil/brent_monthly_eia.csv",
    "data/oil/brent_monthly_eia.meta.json",
}
REQUIRED_FOR_AUDIT = ("modules_report", "api_index", "p2_release", "c6_report",
                      "signoff_registry", "prereg", "source_registry")

__all__ = ["AuditError", "build_audit", "render_markdown", "write_audit", "main", "SCHEMA",
           "DOCS", "ARTIFACTS", "GATE_STATUSES"]


class AuditError(RuntimeError):
    """A required input is missing/unreadable: fail closed (exit 2, nothing written)."""


# ------------------------------------------------------------------------------------ primitives
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuditError(f"{label} is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"{label} is unreadable: {path}: {error}") from None


def _read_text(path: Path, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AuditError(f"{label} is missing: {path}") from None
    except OSError as error:
        raise AuditError(f"{label} is unreadable: {path}: {error}") from None


def _finding(check: str, gate: str, severity: str, message: str,
             evidence: dict | None = None) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    digest = hashlib.sha1(message.encode("utf-8")).hexdigest()[:8]
    return {"id": f"{check}-{gate}-{severity}-{digest}", "check": check, "gate": gate,
            "severity": severity, "message": message, "evidence": evidence or {}}


def _check(check_id: str, title: str, findings: list, evidence: dict) -> dict:
    if any(item["severity"] in ("BLOCKER", "HIGH") for item in findings):
        status = "FAIL"
    elif findings:
        status = "WARN"
    else:
        status = "PASS"
    return {"id": check_id, "title": title, "status": status, "findings": findings,
            "evidence": evidence}


def _normalise(text: str) -> str:
    return re.sub(r"[*`_]", " ", text or "")


def _numbers(text: str, pattern: str) -> list[int]:
    return [int(match.group(1)) for match in re.finditer(pattern, text)]


def _git_tracked(repo: Path) -> dict:
    try:
        done = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        if done.returncode != 0:
            return {"available": False, "files": []}
        return {"available": True, "files": [line.strip() for line in done.stdout.splitlines()
                                             if line.strip()]}
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "files": []}


def _run_tests(suites=PRODUCT_TEST_SUITES, timeout: float = 900.0) -> dict:
    done = subprocess.run([sys.executable, "-m", "unittest", *suites], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=str(ROOT))
    combined = (done.stdout or "") + (done.stderr or "")
    match = re.search(r"Ran (\d+) tests", combined)
    failed = re.search(r"FAILED \(([^)]*)\)", combined)
    return {"suites": list(suites), "returncode": done.returncode,
            "ran": int(match.group(1)) if match else None,
            "ok": done.returncode == 0,
            "failed": failed.group(1) if failed else None,
            "tail": combined.strip().splitlines()[-1] if combined.strip() else ""}


def _run_release_check(repo: Path, timeout: float = 600.0) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "release_check.json"
        done = subprocess.run([sys.executable, str(repo / "tools" / "release_check.py"),
                               "--skip-tests", "--out", str(target)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=str(repo))
        if not target.is_file():
            return {"available": False, "returncode": done.returncode,
                    "stderr": (done.stderr or "").strip()[:300]}
        report = json.loads(target.read_text(encoding="utf-8"))
        return {"available": True, "status": report.get("status"),
                "blockers": report.get("blockers"), "warnings": report.get("warnings"),
                "counts": report.get("counts"), "returncode": done.returncode}


def _long_numeric_arrays(node, path="", limit=200, out=None) -> list:
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            _long_numeric_arrays(value, f"{path}.{key}" if path else str(key), limit, out)
    elif isinstance(node, list):
        numeric = [item for item in node if isinstance(item, (int, float))
                   and not isinstance(item, bool)]
        if len(numeric) >= limit:
            out.append({"path": path, "length": len(numeric)})
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                _long_numeric_arrays(value, f"{path}[{index}]", limit, out)
    return out


def _doc_claims_forbidden(text: str) -> list:
    hits = []
    normalised = _normalise(text)
    for pattern, description in FORBIDDEN_CLAIMS:
        for match in pattern.finditer(normalised):
            window = normalised[max(0, match.start() - 100): match.end() + 100].lower()
            if any(token in window for token in NEGATIONS):
                continue
            hits.append({"claim": description, "match": match.group(0),
                         "context": window.strip()[:160]})
    return hits


# ------------------------------------------------------------------------------------- A1 modules
def check_g1_modules(repo: Path, documents: dict) -> dict:
    findings, evidence = [], {}
    registry = documents["modules_report"]
    api = documents["api_index"]
    modules_present = list(registry.get("modules_present") or [])
    registered = registry.get("registered") or []
    statuses = sorted({str(item.get("status")) for item in registered})
    evidence.update({"registry_entries": registry.get("entries"),
                     "registered": len(registered), "modules_present": modules_present,
                     "all_research_only": registry.get("all_research_only"),
                     "statuses": statuses,
                     "api_modules": [item.get("module_id") for item in api.get("modules") or []],
                     "api_schema": api.get("schema"), "api_version": api.get("api_version")})
    if not modules_present:
        findings.append(_finding("A1", "G1", "BLOCKER", "the module registry lists no modules"))
    if registry.get("all_research_only") is not True or statuses not in ([], ["RESEARCH_ONLY"]):
        findings.append(_finding("A1", "G1", "BLOCKER",
                                 "a registry module is not RESEARCH_ONLY",
                                 {"statuses": statuses}))
    api_modules = [item.get("module_id") for item in api.get("modules") or []]
    if set(api_modules) != set(modules_present):
        findings.append(_finding("A1", "G1", "HIGH",
                                 "the API module passports do not match the registry",
                                 {"registry": modules_present, "api": api_modules}))
    passport_dir = repo / "build" / "api" / "modules"
    passports = sorted(path.name for path in passport_dir.glob("*.json")) \
        if passport_dir.is_dir() else []
    evidence["passport_files"] = passports
    if len(passports) < len(modules_present):
        findings.append(_finding("A1", "G1", "HIGH",
                                 f"only {len(passports)}/{len(modules_present)} module passports "
                                 "are materialised in build/api/modules", {"passports": passports}))
    if not (repo / DOCS["registry_doc"]).is_file():
        findings.append(_finding("A1", "G1", "HIGH", f"missing document: {DOCS['registry_doc']}"))
    if api.get("status") != "RESEARCH_ONLY":
        findings.append(_finding("A1", "G1", "HIGH",
                                 f"API status is {api.get('status')!r}, expected RESEARCH_ONLY"))
    return _check("A1", "G1: registry, module passports and API contract", findings, evidence)


# ---------------------------------------------------------------------------------- A2 g2 evidence
def check_g2_evidence(repo: Path, documents: dict) -> dict:
    findings, evidence = [], {}
    for name in ("price_bridge", "oil_backtest", "agri_bridge", "quantity_bridge"):
        path = repo / ARTIFACTS[name]
        evidence[name] = {"path": ARTIFACTS[name], "exists": path.is_file()}
        if not path.is_file():
            findings.append(_finding("A2", "G2", "BLOCKER",
                                     f"G2 artifact is missing: {ARTIFACTS[name]}"))
    review = documents.get("g2_review_json")
    if review is None:
        findings.append(_finding("A2", "G2", "BLOCKER",
                                 "the G2 external review document is missing (build/external_review_g2)"))
    else:
        summary = review.get("summary", {})
        verdicts = summary.get("verdicts", {})
        evidence["review"] = {"schema": review.get("schema"), "status": summary.get("status"),
                              "verdicts": verdicts,
                              "by_severity": summary.get("by_severity")}
        if summary.get("status") not in ("PASS", "PASS_WITH_FINDINGS"):
            findings.append(_finding("A2", "G2", "HIGH",
                                     f"the G2 external review status is {summary.get('status')!r}"))
        not_confirmed = [name for name, verdict in verdicts.items() if verdict != "CONFIRMED"]
        if not_confirmed:
            findings.append(_finding("A2", "G2", "HIGH",
                                     "the G2 external review does not confirm every track",
                                     {"not_confirmed": not_confirmed}))
    if not (repo / DOCS["g2_review"]).is_file():
        findings.append(_finding("A2", "G2", "HIGH", f"missing document: {DOCS['g2_review']}"))
    # the oil backtest must publish a losing/limited verdict, not a success story
    backtest = documents.get("oil_backtest")
    if backtest is not None and backtest.get("status") != "RESEARCH_ONLY":
        findings.append(_finding("A2", "G2", "HIGH",
                                 "the oil backtest is not marked RESEARCH_ONLY"))
    return _check("A2", "G2: bridges, backtest and their external review", findings, evidence)


# ------------------------------------------------------------------------------- A3 g3 calibration
def check_g3_calibration(repo: Path, documents: dict,
                         docs_text: dict | None = None) -> dict:
    findings, evidence = [], {}
    missing_docs = [name for name in ("pit_doc", "quantity_doc", "agri_validation")
                    if not (repo / DOCS[name]).is_file()]
    evidence["missing_docs"] = missing_docs
    for name in missing_docs:
        findings.append(_finding("A3", "G3", "HIGH", f"missing document: {DOCS[name]}"))
    eval_report = documents.get("prereg_eval")
    evidence["prereg_eval"] = {"present": eval_report is not None}
    if eval_report is None:
        findings.append(_finding("A3", "G3", "HIGH",
                                 "the pre-registration evaluation report is missing"))
    review_text = (docs_text or {}).get("g3_review") or _read_text(
        repo / DOCS["g3_review"], "G3 review document")
    evidence["g3_review_verdict"] = ("CONFIRMED_WITH_NOTES"
                                     if "CONFIRMED_WITH_NOTES" in review_text else
                                     "NOT_CONFIRMED" if "NOT_CONFIRMED" in review_text else
                                     "UNKNOWN")
    if evidence["g3_review_verdict"] == "NOT_CONFIRMED":
        findings.append(_finding("A3", "G3", "HIGH",
                                 "the G3 external review returned NOT_CONFIRMED"))
    source_counts = {}
    registry = documents["source_registry"]
    for item in registry.get("sources") or []:
        status = str(item.get("status"))
        source_counts[status] = source_counts.get(status, 0) + 1
    evidence["source_statuses"] = source_counts
    evidence["sources_total"] = len(registry.get("sources") or [])
    verified_total = sum(count for status, count in source_counts.items()
                         if str(status).startswith(("VERIFIED", "ACCESS_VERIFIED")))
    unverified = evidence["sources_total"] - verified_total
    if unverified:
        findings.append(_finding("A3", "G3", "MEDIUM",
                                 f"{unverified} of {evidence['sources_total']} sources are not "
                                 "VERIFIED: their numbers cannot be cited in client materials",
                                 {"statuses": source_counts}))
    quantity = documents.get("quantity_bridge") or {}
    evidence["quantity_channel"] = {"present": bool(quantity),
                                    "status": quantity.get("status")}
    if not quantity:
        findings.append(_finding("A3", "G3", "HIGH", "the quantity bridge artifact is missing"))
    return _check("A3", "G3: calibration, quantity channel, source registry and G3 review",
                  findings, evidence)


# --------------------------------------------------------------------------------- A4 g4 prereg
def check_g4_prereg(repo: Path, documents: dict,
                    docs_text: dict | None = None) -> dict:
    findings, evidence = [], {}
    prereg = documents["prereg"]
    status = str(prereg.get("status"))
    evidence["prereg"] = {"id": prereg.get("preregistration_id"),
                          "version": prereg.get("version"), "status": status,
                          "content_sha256": prereg.get("content_sha256")}
    if status != "ACTIVE":
        findings.append(_finding("A4", "G4", "BLOCKER",
                                 f"pre-registration {prereg.get('version')} is {status}: G4 needs an "
                                 "owner activation (the audit never activates it)",
                                 {"status": status}))
    v2_text = (docs_text or {}).get("v2_review") or _read_text(
        repo / DOCS["v2_review"], "V2 review document")
    not_confirmed = re.search(r"NOT[\s_]*CONFIRMED", v2_text)
    evidence["v2_verdict"] = ("NOT_CONFIRMED" if not_confirmed else
                              "CONFIRMED" if re.search(r"\bCONFIRMED\b", v2_text) else "UNKNOWN")
    if evidence["v2_verdict"] != "CONFIRMED":
        findings.append(_finding("A4", "G4", "BLOCKER",
                                 "the independent V2 verdict is not CONFIRMED",
                                 {"verdict": evidence["v2_verdict"]}))
    eval_report = documents.get("prereg_eval") or {}
    label = eval_report.get("label") or eval_report.get("verdict") or eval_report.get("status")
    evidence["c4_label"] = label
    c4_fail = "FAIL" in v2_text or "FAIL" in json.dumps(eval_report, ensure_ascii=False)
    evidence["c4_fail_evidence"] = c4_fail
    if not c4_fail:
        findings.append(_finding("A4", "G4", "HIGH",
                                 "the C4 FAIL evidence is not visible in the evaluation package"))
    signoff = _signoff_for_gate(documents["signoff_registry"], "G4")
    evidence["signoff"] = signoff
    if signoff is None or signoff.get("status") != "SIGNED_BY_HUMAN":
        findings.append(_finding("A4", "G4", "HIGH",
                                 "docs/160 (G4 signature object) is not signed by a human"))
    return _check("A4", "G4: pre-registration, independent V2 review and human signature",
                  findings, evidence)


def _signoff_for_gate(registry: dict, gate: str) -> dict | None:
    for entry in registry.get("signoffs") or []:
        if str(entry.get("gate")) == gate:
            return entry
    return None


# --------------------------------------------------------------------------------- A5 g5 products
def check_g5_products(repo: Path, documents: dict, tests: dict | None) -> dict:
    findings, evidence = [], {}
    api = documents["api_index"]
    evidence["api"] = {"schema": api.get("schema"), "api_version": api.get("api_version"),
                       "counts": api.get("counts"), "status": api.get("status"),
                       "policy": api.get("policy")}
    if int((api.get("counts") or {}).get("modules") or 0) < 5:
        findings.append(_finding("A5", "G5", "HIGH",
                                 "the API exposes fewer than five module passports",
                                 {"counts": api.get("counts")}))
    if api.get("policy", {}).get("no_point_forecast") is not True:
        findings.append(_finding("A5", "G5", "HIGH",
                                 "the API policy does not forbid point forecasts"))
    for name, relative in (
            ("api_gateway", "tools/forecast_api_server.py"),
            ("flaks_tool", "tools/flaks_procurement.py"),
            ("simulator_tool", "tools/simulator_ui.py"),
            ("client_pack_tool", "tools/client_pack_v2.py")):
        evidence[name] = {"path": relative, "exists": (repo / relative).is_file()}
        if not evidence[name]["exists"]:
            findings.append(_finding("A5", "G5", "HIGH", f"missing product tool: {relative}"))
    for name, relative in (("flaks_doc", DOCS["flaks_doc"]),
                           ("flaks_pilot_doc", DOCS["flaks_pilot_doc"]),
                           ("simulator_doc", DOCS["simulator_doc"]),
                           ("client_pack_doc", DOCS["client_pack_doc"]),
                           ("api_doc", DOCS["api_doc"])):
        evidence[name] = {"path": relative, "exists": (repo / relative).is_file()}
        if not evidence[name]["exists"]:
            findings.append(_finding("A5", "G5", "HIGH", f"missing product document: {relative}"))
    product_dirs = {
        "flaks_scenarios": repo / "build" / "flaks_procurement",
        "simulator_input": repo / "build" / "geopolitics" / "report.json",
        "client_pack_v2": repo / "build" / "client_pack" / "v2_7",
        "api_contract": repo / "build" / "api" / "index.json",
    }
    for name, path in product_dirs.items():
        exists = path.is_dir() or path.is_file()
        evidence[name] = {"path": str(path.relative_to(repo)).replace("\\", "/"),
                          "exists": exists}
        if not exists:
            findings.append(_finding("A5", "G5", "MEDIUM",
                                     f"product artifact is absent: {name}",
                                     {"path": evidence[name]["path"]}))
    if tests is not None:
        evidence["tests"] = tests
        if not tests.get("ok"):
            findings.append(_finding("A5", "G5", "HIGH",
                                     "the product test suites are not green",
                                     {"returncode": tests.get("returncode"),
                                      "failed": tests.get("failed"), "ran": tests.get("ran")}))
    return _check("A5", "G5: API, Client Pack v2, FLAKS MVP, Simulator UI", findings, evidence)


# ------------------------------------------------------------------------------------- A8 c6 gate
def check_c6(documents: dict) -> dict:
    findings, evidence = [], {}
    c6 = documents["c6_report"].get("c6") or {}
    resolved = int(c6.get("n_resolved") or 0)
    periods = int(c6.get("distinct_target_periods") or 0)
    evidence.update({"n_resolved": resolved,
                     "forecasts_required": C6_REQUIRED_FORECASTS,
                     "distinct_periods": periods,
                     "periods_required": C6_REQUIRED_PERIODS,
                     "gate": c6.get("gate"), "status_line": c6.get("status_line"),
                     "status_12_4": c6.get("status_12_4")})
    if resolved < C6_REQUIRED_FORECASTS or periods < C6_REQUIRED_PERIODS:
        findings.append(_finding("A8", "G6", "BLOCKER",
                                 f"C6 is not met: {resolved}/{C6_REQUIRED_FORECASTS} resolved "
                                 f"forecasts, {periods}/{C6_REQUIRED_PERIODS} distinct periods "
                                 f"(gate {c6.get('gate')})",
                                 {"status_line": c6.get("status_line")}))
    if str(c6.get("gate")) == "MET" and (resolved < C6_REQUIRED_FORECASTS
                                         or periods < C6_REQUIRED_PERIODS):
        findings.append(_finding("A8", "G6", "BLOCKER",
                                 "C6 gate claims MET while the counters are below the thresholds"))
    return _check("A8", "C6: resolved forecasts and distinct periods", findings, evidence)


# ------------------------------------------------------------------------------ A9 signatures
def check_signatures(repo: Path, documents: dict) -> dict:
    findings, evidence = [], {}
    registry = documents["signoff_registry"]
    rules = registry.get("rules") or {}
    entries = registry.get("signoffs") or []
    evidence["rules"] = {"agent_may_sign": rules.get("agent_may_sign"),
                         "fail_closed": rules.get("fail_closed")}
    if rules.get("agent_may_sign") is not False:
        findings.append(_finding("A9", "G4", "BLOCKER",
                                 "the sign-off registry allows an agent to sign"))
    rows = []
    for entry in entries:
        row = {"id": entry.get("id"), "gate": entry.get("gate"),
               "status": entry.get("status"), "date": entry.get("date")}
        obj = (entry.get("object") or {})
        path = repo / str(obj.get("path"))
        row["object_exists"] = path.is_file()
        if not path.is_file():
            findings.append(_finding("A9", "G4", "HIGH",
                                     f"the signature object is missing: {obj.get('path')}",
                                     {"entry": entry.get("id")}))
            row["object_sha_ok"] = False
        else:
            observed = sha256_file(path)
            row["object_sha_ok"] = observed == obj.get("sha256")
            if not row["object_sha_ok"]:
                findings.append(_finding("A9", "G4", "HIGH",
                                         f"the signature object SHA-256 does not recompute: "
                                         f"{obj.get('path')}",
                                         {"recorded": obj.get("sha256"), "observed": observed}))
        signature = entry.get("signature") or {}
        row["signer_type"] = signature.get("signer_type")
        row["name_present"] = bool(signature.get("name"))
        if entry.get("status") == "SIGNED_BY_HUMAN":
            if signature.get("signer_type") != "human" or not signature.get("name") \
                    or not entry.get("date"):
                findings.append(_finding("A9", "G4", "BLOCKER",
                                         "a SIGNED entry lacks a human signer name/type/date",
                                         {"entry": entry.get("id")}))
        rows.append(row)
    evidence["entries"] = rows
    signed_gates = sorted({row["gate"] for row in rows
                           if row["status"] == "SIGNED_BY_HUMAN" and row.get("object_sha_ok")})
    evidence["signed_gates"] = signed_gates
    pending = [row["id"] for row in rows if row["status"] == "PENDING_HUMAN_SIGNATURE"]
    evidence["pending"] = pending
    missing_gates = [gate for gate in ("G4", "G5", "G6") if gate not in signed_gates]
    evidence["gates_without_signature"] = missing_gates
    if pending:
        findings.append(_finding("A9", "G4", "MEDIUM",
                                 f"{len(pending)} signature(s) wait for a human",
                                 {"pending": pending}))
    for gate in missing_gates:
        findings.append(_finding("A9", gate, "HIGH" if gate == "G4" else "MEDIUM",
                                 f"no valid human signature for gate {gate}"))
    return _check("A9", "sign-off registry: objects, SHA-256, human signatures", findings, evidence)


# ------------------------------------------------------------------------------ A10 data/licence
def check_data_licence(repo: Path, documents: dict, tracked: dict | None) -> dict:
    findings, evidence = [], {}
    registry = documents["source_registry"]
    sources = registry.get("sources") or []
    statuses = {}
    for item in sources:
        status = str(item.get("status"))
        statuses[status] = statuses.get(status, 0) + 1
    evidence["source_statuses"] = statuses
    evidence["sources_total"] = len(sources)
    licence_missing = [item.get("source_id") for item in sources
                       if not (item.get("licence") or item.get("license"))]
    evidence["licence_missing"] = licence_missing
    if licence_missing:
        findings.append(_finding("A10", "G3", "HIGH",
                                 f"{len(licence_missing)} source(s) have no licence field",
                                 {"sources": licence_missing[:10]}))
    verified = sum(count for status, count in statuses.items()
                   if str(status).startswith(("VERIFIED", "ACCESS_VERIFIED")))
    evidence["verified_share"] = round(verified / len(sources), 4) if sources else 0.0
    if verified < 5:
        findings.append(_finding("A10", "G6", "MEDIUM",
                                 f"only {verified}/{len(sources)} sources are VERIFIED: the rest "
                                 "cannot be used in client-facing numbers",
                                 {"statuses": statuses}))
    # raw data policy: git-tracked files outside the documented allow-list
    tracked = tracked if tracked is not None else _git_tracked(repo)
    evidence["git_available"] = tracked.get("available")
    violations = []
    if tracked.get("available"):
        for path in tracked.get("files") or []:
            if path in RAW_ALLOWED:
                continue
            if path.startswith("data/") or Path(path).suffix.lower() in (
                    ".csv", ".tsv", ".zip", ".xlsx", ".dta", ".parquet"):
                violations.append(path)
    evidence["raw_tracked_violations"] = violations
    if violations:
        findings.append(_finding("A10", "G6", "BLOCKER",
                                 "raw third-party data is tracked by git outside the allow-list",
                                 {"files": violations[:20]}))
    allowed_tracked = [path for path in (tracked.get("files") or []) if path in RAW_ALLOWED]
    evidence["allow_listed_tracked"] = allowed_tracked
    if allowed_tracked:
        findings.append(_finding("A10", "G6", "MEDIUM",
                                 "the raw-data policy has committed exceptions (documented in "
                                 "release_check ALLOWED_RAW_TRACKED): a beta claim of 'no raw data "
                                 "in git' would be false",
                                 {"files": allowed_tracked}))
    gitignore = repo / ".gitignore"
    ignore_text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    evidence["gitignore_rules"] = {pattern: pattern in ignore_text
                                   for pattern in ("/data/raw/", "/data/external/", "build/")}
    missing = [pattern for pattern, present in evidence["gitignore_rules"].items() if not present]
    if missing:
        findings.append(_finding("A10", "G6", "HIGH",
                                 f".gitignore does not cover: {missing}"))
    return _check("A10", "data and licence risks (sources, licences, raw-data policy)",
                  findings, evidence)


# --------------------------------------------------------------------------- A11 docs consistency
def check_docs_consistency(repo: Path, documents: dict, readme_text: str | None = None) -> dict:
    findings, evidence = [], {}
    readme = readme_text if readme_text is not None else _read_text(repo / "README.md", "README")
    plan = _read_text(repo / DOCS["plan"], "plan document")
    evidence["readme"] = {"mentions_18_nov": "18 ноября 2026" in readme,
                          "mentions_20_nov": "20 ноября 2026" in readme,
                          "says_flaks_ahead": bool(re.search(r"FLAKS и Simulator UI впереди",
                                                             readme)),
                          "mentions_flaks_doc": "184_FLAKS" in readme or "FLAKS" in readme,
                          "mentions_simulator_doc": "192_Simulator" in readme}
    if evidence["readme"]["mentions_18_nov"]:
        findings.append(_finding("A11", "G6", "MEDIUM",
                                 "README states 'actual as of 18 November 2026' while the audit "
                                 "date is 20 November 2026: the status block is stale",
                                 {"audit_date": AUDIT_DATE}))
    if evidence["readme"]["says_flaks_ahead"] and (repo / DOCS["flaks_doc"]).is_file() \
            and (repo / DOCS["simulator_doc"]).is_file():
        findings.append(_finding("A11", "G6", "HIGH",
                                 "README says 'FLAKS and Simulator UI ahead' although docs/184/189/192 "
                                 "and their artifacts already exist: the public status understates "
                                 "the work and misleads the beta decision"))
    plan_checks = {
        "plan_g5_not_started": bool(re.search(r"FLAKS/Simulator\s+—\s+не начаты", plan)),
        "plan_g4_signature_pending": bool(re.search(
            r"\| \*\*G4\*\*[^\n]*подпись docs/160(?![^\n]*SIGNED_BY_HUMAN)", plan)),
        "plan_mentions_g6": "G6" in plan,
        "plan_date_18_nov": "2026-11-18" in plan or "18 ноября 2026" in plan,
    }
    evidence["plan"] = plan_checks
    if plan_checks["plan_g5_not_started"]:
        findings.append(_finding("A11", "G6", "HIGH",
                                 "docs/164 still says 'FLAKS/Simulator — not started' although the "
                                 "products exist: the plan's G5 row is stale"))
    if plan_checks["plan_g4_signature_pending"]:
        findings.append(_finding("A11", "G4", "MEDIUM",
                                 "docs/164 still describes the docs/160 signature as pending before "
                                 "the G4 word while the registry records SIGNED_BY_HUMAN"))
    if plan_checks["plan_date_18_nov"]:
        findings.append(_finding("A11", "G6", "MEDIUM",
                                 "docs/164 is dated 18 November 2026 and has not been revised for "
                                 "the 20 November state (G5 products, G4 signature)"))
    for key in ("flaks_doc", "simulator_doc", "client_pack_doc", "api_doc"):
        if not (repo / DOCS[key]).is_file():
            findings.append(_finding("A11", "G5", "HIGH", f"missing product document: {DOCS[key]}"))
    return _check("A11", "README and docs/164 match the artefacts", findings, evidence)


# ------------------------------------------------------------------------------ A7 admission
def check_admission(documents: dict, docs_text: dict | None = None) -> dict:
    findings, evidence = [], {}
    p2 = documents["p2_release"]
    registry = documents["modules_report"]
    api = documents["api_index"]
    c6 = documents["c6_report"]
    structures = {
        "p2.admission": p2.get("admission"),
        "p2.status": p2.get("status"),
        "registry.all_research_only": registry.get("all_research_only"),
        "api.policy.no_admission": (api.get("policy") or {}).get("no_admission"),
        "api.policy.no_point_forecast": (api.get("policy") or {}).get("no_point_forecast"),
        "c6.candidate_status": ((c6.get("pair") or {}).get("candidate")),
    }
    world = (p2.get("world") or {}).get("candidates") or {}
    structures["p2.admitted"] = world.get("admitted")
    stages = {}
    for key in ("t1", "regime"):
        block = world.get(key) or {}
        stages[key] = block.get("stages") or {}
        structures[f"p2.{key}.status"] = block.get("status")
    active_flags = [name for name, block in stages.items()
                    if (block or {}).get("LIMITED_DEPLOYMENT_to_ACTIVE")]
    evidence.update(structures)
    evidence["active_stage_flags_true"] = active_flags
    if structures["p2.admission"] is not False:
        findings.append(_finding("A7", "G6", "BLOCKER",
                                 "the P2 release manifest does not declare admission=false"))
    if structures["p2.admitted"] is not False:
        findings.append(_finding("A7", "G6", "BLOCKER",
                                 "a world candidate is marked admitted"))
    if active_flags:
        findings.append(_finding("A7", "G6", "BLOCKER",
                                 f"a candidate claims a stage to ACTIVE: {active_flags}"))
    for status in (structures["p2.t1.status"], structures["p2.regime.status"]):
        if status not in (None, "CANDIDATE", "SHADOW_TEST"):
            findings.append(_finding("A7", "G6", "HIGH",
                                     f"an unexpected candidate status is present: {status!r}"))
    if structures["api.policy.no_admission"] is not True:
        findings.append(_finding("A7", "G5", "HIGH", "the API policy does not set no_admission"))
    # negation-aware scan of the key public documents and artefacts
    payloads = {name: json.dumps(document, ensure_ascii=False)
                for name, document in documents.items() if isinstance(document, dict)}
    scanned = 0
    for name, payload in payloads.items():
        for hit in _doc_claims_forbidden(payload):
            findings.append(_finding("A7", "G6", "HIGH",
                                     f"{name}: forbidden claim ({hit['claim']}: {hit['match']})",
                                     hit))
            scanned += 1
    for name, text in (docs_text or {}).items():
        for hit in _doc_claims_forbidden(text):
            findings.append(_finding("A7", "G6", "HIGH",
                                     f"{name}: forbidden claim ({hit['claim']}: {hit['match']})",
                                     hit))
            scanned += 1
    evidence["forbidden_claims_found"] = scanned
    evidence["claims_not_allowed"] = sorted({hit["claim"]
                                             for hit in _doc_claims_forbidden(
                                                 " ".join((docs_text or {}).values()))} |
                                            {"модель допущена", "ACTIVE", "C6 пройден",
                                             "beta 2.0 готова", "гарантированная доходность"})
    return _check("A7", "admission=false and no ACTIVE/point-forecast claims",
                  findings, evidence)


# ------------------------------------------------------------------------------- A12 release_check
def check_release_check(release: dict | None) -> dict:
    findings, evidence = [], {}
    if release is None:
        evidence["skipped"] = True
        return _check("A12", "release_check snapshot", findings, evidence)
    evidence.update(release)
    if release.get("available") is False:
        findings.append(_finding("A12", "G6", "HIGH",
                                 "release_check could not produce a report",
                                 {"stderr": release.get("stderr")}))
    elif release.get("status") != "PASS":
        severity = "BLOCKER" if release.get("status") == "FAIL" else "MEDIUM"
        findings.append(_finding("A12", "G6", severity,
                                 f"release_check status is {release.get('status')}: "
                                 f"blockers={release.get('blockers')}, "
                                 f"warnings={release.get('warnings')}",
                                 {"counts": release.get("counts")}))
    return _check("A12", "release_check snapshot (status, blockers, warnings)", findings, evidence)


# ------------------------------------------------------------------------------- A6 g6 checklist
def check_g6_beta(findings_all: list, gates: dict, release: dict | None,
                  tracked: dict | None) -> dict:
    findings, evidence = [], {}
    blockers = []
    for gate, status in gates.items():
        if status in ("BLOCKED", "NOT_READY", "NOT_STARTED", "FAIL"):
            blockers.append({"gate": gate, "status": status})
    evidence["gate_blockers"] = blockers
    evidence["beta_artifacts"] = {
        "build/beta": (ROOT / "build" / "beta").exists(),
        "beta_release_doc": any((ROOT / "docs").glob("19[0-9]_*Beta*")),
    }
    if not (ROOT / "build" / "beta").exists():
        findings.append(_finding("A6", "G6", "MEDIUM",
                                 "no beta release artifact exists (build/beta): beta 2.0 has not "
                                 "been packaged"))
    ships = [item for item in findings_all if item["severity"] in ("BLOCKER", "HIGH")]
    evidence["blocking_findings"] = len(ships)
    evidence["counts_by_gate"] = {gate: sum(1 for item in findings_all
                                            if item["gate"] == gate)
                                  for gate in ("G1", "G2", "G3", "G4", "G5", "G6")}
    if release is not None and release.get("status") == "PASS" and not ships:
        status = "READY_FOR_REVIEW"
    elif gates.get("G4") in ("BLOCKED", "FAIL") or gates.get("G6") == "NOT_READY":
        status = "NOT_READY"
    else:
        status = "NOT_READY"
    evidence["verdict"] = status
    return _check("A6", "G6/beta readiness checklist (blockers, beta artifacts, verdict)",
                  findings, evidence)


# ---------------------------------------------------------------------------------- audit build
def _gate_status(findings: list, gate: str) -> str:
    own = [item for item in findings if item["gate"] == gate]
    if any(item["severity"] in ("BLOCKER", "HIGH") for item in own):
        return "FAIL"
    if any(item["severity"] in ("MEDIUM", "LOW", "INFO") for item in own):
        return "PASS_WITH_NOTES"
    return "PASS"


def build_audit(repo: Path | str = ROOT, run_tests: bool = True,
                run_release_check: bool = False, audit_date: str = AUDIT_DATE,
                overrides: dict | None = None) -> dict:
    repo = Path(repo)
    overrides = dict(overrides or {})
    documents = {}
    for name in REQUIRED_FOR_AUDIT + ("prereg_eval",):
        path = repo / ARTIFACTS[name]
        overlay = overrides.get(name)
        if overlay is not None:
            documents[name] = overlay
        else:
            documents[name] = _read_json(path, f"artifact {name}")
    for optional in ("price_bridge", "oil_backtest", "agri_bridge", "quantity_bridge",
                     "g2_review_json", "p2_review_json", "geopolitics", "api_openapi"):
        path = repo / ARTIFACTS[optional]
        if optional in overrides:
            documents[optional] = overrides[optional]
        elif path.is_file():
            documents[optional] = _read_json(path, f"artifact {optional}")
        else:
            documents[optional] = None
    docs_text = {}
    for name, relative in DOCS.items():
        if name in overrides.get("_docs", {}):
            docs_text[name] = overrides["_docs"][name]
        else:
            path = repo / relative
            docs_text[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    readme_text = overrides.get("_readme")
    if readme_text is None:
        readme_path = repo / "README.md"
        readme_text = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else None
    tracked = overrides.get("_git_tracked")
    if tracked is None:
        tracked = _git_tracked(repo)
    tests = overrides.get("_tests")
    if tests is None and run_tests:
        tests = _run_tests()
    release = overrides.get("_release_check")
    if release is None and run_release_check:
        release = _run_release_check(repo)
    docs_text_scan = {name: text for name, text in docs_text.items() if text}
    if readme_text:
        docs_text_scan["README.md"] = readme_text
    checks = [
        check_g1_modules(repo, documents),
        check_g2_evidence(repo, documents),
        check_g3_calibration(repo, documents, docs_text),
        check_g4_prereg(repo, documents, docs_text),
        check_g5_products(repo, documents, tests),
        check_admission(documents, docs_text_scan),
        check_c6(documents),
        check_signatures(repo, documents),
        check_data_licence(repo, documents, tracked),
        check_docs_consistency(repo, documents, readme_text),
        check_release_check(release),
    ]
    findings = [finding for check_item in checks for finding in check_item["findings"]]
    gates = {
        "G1": _gate_status(findings, "G1"),
        "G2": _gate_status(findings, "G2"),
        "G3": _gate_status(findings, "G3"),
        "G4": "BLOCKED" if any(item["gate"] == "G4" and item["severity"] == "BLOCKER"
                               for item in findings) else _gate_status(findings, "G4"),
        "G5": _gate_status(findings, "G5"),
        "G6": "NOT_READY",
    }
    g6 = check_g6_beta(findings, gates, release, tracked)
    checks.append(g6)
    findings = [finding for check_item in checks for finding in check_item["findings"]]
    by_severity = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        by_severity[finding["severity"]] += 1
    blockers = [finding for finding in findings if finding["severity"] in ("BLOCKER", "HIGH")]
    blocked_gates = [gate for gate, status in gates.items() if status != "PASS"]
    overall = "BETA_NOT_READY" if (blockers or gates["G4"] != "PASS"
                                   or gates["G6"] != "PASS") else "BETA_READY"
    audit = {
        "schema": SCHEMA,
        "audit_date": audit_date,
        "review_type": "INDEPENDENT_CHECKLIST",
        "gates": {gate: {"status": gates[gate],
                         "blocking_findings": sum(1 for item in findings
                                                  if item["gate"] == gate
                                                  and item["severity"] in ("BLOCKER", "HIGH")),
                         "all_findings": sum(1 for item in findings if item["gate"] == gate)}
                  for gate in ("G1", "G2", "G3", "G4", "G5", "G6")},
        "checks": checks,
        "summary": {
            "verdict": overall,
            "gates_not_passed": blocked_gates,
            "findings": len(findings),
            "blockers": len(blockers),
            "by_severity": by_severity,
            "blocker_ids": [item["id"] for item in blockers],
        },
        "blockers": sorted(
            [{"id": item["id"], "gate": item["gate"], "check": item["check"],
              "severity": item["severity"], "message": item["message"],
              "evidence": item["evidence"]} for item in blockers],
            key=lambda item: (SEVERITIES.index(item["severity"]), item["gate"])),
        "signoffs": {
            "signed_gates": checks[7]["evidence"].get("signed_gates"),
            "gates_without_signature": checks[7]["evidence"].get("gates_without_signature"),
            "entries": checks[7]["evidence"].get("entries"),
        },
        "c6": checks[6]["evidence"],
        "products": checks[4]["evidence"],
        "release_check": release,
        "claims_not_allowed": checks[5]["evidence"].get("claims_not_allowed"),
        "limits": [
            "checklist audit: artefacts and documents are compared, the economic validity of the "
            "models is not re-derived (external reviews docs/160 and docs/168 do that)",
            "the audit never activates pre-registrations, signs documents or changes statuses",
            "product tests are executed only when --tests is enabled; their result is a snapshot "
            "of the current working tree",
            "release_check is executed only with --release-check; its status depends on the "
            "current index and may change while other tracks commit",
            "raw-data policy has documented exceptions (EIA Brent series) - a beta claim of 'no "
            "raw data in git' would be false and must not be made",
            "beta readiness is a judgement over gates, blockers and signatures; this audit does "
            "not replace the owner's beta decision",
        ],
    }
    digest_payload = {key: value for key, value in audit.items() if key != "audit_date"}
    audit["digest"] = hashlib.sha256(json.dumps(digest_payload, ensure_ascii=False,
                                                sort_keys=True).encode("utf-8")).hexdigest()
    return audit


# ------------------------------------------------------------------------------------ rendering
def render_markdown(audit: dict) -> str:
    summary = audit["summary"]
    lines = ["# Beta readiness audit (G6-prep)", ""]
    lines.append(f"Audit date **{audit['audit_date']}** · schema `{audit['schema']}` · "
                 f"type **{audit['review_type']}**")
    lines.append("")
    lines.append(f"## Verdict: {summary['verdict']}")
    lines.append("")
    lines.append(f"Gates not passed: {', '.join(summary['gates_not_passed']) or 'none'} · "
                 f"findings {summary['findings']} · blockers {summary['blockers']} "
                 f"(BLOCKER {summary['by_severity']['BLOCKER']}, HIGH "
                 f"{summary['by_severity']['HIGH']}, MEDIUM {summary['by_severity']['MEDIUM']}, "
                 f"LOW {summary['by_severity']['LOW']})")
    lines.append("")
    lines.append("## Gate status")
    lines.append("")
    lines.append("| Gate | Status | Blocking findings | All findings |")
    lines.append("|---|---|---|---|")
    for gate, block in audit["gates"].items():
        lines.append(f"| {gate} | **{block['status']}** | {block['blocking_findings']} | "
                     f"{block['all_findings']} |")
    lines.append("")
    if audit["blockers"]:
        lines.append("## Blockers (BLOCKER/HIGH)")
        lines.append("")
        lines.append("| ID | Gate | Severity | Message |")
        lines.append("|---|---|---|---|")
        for item in audit["blockers"]:
            lines.append(f"| `{item['id']}` | {item['gate']} | **{item['severity']}** | "
                         f"{item['message']} |")
        lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Status | Findings |")
    lines.append("|---|---|---|")
    for check in audit["checks"]:
        lines.append(f"| {check['id']} {check['title']} | **{check['status']}** | "
                     f"{len(check['findings'])} |")
    lines.append("")
    lines.append("## C6 status")
    lines.append("")
    c6 = audit["c6"]
    lines.append(f"{c6.get('status_line')} · gate `{c6.get('gate')}` · resolved "
                 f"{c6.get('n_resolved')}/{c6.get('forecasts_required')}, distinct periods "
                 f"{c6.get('distinct_periods')}/{c6.get('periods_required')}")
    lines.append("")
    lines.append("## Signatures")
    lines.append("")
    lines.append(f"Signed gates: {', '.join(audit['signoffs'].get('signed_gates') or []) or 'none'}; "
                 f"without signature: "
                 f"{', '.join(audit['signoffs'].get('gates_without_signature') or []) or 'none'}")
    lines.append("")
    lines.append("## Claims that must not be made")
    lines.append("")
    for claim in audit.get("claims_not_allowed") or []:
        lines.append(f"- {claim}")
    lines.append("")
    lines.append("## Limits of this audit")
    lines.append("")
    for limit in audit["limits"]:
        lines.append(f"- {limit}")
    lines.append("")
    lines.append(f"Audit digest: `{audit['digest']}`")
    lines.append("")
    return "\n".join(lines)


def write_audit(audit: dict, out_dir: Path | str = DEFAULT_OUT,
                repo: Path | str = ROOT) -> dict:
    out = Path(out_dir)
    out = out if out.is_absolute() else Path(repo) / out
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    md_path = out / "report.md"
    payload = json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(audit), encoding="utf-8", newline="\n")
    return {"json": str(json_path).replace("\\", "/"),
            "markdown": str(md_path).replace("\\", "/"),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": len(payload.encode("utf-8"))}


# ------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G6/beta readiness audit: gates G1-G6, blockers, signatures, claims "
                    "(read-only, fail-closed)")
    parser.add_argument("--repo", default=str(ROOT), help="repository root")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--date", default=AUDIT_DATE, help="audit date (default: 2026-11-20)")
    parser.add_argument("--no-tests", action="store_true",
                        help="do not run the product test suites (fast snapshot)")
    parser.add_argument("--release-check", action="store_true",
                        help="also run tools/release_check.py and record its status")
    parser.add_argument("--no-write", action="store_true", help="compute only, write nothing")
    parser.add_argument("--json", action="store_true", help="print the audit to stdout")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        audit = build_audit(repo=Path(args.repo), run_tests=not args.no_tests,
                            run_release_check=args.release_check, audit_date=args.date)
    except AuditError as error:
        print(json.dumps({"status": "BLOCKED_AUDIT", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    written = {} if args.no_write else write_audit(audit, args.out, Path(args.repo))
    summary = {"status": audit["summary"]["verdict"],
               "gates": {gate: block["status"] for gate, block in audit["gates"].items()},
               "blockers": audit["summary"]["blockers"],
               "by_severity": audit["summary"]["by_severity"],
               "digest": audit["digest"], "out": written}
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
