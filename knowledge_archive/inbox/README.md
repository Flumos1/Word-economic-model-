# Очередь архива (`inbox/`)

Сюда попадают записи до валидации и слияния: `tools/archivist.py add` пишет файл
`<id>.jsonl` (одна строка JSON). Вручную файлы не создаются и не правятся.

Порядок:

```powershell
python tools/archivist.py add ...     # запись в inbox/<id>.jsonl
python tools/archivist.py --check     # дефект -> exit 2, запись остаётся здесь
python tools/archivist.py merge       # валидные -> entries/, дубли пропускаются
```

После успешного `merge` обработанные файлы переносятся в `inbox/merged/` (не удаляются).
Очередь после слияния пуста — это нормальное состояние, а не потеря данных.
