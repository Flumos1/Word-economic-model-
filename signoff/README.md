# G4: подпись docs/160

## Файлы

| Файл | Назначение |
|---|---|
| `G4_SIGN_160_form.docx` / `.pdf` | отдельный печатный экземпляр: что подписывается, чек-лист, решение, поля человека |
| `README.md` | эта инструкция |
| `G4_SIGN_160_entry.template.json` | шаблон записи в реестр; **не является подписью** |
| `scanned/` | сюда положить скан/PDF после подписи (не коммитится) |

## Что подписывается

Только внешняя состязательная проверка G2 — [docs/160](../docs/160_Внешняя_проверка_G2.md),
версия 1.0 от 2026-11-16. Пакет: [docs/185](../docs/185_Подпись_docs_160.md).
Это **не** допуск модели, не подтверждение docs/170/V2 и не разрешение на публикацию.

Пины (LF-нормализация):

- docs/160: `5579a34c87f0dcc728058454bd46aa9132c8121197e78aeea515b6128fa210d2`
- docs/185: `c4d275c330d55e17a18ec8d5d81341185845ffbdc53a0e5ae0ed7802cc7c99e3`

## Порядок

1. Человек читает docs/160 и docs/185, проходит чек-лист в форме.
2. Проверяет хеши (PowerShell):
```powershell
python -c "import hashlib,pathlib;p=pathlib.Path('docs/160_Внешняя_проверка_G2.md');print(hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest())"
python -c "import hashlib,pathlib;p=pathlib.Path('docs/185_Подпись_docs_160.md');print(hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest())"
```
3. Выбирает `APPROVE` / `APPROVE_WITH_NOTES` / `REJECT`, заполняет ФИО, роль, организацию, дату, подпись.
4. Сканирует/сохраняет PDF в `signoff/scanned/G4_SIGN_160_signed.pdf` (или передаёт Flumos).
5. Человек вносит в `protocols/signoff_registry.json` (запись `G4-SIGN-160`):
   - `status`: `SIGNED_BY_HUMAN` или `REJECTED_BY_HUMAN`;
   - `signature`: `{name, role, signer_type: "human", decision, scope}`;
   - `date`: `YYYY-MM-DD`;
   - при `APPROVE_WITH_NOTES` — замечания.
6. После этого: `python -m unittest tests.test_signoff_registry` — должно быть OK.
   Если форма/пакет менялись — новая версия и перепин, иначе тест падает по замыслу.

## Важно

- Не редактировать docs/160 и docs/185: их SHA в реестре и тестах.
- Скан — персональные данные: не коммитить; `scanned/` в `.gitignore`.
- Агент не подписывает и не выдумывает ФИО/дату/решение.
- `G4_SIGN_160_entry.template.json` — только заготовка; без подписи человека она недействительна.
