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

## Сети агентов (2026-11-22)

Три взаимодополняющих контура; оркестрирует Lead, каждый контур не проверяет сам себя:

| Контур | Роль / вход | Что даёт | Документ |
|---|---|---|---|
| Market intelligence | `agents/market_intelligence/` | OSINT, gated-доступ по разрешению, факты/числа, кейсы, evidence-QA | [docs/212](../docs/212_Агентная_сеть_market_intelligence.md), [docs/224](../docs/224_Gated_источники.md) |
| Legal counsel | `agents/legal/` — `international-counsel` | международное публичное/частное право, прецеденты, санкции, формат мемо; **не** юридическая консультация | [docs/222](../docs/222_Юрист_международник.md) |
| Archivist | `agents/archive/` + `knowledge_archive/` | провенанс, версии, context pack для других агентов, межпроектная база | [docs/223](../docs/223_Архивариус_и_межпроектная_база.md) |

Правила стыковки:

1. **OSINT → Archivist**: факт-пак с `source_id`, датой и цитатой; архивист проверяет провенанс и
   складывает в namespace `osint`/`amfe`.
2. **Legal ← OSINT/Archivist**: юрист получает источники и факты, но не выдумывает нормы; ссылки —
   только с официальным локатором и `retrieved_at`.
3. **Archivist → все**: `python tools/archivist.py pack --project ... --role ...` собирает контекст;
   агент не копирует весь архив, а получает релевантный пакет.
4. **Lead → человеку**: доступ к gated-источникам (G2/G3), подпись юридического мемо и отправка
   писем — только человек; агенты готовят запрос/черновик.
5. **Запрещено**: обход доступа, утёкшие базы, персональные данные, выдача гипотезы за факт,
   «юридическая консультация» без human-lawyer.

