# 171. Client Pack v2: единый клиентский пакет

Дата: 2026-11-17. Волна G5 (старт). Инструмент: [tools/client_pack_v2.py](../tools/client_pack_v2.py),
тесты: [tests/test_client_pack_v2.py](../tests/test_client_pack_v2.py) (29 тестов).
Источники чисел: `build/product/forecast_latest.json` (product feed), `protocols/module_registry`
(паспорта модулей), рыночный сценарий `hormuz_closed_6m` (oil v3), `build/api/index.json`
(forecast API v2), `build/registry/modules.jsonl`. Ничего не пересчитывается и не коммитится.

## 1. Что это

Client Pack v2 — один пакет для исследовательского клиента, который сводит воедино то, что
уже опубликовано раздельными инструментами (product pack, market client pack, API, реестр
модулей), и явно проговаривает границы: **ни одна модель не допущена**, кандидат остаётся в
статусе `CANDIDATE`, C4/C6 открыты, рыночные числа — сценарная арифметика над объявленными
допущениями, а не доверительный интервал или прогноз.

Состав (8 файлов):
- `01_Executive_Summary.docx` — ключевые числа только из артефактов + дисклеймер;
- `02_Numbers.xlsx` — кандидат/срез API, паспорта модулей, рыночный сценарий, источники;
- `03_Dashboard.html` — статическая панель без JS и внешних ресурсов;
- `04_Machine_Readable.json` — машиночитаемая версия с источником каждого блока;
- `05_Methodology_and_Sources.docx` — метод, перечень источников, детерминизм;
- `06_Licence_and_Disclaimer.docx` — лицензии/дисклеймеры `RESEARCH_ONLY`;
- `07_Reproducibility.json` — команда, хеши детерминированных артефактов, `file_count`;
- `README.md` — краткая инструкция.

## 2. Источники и честность чисел

| Блок | Источник | Примечание |
|---|---|---|
| Кандидат, точечный прогноз, 19 квантилей, C4/C6 | `build/product/forecast_latest.json` | статус `CANDIDATE`, C4 FAIL/открыт, C6 `NO_RESOLVED_FORECASTS` |
| Паспорта модулей | `build/registry/modules.jsonl` + реестр | 5 модулей, все `RESEARCH_ONLY`, code_sha256 file-level |
| Рыночный сценарий | oil v3 `hormuz_closed_6m` | пик/среднее Brent, units/assumptions, `RESEARCH_ONLY` |
| Forecast API | `build/api/index.json` | версия v2, модули/сценарии/эндпоинты |
| Лицензии | `protocols/licence_register.json` | ссылки на записи, без сырых значений |

Все числа пакета берутся из артефактов; пакет fail-closed: отсутствие любого входа — ошибка, а
не «пустой» документ.

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
- Пакет не заменяет независимую проверку/подпись; client-facing использование — после G5/G6.

## 5. Сборка и проверка

```bash
python tools/register_modules.py
python tools/product_pack.py
python tools/client_pack_v2.py --out build/client_pack_v2
python tests/test_client_pack_v2.py
```

Демо-прогон: `{"status": "OK", "files": 8, "candidate": "ENSEMBLE_INVERSE_MSE", "status":
"CANDIDATE", "c4_pass": false, "c6_status": "NO_RESOLVED_FORECASTS", "modules": 5,
"scenario": "hormuz_closed_6m", "api_version": "v2"}`.

## 6. Отличия от прежних пакетов

| Пакет | Назначение | Отличие v2 |
|---|---|---|
| `client_pack.py` (v1) | внутренний пакет кандидата/оценки | v2 добавляет рыночный сценарий, паспорта модулей, API-срез и единый reproducibility-манифест |
| `market_client_pack.py` | рыночный пакет по нефти | v2 — мульти-модульный, включает product feed и API, но сохраняет тот же подход к детерминизму |
| `product_pack.py` | внутренний product feed | v2 — клиентская обёртка, ничего не пересчитывает |
