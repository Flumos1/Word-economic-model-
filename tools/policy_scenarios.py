"""Policy scenarios CLI (docs/177 blocks 11/12; docs/174 UC-04/11/38/39).

Turns the research-only policy block (``amfe.world.policy``) into a deterministic JSON + Markdown
report:

    build/policy/report.json              index + every scenario report (aggregate-only)
    build/policy/report.md                human-readable summary
    build/policy/reports/<scenario>.json  one scenario report
    build/policy/reports/<scenario>.md    one scenario report, readable

The three built-in scenarios are the MVP from docs/177 §3.2 and §5:

* ``RATE-PLUS-100BP``        - policy rate 1 pp above the rule;
* ``DEFICIT-PLUS-2PCT``      - primary balance lower by 2% of GDP;
* ``TARIFF-INFLATION-SHOCK`` - direct inflation impulse 1.5 pp with decay and a negative gap effect.

Design rules
------------
* **No data, no network.**  The baseline is declared in ``PolicyBaseline``; there is no third-party
  file, no download, no clock and no randomness.  A config JSON may override the declared numbers.
* **Identities are checked, not assumed.**  The debt recurrence, the deficit decomposition
  ``D = r*b - pb``, the central-bank balance sheet and the SFC link ``S = I + CA`` are verified after
  every scenario; a failure aborts the run (fail-closed, nothing written).
* **Aggregates only.**  Period paths contain macro aggregates (rate, inflation, gap, debt, deficit);
  never raw series, sector vectors or micro rows.
* **Deterministic.**  Fixed iteration order, sorted JSON keys, LF newlines, no timers: two runs on
  the same inputs produce byte-identical files, and every report carries a SHA-256 digest.
* **Fail-closed.**  A missing ``--config`` is ``BLOCKED_INPUT`` (exit 2); an invalid config, an
  unknown ``--only`` id or a broken identity raises (exit 1).  Nothing is written on failure.
* **Research only.**  ``status=RESEARCH_ONLY``, ``admission=false``, ``gating=false``,
  ``causal_claim=false``: a scenario is not a forecast, not an admission decision and not a causal
  estimate of policy effects.

CLI
---
    python tools/policy_scenarios.py --out build/policy
    python tools/policy_scenarios.py --config policy.json --periods 8 --only RATE-PLUS-100BP
    python tools/policy_scenarios.py --json
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

from amfe.world.policy import (  # noqa: E402
    ADMISSION,
    CAUSAL_CLAIM,
    GATING,
    STATUS,
    STATUS_NOTE,
    PolicyError,
    baseline_from_config,
    default_scenarios,
    run_all,
    scenarios_from_config,
)

DEFAULT_OUT = ROOT / "build" / "policy"
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
REPORT_SCHEMA = "amfe.world.policy.report.v1"


class BlockedInput(RuntimeError):
    """A required input file is missing (fail-closed, exit 2)."""


# ---------------------------------------------------------------------------------------- markdown
def _num(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}".replace("-", "−")


def _signed(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    text = f"{float(value):+.{digits}f}"
    return text.replace("-", "−")


def _pct(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:+.{digits}f}%".replace("-", "−")


_UNIT_LABELS = {"pp": "п.п.", "gdp_share": "доля ВВП"}


def _unit(value: str) -> str:
    return _UNIT_LABELS.get(value, value)


def _render_baseline(baseline: dict) -> list[str]:
    rule = baseline["rule"]
    return [
        "## Базовая линия (объявленные допущения, не данные)",
        "",
        "| Параметр | Значение |",
        "|---|---|",
        f"| Горизонт | {baseline['periods']} лет |",
        f"| Инфляция (цель) | {_num(baseline['inflation_pp'], 2)} п.п. |",
        f"| Разрыв выпуска | {_num(baseline['output_gap_pp'], 2)} п.п. |",
        f"| Потенциальный рост | {_pct(baseline['potential_growth'], 2)} |",
        f"| Первичный баланс | {_pct(baseline['primary_balance'], 2)} ВВП |",
        f"| Долг/ВВП | {_num(baseline['debt_ratio'] * 100, 1)}% |",
        f"| Ставка: r*, phi_pi, phi_y, инерция | {_num(rule['r_star'], 2)}; "
        f"{_num(rule['phi_pi'], 2)}; {_num(rule['phi_y'], 2)}; {_num(rule['inertia'], 2)} |",
        f"| Нейтральная ставка | {_num(rule['neutral_rate'], 2)} п.п. |",
        f"| Принцип Тейлора | {'да' if rule['taylor_principle'] else 'нет'} |",
        f"| S = I + CA | {_num(baseline['savings_ratio'] * 100, 2)}% = "
        f"{_num(baseline['investment_ratio'] * 100, 2)}% + "
        f"({_num(baseline['current_account_ratio'] * 100, 2)}%) |",
        "",
    ]


def render_scenario_markdown(report: dict) -> str:
    scenario = report["scenario"]
    summary = report["summary"]
    sfc = report["sfc"]
    cb = report["central_bank"]
    lines = [
        f"# '{scenario['scenario_id']}': {scenario['title_ru']}",
        "",
        f"Статус: **{report['status']}**, admission=false, gating=false, causal_claim=false. "
        "Сценарий — не прогноз и не причинная оценка.",
        "",
        f"Размер: `{_num(scenario['size'], 4)}` ({_unit(scenario['unit'])}); "
        f"затухание: `{_num(scenario['decay'], 2)}`; {scenario['notes']}",
        "",
        "## Итоговые агрегаты",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Ставка: конечная / Δ к базе | {_num(summary['final_policy_rate_pp'], 2)} / "
        f"{_signed(summary['final_policy_rate_delta_pp'], 2)} п.п. |",
        f"| Ставка: пик Δ | {_signed(summary['policy_rate_max_delta_pp'], 2)} п.п. |",
        f"| Инфляция: конечная / Δ к базе | {_num(summary['final_inflation_pp'], 2)} / "
        f"{_signed(summary['final_inflation_delta_pp'], 2)} п.п. |",
        f"| Инфляция: максимум Δ | {_signed(summary['max_inflation_delta_pp'], 2)} п.п. |",
        f"| Разрыв: конечный / Δ к базе | {_num(summary['final_output_gap_pp'], 2)} / "
        f"{_signed(summary['final_output_gap_delta_pp'], 2)} п.п. |",
        f"| Разрыв: минимум Δ | {_signed(summary['min_output_gap_delta_pp'], 2)} п.п. |",
        f"| Долг/ВВП: конечный / Δ к базе | {_num(summary['final_debt_ratio'] * 100, 1)}% / "
        f"{_signed(summary['final_debt_ratio_delta'] * 100, 1)} п.п. |",
        f"| Дефицит: конечный / Δ к базе | {_pct(summary['final_deficit_ratio'], 2)} / "
        f"{_signed(summary['final_deficit_ratio_delta'] * 100, 2)} п.п. |",
        f"| Сумма дефицитов за горизонт | {_pct(summary['cumulative_deficit_ratio'], 1)} ВВП |",
        f"| r − g (начало) | {_signed(summary['r_minus_g_initial'] * 100, 2)} п.п. |",
        f"| Стабилизирующий первичный баланс | "
        f"{_signed(summary['stabilizing_primary_balance_initial'] * 100, 2)}% ВВП |",
        f"| Тренд долга | {summary['debt_trend']} |",
        "",
        "## Путь (агрегаты по периодам)",
        "",
        "| Период | Ставка | Инфляция | Разрыв | Долг/ВВП | Дефицит/ВВП |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["paths"]:
        lines.append(
            f"| {row['period']} | {_num(row['policy_rate_pp'], 2)} | "
            f"{_num(row['inflation_pp'], 2)} | {_num(row['output_gap_pp'], 2)} | "
            f"{_num(row['debt_ratio'] * 100, 1)}% | {_pct(row['deficit_ratio'], 2)} |")
    checks = report["checks"]
    lines += [
        "",
        "## Проверки (тождества)",
        "",
        "| Проверка | Статус |",
        "|---|---|",
        f"| Долг: b'=(1+r)/(1+g)b−pb | {'PASS' if checks['debt_identity'] else 'FAIL'} |",
        f"| Дефицит: D=r·b−pb | {'PASS' if checks['deficit_identity'] else 'FAIL'} |",
        f"| Баланс ЦБ: assets=liabilities+equity | "
        f"{'PASS' if checks['central_bank_identity'] else 'FAIL'} |",
        f"| SFC: S=I+CA | {'PASS' if checks['sfc_identity'] else 'FAIL'} |",
        f"| Фискальная SFC-связь | {'PASS' if checks['fiscal_sfc_identity'] else 'FAIL'} |",
        "",
        f"Максимальный остаток тождеств: `{summary['debt_identity_max_residual']:.3e}` "
        f"(долг), `{summary['deficit_identity_max_residual']:.3e}` (дефицит).",
        "",
        "## Баланс ЦБ и SFC",
        "",
        f"- Баланс ЦБ: активы {_num(cb['baseline']['assets'], 1)}, ликвидность "
        f"{_num(cb['baseline']['liquidity'], 1)} "
        f"({_pct(cb['baseline']['liquidity_to_gdp'], 2)} ВВП), капитал "
        f"{_num(cb['baseline']['equity'], 1)}.",
        f"- QE-иллюстрация {_pct(cb['qe_illustration']['purchase_to_gdp'], 0)} ВВП: облигации "
        f"{_pct(cb['qe_illustration']['delta_bonds_to_gdp'], 1)} ВВП, резервы "
        f"{_pct(cb['qe_illustration']['delta_reserves_to_gdp'], 1)} ВВП; тождество "
        f"{'PASS' if cb['qe_illustration']['identity_ok'] else 'FAIL'}.",
        f"- SFC: S {_pct(sfc['baseline']['savings'], 1)} = I "
        f"{_pct(sfc['baseline']['investment'], 1)} + CA "
        f"{_pct(sfc['baseline']['current_account'], 1)}; остаток "
        f"`{sfc['baseline']['residual']:.3e}`; S_private (остаток) "
        f"{_pct(sfc['private_saving_ratio'], 1)} ВВП.",
        "",
        "## Допущения и ограничения",
        "",
    ]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in report["assumptions"]]
    lines += ["", "Ограничения:"]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", f"digest: `{report['digest']}`", ""]
    return "\n".join(lines)


def render_index_markdown(payload: dict) -> str:
    index = payload["index"]
    lines = [
        "# Монетарный и фискальный блок: сценарии политики",
        "",
        f"Статус: **{index['status']}**, admission=false, gating=false, causal_claim=false. "
        "Сценарий — не прогноз, не допуск и не причинная оценка.",
        "",
        f"Протокол: `{index['protocol']['file']}` v{index['protocol']['version']} "
        f"({index['protocol']['status']}), admission=false.",
        "",
        "## Сценарии",
        "",
        "| id | Тип | Размер | Δ ставки | Δ инфляции (пик) | Δ разрыва (мин) | Δ долга | Δ дефицита |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for entry in index["scenarios"]:
        headline = entry["headline"]
        lines.append(
            f"| `{entry['id']}` | {entry['kind']} | {_num(entry['size'], 3)} "
            f"{_unit(entry['unit'])} | "
            f"{_signed(headline['final_policy_rate_delta_pp'], 2)} п.п. | "
            f"{_signed(headline['max_inflation_delta_pp'], 2)} п.п. | "
            f"{_signed(headline['min_output_gap_delta_pp'], 2)} п.п. | "
            f"{_signed(headline['final_debt_ratio_delta'] * 100, 1)} п.п. ВВП | "
            f"{_signed(headline['final_deficit_ratio_delta'] * 100, 2)} п.п. ВВП |")
    checks = index["checks"]
    lines += [
        "",
        "## Проверки",
        "",
        "| Проверка | Статус |",
        "|---|---|",
        f"| Сценариев | {checks['scenario_count']} |",
        f"| Тождества долга | {'PASS' if checks['all_debt_identities'] else 'FAIL'} |",
        f"| Тождества дефицита | {'PASS' if checks['all_deficit_identities'] else 'FAIL'} |",
        f"| Баланс ЦБ | {'PASS' if checks['all_central_bank_identities'] else 'FAIL'} |",
        f"| SFC: S=I+CA | {'PASS' if checks['all_sfc_identities'] else 'FAIL'} |",
        f"| Фискальная SFC-связь | {'PASS' if checks['all_fiscal_sfc_identities'] else 'FAIL'} |",
        "",
        f"Максимальный остаток тождеств: `{index['max_identity_residual']:.3e}`.",
        "",
    ]
    lines += _render_baseline(index["baseline"])
    lines += ["## Допущения", ""]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in index["assumptions"]]
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in index["limitations"]]
    lines += ["", "## Воспроизведение", "", "```powershell",
              "python tools/policy_scenarios.py --out build/policy", "```", "",
              f"digest: `{index['digest']}`", ""]
    return "\n".join(lines)


def render_report_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


# ------------------------------------------------------------------------------------------- files
def write_outputs(index: dict, reports: list[dict], out_dir) -> dict:
    """Write every artefact deterministically (UTF-8, LF); no timers, no wall clock."""
    target = Path(out_dir)
    reports_dir = target / "reports"
    paths: dict = {"reports": {}}
    for report in reports:
        scenario_id = report["scenario"]["scenario_id"]
        reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = reports_dir / f"{scenario_id}.json"
        md_path = reports_dir / f"{scenario_id}.md"
        json_path.write_text(render_report_json(report), encoding="utf-8", newline="\n")
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
        "index": index,
        "scenarios": reports,
    }
    report_json.write_text(render_report_json(payload), encoding="utf-8", newline="\n")
    report_md.write_text(render_index_markdown(payload), encoding="utf-8", newline="\n")
    paths["report"] = {"json": str(report_json), "md": str(report_md)}
    return paths


# -------------------------------------------------------------------------------------------- runner
def load_config(path) -> dict:
    """Read a JSON config fail-closed; a missing file is ``BlockedInput`` (exit 2)."""
    target = Path(path)
    if not target.is_file():
        raise BlockedInput(f"конфигурация не найдена: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"конфигурация не читается: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("конфигурация: ожидается JSON-объект")
    return data


def build_inputs(config: dict | None = None, periods: int | None = None):
    """Turn an optional config into a validated baseline and scenario list."""
    data = {} if config is None else dict(config)
    if "baseline" in data:
        baseline_data = data.pop("baseline")
        if not isinstance(baseline_data, dict):
            raise PolicyError("config.baseline: ожидается объект")
        extra = sorted(set(data) - {"scenarios"})
        if extra:
            raise PolicyError(f"config: неизвестные ключи {extra}")
    else:
        baseline_data = {key: value for key, value in data.items() if key != "scenarios"}
    baseline = baseline_from_config(baseline_data)
    if periods is not None:
        baseline = replace(baseline, periods=int(periods))
    raw_scenarios = data.get("scenarios")
    scenarios = default_scenarios() if raw_scenarios is None else scenarios_from_config(raw_scenarios)
    return baseline, scenarios


def run(args) -> dict:
    config = load_config(args.config) if getattr(args, "config", None) else None
    baseline, scenarios = build_inputs(config, getattr(args, "periods", None))
    only = None
    if getattr(args, "only", None):
        only = [item.strip() for item in str(args.only).split(",") if item.strip()]
    result = run_all(baseline=baseline, scenarios=scenarios, only=only)
    paths = write_outputs(result["index"], result["reports"], args.out)
    result["paths"] = paths
    return result


def _print_summary(result: dict) -> None:
    index = result["index"]
    print(f"Политические сценарии — статус {index['status']} (исследовательский, не допуск)")
    print(f"  горизонт: {index['baseline']['periods']} лет; сценариев: {len(index['scenarios'])}")
    for entry in index["scenarios"]:
        headline = entry["headline"]
        print(f"  {entry['id']:<24} ставка {_signed(headline['final_policy_rate_delta_pp'], 2)} п.п.; "
              f"инфляция (пик) {_signed(headline['max_inflation_delta_pp'], 2)} п.п.; "
              f"разрыв (мин) {_signed(headline['min_output_gap_delta_pp'], 2)} п.п.; "
              f"долг {_signed(headline['final_debt_ratio_delta'] * 100, 1)} п.п. ВВП")
    checks = index["checks"]
    print(f"  проверки: долг {'PASS' if checks['all_debt_identities'] else 'FAIL'}, "
          f"дефицит {'PASS' if checks['all_deficit_identities'] else 'FAIL'}, "
          f"ЦБ {'PASS' if checks['all_central_bank_identities'] else 'FAIL'}, "
          f"SFC {'PASS' if checks['all_sfc_identities'] else 'FAIL'}, max остаток "
          f"{index['max_identity_residual']:.3e}")
    print(f"  отчёт: {result['paths']['report']['json']}")
    print(f"  сводка: {result['paths']['report']['md']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сценарии политики: ставка, дефицит, тарифы (docs/177 блоки 11/12)")
    parser.add_argument("--config", default=None,
                        help="JSON с baseline/scenarios (необязательно; по умолчанию — MVP)")
    parser.add_argument("--periods", type=int, default=None,
                        help="переопределить горизонт в годах (1..120)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="каталог отчётов (build/policy)")
    parser.add_argument("--only", default=None, help="прогнать только эти id (через запятую)")
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
    except PolicyError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.json:
        print(json.dumps(result["index"], ensure_ascii=True, sort_keys=True, indent=2))
    else:
        _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
