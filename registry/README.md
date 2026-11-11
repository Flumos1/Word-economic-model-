# Журнал будущих прогнозов (forward registry)

Здесь живёт **append-only** журнал прогнозов AMFE — `registry/forecasts.jsonl`, одна каноническая
JSON-строка на прогноз (UTF-8, LF). Правила и поля заданы замороженным протоколом
`protocols/evaluation_protocol_v4.json` → `forward_registry`.

## Что коммитится, а что нет

| Что | Где | В git |
| --- | --- | --- |
| Журнал прогнозов: идентификаторы, даты, хеши, числа прогнозов | `registry/forecasts.jsonl` | **да** |
| README и правила | `registry/README.md` | **да** |
| Сырые сторонние данные (FRED/ALFRED/FRED-MD/SPF и т.п.) | `/data/raw/`, `/data/external/` | **нет** (`.gitignore`, `licence_policy`) |

Журнал — это метаданные и собственные прогнозы проекта, а не данные. Он коммитится **до публикации
фактического значения**, поэтому служит доказательством, что прогноз не подогнан задним числом.
Сырые наборы сторонних данных в журнал и в репозиторий не попадают никогда: только URL, дата
получения и SHA-256 в `protocols/data_log.json`.

## Поля записи

Обязательные (протокол v4, `forward_registry.record`):

- `candidate_id` — идентификатор кандидата или контрольного плеча (`RW`, `EQUAL_WEIGHT_POINT`,
  `EQUAL_WEIGHT_DENSITY`, `OWN_NOWCAST_BRIDGE`, …);
- `code_commit` — git-коммит (7–64 hex), которым построен прогноз;
- `data_vintage_hash` — SHA-256 винтажа данных на момент origin (64 hex);
- `target` — `T1_CPI_YOY`, `T2_GDP_GROWTH` или `T3_RECESSION`;
- `horizon` — допустимые горизонты цели по протоколу;
- `origin` — конец месяца `YYYY-MM`;
- `scoring_rule_commit` — коммит/хеш правил скоринга (7–64 hex);
- для T1/T2: `quantiles_5_to_95_step_5` — 19 строго возрастающих квантилей (5%…95% с шагом 5%)
  и `point`; для T3: `event_probability` ∈ [0, 1], поле `point` не используется (`null`).

Необязательные метаданные: `registered_at` (ISO 8601), `correction_of` (SHA-256 заменяемой
строки), `notes`, `control_arm` (bool).

## Append-only

- Строки только добавляются; правки и удаления запрещены.
- Дубликат `(candidate_id, target, horizon, origin)` отвергается.
- Исправление — **новая** строка с `correction_of = SHA-256` канонического JSON заменяемой строки.
- `forward_registry.start` в протоколе = `null`: началом журнала считается дата первого коммита,
  то есть первого `registered_at`. Задним числом записи не добавляются.

## Запуск

```powershell
python tools/registry.py --status      # сводка: число строк, первая/последняя дата, пары
python tools/registry.py --validate    # проверка схемы и append-only правил
python tools/registry.py --append registry/record.json
python tools/registry.py --list
```

Валидатор — `tools/registry.py`; он же единственный писатель. Файл `registry/forecasts.jsonl`
создан пустым: первая запись появится после R2 и регистрации первого кандидата (протокол,
`admission.stages`, `acceptance_checklist`: «forward registry opened and the first registry
commit made; candidates registered before any evaluation»).
