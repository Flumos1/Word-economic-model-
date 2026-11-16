#!/usr/bin/env python3
"""AMFE static Russian website builder: public research site generated from real artefacts.

The site is a **derived, read-only publication** of already registered results. It adds no numbers of
its own: every figure is traceable to a committed protocol/report or to a generated artefact:

  * ``build/product/forecast_latest.json`` - the T1 CPI product feed (CANDIDATE);
  * ``build/world/report_index.json`` and ``build/world/reports/*.json`` - FIGARO 2019 scenario reports
    (RESEARCH_ONLY comparative statics);
  * ``protocols/first_real_evaluation_report_v5.json`` - the first real evaluation of T1 CPI;
  * ``protocols/regime_v4_candidate_evaluation_report.json`` - the SHADOW_TEST candidate;
  * ``build/shadow/shadow_status.json`` - the shadow-track status (C6 progress);
  * ``data/mrio/**/manifest.json`` and ``data/mrio/reports/blocked.json`` - dataset coverage and
    blocked-source facts (metadata only, no raw values);
  * ``docs/120,125,129,131`` - titles and links for the documentation page.

It writes a static Russian site to ``site/`` with no JavaScript, no frameworks and no external URLs:

    site/index.html, model.html, forecast.html, scenarios.html, data.html, methodology.html,
    docs.html, assets/style.css

Everything except ``generated_at`` is a pure function of the inputs: the same repository and the same
``--generated-at`` produce byte-identical files. The build is fail-closed: a missing or broken required
input aborts with exit code ``2`` and writes nothing. Only derived aggregates are published (coverage
percentages, totals, top lists); raw MRIO matrices and raw observations never enter the site, and the
disclaimer "не допуск / не инвестиционная рекомендация" is present on every page.

Usage:
    python tools/site_build.py
    python tools/site_build.py --generated-at 2026-11-14T12:00:00Z --out-dir site
    python -m unittest tests.test_site_build -v
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FEED = "build/product/forecast_latest.json"
WORLD_INDEX = "build/world/report_index.json"
WORLD_REPORTS = "build/world/reports"
REPORT_V5 = "protocols/first_real_evaluation_report_v5.json"
REPORT_V4 = "protocols/regime_v4_candidate_evaluation_report.json"
SHADOW = "build/shadow/shadow_status.json"
FIGARO_MANIFEST = "data/mrio/figaro/figaro_2019/manifest.json"
WIOD_MANIFESTS = (
    "data/mrio/wiod_2016/wiod_2000/manifest.json",
    "data/mrio/wiod_2016/wiod_2007/manifest.json",
    "data/mrio/wiod_2016/wiod_2014/manifest.json",
)
BLOCKED = "data/mrio/reports/blocked.json"

DOC_FILES = (
    "docs/120_Аудит_ТЗ_и_план_P2.md",
    "docs/121_Данные_MRIO.md",
    "docs/122_IO_SFC_ядро.md",
    "docs/123_GVAR_прототип.md",
    "docs/124_Поддержка_продукта.md",
    "docs/125_Реальный_MRIO_FIGARO.md",
    "docs/126_Полный_мир_источники.md",
    "docs/127_Производительность_мирового_конвейера.md",
    "docs/128_GVAR_на_реальных_данных.md",
    "docs/129_Схема_v1.1_и_WIOD.md",
    "docs/130_OECD_ICIO.md",
    "docs/131_Сценарные_отчёты_мира.md",
    "docs/135_Альтернативная_динамика_D4.md",
    "docs/136_Мезо_микро_труд_домохозяйства.md",
    "docs/137_P2_0.1_релиз.md",
    "docs/138_Внешняя_проверка_P2_0.1.md",
    "docs/139_C6_календарь.md",
    "docs/140_Кейсы_заказчиков_и_ценность.md",
    "docs/141_Кейсы_заказчиков_20_дополнительных.md",
    "docs/142_Клиентский_пакет.md",
    "docs/143_Отличие_от_ИИ_чата.md",
    "docs/144_Товарные_модули_архитектура.md",
    "docs/145_AMFE_2.0_план.md",
    "docs/146_P2_0.2.md",
    "docs/147_Рынки_нефть.md",
    "docs/148_Агро_соя.md",
    "docs/149_Финансовый_слой.md",
    "docs/150_Агентный_симулятор.md",
    "docs/151_Обучение_валидация.md",
    "docs/152_Мост_цена_MRIO.md",
    "docs/153_Мост_агро_MRIO.md",
    "docs/154_ABM_SFC_интеграция.md",
    "docs/155_Бэктест_нефти.md",
    "docs/156_Forecast_API_и_рыночный_пакет.md",
    "docs/157_Реестр_модулей.md",
    "docs/119_Регламент_теневого_трека.md",
    "docs/113_Продуктовый_пакет.md",
    "docs/114_Публикационный_пакет.md",
)
DOC_TITLES = {
    "docs/120_Аудит_ТЗ_и_план_P2.md": "Аудит ТЗ и план P2",
    "docs/121_Данные_MRIO.md": "Данные MRIO",
    "docs/122_IO_SFC_ядро.md": "IO/SFC-ядро",
    "docs/123_GVAR_прототип.md": "GVAR-прототип",
    "docs/124_Поддержка_продукта.md": "Поддержка продукта",
    "docs/125_Реальный_MRIO_FIGARO.md": "Реальный MRIO FIGARO",
    "docs/126_Полный_мир_источники.md": "Источники полного мира",
    "docs/127_Производительность_мирового_конвейера.md": "Производительность конвейера",
    "docs/128_GVAR_на_реальных_данных.md": "GVAR на реальных данных",
    "docs/129_Схема_v1.1_и_WIOD.md": "Схема v1.1 и WIOD",
    "docs/130_OECD_ICIO.md": "OECD ICIO",
    "docs/131_Сценарные_отчёты_мира.md": "Сценарные отчёты мира",
    "docs/135_Альтернативная_динамика_D4.md": "Альтернативная динамика D4",
    "docs/136_Мезо_микро_труд_домохозяйства.md": "Мезо/микро: труд и домохозяйства",
    "docs/137_P2_0.1_релиз.md": "P2 0.1 — релиз",
    "docs/138_Внешняя_проверка_P2_0.1.md": "Внешняя проверка P2 0.1",
    "docs/139_C6_календарь.md": "C6-календарь",
    "docs/140_Кейсы_заказчиков_и_ценность.md": "Кейсы заказчиков",
    "docs/141_Кейсы_заказчиков_20_дополнительных.md": "Ещё 20 кейсов",
    "docs/142_Клиентский_пакет.md": "Клиентский пакет",
    "docs/143_Отличие_от_ИИ_чата.md": "Отличие от ИИ-чата",
    "docs/144_Товарные_модули_архитектура.md": "Товарные модули",
    "docs/145_AMFE_2.0_план.md": "AMFE 2.0 — план",
    "docs/146_P2_0.2.md": "P2 0.2 — релиз",
    "docs/147_Рынки_нефть.md": "Рынки: нефть",
    "docs/148_Агро_соя.md": "Агро и соя",
    "docs/149_Финансовый_слой.md": "Финансовый слой",
    "docs/150_Агентный_симулятор.md": "Агентный симулятор",
    "docs/151_Обучение_валидация.md": "Обучение и валидация",
    "docs/152_Мост_цена_MRIO.md": "Мост цена → MRIO",
    "docs/153_Мост_агро_MRIO.md": "Мост агро → MRIO",
    "docs/154_ABM_SFC_интеграция.md": "ABM → SFC",
    "docs/155_Бэктест_нефти.md": "Бэктест нефти",
    "docs/156_Forecast_API_и_рыночный_пакет.md": "Forecast API и пакет",
    "docs/157_Реестр_модулей.md": "Реестр модулей",
    "docs/119_Регламент_теневого_трека.md": "Регламент теневого трека",
    "docs/113_Продуктовый_пакет.md": "Продуктовый пакет",
    "docs/114_Публикационный_пакет.md": "Публикационный пакет",
}
SCENARIO_IDS = ("energy_minus5", "energy_plus50", "import_plus10", "import_plus20",
                "fx_minus10_DE", "fx_minus20_DE", "fx_minus10_FR", "fx_minus20_FR")
TOP_SCENARIOS = ("energy_minus5", "import_plus10", "fx_minus20_DE")

DISCLAIMER = ("Это исследовательский артефакт, а не допущенная модель и не инвестиционная "
              "рекомендация. Статусы не присваиваются сайтом.")
EN_DISCLAIMER = ("Research only — not admission, not investment advice. AMFE is a research project: "
                 "CANDIDATE means the model is not admitted for use; the numbers are evaluation and "
                 "scenario results, not advice or trading forecasts.")

PAGES = ("index", "model", "forecast", "scenarios", "data", "methodology", "docs", "status")

SITE_URL = "https://flumos1.github.io/Word-economic-model-/"
REPO_URL = "https://github.com/Flumos1/Word-economic-model-"
SITE_DESCRIPTIONS = {
    "ru": ("AMFE — исследовательская модель мировой экономики: межстрановые таблицы затрат–выпуск "
           "FIGARO, WIOD и OECD ICIO, прогноз инфляции США CPI и сценарные отчёты. Не допуск и не "
           "инвестиционная рекомендация."),
    "en": ("AMFE is a research model of the world economy: FIGARO, WIOD and OECD ICIO inter-country "
           "input–output tables, a US CPI inflation forecast and scenario reports. Research only — "
           "not admission, not investment advice."),
}
CITATION = {
    "ru": """@misc{amfe2026,
  title  = {AMFE: Adaptive MacroForecast Engine — исследовательская модель мировой экономики},
  year   = {2026},
  note   = {Версия сайта: %s; протокол оценивания %s; статус CANDIDATE/SHADOW_TEST},
  url    = {%s}
}""",
    "en": """@misc{amfe2026,
  title  = {AMFE: Adaptive MacroForecast Engine — a research model of the world economy},
  year   = {2026},
  note   = {Site snapshot: %s; evaluation protocol %s; status CANDIDATE/SHADOW_TEST},
  url    = {%s}
}""",
}


class SiteBuildError(RuntimeError):
    """A required input is missing, broken or inconsistent: the site is not written."""


# --------------------------------------------------------------------------------------- helpers
def read_json(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise SiteBuildError(f"{label}: нет файла {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SiteBuildError(f"{label}: не удалось прочитать {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise SiteBuildError(f"{label}: ожидался JSON-объект")
    return document


def oecd_source_row(lang: str = "ru") -> list[str]:
    """OECD ICIO row: auto-downloaded 2016 edition when the coverage protocol exists, else blocked."""
    try:
        coverage = read_json(ROOT / "protocols" / "oecd_icio_coverage.json", "OECD coverage")
    except SiteBuildError:
        coverage = {}
    if coverage:
        release_2025 = coverage.get("release_2025") or {}
        if release_2025.get("status") == "AVAILABLE":
            if lang == "en":
                return ["OECD ICIO 2025 (auto: 81×50, 1995–2022)", "AVAILABLE_2025",
                        "Official SML release downloaded from the OECD webfs links (Range download); "
                        "D1 PASS for 28 years, world value-added coverage 94.6–97.2%",
                        "2016 edition (71×34, 2000–2011) stays as the fallback dataset (docs/130)"]
            return ["OECD ICIO 2025 (авто: 81×50, 1995–2022)", "AVAILABLE_2025",
                    "Официальный SML-релиз скачан по ссылкам webfs OECD (Range); D1 PASS за 28 лет, "
                    "покрытие мировой ДС 94,6–97,2%",
                    "Редакция 2016 (71×34, 2000–2011) остаётся запасным датасетом (docs/130)"]
        if lang == "en":
            return ["OECD ICIO 2016 (auto: 71×34, 1995–2011)", "AVAILABLE_AUTO_2016",
                    "Official per-year OECD archives (2016 edition) retrieved via Internet Archive; "
                    "D1 PASS 2000–2011, world value-added coverage 95.4%; 1995–1999 fail only on one "
                    "negative final-demand cell",
                    "2025 release (60+×45, 1995–2022) — browser download only (docs/130)"]
        return ["OECD ICIO 2016 (авто: 71×34, 1995–2011)", "AVAILABLE_AUTO_2016",
                "Официальные per-year архивы OECD (2016 edition) получены через Internet Archive; "
                "D1 PASS 2000–2011, покрытие мировой ДС 95,4%; 1995–1999 — FAIL только по одной "
                "отрицательной ячейке спроса",
                "Релиз 2025 (60+×45, 1995–2022) — только вручную в браузере (docs/130)"]
    if lang == "en":
        return ["OECD ICIO 2025", "BLOCKED_AUTOMATED",
                "Non-browser clients get HTTP 403; the terms are accepted in the browser",
                "Download manually in the browser, accept the terms, put files in "
                "data/mrio/oecd_icio/archive/"]
    return ["OECD ICIO 2025", "BLOCKED_AUTOMATED",
            "Небраузерные клиенты получают HTTP 403; условия требуют принятия в браузере",
            "Скачать вручную в браузере, принять условия, положить в data/mrio/oecd_icio/archive/"]


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def fmt(value, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_int(value) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def fmt_pct(value, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_sci(value) -> str:
    try:
        return f"{float(value):.2g}"
    except (TypeError, ValueError):
        return "—"


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SiteBuildError(f"{label}: ожидалось число")
    return float(value)


def _criterion_value(block: dict, window: str, loss: str, field: str):
    return ((((block.get("windows") or {}).get(window) or {}).get(loss)) or {}).get(field)


def _svg_interval_ladder(quantiles: list[float], point: float, lang: str = "ru") -> str:
    """Inline SVG: 50/80/90% intervals around the point forecast (no JS, deterministic)."""
    width, height, left, right, top = 720, 200, 120, 24, 26
    low = min(min(quantiles), point) - 1.0
    high = max(max(quantiles), point) + 1.0
    span = high - low or 1.0

    def x(value: float) -> float:
        return left + (float(value) - low) / span * (width - left - right)

    rows = [("90%", quantiles[0], quantiles[18], "#94a3b8"),
            ("80%", quantiles[1], quantiles[17], "#64748b"),
            ("50%", quantiles[4], quantiles[14], "#334155")]
    parts = [f'<svg class="chart" role="img" viewBox="0 0 {width} {height}" '
             f'aria-label="{"Интервалы прогноза CPI" if lang == "ru" else "CPI forecast intervals"}">']
    for index, (label, low_value, high_value, color) in enumerate(rows):
        y = top + index * 48
        parts.append(f'<text x="{left - 12}" y="{y + 16}" text-anchor="end" '
                     f'class="chart-label">{label}</text>')
        parts.append(f'<rect x="{x(low_value):.1f}" y="{y}" width="{max(1.0, x(high_value) - x(low_value)):.1f}" '
                     f'height="22" rx="4" fill="{color}" opacity="0.85"></rect>')
        parts.append(f'<text x="{x(low_value) - 6:.1f}" y="{y + 16}" text-anchor="end" '
                     f'class="chart-value">{fmt(low_value, 2)}</text>')
        parts.append(f'<text x="{x(high_value) + 6:.1f}" y="{y + 16}" '
                     f'class="chart-value">{fmt(high_value, 2)}</text>')
    point_x = x(point)
    parts.append(f'<line x1="{point_x:.1f}" y1="{top - 6}" x2="{point_x:.1f}" y2="{top + 3 * 48 - 10}" '
                 f'stroke="#b91c1c" stroke-width="2" stroke-dasharray="4 3"></line>')
    parts.append(f'<text x="{point_x:.1f}" y="{top - 10}" text-anchor="middle" '
                 f'class="chart-point">{fmt(point, 2)}</text>')
    ticks = 5
    for index in range(ticks + 1):
        value = low + span * index / ticks
        parts.append(f'<line x1="{x(value):.1f}" y1="{top + 3 * 48 - 6}" x2="{x(value):.1f}" '
                     f'y2="{top + 3 * 48}" stroke="#cbd5e1"></line>')
        parts.append(f'<text x="{x(value):.1f}" y="{top + 3 * 48 + 16}" text-anchor="middle" '
                     f'class="chart-value">{fmt(value, 1)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_bars(items: list[tuple[str, float]], lang: str = "ru", unit: str = "USD_mn") -> str:
    """Inline SVG: horizontal bars for scenario output effects (negative = left, positive = right)."""
    width, label_width, right, row_height, top = 760, 230, 90, 30, 14
    height = top + row_height * len(items) + 18
    largest = max([abs(float(value)) for _, value in items] or [1.0]) or 1.0
    zero = label_width
    scale = (width - label_width - right) / (2.0 * largest)
    parts = [f'<svg class="chart" role="img" viewBox="0 0 {width} {height}" '
             f'aria-label="{"Эффекты сценариев" if lang == "ru" else "Scenario effects"}">']
    parts.append(f'<line x1="{zero}" y1="{top}" x2="{zero}" y2="{height - 8}" stroke="#94a3b8"></line>')
    for index, (label, value) in enumerate(items):
        value = float(value)
        y = top + index * row_height
        parts.append(f'<text x="{label_width - 10}" y="{y + 15}" text-anchor="end" '
                     f'class="chart-label">{esc(label)}</text>')
        bar = abs(value) * scale
        x = zero - bar if value < 0 else zero
        color = "#b91c1c" if value < 0 else "#047857"
        parts.append(f'<rect x="{x:.1f}" y="{y + 2}" width="{max(1.0, bar):.1f}" height="18" '
                     f'rx="3" fill="{color}" opacity="0.85"></rect>')
        text_x = x - 6 if value < 0 else x + bar + 6
        anchor = "end" if value < 0 else "start"
        parts.append(f'<text x="{text_x:.1f}" y="{y + 16}" text-anchor="{anchor}" '
                     f'class="chart-value">{fmt_int(value)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_progress(value: int, total: int, lang: str = "ru") -> str:
    """Inline SVG progress bar (C6 journal)."""
    width, height = 720, 46
    fraction = 0.0 if total <= 0 else max(0.0, min(1.0, float(value) / float(total)))
    parts = [f'<svg class="chart" role="img" viewBox="0 0 {width} {height}" '
             f'aria-label="{"Прогресс C6" if lang == "ru" else "C6 progress"}">',
             f'<rect x="0" y="10" width="{width}" height="22" rx="6" fill="#e2e8f0"></rect>',
             f'<rect x="0" y="10" width="{max(2.0, width * fraction):.1f}" height="22" rx="6" '
             f'fill="#0f766e"></rect>',
             f'<text x="{width / 2:.0f}" y="26" text-anchor="middle" class="chart-label">'
             f'{value} / {total}</text>', "</svg>"]
    return "".join(parts)


def _svg_sparkline(values: list[float], width: int = 320, height: int = 70) -> str:
    """Inline SVG sparkline of the 19 quantiles (deterministic, no labels)."""
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1.0
    step = width / max(1, len(values) - 1)
    points = " ".join(f"{index * step:.1f},{height - 6 - (value - low) / span * (height - 12):.1f}"
                      for index, value in enumerate(values))
    return (f'<svg class="chart" role="img" viewBox="0 0 {width} {height}" '
            f'aria-label="Распределение квантилей">'
            f'<polyline fill="none" stroke="#0f766e" stroke-width="2" points="{points}"></polyline></svg>')


def _citation_block(lang: str, generated_at: str, protocol: str) -> str:
    text = CITATION[lang] % (generated_at[:10], protocol, SITE_URL)
    return f'<pre class="citation">{esc(text)}</pre>'


def _doc_links() -> list[dict]:
    links = []
    for relative in DOC_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise SiteBuildError(f"docs: нет файла {relative}")
        content = path.read_text(encoding="utf-8")
        heading = next((line[2:].strip() for line in content.splitlines()
                        if line.startswith("# ")), None)
        title = heading.split(". ", 1)[-1] if heading else DOC_TITLES[relative]
        links.append({"file": relative, "title": title, "href": relative,
                      "href_en": f"../{relative}"})
    return links


# ---------------------------------------------------------------------------------------- inputs
def load_context(root: Path = ROOT) -> dict:
    root = Path(root)
    feed = read_json(root / FEED, "продуктовый фид")
    for key in ("status", "status_label", "conditional_data_class", "candidate", "target", "horizon",
                "origin", "point", "quantiles_5_to_95_step_5", "controls", "protocol",
                "c4_status", "c6_status"):
        if key not in feed:
            raise SiteBuildError(f"продуктовый фид: нет поля {key!r}")
    if feed["status"] not in ("CANDIDATE", "SHADOW_TEST", "LIMITED_DEPLOYMENT", "ACTIVE"):
        raise SiteBuildError(f"продуктовый фид: неизвестный статус {feed['status']!r}")
    quantiles = feed["quantiles_5_to_95_step_5"]
    if not isinstance(quantiles, list) or len(quantiles) != 19:
        raise SiteBuildError("продуктовый фид: ожидались 19 квантилей")
    if any(b <= a for a, b in zip(quantiles, quantiles[1:])):
        raise SiteBuildError("продуктовый фид: квантили не возрастают строго")
    point = _number(feed["point"], "фид.point")
    for arm in ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"):
        if arm not in (feed.get("controls") or {}):
            raise SiteBuildError(f"продуктовый фид: нет контрольного плеча {arm}")

    report_v5 = read_json(root / REPORT_V5, "отчёт v5")
    report_v4 = read_json(root / REPORT_V4, "отчёт v4 SHADOW_TEST")
    shadow = read_json(root / SHADOW, "shadow_status")
    world = read_json(root / WORLD_INDEX, "индекс мировых отчётов")
    dataset = world.get("dataset") or {}
    if not isinstance(dataset.get("countries"), int) or not isinstance(dataset.get("industries"), int):
        raise SiteBuildError("индекс мировых отчётов: нет countries/industries")
    scenarios = world.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < 3:
        raise SiteBuildError("индекс мировых отчётов: нет списка сценариев")
    reports = {}
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "")
        if scenario_id not in SCENARIO_IDS:
            continue
        relative = str(scenario.get("file_json") or "")
        if not relative:
            raise SiteBuildError(f"сценарий {scenario_id}: нет file_json")
        reports[scenario_id] = read_json(root / WORLD_REPORTS / Path(relative).name,
                                         f"отчёт сценария {scenario_id}")

    figaro = read_json(root / FIGARO_MANIFEST, "манifest FIGARO")
    if not (figaro.get("coverage") or {}).get("named_share_pct"):
        raise SiteBuildError("манifest FIGARO: нет coverage.named_share_pct")
    wiod = []
    for relative in WIOD_MANIFESTS:
        path = root / relative
        if path.is_file():
            document = read_json(path, f"манifest {Path(relative).parent.name}")
            wiod.append({"file": relative, "document": document})
    if not wiod:
        raise SiteBuildError("нет ни одного манifestа WIOD")
    blocked = read_json(root / BLOCKED, "список источников MRIO")

    return {"root": root, "feed": feed, "point": point, "quantiles": [float(q) for q in quantiles],
            "report_v5": report_v5, "report_v4": report_v4, "shadow": shadow,
            "world": world, "dataset": dataset, "scenarios": scenarios, "reports": reports,
            "figaro": figaro, "wiod": wiod, "blocked": blocked, "doc_links": _doc_links()}


# ----------------------------------------------------------------------------------------- layout
NAV_LABELS = {
    "ru": (("index", "Главная"), ("model", "Модель"), ("forecast", "Прогноз"),
           ("scenarios", "Сценарии"), ("data", "Данные"),
           ("methodology", "Методология"), ("docs", "Документы"), ("status", "Статус")),
    "en": (("index", "Home"), ("model", "Model"), ("forecast", "Forecast"),
           ("scenarios", "Scenarios"), ("data", "Data"),
           ("methodology", "Methodology"), ("docs", "Documents"), ("status", "Status")),
}
BRAND_LABELS = {
    "ru": "AMFE · исследовательская модель мировой экономики",
    "en": "AMFE · research model of the world economy",
}
FOOTER = {
    "ru": ("Дисклеймер.", DISCLAIMER,
           "Страницы содержат только собственные прогнозы и производные метрики проекта; сырых "
           "сторонних данных здесь нет.",
           "Снимок:", "статический сайт без JavaScript, фреймворков и внешних ресурсов · собран"),
    "en": ("Disclaimer.", EN_DISCLAIMER,
           "These pages contain only the project's own forecasts and derived metrics; there are no "
           "raw third-party data here.",
           "Snapshot:", "static site with no JavaScript, frameworks or external resources · built by"),
}
NAV_ARIA = {"ru": "Основная навигация", "en": "Main navigation"}


def page(title: str, current: str, body: str, generated_at: str, lang: str = "ru",
         description: str | None = None) -> str:
    if lang not in NAV_LABELS:
        raise SiteBuildError(f"неизвестный язык страницы: {lang!r}")
    meta_description = description or SITE_DESCRIPTIONS[lang]
    stamp = generated_at[:10]
    nav = []
    for page_id, label in NAV_LABELS[lang]:
        active = ' class="active"' if page_id == current else ""
        href = "index.html" if page_id == "index" else f"{page_id}.html"
        nav.append(f'<a href="{href}"{active}>{label}</a>')
    if lang == "ru":
        css = "assets/style.css"
        switch = (f'<div class="lang-switch" aria-label="Язык">'
                  f'<span class="current" lang="ru">RU</span>'
                  f'<a href="en/{current}.html" hreflang="en" lang="en">EN</a></div>')
    else:
        css = "../assets/style.css"
        switch = (f'<div class="lang-switch" aria-label="Language">'
                  f'<a href="../{current}.html" hreflang="ru" lang="ru">RU</a>'
                  f'<span class="current" lang="en">EN</span></div>')
    heading, disclaimer, note, snapshot, built = FOOTER[lang]
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title + " — AMFE" if not title.startswith("AMFE") else title)}</title>
<meta name="description" content="{esc(meta_description)}">
<meta name="theme-color" content="#0f172a">
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(title + " — AMFE" if not title.startswith("AMFE") else title)}">
<meta property="og:description" content="{esc(meta_description)}">
<meta property="og:locale" content="{'ru_RU' if lang == 'ru' else 'en_US'}">
<meta name="twitter:card" content="summary">
<link rel="stylesheet" href="{css}">
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="index.html">{esc(BRAND_LABELS[lang])}</a>
    <span class="build-stamp" title="{'Дата сборки снимка' if lang == 'ru' else 'Snapshot build date'}">{'обновлено' if lang == 'ru' else 'updated'} {esc(stamp)}</span>
    <nav aria-label="{esc(NAV_ARIA[lang])}">{''.join(nav)}</nav>
    {switch}
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="site-footer">
  <div class="wrap">
    <p><strong>{esc(heading)}</strong> {esc(disclaimer)} {esc(note)}</p>
    <p class="muted">{esc(snapshot)} {esc(generated_at)} · {esc(built)}
    <code>tools/site_build.py</code>.</p>
  </div>
</footer>
</body>
</html>
"""


def _table(headers: list[str], rows: list[list[str]], caption: str | None = None) -> str:
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "\n".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    caption_html = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (f'<div class="table-wrap"><table>{caption_html}\n<thead><tr>{head}</tr></thead>\n'
            f"<tbody>\n{body}\n</tbody></table></div>")


def _badge(status: str) -> str:
    css = "badge"
    if status in ("CANDIDATE", "SHADOW_TEST"):
        css += " badge-warn"
    return f'<span class="{css}">{esc(status)}</span>'


def _disclaimer_box() -> str:
    return ('<aside class="callout callout-warn"><p><strong>Не допуск и не инвестиционная '
            'рекомендация.</strong> AMFE — исследовательский проект. Статус CANDIDATE означает, что '
            'модель не допущена к использованию; цифры ниже — результаты проверок и сценариев, а не '
            'совет и не прогноз для сделок.</p></aside>')


# --------------------------------------------------------------------------------------- figures
def _v5_figures(context: dict) -> dict:
    criteria = ((context["report_v5"].get("admission") or {}).get("criteria") or {})
    c1 = criteria.get("C1") or {}
    c2 = criteria.get("C2") or {}
    c4 = criteria.get("C4") or {}
    c6 = criteria.get("C6") or {}
    w1 = (c4.get("per_window") or {}).get("W1") or {}
    return {
        "c1_pass": bool(c1.get("pass")),
        "c2_pass": bool(c2.get("pass")),
        "c5_pass": bool((criteria.get("C5") or {}).get("pass")),
        "c1_w1_rmse": _criterion_value(c1, "W1", "RMSE", "relative_loss"),
        "c1_w1_crps": _criterion_value(c1, "W1", "CRPS", "relative_loss"),
        "c2_w1_rmse_p": _criterion_value(c2, "W1", "RMSE", "adjusted_p_value"),
        "c2_w2_crps_p": _criterion_value(c2, "W2", "CRPS", "adjusted_p_value"),
        "c4_pass": bool(c4.get("pass")),
        "c4_rmse": ((w1.get("RMSE") or {}).get("relative_loss_recession")),
        "c4_crps": ((w1.get("CRPS") or {}).get("relative_loss_recession")),
        "c4_rmse_pass": bool((w1.get("RMSE") or {}).get("pass")),
        "c4_crps_pass": bool((w1.get("CRPS") or {}).get("pass")),
        "c4_gw_p": ((w1.get("RMSE") or {}).get("conditional_gw_p")),
        "c6_status": c6.get("status"),
    }


def _v4_figures(context: dict) -> dict:
    report = context["report_v4"]
    admission = report.get("admission") or {}
    criteria = admission.get("criteria") or {}
    c4 = criteria.get("C4") or {}
    w1 = (c4.get("per_window") or {}).get("W1") or {}
    c1 = criteria.get("C1") or {}
    return {
        "candidate": report.get("candidate"),
        "status": admission.get("status"),
        "status_label": admission.get("status_label"),
        "conditional": bool(admission.get("conditional_data_class")),
        "c1_pass": bool(c1.get("pass")),
        "c1_w1_rmse": _criterion_value(c1, "W1", "RMSE", "relative_loss"),
        "c2_pass": bool((criteria.get("C2") or {}).get("pass")),
        "c4_pass": bool(c4.get("pass")),
        "c4_w1_rmse": ((w1.get("RMSE") or {}).get("relative_loss_recession")),
        "c4_w1_crps": ((w1.get("CRPS") or {}).get("relative_loss_recession")),
        "c5_pass": bool((criteria.get("C5") or {}).get("pass")),
        "c6_status": (criteria.get("C6") or {}).get("status"),
    }


def _world_figures(context: dict) -> dict:
    dataset = context["dataset"]
    coverage = (context["figaro"].get("coverage") or {})
    wiod = []
    for item in context["wiod"]:
        document = item["document"]
        dataset_info = document.get("dataset") or {}
        cov = document.get("coverage") or {}
        wiod.append({
            "id": document.get("dataset_id") or Path(item["file"]).parent.name,
            "countries": dataset_info.get("countries"),
            "industries": dataset_info.get("industries"),
            "year": (dataset_info.get("years") or [None])[0],
            "share": cov.get("named_share_percent"),
            "licence": (document.get("source") or {}).get("licence"),
        })
    shares = [item["share"] for item in wiod if item["share"] is not None]
    return {
        "countries": dataset.get("countries"),
        "industries": dataset.get("industries"),
        "sectors": dataset.get("sectors"),
        "cells": dataset.get("cells_Z"),
        "stored_share": dataset.get("stored_share"),
        "units": dataset.get("units"),
        "year": (dataset.get("years") or [None])[0],
        "source": dataset.get("source"),
        "total_output": dataset.get("total_output"),
        "bytes": dataset.get("bytes"),
        "sha256": dataset.get("sha256"),
        "figaro_share": coverage.get("named_share_pct"),
        "figaro_named": coverage.get("named_countries"),
        "figaro_world_gdp": coverage.get("world_gdp_usd"),
        "wiod": wiod,
        "wiod_min": min(shares) if shares else None,
        "wiod_max": max(shares) if shares else None,
    }


# ----------------------------------------------------------------------------------------- pages
def render_index(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    shadow = context["shadow"]
    world = _world_figures(context)
    v4 = _v4_figures(context)
    energy = context["scenarios"][0] if context["scenarios"] else {}
    headline = energy.get("headline") or {}
    body = f"""
<section class="hero">
  <h1>AMFE: исследовательская модель мировой экономики</h1>
  <p class="lead">Межстрановая модель «страна × отрасль» на реальных таблицах FIGARO и WIOD,
  прогнозный ансамбль для инфляции CPI США и сценарные отчёты о распространении шоков по цепочкам
  поставок. Всё на этой странице — исследование, а не допущенная к применению модель.</p>
  <p class="badges">{_badge("CANDIDATE")} прогноз T1 CPI · {_badge(v4["status"] or "SHADOW_TEST")}
  теневой трек {esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")} · статусы не присваиваются сайтом</p>
</section>
{_disclaimer_box()}
<section>
  <h2>Последний прогноз CPI (T1, год к году, h=1)</h2>
  <div class="grid">
    <div class="card"><div class="card-label">Точка</div>
      <div class="card-value">{fmt(context["point"])}</div>
      <div class="muted">origin {esc(feed["origin"])}, кандидат {esc(feed["candidate"])}</div></div>
    <div class="card"><div class="card-label">90% интервал</div>
      <div class="card-value">[{fmt(context["quantiles"][0])}; {fmt(context["quantiles"][18])}]</div>
      <div class="muted">квантили q05…q95, 19 значений</div></div>
    <div class="card"><div class="card-label">Статус</div>
      <div class="card-value">{_badge(feed["status"])}</div>
      <div class="muted">conditional_data_class = {str(feed["conditional_data_class"]).lower()}</div></div>
  </div>
</section>
<section>
  <h2>Мировая модель на реальных данных</h2>
  <div class="grid">
    <div class="card"><div class="card-label">FIGARO 2019</div>
      <div class="card-value">{esc(world["countries"])} × {esc(world["industries"])}</div>
      <div class="muted">стран × отраслей; {esc(world["sectors"])} секторов; покрытие ВВП
      {fmt_pct(world["figaro_share"])}</div></div>
    <div class="card"><div class="card-label">WIOD 2016</div>
      <div class="card-value">{esc(world["wiod"][0]["countries"] if world["wiod"] else "—")} ×
      {esc(world["wiod"][0]["industries"] if world["wiod"] else "—")}</div>
      <div class="muted">стран × отраслей; покрытие ВВП
      {fmt_pct(world["wiod_min"])}–{fmt_pct(world["wiod_max"])}</div></div>
    <div class="card"><div class="card-label">Сценарии</div>
      <div class="card-value">{len(context["scenarios"])}</div>
      <div class="muted">energy / import / FX на реальном FIGARO; статус
      {esc(context["world"].get("status") or "RESEARCH_ONLY")}</div></div>
  </div>
</section>
<section>
  <h2>Ключевой сценарий</h2>
  <p>Энергетический шок: {esc(energy.get("description") or "снижение конечного спроса на 5%")}.</p>
  <p>Суммарное изменение выпуска:
  <strong>{fmt_int(headline.get("total_delta_output"))} USD_mn</strong>
  ({fmt((headline.get("relative_delta_output") or 0) * 100, 3)}% от базового выпуска);
  крупнейшая страна — {esc(headline.get("largest_country") or "—")},
  крупнейшая отрасль — {esc(headline.get("largest_industry") or "—")}.</p>
  <p class="muted">Сравнительная статика, не прогноз. Полные таблицы — на странице
  <a href="scenarios.html">«Сценарии»</a>.</p>
</section>
<section>
  <h2>Разделы</h2>
  <ul class="links">
    <li><a href="model.html">Модель</a> — архитектура: данные MRIO → IO/SFC → GVAR → сценарии.</li>
    <li><a href="forecast.html">Прогноз</a> — продукт T1 CPI: интервалы, C4/C6, теневой трек.</li>
    <li><a href="scenarios.html">Сценарии</a> — 8 отчётов energy/import/FX с топ-странами и отраслями.</li>
    <li><a href="data.html">Данные</a> — FIGARO, WIOD, OECD, Eora: покрытие и лицензии.</li>
    <li><a href="methodology.html">Методология</a> — протоколы, статусы допуска C1–C6 и ограничения.</li>
    <li><a href="docs.html">Документы</a> — ключевые документы проекта.</li>
  </ul>
</section>
"""
    return page("AMFE: исследовательская модель мировой экономики", "index", body, generated_at)


def render_model(context: dict, generated_at: str) -> str:
    world = _world_figures(context)
    rows = [
        ["Данные MRIO", "FIGARO 2019, WIOD 2016 — реальные таблицы «страна × отрасль»",
         "реализовано", "50×64 и 44×56; сырые данные вне git"],
        ["IO-ядро", "статическая модель Леонтьева x = (I−A)⁻¹(y+fda), валидация балансов",
         "реализовано", "спектральный радиус и остатки сходятся"],
        ["SFC-слой", "запасы/потоки, межстрановое погашение", "частично",
         "прототипы счётов без эмпирической калибровки"],
        ["GVAR", "межстрановые связи и переносы шоков", "прототип",
         "на реальных данных, без полной калибровки"],
        ["Сценарии", "сравнительная статика energy/import/FX", "реализовано",
         f"{len(context['scenarios'])} отчётов, статус RESEARCH_ONLY"],
        ["Динамика и ожидания", "быстрый/медленный контур, drift", "не реализовано",
         "отдельный этап после данных и калибровки"],
        ["Продукт прогноза", "ансамбль T1 CPI, протокол v5", "CANDIDATE / SHADOW_TEST",
         "допуск ACTIVE не присвоен"],
    ]
    body = f"""
<section><h1>Модель: архитектура AMFE</h1>
<p class="lead">Пять слоёв: реальные таблицы «затраты–выпуск» → IO/SFC-ядро → GVAR →
сценарные отчёты → прогнозный продукт. Ниже — что действительно реализовано на реальных данных,
а что остаётся прототипом.</p></section>
{_disclaimer_box()}
<section>
  <h2>Схема</h2>
  <ol class="flow">
    <li><strong>Данные MRIO.</strong> FIGARO 2019 ({esc(world["countries"])} стран ×
    {esc(world["industries"])} отраслей, {fmt_pct(world["figaro_share"])} ВВП) и WIOD 2016
    ({esc(world["wiod"][0]["countries"] if world["wiod"] else "—")} ×
    {esc(world["wiod"][0]["industries"] if world["wiod"] else "—")}).</li>
    <li><strong>IO/SFC-ядро.</strong> Блочная матрица A, решение x = (I−A)⁻¹(y+fda); проверки
    балансов строк/столбцов и глобального равенства добавленной стоимости и конечного спроса.</li>
    <li><strong>GVAR.</strong> Прототип межстрановых связей для переноса шоков; полная
    эмпирическая калибровка не завершена.</li>
    <li><strong>Сценарии.</strong> Сравнительная статика: энергетический шок, импортные издержки,
    FX pass-through; результаты по странам и отраслям.</li>
    <li><strong>Прогнозный продукт.</strong> Ансамбль T1 CPI (инфляция США г/г, h=1) с интервалами;
    протокол допуска v5.</li>
  </ol>
</section>
<section>
  <h2>Что реализовано и что нет</h2>
  {_table(["Слой", "Содержание", "Статус", "Комментарий"], rows)}
  <p class="muted">Честная граница: реальные MRIO и сценарии на них есть; полной динамической
  SFC-модели, калиброванного GVAR и поведенческих ожиданий пока нет. Поэтому статусы —
  CANDIDATE/SHADOW_TEST и RESEARCH_ONLY, а не «допущенная модель».</p>
</section>
<section>
  <h2>Инварианты, которые проверяются</h2>
  <ul>
    <li>Балансы строк и столбцов MRIO и глобальное Σva = Σy (для WIOD — со знаковыми
    корректировками схемы v1.1).</li>
    <li>Продуктивность матрицы A: спектральный радиус &lt; 1 и положительный вектор выпуска.</li>
    <li>Сценарии воспроизводимы: одинаковые входы дают одинаковые отчёты, остатки
    материальных балансов — на уровне 1e−8.</li>
  </ul>
</section>
"""
    return page("Модель", "model", body, generated_at)


def render_forecast(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    v5 = _v5_figures(context)
    v4 = _v4_figures(context)
    shadow = context["shadow"]
    c6 = shadow.get("c6") or {}
    projection = shadow.get("projection") or {}
    journal = shadow.get("journal") or {}
    quantiles = context["quantiles"]
    intervals = [("50%", quantiles[4], quantiles[14], "q25…q75"),
                 ("80%", quantiles[1], quantiles[17], "q10…q90"),
                 ("90%", quantiles[0], quantiles[18], "q05…q95")]
    rows = [[level, f"{fmt(low)} … {fmt(high)}", labels] for level, low, high, labels in intervals]
    controls = "".join(
        f"<li>{esc(arm)}: <strong>{fmt(feed['controls'][arm]['point'])}</strong></li>"
        for arm in ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"))
    body = f"""
<section><h1>Прогноз T1 CPI: инфляция США, год к году, h=1</h1>
<p class="lead">Кандидат {esc(feed["candidate"])}, origin {esc(feed["origin"])},
статус {_badge(feed["status"])}, conditional_data_class =
{str(feed["conditional_data_class"]).lower()}. Это прогноз исследовательского ансамбля, а не
допущенная модель.</p></section>
{_disclaimer_box()}
<section>
  <h2>Точечный прогноз и интервалы</h2>
  <div class="grid">
    <div class="card"><div class="card-label">Точка</div>
      <div class="card-value">{fmt(context["point"])}</div></div>
    <div class="card"><div class="card-label">Медиана (q50)</div>
      <div class="card-value">{fmt(quantiles[9])}</div></div>
    <div class="card"><div class="card-label">Протокол</div>
      <div class="card-value">{esc(feed["protocol"]["version"])}</div>
      <div class="muted">FROZEN, SHA-256 {esc(str(feed["protocol"]["sha256"])[:12])}…</div></div>
  </div>
  {_table(["Интервал", "Границы", "Квантили"], rows, "Интервалы из 19 квантилей 5%…95%")}
  {_svg_interval_ladder(quantiles, context["point"])}
  <p class="muted">Диаграмма построена из тех же 19 квантилей; красная линия — точечный прогноз.</p>
  <p><strong>Контрольные плечи (точка):</strong></p>
  <ul>{controls}</ul>
</section>
<section>
  <h2>Что подтверждено</h2>
  <p>Первая реальная оценка протокола v5 по паре T1 h=1:</p>
  <ul>
    <li><strong>C1 PASS</strong> — относительная потеря RMSE/CRPS {fmt(v5["c1_w1_rmse"])} /
    {fmt(v5["c1_w1_crps"])} на окне W1 при пороге 0.97.</li>
    <li><strong>C2 PASS</strong> — скорректированные p-значения Romano–Wolf
    {fmt(v5["c2_w1_rmse_p"])} (W1 RMSE) и {fmt(v5["c2_w2_crps_p"])} (W2 CRPS), FWER 0.10.</li>
    <li><strong>C5 PASS</strong> — кандидат не хуже компаратора на вторичных фактах.</li>
  </ul>
</section>
<section>
  <h2>Что НЕ подтверждено</h2>
  <ul>
    <li><strong>C4 — FAIL по RMSE.</strong> Относительная потеря {fmt(v5["c4_rmse"])} в
    рецессионных месяцах W1_EXT (порог ≤ 1.00), условный Giacomini–White отвергает в пользу
    компаратора (p ≈ {fmt_sci(v5["c4_gw_p"])}). По CRPS C4 PASS ({fmt(v5["c4_crps"])}), но C4
    требует прохождения по каждому loss.</li>
    <li><strong>C6 — нет.</strong> В forward-журнале {esc(c6.get("n_resolved", 0))} из
    {esc(c6.get("min_forecasts", 12))} разрешённых прогнозов; различных целевых периодов
    {esc(c6.get("distinct_target_periods", 0))} из
    {esc(c6.get("min_distinct_target_periods", 4))}. Гейт C6 физически не может быть пройден до
    накопления журнала.</li>
    <li>SHADOW_TEST, LIMITED_DEPLOYMENT и ACTIVE для этого кандидата не достигнуты.</li>
  </ul>
</section>
<section>
  <h2>Теневой трек: {esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")}</h2>
  <p>Статус кандидата: {_badge(v4["status"] or "SHADOW_TEST")}
  ({esc(v4["status_label"] or "SHADOW_TEST")}), conditional_data_class =
  {str(v4["conditional"]).lower()}; C1/C2/C4/C5 — PASS, C6 — ожидает журнал.
  Трек ведёт журнал и считает расстояние до гейта C6.</p>
  {_table(["Показатель", "Значение"], [
      ["Статус трека", esc(shadow.get("status") or "—")],
      ["Строк журнала всего", esc(journal.get("rows_total", "—"))],
      ["Разрешённых прогнозов", f'{esc(c6.get("n_resolved", 0))} / {esc(c6.get("min_forecasts", 12))}'],
      ["Различных целевых периодов",
       f'{esc(c6.get("distinct_target_periods", 0))} / {esc(c6.get("min_distinct_target_periods", 4))}'],
      ["Ориентир гейта C6", esc(projection.get("estimated_gate_date") or "—")],
      ["C4 W1 RMSE / CRPS", f'{fmt(v4["c4_w1_rmse"])} / {fmt(v4["c4_w1_crps"])}'],
  ])}
  <p class="muted">Новых допусков не присваивается: трек — это мониторинг, а не решение о допуске.</p>
</section>
"""
    return page("Прогноз T1 CPI", "forecast", body, generated_at)


def render_scenarios(context: dict, generated_at: str) -> str:
    rows = []
    for scenario in context["scenarios"]:
        headline = scenario.get("headline") or {}
        rows.append([
            esc(scenario.get("id")),
            fmt((headline.get("relative_delta_output") or 0) * 100, 3) + "%",
            fmt_int(headline.get("total_delta_output")),
            esc(headline.get("largest_country") or "—"),
            esc(headline.get("largest_industry") or "—"),
            esc(scenario.get("description") or ""),
        ])
    tops = ""
    for scenario_id in TOP_SCENARIOS:
        report = context["reports"].get(scenario_id)
        if not report:
            continue
        impacts = report.get("impacts") or {}
        countries = ((impacts.get("countries") or {}).get("top") or [])[:5]
        industries = ((impacts.get("industries") or {}).get("top") or [])[:5]
        country_rows = [[esc(item.get("name")), fmt_int(item.get("delta_output")),
                         fmt((item.get("share_of_abs") or 0) * 100, 1) + "%"] for item in countries]
        industry_rows = [[esc(item.get("name")), fmt_int(item.get("delta_output")),
                          fmt((item.get("share_of_abs") or 0) * 100, 1) + "%"] for item in industries]
        tops += f"""
<h3>{esc(scenario_id)}: {esc((report.get("scenario") or {}).get("description") or "")}</h3>
<div class="columns">
  <div>{_table(["Страна", "Δ выпуска, USD_mn", "Доля |Δ|"], country_rows, "Топ-5 стран")}</div>
  <div>{_table(["Отрасль", "Δ выпуска, USD_mn", "Доля |Δ|"], industry_rows, "Топ-5 отраслей")}</div>
</div>"""
    body = f"""
<section><h1>Сценарии мира: сравнительная статика на реальном FIGARO 2019</h1>
<p class="lead">Восемь сценариев: энергетический шок −5% и +50%, рост импортных издержек и FX pass-through для
DE и FR. Это не прогноз: сценарии показывают, как шок расходится по цепочкам поставок при
фиксированной технологии.</p></section>
{_disclaimer_box()}
<section>
  <h2>Все сценарии</h2>
  {_table(["Сценарий", "Δ выпуска, %", "Δ выпуска, USD_mn", "Крупнейшая страна",
           "Крупнейшая отрасль", "Описание"], rows,
          f"Статус: {context['world'].get('status') or 'RESEARCH_ONLY'}")}
  {_svg_bars([(str(item.get("id")), ((item.get("headline") or {}).get("total_delta_output") or 0.0))
              for item in context["scenarios"]])}
</section>
<section>
  <h2>Топ-5 стран и отраслей</h2>
  <p class="muted">Те же 8 сценариев рассчитаны на OECD ICIO 2011 (71 экономика × 34 отрасли, 95,4% мировой ДС): energy +50% +2,74 трлн, import +10% −4,61 трлн, FX USA −20% +0,67 трлн USD; SFC PASS — см. <a href="docs/131_Сценарные_отчёты_мира.md">docs/131</a>.</p>
  <p>Суммы по модулю изменения выпуска; ROW — агрегат «остального мира» FIGARO.</p>
  {tops}
</section>
<section>
  <h2>Метод и допущения</h2>
  <ul>
    <li>Модель: статическая линейная модель Леонтьева x = (I−A)⁻¹(y+fda),
    A = Z·diag(x)⁻¹; сценарий меняет y или импортные коэффициенты.</li>
    <li>Без цен, замещения, динамики и обратных связей; коэффициенты A фиксированы.</li>
    <li>Энергия: конечный спрос −5% в отраслях CPA_B, CPA_C19, CPA_D35.</li>
    <li>Импорт: рост импортных издержек на 10%/20% с пропорциональным сокращением спроса.</li>
    <li>FX: pass-through 10%/20% в стоимость импортных затрат для выбранной страны.</li>
    <li>Отчёты агрегатные: без сырых матриц Z и без значений фактических наблюдений.</li>
  </ul>
</section>
"""
    return page("Сценарии", "scenarios", body, generated_at)


def render_data(context: dict, generated_at: str) -> str:
    world = _world_figures(context)
    figaro_source = ((context["figaro"].get("source") or {}))
    wiod_rows = []
    for item in world["wiod"]:
        wiod_rows.append([esc(item["id"]), esc(item["year"]),
                          f'{esc(item["countries"])} × {esc(item["industries"])}',
                          fmt_pct(item["share"], 4), esc(item["licence"] or "—")])
    blocked = {}
    for source in (context["blocked"].get("sources") or []):
        if isinstance(source, dict) and source.get("source_id"):
            blocked[source["source_id"]] = source
    oecd = blocked.get("oecd_icio") or {}
    eora = blocked.get("eora_mrio") or {}
    comtrade = blocked.get("un_comtrade") or {}
    body = f"""
<section><h1>Данные: реальные MRIO и лицензии</h1>
<p class="lead">Мир строится на реальных таблицах «затраты–выпуск». Сырые данные лежат вне git и
на сайте не публикуются: здесь только метаданные, покрытие и лицензии.</p></section>
{_disclaimer_box()}
<section>
  <h2>FIGARO 2019 — основной мир</h2>
  {_table(["Параметр", "Значение"], [
      ["Источник", esc(figaro_source.get("name") or world["source"] or "Eurostat FIGARO")],
      ["Размер", f'{fmt_int(world["countries"])} × {fmt_int(world["industries"])} '
                 f'(стран × отраслей; {esc(world["sectors"])} секторов)'],
      ["Год", esc(world["year"])],
      ["Единицы", esc(world["units"])],
      ["Покрытие ВВП по поимённым странам", fmt_pct(world["figaro_share"])],
      ["Покрытие с агрегатом ROW", "100%"],
      ["Ячеек Z", fmt_int(world["cells"]) + " (хранение разреженное)"],
      ["Лицензия", esc(figaro_source.get("licence") or "Eurostat re-use policy (open data)")],
      ["Канонический JSON", fmt_int(world["bytes"]) + " байт, SHA-256 "
                             + esc(str(world["sha256"])[:12]) + "…"],
  ])}
  <p class="muted">Третья сторона: данные не коммитятся, публикуются только производные метрики.</p>
</section>
<section>
  <h2>WIOD 2016 — второй мир</h2>
  {_table(["Набор", "Год", "Страны × отрасли", "Покрытие ВВП", "Лицензия"], wiod_rows)}
  <p>Диапазон покрытия по наборам: {fmt_pct(world["wiod_min"], 1)}–{fmt_pct(world["wiod_max"], 1)}.
  WIOD содержит отрицательные ячейки (запасы, добавленная стоимость), поэтому используется схема
  v1.1 со знаковыми корректировками — базовые y и va остаются неотрицательными.</p>
</section>
<section>
  <h2>Источники: статусы</h2>
  {_table(["Источник", "Статус", "Почему", "Что делать"], [
      oecd_source_row("ru"),
      ["Eora MRIO", esc(eora.get("status") or "BLOCKED_LICENCE"),
       "Свободная только академическая лицензия с регистрацией; коммерческая — покупка",
       "Не использовать без лицензии; парсер Eora в этой волне не реализован"],
      ["UN Comtrade", esc(comtrade.get("status") or "AVAILABLE_API"),
       "Это не MRIO: торговые потоки без отраслевой матрицы",
       "Нужен мост HS → отрасль; отдельное решение"],
  ])}
</section>
<section>
  <h2>Что публиковать нельзя</h2>
  <ul>
    <li>Сырые сторонние наборы (FIGARO/WIOD/OECD/Eora/Comtrade) и их значения — только по
    лицензии источника; в репозиторий и на сайт не попадают.</li>
    <li>Можно публиковать производные агрегаты: покрытие, итоги, доли, топ-списки, остатки
    валидации — именно они и приведены выше.</li>
    <li>Нельзя выдавать исследовательский мир за официальный прогноз или допущенную модель.</li>
  </ul>
</section>
"""
    return page("Данные", "data", body, generated_at)


def render_methodology(context: dict, generated_at: str) -> str:
    v5 = _v5_figures(context)
    v4 = _v4_figures(context)
    feed = context["feed"]
    criteria_rows = [
        ["C1", "Относительная потеря (RMSE/CRPS) против компаратора",
         f'{_badge("PASS") if v5["c1_pass"] else _badge("FAIL")} '
         f'{fmt(v5["c1_w1_rmse"])} / {fmt(v5["c1_w1_crps"])} (W1, порог 0.97)'],
        ["C2", "Romano–Wolf: значимость превосходства", _badge("PASS") if v5["c2_pass"]
         else _badge("FAIL")],
        ["C3", "Model Confidence Set", "не реализован (негейтящий)"],
        ["C4", "Устойчивость в рецессиях (RMSE и CRPS)", _badge("FAIL")
         + f' RMSE {fmt(v5["c4_rmse"])}, CRPS {_badge("PASS")} {fmt(v5["c4_crps"])}'],
        ["C5", "Вторичные факты", _badge("PASS") if v5["c5_pass"] else _badge("FAIL")],
        ["C6", "Forward-журнал: 12 разрешённых прогнозов, ≥4 периода",
         "не пройден: 0 из 12"],
    ]
    tiers = [
        ["CANDIDATE", "кандидат прошёл часть критериев, допуска нет", "T1 CPI — здесь",
         f'{esc(feed["candidate"])}'],
        ["SHADOW_TEST", "теневой трек на живом журнале, без права использования",
         "текущий", esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")],
        ["LIMITED_DEPLOYMENT", "ограниченное применение после C1–C6", "не достигнут", "—"],
        ["ACTIVE", "допущенная модель", "не достигнут", "—"],
    ]
    body = f"""
<section><h1>Методология и статусы</h1>
<p class="lead">AMFE разделяет «прогнозную точность» и «допуск». Критерии C1–C6 проверяются
замороженным протоколом на размеченных окнах и forward-журнале; сайт статусы не присваивает.</p>
</section>
{_disclaimer_box()}
<section>
  <h2>Протоколы</h2>
  <ul>
    <li><strong>Протокол v5</strong> — текущий замороженный протокол оценки T1 CPI
    (версия {esc(feed["protocol"]["version"])}); расширенный C4 на реконструированных данных
    1990–1996 и forward-журнал.</li>
    <li><strong>Протокол v4</strong> — предыдущая замороженная версия; результаты v4 сохранены,
    оценки не переписываются.</li>
    <li>Замороженная цепочка: протокол → R3-спецификация → NF-SIM (пороги) → состав компаратора.
    Хеши сверяются, при расхождении инструменты падают (fail-closed).</li>
  </ul>
</section>
<section>
  <h2>Критерии допуска C1–C6</h2>
  {_table(["Критерий", "Что проверяет", "Текущий результат T1 h=1"], criteria_rows)}
</section>
<section>
  <h2>Статусная лестница</h2>
  {_table(["Статус", "Значение", "Состояние", "Кандидат"], tiers)}
  <p class="muted">Статусы присваиваются протоколом и человеком, а не сайтом. Сайт — витрина
  зарегистрированных результатов.</p>
</section>
<section>
  <h2>Ограничения</h2>
  <ul>
    <li>C4 FAIL по RMSE: ансамбль хуже компаратора в рецессиях; CRPS PASS не компенсирует RMSE.</li>
    <li>C6 не пройден: 0 из 12 разрешённых прогнозов, нужно ≥4 различных целевых периода;
    ускорять гейт нельзя.</li>
    <li>conditional_data_class = true: часть C4 опирается на реконструированные данные, а не на
    REAL_TIME.</li>
    <li>Мир: сценарии — сравнительная статика без цен, замещения и динамики; GVAR и SFC
    откалиброваны не полностью.</li>
    <li>Сырые сторонние данные не публикуются; сайт содержит только производные метрики.</li>
  </ul>
</section>
"""
    return page("Методология", "methodology", body, generated_at)


def render_docs(context: dict, generated_at: str) -> str:
    rows = [[f'<a href="{esc(link["href"])}">{esc(link["title"])}</a>', esc(link["file"])]
            for link in context["doc_links"]]
    body = f"""
<section><h1>Документы проекта</h1>
<p class="lead">Ключевые документы: данные, ядро, сценарии, протоколы, поддержка и публикация.
Ссылки ведут на файлы репозитория.</p></section>
{_disclaimer_box()}
<section>
  {_table(["Документ", "Файл"], rows)}
</section>
<section>
  <h2>Первоисточники чисел сайта</h2>
  <ul>
    <li><code>build/product/forecast_latest.json</code> — прогноз T1 CPI и его статус.</li>
    <li><code>build/world/report_index.json</code>, <code>build/world/reports/*.json</code> —
    сценарии мира.</li>
    <li><code>protocols/first_real_evaluation_report_v5.json</code>,
    <code>protocols/regime_v4_candidate_evaluation_report.json</code> — критерии.</li>
    <li><code>build/shadow/shadow_status.json</code> — теневой трек и прогресс C6.</li>
    <li><code>data/mrio/**/manifest.json</code> — покрытие FIGARO/WIOD (метаданные).</li>
  </ul>
</section>
"""
    return page("Документы", "docs", body, generated_at)


def _disclaimer_box_en() -> str:
    return ('<aside class="callout callout-warn"><p><strong>Research only — not admission, not '
            'investment advice.</strong> AMFE is a research project. CANDIDATE means the model is not '
            'admitted for use; the figures below are evaluation and scenario results, not advice and '
            'not trading forecasts.</p></aside>')


def render_index_en(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    world = _world_figures(context)
    v4 = _v4_figures(context)
    energy = context["scenarios"][0] if context["scenarios"] else {}
    headline = energy.get("headline") or {}
    body = f"""
<section class="hero">
  <h1>AMFE: research model of the world economy</h1>
  <p class="lead">A cross-country country × industry model built on real FIGARO and WIOD tables, a
  forecast ensemble for US CPI inflation and scenario reports on how shocks propagate through supply
  chains. Everything on this page is research, not an admitted model.</p>
  <p class="badges">{_badge("CANDIDATE")} T1 CPI forecast · {_badge(v4["status"] or "SHADOW_TEST")}
  shadow track {esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")} · statuses are not assigned by this site</p>
</section>
{_disclaimer_box_en()}
<section>
  <h2>Latest CPI forecast (T1, year over year, h=1)</h2>
  <div class="grid">
    <div class="card"><div class="card-label">Point</div>
      <div class="card-value">{fmt(context["point"])}</div>
      <div class="muted">origin {esc(feed["origin"])}, candidate {esc(feed["candidate"])}</div></div>
    <div class="card"><div class="card-label">90% interval</div>
      <div class="card-value">[{fmt(context["quantiles"][0])}; {fmt(context["quantiles"][18])}]</div>
      <div class="muted">quantiles q05…q95, 19 values</div></div>
    <div class="card"><div class="card-label">Status</div>
      <div class="card-value">{_badge(feed["status"])}</div>
      <div class="muted">conditional_data_class = {str(feed["conditional_data_class"]).lower()}</div></div>
  </div>
</section>
<section>
  <h2>World model on real data</h2>
  <div class="grid">
    <div class="card"><div class="card-label">FIGARO 2019</div>
      <div class="card-value">{esc(world["countries"])} × {esc(world["industries"])}</div>
      <div class="muted">countries × industries; {esc(world["sectors"])} sectors; GDP coverage
      {fmt_pct(world["figaro_share"])}</div></div>
    <div class="card"><div class="card-label">WIOD 2016</div>
      <div class="card-value">{esc(world["wiod"][0]["countries"] if world["wiod"] else "—")} ×
      {esc(world["wiod"][0]["industries"] if world["wiod"] else "—")}</div>
      <div class="muted">countries × industries; GDP coverage
      {fmt_pct(world["wiod_min"])}–{fmt_pct(world["wiod_max"])}</div></div>
    <div class="card"><div class="card-label">Scenarios</div>
      <div class="card-value">{len(context["scenarios"])}</div>
      <div class="muted">energy / import / FX on real FIGARO; status
      {esc(context["world"].get("status") or "RESEARCH_ONLY")}</div></div>
  </div>
</section>
<section>
  <h2>Key scenario</h2>
  <p>Energy shock: {esc(energy.get("description") or "a 5% cut in final demand")}.</p>
  <p>Total output change: <strong>{fmt_int(headline.get("total_delta_output"))} USD_mn</strong>
  ({fmt((headline.get("relative_delta_output") or 0) * 100, 3)}% of baseline output); largest country —
  {esc(headline.get("largest_country") or "—")}, largest industry —
  {esc(headline.get("largest_industry") or "—")}.</p>
  <p class="muted">Comparative statics, not a forecast. Full tables on the
  <a href="scenarios.html">Scenarios</a> page.</p>
</section>
<section>
  <h2>Sections</h2>
  <ul class="links">
    <li><a href="model.html">Model</a> — architecture: MRIO data → IO/SFC → GVAR → scenarios.</li>
    <li><a href="forecast.html">Forecast</a> — the T1 CPI product: intervals, C4/C6, shadow track.</li>
    <li><a href="scenarios.html">Scenarios</a> — 7 energy/import/FX reports with top countries and
    industries.</li>
    <li><a href="data.html">Data</a> — FIGARO, WIOD, OECD, Eora: coverage and licences.</li>
    <li><a href="methodology.html">Methodology</a> — protocols, admission criteria C1–C6 and limits.</li>
    <li><a href="docs.html">Documents</a> — key project documents.</li>
  </ul>
</section>
"""
    return page("AMFE: research model of the world economy", "index", body, generated_at, lang="en")


def render_model_en(context: dict, generated_at: str) -> str:
    world = _world_figures(context)
    rows = [
        ["MRIO data", "FIGARO 2019 and WIOD 2016 — real country × industry tables", "implemented",
         "50×64 and 44×56; raw data outside git"],
        ["IO core", "static Leontief model x = (I−A)⁻¹(y+fda), balance validation", "implemented",
         "spectral radius and residuals converge"],
        ["SFC layer", "stocks and flows, cross-country settlement", "partial",
         "accounting prototypes without empirical calibration"],
        ["GVAR", "cross-country linkages and shock transmission", "prototype",
         "on real data, not fully calibrated"],
        ["Scenarios", "energy/import/FX comparative statics", "implemented",
         f"{len(context['scenarios'])} reports, RESEARCH_ONLY"],
        ["Dynamics and expectations", "fast/slow loop, drift", "not implemented",
         "a later stage after data and calibration"],
        ["Forecast product", "T1 CPI ensemble, protocol v5", "CANDIDATE / SHADOW_TEST",
         "ACTIVE admission not granted"],
    ]
    body = f"""
<section><h1>Model: the AMFE architecture</h1>
<p class="lead">Five layers: real input–output tables → IO/SFC core → GVAR → scenario reports →
forecast product. Below is what is actually implemented on real data and what remains a prototype.</p>
</section>
{_disclaimer_box_en()}
<section>
  <h2>Pipeline</h2>
  <ol class="flow">
    <li><strong>MRIO data.</strong> FIGARO 2019 ({esc(world["countries"])} countries ×
    {esc(world["industries"])} industries, {fmt_pct(world["figaro_share"])} of world GDP) and WIOD 2016
    ({esc(world["wiod"][0]["countries"] if world["wiod"] else "—")} ×
    {esc(world["wiod"][0]["industries"] if world["wiod"] else "—")}).</li>
    <li><strong>IO/SFC core.</strong> Block matrix A, solution x = (I−A)⁻¹(y+fda); row/column balance
    checks and the global value-added = final-demand identity.</li>
    <li><strong>GVAR.</strong> A prototype of cross-country linkages for shock transmission; full
    empirical calibration is not complete.</li>
    <li><strong>Scenarios.</strong> Comparative statics: an energy shock, import costs, FX
    pass-through; results by country and industry.</li>
    <li><strong>Forecast product.</strong> A T1 CPI ensemble (US year-over-year inflation, h=1) with
    intervals, under admission protocol v5.</li>
  </ol>
</section>
<section>
  <h2>Implemented vs not implemented</h2>
  {_table(["Layer", "Content", "Status", "Comment"], rows)}
  <p class="muted">An honest boundary: real MRIO tables and scenarios on them exist; a full dynamic
  SFC model, a calibrated GVAR and behavioural expectations do not. That is why the statuses are
  CANDIDATE/SHADOW_TEST and RESEARCH_ONLY, not “an admitted model”.</p>
</section>
<section>
  <h2>Invariants that are checked</h2>
  <ul>
    <li>MRIO row/column balances and the global Σva = Σy identity (for WIOD — with the signed
    adjustments of schema v1.1).</li>
    <li>Productiveness of matrix A: spectral radius &lt; 1 and a positive output vector.</li>
    <li>Scenarios are reproducible: identical inputs give identical reports, material-balance
    residuals are around 1e−8.</li>
  </ul>
</section>
"""
    return page("Model", "model", body, generated_at, lang="en")


def render_forecast_en(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    v5 = _v5_figures(context)
    v4 = _v4_figures(context)
    shadow = context["shadow"]
    c6 = shadow.get("c6") or {}
    projection = shadow.get("projection") or {}
    journal = shadow.get("journal") or {}
    quantiles = context["quantiles"]
    intervals = [("50%", quantiles[4], quantiles[14], "q25…q75"),
                 ("80%", quantiles[1], quantiles[17], "q10…q90"),
                 ("90%", quantiles[0], quantiles[18], "q05…q95")]
    rows = [[level, f"{fmt(low)} … {fmt(high)}", labels] for level, low, high, labels in intervals]
    controls = "".join(
        f"<li>{esc(arm)}: <strong>{fmt(feed['controls'][arm]['point'])}</strong></li>"
        for arm in ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"))
    body = f"""
<section><h1>T1 CPI forecast: US inflation, year over year, h=1</h1>
<p class="lead">Candidate {esc(feed["candidate"])}, origin {esc(feed["origin"])},
status {_badge(feed["status"])}, conditional_data_class =
{str(feed["conditional_data_class"]).lower()}. This is a research ensemble forecast, not an admitted
model.</p></section>
{_disclaimer_box_en()}
<section>
  <h2>Point forecast and intervals</h2>
  <div class="grid">
    <div class="card"><div class="card-label">Point</div>
      <div class="card-value">{fmt(context["point"])}</div></div>
    <div class="card"><div class="card-label">Median (q50)</div>
      <div class="card-value">{fmt(quantiles[9])}</div></div>
    <div class="card"><div class="card-label">Protocol</div>
      <div class="card-value">{esc(feed["protocol"]["version"])}</div>
      <div class="muted">FROZEN, SHA-256 {esc(str(feed["protocol"]["sha256"])[:12])}…</div></div>
  </div>
  {_table(["Interval", "Bounds", "Quantiles"], rows, "Intervals from the 19 quantiles 5%…95%")}
  {_svg_interval_ladder(quantiles, context["point"], "en")}
  <p class="muted">The chart is built from the same 19 quantiles; the red line is the point forecast.</p>
  <p><strong>Control arms (point):</strong></p>
  <ul>{controls}</ul>
</section>
<section>
  <h2>What is confirmed</h2>
  <p>The first real evaluation under protocol v5 for the T1 h=1 pair:</p>
  <ul>
    <li><strong>C1 PASS</strong> — relative RMSE/CRPS loss {fmt(v5["c1_w1_rmse"])} /
    {fmt(v5["c1_w1_crps"])} on window W1 against a threshold of 0.97.</li>
    <li><strong>C2 PASS</strong> — Romano–Wolf adjusted p-values {fmt(v5["c2_w1_rmse_p"])} (W1 RMSE)
    and {fmt(v5["c2_w2_crps_p"])} (W2 CRPS), FWER 0.10.</li>
    <li><strong>C5 PASS</strong> — the candidate is not worse than the comparator on secondary facts.</li>
  </ul>
</section>
<section>
  <h2>What is NOT confirmed</h2>
  <ul>
    <li><strong>C4 — FAIL on RMSE.</strong> Relative loss {fmt(v5["c4_rmse"])} in recession months of
    W1_EXT (threshold ≤ 1.00); the conditional Giacomini–White test rejects in favour of the comparator
    (p ≈ {fmt_sci(v5["c4_gw_p"])}). C4 PASS on CRPS ({fmt(v5["c4_crps"])}), but C4 requires passing
    every loss.</li>
    <li><strong>C6 — not passed.</strong> The forward journal has
    {esc(c6.get("n_resolved", 0))} of {esc(c6.get("min_forecasts", 12))} resolved forecasts and
    {esc(c6.get("distinct_target_periods", 0))} of
    {esc(c6.get("min_distinct_target_periods", 4))} distinct target periods. The C6 gate cannot be
    passed before the journal accumulates.</li>
    <li>SHADOW_TEST, LIMITED_DEPLOYMENT and ACTIVE have not been reached for this candidate.</li>
  </ul>
</section>
<section>
  <h2>Shadow track: {esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")}</h2>
  <p>Candidate status: {_badge(v4["status"] or "SHADOW_TEST")}
  ({esc(v4["status_label"] or "SHADOW_TEST")}), conditional_data_class =
  {str(v4["conditional"]).lower()}; C1/C2/C4/C5 — PASS, C6 — awaiting the journal. The track keeps the
  journal and measures the distance to the C6 gate.</p>
  {_table(["Metric", "Value"], [
      ["Track status", esc(shadow.get("status") or "—")],
      ["Journal rows in total", esc(journal.get("rows_total", "—"))],
      ["Resolved forecasts", f'{esc(c6.get("n_resolved", 0))} / {esc(c6.get("min_forecasts", 12))}'],
      ["Distinct target periods",
       f'{esc(c6.get("distinct_target_periods", 0))} / {esc(c6.get("min_distinct_target_periods", 4))}'],
      ["C6 gate estimate", esc(projection.get("estimated_gate_date") or "—")],
      ["C4 W1 RMSE / CRPS", f'{fmt(v4["c4_w1_rmse"])} / {fmt(v4["c4_w1_crps"])}'],
  ])}
  <p class="muted">No new admissions are granted: the track is monitoring, not an admission decision.</p>
</section>
"""
    return page("T1 CPI forecast", "forecast", body, generated_at, lang="en")


def render_scenarios_en(context: dict, generated_at: str) -> str:
    rows = []
    for scenario in context["scenarios"]:
        headline = scenario.get("headline") or {}
        rows.append([
            esc(scenario.get("id")),
            fmt((headline.get("relative_delta_output") or 0) * 100, 3) + "%",
            fmt_int(headline.get("total_delta_output")),
            esc(headline.get("largest_country") or "—"),
            esc(headline.get("largest_industry") or "—"),
            esc(scenario.get("description") or ""),
        ])
    tops = ""
    for scenario_id in TOP_SCENARIOS:
        report = context["reports"].get(scenario_id)
        if not report:
            continue
        impacts = report.get("impacts") or {}
        countries = ((impacts.get("countries") or {}).get("top") or [])[:5]
        industries = ((impacts.get("industries") or {}).get("top") or [])[:5]
        country_rows = [[esc(item.get("name")), fmt_int(item.get("delta_output")),
                         fmt((item.get("share_of_abs") or 0) * 100, 1) + "%"] for item in countries]
        industry_rows = [[esc(item.get("name")), fmt_int(item.get("delta_output")),
                          fmt((item.get("share_of_abs") or 0) * 100, 1) + "%"] for item in industries]
        tops += f"""
<h3>{esc(scenario_id)}: {esc((report.get("scenario") or {}).get("description") or "")}</h3>
<div class="columns">
  <div>{_table(["Country", "Δ output, USD_mn", "Share of |Δ|"], country_rows, "Top-5 countries")}</div>
  <div>{_table(["Industry", "Δ output, USD_mn", "Share of |Δ|"], industry_rows, "Top-5 industries")}</div>
</div>"""
    body = f"""
<section><h1>World scenarios: comparative statics on real FIGARO 2019</h1>
<p class="lead">Eight scenarios: an energy shock (−5% and +50%), higher import costs and FX pass-through for DE and FR.
This is not a forecast: the scenarios show how a shock propagates through supply chains under fixed
technology.</p></section>
{_disclaimer_box_en()}
<section>
  <h2>All scenarios</h2>
  {_table(["Scenario", "Δ output, %", "Δ output, USD_mn", "Largest country",
           "Largest industry", "Description"], rows,
          f"Status: {context['world'].get('status') or 'RESEARCH_ONLY'}")}
  {_svg_bars([(str(item.get("id")), ((item.get("headline") or {}).get("total_delta_output") or 0.0))
              for item in context["scenarios"]], "en")}
</section>
<section>
  <h2>Top-5 countries and industries</h2>
  <p class="muted">The same 8 scenarios are computed on OECD ICIO 2011 (71 economies × 34 industries, 95.4% of world value added): energy +50% +2.74 trn, import +10% −4.61 trn, FX USA −20% +0.67 trn USD; SFC PASS — see <a href="../docs/131_Сценарные_отчёты_мира.md">docs/131</a>.</p>
  <p>Sums over the absolute output change; ROW is the FIGARO rest-of-world aggregate.</p>
  {tops}
</section>
<section>
  <h2>Method and assumptions</h2>
  <ul>
    <li>Model: static linear Leontief model x = (I−A)⁻¹(y+fda), A = Z·diag(x)⁻¹; a scenario changes y
    or the import coefficients.</li>
    <li>No prices, substitution, dynamics or feedback loops; coefficients A are fixed.</li>
    <li>Energy: final demand −5% in industries CPA_B, CPA_C19, CPA_D35.</li>
    <li>Imports: import costs +10%/20% with a proportional cut in demand.</li>
    <li>FX: 10%/20% pass-through into import costs for the chosen country.</li>
    <li>Reports are aggregate: no raw Z matrices and no raw observed values.</li>
  </ul>
</section>
"""
    return page("Scenarios", "scenarios", body, generated_at, lang="en")


def render_data_en(context: dict, generated_at: str) -> str:
    world = _world_figures(context)
    figaro_source = ((context["figaro"].get("source") or {}))
    wiod_rows = []
    for item in world["wiod"]:
        wiod_rows.append([esc(item["id"]), esc(item["year"]),
                          f'{esc(item["countries"])} × {esc(item["industries"])}',
                          fmt_pct(item["share"], 4), esc(item["licence"] or "—")])
    blocked = {}
    for source in (context["blocked"].get("sources") or []):
        if isinstance(source, dict) and source.get("source_id"):
            blocked[source["source_id"]] = source
    oecd = blocked.get("oecd_icio") or {}
    eora = blocked.get("eora_mrio") or {}
    comtrade = blocked.get("un_comtrade") or {}
    body = f"""
<section><h1>Data: real MRIO tables and licences</h1>
<p class="lead">The world is built on real input–output tables. Raw data lives outside git and is not
published here: this page shows only metadata, coverage and licences.</p></section>
{_disclaimer_box_en()}
<section>
  <h2>FIGARO 2019 — the main world</h2>
  {_table(["Parameter", "Value"], [
      ["Source", esc(figaro_source.get("name") or world["source"] or "Eurostat FIGARO")],
      ["Size", f'{fmt_int(world["countries"])} × {fmt_int(world["industries"])} '
               f'(countries × industries; {esc(world["sectors"])} sectors)'],
      ["Year", esc(world["year"])],
      ["Units", esc(world["units"])],
      ["GDP coverage of named countries", fmt_pct(world["figaro_share"])],
      ["Coverage including the ROW aggregate", "100%"],
      ["Z cells", fmt_int(world["cells"]) + " (sparse storage)"],
      ["Licence", esc(figaro_source.get("licence") or "Eurostat re-use policy (open data)")],
      ["Canonical JSON", fmt_int(world["bytes"]) + " bytes, SHA-256 "
                         + esc(str(world["sha256"])[:12]) + "…"],
  ])}
  <p class="muted">Third party: data is not committed, only derived metrics are published.</p>
</section>
<section>
  <h2>WIOD 2016 — the second world</h2>
  {_table(["Dataset", "Year", "Countries × industries", "GDP coverage", "Licence"], wiod_rows)}
  <p>Coverage range across datasets: {fmt_pct(world["wiod_min"], 1)}–{fmt_pct(world["wiod_max"], 1)}.
  WIOD contains negative cells (inventory changes, value added), so schema v1.1 with signed
  adjustments is used — base y and va stay non-negative.</p>
</section>
<section>
  <h2>Sources: statuses</h2>
  {_table(["Source", "Status", "Why", "What to do"], [
      oecd_source_row("en"),
      ["Eora MRIO", esc(eora.get("status") or "BLOCKED_LICENCE"),
       "Only an academic licence with registration is free; commercial use requires purchase",
       "Do not use without a licence; the Eora parser is not implemented in this wave"],
      ["UN Comtrade", esc(comtrade.get("status") or "AVAILABLE_API"),
       "Not an MRIO: trade flows without an industry matrix",
       "Requires an HS → industry bridge; a separate decision"],
  ])}
</section>
<section>
  <h2>What must not be published</h2>
  <ul>
    <li>Raw third-party datasets (FIGARO/WIOD/OECD/Eora/Comtrade) and their values — only under the
    source licence; they never enter the repository or this site.</li>
    <li>Derived aggregates may be published: coverage, totals, shares, top lists, validation
    residuals — those are shown above.</li>
    <li>The research world must not be presented as an official forecast or an admitted model.</li>
  </ul>
</section>
"""
    return page("Data", "data", body, generated_at, lang="en")


def render_methodology_en(context: dict, generated_at: str) -> str:
    v5 = _v5_figures(context)
    v4 = _v4_figures(context)
    feed = context["feed"]
    criteria_rows = [
        ["C1", "Relative loss (RMSE/CRPS) against the comparator",
         f'{_badge("PASS") if v5["c1_pass"] else _badge("FAIL")} '
         f'{fmt(v5["c1_w1_rmse"])} / {fmt(v5["c1_w1_crps"])} (W1, threshold 0.97)'],
        ["C2", "Romano–Wolf: significance of the improvement", _badge("PASS") if v5["c2_pass"]
         else _badge("FAIL")],
        ["C3", "Model Confidence Set", "not implemented (non-gating)"],
        ["C4", "Recession robustness (RMSE and CRPS)", _badge("FAIL")
         + f' RMSE {fmt(v5["c4_rmse"])}, CRPS {_badge("PASS")} {fmt(v5["c4_crps"])}'],
        ["C5", "Secondary facts", _badge("PASS") if v5["c5_pass"] else _badge("FAIL")],
        ["C6", "Forward journal: 12 resolved forecasts, ≥4 periods",
         "not passed: 0 of 12"],
    ]
    tiers = [
        ["CANDIDATE", "the candidate passed some criteria, no admission", "T1 CPI — here",
         f'{esc(feed["candidate"])}'],
        ["SHADOW_TEST", "shadow track on the live journal, no right to use", "current",
         esc(v4["candidate"] or "ENSEMBLE_REGIME_V4")],
        ["LIMITED_DEPLOYMENT", "limited use after C1–C6", "not reached", "—"],
        ["ACTIVE", "an admitted model", "not reached", "—"],
    ]
    body = f"""
<section><h1>Methodology and statuses</h1>
<p class="lead">AMFE keeps forecast accuracy and admission apart. Criteria C1–C6 are checked by a
frozen protocol on labelled windows and the forward journal; this site does not assign statuses.</p>
</section>
{_disclaimer_box_en()}
<section>
  <h2>Protocols</h2>
  <ul>
    <li><strong>Protocol v5</strong> — the current frozen evaluation protocol for T1 CPI (version
    {esc(feed["protocol"]["version"])}); an extended C4 on reconstructed 1990–1996 data and the
    forward journal.</li>
    <li><strong>Protocol v4</strong> — the previous frozen version; v4 results are preserved and not
    rewritten.</li>
    <li>The frozen chain is protocol → R3 specification → NF-SIM thresholds → comparator composition.
    Hashes are verified and tools fail closed on a mismatch.</li>
  </ul>
</section>
<section>
  <h2>Admission criteria C1–C6</h2>
  {_table(["Criterion", "What it checks", "Current T1 h=1 result"], criteria_rows)}
</section>
<section>
  <h2>Status ladder</h2>
  {_table(["Status", "Meaning", "State", "Candidate"], tiers)}
  <p class="muted">Statuses are assigned by the protocol and by a human, not by this site. The site is
  a showcase of registered results.</p>
</section>
<section>
  <h2>Limitations</h2>
  <ul>
    <li>C4 FAIL on RMSE: the ensemble is worse than the comparator in recessions; CRPS PASS does not
    compensate for RMSE.</li>
    <li>C6 not passed: 0 of 12 resolved forecasts, at least 4 distinct target periods are needed; the
    gate cannot be accelerated.</li>
    <li>conditional_data_class = true: part of C4 relies on reconstructed data, not on REAL_TIME.</li>
    <li>World model: scenarios are comparative statics without prices, substitution or dynamics; GVAR
    and SFC are not fully calibrated.</li>
    <li>Raw third-party data is not published; the site contains only derived metrics.</li>
  </ul>
</section>
"""
    return page("Methodology", "methodology", body, generated_at, lang="en")


def render_docs_en(context: dict, generated_at: str) -> str:
    rows = [[f'<a href="../{esc(link["file"])}">{esc(link["title"])}</a>', esc(link["file"])]
            for link in context["doc_links"]]
    body = f"""
<section><h1>Project documents</h1>
<p class="lead">Key documents: data, core, scenarios, protocols, support and publication. The links
point to repository files.</p></section>
{_disclaimer_box_en()}
<section>
  {_table(["Document", "File"], rows)}
</section>
<section>
  <h2>Primary sources of the numbers on this site</h2>
  <ul>
    <li><code>build/product/forecast_latest.json</code> — the T1 CPI forecast and its status.</li>
    <li><code>build/world/report_index.json</code>, <code>build/world/reports/*.json</code> — world
    scenarios.</li>
    <li><code>protocols/first_real_evaluation_report_v5.json</code>,
    <code>protocols/regime_v4_candidate_evaluation_report.json</code> — criteria.</li>
    <li><code>build/shadow/shadow_status.json</code> — the shadow track and C6 progress.</li>
    <li><code>data/mrio/**/manifest.json</code> — FIGARO/WIOD coverage (metadata).</li>
  </ul>
</section>
"""
    return page("Documents", "docs", body, generated_at, lang="en")


def _p2_release() -> dict:
    """Optional P2 0.1 release manifest (build artefact); empty when the release was not built."""
    try:
        return read_json(ROOT / "build" / "p2_release" / "p2_release.json", "P2 0.1 manifest")
    except SiteBuildError:
        return {}


def _p2_release_table(lang: str = "ru") -> str:
    """D1–D6 status table from the optional P2 0.1 manifest (fail-soft when absent)."""
    release = _p2_release()
    if not release:
        return ('<p class="muted">Манифест P2 0.1 ещё не собран: '
                '<code>python tools/p2_release.py</code>.</p>' if lang == "ru" else
                '<p class="muted">The P2 0.1 manifest has not been built yet: '
                '<code>python tools/p2_release.py</code>.</p>')
    titles_ru = {"D1": "Данные", "D2": "Статическое ядро", "D3": "SFC",
                 "D4": "Динамика", "D5": "Сценарии", "D6": "Интеграция"}
    titles_en = {"D1": "Data", "D2": "Static core", "D3": "SFC",
                 "D4": "Dynamics", "D5": "Scenarios", "D6": "Integration"}
    titles = titles_ru if lang == "ru" else titles_en
    rows = []
    for criterion in release.get("criteria") or []:
        key = str(criterion.get("id") or "")
        rows.append([esc(key), _badge(str(criterion.get("status") or "—")),
                     esc(titles.get(key, ""))])
    admitted = "Да" if release.get("admission") else ("Нет" if lang == "ru" else "No")
    if lang == "ru":
        intro = (f'<p>Релиз <strong>{esc(release.get("release") or "P2 0.1")}</strong> '
                 f'(версия {esc(release.get("version") or "0.1")}, {esc(release.get("date") or "—")}) — '
                 f'<strong>{esc(release.get("status") or "RESEARCH_RELEASE")}</strong>; допуск модели: '
                 f'<strong>{admitted}</strong>. D4 — честно отрицательный (RESEARCH_ONLY), не допуск. '
                 f'Подробности: <a href="docs/137_P2_0.1_релиз.md">docs/137</a>.</p>')
    else:
        intro = (f'<p>Release <strong>{esc(release.get("release") or "P2 0.1")}</strong> '
                 f'(version {esc(release.get("version") or "0.1")}, {esc(release.get("date") or "—")}) — '
                 f'<strong>{esc(release.get("status") or "RESEARCH_RELEASE")}</strong>; model '
                 f'admission: <strong>{admitted}</strong>. D4 is an honest negative (RESEARCH_ONLY), '
                 f'not an admission. Details: <a href="../docs/137_P2_0.1_релиз.md">docs/137</a>.</p>')
    return intro + _table(["D", "Статус" if lang == "ru" else "Status",
                           "Критерий" if lang == "ru" else "Criterion"], rows)


def render_status(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    shadow = context["shadow"]
    c6 = shadow.get("c6") or {}
    projection = shadow.get("projection") or {}
    journal = shadow.get("journal") or {}
    v4 = _v4_figures(context)
    world = _world_figures(context)
    protocol = feed["protocol"]["version"]
    sha = str(feed["protocol"]["sha256"])[:12]
    body = f"""
<section><h1>Статус и свежесть снимка</h1>
<p class="lead">Снимок собран <strong>{esc(generated_at)}</strong>; протокол оценивания
<strong>{esc(protocol)}</strong> (FROZEN, SHA-256 <code>{esc(sha)}…</code>). Сайт не присваивает
статусы и не пересчитывает модель: он показывает зарегистрированные результаты.</p></section>
{_disclaimer_box()}
<section>
  <h2>Текущие статусы</h2>
  {_table(["Объект", "Статус", "Что это означает"], [
      [f"Прогноз T1 CPI ({esc(feed['candidate'])})", _badge(feed["status"]),
       "Кандидат прошёл C1/C2/C5, но не C4 и не C6; к применению не допущен."],
      [f"Теневой трек {esc(v4['candidate'] or 'ENSEMBLE_REGIME_V4')}",
       _badge(v4["status"] or "SHADOW_TEST"),
       "Идёт мониторинг и накопление журнала; C4 PASS по обоим loss, C6 ждёт."],
      ["Мировая модель (FIGARO 2019 / OECD 2016 / WIOD 2016)", "RESEARCH_ONLY",
       "Сценарии сравнительной статики, не прогноз и не допуск."],
  ])}
</section>
<section>
  <h2>P2 0.1 — релиз исследовательского мира</h2>
  {_p2_release_table("ru")}
</section>
<section>
  <h2>Прогресс C6 (forward-журнал)</h2>
  {_svg_progress(int(c6.get("n_resolved", 0)), int(c6.get("min_forecasts", 12)))}
  {_table(["Показатель", "Значение"], [
      ["Разрешённых прогнозов", f'{esc(c6.get("n_resolved", 0))} / {esc(c6.get("min_forecasts", 12))}'],
      ["Различных целевых периодов",
       f'{esc(c6.get("distinct_target_periods", 0))} / {esc(c6.get("min_distinct_target_periods", 4))}'],
      ["Строк журнала всего", esc(journal.get("rows_total", "—"))],
      ["Последний винтаж", esc(journal.get("last_vintage_week", "—"))],
      ["Ориентир гейта C6", esc(projection.get("estimated_gate_date") or "—")],
      ["Допуск присвоен?", "Нет" if not c6.get("pass") else "Да"],
  ])}
  <p class="muted">Ориентир — оценка по частоте релизов, а не обещание даты; гейт требует реальных
  разрешённых прогнозов.</p>
</section>
<section>
  <h2>Что в этом снимке</h2>
  <ul>
    <li>Мир FIGARO 2019: {fmt_int(world["countries"])} × {fmt_int(world["industries"])},
    покрытие ВВП {fmt_pct(world["figaro_share"])}.</li>
    <li>OECD ICIO 2016: 71 экономика × 34 отрасли, 2000–2011, покрытие мировой ДС 95,4%
    (docs/130).</li>
    <li>WIOD 2016: 15 лет 2000–2014, 44 × 56, покрытие 84,4–89,9% (docs/129).</li>
    <li>Сценариев: {len(context["scenarios"])}; все — сравнительная статика на реальной таблице.</li>
    <li>Прогноз T1 CPI: origin {esc(feed["origin"])}, h={esc(feed["horizon"])},
    точка {fmt(context["point"])}.</li>
  </ul>
</section>
<section>
  <h2>Как цитировать и где история</h2>
  {_citation_block("ru", generated_at, protocol)}
  <p>Репозиторий и история коммитов:
  <a href="{esc(REPO_URL)}">{esc(REPO_URL)}</a>. Живой сайт:
  <a href="{esc(SITE_URL)}">{esc(SITE_URL)}</a>.</p>
  <p class="muted">Обновление сайта: пересобрать артефакты, затем
  <code>python tools/site_build.py</code> и push в <code>site/**</code> — деплой запустится сам.</p>
</section>
"""
    return page("Статус и релизы", "status", body, generated_at)


def render_status_en(context: dict, generated_at: str) -> str:
    feed = context["feed"]
    shadow = context["shadow"]
    c6 = shadow.get("c6") or {}
    projection = shadow.get("projection") or {}
    journal = shadow.get("journal") or {}
    v4 = _v4_figures(context)
    world = _world_figures(context)
    protocol = feed["protocol"]["version"]
    sha = str(feed["protocol"]["sha256"])[:12]
    body = f"""
<section><h1>Status and snapshot freshness</h1>
<p class="lead">Snapshot built <strong>{esc(generated_at)}</strong>; evaluation protocol
<strong>{esc(protocol)}</strong> (FROZEN, SHA-256 <code>{esc(sha)}…</code>). The site does not assign
statuses and does not recompute the model: it publishes registered results.</p></section>
{_disclaimer_box_en()}
<section>
  <h2>Current statuses</h2>
  {_table(["Object", "Status", "What it means"], [
      [f"T1 CPI forecast ({esc(feed['candidate'])})", _badge(feed["status"]),
       "The candidate passes C1/C2/C5 but not C4 or C6; it is not admitted for use."],
      [f"Shadow track {esc(v4['candidate'] or 'ENSEMBLE_REGIME_V4')}",
       _badge(v4["status"] or "SHADOW_TEST"),
       "Monitoring and journal accumulation; C4 passes both losses, C6 is pending."],
      ["World model (FIGARO 2019 / OECD 2016 / WIOD 2016)", "RESEARCH_ONLY",
       "Comparative-statics scenarios, not a forecast and not an admission."],
  ])}
</section>
<section>
  <h2>P2 0.1 — research-world release</h2>
  {_p2_release_table("en")}
</section>
<section>
  <h2>C6 progress (forward journal)</h2>
  {_svg_progress(int(c6.get("n_resolved", 0)), int(c6.get("min_forecasts", 12)), "en")}
  {_table(["Indicator", "Value"], [
      ["Resolved forecasts", f'{esc(c6.get("n_resolved", 0))} / {esc(c6.get("min_forecasts", 12))}'],
      ["Distinct target periods",
       f'{esc(c6.get("distinct_target_periods", 0))} / {esc(c6.get("min_distinct_target_periods", 4))}'],
      ["Journal rows in total", esc(journal.get("rows_total", "—"))],
      ["Last vintage", esc(journal.get("last_vintage_week", "—"))],
      ["C6 gate estimate", esc(projection.get("estimated_gate_date") or "—")],
      ["Admission granted?", "No" if not c6.get("pass") else "Yes"],
  ])}
  <p class="muted">The estimate follows release frequency and is not a promised date; the gate needs
  real resolved forecasts.</p>
</section>
<section>
  <h2>What is in this snapshot</h2>
  <ul>
    <li>FIGARO 2019 world: {fmt_int(world["countries"])} × {fmt_int(world["industries"])},
    GDP coverage {fmt_pct(world["figaro_share"])}.</li>
    <li>OECD ICIO 2016: 71 economies × 34 industries, 2000–2011, 95.4% world value-added coverage
    (docs/130).</li>
    <li>WIOD 2016: 15 years 2000–2014, 44 × 56, coverage 84.4–89.9% (docs/129).</li>
    <li>Scenarios: {len(context["scenarios"])}; all are comparative statics on a real table.</li>
    <li>T1 CPI forecast: origin {esc(feed["origin"])}, h={esc(feed["horizon"])},
    point {fmt(context["point"])}.</li>
  </ul>
</section>
<section>
  <h2>How to cite and where the history lives</h2>
  {_citation_block("en", generated_at, protocol)}
  <p>Repository and commit history:
  <a href="{esc(REPO_URL)}">{esc(REPO_URL)}</a>. Live site:
  <a href="{esc(SITE_URL)}">{esc(SITE_URL)}</a>.</p>
  <p class="muted">Site update: rebuild the artefacts, then <code>python tools/site_build.py</code>
  and push to <code>site/**</code>; deployment starts automatically.</p>
</section>
"""
    return page("Status and releases", "status", body, generated_at, lang="en")


CSS = """/* AMFE static site: no external resources, no JavaScript, no frameworks. */
:root {
  color-scheme: light dark;
  --bg: #f7f7f4;
  --fg: #1d232a;
  --muted: #5c6670;
  --card: #ffffff;
  --line: #d8d8d0;
  --accent: #0b5c6b;
  --warn-bg: #fff4e0;
  --warn-border: #b45309;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181c;
    --fg: #e8e6e0;
    --muted: #a4acb4;
    --card: #1d242a;
    --line: #333c44;
    --accent: #6fc3d2;
    --warn-bg: #3a2b12;
    --warn-border: #e0a33a;
  }
}
* { box-sizing: border-box; }
html { font-size: 16px; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.55;
}
.wrap { max-width: 66rem; margin: 0 auto; padding: 0 1.25rem; }
.site-header {
  border-bottom: 1px solid var(--line);
  background: var(--card);
  position: sticky;
  top: 0;
  z-index: 2;
}
.site-header .wrap {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem 1.5rem;
  padding-top: 0.7rem;
  padding-bottom: 0.7rem;
}
.brand { font-weight: 700; color: var(--fg); text-decoration: none; }
.site-header nav { display: flex; flex-wrap: wrap; gap: 0.35rem 0.9rem; }
.site-header nav a {
  color: var(--muted);
  text-decoration: none;
  padding: 0.15rem 0.1rem;
  border-bottom: 2px solid transparent;
}
.site-header nav a:hover, .site-header nav a:focus { color: var(--fg); }
.site-header nav a.active { color: var(--accent); border-bottom-color: var(--accent); }
.lang-switch { display: flex; gap: 0.35rem; margin-left: auto; align-items: center; font-size: 0.85rem; }
.lang-switch a, .lang-switch .current {
  display: inline-block;
  padding: 0.05rem 0.55rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  text-decoration: none;
}
.lang-switch .current { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.lang-switch a { color: var(--muted); }
.lang-switch a:hover, .lang-switch a:focus { color: var(--fg); }
main { padding: 1.5rem 0 2.5rem; }
h1 { font-size: clamp(1.5rem, 1.1rem + 1.6vw, 2.3rem); line-height: 1.2; margin: 0.4rem 0 0.8rem; }
h2 { font-size: clamp(1.15rem, 1rem + 0.6vw, 1.5rem); margin: 2rem 0 0.7rem; }
h3 { font-size: 1.05rem; margin: 1.4rem 0 0.5rem; }
p, li { max-width: 60rem; }
.lead { font-size: 1.08rem; color: var(--muted); }
section { margin-bottom: 1.6rem; }
.hero { padding: 1.2rem 0 0.2rem; }
.badges { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.badge {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.05rem 0.55rem;
  font-size: 0.82rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.badge-warn { background: var(--warn-bg); border-color: var(--warn-border); color: var(--fg); }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.9rem;
  margin: 1rem 0;
}
.card { background: var(--card); border: 1px solid var(--line); border-radius: 0.6rem; padding: 0.9rem 1rem; }
.card-label { font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.card-value { font-size: 1.45rem; font-weight: 700; margin: 0.15rem 0 0.25rem; }
.muted { color: var(--muted); font-size: 0.92rem; }
.callout { border-left: 4px solid var(--line); padding: 0.7rem 1rem; border-radius: 0.3rem; margin: 1rem 0; }
.callout-warn { background: var(--warn-bg); border-left-color: var(--warn-border); }
.table-wrap { overflow-x: auto; margin: 0.9rem 0; border: 1px solid var(--line); border-radius: 0.5rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.94rem; background: var(--card); }
caption { text-align: left; padding: 0.55rem 0.7rem; color: var(--muted); font-size: 0.88rem; }
th, td { text-align: left; vertical-align: top; padding: 0.45rem 0.65rem; border-bottom: 1px solid var(--line); }
thead th { background: rgba(127, 127, 127, 0.12); font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
.columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); gap: 1rem; }
.flow { padding-left: 1.3rem; }
.flow li { margin-bottom: 0.5rem; }
.links { padding-left: 1.2rem; }
.links li { margin-bottom: 0.35rem; }
a { color: var(--accent); }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.9em; }
.site-footer { border-top: 1px solid var(--line); background: var(--card); padding: 1.1rem 0 1.6rem; }
.site-footer p { margin: 0.35rem 0; font-size: 0.88rem; }
.build-stamp { color: var(--muted); font-size: 0.78rem; white-space: nowrap; }
.chart { width: 100%; height: auto; margin: 0.8rem 0; display: block; }
.chart-label { fill: var(--fg); font-size: 13px; }
.chart-value { fill: var(--muted); font-size: 12px; }
.chart-point { fill: #b91c1c; font-size: 12px; font-weight: 600; }
.citation { background: var(--card); border: 1px solid var(--line); border-radius: 0.4rem;
  padding: 0.8rem 1rem; overflow-x: auto; font-size: 0.82rem; line-height: 1.45; }
@media (max-width: 40rem) {
  .site-header .wrap { gap: 0.4rem 0.8rem; }
  .site-header nav { font-size: 0.92rem; }
}
"""

RENDERERS_RU = {
    "index": render_index, "model": render_model, "forecast": render_forecast,
    "scenarios": render_scenarios, "data": render_data, "methodology": render_methodology,
    "docs": render_docs, "status": render_status,
}
RENDERERS_EN = {
    "index": render_index_en, "model": render_model_en, "forecast": render_forecast_en,
    "scenarios": render_scenarios_en, "data": render_data_en, "methodology": render_methodology_en,
    "docs": render_docs_en, "status": render_status_en,
}
LANGUAGES = (("ru", RENDERERS_RU), ("en", RENDERERS_EN))


# ---------------------------------------------------------------------------------------- build
def build_site(root: Path = ROOT, out_dir: Path | None = None,
               generated_at: str | None = None) -> list[Path]:
    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    context = load_context(Path(root))
    out_dir = Path(out_dir) if out_dir is not None else Path(root) / "site"
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lang, renderers in LANGUAGES:
        target_dir = out_dir if lang == "ru" else out_dir / lang
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in PAGES:
            document = renderers[name](context, generated_at)
            path = target_dir / f"{name}.html"
            path.write_text(document, encoding="utf-8", newline="\n")
            written.append(path)
    style_path = assets / "style.css"
    style_path.write_text(CSS, encoding="utf-8", newline="\n")
    written.append(style_path)
    # The documentation page links project docs; GitHub Pages serves only this directory, so the
    # linked documents are copied next to the pages (site/docs/...) to keep the links resolving
    # both locally and on the deployed site.
    docs_dir = out_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for relative in DOC_FILES:
        source = Path(root) / relative
        target = docs_dir / Path(relative).name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        written.append(target)
    # Crawler metadata: sitemap and robots are static files of the deployed site (absolute URLs are
    # required by the sitemap protocol; they never appear in the HTML/CSS themselves).
    pages = [""] + [f"{name}.html" for name in PAGES] + ["en/"] + [
        f"en/{name}.html" for name in PAGES]
    urls = "\n".join(
        f"  <url><loc>{esc(SITE_URL + page)}</loc><lastmod>{generated_at[:10]}</lastmod></url>"
        for page in pages)
    sitemap = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n')
    sitemap_path = out_dir / "sitemap.xml"
    sitemap_path.write_text(sitemap, encoding="utf-8", newline="\n")
    written.append(sitemap_path)
    robots = ("User-agent: *\nAllow: /\n"
              f"Sitemap: {SITE_URL}sitemap.xml\n")
    robots_path = out_dir / "robots.txt"
    robots_path.write_text(robots, encoding="utf-8", newline="\n")
    written.append(robots_path)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Собрать двуязычный статический сайт AMFE (RU в site/, EN в site/en/)")
    parser.add_argument("--root", default=str(ROOT), help="корень репозитория")
    parser.add_argument("--out-dir", default=None, help="каталог вывода (по умолчанию site/)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 дата-время снимка; по умолчанию текущее UTC (для детерминизма)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = Path(args.out_dir) if args.out_dir else root / "site"
    generated_at = args.generated_at
    if generated_at is not None:
        try:
            datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        except ValueError:
            print(json.dumps({"ok": False, "error": f"--generated-at: ожидался ISO 8601, "
                                                   f"получено {generated_at!r}"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
    try:
        written = build_site(root, out_dir=out_dir, generated_at=generated_at)
    except SiteBuildError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    relative = [str(path.relative_to(out_dir)).replace("\\", "/") for path in written]
    sizes = {name: (out_dir / name).stat().st_size for name in relative}
    print(json.dumps({"ok": True, "out_dir": str(out_dir).replace("\\", "/"),
                      "files": relative,
                      "bytes": sizes, "total_bytes": sum(sizes.values())},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
