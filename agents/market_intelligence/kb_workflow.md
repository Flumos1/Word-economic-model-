# Как пополняется база знаний (inbox → validate → merge → index)

Владелец процесса: роль `kb-librarian`. Инструмент: [`tools/kb.py`](../../tools/kb.py)
(единственный путь записи; сеть не используется, часы не читаются).

## 1. Поток

```
роль → inbox/<id>.jsonl → validate → merge → entries/seed.jsonl → index.json / index.md
```

1. **Inbox.** Автор (сегментная роль, `osint-scout`, `licensed-data`, `case-writer`) кладёт запись
   одной строкой JSONL в `knowledge_base/inbox/`. Имя файла — `<id>.jsonl`, где `id` = `KB-NNNN`.
   Запись не редактируется после создания: ошибка исправляется новой записью.
2. **Validate.** `python tools/kb.py --check` (или `validate`): схема, обязательные поля, enum'ы,
   `review_due = as_of + ttl_days`, существование `source_ids` и ссылок, правила grade/статуса,
   отсутствие цифр в `HYPOTHESIS`.
3. **Merge.** `python tools/kb.py merge`: сначала валидирует inbox, затем добавляет валидные строки
   в `entries/seed.jsonl`; дубликат по `id` пропускается (idempotent), а обработанные файлы inbox
   переносятся в `inbox/merged/` — не удаляются, но и не проверяются повторно.
4. **Index.** `python tools/kb.py index --today YYYY-MM-DD`: собирает `index.json` и `index.md` —
   счётчики по `kind`, `evidence_status`, `grade`, `licence`, свежесть (`fresh/due_soon/stale`).

## 2. Минимальная запись

```json
{
  "id": "KB-0021",
  "schema": "amfe.kb.entry.v1",
  "kind": "segment",
  "title": "Переработка: маржа при шоке сырья",
  "summary": "Гипотеза боли CFO/экономики переработки; модуля маржи НПЗ в AMFE нет.",
  "tags": ["segment", "downstream"],
  "evidence_status": "HYPOTHESIS",
  "evidence_grade": "C",
  "source_ids": ["SRC-SEC-EDGAR"],
  "licence": "OPEN",
  "as_of": "2026-11-21",
  "ttl_days": 180,
  "review_due": "2027-05-20",
  "links": ["docs/191_Маркетинг_нефтегаз.md"],
  "created": "2026-11-21",
  "updated": "2026-11-21",
  "author_role": "downstream-chemicals",
  "notes": "Цифры не приводятся: нужен первоисточник."
}
```

Создать такую запись: `python tools/kb.py add --kind segment --title "..." --summary "..." \
--evidence HYPOTHESIS --grade C --source SRC-SEC-EDGAR --tags segment,downstream \
--link docs/191_Маркетинг_нефтегаз.md --author-role downstream-chemicals`.

## 3. Правила приёма

| Проверка | Правило |
|---|---|
| Схема | обязательные поля не пустые, enum'ы из `schema.json` |
| ID | `KB-\d{4}`, уникален, в файле по возрастанию |
| Evidence | `VERIFIED` требует grade A, `source_ids`, `retrieved_at`; `HYPOTHESIS`/`NOT_FETCHED` — grade C или пусто |
| Цифры | в `HYPOTHESIS`/`NOT_FETCHED` нет сумм, процентов и валют — только формулировки |
| Лицензия | совместима с источниками: `NONE`/`UNKNOWN` не подпирает `VERIFIED` |
| Свежесть | `review_due` пересчитан и совпадает; просроченное идёт в отчёт, а не удаляется |
| Ссылки | каждый `links[]` существует в репозитории |
| Источники | каждый `source_ids[]` есть в `sources.json` |
| Данные | нет сырых выгрузок, персональных данных, контактов, ключей |

## 4. Отклонение и архив

- Дефектная запись не попадает в `entries/`: она остаётся в inbox, автор получает список ошибок
  (код, поле, значение).
- Устаревшая запись не удаляется: создаётся новая с `supersedes` и обновлённым `as_of`; в отчёте
  индекса она попадает в `stale`.
- Дубль по `id` — пропуск, а не перезапись. Дубль по содержанию — ручное решение библиотекаря.

## 5. Еженедельный цикл библиотекаря

```powershell
python tools/kb.py --check                       # exit 0 или список дефектов
python tools/kb.py merge                         # inbox → entries, без дублей
python tools/kb.py index --today 2026-11-21      # index.json + index.md
python tools/kb.py search "TCO" --kind case_template --json
python -m unittest tests.test_kb
```

Отчёт Director: сколько принято/отклонено, что просрочено, какие источники нужно продлить, какие
темы без записей (пробелы покрытия).

## 6. Связь с памятью агентов

- `knowledge_base/` — **знания** (факты, источники, шаблоны, лицензии, свежесть).
- `agents/memory/*.jsonl` — **журнал работы** ролей (что делали, какие решения).
- Запись в KB не заменяет запись в память и наоборот; ссылаться друг на друга через `links`.
- Секреты, ключи и сырые сторонние данные запрещены в обоих контурах.
