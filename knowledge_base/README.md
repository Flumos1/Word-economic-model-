# Knowledge base AMFE — market intelligence

Статус: **RESEARCH_ONLY, admission=false**. Локальная база знаний сети market intelligence:
сегменты, источники, шаблоны кейсов, целевые аккаунты и методические записи — с доказательствами,
лицензией и сроком свежести. Дата создания: 2026-11-21. Владелец: роль `kb-librarian`.
Процесс: [`agents/market_intelligence/kb_workflow.md`](../agents/market_intelligence/kb_workflow.md).
Описание: [`docs/213_База_знаний.md`](../docs/213_База_знаний.md).

## 1. Что внутри

```
knowledge_base/
  README.md            # этот файл
  schema.json          # схема записи и источника + x-rules (семантические правила)
  sources.json         # реестр законных источников (id, класс, доступ, лицензия)
  entries/seed.jsonl   # записи (append-only JSON Lines), сейчас 19
  inbox/               # входящие записи до валидации и слияния
  index.json / index.md # собираются tools/kb.py index
```

Записей: **19** — 6 сегментов, 4 источника, 4 шаблона кейсов, 4 целевых аккаунта, 1 метод.
Ни одного `VERIFIED`: в среде нет доступа к сети и лицензированным БД, поэтому все факты о рынке —
`HYPOTHESIS` или `NOT_FETCHED`. Это ограничение метода, а не вывод о рынке.

## 2. Схема записи (`amfe.kb.entry.v1`)

| Поле | Смысл |
|---|---|
| `id` | `KB-NNNN`, устойчивый, не меняется при обновлении |
| `kind` | `segment` / `source` / `case_template` / `target_account` / `method` / `playbook` |
| `title`, `summary` | короткая формулировка и объяснение своими словами |
| `tags` | нижний регистр, `[a-z0-9_-]` |
| `evidence_status` | `VERIFIED` / `PARTIAL` / `HYPOTHESIS` / `NOT_FETCHED` |
| `evidence_grade` | `A` / `B` / `C` / `null` — сила источника |
| `source_ids` | ссылки на `sources.json` |
| `licence` | `OPEN` / `ATTRIBUTION` / `PUBLIC_DOMAIN` / `SUBSCRIPTION` / `LICENCE_REQUIRED` / `INTERNAL` / `NONE` |
| `as_of`, `ttl_days`, `review_due` | свежесть: `review_due = as_of + ttl_days` |
| `links` | относительные пути репозитория; каждый обязан существовать |
| `retrieved_at` | дата фактического получения данных; `null` = не получено |
| `account_id` | только для `kind=target_account`, обязан быть в `marketing/target_accounts.json` |
| `supersedes`, `archived` | замена и архив вместо удаления |

Правила (полный список — `schema.json` → `x-rules`):

1. `grade A` только при `VERIFIED`; `HYPOTHESIS`/`NOT_FETCHED` — только `C` или `null`.
2. `VERIFIED` требует `grade A`, `source_ids`, `retrieved_at`, источника с проверенными условиями
   и доступом не `NOT_AVAILABLE_IN_ENV`.
3. `licence NONE` — только при `NOT_FETCHED`; `NOT_FETCHED` обязан иметь причину и блокер в `notes`.
4. В `HYPOTHESIS`/`NOT_FETCHED` запрещены цифры с валютой, процентами и масштабом (млн/млрд/тыс):
   только формулировки. Число без источника — дефект.
5. Запрещены формулировки `admitted`/«допущенная модель»: сеть `RESEARCH_ONLY`.
6. Повтор `(kind, title)` запрещён: изменение оформляется новой записью с `supersedes`.

## 3. Лицензии и свежесть

- Лицензия записи не может быть «шире» лицензии источника: запись с `OPEN`/`PUBLIC_DOMAIN` не
  подпирается подпиской, `SUBSCRIPTION`/`LICENCE_REQUIRED` источники закрывают запись
  соответствующей лицензией, `INTERNAL` допустим для внутренних гипотез.
- `review_due` пересчитывается валидатором: расхождение — ошибка, а не предупреждение.
- Просроченная запись не удаляется: она попадает в `stale` индекса и заменяется новой записью.
- Сырые выгрузки, ключи, персональные данные и контакты в KB не попадают.

## 4. Источники

`sources.json` — 16 источников: регуляторная статистика, раскрытие эмитентов, лицензированные БД,
внутренняя карточка. У каждого: `class`, `access`, `licence`, `url_checked`, `terms_checked`,
`terms_hash`, `retrieved_at`, `jurisdiction`, `allowed_use`.
Пока `url_checked=false` и `terms_checked=false` у всех: сеть в среде недоступна, проверку делает
человек; `terms_hash` заполняется только после фактической проверки условий.

## 5. Команды

```powershell
python tools/kb.py --check                                  # валидация, exit 0 / 2
python tools/kb.py validate --json                          # машинная сводка
python tools/kb.py search "TCO" --kind case_template        # поиск
python tools/kb.py search "TATNEFT" --json                  # поиск в JSON
python tools/kb.py add --kind segment --title "..." --summary "..." `
    --evidence HYPOTHESIS --grade C --source SRC-EIA `
    --tags segment,gas --link docs/191_Маркетинг_нефтегаз.md `
    --author-role gas-lng --as-of 2026-11-21               # запись в inbox
python tools/kb.py merge                                    # inbox -> entries, без дублей
python tools/kb.py index --today 2026-11-21                 # index.json + index.md
python tools/kb.py export --format json --out .tmp/kb.json  # экспорт (JSON/MD)
python tools/kb.py export --format md                       # экспорт в stdout
python -m unittest tests.test_kb
```

Инструмент детерминирован (нет сети, нет часов: дата передаётся явно), fail-closed (любой дефект —
exit 2, файлы не пишутся), пишет только внутри `knowledge_base/`.

## 6. Границы

- Только законные источники: открытые публикации, отчётность, регуляторная статистика,
  лицензированные БД с доступом. Обход авторизации и платного доступа запрещён.
- Персональные данные, контакты, инвест-рекомендации и прогнозы не хранятся.
- База знаний не заменяет научный контур AMFE (`admission=false`) и не является допущенной моделью.
- Запись в KB не заменяет запись в памяти роли (`agents/memory/*.jsonl`): это разные контуры.
