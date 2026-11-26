# 255. PIT-кандидат №3: FOMC SEP / Greenbook-Tealbook (исследование)

Дата: 2026-11-26. Задача: найти третий scorable PIT-архив квартальных прогнозов США. **Вывод: настоящий PIT-архив существует (Greenbook/Tealbook — публикация с ~5-летним лагом; FOMC SEP — публичный архив с 2012), но ни один из кандидатов не даёт прямого маппинга на текущие цели протокола T1/T2/T3. Для существующего адаптера пригодных точек 0.** Значения третьих сторон в этом документе не публикуются; скачивание/`data/raw` не потребовалось.

## 1. Короткий ответ

| Кандидат | PIT-архив | Маппинг на T1/T2/T3 | Вердикт |
|---|---|---|---|
| FOMC SEP, 2012→ | Да, официальные страницы по заседаниям + FRED/ALFRED medians (частично) | **Нет**: SEP даёт Q4/Q4 annual и year-end/Q4 average; протокол — q/q annualised T2 или monthly y/y CPI T1 | **NOT_SCORABLE** для текущего протокола |
| FOMC SEP dot plot / individual projections | Да, после 5 лет; Figure 2 доступна в HTML | **Нет**: не T1/T2/T3 | **NOT_SCORABLE** |
| Greenbook/Tealbook (FRASER/FOMC) | **Да, настоящий PIT**: публикация с ~5-летним лагом | **Нет готового**: только PDF/OCR/полнотекст, таблицы не машиночитаемы; извлечение без выдумывания не автоматизируется | **NOT_SCORABLE** (для автоматического адаптера); возможен только ручной curated-слой |
| FRB/US model database | Нет архива прогнозов с датами публикации | — | **NOT_SCORABLE** |

**Найден ли PIT-архив?** Да, но не тот, который решает задачу: FOMC SEP — PIT-публикации с датами, однако определения не совпадают с T1/T2; Greenbook/Tealbook — настоящий PIT (5-летний лаг), но не машиночитаемый. **PIT-архив, пригодный как третий scorable источник для текущего протокола, не найден.**

## 2. FOMC SEP

### 2.1. Архив и даты публикации

- Официальная страница заседаний: <https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm> — **HTTP 200** (размер через fetch-tool не отдаётся; `Content-Length` недоступен).
- Официальное описание исторических материалов: <https://www.federalreserve.gov/monetarypolicy/fomc_historical.htm> — **HTTP 200**. Ключевые факты (дословно по смыслу):
  - SEP введён с заседания 30–31 октября 2007; участники подают прогнозы на **четырёх** заседаниях в год;
  - «A compilation and summary of these projections (which includes individual projections without attribution) is circulated to participants, and a detailed summary of the economic projections (the Summary of Economic Projections, or "SEP") is released in conjunction with the Chairman's post-meeting press conference.»;
  - «From October 2007 to September 2020, the SEP ... was included as an addendum to the minutes that are released three weeks after the meeting. Starting in April 2011, an advance version of the SEP table on the ranges and central tendencies ... was released in conjunction with the Chairman's post-meeting press conference.»;
  - «Beginning in December 2020, all SEP exhibits ... are released in conjunction with the Chairman's press conference.»;
  - «the compilation and summary of individual projections ... is made available to the public after five years. For SEP compilations from 2007 to 2015, a participant key ... ten years ... Starting with ... March 2016, ... five years.»
- Страницы SEP по заседаниям (accessible version) имеют шаблон `https://www.federalreserve.gov/monetarypolicy/fomcprojtablYYYYMMDD.htm`:
  - `.../fomcprojtabl20130619.htm` — **HTTP 200**;
  - `.../fomcprojtabl20140319.htm` — **HTTP 200**;
  - `.../fomcprojtabl20191211.htm` — **HTTP 200**;
  - `.../fomcprojtabl20260318.htm` — **HTTP 200**;
  - `.../fomcprojtabl20121212.htm` — **HTTP 404** (для 2012 доступны не все accessible-HTML; старые выпуски — PDF/иной формат). Точные размеры HTML/PDF через fetch-tool не получены (`n/a`); PDF-эндпоинты отдают `application/pdf`, tool их не рендерит.
- **Машиночитаемого единого архива всех выпусков 2012→ нет:** это набор HTML/PDF страниц по заседаниям, без CSV/JSON API и без стабильного bulk-download. Для 2012 часть страниц не accessible-HTML.

### 2.2. Что содержит SEP (определения)

Стандартная таблица SEP (Table 1) содержит медианы/central tendency/ranges для:
- **изменение real GDP** — «Q4/Q4» annual percent change;
- **unemployment rate** — Q4 average (percent);
- **PCE inflation** — Q4/Q4 annual percent change;
- **core PCE inflation** — Q4/Q4;
- **federal funds rate** — year-end target (midpoint);
- **longer-run** значения.

Это **не** q/q annualised GDP growth (T2) и **не** monthly y/y CPI (T1); это annual Q4/Q4 / Q4 average. T3 (NBER recession) в SEP нет.

### 2.3. PIT-даты (критично)

Из официального описания:
- **2012–сентябрь 2020:** detailed SEP (медианы и пр.) — addendum к minutes, публикуется **через 3 недели после заседания**; advance table (ranges/central tendencies) — на пресс-конференции, но это не полный median SEP. Использовать дату заседания как PIT для медиан нельзя — это look-ahead на 3 недели.
- **С декабря 2020:** все SEP exhibits — на пресс-конференции (день заседания).
- **Individual projections/dot plot compilation:** только через **5 лет**; participant key для 2007–2015 — 10 лет.
- Для каждого выпуска нужна **фактическая дата публикации медиан** (meeting minutes release date / Fed page), а не дата URL `fomcprojtablYYYYMMDD`; для 2012–2020 — дата minutes.

### 2.4. FRED/ALFRED (machine-readable)

ALFRED search: <https://alfred.stlouisfed.org/search?et=production&st=nation&t=median%3Bprojection&ob=sr&od=asc> — **HTTP 200**, 9 series. Найдены median series:
`FEDTARMD`, `FEDTARMDLR`, `GDPC1MD`, `GDPC1MDLR`, `UNRATEMD`, `UNRATEMDLR`, `PCECTPIMD`, `PCECTPIMDLR`, `JCXFEMD` (fed funds, GDP, unemployment, PCE, core PCE; annual / longer-run).

- FRED series page `FEDTARMD`: **HTTP 200**, units «Percent», annual, **Release Dates 2015-12-16 … 2026-09-16**.
- ALFRED `GDPC1MD`: **HTTP 200**, units «Fourth Quarter to Fourth Quarter Percent Change», annual, **Release Dates 2015-12-16 … 2026-09-16**; source U.S. FOMC.
- Следовательно, machine-readable medians доступны **не с 2012, а с 2015-12-16** (в текущем ALFRED/FRED наборе), и только annual Q4/Q4 / Q4 averages. Использовать их как «архив 2012→» нельзя; 2012–2015 отсутствуют.
- **Маппинг:** ни одна из 9 series не является q/q annualised GDP или monthly y/y CPI. Прямого маппинга нет; интерполяция/пересчёт из Q4/Q4 в q/q **запрещены** (нужен квартальный путь, которого в SEP нет).

### 2.5. Dot Plot

- Figure 2 в SEP HTML — таблица «Number of participants with projected midpoint of target range or target level» по годам (агрегированные точки dot plot), доступна в HTML (например, 2019-12-11 page, 200).
- **Individual projections** (compilation) — только через 5 лет; participant key — 5/10 лет.
- Единого machine-readable dot-plot history (CSV/API) нет; HTML/PDF можно парсить, но это не таргет T1/T2/T3 и не даёт scorable rows. **NOT_SCORABLE.**

## 3. Greenbook / Tealbook (FRASER / FOMC)

### 3.1. Что это и где

- Официальное описание: <https://www.federalreserve.gov/monetarypolicy/fomc_historical.htm> — **HTTP 200**. Greenbooks 1964–2010; Tealbooks с июня 2010; «Report to the FOMC on Economic Conditions and Monetary Policy», staff forecasts of U.S./international economy; historical materials «made available ... with about a five-year lag» (дословно: «Transcripts, Bluebooks, Greenbooks, compilations and summaries of FOMC participants' individual economic projections, and agendas of FOMC meetings are made available to the public through this website with about a five-year lag»).
- FRASER: заявленный в задаче `https://fraser.stlouisfed.org/title/greenbook-1180` при fetch вернул **HTTP 200**, но это **не страница Greenbook**, а общий browse-экран FRASER; канонический title для FOMC meeting documents — <https://fraser.stlouisfed.org/title/federal-open-market-committee-meeting-minutes-transcripts-documents-677> (**HTTP 200**).
- Пример item: Meeting 2–3 Nov 2010 (`id=23296`) — HTML **200**; metadata JSON `https://fraser.stlouisfed.org/metadata.php?type=item&id=23296&json=1` — **HTTP 200**; в metadata есть `pdfUrl` и `textUrl` для Tealbook A/B, SEP compilation, minutes, material. Tealbook A text: `.../FOMC20101103tealbooka20101027.txt` — **HTTP 200**, 104 страницы (в тексте `Page X of 104`); Tealbook tables/charts accessible material text — **HTTP 200**.
- **PIT-дата:** дата meeting (Nov 2010) — не дата публикации; документ опубликован с ~5-летним лагом (для этого item доступен/«content last modified 01/29/2016»); точная дата public release — в документе/Board, не в `originInfo` (там meeting date); `recordCreationDate` FRASER — дата оцифровки, не PIT. **Без явной даты public-release нельзя использовать как PIT.**

### 3.2. Машиночитаемость

- Есть PDF и OCR/полнотекстовые `.txt`; таблицы в тексте — **плоский OCR/HTML**, не CSV/JSON. Пример Tealbook accessible material содержит текстовые таблицы и описания графиков; колонки/годы/quarterly rows могут быть извлечены только эвристически, с высоким риском перепутать строки/колонки/единицы.
- Нет официального machine-readable датасета таблиц Tealbook/Greenbook; FRASER metadata/IIIF даёт страницы, но не структурированные значения.
- **Извлечение без выдумывания** возможно только как ручной/полуручной curated-процесс с page-level provenance и проверкой каждой ячейки; для автоматического адаптера — **NOT_SCORABLE**.
- Объём: Greenbook 1964–2010 + Tealbook 2010–2015, ~8 заседаний/год → сотни PDF; таблицы разнородны по годам; scorable rows для текущего протокола = 0 (нет прямого маппинга и нет структурированного архива).

## 4. FRB/US model database

- Официальная страница FRB/US: <https://www.federalreserve.gov/econres/us-models-about.htm> — **HTTP 200**; `https://www.federalreserve.gov/econres/frbus/` — **HTTP 404**.
- FRB/US — модель/уравнения/данные/симуляционные программы; **архива прогнозов с датами публикации** на странице нет. Это не PIT-архив прогнозов и не даёт T1/T2/T3 rows. **NOT_SCORABLE.**

## 5. Сводная таблица PIT/маппинга/точек

| Кандидат | URL/статус | Частота/PIT | Определения | T1 y/y | T2 q/q annualised | T3 | Точек для адаптера | Вердикт |
|---|---|---|---|---|---|---|---|---|
| FOMC SEP (HTML/PDF) | `fomccalendars` 200; `fomcprojtabl` 2013+ 200, 2012 404; sizes n/a | 4/год; 2012–2020 медианы через 3 нед. после meeting; 2020-12+ в день meeting | GDP Q4/Q4; unemployment Q4 avg; PCE/core PCE Q4/Q4; fed funds year-end | нет (PCE, не CPI; annual) | нет (Q4/Q4 ≠ q/q annualised) | нет | **0** | **NOT_SCORABLE** |
| FOMC SEP FRED/ALFRED 9 series | FRED/ALFRED 200; releases 2015-12-16…2026-09-16 | 4/год, annual | units annual/Q4/Q4 | нет | нет | нет | **0**; 2012–2015 отсутствуют | **NOT_SCORABLE** |
| SEP dot plot/individual | HTML/PDF 200; individual 5-year | 4/год; 5-летний лаг individual | anonymous participant rate points | нет | нет | нет | **0** | **NOT_SCORABLE** |
| Greenbook/Tealbook FRASER/FOMC | title 677 200; item/metadata/text 200; PDF only; sizes via metadata n/a | ~8/год 1964–2015; публикация ~5-летний лаг; точная release date не machine-readable | staff GDP/inflation/unemployment forecasts, разные таблицы | нет готового | нет готового | нет | **0** (только ручной curated) | **NOT_SCORABLE** для автоматического адаптера |
| FRB/US | us-models-about 200; frbus/ 404 | нет архива прогнозов | model/equations/data | нет | нет | нет | **0** | **NOT_SCORABLE** |

## 6. Формат для адаптера

**Для текущего протокола формат не нужен: ни один кандидат не должен эмитить строки T1/T2/T3.** Если Lead захочет отдельную будущую цель «annual Q4/Q4 GDP/PCE» (новая цель, не T2), возможный формат для FOMC SEP:

```
source_id: FOMC_SEP
release_date: YYYY-MM-DD        # фактическая public date медиан; 2012-2020 = minutes/addendum, не meeting_date
meeting_date: YYYY-MM-DD
vintage_class: PUBLIC_SEP_MEDIAN   # vs SEP_INDIVIDUAL_5Y
variable: gdp_q4q4 | pce_q4q4 | core_pce_q4q4 | unemployment_q4avg | fed_funds_yearend
target_year: YYYY
horizon_type: Q4/Q4 | Q4_AVERAGE | YEAR_END
value_unit: percent | percent
source_url: https://www.federalreserve.gov/monetarypolicy/fomcprojtablYYYYMMDD.htm
```

Но: это **не** T1/T2/T3, и смешивать с ними нельзя; для T1 нужен monthly y/y CPI, для T2 — q/q annualised GDP. Для Greenbook/Tealbook адаптер не предлагается: PDF/OCR + page-level таблицы, без официальной структуры, автоматическое извлечение будет reconstruction/fabrication risk.

## 7. Что заблокировано

1. **T1:** SEP даёт PCE annual Q4/Q4, не CPI monthly y/y; Greenbook/Tealbook — PDF-таблицы без структуры; FRB/US — не прогнозный архив. Прямого T1-кандидата нет.
2. **T2:** SEP даёт Q4/Q4 annual GDP, не q/q annualised; восстанавливать квартальный путь из annual — запрещённая интерполяция. FRED/ALFRED series annual и с 2015, не 2012.
3. **T3:** ни SEP, ни Greenbook/Tealbook не дают NBER recession probability; FRB/US — нет.
4. **PIT:** для FOMC SEP 2012–2020 медианы публиковались через 3 недели после meeting; дата meeting в URL — не PIT. Для Greenbook/Tealbook точная public-release дата не в machine-readable metadata; `originInfo` — meeting date, `recordCreationDate` — оцифровка. Без даты релиза использовать нельзя.
5. **Machine-readable:** FOMC SEP не имеет единого CSV/API архива 2012→ (FRED/ALFRED только 2015+ annual); Greenbook/Tealbook — PDF/OCR/HTML, не структурированные таблицы.
6. **Объём:** FOMC SEP ~4 выпуска/год × 2012–2026 ≈ 59 релизов; FRED/ALFRED 9 series × ~44 релиза (2015-12…2026-09); Greenbook/Tealbook сотни PDF; но **scorable rows для текущих целей = 0**.

## 8. Ограничения проверки

- HTTP-статусы получены fetch-tool; **размеры/Content-Length** для HTML/PDF не отдаются (n/a). FRASER PDF отдаётся как `application/pdf`; fetch-tool его не рендерит, поэтому PDF проверялся через OCR/текстовый слой FRASER (`textUrl`, HTTP 200), а не через бинарное сравнение.
- 2012 SEP HTML не найден (404); 2012 выпуски, вероятно, PDF-only — это ограничение архива, а не вывод о содержимом. Полный перечень всех дат 2012–2026 не скачивался: для вывода о маппинге это не требуется; для будущего адаптера нужно построить точный release-date index.
- Greenbook/Tealbook release dates не извлечены для каждого документа; официально «about five-year lag»; точные даты требуют page-level public-release authorization/Board records. Без этого PIT-классификация была бы реконструкцией — не делал.
- Значения третьих сторон не публиковались и не сохранялись; `data/raw` не использовался. Никаких tools/protocols/tests/docs174 изменений.
- Не проверял, публикует ли сторонний vendor готовый machine-readable Greenbook/Tealbook dataset (например, платные/академические архивы); это могло бы изменить вывод о feasibility, но требует лицензии/provenance.

**Итог:** настоящий PIT-архив есть (Greenbook/Tealbook с ~5-летним лагом; FOMC SEP с датами), но **третий scorable источник для текущего протокола не найден**: FOMC SEP не маппится на T1/T2/T3, FRED/ALFRED неполон и annual, Greenbook/Tealbook не машиночитаем; FRB/US не архив прогнозов. Блокеры — definition mismatch, PIT release-date ambiguity и отсутствие структурированных таблиц.
