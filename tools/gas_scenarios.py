"""CLI: gas/LNG scenarios for the AMFE market layer (docs/188, UC-24).

Commands
--------
    python tools/gas_scenarios.py                          # all five scenarios -> build/gas_scenarios
    python tools/gas_scenarios.py --json                   # print the report as JSON, write nothing
    python tools/gas_scenarios.py --scenario hard_winter_6m --scenario soft_winter_6m
    python tools/gas_scenarios.py --param ttf_price_eur_per_mwh=50 --param eur_usd=1.05
    python tools/gas_scenarios.py --quiet                  # write files, print one line

Design rules
------------
* **No data input.**  The tool needs no file, no network and no credential: every number is a declared
  assumption in :mod:`amfe.markets.gas`, so nothing raw can leak into an artifact.
* **Aggregate-only.**  Outputs carry monthly aggregates (paths of at most 12 values), hub quotes,
  balance identities, assumption provenance and checks; never a per-country, per-company or raw
  series.
* **Fail-closed.**  An unknown scenario, an unknown parameter, a malformed ``key=value``, a calibration
  that does not close or a failed balance raises and exits non-zero **before** anything is written.
* **Deterministic.**  No clock, no randomness, no ordering by dict: two runs produce byte-identical
  JSON and Markdown and a stable SHA-256 digest of the report payload.
* **Research only.**  ``status=RESEARCH_ONLY``: a scenario is not a forecast, not an admission and not
  an investment recommendation.
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

from amfe.markets.contract import MarketError  # noqa: E402
from amfe.markets.gas import (  # noqa: E402
    DEFAULT_PARAMS,
    GAS_MODULE,
    build_report,
)

DEFAULT_OUT = ROOT / "build" / "gas_scenarios"
SCHEMA = "amfe.markets.gas_report.v1"
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    return {"path": str(path).replace("\\", "/"), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _typed(name: str, text: str):
    """Convert a CLI value to the type of the declared parameter (fail-closed)."""
    if name not in DEFAULT_PARAMS:
        raise MarketError(f"unknown parameter {name!r}; see --list-parameters")
    reference = DEFAULT_PARAMS[name]
    if isinstance(reference, bool):
        lowered = text.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise MarketError(f"{name} expects a boolean, got {text!r}")
    try:
        if isinstance(reference, int) and not isinstance(reference, bool):
            return int(text)
        return float(text)
    except ValueError:
        raise MarketError(f"{name} expects a numeric value, got {text!r}") from None


def parse_overrides(pairs: list[str] | None) -> dict:
    overrides = {}
    for item in pairs or []:
        if "=" not in item:
            raise MarketError(f"--param expects key=value, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise MarketError(f"--param has an empty key: {item!r}")
        if not value.strip():
            raise MarketError(f"--param {key} has an empty value")
        overrides[key] = _typed(key, value)
    return overrides


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def markdown(document: dict) -> str:
    """Human-readable report: same numbers as the JSON, aggregates only, no timestamps."""
    scenarios = document["scenarios"]
    first = scenarios[0]
    params = first["meta"]["parameters"]
    base_usd = first["summary"]["base_price_usd_per_mmbtu"]
    base_eur = first["summary"]["base_price_eur_per_mwh"]
    lines: list[str] = []
    add = lines.append
    add("# Газовая модель (газ/LNG): сценарии TTF, JKM, Henry Hub")
    add("")
    add(f"Статус: **{document['status']}** — исследовательский прототип, не прогноз, не допуск и не "
        "инвестиционная рекомендация. Модуль: `" + document["module"]["module_id"] + "`.")
    add("")
    add(f"Базовая точка: TTF **{_fmt(base_usd, 2)} USD/MMBtu** ({_fmt(base_eur, 1)} EUR/MWh) при "
        f"EUR/USD {_fmt(float(params['eur_usd']), 2)}; "
        f"JKM {_fmt(first['summary']['jkm_usd_per_mmbtu_path'][0], 2)} USD/MMBtu; "
        f"Henry Hub {_fmt(first['summary']['henry_hub_reference_usd_per_mmbtu'], 2)} USD/MMBtu "
        f"({_fmt(first['summary']['henry_hub_reference_usd_per_mcf'], 3)} USD/Mcf) — ориентир, не "
        "расчётная цена.")
    add("")
    add("## 1. Сценарии: сводка")
    add("")
    add("| Сценарий | TTF пик, USD/MMBtu | TTF пик, EUR/MWh | TTF среднее | TTF минимум | JKM пик | "
        "Диверсия, bcm/год | ПХГ на конец, bcm | Диапазон пика TTF |")
    add("|---|---|---|---|---|---|---|---|---|")
    for item in scenarios:
        summary = item["summary"]
        rng = summary["ranges"]["ttf_peak"]
        diversion = summary["arbitrage"]["diversion"]["diversion_bcm_y"]
        add(f"| `{summary['scenario_label']}` | {_fmt(summary['ttf_peak_usd_per_mmbtu'])} | "
            f"{_fmt(summary['ttf_peak_eur_per_mwh'], 1)} | "
            f"{_fmt(summary['ttf_mean_usd_per_mmbtu'])} | "
            f"{_fmt(summary['ttf_trough_usd_per_mmbtu'])} | "
            f"{_fmt(summary['jkm_peak_usd_per_mmbtu'])} | {_fmt(diversion, 2)} | "
            f"{_fmt(summary['storage']['end_bcm'], 1)} | "
            f"{_fmt(rng['low'])}–{_fmt(rng['high'])} |")
    add("")
    add("## 2. Пути TTF по месяцам, USD/MMBtu")
    add("")
    add("| Сценарий | 1 | 2 | 3 | 4 | 5 | 6 |")
    add("|---|---|---|---|---|---|---|")
    for item in scenarios:
        path = item["summary"]["ttf_usd_per_mmbtu_path"]
        cells = " | ".join(_fmt(value) for value in path)
        add(f"| `{item['summary']['scenario_label']}` | {cells} |")
    add("")
    add("## 3. Балансы и потоки СПГ")
    add("")
    for item in scenarios:
        summary = item["summary"]
        flows = summary["lng_flows"]
        storage = summary["storage"]
        add(f"### `{summary['scenario_label']}`")
        add("")
        add(f"- TTF: пик {_fmt(summary['ttf_peak_usd_per_mmbtu'])} USD/MMBtu "
            f"({_fmt(summary['ttf_peak_eur_per_mwh'], 1)} EUR/MWh), изменение к базе "
            f"{summary['ttf_change_pct_vs_base']:+.1f} %; среднее {_fmt(summary['ttf_mean_usd_per_mmbtu'])} "
            f"({summary['ttf_mean_change_pct_vs_base']:+.1f} %), минимум "
            f"{_fmt(summary['ttf_trough_usd_per_mmbtu'])} ({summary['ttf_trough_change_pct_vs_base']:+.1f} %).")
        add(f"- JKM: пик {_fmt(summary['jkm_peak_usd_per_mmbtu'])} USD/MMBtu "
            f"({summary['jkm_change_pct_vs_base']:+.1f} %), среднее "
            f"{_fmt(summary['jkm_mean_usd_per_mmbtu'])}; спред JKM−TTF по месяцам "
            f"{', '.join(f'{value:+.2f}' for value in summary['jkm_minus_ttf_usd_per_mmbtu_path'])}.")
        add(f"- СПГ: импорт ЕС {_fmt(flows['eu_lng_imports_bcm_y_path'][-1], 1)} bcm/год "
            f"(база {_fmt(float(params['eu_lng_imports_bcm_y']), 1)}), "
            f"Азия {_fmt(flows['asia_lng_imports_bcm_y_path'][-1], 1)} bcm/год "
            f"(база {_fmt(float(params['asia_lng_supply_bcm_y']), 1)}), мир "
            f"{_fmt(flows['global_lng_bcm_y_path'][-1], 1)} bcm/год (объявлено "
            f"{_fmt(flows['global_lng_supply_bcm_y'], 1)}); диверсия "
            f"{_fmt(flows['diversion_bcm_y'], 2)} bcm/год = "
            f"{_fmt(flows['diversion_share_of_global_pct'], 2)} % мировой торговли СПГ.")
        arbitrage_line = (
            f"- Арбитраж: полоса фрахт+регазификация "
            f"{_fmt(summary['arbitrage']['band_usd_per_mmbtu'])} USD/MMBtu; режим "
            f"`{summary['arbitrage']['diversion']['binding']}`"
            + (f", невязка {_fmt(float(summary['arbitrage']['diversion']['residual']), 4)} "
               f"USD/MMBtu"
               if summary["arbitrage"]["diversion"]["residual"] is not None
               else ", переток не требуется (нет арбитражного окна)")
            + "."
            + (" При полной мощности "
               f"({_fmt(summary['arbitrage']['capacity_case']['diversion_bcm_y'], 0)} bcm/год) "
               f"JKM−TTF = "
               f"{_fmt(summary['arbitrage']['capacity_case']['jkm_minus_ttf_usd_per_mmbtu'])} "
               "USD/MMBtu, в полосу не попадает — поток шёл бы обратно."
               if summary["arbitrage"]["capacity_case"] else ""))
        add(arbitrage_line)
        add(f"- ПХГ: старт {_fmt(storage['start_bcm'], 1)} bcm, конец {_fmt(storage['end_bcm'], 1)} bcm "
            f"({_fmt(storage['end_share_of_capacity'], 1)} % ёмкости), изъято "
            f"{_fmt(storage['drawn_bcm'], 1)} bcm; правило: {_fmt(100 * storage['cover_share'], 0)} % "
            f"разрыва, лимит {_fmt(storage['max_draw_bcm_day'], 2)} bcm/сут, минимум "
            f"{_fmt(100 * storage['min_share'], 0)} % ёмкости.")
        add(f"- Диапазон пика TTF: {_fmt(summary['ranges']['ttf_peak']['low'])}–"
            f"{_fmt(summary['ranges']['ttf_peak']['high'])} USD/MMBtu (оболочка вариантов "
            f"чувствительности, не доверительный интервал).")
        add("")
    add("## 4. Допущения: чем заменить")
    add("")
    replace_with = first["meta"]["replace_with"]
    add(f"Всего допущений: **{len(first['assumptions'])}** (классы: "
        f"{', '.join(sorted({item['source_class'] for item in first['assumptions']}))}); "
        f"подсказок replace-with: **{len(replace_with)}**.")
    add("")
    add("| Параметр | Чем заменить |")
    add("|---|---|")
    for item in replace_with:
        add(f"| `{item['name']}` | {item['source']} |")
    add("")
    add("## 5. Проверки")
    add("")
    add("| Сценарий | Проверки | Не пройдено |")
    add("|---|---|---|")
    for item in scenarios:
        failed = [key for key, value in item["checks"].items() if not value]
        add(f"| `{item['summary']['scenario_label']}` | {len(item['checks'])} | "
            f"{', '.join(failed) if failed else '—'} |")
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
    overrides = parse_overrides(args.param)
    scenario_ids = list(args.scenario) if args.scenario else None
    document = build_report(scenario_ids, overrides)
    written = {}
    if not args.json:
        out_dir = Path(args.out)
        written["report_json"] = _write(
            out_dir / "report.json",
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["report_md"] = _write(out_dir / "report.md", markdown(document))
    return {"report": document, "written": written}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gas/LNG scenarios on the AMFE market module (research-only, no admission, "
                    "no data input)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--scenario", action="append", default=None,
                        help="scenario id (repeatable); default: all declared scenarios")
    parser.add_argument("--param", action="append", default=None,
                        help="parameter override key=value (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON, write nothing")
    parser.add_argument("--quiet", action="store_true", help="write files, print one line")
    parser.add_argument("--list-scenarios", action="store_true", help="print scenario ids and exit")
    parser.add_argument("--list-parameters", action="store_true",
                        help="print the declared parameters and their defaults and exit")
    args = parser.parse_args(argv)
    if args.list_scenarios:
        for scenario in GAS_MODULE.scenarios():
            print(f"{scenario.scenario_id}\t{scenario.horizon_months}m\t{scenario.description}")
        return EXIT_OK
    if args.list_parameters:
        for name in sorted(DEFAULT_PARAMS):
            print(f"{name}\t{DEFAULT_PARAMS[name]!r}\t{DEFAULT_PARAMS[name].__class__.__name__}")
        return EXIT_OK
    try:
        result = run(args)
    except MarketError as error:
        print(f"gas scenario error: {error}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as error:  # writing is the only I/O: fail-closed, nothing half-written
        print(f"gas scenario I/O error: {error}", file=sys.stderr)
        return EXIT_BLOCKED
    document = result["report"]
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    if args.quiet:
        peaks = ", ".join(f"{item['summary']['scenario_label']}="
                          f"{item['summary']['ttf_peak_usd_per_mmbtu']:.2f}"
                          for item in document["scenarios"])
        print(f"{document['status']} | TTF пик USD/MMBtu: {peaks} | "
              f"{result['written']['report_md']['path']}")
        return EXIT_OK
    print("Газ/LNG: сценарии (RESEARCH_ONLY, не прогноз и не допуск)")
    print(f"  модуль: {document['module']['module_id']} | сценариев: "
          f"{len(document['scenarios'])} | схема: {SCHEMA}")
    for item in document["scenarios"]:
        summary = item["summary"]
        print(f"  {summary['scenario_label']:<32} TTF пик "
              f"{summary['ttf_peak_usd_per_mmbtu']:6.2f} USD/MMBtu "
              f"({summary['ttf_peak_eur_per_mwh']:5.1f} EUR/MWh, "
              f"{summary['ttf_change_pct_vs_base']:+6.1f} %), среднее "
              f"{summary['ttf_mean_usd_per_mmbtu']:6.2f}, JKM пик "
              f"{summary['jkm_peak_usd_per_mmbtu']:6.2f}, диверсия "
              f"{summary['arbitrage']['diversion']['diversion_bcm_y']:5.2f} bcm/год, ПХГ "
              f"{summary['storage']['end_bcm']:5.1f} bcm, проверки "
              f"{'OK' if all(item['checks'].values()) else 'FAIL'}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
