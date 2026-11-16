# 156. Forecast API и клиентский пакет по нефтяному сценарию (G2)

Волна 22 (G2), трек H (продукты). Инструменты: [tools/forecast_api.py](../tools/forecast_api.py),
[tools/market_client_pack.py](../tools/market_client_pack.py). Тесты:
[tests/test_forecast_api.py](../tests/test_forecast_api.py) (10),
[tests/test_market_client_pack.py](../tests/test_market_client_pack.py) (12).
Артефакты: `build/api/` (в git не коммитится), `build/client_pack/market_hormuz_6m/`.

Источник чисел — только реальный `src/amfe/markets` (`OilModule`, `MarketResult.as_dict`,
[docs/147](147_Рынки_нефть.md)): сценарий `oil.brent` / `hormuz_closed_6m` (Ормуз, 6 месяцев).
Никакие числа не выдумываются и не «дополняются»: API и пакет переупаковывают одно и то же
детерминированное `MarketResult`.

## 1. Forecast API: контракт `amfe.forecast_api.v1`

Статический read-only контракт: инструмент не поднимает сервер, а материализует **ровно те JSON-
ответы**, которые вернул бы API. Директорию `build/api/` можно отдать любым static HTTP-сервером.

| Endpoint | Файл | Содержимое |
|---|---|---|
| `GET /v1/scenarios` | `index.json` | модули, сценарии, forecast-таргеты, политика, хеши файлов |
| `GET /v1/scenarios/{scenario_id}` | `scenarios/<module>__<scenario>.json` | полный `MarketResult`: `price_path`, `summary`, `assumptions`, `sensitivity`, `checks`, `meta`, `digest` |
| `GET /v1/forecast/{target}` | `forecasts/oil_brent.json` | конверт сценария: путь, пик/средняя и их low/central/high, чувствительность; **не** точечный прогноз |
| `GET /openapi.json` | `openapi.json` | OpenAPI 3.0.3: пути, параметры, схемы `Index`/`Scenario`/`Forecast`/`Error` |

Схема (`index.json`): `schema = amfe.forecast_api.v1`, `kind = index`, `api_version = v1`,
`generated_at`, `status = RESEARCH_ONLY`, `disclaimer`, `policy {read_only, research_only,
no_admission, no_point_forecast, no_raw_third_party_data, deterministic_except_generated_at}`,
`modules[]`, `scenarios[]`, `forecasts[]`, `endpoints[]`, `counts`, `files[]` (path/SHA-256/bytes).

Сценарий (фрагмент реальных полей): `id = oil.brent/hormuz_closed_6m`, `result.units = USD/bbl`,
`result.price_path = [168.85584, 139.864004, 123.480739, 119.78438, 116.132679, 112.470549]`,
`result.summary.brent_range_usd_per_bbl = {low: 139.04178, central: 168.85584, high: 268.523395,
spread_pct: 76.682}`, `result.digest`, 17 объявленных допущений (`source_class=assumption`),
9 проверок баланса. Forecast-таргет `oil_brent` повторяет конверт и добавляет
`target_definition`, `no_point_forecast: true`, `assumptions_count`, `result_digest`; алиасы
`oil.brent`, `oil_brent`, `brent` допустимы, неизвестный таргет → fail-closed.

Команда:

```powershell
python tools/forecast_api.py --out build/api --generated-at 2026-11-16T10:00:00Z
```

Выход: 1 модуль, 3 сценария (`hormuz_closed_6m`, `hormuz_closed_3m`,
`hormuz_closed_6m_no_spr`), 1 forecast-таргет; `index.json` + `openapi.json` + README.
Fail-closed: если пакет `src/amfe/markets` недоступен/пуст — exit 2 и ничего не пишется.
Детерминизм: при одинаковом `--generated-at` файлы байт-идентичны; при другой метке меняется
только `generated_at` (и хеши файлов в `index.json`, потому что они содержат эту метку).

## 2. Клиентский пакет по сценарию Ормуза

`python tools/market_client_pack.py --out build/client_pack/market_hormuz_6m --generated-at ...`
строит пакет из реального `OilModule.run("hormuz_closed_6m")`:

| Файл | Формат | Содержание |
|---|---|---|
| `01_Executive_Summary` | DOCX + PDF | клиент, сценарий, центральный пик, диапазон, средняя, трафик/обход/SPR, проверки, ограничения |
| `02_Scenario_Report` | DOCX + PDF | параметры сценария, ценовой путь, таблица чувствительности, проверки, digest |
| `03_Price_Sensitivity` | XLSX | листы «Сводка», «Цена по месяцам», «Чувствительность», «Допущения», «Проверки» |
| `04_Machine_Readable` | JSON | `MarketResult.as_dict()` + файлы пакета + лицензия (интеграция) |
| `05_Dashboard` | HTML | офлайн-панель (без JS/внешних URL): карточки пика/средней, путь, чувствительность, проверки |
| `06_Methodology_Appendix` | DOCX + PDF | движок, допущения и replace-with, смысл диапазона, ограничения |
| `07_Reproducibility` | JSON | digest результата, SHA-256 JSON/HTML, параметры и replace-with, команда, версия Python |
| `08_Licence_and_Disclaimer` | DOCX + PDF | research-only, отсутствие сырых данных, отсутствие инвестсовета |
| `README.md` | Markdown | состав, ключевые числа, воспроизведение |

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
| Статус | `RESEARCH_ONLY`; digest `44661501ce927c40…` |

Пакет собирается детерминированно: PDF — через LibreOffice (если он есть; иначе остаются DOCX,
и README это фиксирует); DOCX/PDF/XLSX-контейнеры могут нести служебные метки, поэтому
`07_Reproducibility.deterministic_artifacts` покрывает только JSON и HTML, а DOCX/XLSX/PDF
перечислены по именам. JSON-ответы и dashboard при фиксированном `--generated-at` байт-стабильны.

## 3. Как это продавать (без claims о допуске)

- **Forecast API** — подписка на статический контракт: клиент забирает JSON-файлы/scenarios и
  встраивает в свои панели; тарифы — за доступ к контракту и обновлениям, а не за «точность
  прогноза».
- **Клиентский пакет** — платный research-отчёт по одному сценарию: executive summary для
  руководства, Excel для аналитика, JSON для интеграции, методология и лицензия. Кастомизация —
  другой сценарий/клиент/шаблон.
- Обязательные формулировки: research-only, не допуск, не инвестиционная рекомендация,
  диапазон — варианты чувствительности, а не доверительный интервал; допущения должны быть
  заменены датированными публичными источниками.
- **Что не продаётся как «прогноз цены»**: пакет не содержит точечного прогноза и не является
  торговым сигналом; ценность — прозрачная сценарная арифметика с явными допущениями и
  воспроизводимостью.

## 4. Ограничения

1. **Research-only.** `OilModule` — детерминированная сценарная арифметика, не допущенная модель;
   ни API, ни пакет не меняют статусы CANDIDATE/SHADOW_TEST.
2. **Нет данных реального времени.** Все параметры — объявленные допущения; перед клиентским
   использованием заменяются датированными публичными значениями (ICE/EIA/IEA/OPEC).
3. **Диапазон ≠ доверительный интервал.** low/central/high — разброс по объявленным вариантам
   чувствительности, не вероятностная оценка.
4. **Сравнительная статика.** Нет ценового замещения, обратных связей и поведенческой динамики;
   сценарий не учитывает вторичные эффекты в экономике (это отдельный MRIO-слой).
5. **PDF.** Конвертация LibreOffice может быть недоступна (тогда DOCX) и не байт-детерминирована;
   это зафиксировано в README и reproducibility.
6. **API — статический контракт**, а не живой сервер: нет аутентификации, rate-limit, SLA и
   обновления в реальном времени; версионирование — через `api_version`/`schema`.
7. **Сырые данные не передаются:** в пакете/API только производные числа и объявленные
   допущения; сторонние лицензии не переоформляются.
8. **docs/147** — методологическая основа oil-модуля; docs/142 — образец world-пакета, чей подход
   переиспользован, но `tools/client_pack.py` не изменялся.

## 5. Тесты

- `python -m unittest tests.test_forecast_api` — **10 тестов**: схема/счётчики, все endpoint-файлы,
  реальные `MarketResult` поля, forecast-конверт без точечного прогноза, OpenAPI, отсутствие
  admission/raw/URL, консистентность SHA-256, детерминизм, fail-closed, CLI.
- `python -m unittest tests.test_market_client_pack` — **12 тестов**: все форматы (DOCX/PDF/XLSX/
  JSON/HTML/repro/licence/README), реальные числа в executive/report/XLSX/JSON, офлайн-dashboard,
  reproducibility-хеши, методология/лицензия, «числа не выдуманы», детерминизм JSON/HTML,
  fail-closed (неизвестный сценарий/пустой клиент), CLI `--help`/`--no-pdf`.

Итого **22 теста**, все зелёные (без сети; PDF-конвертация — локальный LibreOffice, если есть).

## 6. Файлы поставки

- `build/api/index.json`, `build/api/openapi.json`, `build/api/README.md`,
  `build/api/scenarios/*.json`, `build/api/forecasts/oil_brent.json`;
- `build/client_pack/market_hormuz_6m/01…08`, `README.md`;
- `tools/forecast_api.py`, `tools/market_client_pack.py`, тесты, docs/156 (этот файл).

## 7. Ссылки

- [docs/144](144_Товарные_модули_архитектура.md) — архитектура товарных модулей;
- [docs/147](147_Рынки_нефть.md) — нефтяной модуль: сценарии, допущения, диапазон;
- [docs/142](142_Клиентский_пакет.md) — клиентский пакет по world-сценарию (образец подхода);
- [docs/143](143_Отличие_от_ИИ_чата.md) — чем продукт отличается от чата с ИИ;
- `src/amfe/markets/{contract,engine,oil}.py` — источник чисел (не изменялся).
