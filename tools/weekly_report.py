#!/usr/bin/env python3
"""AMFE weekly integration report: deterministic, offline status bulletin.

Reads only files of the repository and never touches the network:
  * the frozen protocol ``protocols/evaluation_protocol_v4.json`` (and its pin), the frozen R3
    specification, the frozen NF-SIM threshold table and the comparator composition;
  * the registered candidates, the last real evaluation report, the R2 data gate report and the
    automatic retrieval log (dense ALFRED vintages), the data plan and the forward registry.

It renders Markdown with: data/vintage status, protocol/R3 and hash-chain state, candidate and
admission-stage status, C1-C6 results, forward-registry state, open risks and next steps. The report
is a snapshot of committed state, not an evaluation: it assigns no status. There is no wall clock in
the body, so the same inputs and the same ``--date`` produce byte-identical output.

Usage:
  python tools/weekly_report.py --date 2026-11-12 --out build/weekly_report.md
  python tools/weekly_report.py --root /path/to/repo --date 2026-11-12   # Markdown on stdout
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = "AMFE-WEEKLY-INTEGRATION-REPORT"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def lf_sha256(path: Path | None) -> str | None:
    """SHA-256 of the LF-normalised bytes (the form the project's pins use)."""
    if path is None or not Path(path).is_file():
        return None
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def short(value: str | None, size: int = 12) -> str:
    return value[:size] if value else "—"


def read_pin(path: Path | None) -> str | None:
    """The hash recorded in a ``*.sha256`` pin file (first token)."""
    if path is None or not Path(path).is_file():
        return None
    try:
        parts = Path(path).read_text(encoding="utf-8").split()
    except OSError:
        return None
    return parts[0] if parts else None


def load_json(path: Path) -> tuple:
    """Return ``(document, error)``; a missing or broken input is recorded, never fatal."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"нет файла: {path}"
    except (OSError, ValueError) as exc:
        return None, f"ошибка чтения {path}: {exc!r}"[:300]


def load_jsonl(path: Path) -> tuple:
    """Return ``(records, errors)`` for a JSONL file; a missing file means no records."""
    records: list = []
    errors: list = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records, errors
    except OSError as exc:
        return records, [f"ошибка чтения {path}: {exc!r}"]
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            errors.append(f"строка {number}: {exc!r}")
    return records, errors


def _num(value, digits: int = 4) -> str:
    try:
        if abs(float(value)) >= 1e4 or (0 < abs(float(value)) < 1e-3):
            return f"{float(value):.4g}"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _cells(row: list) -> str:
    return "| " + " | ".join(str(cell) for cell in row) + " |"


def _table(header: list, rows: list) -> list:
    if not rows:
        return ["_нет данных_"]
    lines = [_cells(header), "|" + "|".join("---" for _ in header) + "|"]
    lines.extend(_cells(row) for row in rows)
    return lines


def _file_meta(root: Path, path: Path | None) -> dict:
    info = {"path": None, "exists": False, "sha256": None, "bytes": None}
    if path is None:
        return info
    info["path"] = str(Path(path).relative_to(root)).replace("\\", "/")
    info["exists"] = Path(path).is_file()
    if info["exists"]:
        info["bytes"] = Path(path).stat().st_size
        info["sha256"] = lf_sha256(path)
    return info


def collect(root: Path = ROOT) -> dict:
    """Load every input; never raises on a missing or broken file (recorded in ``errors``)."""
    root = Path(root)
    protocols = root / "protocols"
    named = {
        "protocol": protocols / "evaluation_protocol_v4.json",
        "protocol_pin": protocols / "evaluation_protocol_v4.sha256",
        "r3": protocols / "r3_specification.json",
        "r3_pin": protocols / "r3_specification.sha256",
        "nf_sim": protocols / "nf_sim_thresholds.json",
        "nf_sim_pin": protocols / "nf_sim_thresholds.sha256",
        "data_plan": protocols / "data_plan.json",
        "data_log": protocols / "data_log.json",
        "gate": protocols / "r2_gate_report.json",
        "evaluation": protocols / "first_real_evaluation_report.json",
        "registry": root / "registry" / "forecasts.jsonl",
    }
    files = {key: _file_meta(root, path) for key, path in named.items()}
    data: dict = {}
    errors: list = []
    for key in ("protocol", "r3", "nf_sim", "data_plan", "data_log", "gate", "evaluation"):
        document, error = load_json(named[key])
        data[key] = document
        if error:
            errors.append(f"{key}: {error}")
        if key in ("protocol", "r3", "nf_sim") and document is not None:
            files[f"{key}_pin"]["value"] = read_pin(named[f"{key}_pin"])
    # composition and candidates are referenced by the evaluation report; fall back to the newest file
    composition_path = None
    candidates_path = None
    evaluation = data.get("evaluation") or {}
    composition_ref = (evaluation.get("composition") or {}).get("file")
    if composition_ref:
        composition_path = protocols / composition_ref
    if composition_path is None or not composition_path.is_file():
        found = sorted(protocols.glob("comparator_composition_*.json"))
        composition_path = found[-1] if found else None
    candidates_ref = (evaluation.get("candidates") or {}).get("file")
    if candidates_ref:
        candidates_path = protocols / candidates_ref
    if candidates_path is None or not candidates_path.is_file():
        found = sorted(protocols.glob("candidates_*.json"))
        candidates_path = found[-1] if found else None
    for key, path, pin_name in (("composition", composition_path, "composition_pin"),
                                ("candidates", candidates_path, "candidates_pin")):
        named[key] = path
        named[pin_name] = path.with_suffix(".sha256") if path is not None else None
        files[key] = _file_meta(root, path)
        document, error = load_json(path) if path is not None else (None, "нет файла")
        data[key] = document
        if error:
            errors.append(f"{key}: {error}")
        pin_value = read_pin(named[pin_name]) if path is not None else None
        files[pin_name] = _file_meta(root, named[pin_name])
        files[pin_name]["value"] = pin_value
    registry, registry_errors = load_jsonl(named["registry"])
    data["registry"] = registry
    errors.extend(f"registry: {error}" for error in registry_errors)
    return {"root": root, "as_of": None, "files": files, "data": data, "errors": errors}


# ------------------------------------------------------------------------------------ hash chain
def hash_chain(collected: dict) -> list:
    """Rows ``(artifact, version, sha256, pin, status)`` for the frozen chain."""
    data = collected["data"]
    files = collected["files"]
    rows = []

    def add(label: str, document: dict | None, version_keys: tuple, file_key: str, pin_key: str):
        version = "—"
        if document:
            for key in version_keys:
                if document.get(key):
                    version = str(document[key])
                    break
        current = files[file_key]["sha256"]
        pin = files[pin_key].get("value")
        if current is None:
            status = "нет файла"
        elif pin is None:
            status = "нет пина"
        elif current == pin:
            status = "OK"
        else:
            status = "РАСХОЖДЕНИЕ"
        rows.append([label, version, short(current), short(pin), status])

    add("Протокол оценки v4", data.get("protocol"), ("version", "status"), "protocol", "protocol_pin")
    add("R3-спецификация", data.get("r3"), ("version", "status"), "r3", "r3_pin")
    add("NF-SIM (таблица порогов)", data.get("nf_sim"), ("study_id", "threshold_rule"), "nf_sim",
        "nf_sim_pin")
    add("Состав компаратора", data.get("composition"), ("document_id", "evaluation_date"),
        "composition", "composition_pin")
    add("Кандидаты", data.get("candidates"), ("document_id", "registered_on"), "candidates",
        "candidates_pin")
    return rows


def reference_drift(collected: dict) -> list:
    """Rows ``(artifact, value in the evaluation report, current value, status)``."""
    data = collected["data"]
    evaluation = data.get("evaluation") or {}
    files = collected["files"]
    rows = []

    def add(label: str, reported, current):
        reported_short = short(reported) if reported else "—"
        current_short = short(current) if current else "—"
        if not reported:
            status = "нет ссылки"
        elif current is None:
            status = "нет файла"
        elif reported == current:
            status = "OK"
        else:
            status = "РАСХОЖДЕНИЕ"
        rows.append([label, reported_short, current_short, status])

    add("Протокол v4", (evaluation.get("protocol") or {}).get("sha256"),
        files["protocol"]["sha256"])
    add("R3-спецификация", (evaluation.get("r3_specification") or {}).get("sha256"),
        files["r3"]["sha256"])
    add("Состав компаратора", (evaluation.get("composition") or {}).get("sha256"),
        files["composition"]["sha256"])
    add("Кандидаты", (evaluation.get("candidates") or {}).get("sha256"),
        files["candidates"]["sha256"])
    return rows


# ------------------------------------------------------------------------------------ sections
def _badge(block: dict | None) -> str:
    """Short deterministic state of a criterion block: PASS, a recorded status, or NO."""
    block = block or {}
    if block.get("pass") is True:
        return "PASS"
    status = block.get("status")
    return str(status) if status else "NO"


def _section_summary(collected: dict, as_of: str) -> list:
    data = collected["data"]
    protocol = data.get("protocol") or {}
    r3 = data.get("r3") or {}
    evaluation = data.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    criteria = admission.get("criteria") or {}
    log = data.get("data_log") or {}
    gate = data.get("gate") or {}
    registry = data.get("registry") or []
    vintage_entries = [e for e in log.get("entries", []) if e.get("vintage") == "set"]
    vintage_ok = bool(vintage_entries) and all(e.get("vintage_check") for e in vintage_entries) \
        and not log.get("failures")
    gate_passed = bool(gate.get("passed"))
    chain = hash_chain(collected)
    chain_ok = all(row[4] == "OK" for row in chain if row[4] != "нет пина")
    stage_flags = list((admission.get("stages") or {}).items())
    passed_stages = [name for name, value in stage_flags if value]
    text = [
        f"Отчёт: `{REPORT_ID}`",
        f"Дата отчёта (as_of): **{as_of}**",
        "Режим: детерминированный офлайн-снимок закоммиченного состояния; сеть не используется, "
        "статусы кандидатов отчёт не присваивает.",
        "",
        "## 1. Сводка",
        "",
        f"- **Данные**: плотный винтажный архив — {'OK' if vintage_ok else 'НЕПОЛНЫЙ'} "
        f"({len(vintage_entries)} vintage-рядов, "
        f"{sum(1 for e in vintage_entries if e.get('vintage_check'))} с vintage_check=true, "
        f"failures: {len(log.get('failures') or [])}); ворота R2 — "
        f"{'passed' if gate_passed else 'НЕ ПРОЙДЕНЫ'} "
        f"({(gate.get('totals') or {}).get('matched_pairs')} пар, "
        f"{(gate.get('totals') or {}).get('matched_pairs_pre_2000')} до 2000).",
        f"- **Протокол**: evaluation_protocol_v4 {protocol.get('version', '—')} "
        f"({protocol.get('status', '—')}); R3 {r3.get('version', '—')} ({r3.get('status', '—')}); "
        f"цепочка хешей — {'OK' if chain_ok else 'ЕСТЬ РАСХОЖДЕНИЯ'}.",
        f"- **Кандидат**: {evaluation.get('candidate', '—')} → статус "
        f"`{admission.get('status', 'нет отчёта')}`; data_class="
        f"`{evaluation.get('data_class', '—')}`; admission_allowed="
        f"`{admission.get('admission_allowed', '—')}`; пройдено стадий: "
        f"{len(passed_stages)}/{len(stage_flags)}.",
        f"- **Критерии**: " + ", ".join(
            f"{key} {_badge(criteria.get(key))}" for key in ("C1", "C2", "C3", "C4", "C5", "C6")
        ) + ".",
        f"- **Журнал будущих прогнозов**: {len(registry)} записей"
        + (" (пуст: C6 недоступен, forward-окно не начато)" if not registry else "") + ".",
        "- **Итог**: данные и протокол зафиксированы, REAL_TIME-оценка выполнена, допуск не "
        "присвоен; кандидат остаётся `CANDIDATE`." if evaluation else
        "- **Итог**: отчёт первой реальной оценки отсутствует.",
        "",
    ]
    return text


def _section_data(collected: dict) -> list:
    data = collected["data"]
    plan = data.get("data_plan") or {}
    log = data.get("data_log") or {}
    gate = data.get("gate") or {}
    entries = log.get("entries") or []
    latest = [e for e in entries if e.get("vintage") == "latest"]
    vintages = [e for e in entries if e.get("vintage") == "set"]
    planned = [d for s in plan.get("series", []) for d in (s.get("vintages") or [])]
    lines = ["## 2. Данные и винтажи", ""]
    lines.append(f"- План данных: `{plan.get('plan_id', '—')}` версия "
                 f"{plan.get('version', '—')}, rate limit {plan.get('rate_limit_seconds', '—')} с; "
                 f"плановых винтажных дат: {len(planned)}"
                 + (f" ({planned[0]}…{planned[-1]})" if planned else "")
                 + f", рядов с винтажами: {len(vintages)}.")
    lines.append(f"- Журнал данных: `protocols/data_log.json`, сформирован "
                 f"{log.get('generated_at', '—')}; записей {len(entries)}, "
                 f"failures {len(log.get('failures') or [])}.")
    lines.append("")
    lines.append("### Последние файлы рядов")
    lines.append("")
    rows = [[e.get("series"), e.get("rows"), e.get("last_observation"), e.get("bytes"),
             (e.get("sha256") or "")[:12]] for e in sorted(latest, key=lambda x: str(x.get("series")))]
    lines.extend(_table(["ряд", "строк", "последнее наблюдение", "байт", "sha256"], rows))
    lines.append("")
    lines.append("### Плотные винтажные наборы (ALFRED)")
    lines.append("")
    rows = []
    for entry in sorted(vintages, key=lambda x: str(x.get("series"))):
        actual = entry.get("vintage_dates") or []
        unmapped = entry.get("unmapped_planned_dates") or []
        rows.append([entry.get("series"), len(entry.get("planned_vintages") or []), len(actual),
                     len(unmapped), entry.get("vintage_count"), entry.get("rows"),
                     entry.get("vintage_check"), entry.get("bytes"),
                     (entry.get("sha256") or "")[:12]])
    lines.extend(_table(["ряд", "плановых", "фактических", "unmapped", "винтажей", "строк",
                         "vintage_check", "байт", "sha256"], rows))
    lines.append("")
    totals = gate.get("totals") or {}
    lines.append(f"### Ворота R2 (`protocols/r2_gate_report.json`, {gate.get('generated_at', '—')})")
    lines.append("")
    lines.append(f"passed = **{gate.get('passed')}**; пар: {totals.get('matched_pairs')} "
                 f"(до 2000: {totals.get('matched_pairs_pre_2000')}), "
                 f"расхождений: {totals.get('failures')}, исключено: {totals.get('excluded')}; "
                 f"допуск {gate.get('tolerance')}.")
    lines.append("")
    rows = [[s.get("series"), s.get("channel"), len(s.get("vintage_dates") or []),
             s.get("checked"), s.get("matched"), len(s.get("mismatches") or [])]
            for s in gate.get("series", [])]
    lines.extend(_table(["ряд", "канал", "дат", "checked", "matched", "mismatches"], rows))
    lines.append("")
    return lines


def _section_chain(collected: dict) -> list:
    data = collected["data"]
    lines = ["## 3. Протокол, R3 и цепочка хешей", ""]
    lines.extend(_table(["артефакт", "версия/статус", "sha256 (LF)", "пин", "статус"],
                        hash_chain(collected)))
    lines.append("")
    lines.append("### Ссылки отчёта первой реальной оценки")
    lines.append("")
    lines.extend(_table(["артефакт", "в отчёте", "сейчас", "статус"], reference_drift(collected)))
    lines.append("")
    r3 = data.get("r3") or {}
    open_items = r3.get("open_items") or []
    if open_items:
        sample = "; ".join(str(item).rstrip(".")[:120] for item in open_items[:3])
        lines.append(f"R3 open_items: {len(open_items)} пункт(ов); первые: {sample}.")
        lines.append("")
    return lines


def _section_candidates(collected: dict) -> list:
    data = collected["data"]
    candidates = data.get("candidates") or {}
    evaluation = data.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    protocol = data.get("protocol") or {}
    lines = ["## 4. Кандидаты и стадии допуска", ""]
    rows = []
    for candidate in candidates.get("candidates", []):
        rows.append([candidate.get("id"), ", ".join(candidate.get("targets") or []),
                     ", ".join(candidate.get("members") or []),
                     candidate.get("code_sha256", "")[:12]])
    lines.extend(_table(["кандидат", "цели", "члены", "code sha256"], rows))
    lines.append("")
    if not evaluation:
        lines.append("_Отчёт первой реальной оценки отсутствует: статусы и стадии не определены._")
        lines.append("")
        return lines
    lines.append(f"- Отчёт: `{evaluation.get('report_id', '—')}`, сформирован "
                 f"{evaluation.get('generated_at', '—')}; кандидат `{evaluation.get('candidate')}`, "
                 f"цель {evaluation.get('target')}, горизонт {evaluation.get('horizon')}, "
                 f"mode `{evaluation.get('mode')}`, data_class `{evaluation.get('data_class')}`.")
    lines.append(f"- Статус: **{admission.get('status')}**; admission_allowed = "
                 f"{admission.get('admission_allowed')}; {admission.get('data_class_note', '')}")
    lines.append(f"- Метод множественности: {admission.get('multiplicity_method', '—')}; "
                 f"минимум членов компаратора: {admission.get('min_comparator_members', '—')}.")
    lines.append("")
    lines.append("### Стадии")
    lines.append("")
    stages = admission.get("stages") or {}
    rules = (protocol.get("admission") or {}).get("stages") or {}
    rows = [[name, "пройдена" if value else "не пройдена", rules.get(name, "—")]
            for name, value in stages.items()]
    lines.extend(_table(["переход", "состояние", "правило протокола"], rows))
    lines.append("")
    return lines


def _criterion_line(criteria: dict, key: str, admission: dict) -> tuple:
    """(pass/status cell, detail) for one admission criterion of the last evaluation report."""
    block = criteria.get(key) or {}
    status = block.get("status")
    passed = block.get("pass")
    if key == "C1":
        detail = []
        for window, losses in sorted((block.get("windows") or {}).items()):
            for loss, values in sorted(losses.items()):
                detail.append(f"{window}/{loss} relative_loss="
                              f"{_num(values.get('relative_loss'))} (порог "
                              f"{_num(values.get('threshold'))})")
        return ("PASS" if passed else "FAIL", "; ".join(detail))
    if key == "C2":
        adjusted = []
        for window, losses in sorted((block.get("windows") or {}).items()):
            for loss, values in sorted(losses.items()):
                adjusted.append(f"{window}/{loss} adj.p={_num(values.get('adjusted_p_value'))}")
        return ("PASS" if passed else "FAIL",
                f"{block.get('multiplicity_method', '—')}; family_size="
                f"{block.get('family_size')}; " + "; ".join(adjusted))
    if key == "C3":
        return (str(status or "—"), str(block.get("reason") or "—")[:300])
    if key == "C4":
        parts = []
        for window, losses in sorted((block.get("per_window") or {}).items()):
            for loss, values in sorted(losses.items()):
                parts.append(f"{window}/{loss} {values.get('status')} "
                             f"(рецессионных начал {values.get('recession_origins')}, "
                             f"relative_loss={_num(values.get('relative_loss_recession'))})")
        return ("PASS" if passed else "FAIL", "; ".join(parts) if block.get("applies") else
                "не применимо")
    if key == "C5":
        detail = []
        for window, losses in sorted((block.get("windows") or {}).items()):
            for loss, values in sorted(losses.items()):
                detail.append(f"{window}/{loss} p={_num(values.get('p_value'))}")
        return ("PASS" if passed else "FAIL",
                f"{block.get('alternative', '—')}; " + "; ".join(detail))
    if key == "C6":
        return (str(status or ("PASS" if passed else "FAIL")),
                str(block.get("reason") or admission.get("c6") or "—")[:300])
    return (str(status or ("PASS" if passed else "FAIL")), "")


def _section_criteria(collected: dict) -> list:
    data = collected["data"]
    evaluation = data.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    criteria = admission.get("criteria") or {}
    protocol = data.get("protocol") or {}
    protocol_criteria = {c.get("id"): c for c in (protocol.get("admission") or {}).get("criteria", [])}
    lines = ["## 5. Критерии допуска C1–C6", ""]
    if not criteria:
        lines.append("_Критерии не рассчитаны: отчёт первой реальной оценки отсутствует._")
        lines.append("")
        return lines
    rows = []
    for key in ("C1", "C2", "C3", "C4", "C5", "C6"):
        cell, detail = _criterion_line(criteria, key, admission)
        grep = protocol_criteria.get(key) or {}
        rows.append([key, "да" if grep.get("gating") else "нет", cell, detail])
    lines.extend(_table(["критерий", "gating", "итог", "детали"], rows))
    lines.append("")
    multiplicity = (criteria.get("C2") or {}).get("multiplicity") or {}
    if multiplicity:
        lines.append(f"Множественность: метод {multiplicity.get('method')}, FWER "
                     f"{multiplicity.get('fwer')}, семейство {multiplicity.get('family_size')}, "
                     f"репликаций {multiplicity.get('replications')}, seed "
                     f"{multiplicity.get('seed')}; правило: {multiplicity.get('rule')}.")
        lines.append("")
    return lines


def _section_registry(collected: dict) -> list:
    data = collected["data"]
    records = data.get("registry") or []
    files = collected["files"]
    lines = ["## 6. Журнал будущих прогнозов (forward registry)", ""]
    lines.append(f"- Файл: `{files['registry'].get('path') or 'registry/forecasts.jsonl'}`, "
                 f"записей: **{len(records)}**.")
    if not records:
        lines.append("- Журнал пуст: forward-окно не начато, C6 недоступен. Правила полей — "
                     "`registry/README.md`; запись создаётся до публикации фактического значения.")
        lines.append("")
        return lines
    counts: dict = {}
    for record in records:
        candidate = str(record.get("candidate_id", "—"))
        counts[candidate] = counts.get(candidate, 0) + 1
    rows = [[name, count] for name, count in sorted(counts.items())]
    lines.extend(_table(["кандидат / контрольное плечо", "записей"], rows))
    last = records[-1]
    lines.append("")
    lines.append(f"- Последняя запись: `{last.get('candidate_id', '—')}` / {last.get('target', '—')} "
                 f"h={last.get('horizon', '—')} origin={last.get('origin', '—')}.")
    lines.append("")
    return lines


def _section_risks(collected: dict) -> list:
    data = collected["data"]
    files = collected["files"]
    evaluation = data.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    criteria = admission.get("criteria") or {}
    log = data.get("data_log") or {}
    gate = data.get("gate") or {}
    registry = data.get("registry") or []
    r3 = data.get("r3") or {}
    risks = list(collected["errors"])
    vintage_entries = [e for e in log.get("entries", []) if e.get("vintage") == "set"]
    if not vintage_entries or not all(e.get("vintage_check") for e in vintage_entries):
        risks.append("Плотный винтажный архив неполон или vintage_check не подтверждён: "
                     "REAL_TIME-класс данных под вопросом.")
    if log.get("failures"):
        risks.append(f"Журнал данных содержит {len(log['failures'])} failures — загрузка неполная.")
    if not gate.get("passed"):
        risks.append("Ворота R2 не пройдены: пара (ряд, дата) не подтверждена initial-release "
                     "проверкой; REAL_TIME/PSEUDO-классификация должна учитывать это.")
    if not evaluation:
        risks.append("Отчёт первой реальной оценки отсутствует: кандидаты и критерии не оценены.")
    else:
        if not (criteria.get("C1") or {}).get("pass"):
            risks.append("C1 не пройден: относительная потеря кандидата выше порога 0.97 на W1 и W2 "
                         "(кандидат не лучше компаратора) — статус CANDIDATE сохраняется.")
        if not (criteria.get("C2") or {}).get("pass"):
            risks.append("C2 не пройден: скорректированные p-значения (~1.0) не отвергают "
                         "нулевую гипотезу; семейство мало (family_size=1).")
        if (criteria.get("C3") or {}).get("status") == "NOT_IMPLEMENTED":
            risks.append("C3 (Model Confidence Set) не реализован; критерий негейтинговый, "
                         "это известное ограничение harness.")
        if not (criteria.get("C4") or {}).get("pass"):
            risks.append("C4 не пройден: на W1 рецессионных начал меньше 12 (результат "
                         "INCONCLUSIVE), на W2 CRPS-компонент не пройден.")
        if (criteria.get("C6") or {}).get("status") == "NO_RESOLVED_FORECASTS":
            risks.append("C6 недоступен: в forward-журнале нет разрешённых прогнозов.")
        if admission.get("status") != "ACTIVE":
            risks.append("Ни один переход стадии допуска не выполнен: "
                         f"{admission.get('status')} не меняется на данных W1/W2 (нужен C6).")
    if not registry:
        risks.append("Forward registry пуст (0 записей): forward-окно не начато, C6 недоступен, "
                     "незагрязнённая OOS-проверка не открыта.")
    drift = [row for row in reference_drift(collected) if row[3] == "РАСХОЖДЕНИЕ"]
    if drift:
        risks.append("Отчёт первой реальной оценки ссылается на версии артефактов, отличные от "
                     "текущих: " + ", ".join(row[0] for row in drift) + " (R3 и состав компаратора "
                     "обновлены после оценки, оценку не перезапускали).")
    frozen_gate = (r3.get("freeze_hashes") or {}).get("r2_gate_report")
    current_gate = files["gate"].get("sha256")
    if frozen_gate and current_gate and frozen_gate != current_gate:
        totals = gate.get("totals") or {}
        risks.append(f"R2-отчёт ворот после заморозки R3 имеет новый хеш "
                     f"(R3 фиксирует {short(frozen_gate)}, сейчас {short(current_gate)}: "
                     f"{totals.get('matched_pairs')} пар, "
                     f"{totals.get('matched_pairs_pre_2000')} до 2000); R3 не редактируется, "
                     f"факт фиксируется в этом отчёте.")
    open_items = r3.get("open_items") or []
    if open_items:
        risks.append(f"R3 open_items: {len(open_items)} пункт(ов), включая SPF-маппинг, DFM/FRED-MD "
                     "и чувствительность; они остаются ограничениями.")
    if not vintage_entries and not risks:
        risks.append("Нет данных о винтажном архиве в журнале — статус не определён.")
    lines = ["## 7. Риски", ""]
    if not risks:
        lines.append("- Открытых рисков не обнаружено.")
    for index, risk in enumerate(dict.fromkeys(risks), start=1):
        lines.append(f"{index}. {risk}")
    lines.append("")
    return lines


def _section_next_steps(collected: dict) -> list:
    data = collected["data"]
    evaluation = data.get("evaluation") or {}
    admission = evaluation.get("admission") or {}
    criteria = admission.get("criteria") or {}
    registry = data.get("registry") or []
    steps = []
    if not registry:
        steps.append("Открыть forward registry: первая запись `registry/forecasts.jsonl` до "
                     "публикации фактического значения (старт незагрязнённого forward-окна).")
    if evaluation and not (criteria.get("C1") or {}).get("pass"):
        steps.append("Не менять статус кандидата: C1/C2 не пройдены; ревизия кандидата/компаратора "
                     "допускается только новой версией протокола (v4 frozen).")
    steps.append("Повторить REAL_TIME-прогон W1+W2 для остальных зарегистрированных целей "
                 "(T2_GDP_GROWTH) и зарегистрировать прогнозы в forward registry.")
    steps.append("C6 — после 12 разрешённых прогнозов forward-журнала (coverage/калибровка) — "
                 "еженедельно пересчитывать по мере разрешения прогнозов.")
    steps.append("Перезапускать этот отчёт и сводную регрессию по расписанию "
                 "(workflow `.github/workflows/weekly-report.yml`).")
    steps.append("Следить за расхождениями цепочки хешей: при обновлении R3/композиции "
                 "перезапускать оценку, чтобы отчёт ссылался на актуальные версии.")
    lines = ["## 8. Следующие шаги", ""]
    for index, step in enumerate(steps, start=1):
        lines.append(f"{index}. {step}")
    lines.append("")
    return lines


def _section_sources(collected: dict) -> list:
    files = collected["files"]
    lines = ["## 9. Источники и хеши (LF-normalised sha256)", ""]
    rows = []
    for key in sorted(files):
        info = files[key]
        if not info.get("path"):
            continue
        rows.append([key, f"`{info['path']}`", "да" if info["exists"] else "нет",
                     info.get("bytes"), short(info.get("sha256"))])
    lines.extend(_table(["вход", "файл", "есть", "байт", "sha256"], rows))
    lines.append("")
    return lines


def build_markdown(root: Path = ROOT, as_of: str | None = None) -> str:
    """Render the deterministic Markdown bulletin for the state of ``root`` at ``as_of``."""
    as_of = as_of or utc_today()
    collected = collect(Path(root))
    lines = [f"# AMFE — недельный интеграционный отчёт ({as_of})", ""]
    lines.extend(_section_summary(collected, as_of))
    lines.extend(_section_data(collected))
    lines.extend(_section_chain(collected))
    lines.extend(_section_candidates(collected))
    lines.extend(_section_criteria(collected))
    lines.extend(_section_registry(collected))
    lines.extend(_section_risks(collected))
    lines.extend(_section_next_steps(collected))
    lines.extend(_section_sources(collected))
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic weekly integration report (offline)")
    parser.add_argument("--date", default=None, help="report date YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--out", default=None, help="write the Markdown here (default: stdout)")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    args = parser.parse_args(argv)
    as_of = args.date or utc_today()
    try:
        date.fromisoformat(as_of)
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {as_of!r}", file=sys.stderr)
        return 2
    markdown = build_markdown(Path(args.root), as_of)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"weekly report: {out} ({len(markdown)} bytes, as_of {as_of})")
    else:
        try:
            sys.stdout.write(markdown)
        except UnicodeEncodeError:  # a non-UTF-8 console (e.g. Windows cp1251)
            sys.stdout.buffer.write(markdown.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
