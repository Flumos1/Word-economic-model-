# 174. Внешние прогнозы: PIT-scorecard (task-54/task-82, UC-44)

Дата: 2026-11-26 (обновление task-88: третий scorable — Livingston Survey). Задача: `task-54`
(docs/173 §3, P0-1; docs/174_50_кейсов §UC-44), продолжения `task-82` (M2 docs/207: scorecard ≥2)
и `task-88` (третий OPEN-источник, docs/254).
Инструмент: [`tools/external_forecast_scorecard.py`](../tools/external_forecast_scorecard.py) ·
Загрузка данных: [`tools/fetch_scorecard_data.py`](../tools/fetch_scorecard_data.py) ·
Отчёт: [`protocols/external_forecast_scorecard.json`](../protocols/external_forecast_scorecard.json)
(`content_sha256 = fd038a777a430a7e…`) ·
Тесты: [`tests/test_external_forecast_scorecard.py`](../tests/test_external_forecast_scorecard.py) +
[`tests/test_external_forecast_scorecard_fetch.py`](../tests/test_external_forecast_scorecard_fetch.py)
(**48 + 14 тестов, `Ran 62 tests OK`, офлайн**; вместе с v2 — 111).
Связанные документы: docs/173 (P0-1), docs/207 §M2, docs/95 (SPF в T2-скоринге — те же окна),
docs/86 (SPF-маппинг и даты релизов), docs/84 и `protocols/licence_register.json` (лицензии),
[`docs/254`](254_PIT_кандидат_NYFed_SMP.md) (OSINT третьего источника),
`protocols/evaluation_protocol_v5.json` (FROZEN: окна, факты, потери, политика источников).

> Не путать с [`docs/174_50_кейсов_использования.md`](174_50_кейсов_использования.md): это разные
> файлы. Тот — каталог 50 кейсов (UC-44 в нём — «подписка на scorecard»); этот — сам scorecard.

**Статус: 3 scorable источника из 4; M2 docs/207 (≥2) и MVP docs/173 (≥3) выполнены.**
Скорятся **SPF (233 PIT-точки)**, **NY Fed Staff Nowcast (48 PIT-точек, только 2016Q1–2019Q4
as published)** и **Livingston Survey (32 PIT-точки, 2004Q2–2019Q4, T2 h=0, strict post-2004)**;
суммарно **313 точек** на 838 eligible началах. Отчёт имеет `status = OK`,
`status_reasons = []`: оба критерия источников пройдены (`criteria.min_scored_sources = 3`,
`criteria.m2_min_scored_sources = 2`), и это не отменяет оговорок §7/§9 — CRPS/Brier по-прежнему
`NOT_AVAILABLE`, допуск не присваивается.

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
| `LIVINGSTON_SURVEY` | Philadelphia Fed, Livingston Survey (median levels) | `LIVINGSTON_SURVEY` — **OPEN** (research use, attribution, values outside git; якорь `PHILADELPHIA_FED`) | **AVAILABLE**, 32 точки | загружены публичные `medians.xlsx`/`livingston-release-dates.xlsx` в git-ignored `data/raw/livingston/`; CSV `livingston_t2_h0.csv` — 45 strict post-2004 строк, в W1/W2 скорится 32 (W1 6, W2 26), T2 h=0; 2020+ и pre-2004 в окна/строгий поднабор не входят |
| `CLEVELAND_FED_NOWCAST` | Cleveland Fed, Inflation Nowcasting | `CLEVELAND_FED` — **OPEN** (non-commercial, credit) | **UNAVAILABLE** (`NO_PROTOCOL_HORIZON_MAPPING`) | сопоставления нет: публикуется nowcast текущего месяца по CPI, цель протокола T1 — y/y на t−1+h; скорить нечего, поэтому только метаданные |
| `ATLANTA_FED_GDPNOW` | Atlanta Fed, GDPNow | `ATLANTA_FED_GDPNOW` — **RESTRICTED** | **RESTRICTED_NOT_USED** (`LICENCE_RESTRICTED`) | значения не читаются даже при наличии локального файла (есть тест); модель-величина заменена своим OPEN `OWN_NOWCAST_BRIDGE` (protocol `licence_policy.substitute`), и это прямо сказано в отчёте |

Отдельно зафиксировано: SPF-значения **не** публикуются (условие реестра), поэтому в отчёте у
источника стоит `values_published = false`, а блок значений — `null`; у NY Fed лицензия допускает
выборочную публикацию, но проектная политика `REPORT_VALUES_POLICY = aggregates_and_metadata_only`
всё равно ставит `values_published = false` и `sample = null`; Livingston-значения — median-уровни
третьей стороны, они лежат только в git-ignored `data/raw/livingston/` и тоже не публикуются. Тест
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

### 3.2 Данные Livingston: что именно загружено (task-88)

| Факт | Значение |
|---|---|
| URL `medians.xlsx` | `https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/livingston-survey/historical-data/medians.xlsx` |
| Размер / SHA-256 `medians.xlsx` | **162 457 B** / `ddb2a9f7ed51c2964921d04efefbe3363b3d8aa223df0c7efc28723d4a8175df` |
| Лист / колонки | `RGDPX`; `Date`, `RGDPX_BP`, `RGDPX_ZM` (median-уровни реального GDP, annual rate, SA) |
| URL / SHA-256 release dates | `livingston-release-dates.xlsx`, **22 357 B**, `22d621f3b2e0069443c9eae934797e9c7fddb7ca1179d2e9db41a0da6023db1b` |
| Лист / колонки release dates | `Dates`; `Survey`, `Exact`, `Not After` (161 строка; `Survey` до 2001 кодирует год +100 — сопоставление по порядку строк, дата берётся из `RGDPX`) |
| Сырой CSV (git-ignored) | `data/raw/livingston/livingston_t2_h0.csv`, **3 079 B**, sha256 `f2d72a83210c830a892b9ab5e6c8efe04d5a80867a42114b72c406e8444850fa`; 45 strict post-2004 строк |
| Строки / окно | 45 строк 2004Q2–2026Q2 (все с точными датами релиза); в W1/W2 скорится 32: W1 6 (2004–2006), W2 26 (2007–2019) |
| PIT-граница | strict post-2004; release_date = `Exact`, иначе `Not After` (PIT-safe upper bound); используется только `release_date ≤ origin` |
| Маппинг | T2_GDP_GROWTH h=0: `400*ln(RGDPX_ZM/RGDPX_BP)`; origin — 30 июня/31 декабря; документальноe преобразование median-уровней, не отдельно опубликованный median growth |
| Загрузка | `python tools/fetch_scorecard_data.py --livingston` (два URL, `retrieved_at`, bytes, sha256, counts → `data/raw/livingston/livingston.meta.json`; `--check` сверяет CSV/manifest) |

Ограничения §9: только T2 h=0, только strict post-2004, медиана-уровни → growth; T1/T2 h>0 не
мапятся; pre-2004 base values ненадёжны (X_BP caveat).

## 4. PIT-правила (как не заглянуть в будущее)

1. **Дата публикации, а не дедлайн.** Прогноз входит в scorecard, только если его дата публикации
   `≤ origin` (`origin_policy.default`). Для SPF это официальная дата новостного релиза квартала
   опроса (`src/amfe/ensemble/spf_calendar.py`, docs/86 §5), не дата дедлайна. Для NY Fed — колонка
   `Forecast Date` (пятничная публикация nowcast), не дата квартала. Для Livingston —
   `release_date` из официального `livingston-release-dates.xlsx`: `Exact`, иначе `Not After`
   (консервативная верхняя граница публикации); дата берётся из RGDPX/порядка строк, а не из
   `Survey`-года (который до 2001 сдвинут на +100).
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
| **Livingston** | **T2 h=0** | **360** | **32** | **0.089** | **6 из 204** | **26 из 156** |

SPF h=0: 240 — origin не средний месяц квартала, 2 — до 1990Q3, 1 — релиз 2019Q1 (22.03.2019,
shutdown) позже февральского конца месяца. Это в точности числа docs/95 §2 — независимое
подтверждение, что календарное правило реализовано так же, как в sensitivity-инструменте.
NY Fed: в W1 нет ни одной PUBLISHED-строки (144 `NO_PUBLICATION_BEFORE_ORIGIN` + 60
`RECONSTRUCTED_NOT_REAL_TIME`); в W2 108 начал отброшены как `RECONSTRUCTED_NOT_REAL_TIME`
(2007–2015) — реально скорится только 2016–2019 (48 начал). Это **не** полное покрытие W2 и не
«теперь у нас PIT на 2007–2019».
Livingston: survey semiannual; из 204/156 начал только 30 июня/31 декабря являются квартальными
концами — остальные отброшены как `LIVINGSTON_NO_QUARTER_END_ORIGIN` (170 в W1, 130 в W2), без
интерполяции; в W1 ещё 28 начал до strict-старта отброшены как `LIVINGSTON_BASE_VALUE_CAVEAT`.
Итог — 6 точек 2004–2006 в W1 и 26 в 2007–2019 W2; покрытие 0.089, а не «весь период».

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

### 6.3 Livingston Survey (новое, task-88)

| Цель/h | Окно | n | RMSE | MAE | bias | RMSE (secondary) | coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| T2 h=0 | W1 | **6** | **1.0168** | 0.7888 | +0.7888 | 1.0496 | 0.0294 |
| T2 h=0 | W2 | **26** | **1.2823** | 1.0472 | −0.0995 | 1.3531 | 0.1667 |
| T2 h=0 | W1+W2 | 32 | 1.2369 | 0.9988 | +0.0671 | 1.3016 | 0.0889 |

Livingston — semiannual survey; строгий поднабор 2004Q2–2019Q4. RMSE 1.24 на 32 точках — это
**не** рейтинг против SPF/NY Fed: разные выборки (W1 у Livingston — только 6 точек 2004–2006,
W2 — 26) и другая частота. Значения в отчёт не публикуются; forecast — документированное
преобразование median-уровней `400·ln(RGDPX_ZM/RGDPX_BP)`, не отдельно опубликованный median growth.
Расширить нельзя без протокольного решения: T1/T2 h>0 не мaпятся, pre-2004 исключён из-за caveat.

## 7. Что не посчитано и почему (честно)

| Метрика | Статус | Причина |
|---|---|---|
| CRPS | `NO_PREDICTIVE_DISTRIBUTION` | SPF, NY Fed и Livingston публикуют точечный прогноз/медиану, а не плотность; распределение не подставляется (docs/95 §6.4: «SPF-распределение — производная статистика проекта», другой объект) |
| Brier | `NO_EVENT_PROBABILITY_MAPPING` | ни у одного источника нет вероятности протокольного события; SPF RECESS/Anxious Index — вероятность падения GDP, а не событие NBER (docs/86: `NO_NBER_RECESSION_PROBABILITY_AND_EVENT_MISMATCH`) |
| coverage_90 | `NO_PREDICTIVE_DISTRIBUTION` | нет 5–95% интервала у точечного прогноза |
| Покрытие (`coverage_share`) | **AVAILABLE** | SPF 0.325 (h=0), 0.983 (h=1); NY Fed 0.133 (h=0, W1+W2), 0.308 (W2); Livingston 0.089 (h=0, W1+W2) — это доля покрытых начал, не точность |

CRPS и Brier не «выключены навсегда»: обе функции реализованы (`crps_from_quantiles`,
`brier_score`), проверены на синтетике и включатся автоматически, как только появится источник с
плотностью/вероятностью события, разрешённый реестром.

## 8. Критерии: docs/173 MVP и M2 docs/207

| Критерий | Требуется | Факт | Проходит |
|---|---|---:|---:|
| MVP docs/173: источников со scorecard | ≥ 3 | **3** (SPF, NY Fed, Livingston) | **да** |
| M2 docs/207: scorable источников | ≥ 2 | **3** | **да** |
| Общих PIT-точек | ≥ 50 | **313** | да |
| Даты публикации и ошибки в отчёте | есть | есть | да |
| Без нарушения лицензий | обязательно | 0 RESTRICTED-источников использовано, 0 значений опубликовано | да |

Отсюда `status = OK`, `status_reasons = []`. Это закрывает и M2 docs/207, и MVP docs/173: третий
scorable — Livingston (32 точки), не GDPNow (остаётся RESTRICTED), не Cleveland-«примерно» и не
реконструкции NY Fed, выданные за real-time. Livingston добавлен в строгом виде: только T2 h=0,
только post-2004 strict subset, median-уровни → q/q annualised growth — документированное
преобразование, значения не публикуются. Ограничения §9 сохраняются.

В отчёте это видно машинно: `criteria.min_scored_sources = 3`,
`criteria.m2_min_scored_sources = 2`, `cross_source.criteria_pass = {"sources": true,
"points": true, "m2_sources": true}`, `cross_source.scored_points_total = 313`.

## 9. Ограничения

1. **MVP-объём:** только T2_GDP_GROWTH: SPF h=0/h=1, NY Fed h=0, Livingston h=0 (strict
   post-2004). T1/T3 и h=2/h=4 не скорились.
2. **SPF — последняя редакция файла, не винтаж:** дата релиза фиксирует доступность, но errata и
   ревизии после опроса не аудированы (та же оговорка, что в docs/95 §6.3).
3. **NY Fed — values-only архив:** нет underlying series → ре-оценка невозможна; 2002Q1–2015Q4 —
   реконструкции, не публикации; архив заканчивается 2021Q3, поэтому W2 покрыт только 2016–2019.
   Это PIT-архив **частично**, и отчёт не выдаёт его за полный.
4. **Livingston — только T2 h=0 и только strict post-2004:** semi-annual survey, в W1 всего 6 точек
   (2004–2006), в W2 — 26; 2020+ не входят в окна, pre-2004 исключён из-за caveat к X_BP; T1/T2 h>0
   не мaпятся. Forecast `400·ln(RGDPX_ZM/RGDPX_BP)` — документированное преобразование median-уровней,
   а не отдельно опубликованный median growth; это ограничение нельзя скрывать при сравнении.
5. **Нет плотности/вероятности** у доступных источников → CRPS/Brier/coverage_90 пусты (см. §7).
6. **Перекрывающиеся начала h=0:** 117 точек SPF = ~39 независимых кварталов, 48 NY Fed = 12
   кварталов 2016–2019, 32 Livingston = 16 полугодий 2004–2019; значимость (DM/RW) в этом MVP не
   считается, и отчёт это пишет.
7. **«Внешние vs наши» ещё нет:** в отчёт попадают только внешние прогнозы; присоединение своих
   базлайнов (C6-журнал, R3-ансамбль) — отдельный шаг, зафиксирован в `gaps`.
8. **Данные только из локальных артефактов.** Без `data/raw/spf/spf_median.json` инструмент честно
   пишет `NOT_AVAILABLE / SPF_VALUES_NOT_DOWNLOADED`; без `data/raw/ny_fed/nowcast.csv` —
   `UNAVAILABLE / ARCHIVE_NOT_LOCAL`; без `data/raw/livingston/livingston_t2_h0.csv` —
   `UNAVAILABLE / LIVINGSTON_VALUES_NOT_DOWNLOADED`; ни один источник не фабрикуется.
   `tools/fetch_scorecard_data.py` восстанавливает NY Fed-архив и Livingston (--livingston),
   `tools/fetch_spf.py` — SPF.
9. **Не admission-инструмент:** C1–C6, стадии и допуск не применяются; отчёт диагностический.

## 10. Дальнейшие шаги (третий источник уже добавлен)

1. **«Внешние vs наши»:** добавить к тем же началам свои OPEN-базлайны (C6 forward-registry и
   R3-ансамбль) — это сделает UC-44 продуктом, а не диагностикой.
2. **Расширение целей:** T1/T3 и T2 h=2/h=4 — только при появлении источника с точным
   протокольным мэппингом или отдельном протокольном решении (FROZEN v5 не меняется этой задачей).
3. **Livingston:** расширять нельзя без решения о серии/сезонности для T1 и без снятия caveat к
   pre-2004 X_BP; текущий строгий вариант — 32 точки, и это надо оставлять явным.
4. **GDPNow/Cleveland:** GDPNow остаётся RESTRICTED (значения не читаются); Cleveland — без
   PIT-архива и протокольного мэппинга, UNAVAILABLE.
5. **API-слой:** после ≥3 источников отдать отчёт как `/v1/external-forecasts` (docs/174, UC-45).

## 11. Воспроизведение и CI

```powershell
# 1) открытые данные (значения — только в git-ignored data/raw/)
python tools/fetch_scorecard_data.py --expect-sha256 b0ce3db1592672375e3bd43d827da6cf512fbbb602252abb927e6b6d577c234f
python tools/fetch_scorecard_data.py --livingston
python tools/fetch_scorecard_data.py --check
python tools/fetch_scorecard_data.py --livingston --check
# SPF (если ещё не загружен): python tools/fetch_spf.py

# 2) scorecard
python tools/external_forecast_scorecard.py --check            # метаданные + доступность (без фактов)
python tools/external_forecast_scorecard.py                    # полный прогон, пишет отчёт
python tools/external_forecast_scorecard.py --horizons 0 --max-origins 24 --out <tmp>.json

# 3) тесты
python -m unittest tests.test_external_forecast_scorecard tests.test_external_forecast_scorecard_fetch tests.test_external_scorecard_v2
```

`--check` возвращает 0 только при `status = OK`; сейчас он ожидаемо возвращает **0** (3 scorable,
313 доступных точек). Полный прогон возвращает 0 и печатает сводку с `content_sha256`;
`--max-origins` не используется для отчёта (иначе NY Fed не попадает в первые 30 начал W2 и источник
может выглядеть пустым).

Детерминизм: отчёт не зависит от времени (`generated_at` исключён из `content_sha256`), порядок
обхода начал и источников фиксирован, средние считаются через `math.fsum`; тесты проверяют
совпадение digest'а у двух прогонов и независимость от `generated_at`. CSV NY Fed детерминирован:
повторный `fetch_scorecard_data.py` даёт тот же sha256 `11cb535c…` при том же xlsx; Livingston CSV
детерминирован: повторный `fetch_scorecard_data.py --livingston` даёт sha256 `f2d72a83…` при тех же
xlsx.

Сырые данные: SPF-значения — в git-ignored `data/raw/spf/spf_median.json`; NY Fed xlsx/CSV — в
`data/raw/ny_fed/`; Livingston — `data/raw/livingston/`; архивы ALFRED (`data/raw/GDPC1/vintages.zip`)
только читаются. Инструменты пишут единственный tracked-файл scorecard —
`protocols/external_forecast_scorecard.json`; в git попадают только агрегаты, даты, пути и SHA-256.

## 12. Файлы

| Файл | Размер | SHA-256 / примечание |
|---|---|---|
| `tools/external_forecast_scorecard.py` | 83 028 B | `8615864d51054017…`; добавлены Livingston spec/adapter, strict post-2004, T2 h=0 |
| `tools/fetch_scorecard_data.py` | 37 550 B | `2e6fe7ab58c6b6a6…`; NY Fed + `--livingston` (два xlsx → CSV `livingston_t2_h0.csv`), manifest, `--check` |
| `protocols/external_forecast_scorecard.json` | 40 548 B | sha256 `7386e1a8600f3a6f…`; `content_sha256` `fd038a777a430a7e…`; `generated_at` 2026-11-26T19:48:03Z |
| `tests/test_external_forecast_scorecard.py` | 34 716 B | 48 тестов (`Ran 48 tests OK`) |
| `tests/test_external_forecast_scorecard_fetch.py` | 16 041 B | 14 тестов (`Ran 14 tests OK`) |
| `tests/test_external_scorecard_v2.py` | 23 365 B | 49 тестов (`Ran 49 tests OK`) |
| `protocols/evaluation_protocol_v5.json` | FROZEN | sha256 `48c2f6a18a28cefc…` (в отчёте — `protocol.sha256`) |
| `protocols/licence_register.json` | 50 922 B | sha256 `f660ee11fb31bcd4…` (в отчёте — `licence_register.sha256`); добавлена запись `LIVINGSTON_SURVEY` |
| `protocols/external_forecasts_v2.json` | 43 821 B | sha256 `1bf389f7f7087110…`; 27 источников, 3 `ADAPTER_READY` |

Новые данные (git-ignored, не коммитятся): `data/raw/ny_fed/nowcast.csv` (64 203 B, sha256
`11cb535c…`), `data/raw/ny_fed/nowcast.meta.json`; `data/raw/livingston/medians.xlsx` (162 457 B,
`ddb2a9f7…`), `livingston-release-dates.xlsx` (22 357 B, `22d621f3…`),
`livingston_t2_h0.csv` (3 079 B, `f2d72a83…`), `livingston.meta.json` (URL/дата/размер/sha256/counts);
`data/raw/scorecard_fetch_log.jsonl` (история загрузок).

## 13. Попытка третьего scorable источника: ECB SPF / OECD EO / IMF WEO (2026-11-26)

> Историческая запись: на момент этой попытки (task-82) третьего scorable не было, scorecard = 2
> источника / 281 PIT-точка. Позже (task-88) третий источник найден среди других кандидатов —
> **Livingston Survey**, см. §14; текст ниже оставлен как есть для аудита, его цифры относятся
> именно к той попытке.

Правило владельца — без платных лицензий; третий scorable нужен как замена Consensus/Blue Chip
(RESTRICTED). Проверены открытые машиночитаемые каналы; **ни одно значение не скачивалось в проект и
не читалось**, scorecard остаётся 2 scorable. Это не юридическая консультация.

| Кандидат | Что проверено (сеть, 2026-11-26) | Факт | Маппинг на protocol v5 | Итог |
|---|---|---|---|---|
| **ECB SPF** | страница SPF: HTTP 200, 108 659 B (sha256 `f5cbb45b…`); ECB Data Portal API dataflow `ECB:SPF(1.0)`, structure `ECB_FCT1`: HTTP 200, 1 009 B XML | опрос: инфляция, реальный ВВП и безработица **еврозоны**, горизонты «текущий год … длиннее», квартальные раунды и **годовые** горизонты; лицензия `OPEN_ATTRIBUTION`, terms-страница 200; записи в `licence_register` нет → значения не читались | T2 = **US** GDPC1 q/q annualised, T1 = **US** CPI-U y/y, T3 = NBER US: еврозона и HICP — другой объект/география; преобразование = смена цели (protocol decision) → `NONE` | **UNAVAILABLE / `NO_PROTOCOL_TARGET_MAPPING`**, не scorable |
| **IMF WEO** | data mapper `NGDP_RPCH/USA`: HTTP 200, 121 784 B; IMF SDMX dataflow endpoint: HTTP 200 | значения **годовые**, в payload нет даты публикации/винтажа; WEO-раунды в проекте не загружены | протокол требует квартальный q/q annualised; интерполяция годовых значений выдала бы выдуманную величину → `NONE` | **UNAVAILABLE / `NO_PROTOCOL_HORIZON_MAPPING`**, не scorable |
| **OECD EO** | `sdmx.oecd.org` public SDMX: HTTP 406 с перечнем допустимых форматов (`structure`, `xml-structure-3.0.0`, `sdmx-3.0`, `json-structure-2.0.0`) — сервис жив, нужна согласовка формата | EO — ближайший кандидат (квартальные проекции для членов OECD), но локального архива, publication dates и зарегистрированного преобразования нет | `PENDING`; без архива публикаций PIT невозможен | остаётся **NOT_FETCHED / `PUBLIC_ARCHIVE_NOT_FETCHED`**, пока не scorable |

CBO/FOMC SEP/World Bank GEP/EC ECFIN/BoE и остальные 22 источника — в прежних статусах реестра v2
(annual/cadence или mapping PENDING); в этой попытке не перепроверялись.

**Итог:** третий scorable законно **не получен**: scorecard = **2 источника** (SPF 233 + NY Fed 48 =
281 PIT-точка), `status = PARTIAL`, `status_reasons = ["SCORED_SOURCES_BELOW_CRITERION(2<3)"]`.
`protocols/evaluation_protocol_v5.json`, `protocols/licence_register.json`, PIT-инварианты и
`protocols/external_forecast_scorecard.json` **не менялись** (нового scorable нет, отчёт не
перегенерировался). Реестр `protocols/external_forecasts_v2.json` обновлён: 26 источников,
2 `ADAPTER_READY`, 10 `UNAVAILABLE`, 8 `RESTRICTED`, 6 `NOT_FETCHED`; валидация — **0 errors / 0 warnings**.

**Что разблокирует третий источник:** (1) протокольное решение о целях еврозоны/годовых горизонтах —
это изменение frozen v5, отдельное решение владельца (сейчас не принималось); (2) публичный
machine-readable **PIT-архив квартальных прогнозов США** с датами публикаций (исторические Greenbook/
аналоги — вне доступного в этой задаче архива); (3) для OECD — загрузка архива проекций с
publication dates и регистрация документированного преобразования цели. Без этого — «честные 2, а не
фиктивные 3» (docs/173 §3).

**Тесты:** `tests/test_external_scorecard_v2.py` дополнен проверками статусов ECB SPF/IMF WEO и
наличием этого раздела; `tests/test_external_forecast_scorecard*.py` не менялись и остаются зелёными.

## 14. Livingston Survey — третий scorable (task-88, 2026-11-26)

Третий источник найден не среди ECB/OECD/IMF (§13), а в **Philadelphia Fed Livingston Survey**:
это публичный PIT-архив median-прогнозов с 1946 года, из которого документированным
преобразованием получается протокольная T2 h=0 (`400·ln(RGDPX_ZM/RGDPX_BP)`), а даты публикации
берутся из официального `livingston-release-dates.xlsx` (`Exact`, иначе `Not After`). OSINT-разбор
кандидата — [`docs/254`](254_PIT_кандидат_NYFed_SMP.md); здесь — реализованная часть.

**Что сделано (task-88):**

1. `tools/fetch_scorecard_data.py --livingston`: скачивает `medians.xlsx` (162 457 B, sha256
   `ddb2a9f7…`) и `livingston-release-dates.xlsx` (22 357 B, sha256 `22d621f3…`) в git-ignored
   `data/raw/livingston/`, деривирует `livingston_t2_h0.csv` (45 strict post-2004 строк; 3 079 B,
   sha256 `f2d72a83…`) с колонками
   `survey_date,release_date,release_basis,origin,target_period,horizon,forecast`; пишет
   `livingston.meta.json` (URL, дата, bytes, sha256, счётчики, no row values) и поддерживает
   `--check`.
2. `tools/external_forecast_scorecard.py`: source spec `LIVINGSTON_SURVEY` + adapter
   `livingston_local` (target T2, horizons [0]); PIT: `publication_date = release_date` и только
   `release_date ≤ origin`; причины `LIVINGSTON_NO_QUARTER_END_ORIGIN`,
   `LIVINGSTON_RELEASE_AFTER_ORIGIN`, `LIVINGSTON_BASE_VALUE_CAVEAT`,
   `LIVINGSTON_NO_SURVEY_FOR_ORIGIN`, `LIVINGSTON_VALUES_NOT_DOWNLOADED`; другие месячные концы не
   интерполируются.
3. `protocols/licence_register.json`: запись `LIVINGSTON_SURVEY` — **OPEN** (research use,
   attribution, values outside git; якорь `PHILADELPHIA_FED`).
4. `protocols/external_forecasts_v2.json`: `LIVINGSTON_SURVEY` → `ADAPTER_READY`
   (`SCORED_IN_CURRENT_SCORECARD`), adapter `livingston_local`, data_path
   `data/raw/livingston/livingston_t2_h0.csv`; реестр 27 источников, 3 `ADAPTER_READY`, 0 errors /
   0 warnings.
5. `protocols/external_forecast_scorecard.json` перегенерирован: **3 scorable**, **313 точек**,
   `status = OK`, `status_reasons = []` (SPF 233 + NY Fed 48 + Livingston 32). Значения Livingston
   в отчёт не попадают (`values_published = false`, `sample = null`); в отчёт идут только counts,
   RMSE/MAE/bias/coverage и метаданные.
6. Тесты: scorecard +4 Livingston-теста (fixture/PIT/reason-коды), fetch +4 (parse/CSV/manifest/
   `--check`), v2 обновлён на 27 источников/3 ADAPTER_READY; суммарно `Ran 111 tests OK` по трём
   модулям.

**Ограничения Livingston (не скрывать):** только T2 h=0; только strict post-2004 (pre-2004 `X_BP`
по документации ненадёжен, кроме CPI); survey semiannual — в W1 всего 6 точек (2004–2006), в W2 26
(2007–2019); forecast — документированное преобразование опубликованных median-уровней, а не
отдельно опубликованный median growth; T1/T2 h>0 не мaпятся; значения третьей стороны лежат только
в git-ignored `data/raw/livingston/` и не публикуются.

