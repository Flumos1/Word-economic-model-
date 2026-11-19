#!/usr/bin/env python3
"""Client delivery pack for the Hormuz -> US impact chain (base / stress variants).

Builds the package a paying research client receives from ``tools/hormuz_us_impact.py``:
a one-to-two page executive summary (DOCX + PDF where LibreOffice exists), a scenario workbook
(scenarios/months, assumptions and ranges, CPI/households, social risk/triggers), the full
machine-readable report with its SHA-256, an offline HTML version, a ``manifest.json`` that pins
every file by SHA-256, and a short README.

Rules: every number comes from ``hormuz_us_impact.build_report`` (the real oil module result plus
declared assumptions); nothing is invented and no raw third-party observation is redistributed.
Two variants are always built: ``base`` (``stress=False``) and ``stress`` (``stress=True``); the
CLI ``--scenario`` only selects which one leads the summary. DOCX/XLSX are normalised exactly as in
``tools/market_client_pack.py`` (fixed zip timestamps and ``docProps/core.xml`` dates), so two
builds at the same ``--generated-at`` are byte-identical without PDFs. PDF conversion is optional
and its output may carry LibreOffice metadata; such files are marked ``deterministic: false`` in
the manifest and are never required. Fail-closed: an unknown scenario, an empty client name or a
failed declaration check writes nothing. Status is RESEARCH_ONLY, admission=false.

Usage:
    python tools/hormuz_us_client_pack.py \
        --out build/client_pack/hormuz_us --client "Demo Bank" \
        --generated-at 2026-11-19T09:00:00Z --scenario stress
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _candidate in (ROOT / "src", ROOT / "tools"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import hormuz_us_impact  # noqa: E402  (its own sys.path setup runs on import)

SCHEMA = "amfe.hormuz_us_client_pack.v1"
MANIFEST_SCHEMA = "amfe.hormuz_us_client_pack.manifest.v1"
IMPACT_SCHEMA = "amfe.hormuz_us_impact.v1"
TOOL_VERSION = "1.0.0"
STATUS = "RESEARCH_ONLY"
DEFAULT_CLIENT = "Research client"
DEFAULT_SCENARIO = "stress"
DEFAULT_OIL_SCENARIO = "hormuz_closed_6m"
SCENARIOS = ("base", "stress")
OIL_SCENARIOS = tuple(hormuz_us_impact.SCENARIOS)
DEFAULT_OUT = ROOT / "build" / "client_pack" / "hormuz_us"
DISCLAIMER = (
    "RESEARCH_ONLY, admission=false, causal_claim=false. Механический пересчёт сценарной "
    "арифметики при объявленных допущениях: это не прогноз, не допуск модели, не вероятность "
    "беспорядков и не инвестиционная рекомендация. Диапазоны — разброс вариантов "
    "чувствительности нефтяного модуля, а не доверительный интервал. Сырые сторонние данные не "
    "перераспространяются."
)
ASSETS = {
    "executive": "01_Executive_Summary",
    "workbook": "02_Scenarios_and_Assumptions",
    "json": "03_Full_Report",
    "html": "04_Report",
}


class HormuzPackError(RuntimeError):
    """The impact report or the package cannot be built (fail-closed)."""


# ------------------------------------------------------------------------------------- primitives
def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _datetime(generated_at: str | None) -> datetime:
    if not generated_at:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        return datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HormuzPackError(f"--generated-at must be YYYY-MM-DDTHH:MM:SSZ: {exc}") from exc


def _fmt(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _money(value, digits: int = 2) -> str:
    return "—" if value is None else f"${_fmt(value, digits)}"


def _pp(value, digits: int = 2) -> str:
    return "—" if value is None else f"+{_fmt(value, digits)} п.п."


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(document) -> str:
    return json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def _write_json(path: Path, document) -> None:
    """Write canonical JSON as UTF-8 bytes (never CRLF on Windows)."""
    Path(path).write_bytes(_canonical_json(document).encode("utf-8"))


def _write_text(path: Path, text: str) -> None:
    Path(path).write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------------- impact reports
def _canonical_report(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _attach_content_sha(report: dict) -> dict:
    """Mirror ``hormuz_us_impact.main``: SHA-256 over the canonical report with a null digest."""
    report["content_sha256"] = None
    text = _canonical_report(report).replace("\r\n", "\n")
    report["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return report


def report_sha256(report: dict) -> str:
    """Recompute the content SHA-256 of a report that already carries ``content_sha256``."""
    probe = dict(report)
    probe["content_sha256"] = None
    return hashlib.sha256(_canonical_report(probe).replace("\r\n", "\n").encode("utf-8")).hexdigest()


def validate_report(name: str, report: dict) -> dict:
    """Fail-closed checks on one variant: status, admission flag, block presence, arithmetic."""
    if not isinstance(report, dict):
        raise HormuzPackError(f"variant {name!r}: report is not a mapping")
    if report.get("status") != STATUS:
        raise HormuzPackError(f"variant {name!r}: unexpected status {report.get('status')!r}")
    if report.get("admission") is not False:
        raise HormuzPackError(f"variant {name!r}: admission must be false")
    if report.get("causal_claim") is not False:
        raise HormuzPackError(f"variant {name!r}: causal_claim must be false")
    if report.get("schema") != IMPACT_SCHEMA:
        raise HormuzPackError(f"variant {name!r}: unexpected schema {report.get('schema')!r}")
    failing = [key for key, value in (report.get("checks") or {}).items() if not value]
    if failing:
        raise HormuzPackError(f"variant {name!r}: failed declaration checks {failing}")
    peak = report.get("peak") or {}
    for field in ("gasoline_usd_gal", "diesel_usd_gal", "cpi_headline_pp"):
        if peak.get(field) is None:
            raise HormuzPackError(f"variant {name!r}: peak.{field} missing")
    has_stress = report.get("stress") is not None
    if name == "base" and has_stress:
        raise HormuzPackError("variant 'base' unexpectedly carries a stress block")
    if name == "stress" and not has_stress:
        raise HormuzPackError("variant 'stress' is missing its stress block")
    return report


def build_reports(oil_scenario: str = DEFAULT_OIL_SCENARIO, generated_at: str | None = None,
                  reports: dict | None = None) -> dict:
    """Return ``{"base": report, "stress": report}`` from the real impact tool."""
    if reports is not None:
        for name in SCENARIOS:
            if name not in reports:
                raise HormuzPackError(f"variant {name!r} missing from the supplied reports")
        return {name: validate_report(name, dict(reports[name])) for name in SCENARIOS}
    if oil_scenario not in OIL_SCENARIOS:
        raise HormuzPackError(
            f"unknown oil scenario {oil_scenario!r}; expected one of {list(OIL_SCENARIOS)}")
    built = {}
    for name, stress in (("base", False), ("stress", True)):
        try:
            report = hormuz_us_impact.build_report(oil_scenario, generated_at, stress=stress)
        except Exception as exc:  # pragma: no cover - depends on the oil module
            raise HormuzPackError(f"build_report({oil_scenario!r}, stress={stress}) failed: "
                                  f"{exc!r}") from exc
        built[name] = validate_report(name, _attach_content_sha(report))
    return built


# ------------------------------------------------------------------------------------ DOCX helpers
def _docx(title: str, generated: datetime, subtitle: str = ""):
    from docx import Document
    from docx.shared import Cm, Pt
    document = Document()
    properties = document.core_properties
    properties.created = generated
    properties.modified = generated
    properties.last_modified_by = "AMFE hormuz client pack"
    properties.author = "AMFE"
    properties.revision = 1
    # The executive summary must fit 1-2 pages: compact type and narrower margins.
    styles = document.styles
    styles["Normal"].font.size = Pt(9.5)
    styles["Normal"].paragraph_format.space_after = Pt(2)
    styles["Title"].font.size = Pt(15)
    styles["Heading 1"].font.size = Pt(11.5)
    styles["Heading 1"].paragraph_format.space_before = Pt(6)
    styles["Heading 1"].paragraph_format.space_after = Pt(2)
    styles["List Bullet"].font.size = Pt(9.5)
    styles["List Bullet"].paragraph_format.space_after = Pt(1)
    for section in document.sections:
        section.top_margin = Cm(1.6)
        section.bottom_margin = Cm(1.6)
        section.left_margin = Cm(1.9)
        section.right_margin = Cm(1.9)
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


def _bullets(document, lines) -> None:
    for line in lines:
        document.add_paragraph(line, style="List Bullet")


def _assumption(report: dict, name: str, default=None):
    block = (report.get("assumptions") or {}).get(name) or {}
    return block.get("value", default)


def _stress_assumption(report: dict, name: str):
    block = ((report.get("stress") or {}).get("assumptions") or {}).get(name) or {}
    return block


def _normalise_office_zip(path: Path, moment: datetime) -> None:
    """Rewrite a DOCX/XLSX zip with fixed entry timestamps (same approach as market_client_pack).

    python-docx/openpyxl stamp zip entries with the wall clock, so identical rebuilds could differ
    by a second and break byte-for-byte reproducibility.  Rewriting the archive with the pack's
    ``generated_at`` timestamp and pinning ``dcterms:created/modified`` makes the artefact
    deterministic without changing content.
    """
    date_time = moment.timetuple()[:6]
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ").encode("utf-8")
    with zipfile.ZipFile(path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in entries:
            if info.filename == "docProps/core.xml":
                data = re.sub(
                    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
                    rb"\g<1>" + stamp + rb"\g<2>", data)
            new_info = zipfile.ZipInfo(info.filename, date_time=date_time)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            target.writestr(new_info, data)
    tmp.replace(path)


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


# ------------------------------------------------------------------------------- executive summary
def build_executive(out_path: Path, reports: dict, scenario: str, client: str, oil_scenario: str,
                    generated_at: str, generated: datetime) -> None:
    base, stress = reports["base"], reports["stress"]
    peak_b, peak_s = base["peak"], stress["peak"]
    sblock = stress["stress"]
    oil = base["oil_module"]
    band = base["social_political"]
    stress_band = sblock["social_political"]

    try:
        document = _docx(
            "AMFE — Ормуз → США: цены, CPI, домохозяйства, социальный риск", generated,
            f"Клиент: {client} · пакет: {scenario} · нефтяной сценарий: {oil_scenario} · "
            f"статус: {STATUS} · admission=false · generated_at: {generated_at}")
    except ImportError as exc:  # pragma: no cover - environment without python-docx
        raise HormuzPackError(f"python-docx is required for the executive summary: {exc!r}") from exc
    document.add_paragraph(DISCLAIMER)

    document.add_heading("Главное", level=1)
    triggers_b = ", ".join(band.get("reasons") or []) or "ни один порог не пройден"
    _bullets(document, [
        f"Нефть: пик Brent {_fmt(oil['price_peak_usd_bbl'])} USD/bbl, диапазон "
        f"{_fmt(oil['price_range_usd_bbl']['low'])}–{_fmt(oil['price_range_usd_bbl']['high'])}, "
        f"среднее за горизонт {_fmt(oil['price_mean_usd_bbl'])}. Это сценарная арифметика "
        f"модуля oil.brent, а не прогноз.",
        f"Бензин США на пике: {_money(peak_b['gasoline_usd_gal'])}/gal "
        f"(диапазон чувствительности {_money(peak_b['gasoline_range_usd_gal']['low'])}–"
        f"{_money(peak_b['gasoline_range_usd_gal']['high'])}); дизель "
        f"{_money(peak_b['diesel_usd_gal'])}/gal.",
        f"CPI headline: {_pp(peak_b['cpi_headline_pp'])} к базе; домохозяйство: "
        f"+{_money(peak_b['household_monthly_cost_usd'])}/мес "
        f"(+{_money(peak_b['household_annual_cost_usd'])}/год) при "
        f"{_fmt(_assumption(base, 'household_gallons_per_month'), 0)} галлонах в месяц.",
        f"Регрессивность: удар по бюджету бедных домохозяйств "
        f"{_fmt(peak_b['low_income_budget_shock_pct'], 1)}% дохода, по медианным — "
        f"{_fmt(peak_b['median_budget_shock_pct'], 1)}%; иллюстративное давление на одобрение "
        f"{_fmt(peak_b['approval_pressure_pp'], 1)} п.п.",
        f"Социальный риск: {band['score_0_100']}/100 ({band['band']}); сработавшие триггеры: "
        f"{triggers_b}.",
        f"Стресс-надбавка (слэт/Венесуэла/фрахт/обход): дизель {_money(sblock['diesel_usd_gal'])}/gal "
        f"(+{_fmt(sblock['diesel_delta_pct'], 0)}%), авиакеросин "
        f"{_money(sblock['jet_usd_gal'])}/gal (+{_fmt(sblock['jet_delta_pct'], 0)}%), "
        f"CPI {_pp(sblock['cpi_headline_pp'])}, соцриск "
        f"{stress_band['score_0_100']}/100 ({stress_band['band']}).",
    ])

    document.add_heading("Базовый и стресс-вариант (пик)", level=1)
    document.add_paragraph(
        "Стресс-вариант не переоценивает Brent: он добавляет премию за качество сырья, "
        "фрахт/страховку и креки, поэтому топливо и CPI дороже при том же Brent.")
    _table(document, ["Показатель", "base", "stress"], [
        ["Brent, USD/bbl", _fmt(peak_b["brent_usd_bbl"]), _fmt(peak_s["brent_usd_bbl"])],
        ["Поставленная нефть Δ, USD/bbl", "—",
         f"+{_fmt(stress['stress']['delivered_crude_delta_usd_bbl'])}"],
        ["Бензин, $/gal", _fmt(peak_b["gasoline_usd_gal"]), _fmt(sblock["gasoline_usd_gal"])],
        ["Дизель, $/gal", _fmt(peak_b["diesel_usd_gal"]), _fmt(sblock["diesel_usd_gal"])],
        ["Авиакеросин, $/gal", "—", _fmt(sblock["jet_usd_gal"])],
        ["CPI headline, п.п.", _fmt(peak_b["cpi_headline_pp"]), _fmt(sblock["cpi_headline_pp"])],
        ["Домохозяйство, $/мес", _fmt(peak_b["household_monthly_cost_usd"]), "—"],
        ["Соцриск, 0–100", f"{band['score_0_100']} ({band['band']})",
         f"{stress_band['score_0_100']} ({stress_band['band']})"],
    ])

    document.add_heading("Как читать цифры", level=1)
    document.add_paragraph(
        f"Путь Brent берётся из модуля oil.brent ({oil_scenario}) и переводится в цены топлива "
        f"явным pass-through: {_fmt(_assumption(base, 'gasoline_pass_through_usd_gal_per_usd_bbl'), 3)} "
        f"$/gal бензина и "
        f"{_fmt(_assumption(base, 'diesel_pass_through_usd_gal_per_usd_bbl'), 3)} $/gal дизеля на "
        f"каждый $1/bbl. CPI — прямой вес бензина "
        f"{_fmt(_assumption(base, 'cpi_gasoline_weight'), 3)} плюс дизель через еду/транспорт "
        f"(вес {_fmt(_assumption(base, 'food_transport_weight'), 3)}, pass-through "
        f"{_fmt(_assumption(base, 'diesel_to_food_transport_pass_through'), 2)}) и второй раунд "
        f"{_fmt(_assumption(base, 'second_round_core_share'), 2)}. Стресс-блок (+"
        f"{_money(stress['stress']['effective_quality_premium_usd_bbl'])}/bbl за качество, +"
        f"{_money(stress['stress']['effective_freight_usd_bbl'])}/bbl фрахт) считается только "
        f"для пика.")

    document.add_heading("Социальные последствия", level=1)
    _bullets(document, [
        f"Домохозяйства: +{_money(peak_b['household_monthly_cost_usd'])} в месяц вытесняют еду, "
        f"лекарства и платежи; эффект регрессивный — бедные тратят на бензин "
        f"{_fmt(100 * _assumption(base, 'low_income_gas_budget_share', 0), 0)}% бюджета против "
        f"{_fmt(100 * _assumption(base, 'median_gas_budget_share', 0), 0)}% у медианных.",
        "География и занятость: сельские и пригородные районы без альтернативы автомобилю, "
        "водители грузовиков и такси, малый бизнес с топливной составляющей.",
        f"Политика: рост цен на бензин исторически связан со снижением одобрения; наши "
        f"{_fmt(peak_b['approval_pressure_pp'], 1)} п.п. — иллюстративная чувствительность, "
        f"не оценка. Вероятность протестов, смены власти или насилия здесь не оценивается.",
        f"Стресс-вариант смещает дефицит в дизель и керосин: грузоперевозки, железная дорога, "
        f"сельхозтехника, генераторы, авиабилеты — социальный эффект сильнее, чем по бензину.",
    ])

    document.add_heading("Что делать", level=1)
    _bullets(document, [
        "Резервы/SPR: в сценарий уже заложен объявленный release; вариант без SPR собирается "
        "отдельным пакетом (--oil-scenario hormuz_closed_6m_no_spr) и даёт более высокий пик.",
        "Топливные послабления (смеси, waivers, импортные пошлины) — быстрый, но ограниченный "
        "эффект; измерять по недельным ценам и запасам.",
        f"Адресные выплаты бедным домохозяйствам: снимают регрессивную часть удара "
        f"({_fmt(peak_b['low_income_budget_shock_pct'], 1)}% дохода), но добавляют спросовую "
        f"инфляцию — важны скорость и таргетинг.",
        "Дипломатия/деэскалация и защита судоходства — главный фактор длительности шока, "
        "а не уровня первого месяца.",
        "Коммуникация: публиковать диапазон и триггеры, а не одну цифру; иначе разрыв ожиданий "
        "и реальности сам становится политическим фактором.",
    ])

    document.add_heading("Границы применимости", level=1)
    _bullets(document, [
        "Это не прогноз и не допуск модели: статус RESEARCH_ONLY, admission=false, "
        "causal_claim=false.",
        "Диапазон — разброс объявленных вариантов чувствительности, не доверительный интервал "
        "и не вероятность; социальный риск — иллюстративный индекс, а не вероятность беспорядков.",
        "Нет замещения, запасов нефтепродуктов, ограничений НПЗ и политической реакции внутри "
        "ценового пути; пик CPI может отставать от пика нефти на 1–3 месяца.",
        "Все потребительские параметры и стресс-надбавки — source_class=assumption "
        "(replace-with: AAA/EIA/BLS/ICE/IMO); полный список и ограничения источника — в "
        "03_Full_Report.json и на листе «Допущения и диапазоны».",
    ])
    document.save(str(out_path))


# ---------------------------------------------------------------------------------------- workbook
def _trigger_value(peak: dict, key: str):
    if key.startswith("gasoline_"):
        return peak.get("gasoline_usd_gal")
    if key.startswith("diesel_"):
        return peak.get("diesel_usd_gal")
    if key.startswith("cpi_"):
        return peak.get("cpi_headline_pp")
    if key.startswith("real_income_shock"):
        return peak.get("low_income_budget_shock_pct")
    return None


def build_workbook(out_path: Path, reports: dict, generated: datetime) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    try:
        workbook = Workbook()
    except Exception as exc:  # pragma: no cover - environment without openpyxl
        raise HormuzPackError(f"openpyxl is required for the workbook: {exc!r}") from exc
    head_fill = PatternFill("solid", fgColor="1F4E78")
    head_font = Font(color="FFFFFF", bold=True)
    note_font = Font(italic=True, color="555555")

    def head(sheet, columns):
        for index, name in enumerate(columns, start=1):
            cell = sheet.cell(row=1, column=index, value=name)
            cell.fill = head_fill
            cell.font = head_font

    base, stress = reports["base"], reports["stress"]
    peak_b, peak_s = base["peak"], stress["peak"]
    sblock = stress["stress"]

    sheet = workbook.active
    sheet.title = "Сценарии и месяцы"
    head(sheet, ["Сценарий", "Точка", "Brent, USD/bbl", "Бензин, $/gal", "Дизель, $/gal",
                 "Авиакеросин, $/gal", "CPI headline, п.п.", "Домохозяйство, $/мес",
                 "Бедные, % дохода", "Одобрение, п.п."])
    row_index = 2
    for month, row in enumerate(base["monthly"], start=1):
        for column, value in enumerate((
                "base", f"M{month}", row["brent_usd_bbl"], row["gasoline_usd_gal"],
                row["diesel_usd_gal"], "—", row["cpi_headline_pp"],
                row["household_monthly_cost_usd"], row["low_income_budget_shock_pct"],
                row["approval_pressure_pp"]), start=1):
            sheet.cell(row=row_index, column=column, value=value)
        row_index += 1
    for column, value in enumerate((
            "stress", "пик", peak_s["brent_usd_bbl"], sblock["gasoline_usd_gal"],
            sblock["diesel_usd_gal"], sblock["jet_usd_gal"], sblock["cpi_headline_pp"],
            "—", "—", "—"), start=1):
        sheet.cell(row=row_index, column=column, value=value)
    row_index += 2
    sheet.cell(row=row_index, column=1,
               value="Стресс-блок считается только для пика (исходный инструмент не строит "
                     "стресс-путь по месяцам); месячный путь — из базового варианта.").font = note_font
    sheet.cell(row=row_index + 1, column=1,
               value="Триггеры и пороги — на листе «Соцриск и триггеры»; допущения и диапазоны — "
                     "на листе «Допущения и диапазоны».").font = note_font

    assumptions = workbook.create_sheet("Допущения и диапазоны")
    head(assumptions, ["Блок", "Допущение", "Значение", "Low", "High", "Комментарий"])
    row_index = 2
    for name, block in sorted((base.get("assumptions") or {}).items()):
        assumptions.cell(row=row_index, column=1, value="потребительский")
        assumptions.cell(row=row_index, column=2, value=name)
        assumptions.cell(row=row_index, column=3, value=block.get("value"))
        assumptions.cell(row=row_index, column=4, value="—")
        assumptions.cell(row=row_index, column=5, value="—")
        assumptions.cell(row=row_index, column=6, value=block.get("note"))
        row_index += 1
    for name, block in sorted((sblock.get("assumptions") or {}).items()):
        assumptions.cell(row=row_index, column=1, value="стресс")
        assumptions.cell(row=row_index, column=2, value=name)
        assumptions.cell(row=row_index, column=3, value=block.get("value"))
        assumptions.cell(row=row_index, column=4, value=block.get("low", "—"))
        assumptions.cell(row=row_index, column=5, value=block.get("high", "—"))
        assumptions.cell(row=row_index, column=6, value=block.get("note"))
        row_index += 1
    row_index += 1
    assumptions.cell(row=row_index, column=1,
                     value="Все параметры — source_class=assumption: перед клиентским "
                           "использованием заменяются датированными значениями (AAA/EIA/BLS/"
                           "ICE/IMO).").font = note_font

    households = workbook.create_sheet("CPI и домохозяйства")
    head(households, ["Месяц", "Brent Δ, %", "Бензин Δ, %", "Дизель Δ, %",
                      "CPI прямой, п.п.", "CPI второй раунд, п.п.", "CPI headline, п.п.",
                      "Домохозяйство, $/мес", "Домохозяйство, $/год", "Бедные, % дохода",
                      "Медиана, % дохода", "Одобрение, п.п."])
    for month, row in enumerate(base["monthly"], start=2):
        for column, value in enumerate((
                f"M{month - 1}", row["brent_delta_pct"], row["gasoline_delta_pct"],
                row["diesel_delta_pct"], row["cpi_direct_pp"], row["cpi_core_round_pp"],
                row["cpi_headline_pp"], row["household_monthly_cost_usd"],
                row["household_annual_cost_usd"], row["low_income_budget_shock_pct"],
                row["median_budget_shock_pct"], row["approval_pressure_pp"]), start=1):
            households.cell(row=month, column=column, value=value)

    risk = workbook.create_sheet("Соцриск и триггеры")
    head(risk, ["Сценарий", "Балл 0–100", "Полоса", "Сработавшие триггеры / комментарий"])
    rows = [
        ("base", base["social_political"]["score_0_100"], base["social_political"]["band"],
         ", ".join(base["social_political"].get("reasons") or []) or "ни один порог не пройден"),
        ("stress", sblock["social_political"]["score_0_100"], sblock["social_political"]["band"],
         sblock["social_political"].get("note")),
    ]
    for row_index, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            risk.cell(row=row_index, column=column, value=value)
    row_index = 5
    for column, name in enumerate(["Триггер", "Порог", "base, значение", "base, статус",
                                   "stress, значение", "stress, статус"], start=1):
        cell = risk.cell(row=row_index, column=column, value=name)
        cell.fill = head_fill
        cell.font = head_font
    row_index += 1
    for key, threshold in sorted((base.get("thresholds") or {}).items()):
        value_b = _trigger_value(peak_b, key)
        value_s = _trigger_value(sblock, key)
        risk.cell(row=row_index, column=1, value=key)
        risk.cell(row=row_index, column=2, value=threshold)
        risk.cell(row=row_index, column=3, value=value_b)
        risk.cell(row=row_index, column=4,
                  value="—" if value_b is None else ("сработал" if value_b >= threshold
                                                     else "не сработал"))
        risk.cell(row=row_index, column=5, value=value_s)
        risk.cell(row=row_index, column=6,
                  value="—" if value_s is None else ("сработал" if value_s >= threshold
                                                     else "не сработал"))
        row_index += 1
    row_index += 1
    risk.cell(row=row_index, column=1, value="Мониторинг").font = head_font
    row_index += 1
    for item in base.get("monitoring") or []:
        risk.cell(row=row_index, column=1, value=item)
        row_index += 1
    row_index += 1
    risk.cell(row=row_index, column=1, value="Варианты политики").font = head_font
    row_index += 1
    for item in base.get("policy_options") or []:
        risk.cell(row=row_index, column=1, value=item)
        row_index += 1
    row_index += 1
    risk.cell(row=row_index, column=1,
              value="Индекс социального риска иллюстративный: это не вероятность беспорядков "
                    "и не причинное утверждение.").font = note_font

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
    for column, width in {"A": 34, "B": 12, "C": 14, "D": 14, "E": 14, "F": 14, "G": 16,
                          "H": 16, "I": 14, "J": 14, "K": 14, "L": 14}.items():
        for sheet in workbook.worksheets:
            sheet.column_dimensions[column].width = width

    properties = workbook.properties
    properties.creator = "AMFE"
    properties.lastModifiedBy = "AMFE hormuz client pack"
    properties.created = generated.replace(tzinfo=None)
    properties.modified = generated.replace(tzinfo=None)
    workbook.save(str(out_path))


# ------------------------------------------------------------------------------------------ JSON
def build_machine_json(out_path: Path, reports: dict, scenario: str, client: str,
                       oil_scenario: str, generated_at: str, files: list[dict]) -> None:
    base, stress = reports["base"], reports["stress"]
    document = {
        "schema": SCHEMA,
        "kind": "hormuz_us_client_pack",
        "status": STATUS,
        "admission": False,
        "causal_claim": False,
        "generated_at": generated_at,
        "client": client,
        "scenario": scenario,
        "oil_scenario": oil_scenario,
        "disclaimer": DISCLAIMER,
        "selected": scenario,
        "report": reports[scenario],
        "reports": {"base": base, "stress": stress},
        "sha256": {
            "base_content": base["content_sha256"],
            "stress_content": stress["content_sha256"],
            "selected_content": reports[scenario]["content_sha256"],
        },
        "files": [{"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"],
                   "deterministic": item["deterministic"]}
                  for item in sorted(files, key=lambda entry: entry["path"])],
        "numbers_source": "hormuz_us_impact.build_report(scenario, generated_at, stress=...) — "
                          "полный отчёт без пересчёта и без округления в пакете",
        "licence": {
            "research_only": True,
            "admission": False,
            "causal_claim": False,
            "no_third_party_observations": True,
            "no_investment_advice": True,
            "assumptions_replace_with": [
                {"name": name, "source_class": "assumption", "note": block.get("note")}
                for name, block in sorted((base.get("assumptions") or {}).items())],
        },
        "policy": {"read_only": True, "research_only": True, "no_admission": True,
                   "no_raw_data": True, "deterministic_without_pdf": True},
    }
    _write_json(out_path, document)


# ------------------------------------------------------------------------------------------ HTML
def build_html(out_path: Path, reports: dict, scenario: str, client: str, oil_scenario: str,
               generated_at: str, has_pdf: bool) -> None:
    base, stress = reports["base"], reports["stress"]
    peak_b, peak_s = base["peak"], stress["peak"]
    sblock = stress["stress"]
    oil = base["oil_module"]
    band, sband = base["social_political"], sblock["social_political"]
    esc = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    monthly = "".join(
        f"<tr><td>M{month}</td><td class=\"num\">{_fmt(row['brent_usd_bbl'])}</td>"
        f"<td class=\"num\">{_fmt(row['gasoline_usd_gal'])}</td>"
        f"<td class=\"num\">{_fmt(row['diesel_usd_gal'])}</td>"
        f"<td class=\"num\">{_fmt(row['cpi_headline_pp'])}</td>"
        f"<td class=\"num\">{_fmt(row['household_monthly_cost_usd'])}</td></tr>"
        for month, row in enumerate(base["monthly"], start=1))
    comparison = "".join(
        f"<tr><td>{label}</td><td class=\"num\">{left}</td><td class=\"num\">{right}</td></tr>"
        for label, left, right in (
            ("Brent, USD/bbl", _fmt(peak_b["brent_usd_bbl"]), _fmt(peak_s["brent_usd_bbl"])),
            ("Поставленная нефть Δ, USD/bbl", "—",
             f"+{_fmt(sblock['delivered_crude_delta_usd_bbl'])}"),
            ("Бензин, $/gal", _fmt(peak_b["gasoline_usd_gal"]), _fmt(sblock["gasoline_usd_gal"])),
            ("Дизель, $/gal", _fmt(peak_b["diesel_usd_gal"]), _fmt(sblock["diesel_usd_gal"])),
            ("Авиакеросин, $/gal", "—", _fmt(sblock["jet_usd_gal"])),
            ("CPI headline, п.п.", _fmt(peak_b["cpi_headline_pp"]),
             _fmt(sblock["cpi_headline_pp"])),
            ("Домохозяйство, $/мес", _fmt(peak_b["household_monthly_cost_usd"]), "—"),
            ("Соцриск, 0–100", f"{band['score_0_100']} ({band['band']})",
             f"{sband['score_0_100']} ({sband['band']})"),
        ))
    trigger_rows = "".join(
        f"<tr><td>{esc(key)}</td><td class=\"num\">{_fmt(threshold)}</td>"
        f"<td class=\"num\">{_fmt(_trigger_value(peak_b, key))}</td>"
        f"<td class=\"num\">{_fmt(_trigger_value(sblock, key))}</td></tr>"
        for key, threshold in sorted((base.get("thresholds") or {}).items()))
    limitations = "".join(f"<li>{esc(item)}</li>" for item in base.get("limitations") or [])
    policy = "".join(f"<li>{esc(item)}</li>" for item in base.get("policy_options") or [])
    monitoring = "".join(f"<li>{esc(item)}</li>" for item in base.get("monitoring") or [])
    docx_link = f"<a href=\"{ASSETS['executive']}.docx\">{ASSETS['executive']}.docx</a>"
    pdf_link = (f" · <a href=\"{ASSETS['executive']}.pdf\">{ASSETS['executive']}.pdf</a>"
                if has_pdf else "")
    content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMFE — Ормуз → США (research only)</title>
<style>
  body {{ font-family: system-ui, "Segoe UI", sans-serif; max-width: 64rem; margin: 0 auto;
         padding: 1.5rem; line-height: 1.45; color: #1b1b1b; }}
  h1 {{ font-size: 1.45rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.8rem; }}
  .meta {{ color: #666; font-size: 0.9rem; }}
  .banner {{ border-left: 4px solid #b45309; background: #b4530915; padding: .7rem 1rem;
             margin: 1rem 0; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .card {{ border: 1px solid #8884; border-radius: 8px; padding: .8rem 1rem; min-width: 12rem; }}
  .card .v {{ font-size: 1.4rem; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin: .6rem 0; }}
  th, td {{ border: 1px solid #8884; padding: .35rem .5rem; text-align: left; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  footer {{ margin-top: 2rem; border-top: 1px solid #8884; padding-top: .7rem;
            color: #666; font-size: .85rem; }}
</style>
</head>
<body>
  <h1>AMFE — Ормуз → США: цены, CPI, домохозяйства, социальный риск</h1>
  <p class="meta">Клиент: {esc(client)} · пакет: {esc(scenario)} · нефтяной сценарий:
     {esc(oil_scenario)} · статус: {STATUS} · admission=false · generated_at:
     {esc(generated_at)}</p>
  <div class="banner">{esc(DISCLAIMER)}</div>
  <div class="cards">
    <div class="card"><div>Brent, пик</div>
      <div class="v">{_fmt(oil['price_peak_usd_bbl'])} USD/bbl</div>
      <div class="meta">диапазон {_fmt(oil['price_range_usd_bbl']['low'])}–
        {_fmt(oil['price_range_usd_bbl']['high'])}</div></div>
    <div class="card"><div>Бензин, пик</div>
      <div class="v">{_money(peak_b['gasoline_usd_gal'])}/gal</div>
      <div class="meta">{_money(peak_b['gasoline_range_usd_gal']['low'])}–
        {_money(peak_b['gasoline_range_usd_gal']['high'])} (чувствительность)</div></div>
    <div class="card"><div>Домохозяйство</div>
      <div class="v">+{_money(peak_b['household_monthly_cost_usd'])}</div>
      <div class="meta">в месяц, +{_money(peak_b['household_annual_cost_usd'])} в год</div></div>
    <div class="card"><div>Соцриск</div>
      <div class="v">{band['score_0_100']}/100</div>
      <div class="meta">{esc(band['band'])}; стресс {sband['score_0_100']}/100
        ({esc(sband['band'])})</div></div>
  </div>
  <h2>Базовый и стресс-вариант (пик)</h2>
  <table><thead><tr><th>Показатель</th><th>base</th><th>stress</th></tr></thead>
    <tbody>{comparison}</tbody></table>
  <h2>Путь по месяцам (base)</h2>
  <table><thead><tr><th>Месяц</th><th>Brent</th><th>Бензин, $/gal</th><th>Дизель, $/gal</th>
    <th>CPI, п.п.</th><th>Домохозяйство, $/мес</th></tr></thead>
    <tbody>{monthly}</tbody></table>
  <h2>Триггеры (мониторинг, не прогноз)</h2>
  <table><thead><tr><th>Триггер</th><th>Порог</th><th>base</th><th>stress</th></tr></thead>
    <tbody>{trigger_rows}</tbody></table>
  <h2>Варианты политики (из отчёта)</h2><ul>{policy}</ul>
  <h2>Мониторинг</h2><ul>{monitoring}</ul>
  <h2>Ограничения</h2><ul>{limitations}</ul>
  <footer>Research only: не прогноз, не допуск модели, не вероятность беспорядков и не
  инвестиционная рекомендация. Диапазоны — варианты чувствительности, не доверительный
  интервал. Файлы пакета: {docx_link}{pdf_link} ·
  <a href="{ASSETS['workbook']}.xlsx">{ASSETS['workbook']}.xlsx</a> ·
  <a href="{ASSETS['json']}.json">{ASSETS['json']}.json</a> ·
  <a href="manifest.json">manifest.json</a></footer>
</body>
</html>
"""
    _write_text(out_path, content)


# ------------------------------------------------------------------------------------- manifest
def _kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {".docx": "docx", ".pdf": "pdf", ".xlsx": "xlsx", ".json": "json", ".html": "html",
            ".md": "markdown"}.get(suffix, suffix.lstrip(".") or "unknown")


def build_manifest(out_path: Path, files: list[dict], reports: dict, scenario: str, client: str,
                   oil_scenario: str, generated_at: str, command: str,
                   has_pdf: bool) -> dict:
    entries = [{"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"],
                "format": _kind(item["path"]), "deterministic": item["deterministic"]}
               for item in sorted(files, key=lambda entry: entry["path"])]
    document = {
        "schema": MANIFEST_SCHEMA,
        "kind": "client_pack_manifest",
        "status": STATUS,
        "admission": False,
        "causal_claim": False,
        "generated_at": generated_at,
        "client": client,
        "scenario": scenario,
        "oil_scenario": oil_scenario,
        "versions": {
            "pack": TOOL_VERSION,
            "pack_schema": SCHEMA,
            "impact_schema": IMPACT_SCHEMA,
            "oil_module": "oil.brent",
            "oil_scenario": oil_scenario,
            "python": sys.version.split()[0],
        },
        "integrity": {
            "algorithm": "sha256",
            "files": len(entries),
            "manifest_excludes_itself": True,
            "pdf_included": has_pdf,
            "deterministic_artifacts": sum(1 for item in entries if item["deterministic"]),
        },
        "content_sha256": {"base": reports["base"]["content_sha256"],
                           "stress": reports["stress"]["content_sha256"]},
        "files": entries,
        "command": command,
        "notes": [
            "SHA-256 каждого файла пакета; сам manifest.json в список не входит (self-hash "
            "невозможен) — он проверяется по содержимому каталога.",
            "PDF собирается LibreOffice и может нести метки конвертера: помечен "
            "deterministic=false и не входит в гарантию байт-идентичности.",
            "DOCX/XLSX/JSON/HTML/README детерминированы при фиксированном --generated-at.",
        ],
        "disclaimer": DISCLAIMER,
    }
    _write_json(out_path, document)
    return document


# -------------------------------------------------------------------------------- readme/reproduce
def _readme(reports: dict, scenario: str, client: str, oil_scenario: str, generated_at: str,
            files: list[dict], has_pdf: bool) -> str:
    base, stress = reports["base"], reports["stress"]
    sblock = stress["stress"]
    lines = [
        "# AMFE — клиентский пакет «Ормуз → США»",
        "",
        f"Клиент: **{client}**. Пакет: `{scenario}` (ведущий вариант). Нефтяной сценарий: "
        f"`{oil_scenario}`. Статус: **{STATUS}**, admission=false. Сгенерировано: {generated_at}.",
        "",
        "> " + DISCLAIMER,
        "",
        "## Файлы",
        "",
        "| Файл | Формат | Содержание |",
        "|---|---|---|",
        f"| `{ASSETS['executive']}` | DOCX" + (" + PDF" if has_pdf else "") +
        " | executive summary: цифры, социальные последствия, что делать, границы |",
        f"| `{ASSETS['workbook']}` | XLSX | Сценарии и месяцы, Допущения и диапазоны, "
        "CPI и домохозяйства, Соцриск и триггеры |",
        f"| `{ASSETS['json']}` | JSON | полный отчёт base+stress, SHA-256, лицензия |",
        f"| `{ASSETS['html']}` | HTML | офлайн-версия, без JS и внешних ресурсов |",
        "| `manifest.json` | JSON | SHA-256 каждого файла, версии, статус |",
        "| `README.md` | Markdown | эта инструкция |",
        "",
        "## Ключевые числа (пик)",
        "",
        f"- Brent: {_fmt(base['oil_module']['price_peak_usd_bbl'])} USD/bbl, диапазон "
        f"{_fmt(base['oil_module']['price_range_usd_bbl']['low'])}–"
        f"{_fmt(base['oil_module']['price_range_usd_bbl']['high'])};",
        f"- бензин base {_money(base['peak']['gasoline_usd_gal'])}/gal, stress "
        f"{_money(stress['stress']['gasoline_usd_gal'])}/gal;",
        f"- дизель base {_money(base['peak']['diesel_usd_gal'])}/gal, stress "
        f"{_money(stress['stress']['diesel_usd_gal'])}/gal;",
        f"- CPI headline base {_pp(base['peak']['cpi_headline_pp'])}, stress "
        f"{_pp(sblock['cpi_headline_pp'])};",
        f"- домохозяйство +{_money(base['peak']['household_monthly_cost_usd'])}/мес;",
        f"- соцриск base {base['social_political']['score_0_100']}/100 "
        f"({base['social_political']['band']}), stress "
        f"{stress['stress']['social_political']['score_0_100']}/100 "
        f"({stress['stress']['social_political']['band']}).",
        "",
        "## Как проверить",
        "",
        "```powershell",
        "python -c \"import json,hashlib,pathlib;m=json.load(open('manifest.json',encoding='utf-8'));"
        "print([ (f['path'], hashlib.sha256(pathlib.Path(f['path']).read_bytes()).hexdigest()"
        "==f['sha256']) for f in m['files']])\"",
        "```",
        "",
        "## Воспроизведение",
        "",
        "```powershell",
        f"python tools/hormuz_us_client_pack.py --out build/client_pack/hormuz_us "
        f"--scenario {scenario} --client \"{client}\" --generated-at {generated_at}",
        "```",
        "",
        "PDF-конвертация выполняется LibreOffice, если он установлен; иначе пакет остаётся с DOCX "
        "(флаг `--no-pdf` отключает конвертацию явно).",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ assemble
def _record(files: list[dict], path: Path, deterministic: bool = True) -> None:
    files.append({"path": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size,
                  "deterministic": deterministic})


def build_pack(out_dir: Path = DEFAULT_OUT, scenario: str = DEFAULT_SCENARIO,
               client: str = DEFAULT_CLIENT, generated_at: str | None = None,
               make_pdf: bool = True, oil_scenario: str = DEFAULT_OIL_SCENARIO,
               reports: dict | None = None) -> dict:
    """Build the Hormuz -> US client package; every number comes from hormuz_us_impact."""
    if not client or not client.strip():
        raise HormuzPackError("client name must not be empty")
    if scenario not in SCENARIOS:
        raise HormuzPackError(f"unknown scenario {scenario!r}; expected one of {list(SCENARIOS)}")
    generated_at = generated_at or _now()
    generated = _datetime(generated_at)
    reports = build_reports(oil_scenario, generated_at, reports)
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HormuzPackError(f"cannot create output directory {out_dir}: {exc!r}") from exc
    files: list[dict] = []

    executive = out_dir / f"{ASSETS['executive']}.docx"
    build_executive(executive, reports, scenario, client, oil_scenario, generated_at, generated)
    _normalise_office_zip(executive, generated)
    _record(files, executive)

    workbook = out_dir / f"{ASSETS['workbook']}.xlsx"
    build_workbook(workbook, reports, generated)
    _normalise_office_zip(workbook, generated)
    _record(files, workbook)

    has_pdf = False
    if make_pdf:
        pdf = convert_pdf(executive, out_dir)
        if pdf is not None:
            has_pdf = True
            _record(files, pdf, deterministic=False)

    build_html(out_dir / f"{ASSETS['html']}.html", reports, scenario, client, oil_scenario,
               generated_at, has_pdf)
    _record(files, out_dir / f"{ASSETS['html']}.html")

    build_machine_json(out_dir / f"{ASSETS['json']}.json", reports, scenario, client, oil_scenario,
                       generated_at, files)
    _record(files, out_dir / f"{ASSETS['json']}.json")

    _write_text(out_dir / "README.md",
                _readme(reports, scenario, client, oil_scenario, generated_at, files, has_pdf))
    _record(files, out_dir / "README.md")

    command = (f"python tools/hormuz_us_client_pack.py --out {out_dir.as_posix()} --scenario {scenario} "
               f"--oil-scenario {oil_scenario} --client {client!r} "
               f"--generated-at {generated_at}")
    manifest_path = out_dir / "manifest.json"
    build_manifest(manifest_path, files, reports, scenario, client, oil_scenario, generated_at,
                   command, has_pdf)

    base, stress = reports["base"], reports["stress"]
    return {
        "ok": True,
        "schema": SCHEMA,
        "status": STATUS,
        "admission": False,
        "client": client,
        "scenario": scenario,
        "oil_scenario": oil_scenario,
        "generated_at": generated_at,
        "out": str(out_dir).replace("\\", "/"),
        "files": sorted([item["path"] for item in files] + [manifest_path.name]),
        "pdf": has_pdf,
        "manifest_sha256": _sha256(manifest_path),
        "content_sha256": {"base": base["content_sha256"],
                           "stress": stress["content_sha256"]},
        "numbers": {
            "base": {"brent_peak": base["oil_module"]["price_peak_usd_bbl"],
                     "gasoline_peak": base["peak"]["gasoline_usd_gal"],
                     "diesel_peak": base["peak"]["diesel_usd_gal"],
                     "cpi_peak_pp": base["peak"]["cpi_headline_pp"],
                     "household_monthly": base["peak"]["household_monthly_cost_usd"],
                     "risk": base["social_political"]["score_0_100"],
                     "band": base["social_political"]["band"]},
            "stress": {"gasoline_peak": stress["stress"]["gasoline_usd_gal"],
                       "diesel_peak": stress["stress"]["diesel_usd_gal"],
                       "jet_peak": stress["stress"]["jet_usd_gal"],
                       "cpi_peak_pp": stress["stress"]["cpi_headline_pp"],
                       "delivered_crude_delta": stress["stress"]["delivered_crude_delta_usd_bbl"],
                       "risk": stress["stress"]["social_political"]["score_0_100"],
                       "band": stress["stress"]["social_political"]["band"]},
        },
    }


def verify_manifest(out_dir: Path, manifest_name: str = "manifest.json") -> dict:
    """Recompute every SHA-256 listed in the manifest; return the verification report."""
    out_dir = Path(out_dir)
    manifest_path = out_dir / manifest_name
    if not manifest_path.is_file():
        raise HormuzPackError(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked, mismatched, missing = [], [], []
    for entry in manifest.get("files") or []:
        path = out_dir / entry["path"]
        if not path.is_file():
            missing.append(entry["path"])
            continue
        digest = _sha256(path)
        if digest != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            mismatched.append(entry["path"])
        checked.append(entry["path"])
    present = sorted(item.name for item in out_dir.iterdir() if item.is_file())
    untracked = [name for name in present if name not in checked + [manifest_name]]
    return {
        "ok": not mismatched and not missing and not untracked,
        "schema": manifest.get("schema"),
        "status": manifest.get("status"),
        "admission": manifest.get("admission"),
        "checked": len(checked),
        "mismatched": mismatched,
        "missing": missing,
        "untracked": untracked,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the AMFE Hormuz -> US client pack (RESEARCH_ONLY)")
    parser.add_argument("--out", default=None,
                        help="output directory (default build/client_pack/hormuz_us)")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=SCENARIOS,
                        help="leading variant of the pack: base or stress (both are always built)")
    parser.add_argument("--oil-scenario", default=DEFAULT_OIL_SCENARIO, choices=OIL_SCENARIOS,
                        help="oil module scenario that drives the price path")
    parser.add_argument("--client", default=DEFAULT_CLIENT, help="client name on the documents")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 timestamp YYYY-MM-DDTHH:MM:SSZ (determinism)")
    parser.add_argument("--no-pdf", action="store_true", help="skip LibreOffice PDF conversion")
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out) if args.out else DEFAULT_OUT
    try:
        summary = build_pack(out, scenario=args.scenario, client=args.client,
                             generated_at=args.generated_at, make_pdf=not args.no_pdf,
                             oil_scenario=args.oil_scenario)
    except HormuzPackError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps({"status": "OK", "out": summary["out"], "scenario": summary["scenario"],
                          "oil_scenario": summary["oil_scenario"], "pdf": summary["pdf"],
                          "files": len(summary["files"]), "numbers": summary["numbers"]},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
