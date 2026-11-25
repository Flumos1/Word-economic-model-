#!/usr/bin/env python3
"""Scripted 30-minute Simulator demo for M1.

A single offline, fail-closed driver over the existing ``tools/simulator_ui.py`` scenario table. It
encodes a 6-step demo (one step per declared scenario) with a narrative, the exact ``POST /api/run``
payload, and the expected output keys, then (optionally) drives every step **in-process** through
``SimulatorApp.run`` to prove the whole demo holds together without a live server.

It is not a forecast and not an admission: every result must stay ``RESEARCH_ONLY``,
``admission=false``, ``causal_claim=false``; the driver refuses to endorse anything else.

Usage::

    python tools/simulator_demo.py --check             # validate the script against the scenario table
    python tools/simulator_demo.py --run               # drive all 6 scenarios in-process (no server)
    python tools/simulator_demo.py --runbook           # emit the RU/EN markdown runbook to <out>
    python tools/simulator_demo.py --runbook-json      # emit the machine-readable demo script
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulator_ui  # noqa: E402  (the simulator app + scenario table)

SCHEMA = "amfe.simulator.demo.v1"
STATUS = "RESEARCH_ONLY"

# Each step: id, scenario id, minute marker, narrative (RU/EN) and the exact payload + expected keys.
STEPS: tuple[dict, ...] = (
    {
        "step": 1, "minute": "0–3", "scenario": "hormuz_base",
        "title_ru": "Ормуз: базовое закрытие", "title_en": "Hormuz: base closure",
        "narrative_ru": "Показать карточки: пик Brent, бензин, вклад в CPI, расходы домохозяйства. "
                        "Проговорить: это сценарий на объявленных допущениях, не прогноз.",
        "narrative_en": "Show cards: Brent peak, gasoline, CPI contribution, household cost. State it "
                        "is a scenario on declared assumptions, not a forecast.",
        "payload": {"scenario": "hormuz_base", "price": 75, "shock": 100, "country": "US"},
        "expect": ["cards", "prices", "cpi", "risk", "checks", "assumptions", "limitations", "digest"],
    },
    {
        "step": 2, "minute": "3–7", "scenario": "hormuz_stress",
        "title_ru": "Стресс-надбавки", "title_en": "Stress premiums",
        "narrative_ru": "Подвинуть Brent 75 → 95 и поток 100 % → 60 %, затем включить hormuz_stress: "
                        "дизель/авиакеросин, качество сырья, фрахт — где реально растёт себестоимость.",
        "narrative_en": "Move Brent 75 → 95 and flow 100% → 60%, then enable hormuz_stress: diesel/jet, "
                        "crude quality, freight — where cost really rises.",
        "payload": {"scenario": "hormuz_stress", "price": 90, "shock": 100, "country": "DE"},
        "expect": ["cards", "prices", "sectors", "country", "checks", "digest"],
    },
    {
        "step": 3, "minute": "7–12", "scenario": "gas_shock",
        "title_ru": "Газ: холодная зима", "title_en": "Gas: cold winter",
        "narrative_ru": "TTF/JKM, ПХГ, диверсия — это уже энергетика и закупки. Честно: странового CPI "
                        "у газа нет.",
        "narrative_en": "TTF/JKM, storage, diversion — energy and procurement. Honestly: no country CPI "
                        "for gas.",
        "payload": {"scenario": "gas_shock", "price": 50, "shock": 60, "country": "DE"},
        "expect": ["cards", "prices", "risk", "checks", "digest"],
    },
    {
        "step": 4, "minute": "12–18", "scenario": "geopolitics",
        "title_ru": "Геополитика: страна×отрасль", "title_en": "Geopolitics: country × industry",
        "narrative_ru": "Сравнительная статика MRIO: Δвыпуска по странам и отраслям, выгоды/ущерб. "
                        "Это макро-оверлей, без цен и динамики.",
        "narrative_en": "MRIO comparative statics: output deltas by country and industry, gains/losses. "
                        "A macro overlay, no prices or dynamics.",
        "payload": {"scenario": "geopolitics", "country": "US"},
        "expect": ["cards", "country", "sectors", "checks", "digest"],
    },
    {
        "step": 5, "minute": "18–24", "scenario": "institutions",
        "title_ru": "Институты (блок 15)", "title_en": "Institutions (block 15)",
        "narrative_ru": "Композит 0–100 по шести измерениям, премия за риск и её разброс, Δ инвестиций и "
                        "Δ роста. Профили EXAMPLE-* — иллюстративные, страновой таблицы нет.",
        "narrative_en": "Composite 0–100 over six dimensions, risk premium and its dispersion, investment "
                        "and growth deltas. EXAMPLE-* profiles are illustrative; no country table.",
        "payload": {"scenario": "institutions", "inst_scenario": "POLARIZATION", "profile": "EXAMPLE-MID"},
        "expect": ["cards", "checks", "assumptions", "limitations", "digest"],
    },
    {
        "step": 6, "minute": "24–30", "scenario": "technology",
        "title_ru": "Технологии и ИИ (блок 17)", "title_en": "Technology & AI (block 17)",
        "narrative_ru": "Диффузия → TFP → выпуск и энергия; разложение роста и энергоёмкость. Завершить "
                        "общим дисклеймером: сценарии на объявленных допущениях, не прогноз.",
        "narrative_en": "Diffusion → TFP → output and energy; growth decomposition and energy intensity. "
                        "Close with the disclaimer: scenarios on declared assumptions, not a forecast.",
        "payload": {"scenario": "technology", "tech_scenario": "ai_acceleration"},
        "expect": ["cards", "checks", "assumptions", "limitations", "digest"],
    },
)


def runbook_document() -> dict:
    """The machine-readable demo script (validated against the live scenario table)."""
    return {
        "schema": SCHEMA,
        "kind": "simulator_demo_runbook",
        "status": STATUS,
        "admission": False,
        "causal_claim": False,
        "simulator_tool": "tools/simulator_ui.py",
        "simulator_version": simulator_ui.TOOL_VERSION,
        "duration_minutes": 30,
        "steps": [
            {
                "step": s["step"], "minute": s["minute"], "scenario": s["scenario"],
                "title_ru": s["title_ru"], "title_en": s["title_en"],
                "narrative_ru": s["narrative_ru"], "narrative_en": s["narrative_en"],
                "payload": s["payload"], "expect": s["expect"],
            }
            for s in STEPS
        ],
        "disclaimer": (
            "All numbers are scenario arithmetic on declared assumptions, not a forecast, not an "
            "admission decision and not investment advice. No probability or accuracy is claimed."
        ),
    }


def validate_runbook(document: dict, scenario_by_id: dict) -> list[str]:
    errors: list[str] = []
    if document.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if document.get("admission") is not False or document.get("causal_claim") is not False:
        errors.append("demo must stay admission=false and causal_claim=false")
    steps = document.get("steps")
    if not isinstance(steps, list) or len(steps) != 6:
        errors.append("runbook must contain exactly 6 steps")
        return errors
    seen = set()
    for s in steps:
        scenario = s.get("scenario")
        if scenario not in scenario_by_id:
            errors.append(f"step {s.get('step')}: unknown scenario {scenario!r}")
            continue
        if scenario in seen:
            errors.append(f"step {s.get('step')}: duplicate scenario {scenario!r}")
        seen.add(scenario)
        payload = s.get("payload")
        if not isinstance(payload, dict) or payload.get("scenario") != scenario:
            errors.append(f"step {s.get('step')}: payload.scenario must equal the step scenario")
        if not isinstance(s.get("expect"), list) or "digest" not in s["expect"]:
            errors.append(f"step {s.get('step')}: expect must be a list containing 'digest'")
    required = {spec["id"] for spec in simulator_ui.SCENARIOS}
    covered = {s["scenario"] for s in steps}
    if required - covered:
        errors.append(f"runbook does not cover scenarios: {sorted(required - covered)}")
    return sorted(set(errors))


def run_demo(document: dict, app) -> dict:
    """Drive every step in-process; fail-closed on any non-RESEARCH_ONLY or unexpected result."""
    results = []
    for s in document["steps"]:
        result = app.run(s["payload"])
        if result.get("status") != STATUS:
            raise RuntimeError(f"step {s['step']}: status {result.get('status')!r} != RESEARCH_ONLY")
        if result.get("admission") is not False or result.get("causal_claim") is not False:
            raise RuntimeError(f"step {s['step']}: admission/causal_claim must stay false")
        missing = [key for key in s["expect"] if key not in result]
        if missing:
            raise RuntimeError(f"step {s['step']}: missing expected keys {missing}")
        results.append({
            "step": s["step"], "scenario": result["scenario"]["id"], "ok": True,
            "digest": result.get("digest"), "cards": len(result.get("cards", [])),
            "status": result.get("status"), "admission": result.get("admission"),
            "causal_claim": result.get("causal_claim"),
        })
    return {"schema": SCHEMA, "status": STATUS, "steps_run": len(results),
            "steps": results, "all_research_only": True}


def render_runbook(document: dict, lang: str) -> str:
    en = lang == "en"
    lines = [
        f"# {'Scripted 30-minute Simulator demo' if en else 'Сценарный 30-минутный демо-показ Simulator'}",
        "",
        "**RESEARCH_ONLY · admission=false · causal_claim=false · не прогноз / not a forecast**",
        "",
        (document["disclaimer"] if en else
         "Все числа — сценарная арифметика на объявленных допущениях, не прогноз, не допуск и не "
         "инвестиционная рекомендация. Вероятности и точность не заявляются."),
        "",
        "## Steps / Шаги", "",
        "| Step | Time | Scenario | Narrative |",
        "|---|---|---|---|",
    ]
    for s in document["steps"]:
        narrative = s["narrative_en"] if en else s["narrative_ru"]
        title = s["title_en"] if en else s["title_ru"]
        lines.append(f"| {s['step']} | {s['minute']} | `{s['scenario']}` ({title}) | {narrative} |")
    lines += ["", "## API payloads / Запросы", "", "```json"]
    for s in document["steps"]:
        lines.append(f"# step {s['step']}: {s['scenario']}")
        lines.append(json.dumps(s["payload"], ensure_ascii=False, sort_keys=True))
    lines += ["```", "",
              "## Checks / Проверки", "",
              "Each step must return `status=RESEARCH_ONLY`, `admission=false`, `causal_claim=false` "
              "and a `digest`. The driver refuses any other result.",
              "", "```powershell", "python tools/simulator_demo.py --run", "```"]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the script, exit non-zero on error")
    parser.add_argument("--run", action="store_true", help="drive all 6 scenarios in-process")
    parser.add_argument("--runbook-json", action="store_true", help="emit the machine-readable script")
    parser.add_argument("--runbook", action="store_true", help="write RU/EN markdown runbook to --out")
    parser.add_argument("--out", default=None, help="directory for the markdown runbook")
    return parser


def _configure_output() -> None:
    """Force UTF-8 on stdout/stderr so Cyrillic and ``→`` survive a CP1251 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
            pass


def main(argv=None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    document = runbook_document()
    scenario_by_id = {spec["id"]: spec for spec in simulator_ui.SCENARIOS}
    errors = validate_runbook(document, scenario_by_id)
    if errors:
        print("FAIL: " + "; ".join(errors), file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps({"status": "OK", "steps": len(document["steps"]),
                          "scenarios_covered": sorted({s["scenario"] for s in document["steps"]}),
                          "simulator_version": document["simulator_version"]},
                         ensure_ascii=False, sort_keys=True))
        return 0
    if args.runbook_json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.runbook:
        out = Path(args.out) if args.out else ROOT / "build" / "simulator_demo"
        out.mkdir(parents=True, exist_ok=True)
        (out / "runbook.md").write_text(render_runbook(document, "ru"), encoding="utf-8", newline="\n")
        (out / "runbook_en.md").write_text(render_runbook(document, "en"), encoding="utf-8", newline="\n")
        print(f"OK: runbook written to {out}")
        return 0
    if args.run:
        app = simulator_ui.SimulatorApp(geopolitics_mode="synthetic")
        summary = run_demo(document, app)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    print(json.dumps({"status": STATUS, "steps": len(document["steps"]),
                      "usage": "python tools/simulator_demo.py --check|--run|--runbook|--runbook-json"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#
