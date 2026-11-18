#!/usr/bin/env python3
"""Generate a ready-to-use onboarding prompt for a fresh subagent of a given AMFE role.

The prompt combines the role charter from ``protocols/agent_roles.json``, the role's recent memory
(``agents/memory/<role>.jsonl``) and the task card skeleton. Use it when a durable teammate session
fails or when a large self-contained task should run in a fresh context.

Usage:
  python agents/tools/role_prompt.py --role r2-completion --task task-54 --title "Scorecard" \
      --scopes tools/external_forecast_scorecard.py tests/test_external_forecast_scorecard.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "protocols" / "agent_roles.json"
MEMORY = ROOT / "agents" / "tools" / "memory.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AMFE subagent onboarding prompt")
    parser.add_argument("--role", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--scopes", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    data = json.loads(ROLES.read_text(encoding="utf-8"))
    role = next((item for item in data["roles"] if item["id"] == args.role), None)
    if role is None:
        print(f"unknown role: {args.role}", file=sys.stderr)
        return 2
    pack = subprocess.run([sys.executable, str(MEMORY), "pack", "--role", args.role,
                           "--limit", str(args.limit)], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=60)
    memory_text = pack.stdout.strip() if pack.returncode == 0 else "(memory unavailable)"
    scopes = args.scopes or [s for s in role.get("owns", []) if s.startswith(("tools/", "src/", "tests/", "docs/"))]
    lines = [
        f"# Роль: {role['title']} ({role['id']})",
        "",
        f"Папка: {ROOT}",
        f"Задача: {args.task}" + (f" — {args.title}" if args.title else ""),
        "",
        "## Мандат",
        role["mandate"],
        "",
        "## Что можно писать",
        ", ".join(f"`{s}`" for s in scopes) if scopes else "(см. карточку задачи)",
        "",
        "## Запрещено",
        "; ".join(role.get("never", [])) + "; frozen-файлы; коммиты (коммитит Lead).",
        "",
        "## Память роли",
        f"Файл: `{role.get('memory')}`; индекс: `agents/library/INDEX.md`.",
        "",
        memory_text,
        "",
        "## Definition of Done",
        "- файлы только в своих scopes; frozen/чужие файлы не тронуты;",
        "- тесты добавлены и зелёные; полная регрессия не сломана;",
        "- docs с методикой, ограничениями и честным статусом (без admission claims);",
        "- provenance: SHA-256/пин артефактов; raw-данные не в git;",
        "- не коммитить; отчёт Lead: файлы, тесты, числа, ограничения, что осталось.",
        "",
        "## После работы",
        f"`python agents/tools/memory.py add --role {role['id']} --task {args.task} --kind artifact "
        f"--title \"...\" --summary \"...\" --link <docs>`",
        "",
        "Ответь Lead коротким отчётом.",
        "",
    ]
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"prompt written: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
