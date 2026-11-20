# 171. Client Pack v2: единый клиентский пакет

Дата: 2026-11-17 (обновлено 2026-11-20: опциональный раздел Bridge, 42 теста). Волна G5.
Инструмент: [tools/client_pack_v2.py](../tools/client_pack_v2.py),
тесты: [tests/test_client_pack_v2.py](../tests/test_client_pack_v2.py) (42 теста).
Источники чисел: `build/product/forecast_latest.json` (product feed), `protocols/module_registry`
(паспорта модулей), рыночный сценарий `hormuz_closed_6m` (oil v3), `build/api/index.json`
(forecast API v2), **все** forecast-контракты из индекса (`oil_brent`, `gas_ttf`),
`build/registry/modules.jsonl` и — если он есть — `build/forecast_world_bridge/report.json`
(мост «прогноз → мир», §7). Ничего не пересчитывается и не коммитится.

## 1. Что это

Client Pack v2 — один пакет для исследовательского клиента, который сводит воедино то, что
уже опубликовано раздельными инструментами (product pack, market client pack, API, реестр
модулей), и явно проговаривает границы: **ни одна модель не допущена**, кандидат остаётся в
статусе `CANDIDATE`, C4/C6 открыты, рыночные числа — сценарная арифметика над объявленными
допущениями, а не доверительный интервал или прогноз.

Состав (8 файлов):
- `01_Executive_Summary.docx` — ключевые числа только из артефактов + дисклеймер, при наличии
  моста — таблицы «сценарий → страна/отрасль» (§7);
- `02_Numbers.xlsx` — кандидат/срез API, паспорта модулей, **рыночные модули и forecast-контракты
  (включая газовый)**, рыночный сценарий, источники, листы `Bridge*` при наличии моста;
- `03_Dashboard.html` — статическая панель без JS и внешних ресурсов;
- `04_Machine_Readable.json` — машиночитаемая версия с источником каждого блока (включая
  опциональный `bridge`);
- `05_Methodology_and_Sources.docx` — метод, перечень источников, детерминизм;
- `06_Licence_and_Disclaimer.docx` — лицензии/дисклеймеры `RESEARCH_ONLY`;
- `07_Reproducibility.json` — команда, хеши детерминированных артефактов, `file_count`;
- `README.md` — краткая инструкция.

## 2. Источники и честность чисел

| Блок | Источник | Примечание |
|---|---|---|
| Кандидат, точечный прогноз, 19 квантилей, C4/C6 | `build/product/forecast_latest.json` | статус `CANDIDATE`, C4 FAIL/открыт, C6 `NO_RESOLVED_FORECASTS` |
| Паспорта модулей | `build/registry/modules.jsonl` + реестр | **7 модулей**, все `RESEARCH_ONLY`, code_sha256 file-level |
| Рыночные модули | `build/api/index.json` → `market_modules[]` | **2**: `gas.lng_eu`, `oil.brent`; счётчик выводится из индекса |
| Forecast-контракты | `build/api/index.json` → `forecasts[]` | **2**: `oil_brent` (USD/bbl) и `gas_ttf` (USD/MMBtu), статус `RESEARCH_ONLY` |
| Рыночный сценарий | oil v3 `hormuz_closed_6m` | пик/среднее Brent, units/assumptions, `RESEARCH_ONLY` |
| Forecast API | `build/api/index.json` | версия v2, модули/сценарии/эндпоинты |
| Мост «прогноз → мир» (опционально) | `build/forecast_world_bridge/report.json` или `--bridge` | reduced-form, `RESEARCH_ONLY`/`PARTIAL_INPUTS`, SHA-256, без сырых рядов (§7) |
| Лицензии | `protocols/licence_register.json` | ссылки на записи, без сырых значений |

Все числа пакета берутся из артефактов; пакет fail-closed: отсутствие любого **обязательного**
входа — ошибка, а не «пустой» документ (единственное исключение — опциональный мост, §7: его
отсутствие даёт `available=false` с причиной, а битый/чужой артефакт — ошибку). Количество
паспортов, рыночных модулей и forecast-контрактов **не захардкожено**: оно считается по реестру
и `api/index.json`, поэтому старый индекс с 5 паспортами/1 market-модулем и новый с 7/2
собираются одним и тем же кодом (v1-совместимость выходных ключей сохранена:
`forecast_api.forecast` по-прежнему указывает на нефтяной контракт).

## 3. Детерминизм

DOCX/XLSX нормализуются так же, как в `market_client_pack.py`: фиксируются zip-таймстампы и
`docProps/core.xml` (`dcterms:created/modified` → `generated_at`), поэтому две сборки при одном
`--generated-at` байт-идентичны; `07_Reproducibility.json` фиксирует хеши детерминированных
артефактов. JSON-документ содержит `admission=false`, `research_only=true`.

## 4. Ограничения

- Допущенной модели нет: пакет публикует `CANDIDATE`, C4 и C6 не закрыты; это не инвестиционная
  рекомендация и не прогноз.
- API статический (файлы), не живой шлюз; хеши — file-level, не git-коммит.
- Рыночный сценарий — сравнительная статика oil v3, не вероятностный прогноз.
- Газовый контракт (`gas_ttf`) — это forecast-конверт газового модуля (TTF/JKM, `RESEARCH_ONLY`),
  а не второй рыночный сценарий: сценарий пакета по-прежнему один (`hormuz_closed_6m`).
- Мост «прогноз → мир» — **reduced-form**: линейная сравнительная статика по объявленным
  эластичностям и сепарабельная аллокация страна×отрасль; это не прогноз мира, не
  поведенческая модель и не допуск (§7).
- Пакет не заменяет независимую проверку/подпись; client-facing использование — после G5/G6.

## 5. Сборка и проверка

```bash
python tools/register_modules.py
python tools/product_pack.py
python tools/client_pack_v2.py --out build/client_pack/v2_7 \
  --generated-at 2026-11-19T12:00:00Z --json
# с мостом «прогноз → мир» (каталог — вывод tools/forecast_world_bridge.py)
python tools/forecast_world_bridge.py --dataset oecd_2025 --partial \
  --out build/forecast_world/oecd_2025 --generated-at 2026-11-18T00:00:00Z
python tools/client_pack_v2.py --out build/client_pack/v2_8_bridge \
  --bridge build/forecast_world/oecd_2025 \
  --generated-at 2026-11-20T00:00:00Z --json
python tests/test_client_pack_v2.py
```

Демо-прогон (7 паспортов, 2 market-модуля, 2 forecast-контракта, без моста): `{"status": "OK",
"files": 8, "candidate": "ENSEMBLE_INVERSE_MSE", "candidate_status": "CANDIDATE", "c4_pass":
false, "c6_status": "NO_RESOLVED_FORECASTS", "modules": 7, "market_modules": 2, "forecasts": 2,
"forecast_targets": ["gas_ttf", "oil_brent"], "scenario": "hormuz_closed_6m", "api_version":
"v2", "sources": 6, "bridge": {"available": false, "scenarios": 0, "effects": 0}}`. Пакет:
`build/client_pack/v2_7`, digest документа
`dcc6c60a1afcad62184e848f59bafa81d5d0016dd7a2aedeadba7d5d0009bafc`.

Демо-прогон с мостом (OECD 2025, `PARTIAL_INPUTS`: T1 есть, T2/T3 нет): `sources: 7`,
`bridge: {"available": true, "scenarios": 3, "effects": 15, "sha256":
"29e01502c83dd569d5e221d940a9261ac3a761783fad078ee0fdb28f38671552"}`; пакет
`build/client_pack/v2_8_bridge`, digest `aef636636d3bd7950b948cdad6f472fdf9e1426e26e8e09786d85721d700462e`.

## 6. Отличия от прежних пакетов

| Пакет | Назначение | Отличие v2 |
|---|---|---|
| `client_pack.py` (v1) | внутренний пакет кандидата/оценки | v2 добавляет рыночный сценарий, паспорта модулей, API-срез и единый reproducibility-манифест |
| `market_client_pack.py` | рыночный пакет по нефти | v2 — мульти-модульный, включает product feed и API, но сохраняет тот же подход к детерминизму |
| `product_pack.py` | внутренний product feed | v2 — клиентская обёртка, ничего не пересчитывает |

## 7. Мост «прогноз → мир» (опциональный раздел Bridge)

Пакет умеет включать результат [tools/forecast_world_bridge.py](../tools/forecast_world_bridge.py)
(описание моста — [docs/178](178_Мост_прогноз_мир.md)): таблицу «сценарий → страна/отрасль» с
топ-эффектами, Δ выпуска, ценовым шагом, источником и SHA-256. Блок **необязательный**.

**Как включить**

- по умолчанию ищется `build/forecast_world_bridge/report.json`, затем `forecast_world.json` в той
  же папке;
- путь задаётся явно: `--bridge <файл|каталог>`; каталог — это выход инструмента моста
  (`build/forecast_world/<dataset>/`), файл — сам `forecast_world.json`/`report.json`;
- относительный путь разрешается от корня репозитория, затем от `--inputs-root`; `..` и пути вне
  этих корней отклоняются как небезопасные.

**Что даёт пакету**: продуктовый фид отвечает «какой прогноз», мост — «что это значит для
выпуска по странам и отраслям» в трёх объявленных сценариях (`base`, `inflation_plus_1pp`,
`growth_minus_1pp`). В пакет попадают только агрегаты и топ-ячейки, без сырых матриц.

**Валидация (явная, без «тихих» пропусков)**

| Ситуация | Поведение |
|---|---|
| артефакта нет (поиск по умолчанию) | `bridge.available=false`, `status=NOT_AVAILABLE`, причина + список `searched`; пакет собирается (fail-open только для этого опционального блока) |
| явный `--bridge` не существует / в каталоге нет `report.json`/`forecast_world.json` | `ClientPackError`, exit 2 (явный запрос не может «тихо» превратиться в пустой блок) |
| битый JSON, чужая схема (≠ `amfe.forecast_world_bridge.v1`), статус вне `RESEARCH_ONLY`/`PARTIAL_INPUTS`, `admission≠false`, пустые/дублирующиеся сценарии, нечисловые Δ | `ClientPackError`, exit 2, выходной каталог не создаётся (fail-closed) |
| сырые маркеры (`data/mrio`, ключи `Z`/`x`/`y`/…), нет `checks.no_raw_data=true` | fail-closed: в пакет не могут попасть raw-ряды |
| небезопасный путь (`..`, вне корней) | fail-closed |
| есть `<файл>.sha256` и он не совпадает с файлом | fail-closed; при совпадении `bridge.sha256_verified="sidecar"`, иначе `"file"` |

**Куда попадает**

| Формат | Что видно |
|---|---|
| `04_Machine_Readable.json` | `bridge`: `available`, `source`, `sha256`, `dataset`, `calibration` (`S_price`, `M`, pass-through), `scenarios[]` (статус, шаг CPI, Δ выпуска, % выпуска, интервал 5–95), `effects[]` (плоские строки «сценарий/страна/отрасль/Δ/доля»), `checks`, `risk_overlay`, `limitations`; артефакт добавляется в `sources` и в `07_Reproducibility.json` |
| `02_Numbers.xlsx` | листы `Bridge` (мета, SHA, причина при отсутствии), `Bridge_Scenarios`, `Bridge_Effects` (полный список топ-ячеек) |
| `01_Executive_Summary.docx` | таблица сценариев и топ-5 ячеек на сценарий (полный список — в XLSX/JSON) |
| `03_Dashboard.html` | те же две таблицы в статической панели |
| `README.md` | краткая версия + ограничение reduced-form |

**Ограничение — reduced-form.** Мост линеен по шоку и переиспользует уже опубликованные
калибровки: ценовой канал `quantity_bridge` (Leontief pass-through) и агрегатный спросовый
множитель Леонтьева. Аллокация страна×отрасль сепарабельная (внешнее произведение маргиналов),
кросс-взаимодействие не моделируется; отдельная ячейка — следствие маргиналов. Это
сравнительная статика, **не прогноз мира**, не поведенческая модель и не допуск; `T3` в числа не
входит (риск-контекст). Заявления о превосходстве не делаются, `admission=false`.
