"""CLI: block 17 (science, technology, AI) scenario prototype — adoption -> TFP -> output/energy.

Commands
--------
    python tools/technology_scenarios.py                       # 4 scenarios -> build/technology_scenarios
    python tools/technology_scenarios.py --json                # print the report as JSON, write nothing
    python tools/technology_scenarios.py --scenario ai_acceleration --sector services --horizon 10
    python tools/technology_scenarios.py --params capital_growth=0.01 --params speed.services=0.4
    python tools/technology_scenarios.py --demo                # baseline vs ai_acceleration, 10 years
    python tools/technology_scenarios.py --out build/tech --quiet

Design rules (the same as the other research tools in this repository)
---------------------------------------------------------------------
* **No data input.**  The tool reads no file, no network and no credential: every number is a
  declared assumption in :mod:`amfe.world.technology`, so nothing raw can leak into an artifact.
* **Aggregate-only.**  Outputs carry paths of at most ``horizon + 1`` points per sector and
  aggregate, assumption provenance, checks and limitations — never a per-country or raw series.
* **Fail-closed.**  An unknown scenario/sector/parameter, a malformed ``key=value``, an
  out-of-range value, a horizon outside ``[1, 50]`` or weights that no longer sum to one raise and
  exit non-zero **before** anything is written.
* **Deterministic.**  No clock, no randomness, no ordering by dict: two runs produce byte-identical
  JSON and Markdown and a stable SHA-256 digest of the report payload.
* **Research only.**  ``status=RESEARCH_ONLY``, ``admission=false``: a scenario is not a forecast,
  not a probability and not an investment recommendation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.world import technology  # noqa: E402
from amfe.world.technology import (  # noqa: E402
    DEFAULT_HORIZON_YEARS,
    GLOBAL_PARAMETERS,
    SECTOR_PARAMETERS,
    SECTORS,
    SECTOR_TITLES_RU,
    SCENARIOS,
    TechnologyError,
)

DEFAULT_OUT = ROOT / "build" / "technology_scenarios"
SCHEMA = technology.SCHEMA_REPORT
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    return {"path": str(path).replace("\\", "/"), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _pct(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f} %"


def markdown(document: dict) -> str:
    """Human-readable report: same numbers as the JSON, aggregates only, no timestamps."""
    scenarios = document["scenarios"]
    horizon = document["horizon_years"]
    growth = document["growth"]
    lines: list[str] = []
    add = lines.append
    add("# Блок 17: технологии и ИИ — диффузия, TFP, выпуск, энергия")
    add("")
    add(f"Статус: **{document['status']}** — исследовательский прототип, не прогноз, не допуск и не "
        "инвестиционная рекомендация. `admission = "
        f"{str(document['admission']).lower()}`, данные: `{document['data_class']}`, калибровка: "
        f"`{document['calibration']}`.")
    add("")
    add(f"Модуль: `{document['module']['module_id']}` (блок {document['block']}), горизонт "
        f"{horizon} лет, отрасли: {', '.join(document['sectors'])}; экзогенные темпы: капитал "
        f"{_pct(100 * float(growth['capital_growth']), 1)}, труд "
        f"{_pct(100 * float(growth['labour_growth']), 1)} в год.")
    add("")
    add("## 1. Сценарии: сводка по агрегату")
    add("")
    add("| Сценарий | Внедрение, доля | TFP, % | Выпуск, % | Энергия, % | Энергоёмкость, % |")
    add("|---|---|---|---|---|---|")
    for item in scenarios:
        summary = item["aggregate"]["horizon"]
        add(f"| `{item['scenario_id']}` | {_fmt(summary['adoption'], 3)} | "
            f"{_pct(summary['tfp_gain_pct'])} | {_pct(summary['output_gain_pct'])} | "
            f"{_pct(summary['energy_change_pct'])} | "
            f"{_pct(summary['energy_intensity_change_pct'])} |")
    add("")
    add("## 2. Отрасли: внедрение и выпуск на горизонте")
    add("")
    add("| Сценарий | Отрасль | Потолок | Внедрение | Δ внедрения | Выпуск, % | Энергия, % |")
    add("|---|---|---|---|---|---|---|")
    for item in scenarios:
        for sector, outcome in item["sectors"].items():
            summary = outcome["horizon"]
            add(f"| `{item['scenario_id']}` | {outcome['title_ru']} | "
                f"{_fmt(outcome['profile']['ceiling_effective'], 3)} | "
                f"{_fmt(summary['adoption'], 3)} | {_fmt(summary['delta_adoption'], 3)} | "
                f"{_pct(summary['output_gain_pct'])} | {_pct(summary['energy_change_pct'])} |")
    add("")
    add("## 3. Growth accounting: разложение прироста выпуска, лог-пункты")
    add("")
    add("`ln(Y_t/Y_0) = ln(A_t/A_0) + α·ln(K_t/K_0) + (1−α)·ln(L_t/L_0)`; вклад внедрения — только "
        "через первую компоненту.")
    add("")
    add("| Сценарий | TFP | Капитал | Труд | Сумма | Невязка |")
    add("|---|---|---|---|---|---|")
    for item in scenarios:
        summary = item["aggregate"]["horizon"]
        contributions = summary["contributions_log"]
        add(f"| `{item['scenario_id']}` | {contributions['tfp']:+.4f} | "
            f"{contributions['capital']:+.4f} | {contributions['labour']:+.4f} | "
            f"{contributions['total']:+.4f} | {summary['identity_residual']:.2e} |")
    add("")
    add("## 4. Допущения: чем заменить")
    add("")
    assumptions = document["assumptions"]
    add(f"Всего допущений: **{len(assumptions)}** (все `source_class=assumption`, наблюдений нет).")
    add("")
    add("| Параметр | Значение | Чем заменить |")
    add("|---|---|---|")
    for item in assumptions:
        add(f"| `{item['name']}` | {item['value']} {item['unit']} | {item['replace_with']} |")
    add("")
    add("## 5. Проверки")
    add("")
    add("| Сценарий | Отрасли | Агрегат |")
    add("|---|---|---|")
    for item in scenarios:
        sectors_ok = all(all(check["checks"].values()) for check in item["sectors"].values())
        aggregate_ok = all(item["aggregate"]["checks"].values())
        add(f"| `{item['scenario_id']}` | {'OK' if sectors_ok else 'FAIL'} | "
            f"{'OK' if aggregate_ok else 'FAIL'} |")
    add("")
    failed = [name for name, value in document["checks"].items()
              if isinstance(value, bool) and not value]
    add(f"Сводные проверки отчёта: **{'все пройдены' if not failed else ', '.join(failed)}**.")
    add("")
    add("## 6. Ограничения")
    add("")
    for note in document["limitations"]:
        add(f"- {note}")
    add("")
    add(f"Дайджест отчёта (SHA-256): `{document['digest']}`")
    add("")
    return "\n".join(lines)


def run(args) -> dict:
    overrides = technology.parse_overrides(args.params)
    scenario_ids = list(args.scenario) if args.scenario else None
    sectors = list(args.sector) if args.sector else None
    horizon = DEFAULT_HORIZON_YEARS if args.horizon is None else args.horizon
    document = technology.build_report(scenario_ids, sectors, horizon, overrides)
    written = {}
    if not args.json:
        out_dir = Path(args.out)
        written["report_json"] = _write(
            out_dir / "report.json",
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["report_md"] = _write(out_dir / "report.md", markdown(document))
    return {"report": document, "written": written}


def run_demo(args) -> dict:
    horizon = technology.DEMO_HORIZON_YEARS if args.horizon is None else args.horizon
    document = technology.demo_report(horizon)
    written = {}
    if not args.json:
        out_dir = Path(args.out) / "demo"
        written["demo_json"] = _write(
            out_dir / "demo.json",
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["demo_md"] = _write(out_dir / "demo.md", markdown(document))
    return {"report": document, "written": written}


def _demo_lines(document: dict) -> list[str]:
    horizon = document["demo"]["horizon_years"]
    lines = [f"Демонстрация блока 17 ({horizon} лет): базовая диффузия против ускорения ИИ "
             f"({document['status']}, admission=false)"]
    for item in document["scenarios"]:
        summary = item["aggregate"]["horizon"]
        lines.append(
            f"  {item['scenario_id']:<16} внедрение {_fmt(summary['adoption'], 3)} | "
            f"TFP {_pct(summary['tfp_gain_pct'])} | выпуск {_pct(summary['output_gain_pct'])} | "
            f"энергия {_pct(summary['energy_change_pct'])} | "
            f"энергоёмкость {_pct(summary['energy_intensity_change_pct'])}")
    base = document["scenarios"][0]["aggregate"]["horizon"]
    fast = document["scenarios"][-1]["aggregate"]["horizon"]
    lines.append(f"  разница ускорения: выпуск {_pct(fast['output_gain_pct'] - base['output_gain_pct'])} "
                 f"к базе, энергоёмкость "
                 f"{_pct(fast['energy_intensity_change_pct'] - base['energy_intensity_change_pct'])}")
    lines.append("  это сценарная арифметика на объявленных допущениях, не прогноз эффекта ИИ")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Block 17 (technology/AI) scenario prototype: logistic adoption -> growth "
                    "accounting -> TFP/output/energy (research-only, no data, no admission)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--scenario", action="append", default=None,
                        help="scenario id (repeatable); default: all four declared scenarios")
    parser.add_argument("--sector", action="append", default=None,
                        help="sector id (repeatable); default: all declared sectors")
    parser.add_argument("--horizon", type=int, default=None,
                        help=f"horizon in years, 1..{technology.MAX_HORIZON_YEARS} "
                             f"(default {DEFAULT_HORIZON_YEARS}, demo "
                             f"{technology.DEMO_HORIZON_YEARS})")
    parser.add_argument("--params", action="append", default=None,
                        help="parameter override key=value (repeatable); global keys or "
                             "<parameter>.<sector>")
    parser.add_argument("--demo", action="store_true",
                        help="run the built-in demonstration (baseline vs ai_acceleration)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON, write nothing")
    parser.add_argument("--quiet", action="store_true", help="write files, print one line")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="print scenario ids and exit")
    parser.add_argument("--list-sectors", action="store_true", help="print sector ids and exit")
    parser.add_argument("--list-parameters", action="store_true",
                        help="print the overridable parameters and exit")
    args = parser.parse_args(argv)
    if args.list_scenarios:
        for scenario in SCENARIOS.values():
            print(f"{scenario.scenario_id}\t{scenario.title_ru}")
        return EXIT_OK
    if args.list_sectors:
        for sector in SECTORS:
            print(f"{sector}\t{SECTOR_TITLES_RU[sector]}")
        return EXIT_OK
    if args.list_parameters:
        for name in sorted(GLOBAL_PARAMETERS):
            print(f"{name}\t{GLOBAL_PARAMETERS[name]}\tglobal")
        for name in sorted(SECTOR_PARAMETERS):
            print(f"{name}.<sector>\t{SECTOR_PARAMETERS[name]}\tsector")
        return EXIT_OK
    try:
        result = run_demo(args) if args.demo else run(args)
    except TechnologyError as error:
        print(f"technology scenario error: {error}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as error:  # writing is the only I/O: fail-closed, nothing half-written
        print(f"technology scenario I/O error: {error}", file=sys.stderr)
        return EXIT_BLOCKED
    document = result["report"]
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    if args.demo:
        for line in _demo_lines(document):
            print(line)
        for name, info in result["written"].items():
            print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
        return EXIT_OK
    if args.quiet:
        aggregate = document["scenarios"][0]["aggregate"]["horizon"]
        print(f"{document['status']} | сценариев {len(document['scenarios'])} | внедрение "
              f"{aggregate['adoption']:.3f} | выпуск {aggregate['output_gain_pct']:+.1f} % | "
              f"{result['written']['report_md']['path']}")
        return EXIT_OK
    print("Блок 17: технологии и ИИ (RESEARCH_ONLY, admission=false, не прогноз)")
    print(f"  модуль: {document['module']['module_id']} | горизонт {document['horizon_years']} лет | "
          f"сценариев: {len(document['scenarios'])} | схема: {SCHEMA}")
    for item in document["scenarios"]:
        summary = item["aggregate"]["horizon"]
        print(f"  {item['scenario_id']:<18} внедрение {summary['adoption']:5.3f} | "
              f"TFP {summary['tfp_gain_pct']:+6.2f} % | выпуск "
              f"{summary['output_gain_pct']:+6.2f} % | энергия "
              f"{summary['energy_change_pct']:+6.2f} % | энергоёмкость "
              f"{summary['energy_intensity_change_pct']:+6.2f} % | проверки "
              f"{'OK' if all(item['checks'].values()) else 'FAIL'}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
