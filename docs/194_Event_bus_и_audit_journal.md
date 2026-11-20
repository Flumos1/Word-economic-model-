# 194. Event bus и audit journal (L1)

Дата: 2026-11-20. Автор: трек L1 волны 26 (G6-prep). Статус: **ИНФРАСТРУКТУРНЫЙ ПРОТОТИП,
RESEARCH_ONLY** — локальный журнал событий/аудита; не допуск, не замена существующим журналам.

Основание: [docs/145](145_AMFE_2.0_план.md) (7 уровней, L1 «данные и знания · event bus · audit
journal»), [docs/164](164_Архитектурное_ревью_2.0.md) §2 (L1 «частично»), [docs/173](173_Приоритеты_G6.md)
P1 #15, [docs/121](121_Данные_MRIO.md) (лицензии/данные), [docs/157](157_Реестр_модулей.md) (реестр
модулей).

## 1. Что закрывается

Уровень L1 требовал единого журнала событий/аудита для данных, моделей и продуктов. Раньше факты
жили в разрозненных файлах: `registry/forecasts.jsonl`, `protocols/signoff_registry.json`,
`build/c6/c6_audit.jsonl`, learning registry — каждый со своим форматом, без общей проверки
целостности. Добавлен **append-only журнал** с хеш-цепочкой и **шина событий** с типизированными
kind и единым каноническим `event_id`:

```
src/amfe/data/audit_log.py   — журнал: канонический JSON, event_id, цепочка prev_hash,
                              verify (подмена/разрыв/порядок/усечение), export с manifest,
                              валидация payload/refs, обёртки emit_forecast_event и др.
src/amfe/data/event_bus.py   — Event/EventBus: publish/emit, load, filter, validate, verify,
                              summary, детерминированные ts через инъекцию clock
tools/event_bus.py           — CLI: --emit/--list/--verify/--export/--summary/--demo/--json
```

Существующие журналы **не меняются**: демо только читает их (проверено тестом: хеши источников до/после
совпадают).

## 2. Схема события `AMFE-EVENT/1.0.0`

| Поле | Тип | Смысл |
|---|---|---|
| `schema` | str | `AMFE-EVENT/1.0.0` |
| `seq` | int ≥ 1 | номер события в журнале (непрерывный) |
| `event_id` | hex64 | SHA-256 канонического JSON всех полей, кроме `event_id` |
| `ts` | str | ISO-8601 UTC `YYYY-MM-DDTHH:MM:SSZ` (детерминизм: передаётся caller'ом/clock) |
| `kind` | enum | `DATA_RELEASE` · `MODEL_REGISTERED` · `FORECAST_RECORDED` · `SCENARIO_RUN` · `SIGNOFF` · `PRODUCT_EXPORT` |
| `actor` | str | кто/что отправило (модуль, человек, CLI) |
| `subject` | str | идентификатор субъекта (например `ENSEMBLE_ADAPTIVE:T1_CPI_YOY:2026-11`) |
| `payload` | object | **только агрегаты/хеши**; секреты/сырые данные запрещены (см. §4) |
| `refs` | list | `{path, sha256}` — относительные пути артефактов и их хеши (≤16) |
| `prev_hash` | hex64 | `event_id` предыдущего события; у первого — 64 нуля |
| `correlation_id` | str/null | сквозная связка группы событий |

Канонический JSON: `sort_keys=True`, компактные сепараторы, UTF-8 — одинаковые входы дают одинаковый
`event_id` и одинаковые байты строки. Журнал — JSONL, одна строка = одно событие, дописывание с
`fsync`.

## 3. Проверка целостности

`verify()` пересчитывает всю цепочку и возвращает `ok`, список ошибок с номерами строк и checks:

- `invalid_json` / `invalid_event` — строка не разбирается или нарушает схему;
- `id_mismatch` — `event_id` не соответствует содержимому (подмена payload);
- `chain_broken` — `prev_hash` не совпадает с `event_id` предыдущего события (подмена/перестановка);
- `seq_gap` — нарушена нумерация (удаление/вставка);
- `head_mismatch` / `count_mismatch` — при передаче внешней точки сверки: журнал усечён/переписан;
- checks: `schema_ok`, `ids_ok`, `chain_ok`, `seq_ok`, `kinds_ok`, `payloads_ok`.

Важно: **tamper-evident, не tamper-proof**. Локальный файл можно переписать целиком и пересчитать
цепочку; защита — во внешней сверке `expected_head`/`expected_count` и в экспортированном manifest
(sha256 журнала, count, head) для чекпоинтинга.

## 4. Политика payload (fail-closed)

- Запрещённые ключи (секреты/персональные): `password`, `token`, `api_key`, `private_key`,
  `credential(s)`, `authorization`, `cookie`, `session`, `email`, `phone`, `ssn`, `iban`,
  `card_number`.
- Запрещённые «сырые данные»: `z`, `matrix`, `matrices`, `values`, `rows`, `data`, `raw`, `array`,
  `series`, `timeseries`, `x`, `y`, `va`, `quantiles_5_to_95_step_5` — в журнал идут только
  агрегаты/хеши; полные распределения и матрицы не попадают.
- Лимиты: payload ≤ 8 КиБ, строка ≤ 1024 символов, вложенность ≤ 4, список ≤ 64 элементов,
  refs ≤ 16; только JSON-примитивы (NaN/Inf и объекты сторонних моделей отвергаются).
- `refs`: относительные POSIX-пути без `..`, абсолютные и подозрительные (`secret`, `.env`,
  `id_rsa`, `private`, `token`) отвергаются; `sha256` — 64 lowercase hex.
- Неизвестный `kind` — ошибка (`UnknownKind`); невалидный `ts` — ошибка (`AuditError`).

Обёртки не требуют правок чужих модулей (duck-typed `bus.emit`):

```python
from amfe.data.event_bus import EventBus
from amfe.data.audit_log import emit_forecast_event, emit_scenario_event, emit_export_event

bus = EventBus("build/event_bus/journal.jsonl")
emit_forecast_event(bus, candidate_id="ENSEMBLE_ADAPTIVE", target="T1_CPI_YOY",
                    origin="2026-11", point=3.56, quantiles=quantiles,
                    refs=[{"path": "registry/forecasts.jsonl", "sha256": h}])
emit_scenario_event(bus, scenario_id="hormuz_closed_6m", commodity="Brent",
                    price_low=139.04, price_central=168.89, price_high=268.52,
                    horizon_months=6)
emit_export_event(bus, product="client_pack", format="json", bytes_count=1024, sha256=h)
```

## 5. CLI

```powershell
python tools/event_bus.py --summary                         # сводка журнала
python tools/event_bus.py --emit MODEL_REGISTERED --payload '{"model_id":"m1"}' --actor me
python tools/event_bus.py --emit SCENARIO_RUN --payload @payload.json --ref build/.../report.json
python tools/event_bus.py --list --kind FORECAST_RECORDED --limit 20
python tools/event_bus.py --verify                          # exit 1 при любом дефекте цепочки
python tools/event_bus.py --export build/event_bus/export   # журнал + manifest.json
python tools/event_bus.py --json ...                        # машинный вывод любой команды
```

`--export DIR` кладёт `DIR/journal.jsonl` и `DIR/manifest.json` (`schema amfe.audit.export.v1`,
`count`, `head`, `journal_sha256`, `verify_ok`) — точку внешней сверки.

## 6. Демо на существующих артефактах (read-only)

`python tools/event_bus.py --demo --force` прочитал три реальных артефакта и записал
`build/event_bus/demo.jsonl`:

| Источник (не изменялся) | Событий | Kind |
|---|---|---|
| `registry/forecasts.jsonl` (11 записей) | 11 | `FORECAST_RECORDED` (candidate_id, target, origin, horizon, point, quantiles_count) |
| `protocols/signoff_registry.json` (1 signoff) | 1 | `SIGNOFF` (id, gate, status, review_state, counts, refs на docs/160 и docs/185 с sha256) |
| `build/c6/c6_audit.jsonl` (1 запись) | 1 | `DATA_RELEASE` (week, event, registered, candidates/control rows) |
| **Итого** | **13** | kinds: `DATA_RELEASE 1`, `FORECAST_RECORDED 11`, `SIGNOFF 1` |

- `verify: ok`; head `d70cef2489c76818ba76bf3985420abb3c058bd6dc5f47b3ccc2027df5c68086`.
- Детерминизм: `ts` демо-событий = mtime источника (ISO UTC), поэтому повторный прогон на неизменных
  артефактах даёт те же `event_id` (при `--force`); без `--force` повторный запуск падает (append-only:
  демо-журнал не дописывается молча).
- Источники побайтово не изменились (проверяется тестом).

## 7. Как подключить новый модуль (5 шагов)

1. Выбрать `kind` из AMFE-EVENT/1.0.0 (или расширить словарь в новой версии схемы).
2. Сформировать агрегаты/хеши: без матриц, рядов, полных распределений и секретов.
3. Добавить `refs` на артефакты: относительный путь + `sha256` (можно посчитать `hashlib`).
4. Для тестов передавать явный `ts` (или `clock`) — воспроизводимость и детерминизм.
5. В CI: `bus.verify()` (exit 1 при дефекте) и экспорт manifest для внешней сверки.

## 8. Ограничения (честно)

1. **Локальный JSONL, один писатель**: нет распределённых блокировок и конкурентной записи; при
   нескольких процессах возможны гонки — использовать один процесс-писатель.
2. **Tamper-evident, не tamper-proof**: защита только при внешней сверке head/count/manifest;
   полностью переписанный журнал без внешней точки не отличить.
3. **Нет внешней нотаризации/якорения** (transparency log, подпись) — только хеш-цепочка.
4. **Нет ротации/ретенции** и нет шифрования; политика «не хранить секреты» реализована списком
   запрещённых ключей, а не полноценным DLP (эвристика, возможны пропуски экзотических имён).
5. **Нет аутентификации**: доверие к локальной ФС и процессу; actor — самообъявление.
6. **Демо пока только read-only интеграция**: существующие модули не эмитят события автоматически —
   это следующий шаг (вызовы обёрток в реестрах/подписи/c6), а не сделанное сейчас.
7. Схема `1.0.0` — при расширении kind/полей нужна версия и совместимость чтения.

## 9. Файлы и тесты

| Файл | Что |
|---|---|
| `src/amfe/data/audit_log.py` | канонический JSON, `event_id`, `validate_*`, `AuditLog` (append/verify/export), обёртки |
| `src/amfe/data/event_bus.py` | `Event`, `EventBus` (publish/load/filter/verify/export/summary), ошибки |
| `tools/event_bus.py` | CLI + демо-ридер трёх артефактов |
| `tests/test_event_bus.py` | **23 теста OK**: roundtrip, канонический `event_id` и детерминизм, цепочка, подмена/разрыв/удаление/усечение, append-only префикс, отказ при нарушении непрерывности, неизвестный kind, секреты/сырые данные/oversize/NaN/вложенность/refs/ts, фильтры, обёртки, export+manifest, CLI (emit/list/verify/export/json, exit 1 на подмене), синтетическое демо (4 события, источники неизменны, повтор без `--force` → ошибка) |

Регрессии не запускались по чужим трекам; журналы (`registry/forecasts.jsonl`,
`protocols/signoff_registry.json`, `build/c6/c6_audit.jsonl`) не тронуты.
