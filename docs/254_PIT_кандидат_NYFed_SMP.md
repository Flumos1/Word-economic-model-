# 254. PIT-кандидат №3: NY Fed SMP / Livingston Survey / Cleveland nowcasting

Дата: 2026-11-26. Задача: `task-86`. Роль: продукты/релизы (OSINT/PIT-исследование). Цель —
найти третий **scorable** источник для PIT-scorecard (сейчас 2: SPF 233 точки + NY Fed Staff
Nowcast 48 точек, docs/174). Правило владельца: без платных лицензий; только открытые данные;
PIT строго — значение входит в scorecard, только если дата публикации ≤ origin. Никаких
реконструкций и интерполяций; значения не публикуются в отчёте (только метаданные/агрегаты).
Изменять `tools/`, `protocols/`, `tests/`, docs/174 в этой задаче нельзя — адаптер/реестр
внедряет Lead.

**Итог: третий scorable источник найден — Livingston Survey (Philadelphia Fed) для
`T2_GDP_GROWTH`, горизонт h=0.** Строгий PIT-поднабор: **32 точки** (2004Q2–2019Q4; W1 6,
W2 26, все с точными датами релиза). Более широкий вариант — 55 точек на W1+W2 (с оговоркой
о base-values до 2004) — возможен, но не рекомендуется как строгий. NY Fed SMP/SME и Cleveland
Inflation Nowcasting — **NOT_SCORABLE** под текущий протокол v5 (причины ниже; NY Fed —
потенциал только при смене события/цели протокола, Cleveland — нет PIT-архива/мэппинга).

## 1. Сводка

| Кандидат | PIT-архив | Частота | Маппинг протокола v5 | Точек | Вердикт |
|---|---|---|---|---|---|
| **Livingston Survey** (Philadelphia Fed) | 161 выпуск 1946-06…2026-06; даты релизов отдельным XLSX (exact/not-after) | 2 раза в год (июнь/декабрь) | **T2_GDP_GROWTH h=0** (q/q annualised; из median-уровней RGDPX_ZM/RGDPX_BP) | **32 строгих** (2004Q2–2019Q4; опционально 55 на W1+W2) | **SCORABLE** (T2 h=0) |
| **NY Fed Survey of Market Participants / Survey of Market Expectations (SMP/SME)** | XLSX-данных 27 выпусков 2023-07…2026-10; PDF результатов с 2011; даты публикации результатов в машиночитаемом виде отсутствуют | ~ежемесячно/квартально (SMP 2014–2024, SME с 2025) | T2 — Q4/Q4 annual, не q/q annualised; T3 — «быть в NBER-рецессии сейчас/через 6 мес», не «хотя бы один NBER-месяц в интервале» | 27 выпусков × вероятности; но маппинга нет | **NOT_SCORABLE** (`NO_PROTOCOL_TARGET/EVENT_MAPPING` + нет PIT-даты публикации в данных) |
| **Cleveland Fed Inflation Nowcasting** | chart-JSON с 161 месячным блоком (2013-07…2026-11), дневные nowcast-значения; единый build-комментарий 2026-11-25, без PIT-таймстемпов публикаций | ежедневно | T1 — y/y CPI на t−1+h; источник даёт m/m (и отдельные y/y/quarterly-графики), но серия/архив не PIT и не CPIAUCSL SA | — | **NOT_SCORABLE** (`NO_PROTOCOL_HORIZON_MAPPING`, `NOT_A_PIT_ARCHIVE`) |

Значения всех источников — только в git-ignored `data/raw/` (скачаны для проверки, не
коммитятся); в этом документе нет ни одного фактического прогноза/nowcast-значения — только
даты, размеры, хеши, счётчики, структура файлов и формулы преобразования.

## 2. Метод и PIT-правила

1. Прямые HTTPS GET к официальным страницам/файлам (UA `AMFE-research/1.0`), фиксация `status`,
   `Content-Type`, bytes, sha256; сырьё — в `data/raw/pit_candidates/` (git-ignored).
2. Дата публикации важнее дедлайна: для Livingston — `Exact`/`Not After` из официального
   `livingston-release-dates.xlsx`; `Not After` используется как консервативная верхняя граница
   (публикация не позже неё), поэтому origin обязан быть ≥ этой даты; для NY Fed — нет даты
   публикации результатов в XLSX (только `survey_release_date` — дата рассылки анкеты); для
   Cleveland — нет PIT-таймстемпов.
3. Без реконструкций/интерполяций: не заполняем пропуски, не восстанавливаем квартальные пути из
   годовых, не превращаем m/m в y/y, не «достраиваем» недостающие релизы. Преобразование
   median-уровней в протокольную q/q growth — формула 400·ln(X_ZM/X_BP), применённая к
   опубликованным median-уровням (это transformation, а не реконструкция; см. §4).
4. Окна/цели — из FROZEN `protocols/evaluation_protocol_v5.json`: W1 1990–2006, W2 2007–2019;
   T2 = `400·ln(GDPC1_q/GDPC1_{q−1})`, h=0 (nowcast текущего квартала; origin — конец месяца).
5. Значения не публикуются: в отчёт пойдут ошибки/агрегаты/покрытие; сырьё — только data/raw.

## 3. NY Fed SMP / SME: NOT_SCORABLE

### 3.1 Что доступно (проверено 2026-11-26)

| URL | HTTP | Байт | SHA-256 (первые 16) |
|---|---|---|---|
| `https://www.newyorkfed.org/research/survey-of-market-participants` (URL из задачи) | 200 | 101 158 | `cff20b8bd63f538e` |
| `https://www.newyorkfed.org/markets/market-intelligence/survey-of-market-expectations` — реальный архив SMP/SME | 200 | 218 450 | `5a495915d90b12eb` |
| `…/medialibrary/media/markets/survey/2026/oct-2026-data.xlsx` | 200 | 175 778 | `1cceb9bf327b9de1` |
| `…/2024/dec-2024-data.xlsx` | 200 | 201 462 | `96a30aa567d399b9` |
| `…/2023/sep-2023-data.xlsx` | 200 | 164 030 | `6a89cc544fc2974b` |
| `…/2026/oct-2026-sme-results.pdf` | 200 | 248 471 | `38c0c20e010c4652` |
| `…/2026/oct-survey-sme.pdf` | 200 | 314 979 | `777459e24227d95f` |

Важное расхождение: URL из задачи отдаёт 200, но это **навигационная страница без архива SMP**
(в HTML нет ссылок на SMP-релизы); рабочий архив — страница `Survey of Market Expectations` (SME):
она объединила Survey of Primary Dealers (2002–2024) и Survey of Market Participants (2014–2024),
начиная с 2025 — SME. На странице — PDF вопросников и результатов (2011–2026) и **27 XLSX-файлов
данных** (2023-07…2026-10).

### 3.2 Структура данных

XLSX — длинная таблица на «Sheet1», 21 колонка: `survey_release_date`, `survey_due_date`,
`panel_type`, `spd_question_number`, `theme`, `subject_group`, `subject`, `question_type`,
`question_mode`, `question_text`, `question_tag`, `value_tag`, `top_header_value`,
`left_header_value`, `horizon`, `horizon_date`, `bucket_range`, `bucket_low`, `bucket_high`,
`aggregation`, `aggregation_value`. Агрегирование: `count`, `pctl25`, `pctl50`, `pctl75` (median —
`pctl50`). Релевантные вопросы:

- `usrecession_prob_current` / `usrecession_prob_6months` — «What percent chance do you attach to
  the U.S. economy currently being in a recession* / being in a recession* in 6 months?»;
  сноска в PDF: **`*NBER-defined recession`**;
- `realgdpgrowth_modalpe_2026/2027` (Q4/Q4 annual growth), `unemploymentrate_*`, `headline_cpi`,
  `core_pce` и т.д.

### 3.3 Почему не scorable

1. **T2 не мaппится:** в данных — годовой рост **Q4/Q4** (календарные 2026/2027), а протокол T2 —
   **q/q annualised** (`400·ln(GDPC1_q/GDPC1_{q−1})`) на квартальных горизонтах 0/1/2/4. Пересчёт
   годового Q4/Q4 в квартальный путь — реконструкция/интерполяция, запрещена; смена цели тоже
   запрещена без протокольного решения.
2. **T3 не мaппится по событию:** вопрос — вероятность «быть в NBER-рецессии» **в точке** (сейчас/
   через 6 месяцев). Протокол T3 — «хотя бы один NBER-месяц в интервале (t, t+h]» (h=3/6/12);
   это разные события (можно войти и выйти из рецессии внутри интервала). Подмена события
   запрещена; это тот же класс проблемы, что SPF Anxious Index (`NO_EVENT_PROBABILITY_MAPPING`,
   docs/174 §7).
3. **PIT-дата публикации не машиночитаема:** `survey_release_date` в XLSX — дата **рассылки
   анкеты** (например, 2026-10-14), `survey_due_date` — дедлайн (2026-10-19); результаты
   публикуются «примерно через три недели после каждого заседания FOMC, после публикации
   протоколов» (страница SME), т.е. **позже** этих дат. Колонки publication/release date результата
   в XLSX нет; PDF результатов содержит только «Distributed / Received by», не дату публикации.
   Использовать дату рассылки как дату публикации нельзя — это исказит доступность; «вычислить»
   дату публикации по правилу FOMC — реконструкция. Для строгого PIT нужны официальные
   даты публикации каждого релиза (ручной сбор или новый машиночитаемый источник).
4. Даже при будущем протокольном решении: машиночитаемый архив данных — только **27 выпусков
   2023–2026** (до 2023 — лишь PDF); одна `usrecession_prob_6months` на выпуск дала бы ~27 точек,
   `realgdpgrowth` — 2 годовых значения на выпуск, но это не цель протокола.

**Вердикт: NOT_SCORABLE** для текущего протокола v5. Потенциал — только если владелец/Lead
зарегистрирует новую цель/событие (например, «вероятность быть в NBER-рецессии через 6 месяцев» или
годовой Q4/Q4 GDP) и появится официальный машиночитаемый календарь дат публикаций. Это изменение
FROZEN протокола, отдельное решение; в этой задаче не предлагается.

## 4. Livingston Survey: SCORABLE (T2_GDP_GROWTH, h=0)

### 4.1 Источники и файлы (проверено 2026-11-26)

| URL | HTTP | Байт | SHA-256 (первые 16) |
|---|---|---|---|
| `https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-survey` | 200 | 48 210 | `3644c66cf58f06d7` |
| `…/livingston-historical-data` | 200 | 31 859 | `b444f78a271dc902` |
| `…/historical-data/medians.xlsx` | 200 | 162 457 | `ddb2a9f7ed51c296` |
| `…/historical-data/MedianGrowthRate.xlsx` | 200 | 91 887 | `ce6dcfb0b2dfe7e6` |
| `…/livingston-release-dates.xlsx` | 200 | 22 357 | `22d621f3b2e00694` |
| `…/livingston-documentation.pdf` | 200 | 423 974 | `e85138f8185220be` |

Архив: **161 выпуск** 1946-06 … 2026-06 (2 раза в год, июнь/декабрь). `medians.xlsx` — 16 листов
переменных; нужен лист **`RGDPX`** (Real GDP, billions of real dollars, SA, annual rate) с
колонками `Date`, `RGDPX_BP`, `RGDPX_ZM` (уровни). `MedianGrowthRate.xlsx` — производные темпы
роста; для протокола он не используется напрямую (см. §4.3). Даты релизов — отдельный
`livingston-release-dates.xlsx` (лист `Dates`): 161 строка, `Exact` — 53, `Not After` — 107
(+1 пустая служебная); для каждой анкеты есть либо точная дата, либо верхняя граница.

### 4.2 PIT-правила и покрытие

- **Дата публикации:** `Exact`, если она есть; иначе `Not After` (официальная верхняя граница:
  публикация не позже). Origin обязан быть ≥ publication_date; значения с публикацией позже
  квартального конца не используются.
- **Origin:** конец квартала выпуска: **30 июня** (Q2) или **31 декабря** (Q4) — это месяц 3
  текущего квартала, допустимый h=0 origin по протоколу.
- **Цель:** `T2_GDP_GROWTH`, h=0, target period = квартал выпуска (`YYYYQ2`/`YYYYQ4`).
- **Формула:** `forecast = 400 · ln(RGDPX_ZM / RGDPX_BP)`, где `RGDPX_BP` — последний известный
  исторический квартальный уровень (для июньской анкеты — Q1, для декабрьской — Q3; официальная
  таблица base values в документации), `RGDPX_ZM` — медианный прогноз уровня на квартал анкеты
  (Q2/Q4). Это в точности протокольная q/q annualised-формула, применённая к опубликованным
  median-уровням; никаких пропусков/интерполяций.
- **Actual:** только первая публикация GDPC1 (ALFRED), как в текущем scorecard; actual должен быть
  опубликован строго после origin.

Подсчёт (по 161 выпуску, зип по порядку строк `RGDPX` ↔ `Dates`, см. §4.4):

| Срез | Выпусков | PIT-точек h=0 | Из них exact / not-after | W1 | W2 |
|---|---:|---:|---:|---:|---:|
| All usable (1946–2026) | 161 | 68 | 53 / 15 | — | — |
| **W1+W2 (1990–2019)** | **60** | **55** | **40 / 15** | **29** | **26** |
| **Strict post-2004 (рекомендуемый)** | **32** | **32** | **32 / 0** | **6** | **26** |

Строгий поднабор 2004Q2–2019Q4: все 32 выпуска имеют точные даты релиза; первые данные — 2004Q2,
последние — 2019Q4. Покрытие: 32/360 eligible h=0 origins = **8,9%** (W1 6/204 = 2,9%,
W2 26/156 = 16,7%). Более широкий вариант 55 точек (1992Q2–2019Q4; 15 релизов с `Not After`) —
только с оговоркой о base values до 2004 (см. §4.5); 3 выпуска W1+W2 без уровней и 2 с релизом
позже квартального конца отброшены.

### 4.3 Почему не другие цели/горизонты

- **T2 h=2/h=4 не маппятся:** в `RGDPX` есть уровни на 6 и 12 месяцев (Q4/Q2 следующего года), но
  протокольный h=2/h=4 требует q/q growth целевого квартала, т.е. уровня соседнего квартала
  (Q3/Q1), которого в файле нет; восстанавливать его из двух точек или годовых значений —
  интерполяция/реконструкция, запрещено.
- **T1 не маппится без смены цели:** Livingston даёт уровни CPI, а протокол T1 — y/y CPIAUCSL
  (SA) и CPILFESL (SA); пересчёт уровня в y/y требует и базы, и решения о серии/сезонности
  (CPI в Livingston — SA только с декабря 2004; до этого NSA), а горизонты 6/12 месяцев не
  совпадают с сеткой h=1/3/6/12. Это смена цели/серии, а не маппинг, поэтому T1 не заявляется.
- `MedianGrowthRate.xlsx` содержит `G_BP_To_ZM`, но его формула — `((X_ZM/X_BP)^4 − 1)·100`
  (арифметический annualized percent), а протокол T2 определён как `400·ln(...)`; прямое
  использование этой колонки было бы подменой цели. Адаптер должен считать протокольную формулу
  из median-уровней; это надо явно задокументировать при регистрации.
- **Оговорка base values:** документация Livingston предупреждает, что `X_BP` для переменных,
  кроме CPI, до июня 2004 могут быть неточными (историческая база не совпадает с ревизиями).
  Поэтому строгий поднабор начинается с **июня 2004**; для 1992–2003 возможен только
  «широкий» вариант 55 точек с пометкой `LIVINGSTON_BASE_VALUE_CAVEAT` — Lead решает, включать ли
  его (я рекомендую strict 32).

### 4.4 Формат данных для адаптера (Livingston)

Сырьё (скачано в git-ignored `data/raw/pit_candidates/`, в git не коммитится):

```
data/raw/pit_candidates/livingston/medians.xlsx          # 162457 B, sha256 ddb2a9f7…, sheet RGDPX
data/raw/pit_candidates/livingston/MedianGrowthRate.xlsx  # справочно, не использовать как target
data/raw/pit_candidates/livingston_release_dates.xlsx     # 22357 B, sha256 22d621f3…, sheet Dates
data/raw/pit_candidates/livingston/livingston-documentation.pdf  # 423974 B, sha256 e85138f8…
```

**Важный дефект источника:** в `Dates` колонка `Survey` для выпусков **до 2001** содержит год
**+100** (1946 → 2046, 1986 → 2086, 1991 → 2091, 2000 → 2100), с 2001 — корректный год. Даты в
колонках `Exact`/`Not After` корректны. Поэтому сопоставлять надо **по порядку строк** (161 строка
`Dates` ↔ 161 строка `RGDPX`, одинаковый порядок), а не по году из `Survey`; `Date` из `RGDPX` —
авторитетная дата анкеты.

Рекомендуемый промежуточный CSV (git-ignored, создаёт fetch-инструмент; схема
`amfe.scorecard_fetch.livingston_t2h0.v1`), колонки:

```
survey_date,release_date,release_basis,origin,target_period,horizon,forecast
```

- `survey_date` — YYYY-MM-DD из `RGDPX.Date` (1 июня/1 декабря; без сдвига);
- `release_date` — `Exact`, иначе `Not After` (YYYY-MM-DD); PIT-safe верхняя граница;
- `release_basis` — `exact` или `not_after`;
- `origin` — `YYYY-06-30` / `YYYY-12-31` (квартальный конец после релиза; строки с
  release_date > quarter_end не включаются);
- `target_period` — `YYYYQ2` / `YYYYQ4` (квартал анкеты);
- `horizon` — `0`;
- `forecast` — `400*ln(RGDPX_ZM/RGDPX_BP)` (float, finite; значения только в data/raw);
- строки только с непустыми BP/ZM; никаких заполнений/интерполяций.

Адаптер (`source_id=LIVINGSTON_SURVEY`, adapter `livingston_local`, target
`T2_GDP_GROWTH`, supported horizons `[0]`):

- `status`: `AVAILABLE`, если CSV есть; иначе `UNAVAILABLE / LIVINGSTON_VALUES_NOT_DOWNLOADED`.
- `forecast(target=T2, origin, horizon=0)`: origin обязан быть 30 июня/31 декабря; иначе
  `reason=LIVINGSTON_NO_QUARTER_END_ORIGIN`; берётся выпуск, чей `origin` совпадает; PIT-гейт
  `publication_date ≤ origin`, иначе `reason=LIVINGSTON_RELEASE_AFTER_ORIGIN`; для strict-режима
  строки до 2004-06 отбрасываются с `reason=LIVINGSTON_BASE_VALUE_CAVEAT`.
- `provenance` — `PUBLISHED_MEDIAN_LEVELS_DERIVED_GROWTH` (в отчёт не попадают значения, только
  флаг/агрегаты).
- В отчёт — только `n`, coverage, RMSE/MAE/bias; значения Livingston не публиковать (условие
  research-use/values outside git; licence entry Philadelphia Fed).

### 4.5 Лицензия и провенанс

Страница Livingston — официальный сайт Philadelphia Fed; по `protocols/licence_register.json`
запись `PHILADELPHIA_FED` — `OPEN`, research use, «values outside git»; условия сайта
(`https://www.philadelphiafed.org/about-us/privacy-notice`) применимы и к Livingston. Рекомендую
завести отдельную запись `LIVINGSTON_SURVEY` (или расширить `PHILADELPHIA_FED` с URL Livingston и
условием values-outside-git), чтобы провенанс был аудируемым. terms-хеш новой записи фиксирует Lead
по действующему процессу; в этой задаче протоколы не менялись.

## 5. Cleveland Fed Inflation Nowcasting: NOT_SCORABLE

### 5.1 Что доступно

| URL | HTTP | Байт | SHA-256 (первые 16) |
|---|---|---|---|
| `https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting` | 200 | 198 314 | `9e2bbb0789fa70d4` |
| `…/media/files/webcharts/inflationnowcasting/nowcast_month.json` | 200 | 7 710 703 | `da8d3f57994fe296` |
| `…/nowcast_quarter.json` | 200 | 5 051 057 | `fb6ae671485b5dde` |
| `…/nowcast_year.json` | 200 | 7 670 143 | `33758ce12635017d` |

Страница — интерактивный график; **машиночитаемые данные есть** в chart-JSON. Структура (JSON-массив):
`month` — 161 блок (subcaption `2013-7`…`2026-11`), `quarter` — 54 блока (`2013:Q3`…`2026:Q4`),
`year` — 161 блок; в каждом блоке `chart` (y-axis: `Month-over-month percent change`,
`Quarterly Annualized Percent Change`, `Year-over-year percent change`), `categories` (дневные
метки вида `08/20`…, последняя `PCE Jul`/`PCE Oct`) и `dataset` (CPI Inflation, Core CPI Inflation,
PCE Inflation, Core PCE Inflation + Actual-серии). То есть это **текущий снимок графиков** с
историческими дневными nowcast-значениями, но:

- у всех блоков `chart._comment = 2026-11-25 00:00` — единый build-date, **нет PIT-таймстемпов
  публикации** и версий/винтажей; нельзя доказать, какое значение и когда было опубликовано;
- нет release-календаря/архива публикаций; JSON — рендер-кэш страницы, а не PIT-хранилище;
- значения — m/m (месяц), quarterly annualized (квартал) и y/y (год), но не по протокольным
  сериям/горизонтам: T1 — y/y CPIAUCSL/CPILFESL (SA) на t−1+h; Cleveland — nowcast текущего
  месяца CPI (сезонность/серия требуют решения), quarterly-график — инфляция, не GDP (T2);
  преобразование m/m→y/y — смена цели.

**Вердикт: NOT_SCORABLE** (`NO_PROTOCOL_HORIZON_MAPPING`; дополнительно `NOT_A_PIT_ARCHIVE`).
Это согласуется с docs/174 (§2: `UNAVAILABLE`, docs/174 §9). Для будущего T1-мэппинга нужно
отдельное решение о серии/сезонности и официальный PIT-архив с датами публикаций; в текущем
протоколе v5 ничего из этого нет.

## 6. Что сделать Lead (интеграция)

1. **Livingston adapter:** добавить в `tools/external_forecast_scorecard.py` source spec
   `LIVINGSTON_SURVEY` (adapter `livingston_local`, target T2, horizon 0, provenance
   `PUBLISHED_MEDIAN_LEVELS_DERIVED_GROWTH`, publication rule — release dates XLSX): `Exact` else
   `Not After`; strict-post-2004 option; reason-коды из §4.4.
2. **Fetch/derivation tool:** расширить `tools/fetch_scorecard_data.py` или отдельный
   `tools/fetch_livingston.py`: скачивать `medians.xlsx`/`livingston-release-dates.xlsx` в
   `data/raw/livingston/`, проверять SHA-256, собирать CSV §4.4 (LF, UTF-8, значения только
   data/raw; ни одно значение не коммитится; в отчёт — только counts/агрегаты).
3. **Лицензия:** запись `LIVINGSTON_SURVEY` (или расширение `PHILADELPHIA_FED`) в
   `protocols/licence_register.json`: OPEN, research use, values outside git; URL/страницы
   Philadelphia Fed + Livingston; терms-хеш — по процессу Lead.
4. **Реестр v2:** добавить `LIVINGSTON_SURVEY` в `protocols/external_forecasts_v2.json`
   (target GDP, frequency SEMIANNUAL, horizon protocol_h [0], mapping `DERIVED_EXACT`,
   publication `livingston-release-dates.xlsx`, licence OPEN, adapter `livingston_local`,
   status `ADAPTER_READY` после реализации, data_path
   `data/raw/livingston/livingston_t2_h0.csv`; notes — strict 32/55 counts и caveats §4).
5. **Scorecard/docs/174:** добавить третий источник, пересчитать `status` (2→3 scorable),
   обновить coverage/points и tests; отчёт сгенерировать заново; это меняет docs/174/scorecard —
   вне границ этой задачи, делает Lead.
6. **NY Fed/Cleveland:** статусы не менять (NOT_SCORABLE/UNAVAILABLE); при желании — отдельные
   owner-решения по изменению протокольного события/цели (T3/T2 annual) или по сбору дат
   публикаций, но в текущем v5 они не входят.

## 7. Что осталось невозможным / спорные места

- **Livingston:** только T2 h=0 (2 раза в год, 32 строгих точки; W1 — лишь 6 точек 2004–2006);
  T1/T2 h>0 нет; median-уровни → growth — документированное преобразование, а не отдельно
  опубликованный median growth; если Lead требует только «опубликованный» growth, источник
  придётся считать непригодным для T2 (это главный спорный пункт). Pre-2004 base caveat —
  поэтому рекомендую strict-post-2004; `Not After` даты (pre-2004) — консервативная верхняя
  граница, не «точная дата».
- **NY Fed SMP/SME:** machine-readable XLSX есть (27 выпусков 2023–2026), но нет публикационной
  PIT-даты в данных и нет маппинга T2/T3 в текущем протоколе; ранее 2023 — только PDF. Для
  третьего scorable не используется.
- **Cleveland:** chart-JSON машиночитаем, но это не PIT-архив (единый build-comment, нет дат
  публикаций/винтажей) и нет протокольного мэппинга (m/m vs y/y, CPI vs CPIAUCSL SA); остаётся
  NOT_SCORABLE.
- Значения всех источников в отчёте не публикуются; в этом документе — только metadata/aggregates;
  сырьё лежит в git-ignored `data/raw/pit_candidates/` (не коммит), реестр/allowlists/тесты/
  protocols и docs/174 не менялись; не коммичено.

## 8. Ссылки

- [docs/174](174_Внешние_прогнозы_scorecard.md) — текущий scorecard (2 источника: SPF 233 + NY Fed
  Staff Nowcast 48), PIT-правила и статусы источников.
- [docs/205](205_Верификация_источников.md) — статусы источников; [docs/224](224_Gated_источники.md) —
  классы доступа G0–G4.
- `protocols/evaluation_protocol_v5.json` (FROZEN) — T1/T2/T3, окна, origin policy;
  `protocols/licence_register.json` — записи `PHILADELPHIA_FED`, `NY_FED`, `CLEVELAND_FED`;
  `protocols/external_forecasts_v2.json` — реестр источников v2.
- [tools/external_forecast_scorecard.py](../tools/external_forecast_scorecard.py),
  [tools/fetch_scorecard_data.py](../tools/fetch_scorecard_data.py),
  [tests/test_external_forecast_scorecard.py](../tests/test_external_forecast_scorecard.py).
- Philadelphia Fed Livingston:
  `https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-survey`
  (архив/документация: `…/livingston-historical-data`, `…/livingston-documentation.pdf`,
  `…/livingston-release-dates.xlsx`).
- NY Fed:
  `https://www.newyorkfed.org/markets/market-intelligence/survey-of-market-expectations`
  (архив SMP/SME; PDF/ XLSX данные 2023–2026).
- Cleveland Fed:
  `https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting`
  (chart-JSON `…/nowcast_month.json`, `_quarter.json`, `_year.json`).
