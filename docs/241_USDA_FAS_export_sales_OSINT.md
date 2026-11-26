# 241. USDA FAS Export Sales: OSINT-поиск открытого канала

Дата: 2026-11-26. Задача: `task-76`, роль продукты/релизы (OSINT-шаг). Правило владельца:
**NO_PAID_LICENCES**, данные только из открытых S1–S3; ключи API не используем, 403/401 не обходим.
Контекст: `USDA_FAS_EXPORT_SALES` в реестре — **CATALOGUED** (бывший URL
`https://apps.fas.usda.gov/export-sales/` отдаёт 404, страница запроса — 403). Цель — найти
актуальный официальный публичный канал (только домены `fas.usda.gov`, `apps.fas.usda.gov`,
`data.gov`, `usda.gov`), проверить условия, при наличии статического файла — запинить компактный
срез.

**Итог: официальный публичный канал существует, но только как интерактивное веб-приложение
(ESRQS) и OAuth-API; keyless static public file не найден. Статус источника не повышается
(остаётся CATALOGUED/UNVERIFIED), CSV и `.meta.json` не созданы — данных, которые можно законно
запинить автоматически, нет.**

## 1. Задача и границы

| Что | Детали |
|---|---|
| Искали | недельные export sales США: commodity × destination country × week (sales/outstanding/exports), официальный публичный канал и его ToS |
| Проверяли только | `www.fas.usda.gov`, `apps.fas.usda.gov`, `catalog.data.gov`/`data.gov`, `www.usda.gov` |
| Не делали (сознательно) | не использовали API-ключи/OAuth-токены, в т.ч. встроенные в публичный SPA; не обходили 403/401 (не меняли UA, cookies, proxies, не парсили за login); не открывали gated/платные источники; не скачивали сырые ряды; не деанонимизировали лиц; не ставили пакеты |
| Режим запросов | только GET, UA `AMFE-research/1.0`, паузы 1–1.5 с, ~50 запросов; robots и ToS уважались (www.fas/usda robots — 403, дальше не шли) |
| Дата/время обращений | 2026-11-26, ~14:40–14:55 (Europe/Chisinau) |
| Запись | только `docs/241*`; реестр/allowlists/тесты не менялись; ничего не коммитилось; улики — в `%TEMP%\amfe_usda_fas_osint` (вне репозитория) |

## 2. Метод

1. Прямые HTTP GET к официальным URL с фиксацией `status`, `Content-Type`, `bytes`, `sha256`.
2. `robots.txt` для доменов; `data.gov` — `Allow: /`; `apps.fas.usda.gov` — 404 (файла нет);
   `www.fas.usda.gov` — 403 (не читается; трактуем как запрет и не обходим).
3. Поиск по data.gov catalog (HTML-поиск, страница датасета); API data.gov (CKAN) проверен, 404.
4. Официальный публичный `ESRQS_UserManual_PublicUser.pdf` (200) — роли и разделы Reports/Query/API.
5. Публичный SPA `https://apps.fas.usda.gov/esrqs/` (200) — открытые статические JS-бандлы
   прочитаны для карты маршрутов (без исполнения, без login): найдены `/reports/*`, `/lookups/*`,
   `/StaticReports/*`, `tokenEndpoint`, `apiEndpoint`.
6. API-эндпоинты проверялись только анонимным GET (без Authorization). 401 — фиксировали и
   останавливались, токен не добывали.

## 3. Находки (факты; достоверность — по шкале в §4)

### 3.1 Что доступно (200)

| URL | HTTP | Байт | SHA-256 (сокращённо) | Что это |
|---|---|---|---|---|
| `https://catalog.data.gov/dataset/export-sales-reporting` | 200 | 133 663 | `06474e73b8a15f0c…` | Официальная карточка data.gov: dataset **USDA-26351 «Export Sales Reporting»**, publisher **FAS**, «Information on US export sales, by commodity and country of destination, updated weekly»; **License: CC BY 4.0**, Access Level: public; Last Updated: 2015-03-12 |
| `https://data.gov/privacy-policy` | 200 | 131 758 | `52ad176aa1178b20…` | Data.gov Website Policies: federal data не подпадают под domestic copyright (17 U.S.C. § 105); attribution просят, но не требуют |
| `https://data.gov/robots.txt` | 200 | 61 | `378eb3f2f929180e…` | `User-agent: * Allow: /` |
| `https://catalog.data.gov/?keyword=export%20sales` | 200 | 137 395 | `7cd0f90be1320e64…` | Поиск: «Found 1 dataset» — только export-sales-reporting |
| `https://apps.fas.usda.gov/esrqs/` | 200 | 8 517 | `ee61014d3f16cd33…` | Новое официальное публичное приложение **ESRQS** (Angular SPA) |
| `https://apps.fas.usda.gov/esrqs/assets/ESRQS_UserManual_PublicUser.pdf` | 200 | 3 178 757 | `a8448537758c90db…` | Официальный Public User Manual: non-logged user «Can access ESR Query / Reports / ESR API» (§1.2) |
| `https://apps.fas.usda.gov/esrqs/api/lookups/Commodities` | 200 | 46 457 | `4f586a0e0f2f2c84…` | Публичный JSON-справочник товаров (только метаданные, не данные) |
| `https://apps.fas.usda.gov/ESRMS/media/` | 200 | 12 875 | `e703333c05e979e2…` | Redirect на `…/esrqs/media/help/index.html`: тренинг-медиа, не данные |
| `https://apps.fas.usda.gov/GATS/` | 200 | 739 | `d33e2871f217a9a5…` | JS-редирект на `detectscreen.aspx` (интерактивная legacy-система; не export sales) |

Карточка data.gov: **ресурс один** — HTML-ссылка на «Export Sales Query System»
(`http://apps.fas.usda.gov/esrquery/esrq.aspx`); файловых ресурсов (CSV/XLSX/ZIP/API) нет. Старый
URL реестра `https://apps.fas.usda.gov/export-sales/` — 404, старый `esrquery/esrq.aspx` — 404
(1903 B, sha `44ca4dcef66e7893…`), `esrd1.csv`/`essall.csv` — 404; `http://…/esrquery/esrq.aspx` —
сетевой timeout (10060).

### 3.2 Что закрыто/недоступно автоматически

| URL / группа | HTTP | Байт | SHA-256 | Вывод |
|---|---|---|---|---|
| `https://apps.fas.usda.gov/esrqs/api/reports/GetLatestPublishedWeekEndingDate` | **401** | 44 | `b8ff853947298732…` | `{"message":"OAuth token has expired"}` — API требует OAuth |
| `…/api/reports/GetPublishedDateAndWeekEndingDate`, `…/api/reports/WeekEndingDates`, `…/api/lookups/Countries` | **401** | 44 | тот же | То же: без токена данные не отдаются |
| `https://apps.fas.usda.gov/esrqs/api/reports/GetCsvFile?…` и `GetTrendCsvFile?…` | не вызывали | — | — | Видны в JS как эндпоинты с теми же 401-заголовками; токен не добывали |
| `https://apps.fas.usda.gov/esrqs/StaticReports/WeeklyHighlightsReport` (и `CWRCommoditySummary`, `CWRCountryCommoditySummary`, `BellReportCombined`) | **404** | 0 | — | Публичные «StaticReports» из SPA в анонимном доступе не отдаются; `.aspx`-варианты — 404 (1937/1941 B), директория `/StaticReports/` — 403 |
| `https://apps.fas.usda.gov/opendata/api/` | **403** | 13 | `f67df9bd405e5ee7…` | `"Bad API Key"` — OpenData API требует ключ; ключи не используем |
| `https://www.fas.usda.gov/data/export-sales-query-system` | **403** | 420 | `2922974528d1f673…` | Access Denied (бот-политика), не обходили |
| `https://www.fas.usda.gov/data/export-sales-reports` | **403** | 411 | `5d5c4736005b1332…` | То же |
| `https://www.fas.usda.gov/robots.txt` | **403** | 388 | `9ec1cf8570ad43fc…` | robots не читается; дальнейший обход запрещён |
| `https://www.fas.usda.gov/about-fas/website-policies` | **403** | 412 | `24154e706bd0885c…` | Официальные веб-политики FAS напрямую не проверены (403) |
| `https://www.usda.gov/robots.txt` | **403** | 380 | `1b3d45ec3a0e3cdc…` | То же |
| `https://www.usda.gov/policies-and-links` | **403** | 392 | `0b1b24afdfdc494c…` | Политики USDA напрямую не проверены |
| `https://data.gov/api/3/action/package_search…`, `https://catalog.data.gov/api/3/action/…` | 404 | 36 / 111 212 | `8645212e…` / `1193d78e…` | CKAN API data.gov недоступен/сменён; keyless-API через data.gov не найден |
| `https://data.gov/policies`, `/website-policies`, `/search`, `/policy` | 404 | 111 212 | `1193d78e…` | Авторитетная страница политик — только `data.gov/privacy-policy` |

### 3.3 Официальные условия

- **data.gov (карточка датасета):** License **`https://creativecommons.org/licenses/by/4.0`**
  (атрибуция), Access Level: public; dataset last updated 2015-03-12, accrual periodicity `R/P1D`.
- **data.gov (Website Policies, `privacy-policy`):** «In most cases, U.S. Federal data available
  through Data.gov is offered free and without restriction. Data and content created by government
  employees within the scope of their employment are not subject to domestic copyright protection
  under 17 U.S.C. § 105»; attribution — «while not required… we ask that proper credit be given».
- **FAS/USDA собственные страницы политик** — 403; **условия FAS напрямую не проверены**.
  Реестр проекта называет источник «public domain (U.S. Government work); attribution recommended»,
  но это утверждение в рамках OSINT-шага **не подтверждено**: доступная официальная карточка
  data.gov даёт более строгую формулировку **CC BY 4.0 (attribution)**. Расхождение зафиксировано
  для Lead; консервативно — attribution required.

## 4. Оценка достоверности

| Утверждение | Оценка | Почему |
|---|---|---|
| HTTP-статусы/байты/sha256 из §3 | **A** | собственные детерминированные GET, значения записаны и воспроизводимы (URL+дата) |
| Карточка data.gov — официальный публичный каталог USDA/FAS | **A** | 200, publisher FAS, идентификатор USDA-26351 |
| ESRQS — официальное публичное приложение с Public User | **A−** | официальный домен + официальный PDF-мануал; SPA 200 |
| Автоматический доступ к данным без ключа/токена возможен | **F (опровергнуто)** | все data-эндпоинты — 401 OAuth; static reports — 404; OpenData — 403 «Bad API Key» |
| Public domain статус FAS-данных | **C (не подтверждён)** | FAS/USDA policies — 403; data.gov metadata — CC BY 4.0; 17 U.S.C. §105 — общая норма, не проверка конкретного датасета |
| Данные обновляются еженедельно | **B** | так написано в карточке data.gov (но «Last Updated: 2015-03-12» — метаданные не обновлены; фактическую недельную публикацию в 2026 не подтверждали) |

## 5. Выводы: факты и интерпретация

**Факты.** (1) Официальный публичный каталог data.gov существует и доступен (200), но его
единственный ресурс — интерактивная HTML-система запросов, а не файл. (2) Новое приложение ESRQS
доступно (200); официальный мануал подтверждает наличие публичной роли, но (3) data-API ESRQS
требует OAuth (401), публичные StaticReports — 404, OpenData API — ключ (403), старые URL — 404,
`www.fas.usda.gov`/`www.usda.gov` — 403 по бот-политике. (4) Keyless static public file с
недельными export sales не найден.

**Интерпретация.** Официальный канал «есть», но он **интерактивный**: получить данные можно либо
через веб-интерфейс ESRQS человеком, либо через авторизованный API (OAuth/OpenData-ключ) — это уже
не автоматический OSINT и требует отдельного законного доступа. Поэтому:
- статус `USDA_FAS_EXPORT_SALES` **не повышается** — остаётся `CATALOGUED` / `UNVERIFIED`
  (в терминах docs/205);
- `data/public/usda_fas_*` **не создаются** — запинивать нечего; 12-недельный срез невозможен;
- проект не должен утверждать, что данные USDA FAS export sales «получены/проверены».

**Что нужно, чтобы двинуться дальше (по убыванию легальности/дешевизны):**
1. **Ручная выгрузка человеком** отчёта из публичного ESRQS UI (если он доступен без логина;
   мануал утверждает, что Public User может смотреть Reports/Query) с передачей файла и
   provenance: URL, дата, имя отчёта, период, скрин/лог. После этого evidence-qa может решать о
   `VERIFIED` — это уже не автоматический шаг.
2. **Официальный bulk/static endpoint** — не найден; мониторить `apps.fas.usda.gov/esrqs/` и
   data.gov, не выдавая ожидание за факт.
3. **Авторизованный доступ** (OAuth ESRQS / OpenData API key) — только через legal access request
   (G1/G2 по docs/224), ключи не хранить в репозитории и не использовать в OSINT-прогонах.
4. Контакт из карточки data.gov — `esr@fas.usda.gov`; запрос про bulk/API возможен только с
   разрешения владельца (письма не отправлялись).

## 6. Что обновить в реестре (делает Lead, не этот шаг)

- `protocols/source_registry.json`, `USDA_FAS_EXPORT_SALES`: **статус не менять** (`CATALOGUED`);
  `access` с «file» на «interactive/web_app»; `url` — заменить мёртвый
  `https://apps.fas.usda.gov/export-sales/` на `https://apps.fas.usda.gov/esrqs/` и добавить
  `catalog_url: https://catalog.data.gov/dataset/export-sales-reporting` (200, sha256
  `06474e73b8a15f0c5efd4d32e4cd060a4227e3d910f279f0d50b87d58355f8a8`); `retrieved_at` оставить
  `null`; в `notes` — «OSINT 2026-11-26: keyless static file не найден; ESRQS API — OAuth (401),
  StaticReports — 404, OpenData — 403; data.gov — только интерактивный ресурс; см. docs/241»;
  `license` — пометить расхождение: data.gov metadata CC BY 4.0 (attribution) vs public-domain
  утверждение реестра не подтверждено (FAS/USDA policies 403).
- `docs/162`, `docs/197`, `docs/205`: статус не меняется; в строках UNVERIFIED — причина
  «интерактивный/авторизованный канал, keyless static file нет», ссылка на docs/241.
- Не добавлять data/public-файл и не поднимать `VERIFIED`; allowlists/тесты — на стороне Lead.

## 7. Что осталось невозможным

- автоматическая загрузка недельных данных (CSV/XLSX/JSON) без OAuth/ключа — **невозможна** в
  рамках правил (ключи запрещены, 401/403 не обходятся);
- компактный срез «1 товар × 1 страна × 12 недель» и `AMFE-PUBLIC-USDA-FAS/1.0.0` — **не созданы**:
  нет доступного источника; создавать файл из ничего/из заглушек запрещено;
- PIT/винтажи недельных публикаций, проверка «обновлено на этой неделе», самостоятельная
  верификация FAS ToS/public-domain — не выполнены;
- `file_bytes`/`file_sha256`/`observations` для `meta.json` отсутствуют, потому что нет файла.

## 8. Что нельзя заявлять

- что данные USDA FAS export sales **получены, скачаны, запинены или VERIFIED**;
- что **public domain подтверждён** (доступная карточка data.gov говорит CC BY 4.0; FAS/USDA
  policies — 403);
- что **ESR API публичный/keyless** — он отдаёт 401 без OAuth;
- что **403 на www.fas.usda.gov — «ошибка источника»**; это бот-политика, её обход запрещён;
- что «данные недоступны в принципе» — человек через публичный UI может иметь доступ; и что
  «данные доступны» — автоматически получены они не были.

## 9. Приложение: улики

- `%TEMP%\amfe_usda_fas_osint\` (вне репозитория): сохранённые ответы и `probe*.json`,
  `api_probe1.json`, `static_probe*.json`, `policy_probe.json`, `final_probe.json`; PDF-мануал
  `ESRQS_manual_text.txt` (52 стр., извлечён `pypdf`).
- Все ключевые SHA-256 — в таблицах §3; они же — «дата + URL + статус + hash» страницы условий:
  **data.gov dataset** — 200, `06474e73…`; **data.gov privacy-policy** — 200, `52ad176a…`;
  **FAS website-policies** — 403, `24154e70…`; **USDA policies** — 403, `0b1b24af…`.
- robots: data.gov — 200 `378eb3f2…`; www.fas — 403 `9ec1cf85…`; www.usda — 403 `1b3d45ec…`;
  apps.fas — 404 (robots нет).
- Записей в `build/`, `data/public/`, `protocols/`, реестре нет; git не трогался.

## 10. Ссылки

- [docs/162](162_Реестр_источников.md) — реестр источников (статусы CATALOGUED/VERIFIED).
- [docs/197](197_Верификация_источников.md), [docs/205](205_Верификация_источников.md) —
  верификация источников и правило «403/JS-shell не обходим».
- [docs/224](224_Gated_источники.md) — классы доступа G0–G4 и legal access request.
- [docs/239](239_FRED_ALFRED_VERIFIED.md), [docs/240](240_USDA_PSD_VERIFIED.md) — примеры
  запиненных public-data артефактов (FRED, USDA PSD), к которым этот шаг не относится.
- `protocols/source_registry.json` — запись `USDA_FAS_EXPORT_SALES` (правит Lead).
- data.gov: [карточка датасета](https://catalog.data.gov/dataset/export-sales-reporting),
  [Website Policies](https://data.gov/privacy-policy);
  ESRQS: [приложение](https://apps.fas.usda.gov/esrqs/),
  [Public User Manual](https://apps.fas.usda.gov/esrqs/assets/ESRQS_UserManual_PublicUser.pdf).
- Правило проекта: [docs/205], [README](../README.md) — `RESEARCH_ONLY`, `admission=false`.
