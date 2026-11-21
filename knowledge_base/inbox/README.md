# Inbox — входящие записи базы знаний

Сюда роли кладут новые записи **до** валидации. Владелец: `kb-librarian`.
Процесс: [`agents/market_intelligence/kb_workflow.md`](../../agents/market_intelligence/kb_workflow.md).

## Правила

1. Одна строка JSONL = одна запись `amfe.kb.entry.v1` (см. `../schema.json`).
2. Имя файла: `<id>.jsonl`, где `id` — `KB-NNNN`. Файл не перезаписывается: правка — новая запись
   с `supersedes`.
3. Запись обязана быть валидной до слияния: `python tools/kb.py --check` (inbox входит в проверку).
4. `merge` переносит валидные строки в `entries/seed.jsonl`, дубликаты по `id` пропускает, а
   обработанные файлы переносит в `inbox/merged/` (архив, не удаление) — очередь остаётся пустой,
   и `--check` не видит те же id второй раз.
5. Запрещено: сырые выгрузки, персональные данные, контакты, ключи, цифры без источника,
   формулировки о «допущенной модели».

## Быстрый старт

```powershell
python tools/kb.py add --kind method --title "..." --summary "..." `
    --evidence HYPOTHESIS --grade C --source SRC-INTERNAL-CARD `
    --tags method,evidence --link knowledge_base/schema.json `
    --author-role evidence-qa --as-of 2026-11-21
python tools/kb.py --check
python tools/kb.py merge
```
