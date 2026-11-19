# 193. Как пользоваться AMFE: практическое руководство и кейсы

Дата: 2026-11-19. Для кого: аналитик, продакт, менеджер пилота, CFO/закупки клиента.
Статус продукта: **RESEARCH_ONLY, admission=false** — это сценарная аналитика, не прогноз и не
инвестиционная рекомендация. Все команды — из корня репозитория, PowerShell.

## 0. Быстрый старт за 5 минут

```powershell
# 1) сценарий Ормуз → США (база + стресс)
python tools/hormuz_us_impact.py --stress --out build/hormuz_us

# 2) клиентский пакет (PDF/Excel/JSON)
python tools/hormuz_us_client_pack.py --scenario stress --out build/client_pack/hormuz_us `
  --client "Demo Bank" --generated-at 2026-11-19T09:00:00Z

# 3) FLAKS: проверка данных и пилотный отчёт
python tools/flaks_pilot.py --suppliers pilot/flaks/suppliers_template.csv.tmpl `
  --contracts pilot/flaks/contracts_template.csv.tmpl --check
python tools/flaks_pilot.py --suppliers pilot/flaks/suppliers_template.csv.tmpl `
  --contracts pilot/flaks/contracts_template.csv.tmpl --scenario hormuz_6m `
  --out build/flaks_pilot_demo --generated-at 2026-11-19T00:00:00Z

# 4) локальное демо для показа
python tools/simulator_ui.py            # http://127.0.0.1:8090
```

Открыть: `build/hormuz_us/report.md`, `build/client_pack/hormuz_us/01_Executive_Summary.pdf`,
`build/flaks_pilot_demo/hormuz_6m/flaks_pilot_cfo.docx`.

## 1. Карта продуктов

| Что нужно | Инструмент | Результат |
|---|---|---|
| Цена нефти/Ормуз → США | `tools/hormuz_us_impact.py` | JSON/MD: Brent, бензин, дизель, керосин, CPI, домохозяйство, соцриск |
| Клиентский пакет по Ормузу | `tools/hormuz_us_client_pack.py` | DOCX/PDF, XLSX, JSON, HTML, manifest SHA-256 |
| Закупки/TCO/бюджет | `tools/flaks_pilot.py` | JSON, MD, CFO DOCX, manifest; quality/leak-scan |
| Газ/СПГ | `tools/gas_scenarios.py` | TTF/JKM/HH, ПХГ, диверсия, сценарии |
| Геополитика | `tools/geopolitics_scenarios.py` | 15 сценариев, Δвыпуска по странам/отраслям |
| Отрасли | `tools/sector_exposure.py` | макро-шок → отрасли/страны/компании |
| Запасы/фьючерсы | `tools/storage_futures.py` | contango/backwardation, 2020 |
| Политика | `tools/policy_scenarios.py` | ставка/дефицит/долг → T1/T3 |
| Страны EA/CN/GB | `tools/country_forecast.py` | квартальный прогноз, OOS |
| API | `tools/forecast_api_server.py` | REST, ключи, квоты |
| Client Pack v2 | `tools/client_pack_v2.py` | 7 паспортов, 2 market-модуля, газ |
| Демо | `tools/simulator_ui.py` | веб-форма, графики, JSON |

## 2. Кейс 1: CFO нефтегаза — бюджет при Ормузе

**Вопрос клиента:** «Что будет с выручкой/бюджетом, если Ормуз закроется на 6 месяцев?»

```powershell
python tools/hormuz_us_impact.py --stress --out build/hormuz_us
python tools/hormuz_us_client_pack.py --scenario stress --out build/client_pack/hormuz_us `
  --client "Татнефть (демо)" --generated-at 2026-11-19T09:00:00Z
```

**Что показать:**
1. Base: Brent пик **$168,9** (диапазон 139–269), бензин $5,45, дизель $6,62, CPI +3,91 п.п.
2. Stress: дизель **$8,04** (+112%), керосин **$6,93** (+177%), CPI **+5,62 п.п.**, соцриск 80/100.
3. Три решения: хеджирование, бюджетный резерв, приоритеты CAPEX; триггеры пересмотра.

**Что говорить:** «Это не прогноз. Это диапазон при явных допущениях; вот где мы можем
ошибиться и как это проверить».
**Что не говорить:** «будет точно $169», «вероятность 80%», «модель допущена».

**Время:** 10–15 минут; данные не нужны. Для клиентской версии — 1–2 дня на брендинг/проверку.

## 3. Кейс 2: закупки — TCO поставщиков и бюджет

**Вопрос:** «Кто дешевле по TCO и как изменится бюджет закупок?»

1. Скопировать шаблоны:
```powershell
Copy-Item pilot/flaks/suppliers_template.csv.tmpl suppliers.csv
Copy-Item pilot/flaks/contracts_template.csv.tmpl contracts.csv
```
2. Заполнить (16+9 полей, словарь — `pilot/flaks/data_dictionary.md`); имена/ID можно обезличить.
3. Проверить данные (ничего не пишет):
```powershell
python tools/flaks_pilot.py --suppliers suppliers.csv --contracts contracts.csv --check
```
4. Полный отчёт:
```powershell
python tools/flaks_pilot.py --suppliers suppliers.csv --contracts contracts.csv `
  --scenario hormuz_6m --out build/flaks_pilot --anonymize --generated-at 2026-11-19T00:00:00Z
```
**Выход:** `flaks_pilot.json`, `.md`, `flaks_pilot_cfo.docx`, `manifest.json`; quality report,
leak-scan PASS; base total, forecast total, delta %, лидер по TCO.

**Как читать:** TCO = цена + фрахт + пошлины + риск страны + качество + отсрочка; «дешёвая цена»
может проиграть. CAP/FLOOR показывают предел индексации.
**Ограничение:** демо-данные синтетические; реальные — только по NDA, не в git.

## 4. Кейс 3: газ/СПГ — шок трубы и зима

```powershell
python tools/gas_scenarios.py --list-scenarios
python tools/gas_scenarios.py --scenario eu_pipeline_shock_6m --out build/gas_scenarios
python tools/gas_scenarios.py --scenario hard_winter_combo --json
```

Показать: TTF base 11,16; шок трубы пик 20,13 (+80,4%); жёсткая зима+шок 28,53 (+155,7%);
диверсия 10,38 bcm/г; JKM 14,88. Для СПГ-проекта — чувствительность к фрахту/санкциям.
**Не говорить:** «TTF будет 28,5» — это сценарий, не прогноз.

## 5. Кейс 4: геополитика и отрасли

```powershell
python tools/geopolitics_scenarios.py --synthetic --json
python tools/geopolitics_scenarios.py --dataset figaro --out build/geopolitics
python tools/sector_exposure.py --dataset oecd_2025 --scenario inflation_plus_1pp --demo
```

Показать: кто выиграл/проиграл, Δвыпуска по странам/отраслям, асимметрию. Для клиента — «какие
страны/отрасли первыми почувствуют шок и где хеджировать».

## 6. Кейс 5: API, Client Pack и демо

**Локальное демо:**
```powershell
python tools/simulator_ui.py --require-auth `
  --api-keys "demo-pro-key-123:pro,demo-free-key-456:free" --quota-per-hour 60
```
Открыть `http://127.0.0.1:8090`, ввести `demo-pro-key-123`; сценарии: Ормуз base/stress, газ, гео.
Без `--require-auth` — открытое локальное демо.

**API-шлюз:**
```powershell
$env:AMFE_API_KEYS="demo_free_key_01:free,demo_pro_key_01:pro"
python tools/forecast_api_server.py --port 8080
# GET /v1/status, /v1/modules, /v1/scenarios, /v1/forecast?target=gas_ttf
```
**Client Pack:**
```powershell
python tools/client_pack_v2.py --out build/client_pack/v2_7 --generated-at 2026-11-19T12:00:00Z
```
7 паспортов, 2 market-модуля, 2 forecast-контракта; DOCX/XLSX/HTML/JSON.

## 7. Кейс 6: страновой прогноз (research)

```powershell
python tools/fetch_country_data.py
python tools/country_forecast.py --countries EA,CN,GB --targets GDP,CPI `
  --horizons 1,2,3,4 --frequency quarterly --require-real --out build/country_forecast_real
```
OOS: AR_BIC 0,969, PANEL_AR1 0,958 к random walk; выигрыш мал, PIT нет — только research.

## 8. Как читать статусы и ограничения

| Поле | Значение |
|---|---|
| `status` | `RESEARCH_ONLY`, `CANDIDATE`, `SHADOW_TEST`, `LIMITED_DEPLOYMENT` |
| `admission` | `false` — модель не допущена |
| `causal_claim` | `false` — не причинный вывод |
| `confidence_interval` | `false` — это диапазон чувствительности |
| `no_raw` | `true` — сырые ряды не отдаются |

**Всегда проверять:** версия, SHA-256, `generated_at`, допущения, окна. Если числа нужны для
внешнего документа — приложить manifest и оговорки.

## 9. Чек-лист перед клиентом

- [ ] Понятен вопрос клиента и решение, а не «модель».
- [ ] Данные клиента под NDA/обезличены; сырые не коммитятся.
- [ ] В пакете есть допущения и ограничения.
- [ ] Нет слов «прогноз», «вероятность», «допущена».
- [ ] Есть SHA-256/manifest и дата.
- [ ] Определён критерий успеха пилота (деньги/время/риск).

## 10. Частые ошибки

| Симптом | Причина | Что делать |
|---|---|---|
| `KeyError`/пустой отчёт | нет build/артефакта | пересобрать нужный tool/API |
| FLAKS exit 2 | битые/неполные данные | `--check`, исправить шаблон |
| API 401/403/429 | ключ/план/квота | проверить ключ, `X-API-Key`, лимит |
| PDF нет | нет LibreOffice | `--no-pdf` или установить LibreOffice |
| Числа не совпали | другая версия/дата | сверить `generated_at` и SHA |

## 11. Что дальше

1. Выбрать один кейс (Ормуз/закупки/газ) и прогнать на демо.
2. Собрать пакет под клиента (`--client`, брендинг).
3. Пилот: 4–6 недель, критерии успеха до старта.
