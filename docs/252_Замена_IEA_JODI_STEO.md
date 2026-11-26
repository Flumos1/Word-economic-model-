# 252. Замена IEA: JODI-Oil (факт-балансы) + EIA STEO (публичный прогноз)
Версия 1.0.0 · 2026-11-26. Задача `task-84`. Правило владельца: платные/лицензионные источники заменяем открытыми данными. Контекст: IEA OMR/WEO — платные и `RESTRICTED`; замена — открытая JODI-Oil (официальные страновые данные) + публичный прогноз EIA STEO. **Это не юридическая консультация.** Реестр/allowlists не менялись — строки для Lead'а в конце.

## Что заменяет IEA OMR (частично)
- **Фактические балансы:** JODI-Oil первичная база — добыча нефти, экспорт, отгрузка на НПЗ по странам, месячно, открытый CSV.
- **Прогноз:** EIA STEO (ноябрь 2026) — мировая добыча/потребление жидких топлив и цена WTI, месячный горизонт до декабря 2027.

## 1) JODI-Oil: найденные открытые файлы и что скачано
- Раздел загрузок: `https://www.jodidata.org/oil/database/data-downloads.aspx` (HTTP 200; доступен с обычными браузерными заголовками; robots.txt разрешает эти пути).
- Прямой `jodi_oil_data.csv` из задания — **404**, поэтому использованы фактические файлы раздела downloads (формат long CSV: `REF_AREA,TIME_PERIOD,ENERGY_PRODUCT,FLOW_BREAKDOWN,UNIT_MEASURE,OBS_VALUE,ASSESSMENT_CODE`):
  - `https://www.jodidata.org/_resources/files/downloads/oil-data/annual-csv/primary/2024.csv` (11 603 543 B)
  - `https://www.jodidata.org/_resources/files/downloads/oil-data/annual-csv/primary/2025.csv` (11 581 613 B)
  - `https://www.jodidata.org/_resources/files/downloads/oil-data/annual-csv/primary/primaryyear2026.csv` (8 485 626 B)
- Сырые CSV **не сохранены и не закоммичены**: они стримились и фильтровались; в репозиторий попал только компактный срез.
- Срез: `data/public/jodi_oil_crude_kbd_2024_2026.csv` — **11 083 байта**, **sha256 `a9705bedf6f4fbfbe75a96ded477f128f58ae4bc0d8939f79bb45f1717b13e94`**, **300 строк** (300 непустых значений):
  - страны: **SA, US, CA, NO** (4);
  - потоки: **INDPROD** (crude oil production), **TOTEXPSB** (total crude exports), **REFINOBS** (refinery intake);
  - период: **2024-09 .. 2026-09** (25 месяцев);
  - unit: **kb/d** (тысяч баррелей в сутки), assessment codes 1/3 сохранены;
  - колонки: `country,series,period,value_kbd,unit,assessment_code`; LF, UTF-8 без BOM.
- Почему не SA/RU/US/IQ/AE (как в задании): проверено по первичной базе — **RU, IQ, AE не отдают значения KBD** (в CSV пусто/`..`/`x`; у них нетnumeric KBD по этим потокам), поэтому вместо них взяты **CA и NO**, которые отдают все три потока. Это зафиксировано в метаданных.
- Метаданные: `data/public/jodi_oil_crude_kbd_2024_2026.meta.json` — 2 381 B, `schema = AMFE-PUBLIC-JODI/1.0.0`; license, publisher JODI/UNSD, source_url + `source_files`, retrieved_at=2026-11-26, coverage, observations, file_bytes/file_sha256, notes.
- **Лицензия/ToS JODI:** отдельной страницы terms-of-use на сайте не найдено (2026-11-26); публичная страница downloads, в футере «© Copyright JODI 2026», robots.txt не запрещает использованные пути. Строка для реестра: `JODI-Oil public data: free public access, attribution to JODI/UNSD required; no re-sale` (это чтение проекта, а не цитата формальной лицензии — в метаданных поле `license_note`).

## 2) EIA STEO: машиночитаемые таблицы есть
- `https://www.eia.gov/outlooks/steo/xls/STEO_m.xlsx` — HTTP 200, **1 096 645 байт**, **sha256 `7d28012ff726ebea…`** (полный — в метаданных), workbook с листами `1tab…10btab`, `Dates`, `Contents`.
- Из `Dates`: **Forecast Month — November 2026**, modeling completed **2026-11-05**, table beginning 2022-01, **Last Historical Month — 202610**.
- Срез: `data/public/eia_steo_oil_forecast_2026_2027.csv` — **6 669 байт**, **sha256 `55bd1c01f4aef2b925e506c5e27ff9cd705e5b8cf063cd56b531a92c630f4f13`**, **60 строк**:
  - `copr_world` — World crude oil production (млн б/д), лист `3dtab`;
  - `papr_world` — World total petroleum and other liquid fuels production (млн б/д), лист `3atab`;
  - `patc_world` — World total petroleum and other liquid fuels consumption (млн б/д), лист `3etab`;
  - `WTIPUUS` — WTI spot average (USD/барр.), лист `2tab`;
  - период **2026-10 .. 2027-12** (15 месяцев: 1 historical + 14 forecast), колонки `series_id,series_label,period,value,unit,status,source_table`; LF, UTF-8 без BOM.
  - Примеры: мировая добыча нефти 2026-10 = 78.69 → 2027-12 = 82.15 млн б/д (прогноз); потребление жидких топлив 102.78 → 106.03; WTI 102.93 → 70.00 USD/барр.
- Метаданные: `data/public/eia_steo_oil_forecast_2026_2027.meta.json` — 2 014 B, `schema = AMFE-PUBLIC-STEO/1.0.0`; publisher EIA, license **`public domain (U.S. Government work), attribution recommended`**, source_url + `steo_page`, `workbook_bytes`/`workbook_sha256`, forecast vintage, coverage, observations, file_bytes/file_sha256, notes.
- Workbook сырой не закоммичен; в git только срез. Проверка sidecar через `_public_domain_aggregates` (in-memory allowlist): оба файла `allowed`, `problems = []`.

## 3) Что замена НЕ покрывает
- **IEA OMR целиком:** полные мировые балансы спроса/предложения, запасы (OECD/global), торговые матрицы, refining margins, product-by-product breakdown, оценки IEA — не воспроизводятся. JODI — страновые self-reported данные (4 страны, 3 потока), STEO — прогноз EIA, а не IEA.
- **IEA WEO:** долгосрочные сценарии (multi-decade, net-zero и т.п.), сценарные траектории и методология — не заменяются; STEO даёт только короткий горизонт до конца 2027 и один взгляд EIA.
- **Proprietary balances / лицензионные исторические ряды IEA:** не используются и не аппроксимируются как «IEA»; методологии разных источников различаются.
- **Цены/прогнозы IEA:** вместо них — WTI из STEO (не Brent, не IEA price path).
- **PIT/винтажи:** нет. JODI ревизуется и публикуется с лагом (последний месяц 2026-09 на 2026-11-26); STEO — один винтаж (ноябрь 2026), не архив as-published прогнозов.
- **Полнота:** JODI не отдаёт KBD по RU/IQ/AE (проверено), нет полной истории; STEO не покрывает страновую матрицу мира.

## 4) Ограничения и что нельзя заявлять
- Нельзя заявлять, что это «данные IEA» или что JODI/STEO эквивалентны IEA OMR/WEO по методологии/полноте.
- Нельзя заявлять PIT/первую публикацию: JODI значения — последние опубликованные на дату; STEO — прогноз одного винтажа.
- JODI экспорт `TOTEXPSB` — суммарный экспорт **сырой нефти** (не нефтепродуктов); `REFINOBS` — refinery intake; единицы kb/d; коды assessment 1/3 означают разную степень оценённости — сохранены в CSV.
- JODI ToS формально не найден; статус открытости основан на публичной странице загрузок, robots.txt и copyright-уведомлении; атрибуция JODI/UNSD обязательна.
- STEO — публичный домен США, но это прогноз EIA (не факт) и не рекомендация; атрибуция рекомендована.
- Жирные сырые файлы (JODI CSV 8–12 MB, STEO xlsx 1.1 MB) не коммитятся; пин — только компактные таблицы + sidecar.

## 5) Строки для реестра и allowlists (правит Lead)
- Новый источник **JODI_OIL** (или JODI): `status="VERIFIED"`, `retrieved_at="2026-11-26"`, publisher `JODI / UNSD`, url `https://www.jodidata.org/oil/database/data-downloads.aspx`, license — строка из §1, `access="file"`, coverage SA/US/CA/NO 2024-09..2026-09, frequency monthly, fields crude production/exports/refinery intake, `vintage_support="none"`, notes: `data/public/jodi_oil_crude_kbd_2024_2026.csv`, 11 083 B, sha256 `a9705bed…13e94`, 300 rows/300 values, source_files (3 URL), docs/252, «не PIT, не resale».
- Новый источник **EIA_STEO**: `status="VERIFIED"`, `retrieved_at="2026-11-26"`, publisher `U.S. Energy Information Administration`, url `https://www.eia.gov/outlooks/steo/xls/STEO_m.xlsx`, license `public domain (U.S. Government work), attribution recommended`, coverage world 2026-10..2027-12 (Nov 2026 vintage), fields world crude/liquids production & consumption + WTI, `vintage_support="none"`, notes: `data/public/eia_steo_oil_forecast_2026_2027.csv`, 6 669 B, sha256 `55bd1c01…f4f13`, 60 rows, workbook sha256 `7d28012f…`, docs/252.
- `tools/external_review_g2.py`, `ALLOWED_AGGREGATES`:
  `"data/public/jodi_oil_crude_kbd_2024_2026.csv": "data/public/jodi_oil_crude_kbd_2024_2026.meta.json",`
  `"data/public/eia_steo_oil_forecast_2026_2027.csv": "data/public/eia_steo_oil_forecast_2026_2027.meta.json",`
- `tests/test_licence_hygiene.py`, `ALLOWED_TRACKED_RAW`: 4 пути (CSV+meta JODI, CSV+meta STEO).
- `protocols/source_verification_log.jsonl`: evidence-записи по JODI и EIA_STEO (файл/sha/obs) по формату Lead'а.
- IEA остаётся `RESTRICTED`/платным и не используется.

## Воспроизведение
- JODI: скачать 3 primary annual CSV (ссылки §1), отфильтровать `ENERGY_PRODUCT=CRUDEOIL`, `FLOW_BREAKDOWN in {INDPROD,TOTEXPSB,REFINOBS}`, `UNIT_MEASURE=KBD`, `REF_AREA in {SA,US,CA,NO}`, `TIME_PERIOD 2024-09..2026-09`; 300 строк.
- STEO: скачать `STEO_m.xlsx`, листы `3dtab/3atab/3etab/2tab`, строки `copr_world/papr_world/patc_world/WTIPUUS`, месяцы 2026-10..2027-12; `status=historical` для 2026-10, иначе `forecast` (cutoff `Last Historical Month=202610`).
- Проверка хешей: `sha256` CSV сверить с `.meta.json` (совпадает).

## Файлы
`data/public/jodi_oil_crude_kbd_2024_2026.csv` + `.meta.json`, `data/public/eia_steo_oil_forecast_2026_2027.csv` + `.meta.json`, `docs/252_Замена_IEA_JODI_STEO.md`. Реестр, журнал, allowlists, tests, tools, src, frozen и data/oil не менялись; не закоммичено.
