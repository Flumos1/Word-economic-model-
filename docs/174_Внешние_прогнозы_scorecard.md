# 174. Внешние прогнозы: PIT-scorecard (task-54/task-82, UC-44)

Дата: 2026-11-26 (обновление task-82: второй OPEN-источник). Задача: `task-54` (docs/173 §3,
P0-1; docs/174_50_кейсов §UC-44), продолжение `task-82` (M2 docs/207: scorecard ≥2).
Инструмент: [`tools/external_forecast_scorecard.py`](../tools/external_forecast_scorecard.py) ·
Загрузка данных: [`tools/fetch_scorecard_data.py`](../tools/fetch_scorecard_data.py) ·
Отчёт: [`protocols/external_forecast_scorecard.json`](../protocols/external_forecast_scorecard.json)
(`content_sha256 = 494cb0060b306f42…`) ·
Тесты: [`tests/test_external_forecast_scorecard.py`](../tests/test_external_forecast_scorecard.py) +
[`tests/test_external_forecast_scorecard_fetch.py`](../tests/test_external_forecast_scorecard_fetch.py)
(**51 тест, `Ran 51 tests OK`, офлайн**).
Связанные документы: docs/173 (P0-1), docs/207 §M2, docs/95 (SPF в T2-скоринге — те же окна),
docs/86 (SPF-маппинг и даты релизов), docs/84 и `protocols/licence_register.json` (лицензии),
`protocols/evaluation_protocol_v5.json` (FROZEN: окна, факты, потери, политика источников).

> Не путать с [`docs/174_50_кейсов_использования.md`](174_50_кейсов_использования.md): это разные
> файлы. Тот — каталог 50 кейсов (UC-44 в нём — «подписка на scorecard»); этот — сам scorecard.

**Статус: 2 scorable источника из 4; M2 docs/207 (≥2) выполнен, docs/173 (≥3) — ещё нет.**
Скорятся **SPF (233 PIT-точки)** и **NY Fed Staff Nowcast (48 PIT-точек, только 2016Q1–2019Q4
as published)**; суммарно 281 точка на 838 eligible началах. Поэтому отчёт честно имеет
`status = PARTIAL`, `status_reasons = ["SCORED_SOURCES_BELOW_CRITERION(2<3)"]`: второй источник
добавлен, но третий MVP-критерий docs/173 не достигнут. В отчёте отдельно:
`criteria.m2_min_scored_sources = 2`, `cross_source.criteria_pass.m2_sources = true` — M2-требование
видно явно и не маскирует более строгий критерий. Допуск не присваивается.

CRPS и Brier реализованы и покрыты тестами, но для текущих источников `NOT_AVAILABLE` — ни один из
них не публикует ни плотность, ни вероятность события; подставлять «свою» плотность, чтобы получить
число, инструмент отказывается.

## 1. Зачем

docs/173 §3: «мы умеем сравнивать свои модели, но не умеем честно ответить „кто точнее — мы, SPF,
GDPNow или NY Fed, и когда“». Ответ на этот вопрос — не таблица RMSE, а **PIT-дисциплина**: у
каждого внешнего прогноза должна быть дата публикации, горизонт, цель, ошибка и покрытие, посчитанные
на тех же окнах и по тем же правилам, что у проекта, и с фактами из **первой публикации**, а не из
последней ревизии.

M2 docs/207 требует ≥2 scorable источника на открытых данных (платные лицензии не используем).
SPF и NY Fed — оба OPEN в `protocols/licence_register.json`; их значения никогда не попадают в git.

## 2. Источники, лицензии и что с ними можно делать

Статус берётся из `protocols/licence_register.json` (append-only, запись на дату оценки). Правило:
`OPEN` → можно скорить; `RESTRICTED`/`UNVERIFIED` → значения не читаются вообще; публикуются только
агрегаты. Проектная политика строже лицензий: **ни один внешний прогноз/факт не публикуется в
отчёте**, даже если лицензия разрешает выборочную публикацию — в отчёте только агрегаты, counts,
метаданные и SHA-256 (`publication_policy.third_party_values = "aggregates_and_metadata_only"`).

| Источник (id) | Провайдер | Запись в реестре | Статус в отчёте | Что сделано |
|---|---|---|---|---|
| `SPF_GDP_MEDIAN` | Philadelphia Fed, SPF | `PHILADELPHIA_FED` — **OPEN** (research use), но с условием «values outside git» | **AVAILABLE**, 233 точки | скорится: медиана `drgdp2/3` (T2 h=0/h=1); значения только в git-ignored `data/raw/spf/spf_median.json`, в отчёт не пишутся |
| `NY_FED_STAFF_NOWCAST` | NY Fed, Staff Nowcast | `NY_FED` — **OPEN** (атрибуция, mark modifications, redistribute on same terms) | **AVAILABLE**, 48 точек | загружен публичный workbook (2002–2021, values-only), преобразован в `data/raw/ny_fed/nowcast.csv`; 2002Q1–2015Q4 — `RECONSTRUCTED` (не real-time, исключаются), 2016Q1–2021Q3 — `PUBLISHED`; в scoring попадают только PUBLISHED-точки W2 (2016–2019), h=0 |
| `CLEVELAND_FED_NOWCAST` | Cleveland Fed, Inflation Nowcasting | `CLEVELAND_FED` — **OPEN** (non-commercial, credit) | **UNAVAILABLE** (`NO_PROTOCOL_HORIZON_MAPPING`) | сопоставления нет: публикуется nowcast текущего месяца по CPI, цель протокола T1 — y/y на t−1+h; скорить нечего, поэтому только метаданные |
| `ATLANTA_FED_GDPNOW` | Atlanta Fed, GDPNow | `ATLANTA_FED_GDPNOW` — **RESTRICTED** | **RESTRICTED_NOT_USED** (`LICENCE_RESTRICTED`) | значения не читаются даже при наличии локального файла (есть тест); модель-величина заменена своим OPEN `OWN_NOWCAST_BRIDGE` (protocol `licence_policy.substitute`), и это прямо сказано в отчёте |

Отдельно зафиксировано: SPF-значения **не** публикуются (условие реестра), поэтому в отчёте у
источника стоит `values_published = false`, а блок значений — `null`; у NY Fed лицензия допускает
выборочную публикацию, но проектная политика `REPORT_VALUES_POLICY = aggregates_and_metadata_only`
всё равно ставит `values_published = false` и `sample = null`. Тест
`test_report_contains_no_third_party_forecast_values` обходит весь JSON и запрещает ключ `forecast`,
а также ищет в тексте отчёта фактические числа из файла SPF; отдельный тест
`test_project_policy_keeps_values_out_even_when_the_licence_allows_them` фиксирует более строгое
правило.

## 3. Данные NY Fed: что именно загружено (task-82)

| Факт | Значение |
|---|---|
| URL workbook | `https://www.newyorkfed.org/medialibrary/media/research/policy/nowcast/new-york-fed-staff-nowcast_data_2002-present.xlsx` |
| Размер / SHA-256 xlsx | **210 018 B** / `b0ce3db1592672375e3bd43d827da6cf512fbbb602252abb927e6b6d577c234f` |
| Лист / колонки | `Forecasts By Quarter`; `Forecast Date` + кварталы `YYYYQn`; значения — annualised q/q real GDP growth, percent |
| Сырой CSV (git-ignored) | `data/raw/ny_fed/nowcast.csv`, **64 203 B**, sha256 `11cb535c15aaab66e076625b64e6d0cc20995c6b36a7999253aab3fece9bc9ff` |
| Строки | **1 790**: 502 `PUBLISHED` + 1 288 `RECONSTRUCTED` |
| Покрытие | 2002Q1–2021Q3; даты публикаций 2001-11-23…2021-08-27; **23 квартала** с PUBLISHED-строками |
| PIT-граница | 2002Q1–2015Q4 — historical reconstructions (не публиковались в реальном времени); 2016Q1 onward — as published; в W2 (2007–2019) реально скорится 2016–2019 |
| Ограничение | архив values-only, без underlying series и без винтажей; обновления приостановлены после 2021Q3 (licence register note) — PIT-архива после 2021 нет |
| Загрузка | `python tools/fetch_scorecard_data.py` (URL, `retrieved_at`, bytes, sha256, row counts → `data/raw/ny_fed/nowcast.meta.json` + JSONL-лог; `--check` сверяет CSV/manifest; `--expect-sha256` закрепляет текущий xlsx) |

Инструмент загрузки намеренно не пишет значения в git: xlsx и CSV — третьи данные, они живут только
в git-ignored `data/raw/`; в коммит попадают только URL/размер/sha256/счётчики в отчёте и документации.

## 4. PIT-правила (как не заглянуть в будущее)

1. **Дата публикации, а не дедлайн.** Прогноз входит в scorecard, только если его дата публикации
   `≤ origin` (`origin_policy.default`). Для SPF это официальная дата новостного релиза квартала
   опроса (`src/amfe/ensemble/spf_calendar.py`, docs/86 §5), не дата дедлайна. Для NY Fed — колонка
   `Forecast Date` (пятничная публикация nowcast), не дата квартала.
2. **Real-time начало.** До 1990Q3 SPF не проводился в реальном времени → точки до 1990Q3
   отбрасываются с причиной `SPF_NOT_REAL_TIME_BEFORE_1990Q3`. Для NY Fed строки
   `RECONSTRUCTED` (2002Q1–2015Q4) отбрасываются с причиной `RECONSTRUCTED_NOT_REAL_TIME` — это
   реконструкции «что модель показала бы», а не публикации.
3. **h=0 — только средний месяц квартала SPF.** Origin обязан быть концом февраля/мая/августа/ноября;
   иначе `ORIGIN_NOT_A_SURVEY_MONTH`. Так 117 из 360 точек — ровно те 32.5%, что и в docs/95 §2.
4. **h=1 — конец квартала SPF.** Медиана `drgdp3` берётся из опроса этого же квартала (опубликован в
   его среднем месяце), поэтому доступна к концу квартала: 116 из 118 начал.
5. **NY Fed h=0** — любой конец месяца; берётся последняя **PUBLISHED** строка для целевого квартала
   с `date ≤ origin`; более поздние или RECONSTRUCTED строки не используются.
6. **Факт — только первая публикация.** `actuals.primary` из ALFRED-архива винтажей
   (`src/amfe/evaluation/actuals.py`), значение винтажа через 12 месяцев — отдельный `secondary`,
   они никогда не смешиваются. Если первая публикация факта датирована `≤ origin`, точка
   отбрасывается (`ACTUAL_PUBLISHED_AT_OR_ORIGIN`): «прогноз» на уже опубликованный факт — не прогноз.
7. **Окно по origin и цели.** Origin принадлежит окну, только если внутри окна и origin, и период
   цели (`windows.defined_by`); цели, попадающие в COVID-окно исключения, отбрасываются.
8. **Квантили только по протокольной сетке.** Плотность скорится лишь при 19 уровнях 5%…95% шагом
   5% и неубывающей квантильной функции; пересечение квантилей → `INVALID_QUANTILES: …`, а не
   «как-то посчитаем».

Каждая причина исключения считается поимённо и попадает в `exclusions` блока окна; `lookahead_excluded`
— сумма причин, означающих «информации на origin ещё не было».

## 5. Окна и покрытие

Окна — из FROZEN `protocols/evaluation_protocol_v5.json`: W1 1990-01…2006-12, W2 2007-01…2019-12.
Цель T2_GDP_GROWTH (`400·ln(GDP_q/GDP_{q−1})`, annualised), горизонты h=0 и h=1.

| Источник | Цель/h | Начал W1+W2 | Scored | Покрытие | W1 | W2 |
|---|---|---:|---:|---:|---:|---:|
| SPF | T2 h=0 | 360 | **117** | 0.325 | 66 из 204 | 51 из 156 |
| SPF | T2 h=1 | 118 | **116** | 0.983 | 65 из 67 | 51 из 51 |
| NY Fed | T2 h=0 | 360 | **48** | 0.133 | 0 из 204 | 48 из 156 |

SPF h=0: 240 — origin не средний месяц квартала, 2 — до 1990Q3, 1 — релиз 2019Q1 (22.03.2019,
shutdown) позже февральского конца месяца. Это в точности числа docs/95 §2 — независимое
подтверждение, что календарное правило реализовано так же, как в sensitivity-инструменте.
NY Fed: в W1 нет ни одной PUBLISHED-строки (144 `NO_PUBLICATION_BEFORE_ORIGIN` + 60
`RECONSTRUCTED_NOT_REAL_TIME`); в W2 108 начал отброшены как `RECONSTRUCTED_NOT_REAL_TIME`
(2007–2015) — реально скорится только 2016–2019 (48 начал). Это **не** полное покрытие W2 и не
«теперь у нас PIT на 2007–2019».

## 6. Результаты

Метрики — на первой публикации факта; `secondary` — RMSE против винтажа через 12 месяцев.
Это диагностика, не сравнение «кто лучше»: у источников разные наборы начал (SPF W2 2007–2019,
NY Fed только 2016–2019), поэтому RMSE нельзя читать как рейтинг skill.

### 6.1 SPF (без изменений с первого прогона; для сверки)

| Цель/h | Окно | n | RMSE | MAE | bias | RMSE (secondary) |
|---|---|---:|---:|---:|---:|---:|
| T2 h=0 | W1 | 66 | 1.7580 | 1.3972 | −0.4782 | 1.7864 |
| T2 h=0 | W2 | 51 | **1.4694** | 1.1046 | +0.1577 | 1.5518 |
| T2 h=0 | W1+W2 | 117 | 1.6384 | 1.2697 | −0.2010 | 1.6873 |
| T2 h=1 | W1 | 65 | 1.9686 | 1.5262 | −0.2998 | 1.9286 |
| T2 h=1 | W2 | 51 | 1.8919 | 1.3251 | +0.3927 | 1.9528 |
| T2 h=1 | W1+W2 | 116 | 1.9353 | 1.4378 | +0.0046 | 1.9393 |

**Сверка с docs/95 §3.3:** RMSE медианы SPF на W2 h=0 = **1.4694** — совпадает с docs/95 до
четвёртого знака. Это не переиспользование, а независимый пересчёт другим инструментом на тех же
началах (в docs/95 подвыборка была ограничена REAL_TIME-началами, здесь — все начала окна, где
медиана публична; в W2 оба набора совпали: 51).

### 6.2 NY Fed Staff Nowcast (новое)

| Цель/h | Окно | n | RMSE | MAE | bias | RMSE (secondary) | coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| T2 h=0 | W1 | 0 | — | — | — | — | 0.000 |
| T2 h=0 | W2 | **48** | **0.9494** | 0.8216 | −0.2938 | 1.0076 | 0.3077 |
| T2 h=0 | W1+W2 | 48 | 0.9494 | 0.8216 | −0.2938 | 1.0076 | 0.1333 |

Содержательно: на 48 началах 2016–2019 NY Fed nowcast имеет RMSE 0.949 против first publication
(MAE 0.822, bias −0.294 п.п.); при ревизии факта через 12 месяцев RMSE 1.008. **НЕЛЬЗЯ** сравнивать
это с SPF W2 (1.469) как «NY Fed точнее SPF»: 48 начал 2016–2019 против 51 начал 2007–2019 — разные
выборки, разные кризисные эпизоды и разное покрытие. Для честного сравнения нужны общие начала; это
следующий шаг (§10), а не вывод этого документа.

## 7. Что не посчитано и почему (честно)

| Метрика | Статус | Причина |
|---|---|---|
| CRPS | `NO_PREDICTIVE_DISTRIBUTION` | SPF и NY Fed публикуют точечный прогноз/медиану, а не плотность; распределение не подставляется (docs/95 §6.4: «SPF-распределение — производная статистика проекта», другой объект) |
| Brier | `NO_EVENT_PROBABILITY_MAPPING` | ни у одного источника нет вероятности протокольного события; SPF RECESS/Anxious Index — вероятность падения GDP, а не событие NBER (docs/86: `NO_NBER_RECESSION_PROBABILITY_AND_EVENT_MISMATCH`) |
| coverage_90 | `NO_PREDICTIVE_DISTRIBUTION` | нет 5–95% интервала у точечного прогноза |
| Покрытие (`coverage_share`) | **AVAILABLE** | SPF 0.325 (h=0), 0.983 (h=1); NY Fed 0.133 (h=0, W1+W2), 0.308 (W2) — это доля покрытых начал, не точность |

CRPS и Brier не «выключены навсегда»: обе функции реализованы (`crps_from_quantiles`,
`brier_score`), проверены на синтетике и включатся автоматически, как только появится источник с
плотностью/вероятностью события, разрешённый реестром.

## 8. Критерии: docs/173 MVP и M2 docs/207

| Критерий | Требуется | Факт | Проходит |
|---|---|---:|---:|
| MVP docs/173: источников со scorecard | ≥ 3 | **2** (SPF, NY Fed) | **нет** |
| M2 docs/207: scorable источников | ≥ 2 | **2** | **да** |
| Общих PIT-точек | ≥ 50 | **281** | да |
| Даты публикации и ошибки в отчёте | есть | есть | да |
| Без нарушения лицензий | обязательно | 0 RESTRICTED-источников использовано, 0 значений опубликовано | да |

Отсюда `status = PARTIAL`, `status_reasons = ["SCORED_SOURCES_BELOW_CRITERION(2<3)"]`. Это
сознательно: M2 закрыт, но MVP docs/173 требует третьего scorable источника; подгонять третий
(взять GDPNow вопреки RESTRICTED, посчитать Cleveland «примерно» на другой цели или выдать
реконструкции NY Fed за real-time) означало бы нарушить и лицензию, и протокол. Разблокировка — §10.

В отчёте это видно машинно: `criteria.min_scored_sources = 3`,
`criteria.m2_min_scored_sources = 2`, `cross_source.criteria_pass = {"sources": false,
"points": true, "m2_sources": true}`.

## 9. Ограничения

1. **MVP-объём:** только T2_GDP_GROWTH, h=0 и h=1. T1/T3 и h=2/h=4 не скорились.
2. **SPF — последняя редакция файла, не винтаж:** дата релиза фиксирует доступность, но errata и
   ревизии после опроса не аудированы (та же оговорка, что в docs/95 §6.3).
3. **NY Fed — values-only архив:** нет underlying series → ре-оценка невозможна; 2002Q1–2015Q4 —
   реконструкции, не публикации; архив заканчивается 2021Q3, поэтому W2 покрыт только 2016–2019.
   Это PIT-архив **частично**, и отчёт не выдаёт его за полный.
4. **Нет плотности/вероятности** у доступных источников → CRPS/Brier/coverage_90 пусты (см. §7).
5. **Перекрывающиеся начала h=0:** 117 точек SPF = ~39 независимых кварталов, 48 NY Fed = 12
   кварталов 2016–2019; значимость (DM/RW) в этом MVP не считается, и отчёт это пишет.
6. **«Внешние vs наши» ещё нет:** в отчёт попадают только внешние прогнозы; присоединение своих
   базлайнов (C6-журнал, R3-ансамбль) — отдельный шаг, зафиксирован в `gaps`.
7. **Данные только из локальных артефактов.** Без `data/raw/spf/spf_median.json` инструмент честно
   пишет `NOT_AVAILABLE / SPF_VALUES_NOT_DOWNLOADED`; без `data/raw/ny_fed/nowcast.csv` —
   `UNAVAILABLE / ARCHIVE_NOT_LOCAL`; ни один источник не фабрикуется. `tools/fetch_scorecard_data.py`
   восстанавливает NY Fed-архив, `tools/fetch_spf.py` — SPF.
8. **Не admission-инструмент:** C1–C6, стадии и допуск не применяются; отчёт диагностический.

## 10. Как разблокировать третий источник (следующие шаги)

1. **GDPNow:** только если владелец/Lead изменит запись `ATLANTA_FED_GDPNOW` в
   `protocols/licence_register.json` после проверки условий (сейчас `RESTRICTED`: страница данных
   разрешает personal/educational reproduction, а site terms запрещают derivative works без
   разрешения; строгое чтение применяется). Без решения владельца значения не читаются — это не
   «техническое» ограничение, а лицензионное.
2. **Cleveland:** нужен источник с годичным y/y горизонтом (или регистрация T1-мэппинга с
   пересчётом m/m → y/y, что меняет цель) — до этого источник остаётся метаданными.
3. **Другие OPEN источники:** искать ряд с T2-мэппингом и публичной датой релиза (не только текущее
   значение); без PIT-архива источник не scorable — «лучше честные 2, чем фиктивные 3».
4. **«Внешние vs наши»:** добавить к тем же началам свои OPEN-базлайны (C6 forward-registry и
   R3-ансамбль) — это сделает UC-44 продуктом, а не диагностикой.
5. **API-слой:** после ≥3 источников отдать отчёт как `/v1/external-forecasts` (docs/174, UC-45).

## 11. Воспроизведение и CI

```powershell
# 1) открытые данные (значения — только в git-ignored data/raw/)
python tools/fetch_scorecard_data.py --expect-sha256 b0ce3db1592672375e3bd43d827da6cf512fbbb602252abb927e6b6d577c234f
python tools/fetch_scorecard_data.py --check
# SPF (если ещё не загружен): python tools/fetch_spf.py

# 2) scorecard
python tools/external_forecast_scorecard.py --check            # метаданные + доступность (без фактов)
python tools/external_forecast_scorecard.py                    # полный прогон, пишет отчёт
python tools/external_forecast_scorecard.py --horizons 0 --max-origins 24 --out <tmp>.json

# 3) тесты
python -m unittest tests.test_external_forecast_scorecard tests.test_external_forecast_scorecard_fetch
```

`--check` возвращает 0 только при `status = OK`; сейчас он ожидаемо возвращает **3** (PARTIAL,
2<3). Полный прогон возвращает 0 и печатает сводку с `content_sha256`; `--max-origins` не
используется для отчёта (иначе NY Fed не попадает в первые 30 начал W2 и источник может выглядеть
пустым).

Детерминизм: отчёт не зависит от времени (`generated_at` исключён из `content_sha256`), порядок
обхода начал и источников фиксирован, средние считаются через `math.fsum`; тесты проверяют
совпадение digest'а у двух прогонов и независимость от `generated_at`. CSV NY Fed детерминирован:
повторный `fetch_scorecard_data.py` даёт тот же sha256 `11cb535c…` при том же xlsx.

Сырые данные: SPF-значения — в git-ignored `data/raw/spf/spf_median.json`; NY Fed xlsx/CSV — в
`data/raw/ny_fed/`; архивы ALFRED (`data/raw/GDPC1/vintages.zip`) только читаются. Инструменты
пишут единственный tracked-файл scorecard — `protocols/external_forecast_scorecard.json`; в git
попадают только агрегаты, даты, пути и SHA-256.

## 12. Файлы

| Файл | Размер | SHA-256 / примечание |
|---|---|---|
| `tools/external_forecast_scorecard.py` | 70 690 B | `f6f6b60879123309…`; добавлены M2-критерий, publication policy, NY Fed URL |
| `tools/fetch_scorecard_data.py` | 17 265 B | `c757f6b3c3edffc3…`; URL/дата/размер/sha256 в manifest + JSONL-лог; `--check` |
| `protocols/external_forecast_scorecard.json` | 30 987 B | sha256 `073775dfd17c1fef…`; `content_sha256` `494cb0060b306f42…`; `generated_at` 2026-11-26T17:04:43Z |
| `tests/test_external_forecast_scorecard.py` | 29 976 B | 41 тест; `Ran 41 tests OK` |
| `tests/test_external_forecast_scorecard_fetch.py` | 9 531 B | 10 тестов; `Ran 10 tests OK` |
| `protocols/evaluation_protocol_v5.json` | FROZEN | sha256 `48c2f6a18a28cefc…` (в отчёте — `protocol.sha256`) |
| `protocols/licence_register.json` | — | sha256 `4c1ab61c0a4b85f1…` (в отчёте — `licence_register.sha256`) |

Новые данные (git-ignored, не коммитятся): `data/raw/ny_fed/nowcast.csv` (64 203 B, sha256
`11cb535c…`), `data/raw/ny_fed/nowcast.meta.json` (URL/дата/размер/sha256/counts),
`data/raw/scorecard_fetch_log.jsonl` (история загрузок).
