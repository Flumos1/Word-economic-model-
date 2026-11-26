# 240. USDA PSD: компактный публичный агрегат (VERIFIED-кандидат)

Дата: 2026-11-26. Задача: `task-75`, роль data/PIT (r2-completion). Правило владельца:
**NO_PAID_LICENCES**, данные только из открытых S1–S3; USDA PSD — public domain, самый чистый
путь. Статус: **данные реально получены и запинены; реестр/журнал/allowlists обновляет Lead**
(параллельно идёт FRED-задача, чтобы не затереть правки друг друга). Это не юридическая
консультация. Сырой ZIP и полный CSV в git не попадают.

## 1. Что скачано

| Параметр | Значение |
|---|---|
| URL (официальный bulk) | `https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_csv.zip` |
| Лендинг | `https://apps.fas.usda.gov/psdonline/` |
| Получено | 2026-11-26, `urllib` + User-Agent `AMFE-research/1.0`, HTTP 200 `application/x-zip-compressed` |
| ZIP | **10 459 432 B**, sha256 **`a293f2b05b1b7b185d7fdb7e1a562c43e6a0f190c6bf0e03fbe3df876286bd32`** |
| Внутри | `psd_alldata.csv`, **202 290 029 B** (214 стран, 1960–2026, колонки `Commodity_Code, Commodity_Description, Country_Code, Country_Name, Market_Year, Calendar_Year, Month, Attribute_ID, Attribute_Description, Unit_ID, Unit_Description, Value`) |
| Где лежит | временный каталог **вне репозитория** (`%TEMP%\usda_psd_20261126`); ZIP и полный CSV **не коммитятся** |
| Лицензия | **public domain (U.S. Government work)**; USDA FAS — U.S. Government work; атрибуция рекомендуется, не требуется |
| API с ключом | **не использовался** (USDA OpenData API → 403 без ключа; ключи не берём) |

## 2. Что извлечено (компактный срез)

Файл: `data/public/usda_psd_soybean_corn_2022_2026.csv` ·
**20 882 B** · sha256 **`c0f19817d954e1ccc2ce5fbcae5266f58f3da0404f85eab1dbe5b4a647ceb6ee`** ·
**275 наблюдений** · UTF-8 без BOM, LF, значения — дословно из официального файла.

| Отбор | Значение |
|---|---|
| Культуры | `2222000` Oilseed, Soybean · `0440000` Corn (2 культуры) |
| Страны | `US` United States, `BR` Brazil, `AR` Argentina, `CH` China, `E4` European Union; для кукурузы ещё `UP` Ukraine |
| Marketing years | **2022–2026** (5 лет; текущий 2026 — оценка USDA на ноябрь 2026) |
| Атрибуты | `028` Production · `088` Exports · `176` Ending Stocks · `057` Imports · `125` Domestic Consumption |
| Единица | `1000 MT` (1000 метрических тонн), как в источнике |
| Строки | 125 (soybean: 5 стран × 5 лет × 5 атрибутов) + 150 (corn: 6 × 5 × 5) = **275** |
| Колонки CSV | `commodity_code, commodity, country_code, country, market_year, attribute_id, attribute, unit, value, source_month` |

`source_month` — 2-значная колонка `Month` из исходного файла (01–11), **сохранена дословно и не
интерпретируется**: в `psd_alldata` на ключ (commodity, country, market_year, attribute) приходится
одно текущее значение, а не история ревизий.

Метаданные: `data/public/usda_psd_soybean_corn_2022_2026.meta.json` (**2 607 B**),
`schema = AMFE-PUBLIC-USDA-PSD/1.0.0`, содержит `license`, `publisher`, `source_url`, `landing_url`,
`retrieved_at = 2026-11-26`, `coverage`, `unit`, `bulk_zip` (URL/bytes/sha256 + extracted CSV bytes),
`file`, `file_bytes`, `file_sha256`, `observations`, `notes` (агрегат; полный ZIP не коммитится; нет
World-тотала; нет PIT/винтажей).

Проверка соответствия провенанс-требованию `tools/external_review_g2.py` (dry-run с будущей
allowlist, файл не менялся): `allowed = [..., data/public/usda_psd_soybean_corn_2022_2026.csv]`,
`problems = []` — запись проходит проверку sidecar/hash/licence/source-URL/retrieved_at/coverage.

## 3. Что агрегировано и что нет (честно)

- **Агрегировано**: пять балансовых атрибутов PSD из официального bulk; выбранные страны —
  отчётные единицы PSD; единицы сохранены как в источнике; значения не пересчитывались и не
  округлялись сильнее источника.
- **World-тотала нет**: в `psd_alldata` **нет строки «World»** для этих культур; есть отдельные
  страны, агрегат **European Union** и служебная категория **Other**. Складывать страны вручную
  нельзя: EU-агрегат и её страны-члены присутствуют отдельно, «Other» — не остаток мира, а
  отдельная категория PSD, поэтому наивная сумма дала бы двойной счёт. Поэтому World-агрегат **не
  считается и не публикуется**; если он нужен — это отдельная задача с явной методологией
  (например, только агрегат EU + страны вне EU, без Other) и записью метода.
- **Не PIT/не винтажи**: один текущий релиз `psd_alldata` на дату; месячные обновления PSD могут
  менять исторические значения. Зафиксирован датированный снимок, а не архив ревизий. Слово
  «vintage/PIT» для этого файла использовать нельзя (у источника `vintage_support = partial` —
  это про принципиальную возможность со временем собирать месячные срезы, не про этот пин).
- **Не полный bulk**: 275 строк вместо ~миллионов; 2 культуры из десятков; 6 отчётных единиц из
  214; 5 лет из 1960–2026.

## 4. Что можно / нельзя заявлять

**Можно**: «по данным USDA FAS PSD от 2026-11-26, производство кукурузы в США в MY2026 оценивается
в 403 711 тыс. т, экспорт — 83 189 тыс. т, конечные запасы — 44 667 тыс. т» — со ссылкой на срез и
дату; сравнения выбранных стран/лет в рамках 5 атрибутов и 275 наблюдений.

**Нельзя**: называть это полным мировым балансом; публиковать «мировое производство сои» из этого
файла; утверждать PIT-ревизии/винтажи; выдавать за прогноз (в срезе нет WEO/forecast-атрибутов,
это оценки PSD на дату); использовать как замену лицензионным источникам (IEA/Kpler/Vortexa) —
они остаются PAID_LICENCE и не подменяются (RL-19/RL-24).

## 5. USDA_FAS_EXPORT_SALES — не сделано, остаётся CATALOGUED

Попытка дешёвого публичного файла не удалась, поэтому статус `USDA_FAS_EXPORT_SALES` не менялся:

- URL из реестра `https://apps.fas.usda.gov/export-sales/` → **HTTP 404** (на 2026-11-26);
- официальная страница `https://www.fas.usda.gov/data/export-sales-query-system` → **HTTP 403**
  (бот-политика), API/ключи не используем (задача прямо запрещает ключи; OpenData API → 403);
- проверенные кандидаты `export-sales/*.csv` → 404; недельные данные отдаёт интерактивная
  query-система (нужен человек/сессия и проверка ToS), статического bulk-файла не подтверждено.

Итог: **FAS data не загружена, статус не менялся**, никаких чисел из него не цитируется. Нужен
отдельный OSINT-шаг: найти актуальный официальный URL отчётов/экспорта, проверить ToS и robots,
затем решать о загрузке (public domain, но интерфейс не «дешёвый»). Для соевого пилота пока
используется только PSD-срез (и PIT-вход для FAS остаётся будущим).

## 6. Что осталось Lead'у (после обеих задач — FRED и USDA)

Реестр/журнал/allowlists правит Lead; ниже точные значения.

**`protocols/source_registry.json` — источник `USDA_PSD`:**
- `status`: `"VERIFIED"` (данные реально получены и запинены; не менять `USDA_FAS_EXPORT_SALES` — он остаётся `"CATALOGUED"`);
- `retrieved_at`: `"2026-11-26"`;
- `license`: `"public domain (U.S. Government work)"` (без изменений);
- `url`: `"https://apps.fas.usda.gov/psdonline/"` (без изменений);
- `access`: `"file"` (без изменений); `vintage_support`: `"partial"` (без изменений, но в notes — что в пине нет истории ревизий);
- `notes` (предлагаемая строка): `"Compact public aggregate pinned: data/public/usda_psd_soybean_corn_2022_2026.csv (+ .meta.json), 275 observations, market years 2022-2026, Oilseed Soybean and Corn, US/BR/AR/CH/EU (+UA for corn), attributes Production/Exports/Ending Stocks/Imports/Domestic Consumption, unit 1000 MT, file_bytes 20882, sha256 c0f19817d954e1ccc2ce5fbcae5266f58f3da0404f85eab1dbe5b4a647ceb6ee, retrieved 2026-11-26 from psd_alldata_csv.zip (10459432 B, sha256 a293f2b05b1b7b185d7fdb7e1a562c43e6a0f190c6bf0e03fbe3df876286bd32). Full bulk CSV/zip not committed. No World total (no World row in psd_alldata; EU aggregate + members would double-count). No PIT/vintage history: one current value per key, source Month column retained verbatim. Licence public domain (U.S. Government work). See docs/240."`

**`tools/external_review_g2.py` — `ALLOWED_AGGREGATES` добавить строку:**
```python
    "data/public/usda_psd_soybean_corn_2022_2026.csv": "data/public/usda_psd_soybean_corn_2022_2026.meta.json",
```

**`tests/test_licence_hygiene.py` — `ALLOWED_TRACKED_RAW` добавить обе строки:**
```python
    "data/public/usda_psd_soybean_corn_2022_2026.csv",
    "data/public/usda_psd_soybean_corn_2022_2026.meta.json",
```

**Журнал/лицензионный регистр** (если ведётся по источнику): `status = OPEN`, `level =
"derived_public_aggregate"`, `conditions = "public domain (U.S. Government work); attribution
recommended"`, `terms/evidence = source page + bulk URL + дата 2026-11-26`; отдельный `terms_sha256`
для USDA может не требоваться (public domain), но если политика требует — хешировать страницу
`https://www.usda.gov/policies-and-links` (в этой задаче не читалась — **не проверено**).

**Документы**: `docs/238` §3 строка `USDA_PSD` сейчас `CATALOGUED`/«нельзя цитировать» — после
изменения реестра её нужно обновить на `VERIFIED` (правка вне моих scopes, делает Lead); `docs/205`
докет и счётчики 5/15 → 6/15 — тоже обновляет Lead; `docs/212` правило владельца не меняется.

## 7. Проверки

```powershell
python -m unittest tests.test_licence_hygiene tests.test_external_review_g2
# Ran 32 tests in 3.613s
# OK
```

Почему тесты зелёные до правки allowlist: новые файлы ещё **не tracked** (`git ls-files` их не
видит), а провенанс-проверка проверяет только allow-listed пути. После добавления allowlist в
`ALLOWED_AGGREGATES`/`ALLOWED_TRACKED_RAW` dry-run уже проходит: `allowed` включает наш файл,
`problems = []`. Проверка соответствия метаданных (без изменения allowlist):

```text
csv LF only: True | BOM: False | bytes: 20882
sha match: True c0f19817d954e1ccc2ce5fbcae5266f58f3da0404f85eab1dbe5b4a647ceb6ee
licence markers: ('public domain', ...) -> ['public domain']
provenance check: allowed = [..., 'data/public/usda_psd_soybean_corn_2022_2026.csv'], problems = []
```

## 8. Ограничения

1. Срез — датированный снимок **одного** месячного релиза PSD; при следующем обновлении PSD
   исторические MY-значения могут измениться. Это не PIT-архив и не ревизионная история.
2. World/глобальные балансы не публикуются (см. §3); агрегат EU в файле — отчётная единица PSD,
   а не «ЕС-27, посчитанный нами».
3. `USDA_FAS_EXPORT_SALES` не загружен (§5), остаётся `CATALOGUED`; API с ключом не использовался.
4. Полный ZIP/CSV не закоммичены; воспроизведение среза требует повторного скачивания bulk (URL
   может измениться — реестр хранит URL и дату пина; при смене bulk-файла старый sha256 остаётся
   провенансом этого среза).
5. Правовая оценка public domain — по лицензии USDA; это не юридическое заключение. Атрибуция USDA
   FAS рекомендуется.
6. В документ не включены числа как «новые открытия»: это официальные оценки PSD на дату, не наши
   выводы.

## 9. Файлы задачи

| Файл | Байт | sha256 |
|---|---:|---|
| `data/public/usda_psd_soybean_corn_2022_2026.csv` | 20 882 | `c0f19817d954e1ccc2ce5fbcae5266f58f3da0404f85eab1dbe5b4a647ceb6ee` |
| `data/public/usda_psd_soybean_corn_2022_2026.meta.json` | 2 607 | (sidecar; хеш среза внутри) |
| `docs/240_USDA_PSD_VERIFIED.md` | этот файл | — |

Не закоммичено (коммитит Lead после обновления реестра/allowlists). Не изменялись: реестр, журнал,
allowlists, тесты, tools, protocols, FRED-файлы параллельной задачи.
