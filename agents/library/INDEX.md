# Библиотека памяти AMFE

Обновлено: 2026-11-19T07:56:33Z · записей: 61

## По ролям

- `accounting-core` — 8 записей (`agents/memory/accounting-core.jsonl`)
- `baseline-models` — 5 записей (`agents/memory/baseline-models.jsonl`)
- `dense-vintages` — 10 записей (`agents/memory/dense-vintages.jsonl`)
- `evaluation-harness` — 6 записей (`agents/memory/evaluation-harness.jsonl`)
- `lead` — 8 записей (`agents/memory/lead.jsonl`)
- `licence-auditor` — 3 записей (`agents/memory/licence-auditor.jsonl`)
- `multiplicity-rw` — 7 записей (`agents/memory/multiplicity-rw.jsonl`)
- `protocol-reviewer` — 7 записей (`agents/memory/protocol-reviewer.jsonl`)
- `r2-completion` — 7 записей (`agents/memory/r2-completion.jsonl`)

## Открытые вопросы

- mem-lead-0003 (lead): Активация prereg 0.3 и потолок T1 C4

## Артефакты

- `docs/101_Кэш_прогонов.md` ← mem-dense-vintages-0007
- `docs/102_Горизонты_ансамблей.md` ← mem-multiplicity-rw-0002
- `docs/139_C6_календарь.md` ← mem-evaluation-harness-0003
- `docs/146_P2_0.2.md` ← mem-accounting-core-0001, mem-accounting-core-0002
- `docs/157_Реестр_модулей.md` ← mem-dense-vintages-0008
- `docs/161_Реестр_API_паспорта.md` ← mem-dense-vintages-0008
- `docs/162_Реестр_источников.md` ← mem-licence-auditor-0002
- `docs/164_План_и_статус_проекта.md` ← mem-baseline-models-0002, mem-lead-0002, mem-multiplicity-rw-0003
- `docs/166_Реальные_PIT_1990_1996.md` ← mem-r2-completion-0001, mem-r2-completion-0002
- `docs/168_Независимое_ревью_G3.md` ← mem-protocol-reviewer-0001, mem-protocol-reviewer-0003
- `docs/170_Оценка_по_предрегистрации_0_3.md` ← mem-evaluation-harness-0002, mem-lead-0002, mem-lead-0003, mem-protocol-reviewer-0002
- `docs/171_Client_Pack_v2.md` ← mem-dense-vintages-0001
- `docs/173_План_2.1_ревизия_идей_и_практический_маршрут.md` ← mem-dense-vintages-0003
- `docs/174_50_кейсов_использования.md` ← mem-accounting-core-0003, mem-baseline-models-0003, mem-licence-auditor-0003, mem-r2-completion-0003
- `docs/175_Агентная_оркестрация_и_обучение.md` ← mem-accounting-core-0001, mem-baseline-models-0001, mem-evaluation-harness-0001, mem-lead-0001, mem-licence-auditor-0001, mem-multiplicity-rw-0001, mem-protocol-reviewer-0001, mem-r2-completion-0001
- `docs/177_17_блоков_зрелость.md` ← mem-dense-vintages-0004
- `docs/183_Forecast_API_шлюз.md` ← mem-dense-vintages-0005
- `docs/184_FLAKS_Procurement_MVP.md` ← mem-dense-vintages-0006
- `docs/187_Страновой_прогноз.md` ← mem-r2-completion-0006
- `docs/189_FLAKS_пилот.md` ← mem-dense-vintages-0010
- `docs/190_Клиентский_пакет_Ормуз_США.md` ← mem-dense-vintages-0009
- `docs/97_Лицензии_ряды_и_terms.md` ← mem-licence-auditor-0002
- `pilot/flaks/.gitignore` ← mem-dense-vintages-0010
- `pilot/flaks/README.md` ← mem-dense-vintages-0010
- `pilot/flaks/contracts_template.csv` ← mem-dense-vintages-0010
- `pilot/flaks/data_dictionary.md` ← mem-dense-vintages-0010
- `pilot/flaks/pilot_plan_2_weeks.md` ← mem-dense-vintages-0010
- `pilot/flaks/suppliers_template.csv` ← mem-dense-vintages-0010
- `protocols/evaluation_protocol_v5.json` ← mem-evaluation-harness-0001
- `protocols/external_forecast_scorecard.json` ← mem-r2-completion-0004
- `protocols/horizon_matrix_v5.json` ← mem-multiplicity-rw-0004, mem-multiplicity-rw-0005, mem-multiplicity-rw-0006, mem-multiplicity-rw-0007
- `protocols/licence_register.json` ← mem-licence-auditor-0001
- `protocols/pit_prereg_evaluation_report.json` ← mem-evaluation-harness-0002
- `src/amfe/forecast/country.py` ← mem-r2-completion-0006
- `src/amfe/markets/storage.py` ← mem-accounting-core-0007
- `tests/test_client_pack_v2.py` ← mem-dense-vintages-0008
- `tests/test_country_forecast.py` ← mem-r2-completion-0006
- `tests/test_flaks_pilot.py` ← mem-dense-vintages-0010
- `tests/test_flaks_procurement.py` ← mem-dense-vintages-0006
- `tests/test_forecast_api_server.py` ← mem-dense-vintages-0005
- `tests/test_forecast_world_bridge.py` ← mem-accounting-core-0004
- `tests/test_hash_canonicalization.py` ← mem-dense-vintages-0007
- `tests/test_hormuz_us_client_pack.py` ← mem-dense-vintages-0009
- `tests/test_market_client_pack.py` ← mem-dense-vintages-0008
- `tests/test_module_registry.py` ← mem-dense-vintages-0008
- `tests/test_storage_futures.py` ← mem-accounting-core-0007
- `tools/c6_calendar.py` ← mem-evaluation-harness-0005, mem-evaluation-harness-0006
- `tools/country_forecast.py` ← mem-baseline-models-0005, mem-r2-completion-0006
- `tools/eval_cache.py` ← mem-r2-completion-0005
- `tools/external_forecast_scorecard.py` ← mem-r2-completion-0004
- `tools/fetch_country_data.py` ← mem-r2-completion-0006
- `tools/flaks_pilot.py` ← mem-dense-vintages-0010
- `tools/flaks_procurement.py` ← mem-dense-vintages-0006
- `tools/forecast_api_server.py` ← mem-dense-vintages-0005
- `tools/forecast_world_bridge.py` ← mem-accounting-core-0004, mem-accounting-core-0005, mem-dense-vintages-0007
- `tools/gas_scenarios.py` ← mem-accounting-core-0008
- `tools/geopolitics_scenarios.py` ← mem-protocol-reviewer-0006
- `tools/hormuz_us_client_pack.py` ← mem-dense-vintages-0009
- `tools/market_bridge.py` ← mem-dense-vintages-0007
- `tools/policy_scenarios.py` ← mem-baseline-models-0004
- `tools/quantity_bridge.py` ← mem-dense-vintages-0007
- `tools/register_modules.py` ← mem-dense-vintages-0008
- `tools/sector_exposure.py` ← mem-accounting-core-0006, mem-dense-vintages-0007
- `tools/storage_futures.py` ← mem-accounting-core-0007

## Как читать любому агенту/модели

1. Открыть `agents/README.md` и `agents/library/INDEX.md`.
2. Прочитать `agents/memory/<role>.jsonl` своей роли (JSONL, по строке на запись).
3. Сгенерировать контекст: `python agents/tools/memory.py pack --role <role>`.
4. После задачи добавить запись: `python agents/tools/memory.py add ...`.
5. Библиотекарь (`dense-vintages`) пересобирает индекс: `python agents/tools/memory.py index`.

Секреты и сырые данные не пишутся; только пути, SHA-256 и выводы.
