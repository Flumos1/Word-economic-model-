# 72. Harness допуска C1–C6 и журнал будущих прогнозов

Назначение: исполняемый слой замороженного протокола оценки
`protocols/evaluation_protocol_v4.json` — расчёт потерь, порогов C1 и тестов C2/C4/C5/C6, состав
основного компаратора и append-only журнал будущих прогнозов. Harness **не зависит от моделей**:
все функции принимают `numpy`-массивы и не импортируют `src/amfe/forecast`, поэтому он готов и
проверен на синтетике до появления первых моделей.

## 1. Архитектура

```
protocols/evaluation_protocol_v4.json ──┐
protocols/licence_register.json ────────┼─> tools/composition.py ─> protocols/comparator_composition_<дата>.json (+ .sha256)
protocols/r3_specification.json ────────┘                             │
                                                                      v
protocols/nf_sim_thresholds.json ─────────> tools/evaluate.py <──── registry/forecasts.jsonl
                                                      │                ^
                                                      v                │
                                             отчёт допуска C1–C6     tools/registry.py
```

| Модуль | Роль | Зависимости |
| --- | --- | --- |
| `tools/composition.py` | механический состав основного компаратора на дату оценки + канонический JSON и SHA-256 | protocol v4, licence register, r3 spec |
| `tools/registry.py` | append-only журнал будущих прогнозов, схема и валидация, запрет дублей/правок | protocol v4 `forward_registry` |
| `tools/evaluate.py` | потери, C1–C6, GW/DM с HAC и HLN, Romano–Wolf (упрощённо) | protocol v4, nf_sim threshold table |

Ключевое свойство: ни один выбор не делается исследователем. Состав компаратора — функция
замороженного протокола и реестра лицензий; потери и порог фиксированы протоколом и NF-SIM-таблицей;
порядок критериев и уровни значимости — тоже.

## 2. Состав основного компаратора (`tools/composition.py`)

Правило (`primary_comparator_rule`, `comparator_minimum`):

- `comparator_member = true` и `scored ≠ false` у baseline протокола;
- все входы для цели `OPEN` в реестре лицензий **на дату оценки**; статус берётся из последней
  датированной записи реестра на дату или раньше (реестр append-only — изменение статуса это новая
  запись, не правка);
- для point-потерь — только члены с point-прогнозом, для density-потерь — только члены с
  прогнозным распределением (`point_only` исключаются), для T3 — члены с вероятностью события;
- минимум 2 члена на `(target, horizon, loss)`, иначе пара остаётся `CANDIDATE`.

Первый документ: `protocols/comparator_composition_2026-11-11.json` + `.sha256`
(дата первой оценки, после ворот R2). Состав на 2026-11-11:

| Цель | RMSE/CRPS (point) | CRPS (density) | Brier/log (event) |
| --- | --- | --- | --- |
| T1_CPI_YOY | RW, AO, AR_BIC, BVAR_MINNESOTA — 4 на всех горизонтах | те же 4 | — |
| T2_GDP_GROWTH | AO, AR_BIC, BVAR_MINNESOTA; + OWN_NOWCAST_BRIDGE на h=0 (4 / 3 / 3 / 3) | так же | — |
| T3_RECESSION | — | — | PROBIT_SPREAD, SAHM_RULE — 2 на горизонтах 3/6/12 |

Исключены механически: `DFM_FREDMD` (вход `FRED-MD_FRED-QD` — `UNVERIFIED` в реестре),
`SPF_MEDIAN` / `SPF_NEG_GDP_PROB` / `NOWCAST_NYFED` (`comparator_member = false`).
Условие REAL_TIME применяется не в этом документе, а по каждому origin через data gate (R2);
документ фиксирует список членов, а не доступность данных на конкретном origin.

Режимы:

```powershell
python tools/composition.py                      # пересобрать документ и хеш
python tools/composition.py --date 2026-11-11
python tools/composition.py --check              # воспроизвести в памяти и сверить файл и хеш
```

`--check` возвращает 0 при совпадении и 1 при расхождении. Канонический JSON: сортированные ключи,
UTF-8, LF, отступ 2; SHA-256 считается по LF-нормализованному файлу (как у остальных pin-файлов).

## 3. Журнал будущих прогнозов (`tools/registry.py`)

Решение по коммиту: **журнал коммитится**. Записи содержат только идентификаторы, даты, хеши и
собственные числа прогноза — это метаданные и доказательство, что прогноз сделан до публикации
факта; сырые сторонние данные в репозиторий не попадают никогда (`.gitignore`, `licence_policy`).
Поэтому `registry/forecasts.jsonl` (пустой) и `registry/README.md` добавлены в репозиторий.

- файл: `registry/forecasts.jsonl`, одна каноническая JSON-строка на прогноз, UTF-8, LF;
- обязательные поля протокола: `candidate_id`, `code_commit`, `data_vintage_hash`, `target`,
  `horizon`, `origin`, `scoring_rule_commit`, плюс `quantiles_5_to_95_step_5` + `point` для T1/T2
  или `event_probability` для T3;
- append-only: валидатор не даёт изменить/удалить строку; дубликат
  `(candidate_id, target, horizon, origin)` отвергается; исправление — новая строка с
  `correction_of = SHA-256` заменяемой записи;
- `forward_registry.start` в протоколе `null`: началом считается дата первого коммита, т.е. первый
  `registered_at`; задним числом записи не добавляются.

```powershell
python tools/registry.py --status
python tools/registry.py --validate
python tools/registry.py --append registry/record.json
```

## 4. Критерии допуска C1–C6 (`tools/evaluate.py`)

Функции принимают массивы: `point` (n,), `quantiles` (n × 19: 5%…95% шаг 5%), `probability` (n,),
факты `actual_primary` и `actual_secondary`. Основная точка входа —
`evaluate_admission(target, horizon, {"W1": …, "W2": …}, forward=…)`; она возвращает статусы
критериев, стадии и `status` (`CANDIDATE` / `SHADOW_TEST` / `LIMITED_DEPLOYMENT`).

| Критерий | Реализация | Порог |
| --- | --- | --- |
| C1 (гейт) | `loss/relative loss` кандидат/компаратор на W1 и W2 отдельно | `min(0.97, q05)` из `nf_sim_thresholds.json` по `(target, horizon, loss, window)` |
| C2 (гейт) | GW с единственным инструментом (константа) = DM; HAC `h−1`, поправка HLN, односторонняя альтернатива «кандидат лучше» | `p < 0.10` на W1 и W2 для каждой потери; семейная поправка — `apply_romano_wolf` |
| C3 (не гейт) | не реализовано | отчёт `NOT_IMPLEMENTED`, как разрешает протокол |
| C4 (гейт, T1/T2) | относительная потеря ≤ 1.00 на origin-месяцах рецессии NBER (в W1 и W2) + условный GW с инструментами (константа, индикатор рецессии) не отвергает; < 12 рецессионных месяцев → `INCONCLUSIVE` = не пройдено | `p < 0.10` и `a+b > 0` = отказ |
| C5 (гейт) | тот же тест с обратной односторонней альтернативой («кандидат хуже») на вторичных фактах; для T3 вторичные = первичные, C5 повторяет C2 | `p ≥ 0.10` на W1 и W2 |
| C6 (гейт) | ≥ 12 разрешённых прогнозов журнала; кумулятивная относительная потеря ≤ 1.00; покрытие интервалов ±10 п.п. от номинала (T1/T2), для T3 калибровка в вероятностных бинах (≥ 3 непустых) ±10 п.п. | см. протокол `admission.criteria.C6` |

Стадии (`admission.stages`): `CANDIDATE → SHADOW_TEST` = C1, C2, C4 (где применимо), C5;
`SHADOW_TEST → LIMITED_DEPLOYMENT` = C6; `LIMITED_DEPLOYMENT → ACTIVE` = повторный C6 на втором
непересекающемся блоке, отсутствие drift-триггера и ≥ 8 независимых исходов (`long_horizon`) —
последняя стадия в harness вычисляется как `False` до появления журнала. T3 по протоколу имеет
потолок `LIMITED_DEPLOYMENT`.

Multiplicity: семейство кандидатов для `(target, horizon, loss)` собирается вызывающим кодом;
`apply_romano_wolf(reports)` публикует скорректированные p-value. BH-FDR q = 0.10 — только
отчётность (в отчёте указано, что кандидатов надо считать).

## 5. Что упрощено (и почему это не влияет на допуск)

1. **C3 (Model Confidence Set) — не реализовано.** Протокол прямо делает C3 негейтовым:
   сообщаются состав и размер множества; в harness — `NOT_IMPLEMENTED` с `gating: false`.
2. **Romano–Wolf — консервативный шаг-даун (Holm) вместо полного бутстрепа.** Управляет FWER при
   произвольной зависимости, но консервативнее бутстрепа; p-value отчётные. Полный бутстреп
   отложен, на допуск влияет только в сторону ужесточения.
3. **p-value по нормальному приближению с поправкой HLN** (вместо t-распределения). При n ≥ 50
   разница несущественна; указана в отчёте каждого теста.
4. **C4:** относительная потеря считается на рецессионных подвыборках W1 и W2 по отдельности;
   условный GW — HAC-Вальд тест коэффициента при индикаторе рецессии в
   `d_t = a + b·r_t + e_t`; используется формулировка «не отвергать» из протокола.
5. **C6:** покрытие проверяется для центральных 50%, 80% и 90% интервалов, и все три должны быть в
   пределах ±10 п.п. — это строже, чем одно неуточнённое «номинальное» покрытие; для T3 бины
   равной ширины по 0.2.
6. **DM для RMSE** использует дифференциал квадратов ошибок (стандартная формулировка
   Diebold–Mariano), а C1 сравнивает отношения RMSE; это согласовано с NF-SIM (там отношение RMSE).
7. Окно W1 для T3 начинается 1996-12 (`windows.target_overrides.T3_RECESSION.W1_start`) — задаётся
   вызывающим кодом; harness напоминает это в отчёте.

## 6. Запуск и тесты

```powershell
python tools/composition.py --check
python tools/registry.py --status
python tools/evaluate.py --self-test          # синтетика: identical / хороший / плохой кандидат
python -m unittest tests.test_composition tests.test_registry tests.test_evaluate_harness -v
```

Тесты `tests/test_composition.py`, `tests/test_registry.py`, `tests/test_evaluate_harness.py`
проверяют: все члены компаратора OPEN и минимум 2 на плотность/точку/событие; хеш и `--check`;
SPF не член; валидацию схемы журнала, append-only и дубли; identical-методы → отношение потерь = 1;
отрицательный контроль (плохой кандидат отклоняется); порог C1 берётся из NF-SIM; T3 Brier/log,
калибровку и C6; отсутствие импорта `src/amfe/forecast`.

## 7. Открытые пункты (для Lead)

- Журнал открыт пустым: первая запись — после регистрации первого кандидата; `forward_registry.start`
  станет датой первого коммита.
- C3 и полный Romano–Wolf — зарезервированы до появления > 2 кандидатов в семействе (тогда полный
  бутстреп станет осмысленным).
- Входные данные моделей (первичные REAL_TIME-прогнозы) должен поставлять слой моделей; harness
  проверяет только корректность массивов и критериев.
