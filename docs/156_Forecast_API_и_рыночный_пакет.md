# 156. Forecast API и клиентский пакет по нефтяному сценарию (G2 → G3)

Волна 22 (G2), трек H (продукты); обновлено в волне 23 (G3): контракт **v2** с паспортами
модулей из реестра, v1 сохранён как совместимый режим (детали интеграции — [docs/161](161_Реестр_API_паспорта.md)).
Инструменты: [tools/forecast_api.py](../tools/forecast_api.py),
[tools/market_client_pack.py](../tools/market_client_pack.py). Тесты:
[tests/test_forecast_api.py](../tests/test_forecast_api.py) (17),
[tests/test_market_client_pack.py](../tests/test_market_client_pack.py) (14).
Артефакты: `build/api/` (v2), `build/api_v1/` (v1-совместимый),
`build/client_pack/market_hormuz_6m/` (в git не коммитятся).

Источник чисел — только реальный `src/amfe/markets` (`OilModule`, `MarketResult.as_dict`,
[docs/147](147_Рынки_нефть.md)): сценарий `oil.brent` / `hormuz_closed_6m` (Ормуз, 6 месяцев).
Паспорта модулей — метаданные реестра ([docs/157](157_Реестр_модулей.md)), а не числа модели.
Никакие числа не выдумываются: API и пакет переупаковывают одно и то же детерминированное
`MarketResult`.


> **G5 (2026-11-21):** контракт расширен: 9 паспортов, 2 market-модуля (oil + gas),
> 8 сценариев, 2 forecast-конверта; в v2 добавлен generic-конверт и `gas_ttf`,
> v1 остаётся строго совместимым (1/3/1). Детали — docs/161 §11.

## 1. Forecast API: контракт `amfe.forecast_api.v2`

Статический read-only контракт: инструмент не поднимает сервер, а материализует **ровно те JSON-
ответы**, которые вернул бы API. Директорию `build/api/` можно отдать любым static HTTP-сервером.

| Endpoint | Файл | Содержимое |
|---|---|---|
| `GET /v1/modules` | `modules.json` | **9 модулей** (паспорта реестра): `module_id`, `version`, `status`, `kind`, `limitations`, `code_sha256`, `data_status`, `docs`, `tests` (новое в v2) |
| `GET /v1/scenarios` | `index.json` | модули, 8 сценариев, forecast-таргеты, политика, счётчики, хеши файлов |
| `GET /v1/scenarios/{scenario_id}` | `scenarios/<module>__<scenario>.json` | полный `MarketResult`: `price_path`, `summary`, `assumptions`, `sensitivity`, `checks`, `meta`, `digest`; плюс `module_id` и `module_ref` |
| `GET /v1/forecast/{target}` | `forecasts/oil_brent.json` | конверт сценария: путь, пик/средняя и их low/central/high, чувствительность; **не** точечный прогноз |
| `GET /openapi.json` | `openapi.json` | спецификация контракта: OpenAPI 2.0.0 (в файле `openapi: 3.0.3`, `info.version` = 2.0.0), схемы `Index`/`Modules`/`Scenario`/`Forecast`/`Error` |

Схема (`index.json`): `api_version = v2`, `schema = amfe.forecast_api.v2`,
`api_versions = [v1, v2]`, `backward_compatible_with = amfe.forecast_api.v1`, `generated_at`,
`status = RESEARCH_ONLY`, `disclaimer`, `policy {read_only, research_only, no_admission,
no_point_forecast, no_raw_third_party_data, deterministic_except_generated_at}`, `modules[]`,
`scenarios[]`, `forecasts[]`, `endpoints[]`, `counts {modules: 9, market_modules: 2, scenarios: 8,
forecasts: 1}`, `files[]` (path/SHA-256/bytes).

Сценарий (фрагмент реальных полей): `id = oil.brent/hormuz_closed_6m`, `result.units = USD/bbl`,
`price_path = [168.85584, 139.864004, 123.480739, 119.78438, 116.132679, 112.470549]`,
`result.summary.brent_range_usd_per_bbl = {low: 139.04178, central: 168.85584, high: 268.523395,
spread_pct: 76.682}`, `result.digest`, `module_ref = {module_id: oil.brent, version: 0.1.0,
kind: market, status: RESEARCH_ONLY, code_sha256}`, 17 объявленных допущений
(`source_class=assumption`), 9 проверок баланса. Forecast-таргет `oil_brent` повторяет конверт и
добавляет `target_definition`, `no_point_forecast: true`, `assumptions_count`, `result_digest`,
`module_ref`; алиасы `oil.brent`, `oil_brent`, `brent` допустимы, неизвестный таргет →
fail-closed.

**Совместимость v1.** `python tools/forecast_api.py --api-version v1 --out build/api_v1`
воспроизводит контракт `amfe.forecast_api.v1` **без реестра модулей** (те же поля, что в волне 22:
в v1 только нефтяной модуль, 3 сценария, один forecast, без `/v1/modules` и без `module_ref`). Это гарантирует,
что клиенты v1 не ломаются, а v2 только добавляет поля. В v2 поле `modules[]` в `index.json`
описывает 5 паспортов, а `market_modules`/`scenarios` остаются про рыночный модуль-источник.

Команда:

```powershell
python tools/forecast_api.py --out build/api --generated-at 2026-11-16T10:00:00Z
```

Выход v2: `index.json`, `modules.json`, `openapi.json`, `README.md`, 5 файлов
`modules/<module_id>.json`, 3 сценария, один forecast-таргет. Fail-closed: если пакет
`src/amfe/markets` или реестр модулей недоступен — exit 2 и ничего не пишется. Детерминизм: при
одинаковом `--generated-at` файлы байт-идентичны; при другой метке меняется только `generated_at`
(и хеши в `index.json`, потому что файлы содержат эту метку).

## 2. Клиентский пакет по сценарию Ормуза

`python tools/market_client_pack.py --out build/client_pack/market_hormuz_6m --generated-at ...`
строит пакет из реального `OilModule.run("hormuz_closed_6m")` и читает паспорт модуля из того же
реестра:

| Файл | Формат | Содержание |
|---|---|---|
| `01_Executive_Summary` | DOCX + PDF | клиент, сценарий, центральный пик, диапазон, средняя, трафик/обход/SPR, проверки, ограничения, версия модуля |
| `02_Scenario_Report` | DOCX + PDF | параметры сценария, ценовой путь, таблица чувствительности, проверки, digest, `module_ref` |
| `03_Price_Sensitivity` | XLSX | листы «Сводка», «Цена по месяцам», «Чувствительность», «Допущения», «Проверки» |
| `04_Machine_Readable` | JSON | `MarketResult.as_dict()` + `module_ref` + файлы пакета + лицензия (интеграция) |
| `05_Dashboard` | HTML | офлайн-панель (без JS/внешних URL): карточки пика/средней, путь, чувствительность, проверки, паспорт |
| `06_Methodology_Appendix` | DOCX + PDF | движок, допущения и replace-with, смысл диапазона, ограничения |
| `07_Reproducibility` | JSON | digest результата, SHA-256 JSON/HTML, команда, версия Python; `10_Module_Passports.json` входит в детерминированные артефакты |
| `08_Licence_and_Disclaimer` | DOCX + PDF | research-only, отсутствие сырых данных, отсутствие инвестсовета |
| `10_Module_Passports` | JSON | паспорт модуля + список всех 9 модулей реестра: версия, статус, ограничения, code/data SHA-256, docs/tests; ссылка на реестр и его SHA-256 |
| `README.md` | Markdown | состав, ключевые числа, паспорт, воспроизведение |

Ключевые числа (реальный прогон 2026-11-16, `hormuz_closed_6m`):

| Показатель | Значение |
|---|---|
| Центральный пик Brent | **168.85584 USD/bbl** (M1) |
| Диапазон пика (low/central/high) | **139.04178 / 168.85584 / 268.523395** USD/bbl (spread 76.682%) |
| Средняя цена за 6 мес. | **130.098032 USD/bbl**; диапазон 115.862482–165.301602 |
| Трафик Ормуза / обход / SPR release | 20.5 / 4.2 / 1.5 mb/d |
| Чувствительность | 7 вариантов + `closure_9m`; крайние: elasticity_less_elastic 268.523395, elasticity_more_elastic 139.04178 |
| Проверки | 9/9 PASS (`mass_balance`, `stocks_non_negative`, `price_positive`, `strategic_release_feasible`, `elasticity_ramp_monotone`, `no_spurious_shortage`, `range_ordered`, `assumptions_classified`, `deterministic`) |
| Допущения | 17, все `source_class=assumption`, у каждого replace-with источник |
| Модуль | `oil.brent@0.1.0`, `kind=market`, `RESEARCH_ONLY`, code SHA-256 `0bbe243ca6098359…` |
| Статус | `RESEARCH_ONLY`; digest `44661501ce927c40…` |

Пакет собирается детерминированно: PDF — через LibreOffice (если он есть; иначе остаются DOCX,
и README это фиксирует); DOCX/PDF/XLSX-контейнеры могут нести служебные метки, поэтому
`07_Reproducibility.deterministic_artifacts` покрывает только JSON и HTML, а DOCX/XLSX/PDF
перечислены по именам. JSON-ответы, dashboard и `10_Module_Passports.json` при фиксированном
`--generated-at` байт-стабильны.

## 3. Как это продавать (без claims о допуске)

- **Forecast API** — подписка на статический контракт: клиент забирает JSON-файлы/scenarios и
  встраивает в свои панели; тарифы — за доступ к контракту и обновлениям, а не за «точность
  прогноза». В v2 в подписку входит и машиночитаемый паспорт модуля (версия, ограничения, хеши) —
  это то, что делает поставку проверяемой.
- **Клиентский пакет** — платный research-отчёт по одному сценарию: executive summary для
  руководства, Excel для аналитика, JSON для интеграции, методология, лицензия и паспорт модуля.
  Кастомизация — другой сценарий/клиент/шаблон.
- Обязательные формулировки: research-only, не допуск, не инвестиционная рекомендация,
  диапазон — варианты чувствительности, а не доверительный интервал; допущения должны быть
  заменены датированными публичными источниками.
- **Что не продаётся как «прогноз цены»**: пакет не содержит точечного прогноза и не является
  торговым сигналом; ценность — прозрачная сценарная арифметика с явными допущениями,
  воспроизводимостью и паспортом версии.

## 4. Ограничения

1. **Research-only.** `OilModule` — детерминированная сценарная арифметика, не допущенная модель;
   ни API, ни пакет не меняют статусы CANDIDATE/SHADOW_TEST. Паспорт модуля — метаданные, не допуск.
2. **Нет данных реального времени.** Все параметры — объявленные допущения; перед клиентским
   использованием заменяются датированными публичными значениями (ICE/EIA/IEA/OPEC).
3. **Диапазон ≠ доверительный интервал.** low/central/high — разброс по объявленным вариантам
   чувствительности, не вероятностная оценка.
4. **Сравнительная статика.** Нет ценового замещения, обратных связей и поведенческой динамики;
   сценарий не учитывает вторичные эффекты в экономике (это отдельный MRIO-слой, docs/152/159).
5. **PDF.** Конвертация LibreOffice может быть недоступна (тогда DOCX) и не байт-детерминирована;
   это зафиксировано в README и reproducibility.
6. **API — статический контракт**, а не живой сервер: нет аутентификации, rate-limit, SLA и
   обновления в реальном времени; версионирование — через `api_version`/`schema`.
7. **Реестр модулей обязателен для v2**: API и пакет fail-closed, если `build/registry/modules.jsonl`
   отсутствует или битый (`python tools/register_modules.py`). В v1 реестр не требуется.
8. **Сырые данные не передаются:** в пакете/API только производные числа, объявленные допущения и
   паспорт модуля; сторонние лицензии не переоформляются.

## 5. Тесты

- `python tests/test_forecast_api.py` — **17 тестов**: схема/счётчики v2, `/v1/modules` с паспортами
  (9 модулей, версии/статусы/ограничения/хеши), связь `module_ref` со сценарием/forecast, все
  endpoint-файлы, реальные `MarketResult` поля, forecast-конверт без точечного прогноза, OpenAPI,
  отсутствие admission/raw/URL, консистентность SHA-256, детерминизм, fail-closed (нет реестра/
  пакета), v1-совместимость (`--api-version v1`), CLI.
- `python tests/test_market_client_pack.py` — **14 тестов**: все форматы (DOCX/PDF/XLSX/JSON/HTML/
  repro/licence/passports/README), реальные числа в executive/report/XLSX/JSON, офлайн-dashboard,
  reproducibility-хеши, методология/лицензия, «числа не выдуманы», паспорт модуля = реестр,
  fail-closed реестра, детерминизм JSON/HTML, CLI `--help`/`--no-pdf`.

Итого **27 тестов**, все зелёные (без сети; PDF-конвертация — локальный LibreOffice, если есть).

## 6. Файлы поставки

- `build/api/index.json`, `build/api/modules.json`, `build/api/openapi.json`, `build/api/README.md`,
  `build/api/modules/*.json` (5), `build/api/scenarios/*.json` (3), `build/api/forecasts/oil_brent.json`;
- `build/api_v1/` — v1-совместимый контракт (без реестра);
- `build/client_pack/market_hormuz_6m/01…08`, `10_Module_Passports.json`, `README.md`;
- `tools/forecast_api.py`, `tools/market_client_pack.py`, тесты, docs/156 (этот файл), docs/161.

## 7. Ссылки

- [docs/161](161_Реестр_API_паспорта.md) — как реестр модулей связан с API/пакетом (v2);
- [docs/157](157_Реестр_модулей.md) — реестр модулей и паспорт;
- [docs/144](144_Товарные_модули_архитектура.md) — архитектура товарных модулей;
- [docs/147](147_Рынки_нефть.md) — нефтяной модуль: сценарии, допущения, диапазон;
- [docs/142](142_Клиентский_пакет.md) — клиентский пакет по world-сценарию (образец подхода);
- [docs/143](143_Отличие_от_ИИ_чата.md) — чем продукт отличается от чата с ИИ;
- `src/amfe/markets/{contract,engine,oil}.py` — источник чисел (не изменялся);
- `src/amfe/learning/module_registry.py` — источник паспортов (только чтение).