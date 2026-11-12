# 86. SPF: официальный маппинг горизонтов и правила доступности

Дата: 2026-11-12. Статус: **metadata-only проверка**, без публикации и без коммита значений SPF.
Код: [`tools/spf_mapping.py`](../tools/spf_mapping.py); тесты: [`tests/test_spf_mapping.py`](../tests/test_spf_mapping.py) (28 тестов). Значения — только в git-ignored [`data/raw/spf/spf_median.json`](../data/raw/spf/spf_median.json); в репозиторий попадает лишь [`protocols/spf_metadata.json`](../protocols/spf_metadata.json). Правки `src/amfe/ensemble` не выполнялись (см. §7 — требуется решение Lead).

## 1. Источники и дата доступа

Официальная документация SPF, PDF «Documentation», **Last Update: July 28, 2026** (страница загрузки указывает August 14, 2026 — расхождение самой страницы; для цитат использован PDF):

- [spf-documentation.pdf](https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/spf-documentation.pdf) — 62 страницы; прочитано через текстовый экстрактор 2026-11-12.
- [Официальная таблица True Deadline / News Release](https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/spf-release-dates.txt) — даты дедлайнов и публикаций. Прочитано 2026-11-12.
- [Median Forecast landing page](https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts) — ссылки на MedianGrowth.xlsx/Inflation.xlsx и документацию.
- [Survey of Professional Forecasters main page](https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/survey-of-professional-forecasters) — описание тайминга и файлов.

Цитаты ниже — дословные фрагменты PDF с указанием страницы; страницы — по нумерации PDF, не по номеру в оглавлении.

## 2. Официальные определения `drgdp2..drgdp6`

Документация, §3 (стр. 28):

> «Growth rates are those for quarter-over-quarter growth, expressed in annualized percentage points, beginning with the forecast for the current quarter.»  
> «The number “2” appended to a column header represents a forecast for the quarter in which the survey is conducted. The numbers “3” to “6” represent the quarter-over-quarter growth rate forecasts for the following four quarters.»

Также §3 (стр. 28):

> «For all variables except headline and core CPI inflation and headline and core PCE inflation: The survey’s official growth rates are those for the rate of growth of the mean or median forecast for the level. That is, the survey’s reported growth rate is the rate of growth of the mean or median forecast for the level, not the mean or median forecast for the growth rate.»  
> «All growth rates are expressed in annualized percentage points. The formulas use discrete compounding, not continuous compounding.»

Отсюда точный маппинг:

| SPF code | Официальный смысл | Относительно квартала опроса T |
|---|---|---|
| `drgdp2` | Q/Q annualized growth медианного уровня real GDP, квартал опроса | T + 0 |
| `drgdp3` | то же, следующий квартал | T + 1 |
| `drgdp4` | то же, второй квартал после опроса | T + 2 |
| `drgdp5` | то же, третий квартал после опроса | T + 3 |
| `drgdp6` | то же, четвёртый квартал после опроса | T + 4 |

Это не «h2…h6» в смысле «через 2…6 кварталов от origin»: цифра — **номер колонки/квартала опроса**, где 2 = текущий квартал. Поэтому объявленный проектом маппинг `drgdp(h+2)` для протокольных горизонтов T2 **подтверждается** официальной документацией.

**О `drgdp1`:** в SPF MedianGrowth нет колонки `drgdp1`; есть `drgdp2..drgdp6`. `RGDP1` — это исторический уровень за квартал до опроса, а не прогноз (PDF стр. 40: «RGDP1 refers, of course, to the historical value for the quarter before the quarter in which the survey is conducted»). Поэтому `drgdp1` не является forecast-кодом и не маппится ни на одну цель.

## 3. Маппинг на цели/горизонты протокола v4

Протокол T2: `horizons_quarters = [0, 1, 2, 4]` (T2 — q/q annualized GDP growth). Полный маппинг:

| Цель протокола | Горизонт | SPF-код | Статус |
|---|---:|---|---|
| `T2_GDP_GROWTH` | 0 | `drgdp2` | **используется** |
| `T2_GDP_GROWTH` | 1 | `drgdp3` | **используется** |
| `T2_GDP_GROWTH` | 2 | `drgdp4` | **используется** |
| `T2_GDP_GROWTH` | 4 | `drgdp6` | **используется** |
| `T2_GDP_GROWTH` | 3 | `drgdp5` | **не используется**: h=3 отсутствует в протокольной сетке T2 |
| `T1_CPI_YOY` | любой | — | **недоступно**: SPF не публикует monthly y/y CPI |
| `T3_RECESSION` | любой | — | **недоступно**: SPF не даёт NBER recession probability |

`INFCPI1YR` и `INFCPI10YR` — **не** T1-прогнозы: первый является annual-average за четыре следующих квартала (геометрическое среднее Q/Q CPI-прогнозов `CPI3..CPI6`), второй — annual-average за следующие 10 лет; оба не являются monthly y/y CPI. Строить из них T1 интерполяцией запрещено (docs/65, protocol `origin_policy.spf_alignment`).

## 4. Определения `INFCPI1YR` и `INFCPI10YR`

Документация, §5 (стр. 35):

> «The two one-year-ahead series are expectations for average inflation over the four quarters following the quarter when we conducted the survey.»  
> «For INFCPI1YR, we use the geometric average of the quarter-over-quarter median forecasts for CPI inflation, corresponding to the columns CPI3 to CPI6 of the worksheet CPI in the same workbook.»  
> «The file Inflation.xlsx contains expectations for one-year-ahead annual-average inflation ... and expectations for the annual-average rate of CPI inflation over the next 10 years (INFCPI10YR).»

Официальная формула `INFCPI1YR` (PDF §5, стр. 36) — геометрическое связывание четырёх Q/Q-прогнозов CPI (дискретное компаундирование, annualized percentage points). Она **не тождественна** T1 (CPI y/y месяца `t-1+h`): другой объект (среднее за 4 квартала) и другая частота.

Для `INFCPI10YR` PDF §5 (стр. 35) говорит только «over the next 10 years». **Точное начало/конец 10-летнего окна в §5 не указано.** В описании другого ряда, `CPI10` (MedianLevel, стр. 14), сказано: «The “next 10 years” includes the year in which we conducted the survey and the following nine years. Conceptually ... from the fourth quarter of the year before the survey year to the fourth quarter of the year that is 10 years beyond the survey year». Это относится к `CPI10` (уровень/годовая инфляция в MedianLevel), а не к `INFCPI10YR` из Inflation.xlsx; **нельзя переносить это определение без подтверждения Philadelphia Fed**. Поэтому `INFCPI10YR` не маппится ни на T1, ни на какой протокольный горизонт; нужен письменный ответ/сноска от источника.

## 5. Дедлайны и release dates

Документация, §1 «Information Sets and Deadlines» (стр. 6):

> «For surveys conducted after the 1990:Q2 survey, we have set the deadlines for responses to late in the second to third week of the middle month of each quarter.»  
> «A minor change in the timing of the dates of the deadlines occurred beginning with the survey of 2005:Q1. Beginning with this survey, we tightened our production schedule. The dates of the deadlines for responses were moved up a few days (in most surveys), to the second week of the middle month.»

О выпуске (стр. 7):

> «From 1990:Q3 to 2004:Q4, we released the results of the survey to the public in the fourth week of the middle month of the quarter. ... Beginning with the survey of 2005:Q1, we advanced the dates of release a few days, to late in the second week of the middle month of the quarter.»

Точные даты — в официальной таблице release dates (проверено 2026-11-12); примеры:

| Survey | True Deadline | News Release |
|---|---:|---:|
| 1990 Q2 | 8/23/90* | 8/31/90* |
| 1990 Q3 | 8/23/90 | 8/31/90 |
| 2005 Q1 | 2/9/05 | 2/14/05 |
| 2013 Q4 | 11/18/13** | 11/25/13** |
| 2019 Q1 | 3/12/19** | 3/22/19** |
| 2020 Q1 | 2/11/20 | 2/14/20 |
| 2026 Q1 | 3/2/26** | 3/6/26** |
| 2026 Q3 | 8/11/26 | 8/14/26 |

`*` 1990:Q2 не проводился в реальном времени (PDF стр. 6: первый real-time survey — 1990:Q3; для 1990:Q2 прогнозы запрашивались задним числом с мая 1990).  
`**` задержки из-за shutdown/переноса релизов; официальная таблица помечает их.

**Правило доступности:** медианы становятся публичными в **News Release Date**, а не в дедлайн ответов. Поэтому для scoring на origin:

```
available  ⇔  survey_quarter is real-time (≥ 1990Q3)
              and origin is in the middle month of that survey quarter
              and origin ≥ News Release Date of that survey
```

Дедлайн сам по себе **недостаточен**: между дедлайном и релизом значения ещё не опубликованы. В `tools/spf_mapping.py` это реализовано как `availability()`; для кварталов, которых нет в встроенных точных anchors, используется **консервативный fallback — последний день среднего месяца** (он не раньше официального релиза для real-time surveys; для 1990:Q2 исключён).

Пример: `origin=2020-02-11` (дедлайн 2020Q1) — `available=False`; `origin=2020-02-14` (релиз) — `True`. `origin=2020-01-31` — ошибка выравнивания: origin не средний месяц (не SPF deadline month).

## 6. Что делает `tools/spf_mapping.py`

- `mapping_table()` — маппинг T2 0/1/2/4 → `drgdp2/3/4/6`, явные `T1/T3` недоступности, `drgdp5`/h=3, определения `INFCPI*`.
- `VARIABLE_DEFINITIONS` — официальные определения и страницы PDF.
- `deadline_date(quarter)`, `release_date(quarter)`, `availability(origin, survey_quarter=None)` — точные anchors + консервативный fallback; возвращают `available/reason/deadline/release`.
- `forecast_at(target, origin, horizon, values_path=...)` — читает локальные значения; при отсутствии файла возвращает `SPF_VALUES_NOT_DOWNLOADED`, а не падает; для T1/T3/h=3 — явные причины.
- `parse_values(payload)` / `load_values(path)` — валидация структуры, дублей, non-finite; пустой/частичный файл обрабатывается явно.
- `metadata_check()` — сверяет `protocols/spf_metadata.json` с официальными кодами и запретом значений.
- `check()`/CLI — `python tools/spf_mapping.py --check` (metadata/mapping/local values summary); `--origin 2020-02-14 --target T2_GDP_GROWTH --horizon 0` — verdict. Значения не записываются.

## 7. Расхождение с `src/amfe/ensemble/sources.py` (нужно решение Lead)

Объявленный в `src/amfe/ensemble/sources.py` **маппинг** `drgdp2..6` (drgdp2=current quarter, `drgdp(h+2)`) **подтверждён** официальной документацией; менять его не нужно. Но **правило доступности** в `sources.py` использует `SPF_MIN_LAG_DAYS = 14` дней после **первого дня** среднего месяца (`sources.py:65–66`, `:399–402`), а не официальные deadline/release dates. Это может дать look-ahead или ложную недоступность:

- **1990 Q3:** anchor = 1990-08-01, +14 дней = **1990-08-15**; официальный release — **1990-08-31** → `sources.py` считает прогноз доступным на 16 дней раньше публикации (look-ahead).
- **2019 Q1:** anchor = 2019-03-01, +14 = **2019-03-15**; официальный release — **2019-03-22** (shutdown) → 7 дней look-ahead.
- **2005 Q1:** anchor +14 = 2005-02-15; официальный release — 2005-02-14 → наоборот, на 1 день позже (консервативно, но несогласованно).
- **2020 Q1:** anchor +14 = 2020-02-15; официальный release — 2020-02-14; также на 1 день позже.

Рекомендация Lead (не правка в этом треке): в `src/amfe/ensemble/sources.py` заменить `SPF_MIN_LAG_DAYS`-правило на `release_date(survey_quarter)` из `tools/spf_mapping.py` (или на официальную таблицу release dates); добавить тесты на 1990Q3/2019Q1/2020Q1; для 1990:Q2 и ранее возвращать `SPF_NOT_REAL_TIME`. Иначе внешний SPF-прогноз может попасть в ансамбль/оценку до публикации, что противоречит PIT-принципу.

## 8. Ограничения

1. **Значения — third-party.** В репозиторий не коммитятся; локальный `data/raw/spf/spf_median.json` отсутствует в CI без fetch; публикуются только наши производные статистики. `protocols/spf_metadata.json` фиксирует URL/хеши/покрытие, но не значения.
2. **Официальные даты встроены частично.** В `tools/spf_mapping.py` — anchors для ключевых кварталов; для остальных — консервативный fallback (конец среднего месяца). Для точного scoring лучше подключить/распарсить полный `spf-release-dates.txt`; в CI файл не лежит, поэтому fallback является только безопасной границей.
3. **`INFCPI10YR`:** точное окно (год начала/конца) в §5 PDF не зафиксировано; `CPI10` — другой ряд. До ответа Philadelphia Fed не маппить на T1.
4. **SPF — медианы, а не распределения.** Для кандидата это point forecast; density/quantile scoring по SPF невозможен без отдельных probability files, которых мы не скачиваем.
5. **SPF-значения в локальном файле — последняя версия,** а не vintage на дату опроса; erratum/revisions могут влиять на backtest. Нужен отдельный аудит ревизий/errata (официальный `spf-errata.pdf`), не входит в этот трек.
6. **T1/T3:** SPF не даёт monthly y/y CPI и NBER recession probability; `RECESS`/Anxious Index — вероятность падения GDP, не событие NBER. Маппинг T3 на SPF запрещён.
7. **Mapping h=3/drgdp5:** официальный код существует, но протокольного горизонта h=3 нет; нельзя «подтянуть» его к h=4.
8. **Документация PDF** прочитана через текстовый экстрактор (Jina Reader); страницы/формулы могут отображаться неточно; цитаты сверены по тексту PDF. Официальная страница указывает Last Update August 14, 2026, PDF — July 28, 2026; цитаты — по PDF.

## 9. Что осталось для скоринга SPF

1. **Lead:** заменить availability-правило в `src/amfe/ensemble/sources.py` на официальные release dates (`spf_mapping.release_date`), добавить тесты look-ahead (1990Q3/2019Q1/2020Q1), затем использовать SPF в ансамбле.
2. Подключить `tools/spf_mapping.py` как единственный источник маппинга/доступности; не дублировать константы в `sources.py`.
3. Для T2 h=0/1/2/4 — point RMSE/Brier? SPF — point; сравнение с median baselines; отдельно решить веса ансамбля (docs/81).
4. Аудит ревизий SPF/errata и точной даты `INFCPI10YR`.
5. Полные официальные release dates (не только anchors) в инструменте/CI; при отсутствии — fallback помечать как консервативный.
6. Не публиковать/не коммитить SPF-значения; атрибуция Philadelphia Fed обязательна; условия — `PHILADELPHIA_FED` в licence register (research use, not legal opinion).

## 10. Тесты

`python tests/test_spf_mapping.py` — **28 тестов**, все зелёные. Проверяются: официальный T2-маппинг 0/1/2/4; `drgdp5`/h=3; недоступность T1/T3; определения `INFCPI1YR`/`INFCPI10YR`; точные anchors 2020Q1/1990Q3/2019Q1/2026Q1; дедлайн ≠ публикация; fallback; origin не средний месяц; pre-1990Q3; partial/empty/duplicate values; отсутствие файла; metadata-only; локальные значения (если файл есть); CLI `--check`/`--origin`. Если `data/raw/spf/spf_median.json` отсутствует, тесты значений помечаются skip, metadata/mapping-тесты остаются.

**Не проверено:** полный список всех 147 дат release (встроены только anchors), фактические значения SPF в CI без артефакта, ревизии/errata SPF, точное окно `INFCPI10YR`, юридическое чтение лицензии (не юридическая консультация), поведение `sources.py` после правки (её не делали).
