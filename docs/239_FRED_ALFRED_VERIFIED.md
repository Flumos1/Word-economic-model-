# 239. FRED/ALFRED: условия и пин открытого ряда CPIAUCSL (2026-11-26)
Версия 1.0.0 · 2026-11-26. Задача `task-74`. Входы: [protocols/source_registry.json](../protocols/source_registry.json) (`FRED_ALFRED`), [docs/238](238_Карта_источников_без_лицензий.md) (правило владельца `NO_PAID_LICENCES`), образцы пинов `data/public/*` (docs/236). **Это не юридическая консультация**: ниже — что буквально написано на странице условий, дата и хеш страницы, и какой факт из этого следует для проекта.

## Метод
Три GET-запроса `python urllib` с User-Agent `AMFE-research/1.0` **2026-11-26**: страница условий FRED/ALFRED, страница ряда CPIAUCSL, публичный CSV `graph/fredgraph.csv?id=CPIAUCSL`. API-ключи не использовались, API не вызывался, ALFRED `downloaddata` (POST/PIT-винтажи) не использовался, paywall/лимиты не обходились. Скачан **один** компактный ряд; сырые HTML-страницы не сохранены, зафиксированы только их размер и SHA-256.

## Условия FRED/ALFRED
- URL: https://fred.stlouisfed.org/legal/
- Получено: HTTP 200, `text/html; charset=UTF-8`, **132 207 байт**, **SHA-256: `17485cd2c103ddf190362a1c6ac8b1a4121930730b916266186c142c31844594`**, дата 2026-11-26.
- Дословно (General License):
  > «You are granted a limited, revocable, non-exclusive, nonassignable, non-transferable, nonsublicenseable license to access and use the FRED® Services and the FRED® Content through a generally available web browser, mobile device or application, and to download or print a copy of any portion of the FRED® Content to which you have properly gained access solely for your personal, non-commercial use, provided that you keep all copyright or other proprietary notices intact. Any other use of the FRED® Content is strictly prohibited.»
- Дословно (FAQ, что можно): «You can do a lot of things with FRED data, graphs, maps, software, and mobile apps for your own personal, non-commercial use such as…» и в списке разрешённого — «Conduct research, and support your own policy positions with FRED data on social media or a personal blog»; там же: «For any of the above uses, please cite the source of the data and note that it was accessed via FRED. Also, be sure to include any copyright notices that appear with the data.»
- Дословно (запреты, применимы ко всем использованиям): «All use of FRED data—including non-commercial, educational, and personal use—is subject to the following prohibitions.»; про автоматизацию: «Engage, or otherwise participate, in the use of any data mining, mirroring, robots, scraping, or similar data-gathering or extraction methods that are disruptive, or adversely impacts the stability, performance, or availability of the FRED® Services.»; про API-канал: «(l) Use the FRED® API in connection with storing, caching, or archiving any portion of the FRED® Services or FRED® Content…»; ML/AI: «Use the FRED® Services or FRED® Content in connection with the development or training of any software program or system or machine learning…».
- **Вывод по факту (не юридическое заключение):** получение **одного публичного ряда** для личного некоммерческого исследования с атрибуцией **попадает** в General License и прямо названо в FAQ («Conduct research»). Автоматизированный bulk-сбор, API-кэширование/архивирование, перераспространение базы FRED и обучение ML на FRED-контенте — **запрещены/ограничены**; в этой задаче они не использовались (один GET публичного CSV, без API).
- Метка ряда CPIAUCSL (страница https://fred.stlouisfed.org/series/CPIAUCSL, HTTP 200, 91 929 байт, SHA-256 `4e9224355cab683f3ded5f9319a307b82015059664115c5bf2204865e7c916b6`, 2026-11-26): **«Public Domain: Citation Requested»**. Других лицензионных меток на странице нет.

## Что скачано
| Параметр | Значение |
|---|---|
| Ряд | **CPIAUCSL** — Consumer Price Index for All Urban Consumers: All Items in U.S. City Average |
| URL | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL` |
| Файл | `data/public/fred_cpiaucsl.csv` |
| Формат | `date,value`; даты источника `YYYY-MM-DD`; LF-переводы строк; 958 строк данных + заголовок (≤1000) |
| Байт | **17 767** |
| SHA-256 | **`a82d532c22d14c5dc7dc365413f03780185920c387a9efff4c3f0e4862cfaa25`** |
| Наблюдений | **958** |
| Покрытие | **1947-01-01 .. 2026-10-01**, monthly, SA |
| Единица | index 1982-84=100, seasonally adjusted |
| Метаданные | `data/public/fred_cpiaucsl.meta.json` (2 191 B), `schema = AMFE-PUBLIC-FRED/1.0.0`; в них — source_url, license, publisher, retrieved_at, unit, frequency, coverage, file_bytes, file_sha256, notes, citation, `terms_page`/`terms_sha256`, `series_page`/`series_page_sha256`/`series_label` |
| Дата получения | **2026-11-26** |

Проверка целостности: `hashlib.sha256(Path("data/public/fred_cpiaucsl.csv").read_bytes()).hexdigest() == "a82d532c…aa25"` (сверено при записи и повторно при проверке).

## Точная строка `license` для реестра
```
FRED/ALFRED Services Terms of Use: personal non-commercial use with attribution; CPIAUCSL labelled 'Public Domain: Citation Requested'; no bulk download, no API caching, no redistribution
```
(в meta-файле она же в поле `license`; содержит маркеры `non-commercial`/`attribution`, которые требует `LICENCE_MARKERS` в `tools/external_review_g2.py`.)

Что обновить Lead'у в `protocols/source_registry.json` для `FRED_ALFRED` (реестр не трогал — границы задачи): `status = "VERIFIED"`, `retrieved_at = "2026-11-26"`, `license` — строкой выше, `notes` — добавить: путь `data/public/fred_cpiaucsl.csv`, 17 767 B, sha256 `a82d532c…aa25`, 958 наблюдений (1947-01..2026-10), «один публичный ряд через CSV-эндпоинт; ALFRED-винтажи/PIT НЕ используются; не bulk, не перепродажа», ссылку на этот документ. Тогда докет `FRED_ALFRED` переходит из `PARTIAL` в `VERIFIED` (terms-хеш условия в журнале уже есть).

## Что добавить в allowlists (правит Lead, не эта задача)
- `tools/external_review_g2.py`, `ALLOWED_AGGREGATES`:
```
"data/public/fred_cpiaucsl.csv": "data/public/fred_cpiaucsl.meta.json",
```
- `tests/test_licence_hygiene.py`, `ALLOWED_TRACKED_RAW`:
```
"data/public/fred_cpiaucsl.csv",
"data/public/fred_cpiaucsl.meta.json",
```
Проверка sidecar логикой `_public_domain_aggregates` (in-memory allowlist, файл инструмента не менялся): `allowed` содержит `data/public/fred_cpiaucsl.csv`, `problems = []`.

## Ограничения и что НЕ заявляется
- Это **не PIT-винтаж**: скачаны только последние опубликованные значения; ALFRED-архив винтажей и первая публикация не загружались. Нельзя утверждать, что эти значения были доступны на исторические даты.
- Один ряд (CPIAUCSL), один срез; не bulk и не полная база FRED; кэширующего сервиса/API-кэша нет.
- Перераспространение/перепродажа базы FRED, коммерческое использование и обучение ML на FRED-контенте не заявляются и не выполнялись.
- Атрибуция обязательна (FRED + U.S. Bureau of Labor Statistics), citation есть в meta.
- Значения могут быть пересмотрены FRED/BLS после даты пина; пин фиксирует конкретные байты и хеш, а не «истину».
- Условия и метка ряда могут измениться; на 2026-11-26 зафиксированы SHA-256 страницы условий и страницы ряда.

## Проверки
```
$ python -m unittest tests.test_licence_hygiene tests.test_external_review_g2
Ran 32 tests in 4.013s
OK
```
(в этой песочнице DSH-sandbox не даёт Python удалять `tempfile`-каталоги 0700, поэтому без шима 3 теста падают на `PermissionError` в cleanup — это окружение, не логика; прогон выше выполнен с временным `sitecustomize.py` вне репозитория, заменяющим `tempfile.mkdtemp` на обычный `os.mkdir`. Код продукта/тестов не менялся, шим удалён.)
До добавления файла в allowlist тест `test_public_aggregates_carry_full_provenance`/`_tracked_raw_data` может считать пин неразрешённым — это ожидаемо и решается правкой Lead'а; sidecar уже проходит проверку `_public_domain_aggregates` (см. выше).

## Файлы
`data/public/fred_cpiaucsl.csv`, `data/public/fred_cpiaucsl.meta.json`, `docs/239_FRED_ALFRED_VERIFIED.md`. Реестр, журнал, allowlists, tools/tests не менялись; не закоммичено.
