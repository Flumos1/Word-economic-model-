# 110. Форвард-контур: движок, резолв и первая запись журнала

Версия 1.0.0 · 2026-11-12. Волна 5, трек B. Задача: `task-3`. Действующий протокол —
**v4 FROZEN** (`protocols/evaluation_protocol_v4.json`, SHA-256 `bd965f4d…`), секция
`forward_registry`; журнал — [registry/forecasts.jsonl](../registry/forecasts.jsonl),
append-only, единственный писатель — [tools/registry.py](../tools/registry.py).
Новые инструменты: [tools/forward_forecast.py](../tools/forward_forecast.py),
[tools/registry_resolve.py](../tools/registry_resolve.py);
workflow: [.github/workflows/forward-registry.yml](../.github/workflows/forward-registry.yml);
тесты: [tests/test_forward_registry_engine.py](../tests/test_forward_registry_engine.py) (21 тест).
Это отчётность и инфраструктура журнала, **не допуск**: статус ни одного кандидата не меняется.

## 1. Что сделано

1. **Движок записи** `tools/forward_forecast.py`: на месячном origin строит строки
   зарегистрированных кандидатов (`ENSEMBLE_ADAPTIVE`, `ENSEMBLE_INVERSE_MSE`, при запросе —
   `amfe.candidates`/SPF-кандидаты) и контрольных плеч протокола (`RW`, `EQUAL_WEIGHT_POINT`,
   `EQUAL_WEIGHT_DENSITY`, `OWN_NOWCAST_BRIDGE` где определено), проверяет их схему через
   `registry.validate_record` и аппендит через `registry.append_record`. Поля — строго по
   `forward_registry.record`: `candidate_id`, `code_commit` (git SHA), `data_vintage_hash`
   (SHA-256 отпечатка винтажных дат/последних наблюдений, **без сырых значений**), `target`,
   `horizon`, `origin` (`YYYY-MM`), `quantiles_5_to_95_step_5` + `point` для T1/T2 или
   `event_probability` для T3, `scoring_rule_commit` (SHA-256 протокола v4 с нормализацией
   переводов строк). Служебные метаданные — `registered_at` и `notes` (режим данных, класс,
   pre-origin, отпечаток обрезки информации).
2. **Гварды origin** (жёсткие отказы, частичная запись невозможна): будущий месяц
   (`origin > текущего месяца`) — отказ; origin раньше начала журнала (первый
   `registered_at`, т.е. **запрет backdating**) — отказ; факт целевого периода уже
   опубликован (есть first-publication факт `actual_pair` **или** период ≤ последнего
   наблюдения исходного ряда) — отказ. Октябрьский origin 2026-10 отклоняется именно так:
   CPI за октябрь 2026 опубликован 2026-11-12.
3. **Резолв** `tools/registry_resolve.py`: `--status` — C6-статус (`n_resolved`, `min 12`,
   `pass/fail` через `tools/evaluate.c6_forward_check`), `--resolve` — кумулятивная
   относительная потеря и покрытие интервалов по записям с опубликованным фактом. Компаратор
   берётся **из журнала** (строки контрольных плеч `EQUAL_WEIGHT_POINT` /
   `EQUAL_WEIGHT_DENSITY` на тех же `origin/target/horizon`), поэтому резолв никогда не
   пересчитывает прогноз по данным, появившимся после origin. Сырые значения не печатаются
   и не пишутся: только счётчики, относительные потери, покрытие и калибровка.
4. **Workflow** `.github/workflows/forward-registry.yml`: `schedule` 15-го числа 14:00 UTC
   (после релиза CPI BLS) + `workflow_dispatch`; numpy; восстановление приватных артефактов
   `data-refresh`/`r2-metadata` как в `release-check.yml`; генерация строк, `--validate`,
   `--status`/`--resolve`, summary на странице run, артефакт
   `amfe-forward-registry-<run_id>`; **ничего не коммитит, не пушит и не тегирует**.
5. **Первая запись журнала** (append через API, `--validate` и `--status` — см. §3).

## 2. Origin 2026-11 и pre-origin запись

По `origin_policy`: в конце месяца `t` последнее опубликованное значение CPI — месяц `t-1`,
горизонт `h` — это г/г инфляция месяца `t-1+h`; для `h=1` цель — ноябрь 2026, факт
~2026-12-10. Месячный workflow запускается 15-го, т.е. **до конца месяца origin**: строка
пишется досрочно (pre-origin). Это соответствует `integrity`: журнал коммитится **до**
публикации факта. Информационный набор досрочной строки — подмножество набора конца месяца,
look-ahead нет: `amfe.forecast` обрезает все ряды по последнему наблюдению снимка и падает
на любом наблюдении после origin (`assert_no_lookahead`).

`amfe.forecast.data.validate_origin` ограничивает origin последним наблюдением скачанных
tapes; для месячной строки 15-го числа origin на один месяц позже. Движок расширяет это
ограничение **ровно на один месяц** только на время расчёта (контекстный патч
`_PreOriginData`), исторические origin сохраняют исходную семантику. Это записано в
`notes` каждой строки (`pre_origin=true`).

**Режим данных.** `--mode latest` (по умолчанию) — первый релизный снимок (`latest.csv` из
приватного `data-refresh`, получен 2026-11-12, данные по октябрь 2026 включительно): для
октябрьского CPI это первая публикация. Слой прогноза классифицирует tape как
`PSEUDO_REAL_TIME` консервативно; протокольный класс `REAL_TIME_TAPE`
(`data_depth.classification_rule`) записан в `notes` отдельно. `--mode auto|vintage` —
воспроизводимый REAL_TIME по архиву винтажей (последний квартальный винтаж 2026-09-11,
данные по август 2026); используется, если для допуска нужен именно REAL_TIME-класс.
`data_vintage_hash` фиксирует использованные винтажные даты и последние наблюдения — это
делает строку проверяемой, не раскрывая значений.

## 3. Первая запись: журнал

Origin `2026-11`, `T1_CPI_YOY`, `h=1`, `registered_at=2026-11-12T17:27:52Z`, `code_commit`
`0f9eb35…` (HEAD на момент записи), `scoring_rule_commit` `bd965f4d…` (протокол v4),
`data_vintage_hash` `52781a86…10a6e`, режим `latest`, info cutoff: CPIAUCSL/CPILFESL/GS10/
TB3MS/UNRATE = 2026-10, GDPC1 = 2026Q2.

| candidate_id | origin | target | h | point | q05 | q95 | control_arm |
|---|---|---|---|---|---|---|---|
| `ENSEMBLE_ADAPTIVE` | 2026-11 | T1_CPI_YOY | 1 | 3.5570 | 2.361 | 4.955 | нет |
| `ENSEMBLE_INVERSE_MSE` | 2026-11 | T1_CPI_YOY | 1 | 3.6257 | 2.458 | 4.797 | нет |
| `RW` | 2026-11 | T1_CPI_YOY | 1 | 3.7173 | 2.751 | 4.621 | да |
| `EQUAL_WEIGHT_POINT` | 2026-11 | T1_CPI_YOY | 1 | 3.4600 | 2.176 | 5.053 | да |
| `EQUAL_WEIGHT_DENSITY` | 2026-11 | T1_CPI_YOY | 1 | 3.4600 | 2.176 | 5.053 | да |

Строк: **5**, все — по одной паре `(T1_CPI_YOY, 1)`. Проверки: `python tools/registry.py
--validate` → `{"valid": true, "problems": []}`; `python tools/registry.py --status` → `rows=5`,
`forward_registry_start=2026-11-12`. `registry_resolve.py --status` → `n_resolved=0`,
`min_forecasts=12`, `status=FAIL_INSUFFICIENT_DATA` (C6 физически не может пройти до накопления
12 разрешённых прогнозов; ни один факт ещё не опубликован). `--resolve` → `resolved_rows=0`,
`unresolved_rows=5`.

## 4. Как воспроизвести

```powershell
python tools/forward_forecast.py --origin 2026-11 --dry-run     # построить, не писать
python tools/forward_forecast.py --origin 2026-11               # записать через registry.py
python tools/registry.py --validate
python tools/registry.py --status
python tools/registry_resolve.py --status
python tools/registry_resolve.py --resolve
python -m unittest tests.test_forward_registry_engine -v
python -c "import yaml; yaml.safe_load(open('.github/workflows/forward-registry.yml', encoding='utf-8'))"
```

Повторный запуск с тем же origin идемпотентен: строки с той же identity и тем же digest
пропускаются (`skipped`), изменить строку можно только correction-строкой с `correction_of`.

## 5. Тесты (21, все зелёные)

Схема записи (обязательные поля протокола, 19 строго возрастающих квантилей, T3 только
`event_probability` без `point`/квантилей, отказ на неизвестном поле); append-only
(уникальность identity, отказ на дубликат без `correction_of`, коррекция по digest, новая
identity = новая строка); гварды (прошлый и будущий origin, pre-origin текущего месяца,
опубликованный факт); детерминизм (канонический JSON/digest, повторяемый
`data_vintage_hash`); резолв на синтетическом журнале (относительная потеря, покрытие,
пустые факты не входят в счёт, `n<12` → fail, 12 «идеальных» прогнозов → pass);
`evaluate.c6_forward_check` n<12 → fail; отсутствие сырых значений в выводе; YAML-workflow
(валиден, есть schedule/dispatch, нет `git commit`/`git push`, есть upload-artifact).

## 6. Ограничения

- **Pre-origin семантика.** Строка committed 2026-11-12 для origin 2026-11-30: это
  сознательная запись до месяца origin; информационный набор — снимок на дату записи
  (подмножество набора origin), look-ahead нет. Протокол такого случая явно не описывает;
  правило зафиксировано здесь и в `notes` строк.
- **Класс данных.** Tape-снимок слой прогноза помечает `PSEUDO_REAL_TIME`, хотя по
  `data_depth.classification_rule` первый релизный tape — `REAL_TIME_TAPE`. Для
  REAL_TIME-строк нужен `--mode auto` (квартальный винтажный архив отстаёт на релиз: данные
  по август 2026 вместо октября 2026).
- **EQUAL_WEIGHT_POINT = EQUAL_WEIGHT_DENSITY для T1 h=1**, пока все члены компаратора дают и
  точку, и распределение; различие возникнет только с point-only членом (SPF исключён до R2).
- **T3, T2, SPF-кандидаты** движок поддерживает по коду; dry-run 2026-11-12 проверены
  `T3_RECESSION:3` (ENSEMBLE_ADAPTIVE p=0.0413, INVERSE_MSE p=0.0365, EQUAL_WEIGHT p=0.0432) и
  `T2_GDP_GROWTH:0` (OWN_NOWCAST_BRIDGE в контрольных плечах, point 1.8080); в первую запись
  эти пары не входят — задача трека открыть журнал по паре T1 h=1. SPF-кандидаты доступны
  только при наличии SPF-файлов.
- **C6 не ускоряется:** 12 разрешённых месячных прогнозов T1 h=1 — это ~12 месяцев после
  первой записи, т.е. первая возможная дата C6 — конец 2027 / начало 2028 (как в docs/105).
  Пока журнал не накоплен, потолок статуса — `CANDIDATE`.
- **Workflow не коммитит:** строки, построенные в CI, лежат в артефакте; коммит журнала —
  отдельное решение и действие человека/Lead (append-only и независимая проверка).
- Замороженные файлы, `protocols/`, `src/**`, `tools/registry.py`, `tools/evaluate.py`,
  `tools/run_real_evaluation.py`, `composition`, `candidates` не изменялись.
