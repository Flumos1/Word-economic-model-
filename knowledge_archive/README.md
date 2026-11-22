# Межпроектная база знаний AMFE (`knowledge_archive/`)

Статус: **RESEARCH_ONLY, admission=false**. Это архив знаний, общих для проектов AMFE: методы,
источники, правовые рамки (указатели, не заключения), факт-паки и шаблоны — с провенансом,
лицензией, доступом и сроком пересмотра. Дата: 2026-11-22. Владелец: роль `archivist`.
Процесс: [`agents/archive/skills.md`](../agents/archive/skills.md). Описание:
[`docs/223`](../docs/223_Архивариус_и_межпроектная_база.md).

**`knowledge_base/` (рыночные знания) и `tools/kb.py` этим архивом не затрагиваются.**

## 1. Что внутри

```
knowledge_archive/
  README.md              # этот файл
  schema.json            # схема записи, namespace-реестр, x-rules
  entries/seed.jsonl     # записи (append-only JSON Lines), сейчас 14
  inbox/                 # входящие записи до валидации и слияния
  inbox/merged/          # обработанные файлы очереди
  context_packs/         # собранные пакеты контекста
  index.json / index.md  # собираются tools/archivist.py index
```

Записей: **14** — 4 метода, 2 карточки источников, 2 правовые рамки, 3 факт-пака, 3 шаблона.
Ни одного `VERIFIED`: в среде нет сети и лицензированных БД, поэтому знания — `HYPOTHESIS`,
`NOT_FETCHED` или `PARTIAL`. Это ограничение метода, а не вывод о мире.

## 2. Схема записи (`amfe.archive.entry.v1`)

| Поле | Смысл |
|---|---|
| `id` | `AR-NNNN`, устойчивый, не меняется при обновлении |
| `namespace` | `global` / `amfe` / `osint` / `legal` / `cases` / `lead` |
| `kind` | `method` / `source` / `legal` / `fact_pack` / `template` |
| `title`, `summary` | короткая формулировка и объяснение своими словами |
| `tags`, `audience` | темы и роли-получатели (`pack --role` фильтрует по `audience`) |
| `evidence_status` | `VERIFIED` / `PARTIAL` / `HYPOTHESIS` / `NOT_FETCHED` |
| `evidence_grade` | `A` / `B` / `C` / `null` — сила источника |
| `provenance` | `origin`, `origin_type`, `collected_by`, `collected_at`, `method`, `retrieved_at`, `sha256` |
| `source_ids` | ссылки на записи `kind=source` этого же архива |
| `licence` | `OPEN` / `ATTRIBUTION` / `PUBLIC_DOMAIN` / `SUBSCRIPTION` / `LICENCE_REQUIRED` / `INTERNAL` / `NONE` |
| `access` | `PUBLIC` / `PROJECT` / `RESTRICTED` |
| `retention` | `TTL` / `PROJECT` / `PERMANENT` / `LEGAL_HOLD` |
| `as_of`, `ttl_days`, `review_due` | свежесть: `review_due = as_of + ttl_days` |
| `jurisdiction` | только для `kind=legal` |
| `version`, `supersedes`, `archived` | история: замена и архив вместо удаления |
| `owner_role`, `notes`, `links`, `created`, `updated` | владелец, причина/блокер, существующие пути, даты |

Правила (полный список — `schema.json` → `x-rules`, 20 пунктов):

1. `review_due` пересчитывается как `as_of + ttl_days`; расхождение — ошибка.
2. `namespace` — только из `x-namespaces`; новый namespace добавляется в схему с владельцем.
3. Провенанс обязателен; `INTERNAL_DOC` origin — существующий путь, `OFFICIAL_LAW`/`OPEN_WEB` —
   https URL без учётных данных.
4. `VERIFIED` требует grade A, `retrieved_at` и первоисточник; `HYPOTHESIS`/`NOT_FETCHED` — grade
   C или null.
5. `NOT_FETCHED` требует `licence NONE`, `retrieved_at=null` и `notes` с причиной и блокером.
6. В `HYPOTHESIS`/`NOT_FETCHED` запрещены цифры с валютой, процентами и масштабом.
7. Запрещены `admitted`/«допущенная модель», секреты, токены, телефоны, e-mail, реквизиты.
8. `RESTRICTED` несовместим с открытой лицензией; `PUBLIC` — с подпиской.
9. `source_ids` — только живые записи `kind=source`; `fact_pack`/`legal` обязаны цитировать.
10. `kind=legal` требует `jurisdiction`; норма не выдумывается.
11. Повтор `(namespace, kind, title)` запрещён: изменение — `supersedes` + `version+1`.
12. `LEGAL_HOLD` запрещает `archived`; `archived` не попадает в пакеты.

## 3. Лицензии, доступ, свежесть

- Лицензия записи не может быть «шире» источника: `NONE` — только «не получено»,
  `SUBSCRIPTION` не превращается в `PUBLIC`.
- `access` работает поверх namespace: `PUBLIC` берут любые проекты, `PROJECT` — свой namespace и
  `global`, `RESTRICTED` — свой namespace и `--allow-restricted`.
- `review_due` пересчитывается валидатором; расхождение — ошибка, не предупреждение.
- Просроченная запись не удаляется: она попадает в `stale` индекса и заменяется новой версией.
- Сырые выгрузки, ключи, персональные данные и контакты в архив не попадают.

## 4. Команды

```powershell
python tools/archivist.py --check                                   # exit 0 / 2
python tools/archivist.py validate --json --today 2026-11-22
python tools/archivist.py search "провенанс" --namespace global
python tools/archivist.py add --kind method --namespace global --title "..." --summary "..." `
    --owner-role archivist --audience all --as-of 2026-11-22 `
    --origin docs/223_Архивариус_и_межпроектная_база.md --origin-type INTERNAL_DOC `
    --collected-by archivist --method MANUAL_READ --retrieved-at 2026-11-22 `
    --tag provenance --link agents/archive/skills.md               # запись в inbox
python tools/archivist.py merge                                    # inbox -> entries, без дублей
python tools/archivist.py index --today 2026-11-22                 # index.json + index.md
python tools/archivist.py pack --project osint --topic "законный сбор" --role osint-scout `
    --today 2026-11-22                                             # context_packs/*.json|.md
python tools/archivist.py export --format md | Select-Object -First 20   # экспорт в stdout
python -m unittest tests.test_archivist
```

Инструмент детерминирован (нет сети, нет часов: дата передаётся явно), fail-closed (любой
дефект — exit 2, файлы не пишутся) и пишет только внутри `knowledge_archive/`.

## 5. Context pack

```powershell
python tools/archivist.py pack --project legal --topic "доказательства" --role legal-claim `
    --today 2026-11-22
```

Пакет содержит: записи (провенанс, evidence, лицензия, свежесть), `excluded` с причинами
(`namespace:*`, `audience:*`, `stale`, `limit`, `archived`), `gaps` (чего нет), `licence_notices`
(что можно делать). Сортировка детерминирована: совпадение темы → свежесть → id. `stale`
исключается по умолчанию, `--include-stale` включает осознанно.

## 6. Границы

- Только законные источники: открытые публикации, раскрытие, регуляторная статистика,
  лицензированные БД с доступом; обход авторизации и платного доступа запрещён.
- Правовые записи — указатели, а не заключения; нормы, сроки и статьи проверяет юрист.
- Персональные данные, контакты и сырые сторонние данные не хранятся.
- Архив не является моделью, прогнозом или инвестиционной рекомендацией: `admission=false`.
- Запись в архив не заменяет запись в памяти роли (`agents/memory/*.jsonl`) и в
  `knowledge_base/`: это разные контуры.
