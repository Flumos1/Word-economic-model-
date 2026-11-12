#!/usr/bin/env python3
"""AMFE release checklist: offline readiness check for the stage-9 / 1.0 snapshot.

The checker never touches the network and never changes the repository: it reads the committed
artefacts, verifies the frozen hash chain, the repository hygiene rules, the integrity (and licence
sanitisation) of the reports, the local links of README/roadmap, and optionally runs the whole local
regression (``tests/test_*.py``). It writes one machine-readable report
(``protocols/release_check_report.json`` by default) and a Markdown summary.

Verdicts:
  * PASS        - every blocker and every warned check passed, the regression ran and is green;
  * CONDITIONAL - no blocker failed, but the regression was skipped or a warning check failed
                  (e.g. the first-real-evaluation report references a superseded frozen artefact);
  * FAIL        - at least one blocker failed (broken frozen pin, raw data in the index, invalid
                  report, broken README/roadmap link, red regression).

Exit code: 0 = PASS, 1 = FAIL, 2 = CONDITIONAL (so ``exit 0`` means "all PASS").

Usage:
  python tools/release_check.py                         # full check, runs the regression
  python tools/release_check.py --skip-tests=true       # artefact checks only (fast)
  python tools/release_check.py --date 2026-11-12 --out build/release_check.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = "AMFE-RELEASE-CHECK"
TOOL_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 900.0
RAW_EXTENSIONS = {".csv", ".tsv", ".zip", ".xlsx", ".xls", ".parquet", ".dta", ".feather", ".gz"}
ALLOWED_RAW_TRACKED = {"registry/forecasts.jsonl"}
GROUPS = ("frozen", "repository", "reports", "references", "documentation", "regression")
STATUSES = ("PASS", "CONDITIONAL", "FAIL", "SKIP")
SEVERITIES = ("blocker", "warning")
FORBIDDEN_NUMERIC_ARRAY = 8  # a list of more than this many numbers is treated as raw observations


# ------------------------------------------------------------------------------- small utilities
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lf_sha256(path: Path | None) -> str | None:
    """SHA-256 of the LF-normalised bytes (the normalisation the project's pins use)."""
    if path is None or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_pin(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        parts = Path(path).read_text(encoding="utf-8").split()
    except OSError:
        return None
    return parts[0] if parts else None


def load_json(path: Path | None) -> tuple:
    """Return ``(document, error)``; a missing or broken input is recorded, never fatal."""
    if path is None:
        return None, "path is None"
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"нет файла: {path}"
    except (OSError, ValueError) as exc:
        return None, f"ошибка чтения {path}: {exc!r}"[:300]


def short(value: str | None, size: int = 12) -> str:
    return value[:size] if value else "—"


def rel(root: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _check(check_id: str, group: str, severity: str, status: str, detail: str,
           evidence=None) -> dict:
    if status not in STATUSES:
        raise ValueError(status)
    if severity not in SEVERITIES:
        raise ValueError(severity)
    item = {"id": check_id, "group": group, "severity": severity, "status": status,
            "detail": detail}
    if evidence is not None:
        item["evidence"] = evidence
    return item


def _numeric_arrays(value, large_only: bool = True, _path: str = "") -> list:
    """Paths of numeric lists longer than FORBIDDEN_NUMERIC_ARRAY in a JSON document."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_numeric_arrays(child, large_only, f"{_path}/{key}"))
    elif isinstance(value, list):
        numbers = [item for item in value if isinstance(item, (int, float))
                   and not isinstance(item, bool)]
        if numbers and len(numbers) > (FORBIDDEN_NUMERIC_ARRAY if large_only else 0):
            found.append({"path": _path or "/", "length": len(numbers)})
        for index, child in enumerate(value):
            found.extend(_numeric_arrays(child, large_only, f"{_path}[{index}]"))
    return found


def discover_composition(protocols: Path, evaluation: dict | None) -> tuple:
    """Return ``(composition_path, pin_path)`` for the current comparator composition."""
    reference = ((evaluation or {}).get("composition") or {}).get("file")
    candidate = None
    if reference:
        candidate = Path(reference)
        if not candidate.is_absolute():
            if candidate.exists():
                pass
            elif (protocols / candidate.name).exists():
                candidate = protocols / candidate.name
            else:
                candidate = protocols / reference
    if candidate is None or not candidate.is_file():
        found = sorted(protocols.glob("comparator_composition_*.json"))
        candidate = found[-1] if found else None
    pin = None
    if candidate is not None:
        pin = candidate.with_suffix(".sha256")
    return candidate, pin


def collect_facts(root: Path = ROOT) -> dict:
    """Read every artefact the checks need; missing files are recorded as errors."""
    root = Path(root)
    protocols = root / "protocols"
    evaluation, eval_error = load_json(protocols / "first_real_evaluation_report.json")
    composition_path, composition_pin = discover_composition(protocols, evaluation)
    facts = {
        "root": root,
        "protocols": protocols,
        "evaluation": evaluation,
        "evaluation_error": eval_error,
        "gate": None, "gate_error": None,
        "data_log": None, "data_log_error": None,
        "data_plan": None, "data_plan_error": None,
        "r3": None, "r3_error": None,
        "protocol": None, "protocol_error": None,
        "nf_sim": None, "nf_sim_error": None,
        "composition": None, "composition_error": None,
        "candidates": None, "candidates_error": None,
        "composition_path": composition_path,
        "composition_pin": composition_pin,
        "paths": {},
        "errors": [],
    }
    simple = {
        "gate": protocols / "r2_gate_report.json",
        "data_log": protocols / "data_log.json",
        "data_plan": protocols / "data_plan.json",
        "r3": protocols / "r3_specification.json",
        "protocol": protocols / "evaluation_protocol_v4.json",
        "nf_sim": protocols / "nf_sim_thresholds.json",
        "composition": composition_path,
        "candidates": protocols / "candidates_2026-11-11.json",
    }
    for key, path in simple.items():
        facts["paths"][key] = path
        facts[f"{key}_sha"] = lf_sha256(path)
        document, error = load_json(path)
        facts[key] = document
        facts[f"{key}_error"] = error
        if error:
            facts["errors"].append(f"{key}: {error}")
    pins = {
        "protocol_pin": protocols / "evaluation_protocol_v4.sha256",
        "r3_pin": protocols / "r3_specification.sha256",
        "nf_sim_pin": protocols / "nf_sim_thresholds.sha256",
        "composition_pin": composition_pin,
        "licence_register": protocols / "licence_register.json",
        "nf_sim_config": protocols / "nf_sim_config.json",
        "registry": root / "registry" / "forecasts.jsonl",
        "spf_metadata": protocols / "spf_metadata.json",
        "readme": root / "README.md",
        "roadmap": None,
        "docs_83": None,
        "docs_84": None,
    }
    docs = root / "docs"
    roadmap = sorted(docs.glob("42_*.md"))
    pins["roadmap"] = roadmap[0] if roadmap else None
    integration = sorted(docs.glob("83_*.md"))
    pins["docs_83"] = integration[0] if integration else None
    licence_audit = sorted(docs.glob("84_*.md"))
    pins["docs_84"] = licence_audit[0] if licence_audit else None
    facts["paths"].update(pins)
    for key, path in pins.items():
        facts[f"{key}_sha"] = lf_sha256(path)
        facts[f"{key}_pin"] = read_pin(path) if key.endswith("_pin") else None
    return facts


# ----------------------------------------------------------------------------------- checks: frozen
def _frozen_pin_check(facts: dict, name: str, title: str, file_key: str, pin_key: str,
                      extra: dict | None = None) -> dict:
    path = facts["paths"].get(file_key)
    pin_path = facts["paths"].get(pin_key)
    digest = facts.get(f"{file_key}_sha")
    pin = facts.get(f"{pin_key}_pin")
    if digest is None:
        return _check(f"frozen.{name}", "frozen", "blocker", "FAIL",
                      f"{title}: файл отсутствует ({rel(facts['root'], path) if path else '—'})")
    if pin is None:
        return _check(f"frozen.{name}", "frozen", "blocker", "FAIL",
                      f"{title}: пин-файл отсутствует или пуст")
    evidence = {"file": rel(facts["root"], path), "sha256_lf": digest,
                "pin": pin, "pin_file": rel(facts["root"], pin_path)}
    if digest != pin:
        return _check(f"frozen.{name}", "frozen", "blocker", "FAIL",
                      f"{title}: sha256 не совпадает с пином ({short(digest)} != {short(pin)})",
                      evidence)
    detail = f"{title}: пин совпадает ({short(pin)})"
    if extra:
        for key, value in extra.items():
            if value is not None:
                evidence[key] = value
                detail += f"; {key}={value}"
    return _check(f"frozen.{name}", "frozen", "blocker", "PASS", detail, evidence)


def check_frozen(facts: dict) -> list:
    checks = [
        _frozen_pin_check(facts, "protocol_v4", "протокол оценки v4", "protocol", "protocol_pin",
                          extra={"version": (facts.get("protocol") or {}).get("version"),
                                 "status": (facts.get("protocol") or {}).get("status")}),
        _frozen_pin_check(facts, "r3_specification", "R3-спецификация", "r3", "r3_pin",
                          extra={"version": (facts.get("r3") or {}).get("version"),
                                 "status": (facts.get("r3") or {}).get("status")}),
        _frozen_pin_check(facts, "nf_sim", "NF-SIM таблица порогов", "nf_sim", "nf_sim_pin",
                          extra={"study_id": (facts.get("nf_sim") or {}).get("study_id"),
                                 "entries": len((facts.get("nf_sim") or {}).get("entries") or [])}),
        _frozen_pin_check(facts, "composition", "состав компаратора", "composition",
                          "composition_pin",
                          extra={"document_id": (facts.get("composition") or {}).get("document_id"),
                                 "evaluation_date": (facts.get("composition") or {}).get("evaluation_date")}),
    ]
    # cross references inside the frozen chain
    protocol = facts.get("protocol") or {}
    r3 = facts.get("r3") or {}
    nf_sim = facts.get("nf_sim") or {}
    composition = facts.get("composition") or {}
    v4_sha = facts.get("protocol_sha")
    r3_sha = facts.get("r3_sha")
    nf_sha = facts.get("nf_sim_sha")
    licence_sha = facts.get("licence_register_sha")
    sub = {
        "r3.frozen_protocol.sha256": (r3.get("frozen_protocol") or {}).get("sha256"),
        "r3.freeze_hashes.protocol_v4": (r3.get("freeze_hashes") or {}).get("protocol_v4"),
        "r3.freeze_hashes.nf_sim_thresholds": (r3.get("freeze_hashes") or {}).get("nf_sim_thresholds"),
        "composition.protocol.sha256": (composition.get("protocol") or {}).get("sha256"),
        "composition.r3_specification.sha256": (composition.get("r3_specification") or {}).get("sha256"),
        "composition.licence_register.sha256": (composition.get("licence_register") or {}).get("sha256"),
    }
    expected = {
        "r3.frozen_protocol.sha256": v4_sha,
        "r3.freeze_hashes.protocol_v4": v4_sha,
        "r3.freeze_hashes.nf_sim_thresholds": nf_sha,
        "composition.protocol.sha256": v4_sha,
        "composition.r3_specification.sha256": r3_sha,
        "composition.licence_register.sha256": licence_sha,
    }
    bad = {key: {"got": short(value), "expected": short(expected[key])}
           for key, value in sub.items() if value != expected[key]}
    if bad:
        checks.append(_check("frozen.chain", "frozen", "blocker", "FAIL",
                             "согласованность ссылок замороженной цепочки нарушена: "
                             + ", ".join(sorted(bad)), {"mismatches": bad}))
    else:
        checks.append(_check("frozen.chain", "frozen", "blocker", "PASS",
                             "замороженная цепочка согласована (v4 → R3 → composition → licence register)",
                             {"references": {key: short(value) for key, value in sub.items()}}))
    # the R3 spec freezes the gate report of the freeze date; a later report is expected, not an error
    frozen_gate = (r3.get("freeze_hashes") or {}).get("r2_gate_report")
    current_gate = facts.get("gate_sha")
    if frozen_gate is None or current_gate is None:
        checks.append(_check("frozen.r2_gate_fact", "frozen", "warning", "SKIP",
                             "не удалось сравнить хеш ворот R2 из замороженной R3 с текущим отчётом"))
    elif frozen_gate != current_gate:
        checks.append(_check("frozen.r2_gate_fact", "frozen", "warning", "FAIL",
                             "R3 заморожена с прежним отчётом ворот R2; текущий отчёт новее "
                             "(это ожидаемо после этапа 9 и фиксируется в docs/75)",
                             {"frozen": short(frozen_gate), "current": short(current_gate),
                              "note": (r3.get("r2_gate") or {}).get("note")}))
    else:
        checks.append(_check("frozen.r2_gate_fact", "frozen", "warning", "PASS",
                             "хеш отчёта ворот R2 совпадает с зафиксированным в R3"))
    return checks


# ------------------------------------------------------------------------------- checks: repository
def git_tracked_files(root: Path) -> tuple:
    """Return ``(files, error)`` from ``git ls-files``; error means the index is not available."""
    try:
        completed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True,
                                   text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"git недоступен: {exc!r}"
    if completed.returncode != 0:
        return [], f"git ls-files failed: {(completed.stderr or '').strip()[:200]}"
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()], None


def check_repository(facts: dict, tracked_files: list | None = None) -> list:
    root = facts["root"]
    checks = []
    tracked, error = (tracked_files, None) if tracked_files is not None else git_tracked_files(root)
    if error:
        checks.append(_check("repo.index_available", "repository", "warning", "SKIP",
                             f"git-индекс не проверен: {error}"))
        tracked = []
    else:
        checks.append(_check("repo.index_available", "repository", "warning", "PASS",
                             f"git-индекс прочитан: {len(tracked)} файлов"))
        under_data = [path for path in tracked if path == "data" or path.startswith("data/")]
        raw_tracked = [path for path in tracked
                       if Path(path).suffix.lower() in RAW_EXTENSIONS
                       and path not in ALLOWED_RAW_TRACKED]
        if under_data:
            checks.append(_check("repo.no_tracked_data", "repository", "blocker", "FAIL",
                                 f"в git-индексе {len(under_data)} файлов под data/ — сырые данные "
                                 f"не должны коммититься", {"files": under_data[:20]}))
        else:
            checks.append(_check("repo.no_tracked_data", "repository", "blocker", "PASS",
                                 "в git-индексе нет файлов под data/",
                                 {"tracked_files": len(tracked)}))
        if raw_tracked:
            checks.append(_check("repo.no_raw_archives", "repository", "blocker", "FAIL",
                                 "в git-индексе есть файлы сырых форматов",
                                 {"files": raw_tracked[:20]}))
        else:
            checks.append(_check("repo.no_raw_archives", "repository", "blocker", "PASS",
                                 "в git-индексе нет файлов сырых форматов "
                                 f"({', '.join(sorted(RAW_EXTENSIONS))})"))
    gitignore = root / ".gitignore"
    try:
        text = gitignore.read_text(encoding="utf-8")
    except OSError:
        text = ""
    missing = [pattern for pattern in ("/data/raw/", "/data/external/") if pattern not in text]
    if missing:
        checks.append(_check("repo.gitignore_data", "repository", "blocker", "FAIL",
                             "в .gitignore нет правил для сырых данных: " + ", ".join(missing)))
    else:
        checks.append(_check("repo.gitignore_data", "repository", "blocker", "PASS",
                             ".gitignore закрывает /data/raw/ и /data/external/"))
    return checks


# ---------------------------------------------------------------------------------- checks: reports
def check_reports(facts: dict) -> list:
    root = facts["root"]
    checks = []
    v4_sha = facts.get("protocol_sha")

    evaluation = facts.get("evaluation")
    if evaluation is None:
        checks.append(_check("reports.evaluation", "reports", "blocker", "FAIL",
                             f"отчёт первой реальной оценки отсутствует/не читается: "
                             f"{facts.get('evaluation_error')}"))
    else:
        problems = []
        if evaluation.get("report_id") != "AMFE-FIRST-REAL-EVALUATION":
            problems.append("report_id")
        if evaluation.get("data_class") != "REAL_TIME":
            problems.append(f"data_class={evaluation.get('data_class')}")
        if ((evaluation.get("protocol") or {}).get("sha256")) != v4_sha:
            problems.append("ссылка на протокол v4 не совпадает с текущим хешем")
        criteria = (evaluation.get("admission") or {}).get("criteria") or {}
        missing_criteria = [key for key in ("C1", "C2", "C3", "C4", "C5", "C6")
                            if key not in criteria]
        if missing_criteria:
            problems.append("нет критериев " + ", ".join(missing_criteria))
        if "admission" not in evaluation:
            problems.append("нет блока admission")
        if not evaluation.get("licence_note"):
            problems.append("нет licence_note о диджестах вместо значений")
        if problems:
            checks.append(_check("reports.evaluation", "reports", "blocker", "FAIL",
                                 "отчёт первой реальной оценки неполон: " + "; ".join(problems)))
        else:
            checks.append(_check("reports.evaluation", "reports", "blocker", "PASS",
                                 "отчёт первой реальной оценки корректен: "
                                 f"{evaluation.get('report_id')}, data_class=REAL_TIME, "
                                 f"C1–C6 на месте, licence_note есть",
                                 {"generated_at": evaluation.get("generated_at"),
                                  "candidate": evaluation.get("candidate"),
                                  "admission_status": (evaluation.get("admission") or {}).get("status")}))

    gate = facts.get("gate")
    if gate is None:
        checks.append(_check("reports.r2_gate", "reports", "blocker", "FAIL",
                             f"отчёт ворот R2 отсутствует/не читается: {facts.get('gate_error')}"))
    else:
        totals = gate.get("totals") or {}
        problems = []
        if gate.get("gate") != "R2":
            problems.append("gate != R2")
        if gate.get("passed") is not True:
            problems.append("passed != true")
        if (gate.get("protocol") or "") != "protocols/evaluation_protocol_v4.json":
            problems.append("неверная ссылка на протокол")
        if int(totals.get("matched_pairs") or 0) < 30:
            problems.append("меньше 30 пар")
        if int(totals.get("matched_pairs_pre_2000") or 0) < 10:
            problems.append("меньше 10 пар до 2000")
        if int(totals.get("failures") or 0) != 0:
            problems.append("есть расхождения")
        if not gate.get("licence_note"):
            problems.append("нет licence_note")
        if problems:
            checks.append(_check("reports.r2_gate", "reports", "blocker", "FAIL",
                                 "отчёт ворот R2 не прошёл проверку: " + "; ".join(problems)))
        else:
            checks.append(_check("reports.r2_gate", "reports", "blocker", "PASS",
                                 f"ворота R2: passed=true, {totals.get('matched_pairs')} пар "
                                 f"({totals.get('matched_pairs_pre_2000')} до 2000), 0 расхождений",
                                 {"generated_at": gate.get("generated_at"),
                                  "excluded": totals.get("excluded")}))

    data_log = facts.get("data_log")
    plan = facts.get("data_plan") or {}
    expected_series = [series.get("id") for series in plan.get("series", [])
                       if series.get("vintages")]
    if data_log is None:
        checks.append(_check("reports.data_log", "reports", "blocker", "FAIL",
                             f"журнал данных отсутствует/не читается: {facts.get('data_log_error')}"))
    else:
        entries = data_log.get("entries") or []
        sets = [entry for entry in entries if entry.get("vintage") == "set"]
        latest = [entry for entry in entries if entry.get("vintage") == "latest"]
        problems = []
        if data_log.get("no_human_download") is not True:
            problems.append("no_human_download != true")
        if data_log.get("failures"):
            problems.append(f"failures={len(data_log['failures'])}")
        got_series = [entry.get("series") for entry in sets]
        missing_series = [series for series in expected_series if series not in got_series]
        extra_series = [series for series in got_series if series not in expected_series]
        if expected_series and (missing_series or extra_series):
            problems.append("винтажные наборы не совпадают с планом: нет "
                            + (", ".join(missing_series) or "—")
                            + "; лишние: " + (", ".join(extra_series) or "—"))
        for entry in sets:
            if not entry.get("vintage_check"):
                problems.append(f"{entry.get('series')}: vintage_check=false")
            if not (entry.get("vintage_count") or 0):
                problems.append(f"{entry.get('series')}: vintage_count=0")
            sha = entry.get("sha256") or ""
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                problems.append(f"{entry.get('series')}: sha256 не записан")
        if latest and not all(entry.get("last_observation") for entry in latest):
            problems.append("у latest-записей нет last_observation")
        if problems:
            checks.append(_check("reports.data_log", "reports", "blocker", "FAIL",
                                 "журнал данных неполон: " + "; ".join(problems[:8]),
                                 {"planned": len(expected_series), "logged": len(got_series),
                                  "missing": missing_series, "extra": extra_series}))
        else:
            checks.append(_check("reports.data_log", "reports", "blocker", "PASS",
                                 f"журнал данных: {len(entries)} записей, {len(sets)} винтажных "
                                 f"наборов, failures=0, vintage_check=true",
                                 {"generated_at": data_log.get("generated_at"),
                                  "series": sorted(got_series)}))
        # the gate report of the last run covers the series that existed then; a later plan expansion
        # must be followed by a new data-refresh run and a new gate report
        gate_series = sorted(entry.get("series") for entry in (facts.get("gate") or {}).get("series", []))
        uncovered = [series for series in expected_series if series not in gate_series]
        if uncovered:
            checks.append(_check("reports.plan_coverage", "reports", "warning", "FAIL",
                                 f"ворота R2 покрывают {len(gate_series)} из {len(expected_series)} "
                                 f"рядов плана; без ворот остаются: " + ", ".join(uncovered)
                                 + " (нужен перезапуск data-refresh и новый отчёт ворот)",
                                 {"covered": gate_series, "uncovered": uncovered}))
        else:
            checks.append(_check("reports.plan_coverage", "reports", "warning", "PASS",
                                 f"ворота R2 покрывают все {len(expected_series)} рядов плана"))

    # licence sanitisation: no raw third-party observations anywhere in the committed artefacts
    offenders = []
    for name, document in (("first_real_evaluation_report.json", evaluation),
                           ("r2_gate_report.json", gate),
                           ("data_log.json", data_log)):
        if document is None:
            continue
        arrays = _numeric_arrays(document)
        if arrays:
            offenders.append({"file": name, "numeric_arrays": arrays[:8]})
    first_eval = (facts.get("protocols") / "first_evaluation_report.json")
    pilot, _ = load_json(first_eval)
    if pilot is not None:
        arrays = _numeric_arrays(pilot)
        if arrays:
            offenders.append({"file": "first_evaluation_report.json", "numeric_arrays": arrays[:8]})
    spf_metadata, _ = load_json(facts["paths"].get("spf_metadata"))
    if spf_metadata is not None:
        if "series" in spf_metadata:
            offenders.append({"file": "protocols/spf_metadata.json", "problem": "содержит series"})
        for variable in spf_metadata.get("variables") or []:
            if "value" in variable or "values" in variable:
                offenders.append({"file": "protocols/spf_metadata.json",
                                  "problem": "содержит значения переменных"})
                break
    if offenders:
        checks.append(_check("reports.no_third_party_values", "reports", "blocker", "FAIL",
                             "в закоммиченных отчётах есть сырые значения/массивы наблюдений "
                             "(нарушение licence_policy: только производные статистики)",
                             {"offenders": offenders}))
    else:
        checks.append(_check("reports.no_third_party_values", "reports", "blocker", "PASS",
                             "отчёты санитизированы: только агрегаты/диджесты, без массивов "
                             "наблюдений (docs/84 F-1/F-2)"))
    return checks


# ------------------------------------------------------------------------------- checks: references
def check_references(facts: dict) -> list:
    checks = []
    evaluation = facts.get("evaluation") or {}
    root = facts["root"]
    current = {
        "protocol": facts.get("protocol_sha"),
        "r3_specification": facts.get("r3_sha"),
        "composition": facts.get("composition_sha"),
        "candidates": facts.get("candidates_sha"),
    }
    mismatches = {}
    for key, digest in current.items():
        reference = (evaluation.get(key) or {})
        if not reference:
            continue
        referenced = reference.get("sha256")
        if referenced != digest:
            mismatches[key] = {"referenced": short(referenced), "current": short(digest),
                              "referenced_version": reference.get("version")}
    if not evaluation:
        checks.append(_check("reports.references_current", "references", "warning", "SKIP",
                             "отчёт оценки отсутствует — ссылки не с чем сверять"))
    elif mismatches:
        checks.append(_check("reports.references_current", "references", "warning", "FAIL",
                             "отчёт первой реальной оценки ссылается на устаревшие версии "
                             "замороженных артефактов: " + ", ".join(sorted(mismatches))
                             + " (оценка выполнена до последних обновлений; нужен перезапуск "
                               "или запись о версии)", {"mismatches": mismatches}))
    else:
        checks.append(_check("reports.references_current", "references", "warning", "PASS",
                             "ссылки отчёта оценки совпадают с текущими замороженными артефактами"))
    # forward registry state
    registry = facts["paths"].get("registry")
    if registry is None or not registry.is_file():
        checks.append(_check("reports.forward_registry", "references", "warning", "FAIL",
                             "журнал будущих прогнозов отсутствует (registry/forecasts.jsonl)"))
    else:
        try:
            lines = [line for line in registry.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        except OSError as exc:
            lines = []
            checks.append(_check("reports.forward_registry", "references", "warning", "FAIL",
                                 f"журнал будущих прогнозов не читается: {exc!r}"))
        if not lines:
            checks.append(_check("reports.forward_registry", "references", "warning", "FAIL",
                                 "журнал будущих прогнозов пуст: forward-окно не начато, C6 "
                                 "недоступен (D-139)"))
        else:
            checks.append(_check("reports.forward_registry", "references", "warning", "PASS",
                                 f"журнал будущих прогнозов: {len(lines)} записей",
                                 {"records": len(lines)}))
    return checks


# ---------------------------------------------------------------------------------- checks: docs
def local_links(path: Path, root: Path) -> list:
    """Return ``(label, target, resolved)`` for local Markdown links of one document."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    links = []
    for match in re.finditer(r"(?<!\!)\[([^\]]*)\]\(([^)]+)\)", text):
        target = match.group(2).strip()
        if target.startswith(("http://", "https://", "mailto:", "#", "ftp://")):
            continue
        local = target.split("#")[0].strip()
        if not local:
            continue
        candidates = [Path(local)]
        if not Path(local).is_absolute():
            candidates = [Path(path).parent / local, Path(root) / local]
        resolved = next((candidate for candidate in candidates if candidate.exists()), None)
        links.append({"label": match.group(1), "target": target,
                      "resolved": resolved is not None})
    return links


def check_docs(facts: dict) -> list:
    root = facts["root"]
    checks = []
    required = {
        "README.md": facts["paths"].get("readme"),
        "docs/42 — дорожная карта": facts["paths"].get("roadmap"),
        "docs/83 — интеграция и отчёты (этап 9)": facts["paths"].get("docs_83"),
        "docs/84 — лицензионный аудит 2": facts["paths"].get("docs_84"),
    }
    missing = [title for title, path in required.items() if path is None or not Path(path).is_file()]
    if missing:
        checks.append(_check("docs.required", "documentation", "blocker", "FAIL",
                             "нет обязательных документов релиза: " + ", ".join(missing)))
    else:
        checks.append(_check("docs.required", "documentation", "blocker", "PASS",
                             "обязательные документы на месте: README, docs/42, docs/83, docs/84"))
    broken = []
    checked = 0
    for name, path in (("README.md", required["README.md"]),
                       ("docs/42", required["docs/42 — дорожная карта"])):
        if path is None:
            continue
        links = local_links(Path(path), root)
        checked += len(links)
        broken.extend({"document": name, "label": link["label"], "target": link["target"]}
                      for link in links if not link["resolved"])
    if broken:
        checks.append(_check("docs.links", "documentation", "blocker", "FAIL",
                             f"в README/дорожной карте {len(broken)} битых локальных ссылок",
                             {"checked": checked, "broken": broken[:20]}))
    else:
        checks.append(_check("docs.links", "documentation", "blocker", "PASS",
                             f"локальные ссылки README и дорожной карты разрешаются "
                             f"({checked} ссылок)"))

    checklist = sorted((root / "docs").glob("89_*.md")) if (root / "docs").is_dir() else []
    if checklist:
        checks.append(_check("docs.release_checklist", "documentation", "warning", "PASS",
                             f"опубликован релизный чеклист: docs/{checklist[-1].name}"))
    else:
        checks.append(_check("docs.release_checklist", "documentation", "warning", "FAIL",
                             "нет документа релизного чеклиста (docs/89_*.md)"))
    return checks


# ------------------------------------------------------------------------------- checks: regression
def discover_tests(root: Path) -> list:
    tests_dir = Path(root) / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted((path for path in tests_dir.glob("test_*.py") if path.is_file()),
                  key=lambda path: path.name)


def check_regression(root: Path, skip_tests: bool, timeout: float = DEFAULT_TIMEOUT) -> dict:
    if skip_tests:
        tests = discover_tests(root)
        return _check("tests.regression", "regression", "blocker", "SKIP",
                      f"локальная регрессия пропущена (--skip-tests): {len(tests)} тестовых файлов "
                      "не запускались", {"tests": len(tests), "ran": False})
    tests = discover_tests(root)
    if not tests:
        return _check("tests.regression", "regression", "blocker", "FAIL",
                      "в tests/ нет файлов test_*.py", {"ran": False, "total": 0})
    started = time.perf_counter()
    results = []
    for path in tests:
        one = time.perf_counter()
        try:
            completed = subprocess.run([sys.executable, str(path)], cwd=str(root),
                                       capture_output=True, text=True, timeout=timeout)
            status = "passed" if completed.returncode == 0 else "failed"
            tail = "\n".join(((completed.stdout or "") + (completed.stderr or "")).strip()
                             .splitlines()[-25:]) if status == "failed" else ""
        except subprocess.TimeoutExpired:
            status = "timeout"
            tail = f"timeout after {timeout:g} s"
        except OSError as exc:
            status = "failed"
            tail = f"could not run: {exc!r}"
        results.append({"file": rel(root, path), "status": status,
                        "duration_seconds": round(time.perf_counter() - one, 3),
                        "output_tail": tail[-4000:] if tail else None})
    duration = round(time.perf_counter() - started, 3)
    failed = [item for item in results if item["status"] != "passed"]
    evidence = {"ran": True, "total": len(results), "passed": len(results) - len(failed),
                "failed": len(failed), "duration_seconds": duration,
                "failures": [item["file"] for item in failed],
                "tests": results}
    if failed:
        first = failed[0]
        return _check("tests.regression", "regression", "blocker", "FAIL",
                      f"локальная регрессия красная: {len(failed)} из {len(results)} файлов упали "
                      f"({first['file']})", evidence)
    return _check("tests.regression", "regression", "blocker", "PASS",
                  f"локальная регрессия зелёная: {len(results)}/{len(results)} тестовых файлов "
                  f"за {duration:.1f} s", evidence)


# ------------------------------------------------------------------------------------- limitations
def build_limitations(facts: dict, checks: list) -> list:
    limitations = []
    evaluation = facts.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    criteria = admission.get("criteria") or {}
    gate = facts.get("gate") or {}
    r3 = facts.get("r3") or {}
    candidates = facts.get("candidates") or {}
    registry = facts["paths"].get("registry")
    records = 0
    if registry is not None and registry.is_file():
        records = len([line for line in registry.read_text(encoding="utf-8").splitlines()
                       if line.strip()]) if registry.stat().st_size else 0
    if evaluation:
        failed = [key for key in ("C1", "C2", "C3", "C4", "C5", "C6")
                  if (criteria.get(key) or {}).get("pass") is not True]
        limitations.append(
            "научный статус: ни один gating-критерий не пройден ("
            + ", ".join(failed) + f"); кандидат {evaluation.get('candidate')} — "
            f"{(admission.get('status') or '—')}, допуск не присвоен (отчёт "
            f"{evaluation.get('generated_at')}).")
        if (criteria.get("C3") or {}).get("status") == "NOT_IMPLEMENTED":
            limitations.append("C3 (Model Confidence Set) не реализован — негейтинговый критерий, "
                               "известное ограничение harness.")
        if (criteria.get("C6") or {}).get("status") == "NO_RESOLVED_FORECASTS":
            limitations.append("C6 недоступен: forward registry пуст, forward-окно не начато "
                               "(C6 появится не раньше чем через 12 разрешённых прогнозов).")
    if records == 0:
        limitations.append("журнал будущих прогнозов registry/forecasts.jsonl пуст (0 записей).")
    registered = [candidate.get("id") for candidate in candidates.get("candidates") or []]
    evaluated = evaluation.get("candidate")
    unevaluated = [candidate for candidate in registered if candidate != evaluated]
    if unevaluated:
        limitations.append("зарегистрированы, но ещё не оценены кандидаты: "
                           + ", ".join(unevaluated)
                           + "; отчёт первой реальной оценки покрывает только "
                           + (evaluated or "—") + ".")
    stale = [check for check in checks
             if check["id"] == "reports.references_current" and check["status"] == "FAIL"]
    if stale and stale[0].get("evidence", {}).get("mismatches"):
        limitations.append("отчёт первой реальной оценки ссылается на устаревшие версии "
                           "артефактов — для релизных утверждений нужен перезапуск оценки против "
                           "текущей замороженной цепочки или явная запись о версии.")
    frozen_gate = (r3.get("freeze_hashes") or {}).get("r2_gate_report")
    current_gate = facts.get("gate_sha")
    if frozen_gate and current_gate and frozen_gate != current_gate:
        totals = gate.get("totals") or {}
        limitations.append(
            f"в R3 зафиксирован прежний отчёт ворот R2 ({short(frozen_gate)}); текущий отчёт — "
            f"{totals.get('matched_pairs')} пар "
            f"({totals.get('matched_pairs_pre_2000')} до 2000), это ожидаемое обновление после "
            "D-138/D-139 и docs/75.")
    limitations.append("сырые третьи данные не коммитятся: CI-артефакты приватные, retention "
                       "90 дней; локальная регрессия требует скачанных data/raw (это by design).")
    limitations.append("REAL_TIME-оценка покрывает T1 (CPI г/г, h=1) на W1+W2; T2/T3 и W3 "
                       "остаются вне первой реальной оценки.")
    limitations.append("бюллетень tools/weekly_report.py и этот чеклист — статусные снимки, "
                       "а не замороженные документы и не присвоение допуска.")
    return list(dict.fromkeys(limitations))


# ---------------------------------------------------------------------------------- report render
def summarise(checks: list) -> tuple:
    blocker_fails = [check["id"] for check in checks
                     if check["severity"] == "blocker" and check["status"] == "FAIL"]
    warning_fails = [check["id"] for check in checks
                     if check["severity"] == "warning" and check["status"] == "FAIL"]
    skipped = [check["id"] for check in checks if check["status"] == "SKIP"]
    if blocker_fails:
        return "FAIL", 1, blocker_fails, warning_fails, skipped
    if warning_fails or skipped:
        return "CONDITIONAL", 2, blocker_fails, warning_fails, skipped
    return "PASS", 0, blocker_fails, warning_fails, skipped


def render_markdown(report: dict) -> str:
    lines = [
        "# AMFE — релизный чеклист",
        "",
        f"Отчёт: `{report['report_id']}` · сформирован {report['generated_at']}",
        f"**Статус: {report['status']}** (exit code {report['exit_code']})",
        "",
        f"Проверок: {report['counts']['checks']}, PASS: {report['counts']['passed']}, "
        f"FAIL: {report['counts']['failed']}, SKIP: {report['counts']['skipped']}.",
        "",
    ]
    if report["blockers"]:
        lines.append("## Блокеры (FAIL):")
        lines.extend(f"- {item}" for item in report["blockers"])
        lines.append("")
    if report["warnings"]:
        lines.append("## Предупреждения (CONDITIONAL):")
        lines.extend(f"- {item}" for item in report["warnings"])
        lines.append("")
    if report["skipped"]:
        lines.append("## Пропущено:")
        lines.extend(f"- {item}" for item in report["skipped"])
        lines.append("")
    for group in GROUPS:
        group_checks = [check for check in report["checks"] if check["group"] == group]
        if not group_checks:
            continue
        failed = [check for check in group_checks if check["status"] != "PASS"]
        group_status = "PASS" if not failed else ("FAIL" if any(
            check["status"] == "FAIL" and check["severity"] == "blocker" for check in group_checks)
            else "CONDITIONAL")
        lines.append(f"## {group}: {group_status}")
        lines.append("")
        lines.append("| проверка | статус | детали |")
        lines.append("|---|---|---|")
        for check in group_checks:
            lines.append(f"| `{check['id']}` | {check['status']} | {check['detail']} |")
        lines.append("")
    lines.append("## Известные ограничения")
    lines.append("")
    lines.extend(f"{index}. {item}" for index, item in enumerate(report["known_limitations"], 1))
    lines.append("")
    lines.append("## Как запускать")
    lines.append("")
    lines.append("```\npython tools/release_check.py                          # полный чеклист с регрессией\n"
                 "python tools/release_check.py --skip-tests=true       # только артефактные проверки\n"
                 "python tools/release_check.py --date 2026-11-12 --out build/release_check.json\n```")
    lines.append("")
    return "\n".join(lines)


def build_report(root: Path = ROOT, skip_tests: bool = False, timeout: float = DEFAULT_TIMEOUT,
                 generated_at: str | None = None) -> dict:
    """Run every check and assemble the report; deterministic given inputs, date and skip-tests."""
    root = Path(root).resolve()
    facts = collect_facts(root)
    checks = []
    checks.extend(check_frozen(facts))
    checks.extend(check_repository(facts))
    checks.extend(check_reports(facts))
    checks.extend(check_references(facts))
    checks.extend(check_docs(facts))
    checks.append(check_regression(root, skip_tests, timeout))
    status, exit_code, blockers, warnings, skipped = summarise(checks)
    counts = {
        "checks": len(checks),
        "passed": sum(1 for check in checks if check["status"] == "PASS"),
        "failed": sum(1 for check in checks if check["status"] == "FAIL"),
        "skipped": len(skipped),
    }
    report = {
        "report_id": REPORT_ID,
        "tool": "tools/release_check.py",
        "tool_version": TOOL_VERSION,
        "generated_at": generated_at or utc_now(),
        "root": str(root),
        "options": {"skip_tests": skip_tests, "timeout_seconds": timeout},
        "status": status,
        "exit_code": exit_code,
        "counts": counts,
        "blockers": blockers,
        "warnings": warnings,
        "skipped": skipped,
        "checks": checks,
        "known_limitations": build_limitations(facts, checks),
        "readme_version_note": "README: статус проекта — документация версии 0.1; релиз 1.0 "
                               "фиксируется этим чеклистом и docs/89.",
        "executed_tests": next((check.get("evidence", {}).get("total") for check in checks
                                if check["id"] == "tests.regression"), None),
    }
    report["markdown"] = render_markdown(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AMFE release checklist (offline readiness check)")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    parser.add_argument("--out", default=None,
                        help="JSON report path (default: <root>/protocols/release_check_report.json)")
    parser.add_argument("--markdown-out", default=None, help="also write the Markdown summary here")
    parser.add_argument("--skip-tests", nargs="?", const="true", default="false",
                        help="skip the local regression (--skip-tests / --skip-tests=false)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="per-test-file timeout, seconds")
    parser.add_argument("--date", default=None,
                        help="override generated_at with this date (YYYY-MM-DD) for reproducibility")
    parser.add_argument("--no-json", action="store_true", help="do not write the JSON report")
    args = parser.parse_args(argv)

    skip_tests = _as_bool(args.skip_tests)
    generated_at = None
    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
        generated_at = day.strftime("%Y-%m-%dT00:00:00Z")

    root = Path(args.root).resolve()
    report = build_report(root, skip_tests=skip_tests, timeout=args.timeout,
                          generated_at=generated_at)
    out = Path(args.out) if args.out else root / "protocols" / "release_check_report.json"
    if not args.no_json:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        markdown_path = Path(args.markdown_out)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(report["markdown"], encoding="utf-8")
    try:
        sys.stdout.write(report["markdown"])
    except UnicodeEncodeError:  # non-UTF-8 console
        sys.stdout.buffer.write(report["markdown"].encode("utf-8"))
    print(f"\nstatus: {report['status']} (exit {report['exit_code']}); report: {out}")
    return report["exit_code"]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


if __name__ == "__main__":
    sys.exit(main())
