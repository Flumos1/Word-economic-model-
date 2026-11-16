#!/usr/bin/env python3
"""Client delivery pack for one real market-module scenario (oil / Strait of Hormuz).

Turns the deterministic ``amfe.markets`` result for ``oil.brent`` / ``hormuz_closed_6m`` into the
package a paying research client receives: executive summary, scenario report, Excel price /
sensitivity / assumptions workbook, machine-readable JSON, offline dashboard, methodology
appendix, reproducibility manifest and licence/disclaimer (DOCX + PDF where LibreOffice exists).

Rules: every number is taken from the real ``MarketResult`` (``OilModule.run``) — nothing is
invented; aggregate/derived output only (no raw third-party observations); deterministic at a fixed
``--generated-at`` (DOCX/JSON/HTML; PDF is converted by LibreOffice and may carry converter
metadata); fail-closed (a missing module/scenario writes nothing); no external URLs, no JavaScript.

Usage:
    python tools/market_client_pack.py \
        --out build/client_pack/market_hormuz_6m \
        --scenario hormuz_closed_6m --client "Demo Bank" \
        --generated-at 2026-11-16T10:00:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCHEMA = "amfe.market_client_pack.v1"
PASSPORTS_SCHEMA = "amfe.market_client_pack.passports.v1"
REPRO_SCHEMA = "amfe.market_client_pack.repro.v1"
STATUS = "RESEARCH_ONLY"
DEFAULT_CLIENT = "Research client"
DEFAULT_SCENARIO = "hormuz_closed_6m"
DEFAULT_OUT = ROOT / "build" / "client_pack" / f"market_{DEFAULT_SCENARIO}"
DEFAULT_REGISTRY = ROOT / "build" / "registry" / "modules.jsonl"
DISCLAIMER = (
    "Research-only scenario arithmetic, not a forecast, not an admission decision and not "
    "investment advice. The Brent path is produced by the deterministic AMFE market engine from "
    "declared assumptions; the reported range is a range over sensitivity variants, not a "
    "confidence interval. No third-party observations are redistributed."
)


class MarketPackError(RuntimeError):
    """The market module/scenario is unavailable or the package cannot be built."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _datetime(generated_at: str | None) -> datetime:
    if not generated_at:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        return datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise MarketPackError(f"--generated-at must be YYYY-MM-DDTHH:MM:SSZ: {exc}") from exc


def _fmt(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------- module
def load_module(module=None):
    """Return the oil market module; fail-closed when ``src/amfe/markets`` is unavailable."""
    if module is not None:
        return module
    try:
        from amfe.markets.oil import OilModule
    except ImportError as exc:  # pragma: no cover - environment without the markets package
        raise MarketPackError(f"oil module package unavailable: {exc!r}") from exc
    return OilModule()


def run_result(module=None, scenario_id: str = DEFAULT_SCENARIO):
    module = load_module(module)
    try:
        scenario = module.scenario(scenario_id)
    except Exception as exc:
        raise MarketPackError(f"unknown scenario {scenario_id!r}: {exc}") from exc
    try:
        result = module.run(scenario_id)
    except Exception as exc:
        raise MarketPackError(f"scenario {scenario_id!r} failed: {exc}") from exc
    if result.status != STATUS:
        raise MarketPackError(f"unexpected market result status {result.status!r}")
    return module, scenario, result


# ------------------------------------------------------------------------------- module passports
def load_module_passports(registry_path: Path | None = None, module_id: str | None = None) -> dict:
    """Load module passports from the append-only module registry (fail-closed)."""
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY
    if not path.is_file():
        raise MarketPackError(
            f"module registry not found: {path}; run 'python tools/register_modules.py' first")
    try:
        from amfe.learning import module_registry
    except ImportError as exc:  # pragma: no cover - environment without src/amfe/learning
        raise MarketPackError(f"module registry package unavailable: {exc!r}") from exc
    try:
        export = module_registry.export_for_api(path)
    except Exception as exc:
        raise MarketPackError(f"cannot read module registry {path}: {exc!r}") from exc
    modules = list(export.get("modules") or [])
    if module_id is not None:
        modules = [item for item in modules if item.get("module_id") == module_id]
    if not modules:
        raise MarketPackError(
            f"module {module_id!r} not found in the registry {path}" if module_id
            else f"module registry {path} has no passports")
    return {"path": str(path).replace("\\", "/"), "sha256": _sha256(path),
            "schema": export.get("schema"), "export": export, "modules": modules}


def build_module_passports(out_path: Path, result, client: str, generated_at: str,
                           registry: dict) -> dict:
    """Write ``10_Module_Passports.json``: module/version/status/limitations/hashes.

    These are registry metadata only; every number in the pack still comes from ``MarketResult``.
    """
    module = next(item for item in registry["modules"]
                  if item.get("module_id") == result.module_id)
    passport = {
        "module_id": module.get("module_id"),
        "version": module.get("version"),
        "status": module.get("status"),
        "kind": module.get("kind"),
        "limitations": list(module.get("limitations") or []),
        "code_sha256": module.get("code_sha256"),
        "code_files": module.get("code_files"),
        "data_status": module.get("data_status"),
        "data_sha256": module.get("data_sha256"),
        "owner": module.get("owner"),
        "display_name": module.get("display_name"),
        "created_at": module.get("created_at"),
        "docs": list(module.get("docs") or []),
        "tests": list(module.get("tests") or []),
        "entrypoints": list(module.get("entrypoints") or []),
        "components": list(module.get("components") or []),
    }
    document = {
        "schema": PASSPORTS_SCHEMA,
        "kind": "module_passports",
        "generated_at": generated_at,
        "client": client,
        "status": STATUS,
        "disclaimer": DISCLAIMER,
        "module_ref": f"{passport['module_id']}@{passport['version']}",
        "registry": {"path": registry["path"], "sha256": registry["sha256"],
                     "schema": registry["schema"]},
        "module": passport,
        "all_modules": [{
            "module_id": item.get("module_id"), "version": item.get("version"),
            "status": item.get("status"), "kind": item.get("kind"),
            "code_sha256": item.get("code_sha256"),
            "limitations": list(item.get("limitations") or []),
        } for item in registry["export"]["modules"]],
        "numbers_source": "MarketResult (04_Machine_Readable.json): passports carry no scenario "
                          "numbers and no admission claims",
        "policy": {"read_only": True, "research_only": True, "no_admission": True,
                   "passports_are_metadata": True},
    }
    _write_json(out_path, document)
    return passport


# ------------------------------------------------------------------------------------ DOCX/PDF
def _docx(title: str, generated: datetime, subtitle: str = ""):
    from docx import Document
    document = Document()
    properties = document.core_properties
    properties.created = generated
    properties.modified = generated
    properties.last_modified_by = "AMFE market client pack"
    properties.author = "AMFE"
    properties.revision = 1
    document.add_heading(title, level=0)
    if subtitle:
        document.add_paragraph(subtitle)
    return document


def _table(document, headers, rows):
    rows = list(rows)
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    for index, text in enumerate(headers):
        table.rows[0].cells[index].text = str(text)
    for row in rows:
        cells = table.add_row().cells
        for index, text in enumerate(row):
            cells[index].text = str(text)
    return table


def convert_pdf(docx_path: Path, out_dir: Path) -> Path | None:
    """Convert a DOCX to PDF with LibreOffice when it is installed; otherwise return None."""
    candidates = [shutil.which("soffice"), shutil.which("libreoffice"),
                  r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                  "/usr/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    soffice = next((item for item in candidates if item and Path(item).exists()), None)
    if not soffice:
        return None
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir),
                        str(docx_path)], check=True, capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    pdf = Path(out_dir) / (docx_path.stem + ".pdf")
    return pdf if pdf.is_file() else None


# ----------------------------------------------------------------------------------- documents
def _scenario_rows(result) -> list[list[str]]:
    return [
        ["Коммодити", result.commodity],
        ["Сценарий", result.scenario_id],
        ["Горизонт", f"{result.horizon_months} мес."],
        ["Центральный пик", f"{_fmt(result.summary['price_peak'])} {result.units}"],
        ["Пик: low / central / high",
         f"{_fmt(result.summary['brent_range_usd_per_bbl']['low'])} / "
         f"{_fmt(result.summary['brent_range_usd_per_bbl']['central'])} / "
         f"{_fmt(result.summary['brent_range_usd_per_bbl']['high'])} {result.units}"],
        ["Средняя цена: low / central / high",
         f"{_fmt(result.summary['brent_range_mean_usd_per_bbl']['low'])} / "
         f"{_fmt(result.summary['brent_range_mean_usd_per_bbl']['central'])} / "
         f"{_fmt(result.summary['brent_range_mean_usd_per_bbl']['high'])} {result.units}"],
        ["Месяц пика", _month_label(result.summary["price_peak_month"])],
        ["Статус", result.status],
    ]


def _month_label(index: int) -> str:
    return f"месяц {int(index) + 1} (M{int(index) + 1})"


def _path_rows(result) -> list[list[str]]:
    return [[_month_label(month), _fmt(price)] for month, price in enumerate(result.price_path)]


def _sensitivity_rows(result) -> list[list[str]]:
    rows = []
    for item in result.sensitivity:
        rows.append([item.get("variant", "—"), _fmt(item.get("price_peak")),
                     _fmt(item.get("price_mean")), _fmt(item.get("delta_vs_central")),
                     _pct(item.get("delta_pct_vs_central")),
                     json.dumps(item.get("calibration_overrides") or {}, ensure_ascii=False,
                                sort_keys=True)])
    return rows


def _assumption_rows(result) -> list[list[str]]:
    return [[item.name, _fmt(item.value, 3), item.unit, item.source_class, item.source]
            for item in result.assumptions]


def build_executive(out_path: Path, result, client: str, generated: datetime) -> None:
    document = _docx("AMFE — нефтяной сценарий: Ормуз", generated,
                     f"Клиент: {client} · статус: {STATUS} · {result.commodity}")
    document.add_paragraph(DISCLAIMER)
    document.add_heading("Главное", level=1)
    summary = result.summary
    for line in (
            f"Сценарий: {result.scenario_id} — полное закрытие Ормуза на "
            f"{int(summary['closure_months'])} мес.; обходные маршруты и ответ предложения "
            f"включены, стратегические и коммерческие запасы расходуются по объявленным лимитам.",
            f"Центральный пик Brent: {_fmt(summary['price_peak'])} {result.units} "
            f"({_month_label(summary['price_peak_month'])}); диапазон чувствительности: "
            f"{_fmt(summary['brent_range_usd_per_bbl']['low'])}–"
            f"{_fmt(summary['brent_range_usd_per_bbl']['high'])} {result.units}.",
            f"Средняя цена за горизонт: {_fmt(summary['price_mean'])} {result.units}; диапазон "
            f"{_fmt(summary['brent_range_mean_usd_per_bbl']['low'])}–"
            f"{_fmt(summary['brent_range_mean_usd_per_bbl']['high'])} {result.units}.",
            f"Трафик Ормуза: {_fmt(summary['hormuz_transit_mb_per_day'])} mb/d; обход: "
            f"{_fmt(summary['bypass_capacity_mb_per_day'])} mb/d; стратегический release: "
            f"{_fmt(summary['spr_release_mb_per_day'])} mb/d.",
            f"Все проверки баланса: {'PASS' if all(result.checks.values()) else 'FAIL'}; "
            f"допущения: {len(result.assumptions)} (все source_class=assumption, "
            f"с указанием replace-with)."):
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("Ценовой путь (центральный прогон)", level=1)
    _table(document, ["Месяц", f"Brent, {result.units}"], _path_rows(result))
    document.add_heading("Границы применимости", level=1)
    for line in (
            "Это сценарная арифметика на объявленных допущениях, не прогноз и не инвестиционная "
            "рекомендация; диапазон — разброс вариантов чувствительности, не доверительный интервал.",
            "Числа получены детерминированным движком amfe.markets; исходные допущения должны быть "
            "заменены датированными публичными значениями (ICE/EIA/IEA/OPEC) перед клиентским "
            "использованием.",
            "Сырые сторонние данные не передаются; в пакете только производные числа сценария."):
        document.add_paragraph(line, style="List Bullet")
    document.save(str(out_path))


def build_scenario_report(out_path: Path, result, generated: datetime) -> None:
    document = _docx("AMFE — отчёт по сценарию", generated,
                     f"{result.commodity} · {result.scenario_id} · {result.units}")
    document.add_paragraph(DISCLAIMER)
    document.add_heading("1. Параметры сценария", level=1)
    scenario = result.meta.get("scenario") or {}
    document.add_paragraph(str(scenario.get("description", "")))
    _table(document, ["Параметр", "Значение"],
           [[key, _fmt(value, 4)] for key, value in
            sorted((scenario.get("parameters") or {}).items())])
    document.add_heading("2. Ценовой путь", level=1)
    _table(document, ["Месяц", f"Brent, {result.units}"], _path_rows(result))
    document.add_heading("3. Чувствительность", level=1)
    _table(document, ["Вариант", "Пик", "Средняя", "Δ пика", "Δ, %", "Overrides"],
           _sensitivity_rows(result))
    document.add_heading("4. Проверки баланса", level=1)
    _table(document, ["Проверка", "Результат"],
           [[name, "PASS" if pass_ else "FAIL"] for name, pass_ in sorted(result.checks.items())])
    document.add_heading("5. Статус и ограничения", level=1)
    for line in (
            f"Статус: {result.status} (research only; не допуск и не инвестиционная рекомендация).",
            "Сравнительная статика: без ценового замещения, обратных связей и поведенческой "
            "динамики; range — варианты чувствительности.",
            f"Digest результата: {result.digest()}"):
        document.add_paragraph(line, style="List Bullet")
    document.save(str(out_path))


def build_methodology(out_path: Path, result, generated: datetime) -> None:
    document = _docx("AMFE — методология и допущения", generated, result.commodity)
    document.add_paragraph(DISCLAIMER)
    document.add_heading("1. Движок", level=1)
    document.add_paragraph(
        "Детерминированный месячный балансовый движок amfe.markets.engine: предложение, спрос, "
        "обходные маршруты, запасы и цена; цены очищаются с эластичностью, растущей от "
        "краткосрочной к долгосрочной, плюс риск-премия. Никаких случайных величин и временных "
        "меток в результате.")
    document.add_heading("2. Допущения (все заменяемы источником)", level=1)
    _table(document, ["Допущение", "Значение", "Ед.", "Класс", "Replace-with"],
           _assumption_rows(result))
    document.add_heading("3. Диапазон", level=1)
    document.add_paragraph(
        "Диапазон low/central/high — это разброс центрального прогона по объявленным вариантам "
        "чувствительности (эластичность, обход, ответ предложения, риск-премия, 9-месячное "
        "закрытие). Это не доверительный интервал и не вероятностная оценка.")
    document.add_heading("4. Ограничения", level=1)
    for line in (
            "Прототип: сценарная арифметика на публичных допущениях, не допущенная модель.",
            "Нет ценового замещения, обратных связей и поведенческой динамики; нет данных "
            "реального времени.",
            "Числа не являются прогнозом; перед клиентским использованием допущения должны быть "
            "заменены датированными источниками.",
            "PDF может содержать служебные метки конвертера; DOCX/JSON/HTML детерминированы при "
            "фиксированном generated_at."):
        document.add_paragraph(line, style="List Bullet")
    document.save(str(out_path))


def build_licence(out_path: Path, result, generated: datetime) -> None:
    document = _docx("AMFE — лицензия и дисклеймер", generated, "Условия использования пакета")
    document.add_heading("1. Статус", level=1)
    for line in (
            f"AMFE market modules: {STATUS}. Это исследовательский артефакт, а не допущенная "
            "модель, не прогноз и не инвестиционная рекомендация.",
            "Результаты не должны использоваться как единственное основание для торговых, "
            "инвестиционных или регуляторных решений."):
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("2. Данные и допущения", level=1)
    for line in (
            "Пакет не содержит сырых сторонних наблюдений: только производные числа сценария и "
            "объявленные допущения с указанием replace-with источников "
            "(ICE/EIA/IEA/OPEC/company disclosures).",
            "Перед клиентским использованием допущения должны быть заменены датированными "
            "публичными значениями; ответственность за подстановку несёт пользователь.",
            "Лицензии сторонних источников не переоформляются: AMFE не перераспространяет их "
            "данные."):
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("3. Ответственность", level=1)
    document.add_paragraph(
        "Пакет предоставляется «как есть», без гарантий точности или пригодности для конкретной "
        "цели. AMFE не несёт ответственности за решения, принятые на основе сценария.")
    document.save(str(out_path))


# ------------------------------------------------------------------------------------------ XLSX
def build_xlsx(out_path: Path, result, generated: datetime) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    workbook = Workbook()
    head_fill = PatternFill("solid", fgColor="1F4E78")
    head_font = Font(color="FFFFFF", bold=True)

    def head(sheet, columns):
        for index, name in enumerate(columns, start=1):
            cell = sheet.cell(row=1, column=index, value=name)
            cell.fill = head_fill
            cell.font = head_font

    sheet = workbook.active
    sheet.title = "Сводка"
    head(sheet, ["Показатель", "Значение", "Единица"])
    rows = [
        ("Коммодити", result.commodity, ""),
        ("Сценарий", result.scenario_id, ""),
        ("Горизонт", result.horizon_months, "мес."),
        ("Центральный пик", result.summary["price_peak"], result.units),
        ("Пик low", result.summary["brent_range_usd_per_bbl"]["low"], result.units),
        ("Пик central", result.summary["brent_range_usd_per_bbl"]["central"], result.units),
        ("Пик high", result.summary["brent_range_usd_per_bbl"]["high"], result.units),
        ("Средняя low", result.summary["brent_range_mean_usd_per_bbl"]["low"], result.units),
        ("Средняя central", result.summary["brent_range_mean_usd_per_bbl"]["central"], result.units),
        ("Средняя high", result.summary["brent_range_mean_usd_per_bbl"]["high"], result.units),
        ("Месяц пика", int(result.summary["price_peak_month"]) + 1, "M"),
        ("Статус", result.status, ""),
    ]
    for row_index, (name, value, unit) in enumerate(rows, start=2):
        sheet.cell(row=row_index, column=1, value=name)
        sheet.cell(row=row_index, column=2, value=value)
        sheet.cell(row=row_index, column=3, value=unit)

    path_sheet = workbook.create_sheet("Цена по месяцам")
    head(path_sheet, ["Месяц", f"Brent, {result.units}"])
    for row_index, price in enumerate(result.price_path, start=2):
        path_sheet.cell(row=row_index, column=1, value=_month_label(row_index - 2))
        path_sheet.cell(row=row_index, column=2, value=float(price))

    sens = workbook.create_sheet("Чувствительность")
    head(sens, ["Вариант", "Пик", "Средняя", "Δ пика", "Δ, %", "Overrides"])
    for row_index, row in enumerate(result.sensitivity, start=2):
        sens.cell(row=row_index, column=1, value=row.get("variant"))
        sens.cell(row=row_index, column=2, value=row.get("price_peak"))
        sens.cell(row=row_index, column=3, value=row.get("price_mean"))
        sens.cell(row=row_index, column=4, value=row.get("delta_vs_central"))
        sens.cell(row=row_index, column=5, value=row.get("delta_pct_vs_central"))
        sens.cell(row=row_index, column=6,
                  value=json.dumps(row.get("calibration_overrides") or {}, ensure_ascii=False,
                                   sort_keys=True))

    assumptions = workbook.create_sheet("Допущения")
    head(assumptions, ["Допущение", "Значение", "Ед.", "Класс", "Replace-with", "Комментарий"])
    for row_index, item in enumerate(result.assumptions, start=2):
        assumptions.cell(row=row_index, column=1, value=item.name)
        assumptions.cell(row=row_index, column=2, value=item.value)
        assumptions.cell(row=row_index, column=3, value=item.unit)
        assumptions.cell(row=row_index, column=4, value=item.source_class)
        assumptions.cell(row=row_index, column=5, value=item.source)
        assumptions.cell(row=row_index, column=6, value=item.note)

    checks = workbook.create_sheet("Проверки")
    head(checks, ["Проверка", "Результат"])
    for row_index, (name, pass_) in enumerate(sorted(result.checks.items()), start=2):
        checks.cell(row=row_index, column=1, value=name)
        checks.cell(row=row_index, column=2, value="PASS" if pass_ else "FAIL")

    properties = workbook.properties
    properties.creator = "AMFE"
    properties.lastModifiedBy = "AMFE market client pack"
    properties.created = generated.replace(tzinfo=None)
    properties.modified = generated.replace(tzinfo=None)
    workbook.save(str(out_path))


# ----------------------------------------------------------------------------------------- JSON
def build_machine_json(out_path: Path, result, client: str, generated_at: str,
                       files: list[dict]) -> None:
    document = {
        "schema": SCHEMA,
        "kind": "market_client_pack",
        "status": STATUS,
        "generated_at": generated_at,
        "client": client,
        "disclaimer": DISCLAIMER,
        "market_result": result.as_dict(),
        "files": [{"path": item["path"], "bytes": item["bytes"]}
                  for item in sorted(files, key=lambda entry: entry["path"])],
        "licence": {
            "research_only": True,
            "no_third_party_observations": True,
            "no_investment_advice": True,
            "assumptions_replace_with": [{"name": item.name, "source": item.source}
                                         for item in result.assumptions],
        },
    }
    _write_json(out_path, document)


def _write_json(path: Path, document: dict) -> None:
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1)
               + "\n").encode("utf-8")
    Path(path).write_bytes(payload)


# -------------------------------------------------------------------------------------- dashboard
def build_dashboard(out_path: Path, result, client: str, generated_at: str) -> None:
    summary = result.summary
    peak = summary["brent_range_usd_per_bbl"]
    mean = summary["brent_range_mean_usd_per_bbl"]
    path_rows = "".join(
        f"<tr><td>{_month_label(month)}</td><td class=\"num\">{_fmt(price)}</td></tr>"
        for month, price in enumerate(result.price_path))
    sensitivity_rows = "".join(
        f"<tr><td>{item.get('variant')}</td><td class=\"num\">{_fmt(item.get('price_peak'))}</td>"
        f"<td class=\"num\">{_fmt(item.get('delta_vs_central'))}</td></tr>"
        for item in result.sensitivity)
    checks = "".join(f"<li>{name}: {'PASS' if pass_ else 'FAIL'}</li>"
                     for name, pass_ in sorted(result.checks.items()))
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMFE — нефтяной сценарий (research only)</title>
<style>
  body {{ font-family: system-ui, "Segoe UI", sans-serif; max-width: 62rem; margin: 0 auto;
         padding: 1.5rem; line-height: 1.45; }}
  h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
  .meta {{ color: #666; font-size: 0.9rem; }}
  .banner {{ border-left: 4px solid #b45309; background: #b4530915; padding: .7rem 1rem;
             margin: 1rem 0; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .card {{ border: 1px solid #8884; border-radius: 8px; padding: .8rem 1rem; min-width: 12rem; }}
  .card .v {{ font-size: 1.5rem; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin: .6rem 0; }}
  th, td {{ border: 1px solid #8884; padding: .35rem .5rem; text-align: left; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  footer {{ margin-top: 2rem; border-top: 1px solid #8884; padding-top: .7rem;
            color: #666; font-size: .85rem; }}
</style>
</head>
<body>
  <h1>AMFE — Brent / Ормуз: сценарная панель</h1>
  <p class="meta">Клиент: {client} · сценарий: {result.scenario_id} · статус: {STATUS} ·
     generated_at: {generated_at}</p>
  <div class="banner">{DISCLAIMER}</div>
  <div class="cards">
    <div class="card"><div>Центральный пик</div>
      <div class="v">{_fmt(summary['price_peak'])} {result.units}</div></div>
    <div class="card"><div>Пик low–high</div>
      <div class="v">{_fmt(peak['low'])}–{_fmt(peak['high'])}</div>
      <div class="meta">central {_fmt(peak['central'])}</div></div>
    <div class="card"><div>Средняя low–high</div>
      <div class="v">{_fmt(mean['low'])}–{_fmt(mean['high'])}</div>
      <div class="meta">central {_fmt(mean['central'])}</div></div>
  </div>
  <h2>Ценовой путь</h2>
  <table><thead><tr><th>Месяц</th><th>Brent, {result.units}</th></tr></thead>
    <tbody>{path_rows}</tbody></table>
  <h2>Чувствительность (пик)</h2>
  <table><thead><tr><th>Вариант</th><th>Пик</th><th>Δ к центральному</th></tr></thead>
    <tbody>{sensitivity_rows}</tbody></table>
  <h2>Проверки</h2><ul>{checks}</ul>
  <h2>Допущения</h2>
  <p>{len(result.assumptions)} объявленных допущений (все source_class=assumption); полный список —
     в <code>03_Price_Sensitivity.xlsx</code> и <code>06_Methodology_Appendix</code>.</p>
  <footer>Research only: не прогноз, не допуск и не инвестиционная рекомендация. Диапазон —
  варианты чувствительности, не доверительный интервал.</footer>
</body>
</html>
"""
    Path(out_path).write_text(html, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------------------- reproducibility
def build_reproducibility(out_path: Path, result, client: str, generated_at: str,
                          files: list[dict], command: str) -> None:
    deterministic = {}
    for item in files:
        name = item["path"]
        if name.lower().endswith((".json", ".html")) and (Path(out_path).parent / name).is_file():
            deterministic[name] = _sha256(Path(out_path).parent / name)
    document = {
        "schema": REPRO_SCHEMA,
        "generated_at": generated_at,
        "client": client,
        "status": STATUS,
        "module": {
            "module_id": result.module_id,
            "commodity": result.commodity,
            "scenario_id": result.scenario_id,
            "horizon_months": result.horizon_months,
            "engine": (result.meta or {}).get("engine"),
            "parameters": (result.meta or {}).get("parameters"),
            "replace_with": (result.meta or {}).get("replace_with"),
        },
        "result_digest": result.digest(),
        "market_result_sha256": _canonical_sha256(result.as_dict()),
        "deterministic_artifacts": deterministic,
        "container_files": sorted(item["path"] for item in files
                                  if item["path"] not in deterministic),
        "file_count": len(files),
        "notes": [
            "deterministic_artifacts covers the machine-readable JSON and the HTML dashboard; "
            "DOCX/XLSX/PDF containers may carry producer metadata and are listed by size only.",
            "All numbers are produced by the deterministic amfe.markets engine; no randomness.",
        ],
        "command": command,
        "python": sys.version.split()[0],
    }
    _write_json(out_path, document)


# ------------------------------------------------------------------------------------ assemble
def _readme(client: str, result, generated_at: str, files: list[dict], has_pdf: bool,
            passport: dict | None = None) -> str:
    lines = [
        "# AMFE — клиентский пакет по нефтяному сценарию",
        "",
        f"Клиент: **{client}**. Сценарий: `{result.module_id}/{result.scenario_id}`. "
        f"Статус: **{STATUS}**. Сгенерировано: {generated_at}.",
        "",
        "> " + DISCLAIMER,
        "",
        "## Файлы",
        "",
        "| Файл | Формат | Содержание |",
        "|---|---|---|",
        "| `01_Executive_Summary` | DOCX" + (" + PDF" if has_pdf else "") +
        " | главное, ценовой путь, ограничения |",
        "| `02_Scenario_Report` | DOCX" + (" + PDF" if has_pdf else "") +
        " | параметры, путь, чувствительность, проверки |",
        "| `03_Price_Sensitivity` | XLSX | Сводка, Цена по месяцам, Чувствительность, Допущения, "
        "Проверки |",
        "| `04_Machine_Readable` | JSON | MarketResult + файлы + лицензия (интеграция) |",
        "| `05_Dashboard` | HTML | офлайн-панель, без JS/внешних ресурсов |",
        "| `06_Methodology_Appendix` | DOCX" + (" + PDF" if has_pdf else "") +
        " | движок, допущения, диапазон, ограничения |",
        "| `07_Reproducibility` | JSON | digest результата, SHA-256 файлов, команда |",
        "| `08_Licence_and_Disclaimer` | DOCX" + (" + PDF" if has_pdf else "") +
        " | условия использования и дисклеймер |",
        "| `10_Module_Passports` | JSON | модуль/версия/статус/ограничения/хеши из реестра модулей |",
        "",
        "## Ключевые числа",
        "",
        f"- Центральный пик Brent: **{_fmt(result.summary['price_peak'])} {result.units}** "
        f"({_month_label(result.summary['price_peak_month'])});",
        f"- диапазон пика: **{_fmt(result.summary['brent_range_usd_per_bbl']['low'])}–"
        f"{_fmt(result.summary['brent_range_usd_per_bbl']['high'])} {result.units}** "
        "(low/central/high);",
        f"- средняя цена: {_fmt(result.summary['price_mean'])} {result.units};",
        f"- проверок PASS: {sum(1 for value in result.checks.values() if value)}/"
        f"{len(result.checks)}; допущений: {len(result.assumptions)}.",
        "",
        "## Модуль (паспорт реестра)",
        "",
        (f"- `{passport['module_id']}@{passport['version']}` — статус "
         f"**{passport['status']}**, kind `{passport['kind']}`, "
         f"code SHA-256 `{(passport.get('code_sha256') or '')[:16]}…`; ограничения — в "
         "`10_Module_Passports.json`." if passport else
         "- паспорт модуля недоступен (реестр не задан)."),
        "",
        "## Воспроизведение",
        "",
        "```powershell",
        f"python tools/market_client_pack.py --scenario {result.scenario_id} "
        f"--generated-at {generated_at} --out build/client_pack/market_{result.scenario_id}",
        "```",
        "",
        "PDF-конвертация выполняется LibreOffice, если он установлен; иначе пакет содержит DOCX "
        "и эта строка это фиксирует.",
        "",
    ]
    return "\n".join(lines)


def build_pack(out_dir: Path = DEFAULT_OUT, scenario_id: str = DEFAULT_SCENARIO,
               client: str = DEFAULT_CLIENT, generated_at: str | None = None,
               make_pdf: bool = True, module=None,
               registry_path: Path | None = None) -> dict:
    """Build the market client package; every number comes from the real MarketResult."""
    if not client or not client.strip():
        raise MarketPackError("client name must not be empty")
    generated_at = generated_at or _now()
    generated = _datetime(generated_at)
    module, _scenario, result = run_result(module, scenario_id)
    registry = load_module_passports(registry_path, module_id=result.module_id)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []

    def record(path: Path) -> None:
        files.append({"path": path.name, "sha256": _sha256(path),
                      "bytes": path.stat().st_size})

    docx_builders = [
        ("01_Executive_Summary.docx", lambda path: build_executive(path, result, client, generated)),
        ("02_Scenario_Report.docx", lambda path: build_scenario_report(path, result, generated)),
        ("06_Methodology_Appendix.docx", lambda path: build_methodology(path, result, generated)),
        ("08_Licence_and_Disclaimer.docx", lambda path: build_licence(path, result, generated)),
    ]
    for name, builder in docx_builders:
        path = out_dir / name
        builder(path)
        record(path)

    xlsx_path = out_dir / "03_Price_Sensitivity.xlsx"
    build_xlsx(xlsx_path, result, generated)
    record(xlsx_path)

    build_machine_json(out_dir / "04_Machine_Readable.json", result, client, generated_at, files)
    build_dashboard(out_dir / "05_Dashboard.html", result, client, generated_at)
    for path in (out_dir / "04_Machine_Readable.json", out_dir / "05_Dashboard.html"):
        record(path)
    passport = build_module_passports(out_dir / "10_Module_Passports.json", result, client,
                                      generated_at, registry)
    record(out_dir / "10_Module_Passports.json")

    has_pdf = False
    if make_pdf:
        for name, _builder in docx_builders:
            pdf = convert_pdf(out_dir / name, out_dir)
            if pdf is not None:
                has_pdf = True
                record(pdf)
    (out_dir / "README.md").write_text(_readme(client, result, generated_at, files, has_pdf,
                                              passport),
                                      encoding="utf-8", newline="\n")
    record(out_dir / "README.md")
    command = (f"python tools/market_client_pack.py --scenario {scenario_id} "
               f"--client {client!r} --generated-at {generated_at} --out {out_dir}")
    build_reproducibility(out_dir / "07_Reproducibility.json", result, client, generated_at,
                          files, command)
    record(out_dir / "07_Reproducibility.json")
    return {
        "ok": True,
        "schema": SCHEMA,
        "status": STATUS,
        "client": client,
        "scenario_id": scenario_id,
        "module_id": result.module_id,
        "generated_at": generated_at,
        "out": str(out_dir).replace("\\", "/"),
        "module_passport": {key: passport.get(key) for key in
                            ("module_id", "version", "status", "kind", "code_sha256")},
        "files": sorted(item["path"] for item in files),
        "pdf": has_pdf,
        "numbers": {
            "price_peak": result.summary["price_peak"],
            "price_mean": result.summary["price_mean"],
            "range_peak": result.summary["brent_range_usd_per_bbl"],
            "range_mean": result.summary["brent_range_mean_usd_per_bbl"],
            "units": result.units,
            "checks_pass": all(result.checks.values()),
            "assumptions": len(result.assumptions),
        },
        "result_digest": result.digest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the AMFE market client pack for the oil/Hormuz scenario")
    parser.add_argument("--out", default=None,
                        help="output directory (default build/client_pack/market_<scenario>)")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO,
                        choices=("hormuz_closed_6m", "hormuz_closed_3m",
                                 "hormuz_closed_6m_no_spr"))
    parser.add_argument("--client", default=DEFAULT_CLIENT, help="client name on the documents")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 timestamp YYYY-MM-DDTHH:MM:SSZ (determinism)")
    parser.add_argument("--no-pdf", action="store_true", help="skip LibreOffice PDF conversion")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                        help="module registry JSONL (default build/registry/modules.jsonl)")
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out) if args.out else ROOT / "build" / "client_pack" / f"market_{args.scenario}"
    try:
        summary = build_pack(out, scenario_id=args.scenario, client=args.client,
                             generated_at=args.generated_at, make_pdf=not args.no_pdf,
                             registry_path=Path(args.registry))
    except MarketPackError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps({"status": "OK", "out": summary["out"],
                          "scenario": summary["scenario_id"], "pdf": summary["pdf"],
                          "files": len(summary["files"]),
                          "numbers": summary["numbers"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
