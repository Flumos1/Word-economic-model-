"""Institutions scenarios CLI (docs/177 block 15; docs/203; docs/181 section 7).

Turns the research-only institutions block (``amfe.world.institutions``) into a deterministic
JSON + Markdown report:

    build/institutions/report.json              index + every scenario report
    build/institutions/report.md                human-readable summary
    build/institutions/reports/<ID>.json         one scenario report
    build/institutions/reports/<ID>.md           one scenario report, readable
    build/institutions/demo.json / demo.md       profile x scenario matrix (``--demo`` only)

What the tool does
------------------
* takes **one profile** - a built-in ``EXAMPLE-*`` illustration, a user JSON, or six numbers passed
  with ``--indicators`` - and overlays the declared scenarios ``status_quo`` / ``reform`` /
  ``deterioration`` / ``polarization`` on it;
* reports the composite score (0-100), the dispersion between the six dimensions, the risk-premium
  change in basis points and the investment/GDP and growth deltas (explicit elasticities);
* prints assumptions, checks, limitations, ``status=RESEARCH_ONLY`` and ``admission=false`` in both
  JSON and Markdown.

Design rules
------------
* **No country data.**  There is no country table in the block: ``--country`` only accepts
  ``EXAMPLE-*`` profiles or a path to a user JSON.  A country name is not a profile.
* **No data, no network.**  The engine reads no series, no clock and no randomness; only the CLI
  reads the user file named by ``--country`` / ``--out`` paths.
* **Aggregates only.**  The report contains scores, premiums, GDP shares and points - never raw
  indicator series, micro rows or per-country tables.  ``checks.no_raw_data`` is verified.
* **Deterministic.**  Fixed iteration order, sorted JSON keys, LF newlines: two runs on the same
  inputs produce byte-identical files, and every report carries a SHA-256 digest.
* **Fail-closed.**  A missing user profile is ``BLOCKED_INPUT`` (exit 2); broken JSON, an unknown
  country id, bad indicators or an unknown scenario raise (exit 1).  Nothing is written on failure.
* **Research only.**  ``status=RESEARCH_ONLY``, ``admission=false``, ``gating=false``,
  ``causal_claim=false``: a scenario is not a forecast, not an admission decision and not a causal
  estimate of institutions.

CLI
---
    python tools/institutions_scenarios.py --out build/institutions
    python tools/institutions_scenarios.py --scenario reform,polarization --country EXAMPLE-LOW
    python tools/institutions_scenarios.py --indicators "voice=60,stability=55,effectiveness=58,\
regulatory=57,rule_of_law=62,corruption=50"
    python tools/institutions_scenarios.py --country my_profile.json --json
    python tools/institutions_scenarios.py --demo
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.world.institutions import (  # noqa: E402
    ADMISSION,
    ASSUMPTIONS,
    CAUSAL_CLAIM,
    DATA_STATUS,
    GATING,
    INDICATOR_KEYS,
    INDICATOR_LABELS_RU,
    NOT_CALIBRATED,
    STATUS,
    STATUS_NOTE,
    InstitutionError,
    InstitutionProfile,
    InstitutionalIndicators,
    OverlayParameters,
    default_scenarios,
    example_profiles,
    find_example_profile,
    load_bundle,
    parse_indicator_text,
    run_all,
)
from amfe.world.institutions import digest as payload_digest  # noqa: E402
from amfe.world.institutions import limitations as block_limitations  # noqa: E402

DEFAULT_OUT = ROOT / "build" / "institutions"
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
REPORT_SCHEMA = "amfe.world.institutions.report.v1"
DEMO_SCHEMA = "amfe.world.institutions.demo.v1"


class BlockedInput(RuntimeError):
    """A required input file is missing (fail-closed, exit 2)."""


# ---------------------------------------------------------------------------------------- markdown
def _num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}".replace("-", "−")


def _signed(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.{digits}f}".replace("-", "−")


def _status_line(payload: dict) -> str:
    return (f"Статус: **{payload['status']}**, admission=false, gating=false, "
            "causal_claim=false; данные `ASSUMPTION_BASED`, `not_calibrated=true`. "
            "Сценарий — не прогноз, не допуск и не причинная оценка.")


def _scenario_table(rows: list[dict]) -> list[str]:
    lines = [
        "| Сценарий | Тип | Δ качества, пункты | Δ премии, б.п. | Δ инвестиций, п.п. ВВП "
        "| Δ роста, п.п. |",
        "|---|---|---|---|---|---|",
    ]
    for entry in rows:
        headline = entry["headline"]
        lines.append(
            f"| `{entry['id']}` | {entry['kind']} | "
            f"{_signed(headline['delta_score_points'])} | "
            f"{_signed(headline['delta_risk_premium_bps'], 1)} | "
            f"{_signed(headline['delta_investment_pp'], 3)} | "
            f"{_signed(headline['delta_growth_pp'], 3)} |")
    return lines


def _render_profile(profile: dict) -> list[str]:
    lines = [
        "## Профиль (допущения, не данные)",
        "",
        f"`{profile['name']}` · источник `{profile['source']}` · "
        f"`not_calibrated={str(profile['not_calibrated']).lower()}`",
        "",
        "| Индикатор | Значение | Вес | Вклад |",
        "|---|---|---|---|",
    ]
    for item in profile["contributions"]:
        lines.append(f"| {item['label_ru']} (`{item['indicator']}`) | "
                     f"{_num(item['value'], 1)} | {_num(item['weight'], 4)} | "
                     f"{_num(item['contribution'], 2)} |")
    lines += [
        "",
        f"Композит: **{_num(profile['score'], 2)}** из 100; разброс между индикаторами "
        f"{_num(profile['dispersion'], 2)} пункта.",
        "",
        "| Измерение | Значение |",
        "|---|---|",
    ]
    for key, value in profile["dimensions"].items():
        lines.append(f"| {profile['dimension_labels_ru'][key]} (`{key}`) | {_num(value, 1)} |")
    lines += ["", f"_{profile['disclaimer']}_", ""]
    return lines


def _render_parameters(parameters: dict) -> list[str]:
    return [
        "## Параметры оверлея (явные эластичности)",
        "",
        "| Параметр | Значение |",
        "|---|---|",
        f"| Базовая премия за риск | {_num(parameters['base_risk_premium_bps'], 1)} б.п. |",
        f"| beta: б.п. на 1 пункт разрыва качества | "
        f"{_num(parameters['beta_bps_per_point'], 2)} |",
        f"| gamma: б.п. на 1 пункт разброса | "
        f"{_num(parameters['gamma_bps_per_point_dispersion'], 2)} |",
        f"| Потолок премии | {_num(parameters['premium_cap_bps'], 0)} б.п. |",
        f"| Инвестиции/ВВП (база) | {_num(parameters['base_investment_ratio'] * 100, 1)}% |",
        f"| Эластичность инвестиций | "
        f"{_num(parameters['elasticity_investment_pp_per_point'], 3)} п.п. ВВП на пункт |",
        f"| Эластичность роста | "
        f"{_num(parameters['elasticity_growth_pp_per_point'], 3)} п.п. на пункт |",
        f"| Горизонт | {parameters['horizon_years']} лет |",
        "",
        "Все параметры — `ASSUMPTION_BASED`, не оценки: ни одна эластичность не калибровалась.",
        "",
    ]


def _render_checks(checks: dict) -> list[str]:
    labels = {
        "score_bounds": "Композит в [0, 100]",
        "indicator_bounds": "Индикаторы в [0, 100]",
        "weights_normalised": "Сумма весов = 1",
        "premium_non_negative": "Премия >= 0",
        "investment_bounds": "Инвестиции в [0, 1] ВВП",
        "path_monotone_ramp": "Рампа не убывает",
        "no_raw_data": "Нет сырых данных",
        "gating": "gating=false",
        "causal_claim": "causal_claim=false",
    }
    lines = ["| Проверка | Статус |", "|---|---|"]
    for key, value in checks.items():
        ok = (value is True) if key not in ("gating", "causal_claim") else (value is False)
        lines.append(f"| {labels.get(key, key)} | {'PASS' if ok else 'FAIL'} |")
    return lines


def _render_path(rows: list[dict]) -> list[str]:
    lines = [
        "| Год | Рампа | Качество | Разброс | Премия, б.п. | Инвестиции/ВВП |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['year']} | {_num(row['ramp'], 2)} | {_num(row['score'], 2)} | "
            f"{_num(row['dispersion'], 2)} | {_num(row['risk_premium_bps'], 1)} | "
            f"{_num(row['investment_ratio'] * 100, 1)}% |")
    return lines


def render_scenario_markdown(report: dict) -> str:
    scenario = report["scenario"]
    full = report["overlay"]["full_effect"]
    lines = [
        f"# {scenario['scenario_id']}: {scenario['title_ru']}",
        "",
        _status_line(report),
        "",
        f"Профиль: `{report['profile']['name']}` (источник `{report['profile']['source']}`); "
        f"базовый композит {_num(report['overlay']['base']['score'], 2)}.",
        "",
        f"Сдвиги сценария по индикаторам (пункты шкалы 0–100, magnitude "
        f"{_num(scenario['magnitude'], 2)}, рампа {scenario['ramp_years']} г.):",
        "",
        "| Индикатор | Сдвиг |",
        "|---|---|",
    ]
    for key in INDICATOR_KEYS:
        lines.append(f"| {INDICATOR_LABELS_RU[key]} (`{key}`) | "
                     f"{_signed(scenario['effective_deltas'][key], 1)} |")
    lines += [
        "",
        "## Эффект при полной рампе (r = 1)",
        "",
        "| Показатель | База | Сценарий | Δ |",
        "|---|---|---|---|",
        f"| Композит качества (0–100) | {_num(report['overlay']['base']['score'], 2)} | "
        f"{_num(full['score'], 2)} | {_signed(full['delta_score_points'])} пункта |",
        f"| Разброс между индикаторами | {_num(report['overlay']['base']['dispersion'], 2)} | "
        f"{_num(full['dispersion'], 2)} | {_signed(full['delta_dispersion'])} |",
        f"| Премия за риск | {_num(full['risk_premium']['base_bps'], 1)} б.п. | "
        f"{_num(full['risk_premium']['level_bps'], 1)} б.п. | "
        f"{_signed(full['risk_premium']['delta_bps'], 1)} б.п. |",
        f"| Инвестиции/ВВП | {_num(full['investment']['base_ratio'] * 100, 1)}% | "
        f"{_num(full['investment']['level_ratio'] * 100, 1)}% | "
        f"{_signed(full['investment']['delta_pp'], 3)} п.п. |",
        f"| Потенциальный рост | — | — | {_signed(full['growth']['delta_pp'], 3)} п.п. |",
        "",
        f"Запрошенный сдвиг композита {_signed(full['requested_delta_points'], 3)} пункта, "
        f"применённый {_signed(full['applied_delta_points'], 3)}"
        f"{' (ограничен шкалой 0–100)' if full['clipped'] else ''}.",
        "",
        "## Путь по годам",
        "",
    ]
    lines += _render_path(report["overlay"]["path"])
    lines += [
        "",
        f"Полная рампа достигается за {report['overlay']['ramp_years']} г.; "
        f"горизонт {report['overlay']['horizon_years']} лет; "
        f"сошлось: {'да' if report['overlay']['converged'] else 'нет'}.",
        "",
        "## Проверки",
        "",
    ]
    lines += _render_checks(report["checks"])
    lines += ["", "## Допущения", ""]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in report["assumptions"]]
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", f"digest: `{report['digest']}`", ""]
    return "\n".join(lines)


def render_index_markdown(payload: dict) -> str:
    index = payload["index"]
    lines = [
        "# Блок 15 «Институты»: сценарии качества управления",
        "",
        _status_line(index),
        "",
        f"Профиль: `{index['profile']['name']}` · источник `{index['profile']['source']}` · "
        f"страновая таблица: нет · данные не читались.",
        "",
        "## Сценарии",
        "",
    ]
    lines += _scenario_table(index["scenarios"])
    lines += [
        "",
        f"Максимальный |Δ качества|: {_num(index['max_abs_delta_score'], 2)} пункта.",
        "",
        "## Проверки",
        "",
        "| Проверка | Статус |",
        "|---|---|",
        f"| Сценариев | {index['checks']['scenario_count']} |",
        f"| Композит в [0, 100] | {'PASS' if index['checks']['all_score_bounds'] else 'FAIL'} |",
        f"| Индикаторы в [0, 100] | "
        f"{'PASS' if index['checks']['all_indicator_bounds'] else 'FAIL'} |",
        f"| Сумма весов = 1 | "
        f"{'PASS' if index['checks']['all_weights_normalised'] else 'FAIL'} |",
        f"| Премия >= 0 | "
        f"{'PASS' if index['checks']['all_premiums_non_negative'] else 'FAIL'} |",
        f"| Инвестиции в [0, 1] | "
        f"{'PASS' if index['checks']['all_investment_bounds'] else 'FAIL'} |",
        f"| Нет сырых данных | {'PASS' if index['checks']['all_no_raw_data'] else 'FAIL'} |",
        "",
    ]
    lines += _render_profile(index["profile"])
    lines += _render_parameters(index["parameters"])
    lines += ["## Допущения", ""]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in index["assumptions"]]
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in index["limitations"]]
    lines += [
        "",
        "## Что можно и чего нельзя утверждать",
        "",
        "Можно: «при таких допущениях сдвиг шести индикаторов на N пунктов даёт такую-то "
        "премию и такое-то изменение инвестиций».",
        "",
        "Нельзя: «институты страны X такие-то», «премия вырастет на N б.п. в реальности», "
        "«реформа даст рост» — данных нет, вероятности не оценивались, причинность не "
        "устанавливалась.",
        "",
        "## Воспроизведение",
        "",
        "```powershell",
        "python tools/institutions_scenarios.py --out build/institutions",
        "python tools/institutions_scenarios.py --demo",
        "```",
        "",
        f"digest: `{index['digest']}`",
        "",
    ]
    return "\n".join(lines)


def render_demo_markdown(demo: dict) -> str:
    lines = [
        "# Демо: четыре иллюстративных профиля × четыре сценария",
        "",
        _status_line(demo),
        "",
        "Профили `EXAMPLE-*` — геометрические примеры, **не страны** и не измерения. "
        "Таблица показывает только механику оверлея.",
        "",
        "## Δ качества, пункты 0–100",
        "",
        "| Профиль | База | " + " | ".join(entry["id"] for entry in demo["scenarios"]) + " |",
        "|---|---|" + "---|" * len(demo["scenarios"]),
    ]
    for row in demo["profiles"]:
        cells = " | ".join(_signed(row["headline"][entry["id"]]["delta_score_points"])
                           for entry in demo["scenarios"])
        lines.append(f"| `{row['name']}` | {_num(row['score'], 2)} | {cells} |")
    lines += [
        "",
        "## Δ премии за риск, б.п.",
        "",
        "| Профиль | " + " | ".join(entry["id"] for entry in demo["scenarios"]) + " |",
        "|---|" + "---|" * len(demo["scenarios"]),
    ]
    for row in demo["profiles"]:
        cells = " | ".join(_signed(row["headline"][entry["id"]]["delta_risk_premium_bps"], 1)
                           for entry in demo["scenarios"])
        lines.append(f"| `{row['name']}` | {cells} |")
    lines += [
        "",
        "## Δ инвестиций, п.п. ВВП",
        "",
        "| Профиль | " + " | ".join(entry["id"] for entry in demo["scenarios"]) + " |",
        "|---|" + "---|" * len(demo["scenarios"]),
    ]
    for row in demo["profiles"]:
        cells = " | ".join(_signed(row["headline"][entry["id"]]["delta_investment_pp"], 3)
                           for entry in demo["scenarios"])
        lines.append(f"| `{row['name']}` | {cells} |")
    lines += [
        "",
        "Наблюдения (механика, не факты):",
        "",
        "- у `STATUS-QUO` все Δ нулевые — это контрольная линия, а не «нет эффекта в реальности»;",
        "- у `REFORM` композит растёт, премия падает, инвестиции и рост растут; у "
        "`DETERIORATION` — наоборот;",
        "- у `POLARIZATION` потеря композита меньше, чем у `DETERIORATION`, но премия "
        "сопоставима: к сдвигу уровня добавляется премия за рост разброса между индикаторами "
        "(допущение A7);",
        "- одинаковый сценарий даёт разные премии у разных профилей — не потому, что профили "
        "«лучше» или «хуже», а потому что различается база и изменение разброса; это и есть "
        "механизм «одинаковая политика — разный результат» (docs/181 §7), но без данных он "
        "остаётся иллюстрацией;",
        "- порядок величин зависит от эластичностей (A4–A5), а не от измерений; смена "
        "эластичности меняет числа, не меняя знаки.",
        "",
        "Ограничения:",
        "",
    ]
    lines += [f"- {item}" for item in demo["limitations"]]
    lines += ["", f"digest: `{demo['digest']}`", ""]
    return "\n".join(lines)


def render_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


# ------------------------------------------------------------------------------------------- files
def write_outputs(index: dict, reports: list[dict], out_dir, demo: dict | None = None) -> dict:
    """Write every artefact deterministically (UTF-8, LF); no timers, no wall clock."""
    target = Path(out_dir)
    reports_dir = target / "reports"
    paths: dict = {"reports": {}}
    for report in reports:
        scenario_id = report["scenario"]["scenario_id"]
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / f"{scenario_id}.json"
        md_path = reports_dir / f"{scenario_id}.md"
        json_path.write_text(render_json(report), encoding="utf-8", newline="\n")
        md_path.write_text(render_scenario_markdown(report), encoding="utf-8", newline="\n")
        paths["reports"][scenario_id] = {"json": str(json_path), "md": str(md_path)}
    target.mkdir(parents=True, exist_ok=True)
    report_json = target / "report.json"
    report_md = target / "report.md"
    payload = {
        "schema": REPORT_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "not_calibrated": NOT_CALIBRATED,
        "index": index,
        "scenarios": reports,
    }
    report_json.write_text(render_json(payload), encoding="utf-8", newline="\n")
    report_md.write_text(render_index_markdown(payload), encoding="utf-8", newline="\n")
    paths["report"] = {"json": str(report_json), "md": str(report_md)}
    if demo is not None:
        demo_json = target / "demo.json"
        demo_md = target / "demo.md"
        demo_json.write_text(render_json(demo), encoding="utf-8", newline="\n")
        demo_md.write_text(render_demo_markdown(demo), encoding="utf-8", newline="\n")
        paths["demo"] = {"json": str(demo_json), "md": str(demo_md)}
    return paths


# -------------------------------------------------------------------------------------------- runner
def build_inputs(country: str | None = None, indicators: str | None = None,
                 scenarios: str | None = None, horizon: int | None = None,
                 profile_name: str | None = None):
    """Resolve ``--country`` / ``--indicators`` / ``--scenario`` / ``--horizon`` into inputs."""
    user_scenarios = None
    parameters = None
    if country:
        looks_like_file = country.lower().endswith(".json") or Path(country).is_file()
        if looks_like_file:
            try:
                profile, user_scenarios, parameters = load_bundle(country, name=profile_name)
            except FileNotFoundError as exc:
                raise BlockedInput(str(exc)) from exc
        else:
            profile = find_example_profile(country)
            if profile_name:
                profile = replace(profile, name=profile_name)
    else:
        profile = replace(find_example_profile("EXAMPLE-MID"),
                          name=profile_name or "EXAMPLE-MID")
    if indicators:
        values = parse_indicator_text(indicators)
        base_name = profile_name or f"{profile.name}+INDICATORS"
        profile = InstitutionProfile(
            name=base_name,
            indicators=InstitutionalIndicators(**values),
            weights=profile.weights,
            source="USER_INPUT",
            note="индикаторы переданы через --indicators; числа не проверяются на истинность",
        )
    parameters = OverlayParameters() if parameters is None else parameters
    if horizon is not None:
        parameters = replace(parameters, horizon_years=int(horizon))
    pool = default_scenarios() if user_scenarios is None else user_scenarios
    only = None
    if scenarios:
        only = [item.strip() for item in str(scenarios).split(",") if item.strip()]
        if not only:
            raise InstitutionError("scenario: пустой список сценариев")
    return profile, pool, only, parameters


def build_demo(parameters: OverlayParameters | None = None) -> dict:
    """Run every example profile against every declared scenario (the ``--demo`` matrix)."""
    parameters = OverlayParameters() if parameters is None else parameters
    pool = default_scenarios()
    rows = []
    for profile in example_profiles():
        result = run_all(profile=profile, scenarios=pool, parameters=parameters)
        headline = {entry["id"]: entry["headline"] for entry in result["index"]["scenarios"]}
        rows.append({
            "name": profile.name,
            "source": profile.source,
            "score": result["index"]["profile"]["score"],
            "dispersion": result["index"]["profile"]["dispersion"],
            "headline": headline,
            "digest": result["index"]["digest"],
        })
    demo = {
        "schema": DEMO_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "not_calibrated": NOT_CALIBRATED,
        "scenarios": [{"id": item.scenario_id, "kind": item.kind, "title_ru": item.title_ru}
                      for item in pool],
        "profiles": rows,
        "parameters": parameters.to_dict(),
        "assumptions": [dict(item) for item in ASSUMPTIONS],
        "limitations": block_limitations(),
    }
    demo["digest"] = payload_digest(demo)
    return demo


def run(args) -> dict:
    profile, pool, only, parameters = build_inputs(
        country=getattr(args, "country", None),
        indicators=getattr(args, "indicators", None),
        scenarios=getattr(args, "scenario", None),
        horizon=getattr(args, "horizon", None),
        profile_name=getattr(args, "name", None),
    )
    result = run_all(profile=profile, scenarios=pool, only=only, parameters=parameters)
    demo = None
    if getattr(args, "demo", False):
        demo = build_demo(parameters)
    paths = write_outputs(result["index"], result["reports"], args.out, demo=demo)
    result["paths"] = paths
    result["demo"] = demo
    return result


def _print_summary(result: dict) -> None:
    index = result["index"]
    print(f"Блок 15 «Институты» — статус {index['status']} (исследовательский, не допуск)")
    print(f"  профиль: {index['profile']['name']} (источник {index['profile']['source']}); "
          f"композит {index['profile']['score']:.2f}/100, разброс "
          f"{index['profile']['dispersion']:.2f}")
    print(f"  сценариев: {len(index['scenarios'])}; данные: {index['data_status']}, "
          f"not_calibrated={str(index['not_calibrated']).lower()}")
    for entry in index["scenarios"]:
        headline = entry["headline"]
        print(f"  {entry['id']:<16} Δ качества {headline['delta_score_points']:+7.2f} п.; "
              f"Δ премии {headline['delta_risk_premium_bps']:+8.1f} б.п.; "
              f"Δ инвестиций {headline['delta_investment_pp']:+6.3f} п.п. ВВП; "
              f"Δ роста {headline['delta_growth_pp']:+6.3f} п.п.")
    checks = index["checks"]
    print(f"  проверки: качество {'PASS' if checks['all_score_bounds'] else 'FAIL'}, "
          f"веса {'PASS' if checks['all_weights_normalised'] else 'FAIL'}, "
          f"премия {'PASS' if checks['all_premiums_non_negative'] else 'FAIL'}, "
          f"нет сырых данных {'PASS' if checks['all_no_raw_data'] else 'FAIL'}")
    print(f"  отчёт: {result['paths']['report']['json']}")
    print(f"  сводка: {result['paths']['report']['md']}")
    if result.get("demo"):
        print(f"  демо:  {result['paths']['demo']['md']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Блок 15 «Институты»: сценарии качества управления (docs/203, RESEARCH_ONLY)")
    parser.add_argument("--scenario", default=None,
                        help=("сценарии через запятую: status_quo, reform, deterioration, "
                              "polarization (по умолчанию все четыре)"))
    parser.add_argument("--country", default=None,
                        help=("профиль: EXAMPLE-LOW / EXAMPLE-MID / EXAMPLE-HIGH / "
                              "EXAMPLE-POLARIZED или путь к пользовательскому JSON; "
                              "реальных стран в блоке нет"))
    parser.add_argument("--name", default=None,
                        help="переименовать профиль в отчёте (метка, не страна)")
    parser.add_argument("--indicators", default=None,
                        help=("шесть значений 0–100: voice=..,stability=..,effectiveness=..,"
                              "regulatory=..,rule_of_law=..,corruption=.."))
    parser.add_argument("--horizon", type=int, default=None,
                        help="горизонт в годах (1..60), по умолчанию 5")
    parser.add_argument("--demo", action="store_true",
                        help="прогнать все EXAMPLE-профили × все сценарии (демо-матрица)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="каталог отчётов (build/institutions)")
    parser.add_argument("--json", action="store_true", help="напечатать индекс как JSON")
    return parser


def _configure_output() -> None:
    """Windows consoles default to cp1251: make UTF-8 output non-fatal (never crash on Δ)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
            pass


def main(argv=None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except BlockedInput as exc:
        print(f"BLOCKED_INPUT: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except InstitutionError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.json:
        print(json.dumps(result["index"], ensure_ascii=True, sort_keys=True, indent=2))
    else:
        _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
