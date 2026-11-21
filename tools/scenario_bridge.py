"""CLI моста блоков 15/17: институты × технологии → пять макро-каналов (docs/210).

Что делает
----------
* берёт **один профиль** институтов (``EXAMPLE-*`` или пользовательский JSON) и сценарии двух
  исследовательских блоков: ``--institutions-scenario`` (STATUS-QUO / REFORM / DETERIORATION /
  POLARIZATION) и ``--technology-scenario`` (baseline / ai_acceleration / ai_slowdown /
  energy_transition);
* считает оба блока на **одном горизонте** ``--horizon`` и собирает единый отчёт
  ``amfe.world.scenario_bridge.v1``: пять явных каналов (премия в б.п., инвестиции в п.п. ВВП,
  TFP/выпуск/энергия в % к базовой диффузии), разложение канала выпуска, безразмерный индекс с
  весами, проверки, допущения, ограничения и SHA-256;
* пишет ``report.json`` + ``report.md``; с ``--demo`` — ``demo.json`` + ``demo.md`` (контроль,
  чистые эффекты и комбинации); с ``--json`` печатает отчёт и **ничего не пишет**.

Правила
-------
* **Никаких данных.** Ни одного ряда, ни сети, ни валют, ни стран: только два блока на
  объявленных допущениях. ``checks.no_raw_data`` проверяется по всему payload.
* **Только агрегаты.** В отчёте пять чисел на кейс, индекс и его вклады; траектории по годам
  остаются в блоках 15/17, их digest'ы попадают в провенанс.
* **Fail-closed.** Неизвестный сценарий/профиль, битый JSON, горизонт вне [1, 50], неполные или
  нулевые веса — код 1; отсутствующий файл профиля или ошибка записи — код 2. Ничего не пишется.
* **Детерминизм.** Фиксированный порядок кейсов, ключей и округлений; два прогона дают
  байт-идентичные файлы и одинаковый digest.
* **Не прогноз и не допуск.** ``RESEARCH_ONLY``, ``admission=false``, ``gating=false``,
  ``causal_claim=false``; вероятности не оцениваются.

Команды
-------
    python tools/scenario_bridge.py --demo
    python tools/scenario_bridge.py
    python tools/scenario_bridge.py --institutions-scenario reform \\
        --technology-scenario ai_acceleration --horizon 10
    python tools/scenario_bridge.py --profile EXAMPLE-LOW --json
    python tools/scenario_bridge.py --institutions-scenario status_quo,reform \\
        --technology-scenario baseline,energy_transition --out build/scenario_bridge
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from amfe.world import institutions  # noqa: E402
from amfe.world import scenario_bridge as bridge  # noqa: E402
from amfe.world.scenario_bridge import BridgeError  # noqa: E402
from amfe.world.technology import TechnologyError  # noqa: E402

DEFAULT_OUT = ROOT / "build" / "scenario_bridge"
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
REPORT_SCHEMA = bridge.SCHEMA
#: Ошибки обоих блоков считаются невалидным входом (код 1), отсутствие файла — код 2.
INVALID_EXCEPTIONS = (BridgeError, institutions.InstitutionError, TechnologyError)


class BlockedInput(RuntimeError):
    """Обязательный входной файл отсутствует (fail-closed, код 2)."""


# ------------------------------------------------------------------------------------------- вывод
def _num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}".replace("-", "−")


def _signed(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    text = f"{float(value):+.{digits}f}"
    return text.replace("-", "−")


def _status_line(payload: dict) -> str:
    return (f"Статус: **{payload['status']}** — admission=false, gating=false, "
            "causal_claim=false; данные `ASSUMPTION_BASED`, калибровка `NOT_CALIBRATED`. "
            "Числа — арифметика при допущениях, не прогноз, не допуск и не причинная оценка.")


def _channel_table(channels: list[dict]) -> list[str]:
    lines = [
        "| Канал | Единица | Владелец | Вес | Полярность | Опорный масштаб | В индексе |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in channels:
        polarity = ("—" if item["polarity"] == 0 else
                    ("+1" if item["polarity"] > 0 else "−1"))
        lines.append(
            f"| `{item['id']}` — {item['label_ru']} | {item['unit']} | `{item['owner']}` | "
            f"{_num(item['weight'], 2)} | {polarity} | {_num(item['scale'], 2)} | "
            f"{'да' if item['included_in_index'] else 'нет'} |")
    return lines


def _case_table(cases: list[dict]) -> list[str]:
    lines = [
        "| Кейс | Премия, б.п. | Инвестиции, п.п. | TFP, % | Выпуск, % | Энергия, % | Индекс |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        channels = case["channels"]
        lines.append(
            f"| `{case['case_id']}` | {_signed(channels['risk_premium_bps'], 1)} | "
            f"{_signed(channels['investment_delta_pp'], 3)} | "
            f"{_signed(channels['tfp_delta_pct'])} | "
            f"{_signed(channels['output_delta_pct'])} | "
            f"{_signed(channels['energy_demand_delta_pct'])} | "
            f"{_signed(case['index']['value'], 3)} |")
    return lines


def _residual(value) -> str:
    """Невязка аддитивности: ноль печатается как ноль, а не как «−0.000000000001»."""
    number = float(value)
    if abs(number) <= 1e-9:
        return "0"
    return f"{number:+.3e}".replace("-", "−")


def _decomposition_table(cases: list[dict]) -> list[str]:
    lines = [
        "| Кейс | Технологический выпуск, % | Институциональный рост, % | Итого выпуск, % | "
        "Невязка, % |",
        "|---|---|---|---|---|",
    ]
    for case in cases:
        part = case["decomposition"]["output"]
        lines.append(
            f"| `{case['case_id']}` | {_signed(part['technology_output_pct'])} | "
            f"{_signed(part['institutions_growth_pct'])} | {_signed(part['total_pct'])} | "
            f"{_residual(part['residual_pct'])} |")
    return lines


def _focus_case(cases: list[dict]) -> dict:
    """Кейс с максимальным |индекс| (детерминированный tie-break по ``case_id``)."""
    return max(cases, key=lambda case: (abs(case["index"]["value"]), case["case_id"]))


def _contributions_table(case: dict) -> list[str]:
    lines = [
        f"Кейс `{case['case_id']}`: индекс **{_signed(case['index']['value'], 3)}** "
        f"(сумма вкладов = индексу, проверка `additivity_index`).",
        "",
        "| Канал | Значение | Вклад в индекс |",
        "|---|---|---|",
    ]
    for channel in bridge.CHANNEL_IDS:
        lines.append(f"| `{channel}` | {_signed(case['channels'][channel], 3)} | "
                     f"{_signed(case['index']['contributions'][channel], 4)} |")
    lines += [
        "",
        f"Сумма весов в индексе {_num(case['index']['included_weight_sum'], 2)} "
        "(энергия в индекс не входит: полярность 0).",
        "",
    ]
    return lines


def _institutions_table(rows: dict) -> list[str]:
    lines = [
        "| Сценарий 15 | Тип | Рампа, лет | Рампа на H | Δ качества | Премия на H, б.п. | "
        "Δ премии, б.п. | Δ инвестиций, п.п. | Δ роста, п.п. |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows.values():
        lines.append(
            f"| `{row['scenario_id']}` | {row['kind']} | {row['ramp_years']} | "
            f"{_num(row['ramp_at_horizon'], 2)} | {_signed(row['delta_score_points'])} | "
            f"{_num(row['risk_premium_bps'], 1)} | {_signed(row['delta_risk_premium_bps'], 1)} | "
            f"{_signed(row['delta_investment_pp'], 3)} | {_signed(row['delta_growth_pp'], 3)} |")
    return lines


def _technology_table(rows: dict) -> list[str]:
    lines = [
        "| Сценарий 17 | Внедрение | TFP, % (абс.) | Выпуск, % (абс.) | Энергия, % (абс.) | "
        "Энергоёмкость, % (абс.) |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows.values():
        lines.append(
            f"| `{row['scenario_id']}` | {_num(row['adoption'], 3)} | "
            f"{_signed(row['tfp_gain_pct'])} | {_signed(row['output_gain_pct'])} | "
            f"{_signed(row['energy_change_pct'])} | "
            f"{_signed(row['energy_intensity_change_pct'])} |")
    return lines


def _checks_table(payload: dict) -> list[str]:
    labels = {
        "case_count": "Кейсов",
        "all_cases_pass": "Все проверки кейсов",
        "all_channels_declared": "Пять каналов объявлены",
        "weights_sum_to_one": "Сумма весов = 1",
        "horizon_matches_cases": "Горизонт одинаков в блоках",
        "additivity_all_cases": "Аддитивность (индекс и выпуск)",
        "signs_all_cases": "Знаки каналов",
        "no_raw_data": "Нет сырых данных",
        "status_flags": "Статусы RESEARCH_ONLY / admission=false",
        "upstream_checks": "Проверки блока 17",
    }
    lines = ["| Проверка | Статус |", "|---|---|"]
    for key, value in payload["checks"].items():
        if key == "case_count":
            lines.append(f"| {labels[key]} | {value} |")
            continue
        ok = (value is True) if key not in ("gating", "causal_claim") else (value is False)
        lines.append(f"| {labels.get(key, key)} | {'PASS' if ok else 'FAIL'} |")
    failed = [case["case_id"] for case in payload["cases"]
              if not all(value for name, value in case["checks"].items()
                         if name not in ("gating", "causal_claim"))]
    lines += ["", f"Кейсов с проваленной проверкой: {len(failed)}"
              + (f" ({', '.join(failed)})" if failed else "."), ""]
    return lines


def render_markdown(payload: dict) -> str:
    lines = [
        "# Мост блоков 15/17: институты × технологии → пять макро-каналов",
        "",
        _status_line(payload),
        "",
        f"Профиль институтов: `{payload['profile']['name']}` "
        f"(источник `{payload['profile']['source']}`; композит "
        f"{_num(payload['profile']['score'], 2)} из 100, разброс "
        f"{_num(payload['profile']['dispersion'], 2)}). "
        f"Горизонт: **{payload['horizon_years']} лет**, общий для обоих блоков. "
        f"Кейсов: {len(payload['cases'])}.",
        "",
        "Мост не считает новую экономику: он вызывает блок 15 "
        "(`src/amfe/world/institutions.py`) и блок 17 (`src/amfe/world/technology.py`) как есть и "
        "складывает их числа в пять явных каналов. Каналы блока 17 считаются **относительно "
        "объявленной базовой диффузии** `baseline`, каналы блока 15 — как отклонение от "
        "`STATUS-QUO`. Поэтому кейс `STATUS-QUO+baseline` — контрольная линия с пятью нулями.",
        "",
        "## 1. Каналы: единицы, веса, полярности",
        "",
    ]
    lines += _channel_table(payload["channel_universe"])
    lines += [
        "",
        "Индекс: `Σ_c w_c · polarity_c · (channel_c / scale_c)` — безразмерная величина в долях "
        "объявленных опорных масштабов. Веса, полярности и масштабы — конвенция отчёта "
        "(допущения B5–B6), а не оценка важности; энергия в индекс не входит (полярность 0).",
        "",
        "## 2. Кейсы: пять каналов и индекс",
        "",
    ]
    lines += _case_table(payload["cases"])
    lines += [
        "",
        "## 3. Разложение канала выпуска",
        "",
        "`output_delta_pct = 100·(Y_scen/Y_base − 1) + Δgrowth_pp·H`, где первое слагаемое — "
        "growth accounting блока 17, второе — линейное накопление Δ п.п. потенциального роста из "
        "блока 15 (допущение B2). Невязка должна быть нулевой: это проверка аддитивности, а не "
        "оценка ошибки.",
        "",
    ]
    lines += _decomposition_table(payload["cases"])
    lines += ["", "## 4. Индекс: разложение по каналам", ""]
    lines += _contributions_table(_focus_case(payload["cases"]))
    lines += [
        "## 5. Входы: что именно прогнано",
        "",
        f"### Блок 15 «Институты» (docs/203), профиль `{payload['profile']['name']}`",
        "",
        "| Индикатор | Значение |",
        "|---|---|",
    ]
    for key, value in payload["profile"]["indicators"].items():
        lines.append(f"| `{key}` | {_num(value, 1)} |")
    lines += [""]
    lines += _institutions_table(payload["inputs"]["institutions"]["scenarios"])
    lines += [
        "",
        f"Параметры оверлея: β = {_num(payload['inputs']['institutions']['parameters']['beta_bps_per_point'], 2)} б.п. на пункт, "
        f"γ = {_num(payload['inputs']['institutions']['parameters']['gamma_bps_per_point_dispersion'], 2)} б.п. на пункт разброса, "
        f"база премии {_num(payload['inputs']['institutions']['parameters']['base_risk_premium_bps'], 1)} б.п., "
        f"e_inv = {_num(payload['inputs']['institutions']['parameters']['elasticity_investment_pp_per_point'], 3)} п.п., "
        f"e_g = {_num(payload['inputs']['institutions']['parameters']['elasticity_growth_pp_per_point'], 3)} п.п.",
        "",
        "### Блок 17 «Технологии и ИИ» (docs/204), абсолютные приросты за горизонт",
        "",
    ]
    lines += _technology_table(payload["inputs"]["technology"]["scenarios"])
    lines += [
        "",
        f"Отрасли: {', '.join(payload['inputs']['technology']['sectors'])}; digest отчёта блока 17: "
        f"`{payload['inputs']['technology']['digest']}`.",
        "",
        "## 6. Проверки",
        "",
    ]
    lines += _checks_table(payload)
    lines += ["## 7. Допущения", ""]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in payload["assumptions"]]
    lines += ["", "## 8. Ограничения", ""]
    lines += [f"- {item}" for item in payload["limitations"]]
    lines += [
        "",
        "## 9. Что можно и чего нельзя утверждать",
        "",
        "**Можно.** «При допущениях B1–B10 пара (сценарий институтов, сценарий диффузии) даёт "
        "такие-то пять каналов на горизонте H» — с указанием профиля, версии модуля и digest. "
        "«Знак премии противоположен знаку сдвига качества, инвестиции и выпуск идут за "
        "качеством, TFP — за внедрением» — как свойство арифметики, а не факт об экономике. "
        "«Мост не применяет ΔA: коэффициенты MRIO не меняются» — как интеграционный контракт.",
        "",
        "**Нельзя.** «Реформа даст +1,2 % выпуска за 10 лет» — это арифметика при допущении, а "
        "не эффект реформы. «ИИ добавит N % TFP» — это разница двух объявленных наборов "
        "множителей. «Индекс показывает, какой сценарий лучше для страны» — индекс считает "
        "смещение по объявленным полярностям и не является благосостоянием. «Институты страны X "
        "такие-то» — страновой таблицы нет. «Это вероятно» — вероятности не оцениваются.",
        "",
        "## 10. Воспроизведение",
        "",
        "```powershell",
        "python tools/scenario_bridge.py --demo",
        "python tools/scenario_bridge.py",
        "python tools/scenario_bridge.py --institutions-scenario reform "
        "--technology-scenario ai_acceleration --horizon 10",
        "python -m unittest tests.test_scenario_bridge -v",
        "```",
        "",
        f"digest: `{payload['digest']}`",
        "",
    ]
    return "\n".join(lines)


def render_demo_markdown(payload: dict) -> str:
    demo = payload["demo"]
    lines = [
        "# Демо моста 15/17: контроль, чистые эффекты и комбинации",
        "",
        _status_line(payload),
        "",
        f"{demo['title_ru']}. Горизонт **{demo['horizon_years']} лет**; профиль "
        f"`{payload['profile']['name']}` (иллюстративный, не страна).",
        "",
        "## Кейсы",
        "",
    ]
    lines += _case_table(payload["cases"])
    lines += ["", "## Разложение выпуска", ""]
    lines += _decomposition_table(payload["cases"])
    lines += ["", "## Как читать", ""]
    lines += [f"- {item}" for item in demo["notes_ru"]]
    lines += ["", "## Каналы", ""]
    lines += _channel_table(payload["channel_universe"])
    lines += ["", "## Проверки", ""]
    lines += _checks_table(payload)
    lines += ["", "## Допущения", ""]
    lines += [f"- **{item['id']}** ({item['kind']}): {item['statement']}"
              for item in payload["assumptions"]]
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in payload["limitations"]]
    lines += ["", f"digest: `{payload['digest']}`", ""]
    return "\n".join(lines)


def render_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


# ------------------------------------------------------------------------------------------- файлы
def write_outputs(payload: dict, out_dir, demo: bool = False) -> dict:
    """Записать артефакты детерминированно (UTF-8, LF); никаких часов и случайности."""
    target = Path(out_dir)
    if demo:
        json_path = target / "demo.json"
        md_path = target / "demo.md"
        renderer = render_demo_markdown
    else:
        json_path = target / "report.json"
        md_path = target / "report.md"
        renderer = render_markdown
    target.mkdir(parents=True, exist_ok=True)
    json_path.write_text(render_json(payload), encoding="utf-8", newline="\n")
    md_path.write_text(renderer(payload), encoding="utf-8", newline="\n")
    return {
        "json": {"path": str(json_path), "bytes": json_path.stat().st_size,
                 "sha256": hashlib.sha256(json_path.read_bytes()).hexdigest()},
        "md": {"path": str(md_path), "bytes": md_path.stat().st_size,
               "sha256": hashlib.sha256(md_path.read_bytes()).hexdigest()},
    }


def _split(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not items:
        raise BridgeError("ожидается непустой список идентификаторов через запятую")
    return items


def run(args) -> dict:
    """Собрать отчёт по аргументам CLI и записать файлы (кроме ``--json``)."""
    demo = bool(getattr(args, "demo", False))
    horizon = getattr(args, "horizon", None)
    if demo:
        document = bridge.demo_report(bridge.DEMO_HORIZON_YEARS if horizon is None else horizon,
                                      profile=getattr(args, "profile", None))
    else:
        document = bridge.build_bridge(
            profile=getattr(args, "profile", None),
            institution_scenarios=_split(getattr(args, "institutions_scenario", None)),
            technology_scenarios=_split(getattr(args, "technology_scenario", None)),
            horizon_years=bridge.DEFAULT_HORIZON_YEARS if horizon is None else horizon,
        )
    written: dict = {}
    if not getattr(args, "json", False):
        written = write_outputs(document, getattr(args, "out", DEFAULT_OUT), demo=demo)
    return {"report": document, "written": written, "demo": demo}


def _print_summary(result: dict) -> None:
    document = result["report"]
    print(f"Мост блоков 15/17 — статус {document['status']} (исследовательский, не допуск)")
    print(f"  профиль: {document['profile']['name']} "
          f"(источник {document['profile']['source']}); горизонт {document['horizon_years']} лет; "
          f"кейсов: {len(document['cases'])}")
    print("  каналы: премия (б.п.), инвестиции (п.п. ВВП), TFP/выпуск/энергия (% к базовой диффузии)")
    for case in document["cases"]:
        channels = case["channels"]
        print(f"  {case['case_id']:<32} премия {_signed(channels['risk_premium_bps'], 1):>8} | "
              f"инвест. {_signed(channels['investment_delta_pp'], 3):>7} | "
              f"TFP {_signed(channels['tfp_delta_pct']):>7} | "
              f"выпуск {_signed(channels['output_delta_pct']):>7} | "
              f"энергия {_signed(channels['energy_demand_delta_pct']):>7} | "
              f"индекс {_signed(case['index']['value'], 3):>7}")
    summary = document["summary"]
    print(f"  индекс: лучший `{summary['best_case']['case_id']}` "
          f"{_signed(summary['best_case']['index'], 3)}, худший "
          f"`{summary['worst_case']['case_id']}` {_signed(summary['worst_case']['index'], 3)}")
    checks = document["checks"]
    print(f"  проверки: кейсы {'PASS' if checks['all_cases_pass'] else 'FAIL'}, "
          f"аддитивность {'PASS' if checks['additivity_all_cases'] else 'FAIL'}, "
          f"знаки {'PASS' if checks['signs_all_cases'] else 'FAIL'}, "
          f"нет сырых данных {'PASS' if checks['no_raw_data'] else 'FAIL'}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Мост блоков 15/17: институты × технологии → пять макро-каналов "
                     "(RESEARCH_ONLY, без данных, admission=false)"))
    parser.add_argument("--institutions-scenario", default=None,
                        help=("сценарии блока 15 через запятую: status_quo, reform, "
                              "deterioration, polarization (по умолчанию все четыре)"))
    parser.add_argument("--technology-scenario", default=None,
                        help=("сценарии блока 17 через запятую: baseline, ai_acceleration, "
                              "ai_slowdown, energy_transition (по умолчанию все четыре)"))
    parser.add_argument("--profile", default=None,
                        help=("профиль институтов: EXAMPLE-LOW / EXAMPLE-MID / EXAMPLE-HIGH / "
                              "EXAMPLE-POLARIZED или путь к пользовательскому JSON; реальных "
                              "стран в блоках нет"))
    parser.add_argument("--horizon", type=int, default=None,
                        help=f"горизонт в годах, {bridge.MIN_HORIZON_YEARS}.."
                             f"{bridge.MAX_HORIZON_YEARS} (по умолчанию "
                             f"{bridge.DEFAULT_HORIZON_YEARS}, демо {bridge.DEMO_HORIZON_YEARS})")
    parser.add_argument("--demo", action="store_true",
                        help="демо-набор пар: контроль, чистые эффекты и комбинации")
    parser.add_argument("--json", action="store_true",
                        help="напечатать отчёт как JSON и ничего не писать")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="каталог отчётов (build/scenario_bridge)")
    return parser


def _configure_output() -> None:
    """Windows-консоль по умолчанию cp1251: UTF-8 вывод не должен ронять CLI."""
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
    except FileNotFoundError as exc:
        print(f"BLOCKED_INPUT: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except OSError as exc:  # запись — единственный I/O: fail-closed, ничего не дописываем
        print(f"BLOCKED_INPUT (I/O): {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except INVALID_EXCEPTIONS as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.json:
        print(json.dumps(result["report"], ensure_ascii=True, sort_keys=True, indent=2))
    else:
        _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
