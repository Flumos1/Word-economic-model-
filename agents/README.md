# Память агентов AMFE

Каждый исполнитель имеет **свою постоянную память** в `agents/memory/<role>.jsonl`, а
**библиотекарь** (`dense-vintages`, совмещает с продуктами) ведёт общий каталог
`agents/library/`. Память — обычные JSON Lines: их может прочитать **любая модель**, без
специального рантайма.

## Зачем

- агент не теряет контекст между запусками: что делал, какие решения принял, какие артефакты
  создал, какие вопросы остались;
- библиотекарь собирает всё в один индекс: кто что знает, где артефакты, что открыто;
- другой агент/модель может быстро войти в курс: `pack` → готовый контекст;
- решения и уроки не теряются при смене моделей/инструментов.

## Структура

```
agents/
  README.md                 # этот файл
  memory/<role>.jsonl       # append-only память роли (по строке = запись)
  library/index.json        # машинный каталог (библиотекарь)
  library/INDEX.md          # человекочитаемый каталог
  templates/memory_entry.md # шаблон записи
  templates/task_card.md    # шаблон задачи
  tools/memory.py           # add/list/search/index/pack/validate/export
```

## Схема записи (`amfe.agent_memory.v1`)

```json
{
  "schema": "amfe.agent_memory.v1",
  "id": "mem-baseline-models-0004",
  "ts": "2026-11-18T09:00:00Z",
  "role": "baseline-models",
  "task": "task-60",
  "kind": "task|role|decision|lesson|artifact|open_question|status",
  "title": "коротко",
  "summary": "что сделано/чему научились/что важно",
  "artifacts": [{"path": "tools/x.py", "sha256": "..."}],
  "links": ["docs/174_50_кейсов_использования.md"],
  "tags": ["models", "country"],
  "status": "done|open"
}
```

Правила: только текст/пути/SHA; **никаких секретов, ключей, сырых сторонних данных**;
append-only (не редактируем прошлое, новое — новой записью); ID = `mem-<role>-NNNN`.

## Команды

```bash
python agents/tools/memory.py add --role baseline-models --task task-60 --kind lesson \
  --title "..." --summary "..." --link docs/174_50_кейсов_использования.md --tag models
python agents/tools/memory.py list --role baseline-models
python agents/tools/memory.py search "C4"
python agents/tools/memory.py pack --role baseline-models --limit 8   # контекст для любой модели
python agents/tools/memory.py index                                   # библиотекарь
python agents/tools/memory.py validate
python agents/tools/memory.py export --out agents/library/export.jsonl
```

## Роли и память

Все роли из [protocols/agent_roles.json](../protocols/agent_roles.json) имеют поле `memory` с
путём к своему файлу. Библиотекарь — `dense-vintages`: после каждой волны проверяет записи,
убирает дубли, пересобирает индекс и обновляет `INDEX.md`.

## Как подключить любую другую модель

1. Дать модели прочитать `agents/README.md` и `agents/library/INDEX.md`.
2. Выполнить `python agents/tools/memory.py pack --role <role> --limit 8` и вставить результат в
   промпт (или открыть `agents/memory/<role>.jsonl` построчно).
3. После работы — добавить запись через `add`; библиотекарь пересоберёт индекс.

## Безопасность и лицензии

- Секреты/ключи запрещены (валидатор ловит типичные шаблоны).
- Сырые сторонние данные в память и git не пишутся: только агрегаты, пути, SHA-256, ссылки.
- Память — часть репозитория: она проходит те же проверки CI и не содержит admission claims.
