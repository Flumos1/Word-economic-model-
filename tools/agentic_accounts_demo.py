#!/usr/bin/env python3
"""G2 demo: agentic ABM aggregates -> standard SFC sector accounts (offline, deterministic).

Runs the documented simulator scenarios (baseline, income shock, income shock + AI procurement share
0 -> 50%), converts each run into standard SFC sector accounts with
:mod:`amfe.agentic.accounts_bridge`, prints the aggregates/checks and, with ``--json``, emits the whole
result as JSON. Exits ``0`` when every aggregate check passes and ``2`` otherwise (fail-closed).
No network, no wall clock in the output (the simulator is deterministic given its seed).

Usage:
    python tools/agentic_accounts_demo.py
    python tools/agentic_accounts_demo.py --json
    python tools/agentic_accounts_demo.py --json --out build/agentic_accounts.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.agentic.accounts_bridge import build_account_aggregates, render_report  # noqa: E402
from amfe.agentic.simulator import AgenticSimulator, Parameters  # noqa: E402

SCENARIOS = ("baseline", "income_shock", "income_shock_ai")


def scenario_parameters(name: str) -> Parameters:
    """Deterministic parameters for the three documented scenarios."""
    base = Parameters()
    overrides = asdict(base)
    overrides["demo_label"] = name
    if name == "baseline":
        overrides.update(ai_share_start=0.0, ai_share_end=0.0, shock_employment_drop=0.0)
    elif name == "income_shock":
        overrides.update(ai_share_start=0.0, ai_share_end=0.0)
    elif name == "income_shock_ai":
        pass  # defaults: shock at period 8, AI share ramps 0 -> 0.5 between periods 4 and 12
    else:
        raise ValueError(f"unknown scenario: {name!r}")
    return Parameters(**overrides)


def run_scenarios() -> dict:
    """Run the three scenarios and build the SFC aggregates for each one."""
    runs = {}
    for name in SCENARIOS:
        parameters = scenario_parameters(name)
        simulator = AgenticSimulator(parameters)
        result = simulator.run()
        aggregates = build_account_aggregates(result, simulator.ledger)
        runs[name] = aggregates
    return runs


def render_text(runs: dict) -> str:
    lines = ["AMFE G2: ABM -> SFC accounts, demo (offline, deterministic, not a forecast)", ""]
    for name in SCENARIOS:
        aggregates = runs[name]
        lines.append(f"== {name} ==")
        lines.append(render_report(aggregates).rstrip())
        totals = aggregates.totals
        ok = totals["saving"] == totals["investment"] + totals["current_account"]
        lines.append(f"S = I + CA (CA = 0): {'OK' if ok else 'FAIL'} "
                     f"(S={totals['saving']}, I={totals['investment']})")
        lines.append("")
    all_ok = all(all(aggregates.checks.values()) for aggregates in runs.values())
    lines.append(f"all aggregate checks: {'PASS' if all_ok else 'FAIL'}")
    lines.append("Source: src/amfe/agentic/simulator.py -> src/amfe/agentic/accounts_bridge.py. "
                 "Research toy: no forecasts, no admission, no investment advice.")
    return "\n".join(lines) + "\n"


def build_payload(runs: dict) -> dict:
    return {
        "schema": "AMFE-AGENTIC-ACCOUNTS-DEMO/1.0.0",
        "scenarios": list(SCENARIOS),
        "runs": {name: runs[name].as_dict() for name in SCENARIOS},
        "all_checks_pass": all(all(aggregates.checks.values())
                               for aggregates in runs.values()),
        "disclaimer": ("Research toy ABM aggregates, not a forecast, not an admission, "
                       "not investment advice."),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="G2 demo: ABM aggregates -> SFC sector accounts")
    parser.add_argument("--json", action="store_true", help="print the whole result as JSON")
    parser.add_argument("--out", default=None, help="write the JSON payload to this file")
    args = parser.parse_args(argv)
    try:
        runs = run_scenarios()
    except Exception as error:  # fail-closed: bridge invariants are hard requirements
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    payload = build_payload(runs)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(runs), end="")
    return 0 if payload["all_checks_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
