#!/usr/bin/env python3
"""Agent memory and library tooling for AMFE.

Every teammate has an append-only memory log ``agents/memory/<role>.jsonl``; the librarian
(``dense-vintages``, dual hat) maintains the catalog ``agents/library/index.json`` and the
human-readable ``agents/library/INDEX.md``.  Records are plain JSON lines so any model can load
them, and ``pack`` produces a small context file for onboarding a role or a task.

Usage:
  python agents/tools/memory.py add --role baseline-models --task task-60 --kind lesson \
      --title "..." --summary "..." --link docs/175... --artifact tools/x.py
  python agents/tools/memory.py list --role baseline-models
  python agents/tools/memory.py search "C4"
  python agents/tools/memory.py index
  python agents/tools/memory.py pack --role baseline-models --limit 5
  python agents/tools/memory.py validate
  python agents/tools/memory.py export --out agents/library/export.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "agents"
MEMORY = AGENTS / "memory"
LIBRARY = AGENTS / "library"
INDEX_JSON = LIBRARY / "index.json"
INDEX_MD = LIBRARY / "INDEX.md"
SCHEMA = "amfe.agent_memory.v1"
KINDS = {"role", "task", "decision", "lesson", "artifact", "open_question", "status"}
SECRET_PATTERNS = re.compile(r"(api[_-]?key|secret|password|bearer\s+[a-z0-9]|sk-[a-z0-9]{8,}|oc_sk_)",
                             re.IGNORECASE)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def role_path(role: str) -> Path:
    return MEMORY / f"{role}.jsonl"


def read_entries(role: str) -> list[dict]:
    path = role_path(role)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def all_entries() -> list[dict]:
    entries = []
    for path in sorted(MEMORY.glob("*.jsonl")):
        entries.extend(read_entries(path.stem))
    return entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk.replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def validate_entry(entry: dict) -> list[str]:
    problems = []
    for field in ("id", "ts", "role", "kind", "title", "summary"):
        if not entry.get(field):
            problems.append(f"missing {field}")
    if entry.get("kind") not in KINDS:
        problems.append(f"unknown kind {entry.get('kind')}")
    if SECRET_PATTERNS.search(json.dumps(entry, ensure_ascii=False)):
        problems.append("possible secret in entry")
    if not entry["id"].startswith(f"mem-{entry.get('role')}-"):
        problems.append("id does not match role")
    return problems


def next_id(role: str) -> str:
    entries = read_entries(role)
    return f"mem-{role}-{len(entries) + 1:04d}"


def add(args) -> int:
    role = args.role
    entries = read_entries(role)
    entry = {
        "schema": SCHEMA,
        "id": next_id(role),
        "ts": now(),
        "role": role,
        "task": args.task or "",
        "kind": args.kind,
        "title": args.title,
        "summary": args.summary,
        "artifacts": [{"path": p, "sha256": sha256_file(ROOT / p) if (ROOT / p).exists() else None}
                      for p in (args.artifact or [])],
        "links": list(args.link or []),
        "tags": list(args.tag or []),
        "status": args.status or ("done" if args.kind != "open_question" else "open"),
    }
    problems = validate_entry(entry)
    if problems:
        print("INVALID: " + "; ".join(problems), file=sys.stderr)
        return 2
    path = role_path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"added {entry['id']} -> {path.relative_to(ROOT)}")
    return 0


def list_entries(args) -> int:
    entries = read_entries(args.role) if args.role else all_entries()
    if args.tag:
        entries = [e for e in entries if args.tag in e.get("tags", [])]
    for entry in entries:
        task = f" [{entry['task']}]" if entry.get("task") else ""
        print(f"{entry['id']} {entry['kind']}{task}: {entry['title']}")
    print(f"total: {len(entries)}")
    return 0


def search(args) -> int:
    needle = args.query.lower()
    hits = [e for e in all_entries()
            if needle in json.dumps(e, ensure_ascii=False).lower()]
    for entry in hits:
        print(f"{entry['id']} ({entry['role']}): {entry['title']} — {entry['summary'][:120]}")
    print(f"matches: {len(hits)}")
    return 0


def build_index() -> dict:
    entries = all_entries()
    by_role: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    artifacts: dict[str, list[str]] = {}
    open_questions = []
    for entry in entries:
        by_role[entry["role"]] = by_role.get(entry["role"], 0) + 1
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        for tag in entry.get("tags", []):
            by_tag[tag] = by_tag.get(tag, 0) + 1
        for artifact in entry.get("artifacts", []):
            if artifact.get("path"):
                artifacts.setdefault(artifact["path"], []).append(entry["id"])
        if entry.get("kind") == "open_question" and entry.get("status") == "open":
            open_questions.append({"id": entry["id"], "title": entry["title"],
                                   "role": entry["role"], "task": entry.get("task", "")})
    return {
        "schema": "amfe.agent_library.v1",
        "updated_at": now(),
        "entries": len(entries),
        "open_questions": open_questions,
        "by_role": dict(sorted(by_role.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "by_tag": dict(sorted(by_tag.items())),
        "artifacts": {path: sorted(ids) for path, ids in sorted(artifacts.items())},
    }


def write_index() -> dict:
    index = build_index()
    LIBRARY.mkdir(parents=True, exist_ok=True)
    INDEX_JSON.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8", newline="\n")
    lines = ["# Библиотека памяти AMFE", "",
             f"Обновлено: {index['updated_at']} · записей: {index['entries']}", "",
             "## По ролям", ""]
    for role, count in index["by_role"].items():
        lines.append(f"- `{role}` — {count} записей (`agents/memory/{role}.jsonl`)")
    lines += ["", "## Открытые вопросы", ""]
    if index["open_questions"]:
        for question in index["open_questions"]:
            lines.append(f"- {question['id']} ({question['role']}): {question['title']}")
    else:
        lines.append("- нет")
    lines += ["", "## Артефакты", ""]
    for path, ids in index["artifacts"].items():
        lines.append(f"- `{path}` ← {', '.join(ids)}")
    lines += ["", "## Как читать любому агенту/модели", "",
              "1. Открыть `agents/README.md` и `agents/library/INDEX.md`.",
              "2. Прочитать `agents/memory/<role>.jsonl` своей роли (JSONL, по строке на запись).",
              "3. Сгенерировать контекст: `python agents/tools/memory.py pack --role <role>`.",
              "4. После задачи добавить запись: `python agents/tools/memory.py add ...`.",
              "5. Библиотекарь (`dense-vintages`) пересобирает индекс: `python agents/tools/memory.py index`.",
              "", "Секреты и сырые данные не пишутся; только пути, SHA-256 и выводы.", ""]
    INDEX_MD.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"index: {index['entries']} entries, {len(index['by_role'])} roles -> agents/library/")
    return index


def pack(args) -> int:
    role = args.role
    entries = read_entries(role)
    if args.task:
        entries = [e for e in entries if e.get("task") == args.task]
    entries = entries[-args.limit:]
    lines = [f"# Контекст роли `{role}`", "",
             f"Записей: {len(entries)} (последние {args.limit})", ""]
    for entry in entries:
        lines.append(f"## {entry['id']} · {entry['kind']} · {entry['title']}")
        if entry.get("task"):
            lines.append(f"Задача: {entry['task']}")
        lines.append(entry["summary"])
        if entry.get("links"):
            lines.append("Ссылки: " + ", ".join(f"`{link}`" for link in entry["links"]))
        if entry.get("artifacts"):
            lines.append("Артефакты: " + ", ".join(f"`{a['path']}`" for a in entry["artifacts"]))
        lines.append("")
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"context written: {args.out}")
    else:
        print(text)
    return 0


def validate_all(args) -> int:
    problems = []
    for entry in all_entries():
        for problem in validate_entry(entry):
            problems.append(f"{entry.get('id')}: {problem}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"OK: {len(all_entries())} entries valid")
    return 0


def export(args) -> int:
    entries = all_entries()
    payload = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries)
    Path(args.out).write_text(payload, encoding="utf-8", newline="\n")
    print(f"exported {len(entries)} entries -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AMFE agent memory/library")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add")
    p.add_argument("--role", required=True)
    p.add_argument("--task", default="")
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
    p.add_argument("--title", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--artifact", action="append")
    p.add_argument("--link", action="append")
    p.add_argument("--tag", action="append")
    p.add_argument("--status", default=None)
    p.set_defaults(func=add)

    p = sub.add_parser("list")
    p.add_argument("--role")
    p.add_argument("--tag")
    p.set_defaults(func=list_entries)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.set_defaults(func=search)

    p = sub.add_parser("index")
    p.set_defaults(func=lambda args: (write_index() and 0))

    p = sub.add_parser("pack")
    p.add_argument("--role", required=True)
    p.add_argument("--task")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--out")
    p.set_defaults(func=pack)

    p = sub.add_parser("validate")
    p.set_defaults(func=validate_all)

    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p.set_defaults(func=export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
