#!/usr/bin/env python3
"""Score and rank oil & gas target accounts for the AMFE pilot (marketing kit).

The input is a curated list (``marketing/target_accounts.json``); the score is a transparent
weighted sum of six 0-5 criteria.  The tool is deterministic, writes JSON/Markdown, and is used by
the marketing plan (docs/191) so the ranking in prose and in the table cannot drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "marketing" / "target_accounts.json"
DEFAULT_OUT = ROOT / "marketing" / "target_accounts.md"
WEIGHTS = {"fit": 0.35, "digital": 0.20, "procurement": 0.15,
           "access": 0.15, "budget": 0.10, "risk": 0.05}
FIELDS = ("id", "name", "segment", "hq", "why", "use_cases",
          "fit", "digital", "procurement", "access", "budget", "risk", "notes")
TIER_A = 4.2
TIER_B = 3.4


def load_accounts(path: Path = DEFAULT_INPUT) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) < 10:
        raise ValueError("target_accounts.json must be a list of at least 10 accounts")
    seen = set()
    for index, row in enumerate(data):
        missing = [field for field in FIELDS if field not in row]
        if missing:
            raise ValueError(f"account {index}: missing {missing}")
        if row["id"] in seen:
            raise ValueError(f"duplicate id {row['id']}")
        seen.add(row["id"])
        for criterion in WEIGHTS:
            value = row[criterion]
            if not isinstance(value, int) or not 0 <= value <= 5:
                raise ValueError(f"{row['id']}.{criterion} must be an int 0..5")
        if not isinstance(row["use_cases"], list) or not row["use_cases"]:
            raise ValueError(f"{row['id']}: use_cases must be a non-empty list")
    return data


def score(row: dict) -> float:
    return round(sum(WEIGHTS[key] * row[key] for key in WEIGHTS), 3)


def tier(value: float) -> str:
    if value >= TIER_A:
        return "A"
    if value >= TIER_B:
        return "B"
    return "C"


def ranked(rows: list[dict]) -> list[dict]:
    enriched = []
    for row in rows:
        item = dict(row)
        item["score"] = score(row)
        item["tier"] = tier(item["score"])
        enriched.append(item)
    return sorted(enriched, key=lambda item: (-item["score"], item["id"]))


def build_report(rows: list[dict], generated_at: str | None = None) -> dict:
    items = ranked(rows)
    counts = {level: sum(1 for item in items if item["tier"] == level) for level in ("A", "B", "C")}
    return {
        "schema": "amfe.marketing.targets.v1",
        "generated_at": generated_at,
        "weights": WEIGHTS,
        "tier_thresholds": {"A": TIER_A, "B": TIER_B},
        "accounts": len(items),
        "tier_counts": counts,
        "top": items[:10],
        "all": items,
        "note": "Scores are expert assumptions for prioritisation, not company ratings or facts.",
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Целевые аккаунты: нефть и газ",
        "",
        f"Всего: **{report['accounts']}**; Tier A/B/C: **{report['tier_counts']['A']}/"
        f"{report['tier_counts']['B']}/{report['tier_counts']['C']}**.",
        "",
        "| # | Компания | Сегмент | Балл | Tier | Основной кейс | Почему |",
        "|---:|---|---|---:|---|---|---|",
    ]
    for index, item in enumerate(report["all"], start=1):
        use = ", ".join(item["use_cases"][:3])
        why = item["why"].replace("|", "/")
        lines.append(f"| {index} | {item['name']} | {item['segment']} | {item['score']} | "
                     f"{item['tier']} | {use} | {why} |")
    lines += [
        "",
        "## Как читать",
        "",
        "- Балл — взвешенная экспертная оценка (fit 0,35; digital 0,20; procurement 0,15; "
        "access 0,15; budget 0,10; risk 0,05), не рейтинг компании.",
        "- Tier A — первый пилот/ABM; B — второй круг; C — стратегический/позже.",
        "- Оценки подлежат проверке на discovery; цифры компаний в открытых источниках могут "
        "устаревать.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score/rank AMFE oil & gas target accounts")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args(argv)

    rows = load_accounts(Path(args.input))
    report = build_report(rows, args.generated_at)
    text = render_markdown(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(json.dumps({"accounts": report["accounts"], "tiers": report["tier_counts"],
                      "top3": [item["id"] for item in report["top"][:3]],
                      "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
