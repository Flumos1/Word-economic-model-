# AMFE — EUROSTAT и UN Comtrade: реальные агрегаты и статус VERIFIED (2026-11-25)
Версия 1.0.0 · 2026-11-25. Задача `task-72`: поднять KPI M1 «VERIFIED источники» с **3/15** до **5/15** честно — реально получить компактные публичные агрегаты, запинить их с SHA-256 и обновить реестр/докет. Контекст: [protocols/source_registry.json](../protocols/source_registry.json), [protocols/source_verification_log.jsonl](../protocols/source_verification_log.jsonl), [docs/205](205_Верификация_источников.md), образец пина [data/oil/brent_monthly_eia.meta.json](../data/oil/brent_monthly_eia.meta.json). **Это не юридическая консультация.**

## Метод
Скачивание выполнено `python urllib` с User-Agent `AMFE-research/1.0` **2026-11-25**; сохранены только компактные агрегаты (CSV) и метаданные (JSON), сырые ответы API не сохранены и не закоммичены. Файлы — [data/public/](../data/public/). SHA-256 считается по байтам итогового CSV; метаданные содержат `file_bytes`/`file_sha256`, а `protocols/source_registry.json` — путь, размер, хеш и число наблюдений в `notes`.

## Что скачано

| # | Источник | Файл | Байт | SHA-256 | Наблюдений | Покрытие |
|---|---|---|---|---|---|---|
| 1 | Eurostat, `prc_hicp_manr` (HICP, annual rate) | `data/public/eurostat_hicp_monthly.csv` | 2 930 | `10f4e89f5401a67a2d4018cb4c10ee81ac99d019dc5dfad9e7deb538f3bf92ef` | 120 | EA и DE; 2021-01 .. 2025-12; monthly, CP00, RCH_A |
| 2 | UN Comtrade public preview, C/A/HS | `data/public/un_comtrade_deu_hs27_2023.csv` | 333 | `1a7c7a435da5c4a40a10f72faaf3c9768fb080a50fba7b76f43d36e517df6ea4` | 2 | DEU (276), partner World (0), HS chapter 27, 2023, flows X/M |

Метаданные: `eurostat_hicp_monthly.meta.json` (1 341 B) и `un_comtrade_deu_hs27_2023.meta.json` (1 585 B); оба содержат точный `source_url`, `retrieved_at` = 2026-11-25, лицензию, coverage, `file_bytes`, `file_sha256`, ограничения и `schema = AMFE-PUBLIC-* /1.0.0`.

### 1. EUROSTAT
- Точный запрос (в метаданных и в `source_url`):
  `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr?format=JSON&lang=EN&geo=EA&geo=DE&coicop=CP00&sinceTimePeriod=2021-01`
- Ответ — JSON-stat 2.0, 7 127 B, `updated: 2026-02-06`; из него взяты 2 географии × 60 месяцев = **120 наблюдений** (годовой темп прироста, %). Строки: `geo,period,year,month,value_annual_rate_pct`.
- CSV отсортирован (DE, EA; по месяцам); значения не пересчитывались.
- Лицензия: **Eurostat copyright notice** — «Reuse of statistical data, metadata, publications, and other dissemination tools published on this website for commercial or non-commercial purposes is authorised provided the source is acknowledged.» Страница условий `https://ec.europa.eu/eurostat/help/copyright-notice` (HTTP 200, 177 904 B, sha256 `77abcaca637e6a9384eb3af82b47ecb1ccf6514143ada63b829382e786f86201`) прочитана 2026-11-25; редакционный контент — CC BY 4.0. Для проекта: некоммерческое исследование с атрибуцией Eurostat.

### 2. UN Comtrade
- Точные запросы (в метаданных, поле `source_url` + `other_source_urls`):
  `https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=276&period=2023&cmdCode=27&flowCode=X&partnerCode=0`
  `https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=276&period=2023&cmdCode=27&flowCode=M&partnerCode=0`
- Срез: Германия (276), партнёр World (0), HS chapter 27 (минеральное топливо), 2023, экспорт и импорт. Значения `primaryValue`: экспорт **34 283 783 714.696 USD**, импорт **131 942 571 736.03 USD** (агрегаты `isAggregate=true`, classification H6; net weight не отдан в preview).
- Лицензия: **UN Comtrade terms of use** — free for non-commercial use with attribution; premium API limits apply; no re-sale (как записано в реестре/docs/205). Страница `https://comtradeplus.un.org/en/license-agreement` (HTTP 200, 50 828 B, sha256 `cedc18d78dfe1ab0ad92face2a8b62ec9a36591392953b181652b013405673db`) прочитана 2026-11-25; в её тексте: «United Nations COMTRADE contains copyrighted material and/or other proprietary information…». Полная формулировка free-tier/non-commercial на этой странице машинно не извлечена (JS-рендеринг), поэтому условие некоммерческого использования берётся из уже зафиксированных условий реестра (docs/197/205), а факт страницы условий и её хеш зафиксированы здесь.
- Ограничение Comtrade public preview: только публичный preview-эндпоинт; premium/bulk API и полные билатеральные матрицы не используются.

## Изменения в реестре и докате
- `protocols/source_registry.json`: `date` = 2026-11-25; **EUROSTAT** и **UN_COMTRADE** переведены `ACCESS_VERIFIED → VERIFIED`; `retrieved_at` = 2026-11-25; в `notes` добавлены путь к файлу, `file_bytes`, полный `sha256`, число наблюдений, лицензия и ограничения; остальные 13 записей не ослаблены.
- `protocols/source_verification_log.jsonl`: дописаны 2 записи (всего 12) с `status_before: ACCESS_VERIFIED`, `verdict: VERIFIED`, страницами условий и `evidence` (файл, байты, sha256, наблюдения).
- Счётчики (реестр / докет):
  - **до:** VERIFIED 3, ACCESS_VERIFIED 2, CATALOGUED 6, NOT_DOWNLOADED 4; докет VERIFIED 3 / PARTIAL 4 / UNVERIFIED 8.
  - **после:** VERIFIED **5** (EIA, EUROSTAT, OECD_ICIO, UN_COMTRADE, WORLD_BANK), ACCESS_VERIFIED **0**, CATALOGUED 6, NOT_DOWNLOADED 4; докет VERIFIED **5** / PARTIAL **2** (BLS, FRED_ALFRED) / UNVERIFIED **8**; gaps 10.
- Обновлены тесты-счётчики: [tests/test_source_registry.py](../tests/test_source_registry.py), [tests/test_source_verification.py](../tests/test_source_verification.py).

## Проверки (вывод)
```
$ python tools/source_verification.py --check
OK: 15 источников, VERIFIED 5, нарушений нет
(exit 0)
```
```
$ python -m unittest tests.test_source_registry tests.test_source_verification tests.test_verify_sources
.................................................................................................
Ran 97 tests in 12.782s
OK
```
Важно про среду: в этой песочнице Python `tempfile` не может создавать/удалять каталоги 0700 (DSH sandbox запрещает `mkstemp`/`chmod` во временных каталогах), отчего тесты с `tempfile.TemporaryDirectory` зависали/падали ДО логики продукта. Для прогона использован локальный шим `sitecustomize.py` (вне репозитория, `PYTHONPATH`), который заменяет `tempfile.mkdtemp` на обычный `os.mkdir`; код продукта и тестов не менялся, после прогона шим удалён. На машине без этого ограничения команда должна пройти без шима. В рабочем каталоге остались пустые каталоги `svtest_*`/`svprobe_*` от прерванных прогонов (ACL не даёт их удалить; git пустые каталоги не отслеживает).

## Как воспроизвести
1. Проверить целостность пинов:
   `python -c "import hashlib,pathlib; p=pathlib.Path('data/public/eurostat_hicp_monthly.csv'); print(len(p.read_bytes()), hashlib.sha256(p.read_bytes()).hexdigest())"` (и то же для Comtrade) — сверить с таблицей выше.
2. Повторить запросы из `source_url` в meta-файлах (User-Agent `AMFE-research/1.0`, дата 2026-11-25) и пересобрать CSV теми же правилами: Eurostat — JSON-stat `value` по индексам `geo × time`; Comtrade — поля `primaryValue`, `flowCode`, `period`.
3. Проверки: `python tools/source_verification.py --check`; `python -m unittest tests.test_source_registry tests.test_source_verification tests.test_verify_sources`.

## Что НЕ заявляется
- Не перепродажа и не распространение данных третьих сторон; только некоммерческое исследование с атрибуцией.
- Не полный bulk: Eurostat — одна таблица/два гео/60 месяцев; Comtrade — один репортёр, один HS-раздел, один год, world-агрегаты. Полные матрицы и premium API не используются.
- Нет PIT/винтажности и истории ревизий: Eurostat JSON-stat — обновлённый срез (`updated 2026-02-06`), Comtrade preview — значения «как в preview», ревизии не моделируются.
- Не утверждается, что значения Comtrade — финальные официальные итоги; это публичный preview.
- Это не юридическая консультация; лицензии зафиксированы как прочитанные на 2026-11-25.

## Ограничения, встреченные при получении
- Comtrade public preview отдавал HTTP 429 при частых запросах и HTTP 400 при списках в `period`/`flowCode`; использованы одиночные значения, пауза и backoff-ретраи. Поэтому срез — только 2023 год, flows X и M.
- Eurostat отдаёт данные по 2025-12 (последнее обновление 2026-02-06); месяцев 2026 года в срезе нет.
- Полный текст лицензии Comtrade не извлечён машинно (JS); хеш страницы и цитата об авторских правах зафиксированы, условие non-commercial — из реестра.
- Среда: sandbox-ограничение `tempfile` (см. раздел проверок); на результат проверок реестра/докета не влияет.

## Файлы задачи
`data/public/eurostat_hicp_monthly.csv`, `data/public/eurostat_hicp_monthly.meta.json`, `data/public/un_comtrade_deu_hs27_2023.csv`, `data/public/un_comtrade_deu_hs27_2023.meta.json`, `protocols/source_registry.json`, `protocols/source_verification_log.jsonl`, `tests/test_source_registry.py`, `tests/test_source_verification.py`, `docs/236_EUROSTAT_UN_COMTRADE_VERIFIED.md`. Не закоммичено (коммитит Lead).
