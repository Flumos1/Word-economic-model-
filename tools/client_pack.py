"""Client delivery pack for one real AMFE scenario report (docs/142).

Turns an aggregate scenario report (``build/world/reports/<id>.json``) into the package a paying
research client receives: executive summary, scenario report, Excel analyst pack, machine-readable
JSON, offline dashboard, methodology appendix, reproducibility manifest, licence/disclaimer and a
board deck (DOCX/PDF/XLSX/PPTX/HTML/JSON).

Rules: deterministic (except ``generated_at``), fail-closed, aggregate-only (no raw Z/values),
no external URLs, no JavaScript. PDF is produced with LibreOffice when available; otherwise the
DOCX files remain and the README says so.
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
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "AMFE-CLIENT-PACK/1.0.0"
DEFAULT_REPORT = ROOT / "build" / "world" / "reports" / "energy_plus50.json"
DEFAULT_OUT = ROOT / "build" / "client_pack"
DISCLAIMER = (
    "Это исследовательский артефакт, а не допущенная модель и не инвестиционная рекомендация. "
    "Статус AMFE — research only (CANDIDATE/SHADOW_TEST); сценарии — сравнительная статика, "
    "а не прогноз. Сырые сторонние данные не передаются: только производные агрегаты."
)


class ClientPackError(RuntimeError):
    """The report is missing/broken or the package cannot be built: nothing is written."""


def read_report(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ClientPackError(f"scenario report not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClientPackError(f"cannot read scenario report {path}: {exc!r}") from exc
    required = ("schema", "scenario", "baseline", "result", "impacts", "checks")
    missing = [key for key in required if key not in document]
    if missing:
        raise ClientPackError(f"scenario report misses required fields: {missing}")
    scenario = document["scenario"]
    if not isinstance(scenario, dict) or not scenario.get("id"):
        raise ClientPackError("scenario.id is missing")
    for key in ("total_delta_output", "relative_delta_output", "total_output"):
        if key not in document["result"]:
            raise ClientPackError(f"result.{key} is missing")
    impacts = document["impacts"]
    for key in ("by_country", "by_industry"):
        if not isinstance(impacts.get(key), dict) or not impacts[key]:
            raise ClientPackError(f"impacts.{key} must be a non-empty mapping")
    return document


def _fmt(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sorted_items(mapping: dict) -> list[tuple[str, float]]:
    return sorted(((str(key), float(value)) for key, value in mapping.items()),
                  key=lambda item: (-abs(item[1]), item[0]))


def _core_props(document, generated: datetime):
    """Freeze DOCX/PPTX core properties so repeated builds are byte-stable."""
    props = document.core_properties
    props.created = generated
    props.modified = generated
    props.last_modified_by = "AMFE client pack"
    props.revision = 1


# --------------------------------------------------------------------------------------- DOCX
def _docx_document(title: str, generated: datetime):
    from docx import Document
    document = Document()
    _core_props(document, generated)
    document.add_heading(title, level=0)
    return document


def _docx_table(document, headers: Iterable[str], rows: Iterable[Iterable[str]], width=None):
    rows = list(rows)
    table = document.add_table(rows=1, cols=len(list(headers)))
    table.style = "Light Grid Accent 1"
    for index, text in enumerate(headers):
        table.rows[0].cells[index].text = str(text)
    for row in rows:
        cells = table.add_row().cells
        for index, text in enumerate(row):
            cells[index].text = str(text)
    return table


def build_executive(report: dict, client: str, generated: datetime, path: Path) -> None:
    scenario = report["scenario"]
    result = report["result"]
    baseline = report["baseline"]
    entries = _sorted_items(report["impacts"]["by_country"])
    industries = _sorted_items(report["impacts"]["by_industry"])
    document = _docx_document("AMFE: сценарный анализ для клиента", generated)
    document.add_paragraph(f"Клиент: {client}")
    document.add_paragraph(f"Сценарий: {scenario.get('id')} — {scenario.get('description', '')}")
    document.add_paragraph(DISCLAIMER)
    document.add_heading("Главное в трёх пунктах", level=1)
    document.add_paragraph(
        f"1. Совокупный эффект сценария: {_fmt(result['total_delta_output'])} USD млн "
        f"({_pct(result['relative_delta_output'])} от базового выпуска).")
    document.add_paragraph(
        f"2. Базовый выпуск мира: {_fmt(baseline.get('total_output'))} USD млн; проверки "
        f"балансов: {'PASS' if report['checks'].get('validation_pass') else 'FAIL'}, "
        f"продуктивность: {'PASS' if report['checks'].get('baseline_productive') else 'FAIL'}.")
    if entries:
        top_countries = ", ".join(f"{name} ({_fmt(value)})" for name, value in entries[:3])
        document.add_paragraph(f"3. Крупнейшие страны по |Δ|: {top_countries}.")
    document.add_heading("Куда смотреть в первую очередь", level=1)
    if industries:
        document.add_paragraph("Отрасли с наибольшим |Δ| выпуска:")
        _docx_table(document, ["Отрасль", "Δ выпуска, USD млн", "Доля |Δ|"],
                    [[name, _fmt(value), _pct(abs(value) / max(1e-9, sum(abs(v) for _, v in industries)))]
                     for name, value in industries[:5]])
    document.add_heading("Ограничения", level=1)
    document.add_paragraph(
        "Сценарий — сравнительная статика: технология и поведение фиксированы, цены/замещение/"
        "динамика не моделируются. Это research-результат, не допуск и не инвестиционная рекомендация.")
    document.save(path)


def build_scenario_doc(report: dict, client: str, generated: datetime, path: Path) -> None:
    scenario = report["scenario"]
    result = report["result"]
    baseline = report["baseline"]
    checks = report["checks"]
    document = _docx_document("Сценарный отчёт: страна × отрасль", generated)
    document.add_paragraph(f"Клиент: {client}")
    document.add_paragraph(f"Сценарий: {scenario.get('id')}")
    document.add_paragraph(f"Описание: {scenario.get('description', '')}")
    document.add_paragraph(DISCLAIMER)
    document.add_heading("Итоги", level=1)
    _docx_table(document, ["Показатель", "Значение"], [
        ["Базовый выпуск, USD млн", _fmt(baseline.get("total_output"))],
        ["Базовый конечный спрос, USD млн", _fmt(baseline.get("total_final_demand"))],
        ["Выпуск после шока, USD млн", _fmt(result.get("total_output"))],
        ["Δ выпуска, USD млн", _fmt(result.get("total_delta_output"))],
        ["Δ выпуска, %", _pct(result.get("relative_delta_output"))],
        ["Мультипликатор к шоку спроса", _fmt(result.get("output_multiplier_vs_demand"), 3)],
        ["ρ(A) базовый", _fmt(checks.get("baseline_spectral_radius"), 6)],
        ["Валидация балансов", "PASS" if checks.get("validation_pass") else "FAIL"],
        ["Hawkins–Simon", "PASS" if checks.get("hawkins_simon_pass") else "FAIL"],
    ])
    document.add_heading("Страны (полная таблица)", level=1)
    entries = _sorted_items(report["impacts"]["by_country"])
    _docx_table(document, ["Страна", "Δ выпуска, USD млн"],
                [[name, _fmt(value)] for name, value in entries])
    document.add_heading("Отрасли (полная таблица)", level=1)
    industries = _sorted_items(report["impacts"]["by_industry"])
    _docx_table(document, ["Отрасль", "Δ выпуска, USD млн"],
                [[name, _fmt(value)] for name, value in industries])
    document.save(path)


def build_methodology(report: dict, generated: datetime, path: Path) -> None:
    dataset = report.get("dataset") or {}
    document = _docx_document("Методология и воспроизводимость", generated)
    document.add_paragraph(DISCLAIMER)
    document.add_heading("Данные", level=1)
    _docx_table(document, ["Параметр", "Значение"], [
        ["Датасет", str(dataset.get("dataset_id") or "—")],
        ["Схема", str(dataset.get("schema") or "—")],
        ["Страны / отрасли / сектора",
         f'{dataset.get("countries", "—")} / {dataset.get("industries", "—")} / '
         f'{dataset.get("sectors", "—")}'],
        ["Единицы", str(dataset.get("units") or "USD_mn")],
        ["SHA-256 датасета", str(dataset.get("sha256") or "—")],
        ["Лицензия", "сторонние данные OECD/Eurostat/WIOD; передаются только производные агрегаты"],
    ])
    document.add_heading("Метод", level=1)
    document.add_paragraph(
        "Модель затрат–выпуска: A = Z/x; шок конечного спроса/технологии; решение x = (I − A)⁻¹ y "
        "разреженным LU-фактором; проверки: x = Z·1 + y + fda, x = 1'Z + va + vaa, ρ(A) < 1, "
        "Hawkins–Simon. SFC-блок проверяет S = I + CA и ΣCA = 0 (с оговоркой о промежуточной торговле).")
    document.add_heading("Ограничения", level=1)
    for item in (report.get("limitations") or [
            "сравнительная статика без цен, замещения и динамики",
            "конечный спрос берётся по блоку страны-производителя",
            "сценарии не являются прогнозом и не допущены к применению"]):
        document.add_paragraph(f"• {item}")
    document.save(path)


def build_licence(client: str, generated: datetime, path: Path) -> None:
    document = _docx_document("Лицензии и дисклеймер", generated)
    document.add_paragraph(f"Клиент: {client}")
    document.add_paragraph(DISCLAIMER)
    document.add_heading("Данные", level=1)
    document.add_paragraph(
        "FIGARO (Eurostat), OECD ICIO, WIOD 2016 — сторонние данные. Сырые таблицы не передаются "
        "и не перепродаются; клиент получает производные агрегаты и отчёты. Условия источников "
        "(OECD Terms, Eurostat re-use policy, CC BY 4.0 для WIOD) должны соблюдаться при цитировании.")
    document.add_heading("Статус модели", level=1)
    document.add_paragraph(
        "AMFE — исследовательский проект. T1 CPI — CANDIDATE (C4 FAIL, C6 0/12), regime — "
        "SHADOW_TEST. Никакие результаты не являются допуском, прогнозом для сделок или "
        "инвестиционной рекомендацией.")
    document.add_heading("Ответственность", level=1)
    document.add_paragraph(
        "Клиент принимает решения самостоятельно; AMFE не несёт ответственности за решения, "
        "принятые на основе сценариев. Результаты воспроизводимы по манифесту 07_Reproducibility.json.")
    document.save(path)


# --------------------------------------------------------------------------------------- XLSX
def build_xlsx(report: dict, client: str, generated: datetime, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    workbook.properties.created = generated
    workbook.properties.modified = generated
    workbook.properties.creator = "AMFE client pack"
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="D9E1F2")

    def sheet(name: str, headers: list[str], rows: list[list], widths=None):
        worksheet = workbook.create_sheet(name)
        worksheet.append(headers)
        for cell in worksheet[1]:
            cell.font = bold
            cell.fill = header_fill
        for row in rows:
            worksheet.append(row)
        if widths:
            for column, width in zip("ABCDEF", widths):
                worksheet.column_dimensions[column].width = width
        return worksheet

    summary = workbook.active
    summary.title = "Сводка"
    scenario = report["scenario"]
    result = report["result"]
    baseline = report["baseline"]
    checks = report["checks"]
    summary.append(["AMFE — сценарный анализ", client])
    summary.append([])
    for row in [
        ("Сценарий", scenario.get("id")),
        ("Описание", scenario.get("description", "")),
        ("Базовый выпуск, USD млн", float(baseline.get("total_output", 0.0))),
        ("Выпуск после шока, USD млн", float(result.get("total_output", 0.0))),
        ("Δ выпуска, USD млн", float(result.get("total_delta_output", 0.0))),
        ("Δ выпуска, %", float(result.get("relative_delta_output", 0.0)) * 100),
        ("Мультипликатор", result.get("output_multiplier_vs_demand")),
        ("Валидация", "PASS" if checks.get("validation_pass") else "FAIL"),
        ("Датасет", (report.get("dataset") or {}).get("dataset_id")),
    ]:
        summary.append(list(row))
    summary.column_dimensions["A"].width = 34
    summary.column_dimensions["B"].width = 80

    sheet("Страны", ["Страна", "Δ выпуска, USD млн"],
          [[name, value] for name, value in _sorted_items(report["impacts"]["by_country"])],
          widths=[14, 22])
    sheet("Отрасли", ["Отрасль", "Δ выпуска, USD млн"],
          [[name, value] for name, value in _sorted_items(report["impacts"]["by_industry"])],
          widths=[18, 22])
    sfc = report.get("sfc") or {}
    sfc_rows = []
    for assumption in sfc.get("assumptions") or []:
        sfc_rows.append(["Допущение", assumption])
    for key, value in (sfc.get("checks") or {}).items():
        sfc_rows.append([key, value])
    sheet("SFC", ["Показатель", "Значение"], sfc_rows, widths=[46, 40])
    sheet("Проверки", ["Проверка", "Значение"],
          [[key, value] for key, value in checks.items()], widths=[34, 34])

    workbook.remove(workbook["Sheet"]) if "Sheet" in workbook.sheetnames else None
    workbook.save(path)


# --------------------------------------------------------------------------------------- PPTX
def build_pptx(report: dict, client: str, generated: datetime, path: Path):
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError:
        # Fallback for environments without python-pptx: a simple Markdown deck, no fake .pptx.
        scenario = report["scenario"]
        result = report["result"]
        lines = [f"# AMFE: сценарий для клиента — {client}", "",
                 f"- Сценарий: {scenario.get('id')}", f"- {scenario.get('description', '')}",
                 f"- Δ выпуска: {_fmt(result['total_delta_output'])} USD млн "
                 f"({_pct(result['relative_delta_output'])})", "",
                 "## Топ стран по |Δ|"]
        lines += [f"- {name}: {_fmt(value)} USD млн"
                  for name, value in _sorted_items(report["impacts"]["by_country"])[:6]]
        lines += ["", "## Топ отраслей по |Δ|"]
        lines += [f"- {name}: {_fmt(value)} USD млн"
                  for name, value in _sorted_items(report["impacts"]["by_industry"])[:6]]
        lines += ["", "## Ограничения", f"- {DISCLAIMER}"]
        fallback = Path(path).with_suffix(".md")
        fallback.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return fallback

    scenario = report["scenario"]
    result = report["result"]
    baseline = report["baseline"]
    entries = _sorted_items(report["impacts"]["by_country"])[:6]
    industries = _sorted_items(report["impacts"]["by_industry"])[:6]

    presentation = Presentation()
    _core_props(presentation, generated)
    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    slide.shapes.title.text = "AMFE: сценарий для клиента"
    slide.placeholders[1].text = f"{client}\n{scenario.get('id')} · {generated:%Y-%m-%d}"

    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Что случилось"
    frame = slide.placeholders[1].text_frame
    frame.text = f"Сценарий: {scenario.get('description', '')}"
    frame.add_paragraph().text = (
        f"Δ выпуска: {_fmt(result['total_delta_output'])} USD млн "
        f"({_pct(result['relative_delta_output'])})")
    frame.add_paragraph().text = f"Базовый выпуск: {_fmt(baseline.get('total_output'))} USD млн"

    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Топ стран по |Δ|"
    frame = slide.placeholders[1].text_frame
    frame.text = "\n".join(f"{name}: {_fmt(value)} USD млн" for name, value in entries)

    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Топ отраслей по |Δ|"
    frame = slide.placeholders[1].text_frame
    frame.text = "\n".join(f"{name}: {_fmt(value)} USD млн" for name, value in industries)

    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Ограничения и статус"
    frame = slide.placeholders[1].text_frame
    frame.text = "Research only: не допуск и не инвестиционная рекомендация."
    frame.add_paragraph().text = "Сравнительная статика: технология и поведение фиксированы."
    frame.add_paragraph().text = "Сырые сторонние данные не передаются."
    presentation.save(path)
    return Path(path)


# --------------------------------------------------------------------------------------- HTML/JSON
def build_dashboard(report: dict, client: str, generated: datetime, path: Path,
                    source: Path | None = None) -> None:
    if source is not None and Path(source).is_file():
        shutil.copyfile(source, path)
        return
    scenario = report["scenario"]
    result = report["result"]
    entries = _sorted_items(report["impacts"]["by_country"])[:10]
    industries = _sorted_items(report["impacts"]["by_industry"])[:10]
    css = ("body{font-family:system-ui,Arial,sans-serif;margin:2rem;color:#1d232a;background:#f7f7f4}"
           "h1{font-size:1.4rem}table{border-collapse:collapse;margin:1rem 0}"
           "th,td{border:1px solid #cbd5e1;padding:.35rem .6rem;text-align:left}"
           "th{background:#e2e8f0}.muted{color:#5c6670;font-size:.9rem}")
    rows = "".join(f"<tr><td>{name}</td><td>{_fmt(value)}</td></tr>" for name, value in entries)
    inds = "".join(f"<tr><td>{name}</td><td>{_fmt(value)}</td></tr>" for name, value in industries)
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>AMFE: {scenario.get('id')}</title>
<style>{css}</style></head><body>
<h1>AMFE: сценарный анализ — {client}</h1>
<p>Сценарий: <b>{scenario.get('id')}</b> · {scenario.get('description', '')}</p>
<p>Δ выпуска: <b>{_fmt(result['total_delta_output'])}</b> USD млн ({_pct(result['relative_delta_output'])})</p>
<h2>Страны (топ-10 по |Δ|)</h2><table><tr><th>Страна</th><th>Δ, USD млн</th></tr>{rows}</table>
<h2>Отрасли (топ-10 по |Δ|)</h2><table><tr><th>Отрасль</th><th>Δ, USD млн</th></tr>{inds}</table>
<p class="muted">{DISCLAIMER}</p></body></html>
"""
    path.write_text(html, encoding="utf-8", newline="\n")


def build_reproducibility(report: dict, source: Path, generated: datetime, path: Path,
                          outputs: list[Path], command: list[str]) -> None:
    dataset = report.get("dataset") or {}
    document = {
        "schema": "AMFE-CLIENT-PACK-REPRO/1.0.0",
        "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": " ".join(command),
        "input": {"path": str(Path(source).name), "sha256": _sha256(source),
                  "bytes": Path(source).stat().st_size},
        "dataset": {"id": dataset.get("dataset_id"), "sha256": dataset.get("sha256"),
                    "countries": dataset.get("countries"), "industries": dataset.get("industries"),
                    "sectors": dataset.get("sectors"), "units": dataset.get("units")},
        "outputs": {item.name: {"sha256": _sha256(item), "bytes": item.stat().st_size}
                    for item in outputs if item.is_file()},
        "rebuild": ("python tools/client_pack.py --report <input> --client <name> "
                    "--out <dir> --generated-at <iso>"),
        "note": "PDF files are converted with LibreOffice and may embed converter timestamps.",
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------------------- build
def convert_pdf(docx_path: Path, out_dir: Path) -> Path | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    soffice = next((item for item in candidates if item and Path(item).exists()), None)
    if not soffice:
        return None
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir),
                        str(docx_path)], check=True, capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    pdf = out_dir / (docx_path.stem + ".pdf")
    return pdf if pdf.is_file() else None


def build_pack(report_path: Path, client: str, out_dir: Path, generated_at: str | None = None,
               dashboard: Path | None = None, make_pdf: bool = True,
               command: list[str] | None = None) -> dict:
    source = Path(report_path)
    report = read_report(source)
    if not client or not client.strip():
        raise ClientPackError("client name must not be empty")
    generated = (datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                 if generated_at else datetime.now(timezone.utc))
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []

    docs = [
        ("01_Executive_Summary.docx", build_executive),
        ("02_Scenario_Report.docx", build_scenario_doc),
        ("06_Methodology_Appendix.docx", build_methodology),
        ("08_Licence_and_Disclaimer.docx", build_licence),
    ]
    for name, builder in docs:
        path = target / name
        if builder is build_licence:
            builder(client, generated, path)
        elif builder is build_methodology:
            builder(report, generated, path)
        else:
            builder(report, client, generated, path)
        outputs.append(path)

    xlsx = target / "03_Analyst_Pack.xlsx"
    build_xlsx(report, client, generated, xlsx)
    outputs.append(xlsx)
    pptx = target / "09_Board_Deck.pptx"
    deck = build_pptx(report, client, generated, pptx)
    outputs.append(Path(deck))
    html = target / "05_Dashboard.html"
    build_dashboard(report, client, generated, html, dashboard)
    outputs.append(html)
    machine = target / "04_Machine_Readable.json"
    machine.write_text(json.dumps({"schema": SCHEMA, "client": client,
                                   "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "scenario_report": report}, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8", newline="\n")
    outputs.append(machine)

    if make_pdf:
        for docx in [target / name for name, _ in docs]:
            pdf = convert_pdf(docx, target)
            if pdf:
                outputs.append(pdf)

    repro = target / "07_Reproducibility.json"
    build_reproducibility(report, source, generated, repro, outputs,
                          command or sys.argv)
    outputs.append(repro)

    readme = target / "README.md"
    readme.write_text(
        "# AMFE client pack\n\n"
        f"Клиент: {client}\n\n"
        f"Сценарий: {report['scenario'].get('id')}\n\n"
        f"Собрано: {generated:%Y-%m-%dT%H:%M:%SZ}\n\n"
        "Файлы: 01 executive summary, 02 сценарный отчёт, 03 Excel, 04 JSON, 05 dashboard, "
        "06 методология, 07 воспроизводимость, 08 лицензии, 09 deck. PDF — если доступен LibreOffice.\n\n"
        f"{DISCLAIMER}\n", encoding="utf-8", newline="\n")
    outputs.append(readme)
    return {"ok": True, "client": client, "scenario": report["scenario"].get("id"),
            "out": str(target), "files": sorted(item.name for item in outputs),
            "bytes": sum(item.stat().st_size for item in outputs)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Собрать клиентский пакет по реальному сценарию")
    parser.add_argument("--report", default=str(DEFAULT_REPORT),
                        help="JSON сценарного отчёта (build/world/reports/<id>.json)")
    parser.add_argument("--client", default="Demo Client", help="название клиента")
    parser.add_argument("--out", default=None, help="каталог пакета")
    parser.add_argument("--generated-at", default=None, help="ISO UTC (для детерминизма)")
    parser.add_argument("--dashboard", default=None, help="готовый dashboard.html (копируется)")
    parser.add_argument("--no-pdf", action="store_true", help="не конвертировать DOCX в PDF")
    parser.add_argument("--json", action="store_true", help="напечатать итог как JSON")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else DEFAULT_OUT / f"{args.client.lower().replace(' ', '_')}"
    try:
        result = build_pack(Path(args.report), args.client, out, args.generated_at,
                            Path(args.dashboard) if args.dashboard else None,
                            not args.no_pdf, command=[sys.executable, *sys.argv])
    except ClientPackError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json else
          f"ok: {result['out']} ({len(result['files'])} files, {result['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
